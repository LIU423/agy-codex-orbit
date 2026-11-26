"""Lightweight CLI wrapper to delegate scoped tasks to local OpenAI Codex CLI.

Invoked by Antigravity (Gemini) via `run_command`:
- Runs `codex exec --json` (new session) or `codex exec resume <session_id> --json` (follow-up round)
- Defaults to `gpt-6.2-sol` with explicit `model_reasoning_effort` (never silently escalating to Astra or ultra)
- Supports same-workspace serial handoff (`workspace-write`), `read-only` review, or parallel `worktree` isolation
- Sets Codex thread title with `agy: ` prefix via official `thread/name/set` and binds/creates Codex Desktop project
- Writes live delegation metadata + PID to `${PLUGIN_DATA}` for zero-token monitoring in Codex Orbit
- Prints ONLY a compact low-context JSON summary on exit (never raw command stdout)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
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
from server.delegation_store import DelegationStore
from server.models import IsolationMode
from server.routing_policy import (
    DEFAULT_ROUTING_MODE,
    ROUTING_MODE_LABELS,
    VALID_ROUTING_MODES,
    VALID_TASK_TIERS,
    RoutingMode,
    TaskTier,
    load_routing_mode,
    resolve_route,
    save_routing_mode,
)

DEFAULT_CODEX_MODEL = "gpt-6.2-sol"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MAX_FINAL_SUMMARY_CHARS = 1800
MAX_ERROR_CHARS = 600
MAX_CHANGED_FILES = 60


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


def _run_git(cwd: Path, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


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

    elif ev_type in ("turn.failed", "error"):
        err = rec.get("error") or rec.get("message")
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err, ensure_ascii=False)
        else:
            msg = str(err or "Codex turn failed")
        state["error"] = _clip(msg, MAX_ERROR_CHARS)


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if args.prompt is not None and args.prompt != "-":
        return args.prompt.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def run_delegation(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.set_routing_mode:
        saved = save_routing_mode(args.set_routing_mode, data_dir=args.data_dir)
        return 0, {
            "routing_mode": saved,
            "routing_mode_label": ROUTING_MODE_LABELS[saved],
        }

    if args.get_routing_mode:
        active = load_routing_mode(data_dir=args.data_dir)
        return 0, {
            "routing_mode": active,
            "routing_mode_label": ROUTING_MODE_LABELS[active],
        }

    if args.plan_route:
        active_mode: RoutingMode = args.routing_mode or load_routing_mode(data_dir=args.data_dir)
        tier: TaskTier = args.task_tier or "standard"
        decision = resolve_route(
            routing_mode=active_mode,
            task_tier=tier,
            isolation_mode=args.mode,
        )
        result = decision.to_dict()
        if decision.requires_confirmation:
            result["quota"] = query_codex_quota(codex_bin=args.codex_bin)
        return 0, result

    if args.check_quota:
        quota = query_codex_quota(codex_bin=args.codex_bin)
        return 0, quota

    prompt = _read_prompt(args)
    if not prompt:
        return 2, {"status": "failed", "error": "Empty prompt. Pass --prompt, --prompt-file, or stdin."}

    store = DelegationStore(data_dir=args.data_dir)
    existing_rec = store.get_by_session_id(args.session_id, verify_liveness=False) if args.session_id else None

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

    active_routing_mode: RoutingMode = args.routing_mode or load_routing_mode(data_dir=args.data_dir)
    task_tier: TaskTier = args.task_tier or "standard"
    route = resolve_route(
        routing_mode=active_routing_mode,
        task_tier=task_tier,
        isolation_mode=isolation_mode,
    )
    model = args.model or route.model
    reasoning_effort = args.reasoning_effort or route.reasoning_effort

    if args.title:
        agy_title = format_agy_title(args.title)
    elif existing_rec and existing_rec.title:
        agy_title = format_agy_title(existing_rec.title)
    else:
        agy_title = _derive_title_from_prompt(prompt)

    try:
        codex_bin = locate_codex_binary(args.codex_bin)
    except CodexBinaryNotFoundError as exc:
        return 2, {"status": "failed", "error": str(exc)}

    run_id = f"run-{uuid.uuid4().hex[:10]}"
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
    }

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(exec_cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    store.update_round(run_id, child_pid=proc.pid)

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass

    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                _parse_json_event(rec, state=state, store=store, run_id=run_id)
        exit_code = proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        exit_code = 124
        state["error"] = f"Codex delegation timed out after {args.timeout}s."
    except KeyboardInterrupt:
        proc.terminate()
        exit_code = 130
        state["error"] = "Delegation interrupted."

    stderr_text = ""
    try:
        stderr_text = proc.stderr.read().strip()
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
        tmp_dir.rmdir()
    except OSError:
        pass

    # Sync `agy:` thread title and Codex Desktop project binding once session_id is known.
    codex_project_info: dict[str, Any] | None = None
    title_sync_info: dict[str, Any] | None = None
    bound_sid = state["session_id"]
    if bound_sid and not args.no_desktop_project_sync:
        if round_rec.round_index == 1 or args.title:
            title_sync_info = set_codex_thread_name(
                bound_sid,
                agy_title,
                codex_bin=codex_bin,
                use_rpc=not args.codex_bin,
            )
        codex_project_info = ensure_codex_project_and_bind(
            workspace_dir,
            bound_sid,
            allow_create=True,
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
    parser.add_argument("--source-model", default="Gemini", help="Label of delegating model shown in Codex Orbit")
    parser.add_argument("--model", default=None, help=f"Codex model slug (default: {DEFAULT_CODEX_MODEL})")
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
        help="Evaluate active routing mode for --task-tier (and fetch quota if confirmation is required) without running Codex",
    )
    parser.add_argument("--title", default=None, help="Short session title (auto-prefixed with 'agy: ')")
    parser.add_argument("--prompt", default=None, help="Task prompt text (or '-' to read from stdin)")
    parser.add_argument("--prompt-file", default=None, help="Read task prompt from a UTF-8 file")
    parser.add_argument("--data-dir", default=None, help="Override Codex Orbit plugin data directory")
    parser.add_argument("--codex-bin", default=None, help="Override path to codex executable")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds (default: 1800)")
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
