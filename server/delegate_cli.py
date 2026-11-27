"""Lightweight CLI wrapper to delegate scoped tasks to local OpenAI Codex CLI.

Invoked by Antigravity (Gemini) via `run_command`:
- Runs `codex exec --json` (new session) or `codex exec resume <session_id> --json` (follow-up round)
- Selects the model and reasoning effort from persisted routing settings
- Supports same-workspace serial handoff (`workspace-write`), `read-only` review, or parallel `worktree` isolation
- Sets Codex thread title with `agy: ` prefix via official `thread/name/set` and binds/creates Codex Desktop project
- Writes live delegation metadata + PID to `${PLUGIN_DATA}` for zero-token monitoring in Codex Orbit
- Prints ONLY a compact low-context JSON summary on exit (never raw command stdout)
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from math import isfinite
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.codex_desktop import (
    ensure_codex_project_and_bind,
    format_agy_title,
    query_codex_quota,
    set_codex_thread_name,
)
from server.codex_locator import CodexBinaryNotFoundError, locate_codex_binary
from server.delegation_store import DelegationStore, prune_idle_worktrees
from server.delegation_store import run_git as _run_git
from server.doctor import run_doctor
from server.models import DelegationRoundRecord, DelegationSessionRecord, IsolationMode
from server.process_control import terminate_process_tree
from server.routing_policy import (
    DEFAULT_ROUTING_MODE,
    ROUTING_MODE_LABELS,
    VALID_ROUTING_MODES,
    VALID_TASK_TIERS,
    OrbitSettings,
    RouteDecision,
    RoutingMode,
    TaskTier,
    load_settings,
    resolve_route,
    save_settings,
)

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MAX_FINAL_SUMMARY_CHARS = 1800
MAX_ERROR_CHARS = 600
MAX_CHANGED_FILES = 60
DEFAULT_POST_TURN_GRACE_SECONDS = 15.0


def _clip(text: str, limit: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "... [truncated]"


def _derive_title_from_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^(?:goal|task|目标|任务)\s*[:：]\s*", "", stripped, flags=re.IGNORECASE).strip()
        if stripped:
            return format_agy_title(stripped[:60])
    return format_agy_title("Codex Task")


def _is_git_repo(cwd: Path) -> bool:
    proc = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def _prepare_worktree(
    workspace_dir: Path,
    store: DelegationStore,
    run_id: str,
    existing_worktree: str | None = None,
    existing_branch: str | None = None,
) -> tuple[Path, str | None]:
    if existing_worktree:
        existing_path = Path(existing_worktree)
        if existing_path.is_dir():
            return existing_path, existing_branch

    if not _is_git_repo(workspace_dir):
        raise RuntimeError(
            f"Isolation mode 'worktree' requires a Git repository, but '{workspace_dir}' is not one."
        )

    store.worktrees_dir.mkdir(parents=True, exist_ok=True)
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace_dir.name).strip("-") or "repo"
    branch_name = f"codex-orbit/{run_id}"
    wt_path = store.worktrees_dir / f"{safe_repo}-{run_id}"

    proc = _run_git(workspace_dir, ["worktree", "add", "-b", branch_name, str(wt_path), "HEAD"])
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to create git worktree at {wt_path}: {(proc.stderr or proc.stdout).strip()}"
        )
    return wt_path, branch_name


def _collect_git_changed_files(target_dir: Path) -> list[str]:
    if not _is_git_repo(target_dir):
        return []
    proc = _run_git(target_dir, ["status", "--porcelain"])
    if proc.returncode != 0:
        return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        if entry:
            files.append(entry)
    return files


def build_codex_command(
    *,
    codex_bin: Path,
    exec_cwd: Path,
    isolation_mode: IsolationMode,
    session_id: str | None,
    model: str | None,
    reasoning_effort: str | None,
    last_message_file: Path,
    skip_git_check: bool,
    web_search: bool = False,
) -> list[str]:
    prefix = [str(codex_bin)]
    if web_search:
        prefix.append("--search")

    if session_id:
        cmd = [*prefix, "exec", "resume", "--json", "-o", str(last_message_file)]
        if isolation_mode == "read-only":
            cmd.extend(["-c", 'sandbox_mode="read-only"'])
        elif isolation_mode in ("worktree", "workspace-write"):
            cmd.extend(["-c", 'sandbox_mode="workspace-write"'])
        if reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if model:
            cmd.extend(["-m", model])
        if skip_git_check:
            cmd.append("--skip-git-repo-check")
        cmd.extend([session_id, "-"])
        return cmd

    sandbox = "read-only" if isolation_mode == "read-only" else "workspace-write"
    cmd = [
        *prefix,
        "exec",
        "-C",
        str(exec_cwd),
        "-s",
        sandbox,
        "--json",
        "-o",
        str(last_message_file),
    ]
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    if model:
        cmd.extend(["-m", model])
    if skip_git_check:
        cmd.append("--skip-git-repo-check")
    cmd.append("-")
    return cmd


def _parse_json_event(
    rec: dict[str, Any],
    *,
    state: dict[str, Any],
    store: DelegationStore,
    run_id: str,
) -> None:
    ev_type = rec.get("type")

    thread_id = rec.get("thread_id") or rec.get("session_id")
    if not thread_id and isinstance(rec.get("thread"), dict):
        thread_id = rec["thread"].get("id")
    if isinstance(thread_id, str) and thread_id and not state["session_id"]:
        state["session_id"] = thread_id
        store.bind_session_id(run_id, thread_id)

    turn_id = rec.get("turn_id")
    if not turn_id and isinstance(rec.get("turn"), dict):
        turn_id = rec["turn"].get("id")
    if isinstance(turn_id, str) and turn_id and state["turn_id"] != turn_id:
        state["turn_id"] = turn_id
        store.update_round(run_id, turn_id=turn_id)

    item = rec.get("item") if isinstance(rec.get("item"), dict) else None
    if ev_type == "item.completed" and item is not None:
        itype = str(item.get("type") or "").lower()
        if itype in ("command_execution", "commandexecution"):
            state["command_count"] += 1
        elif itype in ("file_change", "filechange"):
            changes = item.get("changes")
            if isinstance(changes, list):
                for ch in changes:
                    if isinstance(ch, dict) and isinstance(ch.get("path"), str):
                        state["changed_files"].add(ch["path"])
            elif isinstance(changes, dict):
                for fp in changes.keys():
                    if isinstance(fp, str):
                        state["changed_files"].add(fp)
        elif itype in ("agent_message", "agentmessage"):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                state["last_agent_message"] = text.strip()
            elif isinstance(item.get("content"), list):
                joined = "\n".join(
                    c.get("text", "")
                    for c in item["content"]
                    if isinstance(c, dict) and isinstance(c.get("text"), str)
                ).strip()
                if joined:
                    state["last_agent_message"] = joined

    elif ev_type == "turn.completed":
        state["turn_completed"] = True
        if state["turn_finished_mono"] is None:
            state["turn_finished_mono"] = time.monotonic()

    elif ev_type in ("turn.failed", "error"):
        err = rec.get("error") or rec.get("message")
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
        else:
            msg = str(err or "Codex turn failed")
        state["error"] = _clip(msg, MAX_ERROR_CHARS)
        if ev_type == "turn.failed":
            state["turn_failed"] = True
            if state["turn_finished_mono"] is None:
                state["turn_finished_mono"] = time.monotonic()


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        try:
            return prompt_path.read_text(encoding="utf-8").strip()
        finally:
            if args.delete_prompt_file:
                with suppress(OSError):
                    prompt_path.unlink(missing_ok=True)
    if args.prompt is not None and args.prompt != "-":
        return args.prompt.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _remaining_5h(quota: dict[str, Any]) -> float | None:
    codex = quota.get("codex")
    primary = codex.get("primary") if isinstance(codex, dict) else None
    remaining = primary.get("remaining_percent") if isinstance(primary, dict) else None
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and isfinite(remaining):
        return float(remaining)
    return None


def _resolve_settings_route(
    settings: OrbitSettings,
    routing_mode: RoutingMode,
    task_tier: TaskTier,
    isolation_mode: str,
    quota_remaining_5h: float | None = None,
) -> RouteDecision:
    return resolve_route(
        routing_mode=routing_mode,
        task_tier=task_tier,
        isolation_mode=isolation_mode,
        default_model=settings.default_model,
        escalation_model=settings.escalation_model,
        quota_remaining_5h=quota_remaining_5h,
        quota_guardrail_enabled=settings.quota_guardrail_enabled,
        quota_guardrail_threshold_pct=settings.quota_guardrail_threshold_pct,
    )


def _plan_route(args: argparse.Namespace, settings: OrbitSettings) -> dict[str, Any]:
    active_mode: RoutingMode = args.routing_mode or settings.routing_mode
    tier: TaskTier = args.task_tier or "standard"
    remaining = args.quota_remaining_5h
    decision = _resolve_settings_route(settings, active_mode, tier, args.mode, remaining)
    quota = None
    if remaining is None and (
        decision.requires_confirmation
        or (active_mode == "silent_best" and settings.quota_guardrail_enabled and tier in ("standard", "complex"))
    ):
        quota = query_codex_quota(codex_bin=args.codex_bin)
        remaining = _remaining_5h(quota)
        if remaining is not None:
            decision = _resolve_settings_route(settings, active_mode, tier, args.mode, remaining)
    elif decision.guardrail_triggered:
        quota = {
            "codex": {
                "available": True,
                "source": "override",
                "primary": {"remaining_percent": remaining},
            }
        }
    result = decision.to_dict()
    if quota is not None:
        result["quota"] = quota
    return result


def run_delegation(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.doctor:
        report = run_doctor(data_dir=args.data_dir, codex_bin=args.codex_bin)
        return (0 if report["ok"] else 1), report

    if args.prune_worktrees:
        result = prune_idle_worktrees(DelegationStore(data_dir=args.data_dir))
        return (1 if result["errors"] else 0), result

    new_settings = {
        "default_model": args.set_default_model,
        "escalation_model": args.set_escalation_model,
        "quota_guardrail_enabled": None if args.set_quota_guardrail is None else args.set_quota_guardrail == "on",
        "quota_guardrail_threshold_pct": args.set_quota_threshold,
    }
    updates = {key: value for key, value in new_settings.items() if value is not None}
    if updates or args.set_routing_mode is not None:
        settings = save_settings(args.data_dir, routing_mode=args.set_routing_mode, **updates)
    else:
        settings = load_settings(args.data_dir)

    if args.get_settings or updates:
        return 0, settings.to_dict()

    if args.get_routing_mode or args.set_routing_mode:
        active = settings.routing_mode
        return 0, {"routing_mode": active, "routing_mode_label": ROUTING_MODE_LABELS[active]}

    if args.plan_route:
        return 0, _plan_route(args, settings)

    if args.check_quota:
        quota = query_codex_quota(codex_bin=args.codex_bin)
        return 0, quota

    store = DelegationStore(data_dir=args.data_dir)
    existing_rec = store.get_by_session_id(args.session_id, verify_liveness=False) if args.session_id else None
    reserved_round = None
    if args.run_id:
        if existing_rec and existing_rec.session_id == args.session_id:
            reserved_round = next((rnd for rnd in existing_rec.rounds if rnd.run_id == args.run_id), None)
        if reserved_round is None or reserved_round.status != "running" or reserved_round.pid is not None:
            return 2, {"status": "failed", "error": "No pending round for this run and session."}
        store.update_round(args.run_id, pid=os.getpid())

    try:
        code, summary = _execute_delegation(args, settings, store, existing_rec, reserved_round)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if reserved_round is None:
            raise
        code, summary = 2, {"status": "failed", "error": str(exc)}

    if code and reserved_round is not None:
        latest = store.get_by_run_id(reserved_round.run_id)
        if latest and any(rnd.run_id == reserved_round.run_id and rnd.status == "running" for rnd in latest.rounds):
            store.update_round(reserved_round.run_id, status="failed", error_summary=summary["error"], ended=True)
    return code, summary


def _execute_delegation(
    args: argparse.Namespace,
    settings: OrbitSettings,
    store: DelegationStore,
    existing_rec: DelegationSessionRecord | None,
    reserved_round: DelegationRoundRecord | None,
) -> tuple[int, dict[str, Any]]:
    prompt = _read_prompt(args)
    if not prompt:
        return 2, {"status": "failed", "error": "Empty prompt. Pass --prompt, --prompt-file, or stdin."}

    if args.cwd:
        workspace_dir = Path(args.cwd).expanduser().resolve()
    elif existing_rec is not None:
        workspace_dir = Path(existing_rec.workspace_dir).expanduser().resolve()
    else:
        workspace_dir = Path.cwd().resolve()

    if not workspace_dir.is_dir():
        return 2, {"status": "failed", "error": f"Workspace directory does not exist: {workspace_dir}"}

    isolation_mode: IsolationMode = args.mode
    if args.session_id and existing_rec is not None and not args.mode_explicit:
        isolation_mode = existing_rec.isolation_mode

    active_routing_mode: RoutingMode = args.routing_mode or settings.routing_mode
    task_tier: TaskTier = args.task_tier or "standard"

    if args.title:
        agy_title = format_agy_title(args.title)
    elif reserved_round and reserved_round.title:
        agy_title = reserved_round.title
    elif existing_rec and existing_rec.title:
        agy_title = format_agy_title(existing_rec.title)
    else:
        agy_title = _derive_title_from_prompt(prompt)

    try:
        codex_bin = locate_codex_binary(args.codex_bin)
    except CodexBinaryNotFoundError as exc:
        return 2, {"status": "failed", "error": str(exc)}

    remaining = None
    if (
        not args.dry_run
        and args.model is None
        and active_routing_mode == "silent_best"
        and settings.quota_guardrail_enabled
    ):
        remaining = _remaining_5h(query_codex_quota(codex_bin=codex_bin))
    route = _resolve_settings_route(settings, active_routing_mode, task_tier, isolation_mode, remaining)
    model = args.model or route.model
    reasoning_effort = args.reasoning_effort or route.reasoning_effort

    run_id = reserved_round.run_id if reserved_round else f"run-{uuid.uuid4().hex[:10]}"
    worktree_path: Path | None = None
    worktree_branch: str | None = None
    exec_cwd = workspace_dir

    if isolation_mode == "worktree":
        if args.dry_run:
            worktree_path = Path(existing_rec.worktree_path) if (existing_rec and existing_rec.worktree_path) else (
                store.worktrees_dir / f"{workspace_dir.name}-{run_id}"
            )
            worktree_branch = (existing_rec.worktree_branch if existing_rec else None) or f"codex-orbit/{run_id}"
            exec_cwd = worktree_path
        else:
            try:
                worktree_path, worktree_branch = _prepare_worktree(
                    workspace_dir,
                    store,
                    run_id,
                    existing_worktree=existing_rec.worktree_path if existing_rec else None,
                    existing_branch=existing_rec.worktree_branch if existing_rec else None,
                )
                exec_cwd = worktree_path
            except RuntimeError as exc:
                return 2, {"status": "failed", "error": str(exc)}

    skip_git_check = not _is_git_repo(exec_cwd) if not args.dry_run else False
    tmp_dir = Path(tempfile.mkdtemp(prefix="codex-orbit-"))
    last_msg_file = tmp_dir / "last_message.txt"

    cmd = build_codex_command(
        codex_bin=codex_bin,
        exec_cwd=exec_cwd,
        isolation_mode=isolation_mode,
        session_id=args.session_id,
        model=model,
        reasoning_effort=reasoning_effort,
        last_message_file=last_msg_file,
        skip_git_check=skip_git_check,
        web_search=args.web_search,
    )

    if args.dry_run:
        return 0, {
            "status": "dry_run",
            "run_id": run_id,
            "title": agy_title,
            "routing_mode": active_routing_mode,
            "task_tier": task_tier,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "web_search": bool(args.web_search),
            "codex_bin": str(codex_bin),
            "workspace_dir": str(workspace_dir),
            "exec_cwd": str(exec_cwd),
            "isolation_mode": isolation_mode,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "worktree_branch": worktree_branch,
            "data_dir": str(store.data_dir),
            "desktop_project_sync": not args.no_desktop_project_sync,
            "command": cmd,
        }

    if reserved_round is not None:
        record = store.update_round(
            run_id,
            model=model,
            reasoning_effort=reasoning_effort,
            worktree_path=str(worktree_path) if worktree_path else None,
            worktree_branch=worktree_branch,
        )
        round_rec = next((rnd for rnd in record.rounds if rnd.run_id == run_id), None) if record else None
        if round_rec is None or round_rec.status != "running":
            tmp_dir.rmdir()
            return 1, {"status": "cancelled", "error": "Follow-up stopped before launch."}
    else:
        _, round_rec = store.start_round(
            run_id=run_id,
            workspace_dir=str(workspace_dir),
            prompt=prompt,
            isolation_mode=isolation_mode,
            source_model=args.source_model,
            title=agy_title,
            session_id=args.session_id,
            worktree_path=str(worktree_path) if worktree_path else None,
            worktree_branch=worktree_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            pid=os.getpid(),
        )

    start_mono = time.perf_counter()
    state: dict[str, Any] = {
        "session_id": args.session_id,
        "turn_id": None,
        "command_count": 0,
        "changed_files": set(),
        "last_agent_message": "",
        "error": None,
        "turn_completed": False,
        "turn_failed": False,
        "turn_finished_mono": None,
    }

    # Redirect stderr to a temp file rather than PIPE to prevent Windows 4 KB
    # anonymous pipe buffer deadlocks when Codex emits verbose warnings/subagent logs.
    stderr_file = tmp_dir / "stderr.log"
    stderr_fh = stderr_file.open("w+", encoding="utf-8", errors="replace")

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(exec_cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_fh,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    store.update_round(run_id, child_pid=proc.pid)

    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        proc.stdin.write(prompt)
        proc.stdin.flush()
        proc.stdin.close()
    except OSError:
        pass

    line_queue: queue.Queue[str | None] = queue.Queue()
    early_sync_lock = threading.Lock()
    early_sync_started = False
    early_title_info: dict[str, Any] | None = None
    early_project_info: dict[str, Any] | None = None

    def _trigger_early_desktop_sync(bound_session_id: str) -> None:
        nonlocal early_sync_started
        if args.no_desktop_project_sync or not bound_session_id:
            return
        with early_sync_lock:
            if early_sync_started:
                return
            early_sync_started = True

        def _worker() -> None:
            nonlocal early_title_info, early_project_info
            try:
                t_info: dict[str, Any] | None = None
                if round_rec.round_index == 1 or args.title:
                    t_info = set_codex_thread_name(
                        bound_session_id,
                        agy_title,
                        codex_bin=codex_bin,
                        use_rpc=not args.codex_bin,
                    )
                p_info = ensure_codex_project_and_bind(
                    workspace_dir,
                    bound_session_id,
                    codex_bin=codex_bin,
                    allow_create=True,
                    use_rpc=not args.codex_bin,
                    promote_source=False,
                )
                with early_sync_lock:
                    early_title_info = t_info
                    early_project_info = p_info
            except Exception:
                pass

        threading.Thread(target=_worker, name="codex-desktop-early-sync", daemon=True).start()

    def _read_stdout_lines() -> None:
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line_queue.put(raw_line)
        except OSError:
            pass
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=_read_stdout_lines, name="codex-stdout-reader", daemon=True)
    reader.start()

    post_turn_grace = max(0.5, float(getattr(args, "post_turn_grace", DEFAULT_POST_TURN_GRACE_SECONDS)))
    hard_deadline = time.monotonic() + max(1, int(args.timeout))
    last_msg_seen_mono: float | None = None
    last_stdout_mono = time.monotonic()
    forced_post_turn_exit = False
    warning_note: str | None = None
    exit_code: int = 1

    try:
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                terminate_process_tree(proc.pid, wait_seconds=2.0)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                exit_code = 124
                state["error"] = f"Codex delegation timed out after {args.timeout}s."
                break

            if state["turn_finished_mono"] is not None:
                if (now - state["turn_finished_mono"]) >= post_turn_grace:
                    forced_post_turn_exit = True
                    break
            elif last_msg_file.is_file():
                try:
                    if last_msg_file.stat().st_size > 0:
                        if last_msg_seen_mono is None:
                            last_msg_seen_mono = now
                        elif (now - last_msg_seen_mono) >= post_turn_grace and (now - last_stdout_mono) >= post_turn_grace:
                            forced_post_turn_exit = True
                            break
                except OSError:
                    pass

            try:
                raw_item = line_queue.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None and not reader.is_alive():
                    break
                continue

            if raw_item is None:
                break
            last_stdout_mono = time.monotonic()
            line = raw_item.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                _parse_json_event(rec, state=state, store=store, run_id=run_id)
                if state["session_id"] and not early_sync_started:
                    _trigger_early_desktop_sync(state["session_id"])

        if forced_post_turn_exit:
            terminate_process_tree(proc.pid, wait_seconds=2.0)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            has_output_msg = bool(state["last_agent_message"]) or (
                last_msg_file.is_file() and last_msg_file.stat().st_size > 0
            )
            if (state["turn_completed"] or has_output_msg) and not state["turn_failed"] and not state["error"]:
                exit_code = 0
                warning_note = (
                    "Codex CLI did not exit after completing the turn; lingering process tree was cleaned up automatically."
                )
            else:
                exit_code = proc.returncode if proc.returncode is not None else 1
        elif exit_code != 124:
            remaining = max(1.0, min(post_turn_grace, hard_deadline - time.monotonic()))
            try:
                exit_code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc.pid, wait_seconds=2.0)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                has_output_msg = bool(state["last_agent_message"]) or (
                    last_msg_file.is_file() and last_msg_file.stat().st_size > 0
                )
                if (state["turn_completed"] or has_output_msg) and not state["turn_failed"] and not state["error"]:
                    exit_code = 0
                    warning_note = (
                        "Codex CLI closed stdout but did not exit in time; lingering process tree was cleaned up automatically."
                    )
                else:
                    exit_code = 124
                    state["error"] = f"Codex delegation timed out waiting for process exit."
    except KeyboardInterrupt:
        terminate_process_tree(proc.pid, wait_seconds=1.5)
        exit_code = 130
        state["error"] = "Delegation interrupted."
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            stderr_fh.close()
        except OSError:
            pass

    stderr_text = ""
    try:
        if stderr_file.is_file():
            raw_err = stderr_file.read_text(encoding="utf-8", errors="replace").strip()
            stderr_text = raw_err[-4000:] if len(raw_err) > 4000 else raw_err
    except OSError:
        pass

    duration_ms = int((time.perf_counter() - start_mono) * 1000)

    final_msg = state["last_agent_message"]
    if not final_msg and last_msg_file.is_file():
        try:
            final_msg = last_msg_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
    try:
        last_msg_file.unlink(missing_ok=True)
        stderr_file.unlink(missing_ok=True)
        tmp_dir.rmdir()
    except OSError:
        pass

    # Sync `agy:` thread title and Codex Desktop project binding once session_id is known.
    # Reuses early sync if already completed, and performs idempotent final confirmation so
    # threads created mid-run are guaranteed bound even if SQLite hadn't flushed at thread.started.
    with early_sync_lock:
        codex_project_info: dict[str, Any] | None = early_project_info
        title_sync_info: dict[str, Any] | None = early_title_info
    bound_sid = state["session_id"]
    if bound_sid and not args.no_desktop_project_sync:
        if (round_rec.round_index == 1 or args.title) and not (title_sync_info and title_sync_info.get("via_rpc")):
            title_sync_info = set_codex_thread_name(
                bound_sid,
                agy_title,
                codex_bin=codex_bin,
                use_rpc=not args.codex_bin,
            )
        # Always run idempotent final sync after Codex exits so the closed rollout's
        # `session_meta.source` is promoted safely for Codex Desktop visibility.
        codex_project_info = ensure_codex_project_and_bind(
            workspace_dir,
            bound_sid,
            codex_bin=codex_bin,
            allow_create=True,
            use_rpc=not args.codex_bin,
            promote_source=True,
        )

    changed_files_set: set[str] = set(state["changed_files"])
    if isolation_mode != "read-only":
        changed_files_set.update(_collect_git_changed_files(exec_cwd))
    changed_files = sorted(changed_files_set)[:MAX_CHANGED_FILES]

    latest_rec = store.get_by_run_id(run_id)
    latest_rnd = next((r for r in (latest_rec.rounds if latest_rec else []) if r.run_id == run_id), None)
    was_cancelled = latest_rnd is not None and latest_rnd.status == "cancelled"

    if was_cancelled:
        final_status = "cancelled"
        error_summary = latest_rnd.error_summary or "Cancelled by user."
    elif exit_code == 0 and not state["error"]:
        final_status = "completed"
        error_summary = None
    else:
        final_status = "failed"
        error_summary = state["error"] or _clip(stderr_text, MAX_ERROR_CHARS) or f"Codex exited with code {exit_code}."

    clipped_final = _clip(final_msg, MAX_FINAL_SUMMARY_CHARS)

    store.update_round(
        run_id,
        status=final_status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        command_count=state["command_count"],
        changed_files=changed_files,
        final_message=clipped_final,
        error_summary=error_summary,
        ended=True,
    )

    summary = {
        "status": final_status,
        "run_id": run_id,
        "session_id": bound_sid,
        "title": agy_title,
        "round": round_rec.round_index,
        "source_model": args.source_model,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "guardrail_triggered": route.guardrail_triggered,
        "guardrail_note": route.guardrail_note,
        "web_search": bool(args.web_search),
        "isolation_mode": isolation_mode,
        "workspace_dir": str(workspace_dir),
        "worktree_path": str(worktree_path) if worktree_path else None,
        "worktree_branch": worktree_branch,
        "duration_ms": duration_ms,
        "command_count": state["command_count"],
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "codex_project": codex_project_info,
        "title_sync": title_sync_info,
        "final_summary": clipped_final,
        "warning": warning_note,
        "error": error_summary,
    }
    return (0 if final_status == "completed" else 1), summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delegate a scoped task to local OpenAI Codex CLI")
    parser.add_argument("--cwd", default=None, help="Target repository/workspace directory")
    parser.add_argument(
        "--mode",
        choices=("read-only", "worktree", "workspace-write"),
        default=None,
        help="Execution mode (default: workspace-write for serial same-workspace handoff, inherited on resume)",
    )
    parser.add_argument("--session-id", default=None, help="Existing Codex session UUID for multi-turn follow-up")
    parser.add_argument("--run-id", help="Use a pending delegation round already registered for --session-id")
    parser.add_argument("--source-model", default="Gemini", help="Label of delegating model shown in Codex Orbit")
    parser.add_argument("--model", default=None, help="Override the model selected by the routing policy")
    parser.add_argument(
        "--reasoning-effort",
        choices=VALID_EFFORTS,
        default=None,
        help="Codex reasoning effort override (otherwise chosen by routing policy & task tier)",
    )
    parser.add_argument(
        "--task-tier",
        choices=VALID_TASK_TIERS,
        default="standard",
        help="Task complexity tier for routing policy: light, standard, complex (default: standard)",
    )
    parser.add_argument(
        "--routing-mode",
        choices=VALID_ROUTING_MODES,
        default=None,
        help=f"Override routing mode for this invocation (default: persisted setting or {DEFAULT_ROUTING_MODE})",
    )
    parser.add_argument(
        "--get-routing-mode",
        action="store_true",
        help="Print current persisted routing mode and exit",
    )
    parser.add_argument(
        "--set-routing-mode",
        choices=VALID_ROUTING_MODES,
        default=None,
        help="Persist routing mode (silent_low_cost, ask, silent_best) and exit",
    )
    parser.add_argument(
        "--plan-route",
        action="store_true",
        help="Evaluate the route for --task-tier and check quota when needed without running a task",
    )
    parser.add_argument("--get-settings", action="store_true", help="Print persisted Orbit settings and exit")
    parser.add_argument("--set-default-model", help="Persist the default model slug (empty resets the default)")
    parser.add_argument("--set-escalation-model", help="Persist the escalation model slug (empty resets the default)")
    parser.add_argument("--set-quota-guardrail", choices=("on", "off"), help="Enable or disable the quota guardrail")
    parser.add_argument("--set-quota-threshold", type=int, help="Set the quota guardrail threshold percent (5 to 50)")
    parser.add_argument("--quota-remaining-5h", type=float, help="Override remaining quota percent for --plan-route")
    parser.add_argument("--doctor", action="store_true", help="Check the Orbit installation and Codex runtime")
    parser.add_argument("--prune-worktrees", action="store_true", help="Remove idle Orbit worktrees")
    parser.add_argument("--title", default=None, help="Short session title (auto-prefixed with 'agy: ')")
    parser.add_argument("--prompt", default=None, help="Task prompt text (or '-' to read from stdin)")
    parser.add_argument("--prompt-file", default=None, help="Read task prompt from a UTF-8 file")
    parser.add_argument("--delete-prompt-file", action="store_true", help="Delete --prompt-file after reading it")
    parser.add_argument("--data-dir", default=None, help="Override Codex Orbit plugin data directory")
    parser.add_argument("--codex-bin", default=None, help="Override path to codex executable")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds (default: 1800)")
    parser.add_argument(
        "--post-turn-grace",
        type=float,
        default=DEFAULT_POST_TURN_GRACE_SECONDS,
        help="Seconds to wait for Codex CLI to exit after turn.completed before cleaning up lingering process tree",
    )
    parser.add_argument(
        "--no-desktop-project-sync",
        action="store_true",
        help="Skip Codex Desktop project binding and thread naming",
    )
    parser.add_argument(
        "--check-quota",
        action="store_true",
        help="Query live Codex remaining quota and reset times without executing a task",
    )
    parser.add_argument(
        "--web-search",
        action="store_true",
        help="Enable Codex live web search (`codex --search`) for up-to-date external research tasks",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print resolved invocation plan without executing Codex")
    args = parser.parse_args(argv)
    if args.run_id and (not args.session_id or args.dry_run):
        parser.error("--run-id requires --session-id and an executing delegation")
    if args.delete_prompt_file and not args.prompt_file:
        parser.error("--delete-prompt-file requires --prompt-file")
    if args.quota_remaining_5h is not None and not isfinite(args.quota_remaining_5h):
        parser.error("--quota-remaining-5h must be a finite number")
    args.mode_explicit = args.mode is not None
    if args.mode is None:
        args.mode = "workspace-write"
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    code, summary = run_delegation(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
