"""Codex Orbit native Antigravity MCP App server (stdio)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

# Ensure repository root is importable when launched directly via script path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from server.codex_desktop import (
    query_codex_quota,
    read_cached_codex_quota,
    read_codex_model_catalog,
    read_rollout_fallback_quota_status,
)
from server.delegation_store import (
    DelegationStore,
    _atomic_write_json,
    prune_idle_worktrees,
    resolve_data_dir,
    runtime_pointer_path,
    utc_now_iso,
)
from server.doctor import run_doctor
from server.models import IsolationMode
from server.overview_builder import build_profile_overview
from server.process_control import cancel_delegation
from server.routing_policy import (
    DEFAULT_REASONING_EFFORT,
    ROUTING_MODE_LABELS,
    VALID_REASONING_EFFORT_SETTINGS,
    OrbitSettings,
    _normalize_reasoning_effort,
    load_settings,
    save_routing_mode,
    save_settings,
    save_ui_locale,
)
from server.session_store import SessionStore

APP_URI = "ui://codex-orbit/app.html"
APP_MIME = "text/html;profile=mcp-app"
QUOTA_MAX_AGE_SECONDS = 60.0

PANE_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Codex Orbit"},
            {"type": "global", "title": "Codex Orbit"},
        ]
    },
}

APP_ONLY_META: dict[str, Any] = {
    "ui": {"visibility": ["app"]},
}


def _resolve_html_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env_html = os.environ.get("CODEX_ORBIT_APP_HTML")
    if env_html:
        return Path(env_html)
    candidates = [
        _REPO_ROOT / "dist" / "app.html",
        _REPO_ROOT / "dist" / "index.html",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _write_runtime_pointer(data_dir: Path | None) -> None:
    if data_dir is None:
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            runtime_pointer_path(),
            {
                "data_dir": str(data_dir.resolve()),
                # POSIX venv executables can be symlinks to the base interpreter.
                "python_bin": str(Path(sys.executable).absolute()),
                "delegate_cli": str((_REPO_ROOT / "server" / "delegate_cli.py").resolve()),
            },
        )
    except OSError:
        # Non-fatal: server continues operating with its configured data_dir.
        pass


def _reap_delegation(process: subprocess.Popen, prompt_path: Path) -> None:
    process.wait()
    with suppress(OSError):
        prompt_path.unlink(missing_ok=True)


def _read_projects(path: Path) -> list[dict[str, str]]:
    """Read the saved project entries, keeping invalid files intact for recovery."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, list) or any(
        not isinstance(project, dict) or not all(
            isinstance(project.get(key), str) for key in ("project_dir", "project_name", "created_at")
        )
        for project in projects
    ):
        raise ValueError("Invalid projects.json format.")
    return projects


def _resolve_workspace_dir(workspace_dir: str, *, create_if_missing: bool = False) -> Path:
    """Expand and resolve a workspace path, optionally creating its directory."""
    if not workspace_dir.strip():
        raise ValueError("Workspace directory is required.")
    resolved = Path(workspace_dir.strip()).expanduser().resolve()
    if create_if_missing and not resolved.exists():
        resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError(f"Workspace path is not an existing directory: {resolved}")
    return resolved


def create_server(
    *,
    sessions_dir: Path | None = None,
    session_index_path: Path | None = None,
    html_path: Path | None = None,
    data_dir: Path | None = None,
    write_pointer: bool = False,
    warmup: bool = False,
) -> MCPServer:
    resolved_data_dir = resolve_data_dir(data_dir)
    if write_pointer:
        _write_runtime_pointer(resolved_data_dir)

    delegation_store = DelegationStore(data_dir=resolved_data_dir)
    store = SessionStore(
        sessions_dir=sessions_dir,
        session_index_path=session_index_path,
        delegation_store=delegation_store,
    )
    mcp = MCPServer(
        name="Codex Orbit",
        version="0.1.0",
    )
    quota_cache: dict[str, Any] = {"ts": 0.0, "data": None}
    quota_refresh_lock = threading.Lock()
    quota_refresh_in_flight = False
    followup_lock = threading.Lock()
    projects_path = resolved_data_dir / "projects.json"
    projects_lock = threading.Lock()

    def _query_live_quota() -> dict[str, Any]:
        data = query_codex_quota(
            codex_home=store.sessions_dir.parent,
            data_dir=resolved_data_dir,
            use_cache=False,
        )
        with quota_refresh_lock:
            quota_cache.update(ts=time.monotonic(), data=data)
        return data

    def _trigger_quota_refresh_async() -> None:
        def refresh() -> None:
            nonlocal quota_refresh_in_flight
            try:
                _query_live_quota()
            finally:
                with quota_refresh_lock:
                    quota_refresh_in_flight = False

        nonlocal quota_refresh_in_flight
        with quota_refresh_lock:
            if quota_refresh_in_flight or (
                quota_cache["data"] is not None and time.monotonic() - quota_cache["ts"] < QUOTA_MAX_AGE_SECONDS
            ):
                return
            quota_refresh_in_flight = True
            try:
                threading.Thread(target=refresh, daemon=True).start()
            except RuntimeError:
                quota_refresh_in_flight = False

    def _read_quota_without_live_query() -> tuple[dict[str, Any] | None, bool]:
        with quota_refresh_lock:
            memory_data = quota_cache["data"]
            memory_recent = time.monotonic() - quota_cache["ts"] < QUOTA_MAX_AGE_SECONDS
        memory_codex = memory_data.get("codex", {}) if memory_data is not None else {}
        memory_available = memory_codex.get("available") is True
        # A live query can itself fall back to historical rollout data.
        if memory_available and memory_recent and memory_codex.get("source") != "rollout_cache":
            return memory_data, True
        disk_cache = read_cached_codex_quota(data_dir=resolved_data_dir, allow_stale=True)
        if disk_cache is not None and disk_cache[1]:
            return disk_cache
        if memory_available:
            return memory_data, False
        if disk_cache is not None:
            return disk_cache
        fallback = read_rollout_fallback_quota_status(store.sessions_dir.parent)
        return (fallback if fallback is not None else memory_data), False

    @mcp.resource(
        APP_URI,
        name="Codex Orbit",
        description="Real-time monitor for local OpenAI Codex sessions.",
        mime_type=APP_MIME,
        meta={"ui": {"prefersBorder": False}},
    )
    def codex_orbit_app_html() -> str:
        target = _resolve_html_path(html_path)
        if not target.exists():
            return (
                "<!doctype html><html><body style='font-family:sans-serif;padding:16px'>"
                "<h3>Codex Orbit UI bundle not found</h3>"
                f"<p>Expected built single-file HTML at <code>{target}</code>. "
                "Run <code>pnpm run build</code> inside <code>ui/</code>.</p>"
                "</body></html>"
            )
        return target.read_text(encoding="utf-8")

    @mcp.tool(
        name="open_codex_orbit",
        title="Codex Orbit",
        description=(
            "Open or focus the Codex Orbit panel to monitor local OpenAI Codex sessions in real time. "
            "Pass focus_running=True when dispatching a task so the right auxiliary pane shows live progress automatically."
        ),
        meta=PANE_META,
        structured_output=False,
    )
    def open_codex_orbit(
        session_id: str | None = None,
        query: str | None = None,
        focus_running: bool = False,
        workspace_dir: str | None = None,
    ) -> types.CallToolResult:
        resolved_session_id = session_id
        norm_target = os.path.normcase(os.path.normpath(workspace_dir)) if workspace_dir else None
        if resolved_session_id is None and focus_running:
            for delegation in delegation_store.load_all(verify_liveness=True):
                if delegation.rounds and delegation.rounds[-1].status == "running" and (
                    norm_target is None
                    or os.path.normcase(os.path.normpath(delegation.workspace_dir)) == norm_target
                ):
                    resolved_session_id = delegation.session_id or delegation.record_id
                    break
        if resolved_session_id is None and focus_running:
            sessions = store.list_sessions(limit=50)
            running = [s for s in sessions if s.status == "running"]
            if norm_target is not None:
                matched = [
                    s
                    for s in running
                    if s.project_dir
                    and os.path.normcase(os.path.normpath(s.project_dir)) == norm_target
                ]
                if matched:
                    running = matched
            if running:
                running.sort(key=lambda s: (0 if s.is_delegated else 1))
                resolved_session_id = running[0].session_id

        bootstrap = {
            "initial_session_id": resolved_session_id,
            "initial_query": query,
            "focus_running": bool(focus_running),
            "preferred_workspace": workspace_dir,
            "sessions_dir": str(store.sessions_dir),
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Opened Codex Orbit.")],
            structured_content=bootstrap,
        )

    def _session_list_payload(query: str, limit: int) -> dict[str, Any]:
        clamped_limit = max(1, min(limit, 200))
        sessions = store.list_sessions(query=query, limit=clamped_limit)
        projects = []
        with suppress(OSError, ValueError):
            projects = _read_projects(projects_path)
        q = query.strip().casefold()
        if q:
            projects = [
                project for project in projects
                if q in project["project_dir"].casefold() or q in project["project_name"].casefold()
            ]
        return {
            "sessions": [s.model_dump(mode="json") for s in sessions],
            "projects": projects,
            "sessions_dir": str(store.sessions_dir),
            "sessions_dir_exists": store.sessions_dir.exists() or len(sessions) > 0,
        }

    @mcp.tool(
        name="orbit_list_sessions",
        title="List Codex sessions",
        description="App-only tool: list local Codex sessions and active delegations with optional search filter.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_list_sessions(query: str = "", limit: int = 80) -> types.CallToolResult:
        payload = {**_session_list_payload(query, limit), **load_settings(resolved_data_dir).to_dict()}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{len(payload['sessions'])} sessions")],
            structured_content=payload,
        )

    @mcp.tool(
        name="orbit_create_project",
        title="Create Orbit project",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_create_project(workspace_dir: str, create_if_missing: bool = False) -> types.CallToolResult:
        """Add a workspace directory to Orbit's project list."""
        try:
            resolved = _resolve_workspace_dir(workspace_dir, create_if_missing=create_if_missing)
            project_dir = str(resolved)
            project_name = resolved.name or project_dir
            project_key = os.path.normcase(os.path.normpath(project_dir))
            with projects_lock:
                projects = _read_projects(projects_path)
                if not any(os.path.normcase(os.path.normpath(p["project_dir"])) == project_key for p in projects):
                    projects.append({
                        "project_dir": project_dir,
                        "project_name": project_name,
                        "created_at": utc_now_iso(),
                    })
                    _atomic_write_json(projects_path, {"projects": projects})
        except (OSError, RuntimeError, ValueError) as exc:
            error = str(exc)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=error)],
                structured_content={"created": False, "error": error},
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Project added.")],
            structured_content={"created": True, "project_dir": project_dir, "project_name": project_name},
        )

    @mcp.tool(
        name="orbit_get_session_detail",
        title="Get Codex session detail",
        description="App-only tool: fetch full parsed detail, delegation metadata, and current byte cursor.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_get_session_detail(session_id: str) -> types.CallToolResult:
        detail, meta_ts = store.get_session_detail(session_id)
        payload = {
            "found": detail is not None,
            "meta_ts": meta_ts,
            "detail": detail.model_dump(mode="json") if detail is not None else None,
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="ok" if detail else "not found")],
            structured_content=payload,
        )

    @mcp.tool(
        name="orbit_poll_session_delta",
        title="Poll Codex session delta",
        description="App-only tool: incrementally read appended rollout records or delegation updates.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_poll_session_delta(
        session_id: str,
        after_cursor: int = 0,
        after_meta_ts: str = "",
    ) -> types.CallToolResult:
        delta, meta_ts = store.poll_session_delta(
            session_id,
            after_cursor=max(0, after_cursor),
            after_meta_ts=after_meta_ts,
        )
        payload = {
            "found": delta is not None,
            "meta_ts": meta_ts,
            "delta": delta.model_dump(mode="json") if delta is not None else None,
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="ok" if delta else "not found")],
            structured_content=payload,
        )

    def launch_error(error: str, *, busy: bool = False) -> types.CallToolResult:
        payload = {"started": False, "error": error}
        if busy:
            payload["busy"] = True
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=error)],
            structured_content=payload,
        )

    def launch_delegation(
        *,
        workspace_dir: Path,
        prompt: str,
        isolation_mode: IsolationMode,
        title: str,
        web_search: bool,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
    ) -> types.CallToolResult:
        """Reserve a round before starting its detached CLI wrapper."""
        normalized = (
            DEFAULT_REASONING_EFFORT if reasoning_effort is None else _normalize_reasoning_effort(reasoning_effort)
        )
        if normalized is None:
            return launch_error(
                f"Invalid reasoning_effort {reasoning_effort!r}. "
                f"Choose from {', '.join(VALID_REASONING_EFFORT_SETTINGS)}."
            )
        explicit_effort = normalized if normalized != DEFAULT_REASONING_EFFORT else None
        initial_effort = explicit_effort
        if initial_effort is None:
            saved_effort = load_settings(resolved_data_dir).reasoning_effort
            if saved_effort != DEFAULT_REASONING_EFFORT:
                initial_effort = saved_effort

        run_id = f"run-{uuid.uuid4().hex[:10]}"
        prompt_path = None
        round_created = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="codex-orbit-", suffix=".prompt", delete=False,
            ) as prompt_file:
                prompt_path = Path(prompt_file.name)
                prompt_file.write(prompt)

            delegation_store.start_round(
                run_id=run_id,
                session_id=session_id,
                workspace_dir=str(workspace_dir),
                prompt=prompt,
                isolation_mode=isolation_mode,
                source_model="Orbit",
                title=title,
                reasoning_effort=initial_effort,
            )
            round_created = True
            command = [
                sys.executable, "-u", str(_REPO_ROOT / "server" / "delegate_cli.py"),
                "--cwd", str(workspace_dir), "--mode", isolation_mode, "--data-dir", str(resolved_data_dir),
                "--source-model", "Orbit", "--run-id", run_id,
                "--prompt-file", str(prompt_path), "--delete-prompt-file",
            ]
            if session_id:
                command.extend(["--session-id", session_id, "--no-desktop-project-sync"])
            elif title.startswith("-"):
                command.append(f"--title={title}")
            else:
                command.extend(["--title", title])
            if web_search:
                command.append("--web-search")
            if explicit_effort is not None:
                command.extend(["--reasoning-effort", explicit_effort])

            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=sys.platform != "win32",
            )
        except (OSError, ValueError) as exc:
            operation = "follow-up" if session_id else "session"
            error = f"Could not start {operation}: {exc}"
            if prompt_path is not None:
                with suppress(OSError):
                    prompt_path.unlink(missing_ok=True)
            if round_created:
                with suppress(OSError):
                    delegation_store.update_round(run_id, status="failed", error_summary=error, ended=True)
            return launch_error(error)

        threading.Thread(target=_reap_delegation, args=(process, prompt_path), daemon=True).start()
        payload: dict[str, Any] = {"started": True, "run_id": run_id}
        if session_id:
            payload["session_id"] = session_id
            message = "Follow-up started."
        else:
            payload.update(workspace_dir=str(workspace_dir), title=title)
            message = "Session started."
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            structured_content=payload,
        )

    @mcp.tool(
        name="orbit_create_session",
        title="Create Codex session",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_create_session(
        workspace_dir: str,
        prompt: str,
        title: str = "",
        mode: str = "workspace-write",
        web_search: bool = False,
        reasoning_effort: str | None = None,
    ) -> types.CallToolResult:
        """Start a new conversation from its workspace and initial prompt."""
        stripped_prompt = prompt.strip()
        if not stripped_prompt:
            return launch_error("Prompt is required.")
        if mode not in ("workspace-write", "read-only", "worktree"):
            return launch_error("Mode must be workspace-write, read-only, or worktree.")
        try:
            resolved = _resolve_workspace_dir(workspace_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            return launch_error(str(exc))
        effective_title = title.strip() or stripped_prompt.splitlines()[0][:60].rstrip()
        return launch_delegation(
            workspace_dir=resolved,
            prompt=prompt,
            isolation_mode=mode,
            title=effective_title,
            web_search=web_search,
            reasoning_effort=reasoning_effort,
        )

    @mcp.tool(
        name="orbit_send_followup",
        title="Send follow-up to Codex session",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_send_followup(
        session_id: str,
        prompt: str,
        web_search: bool = False,
        reasoning_effort: str | None = None,
    ) -> types.CallToolResult:
        session_id = session_id.strip()
        prompt = prompt.strip()
        if not session_id:
            return launch_error("Session ID is required.")
        if not prompt:
            return launch_error("Prompt is required.")

        with followup_lock:
            detail, _ = store.get_session_detail(session_id)
            if detail is None or detail.summary.session_id != session_id:
                return launch_error("Session not found.")

            existing = delegation_store.get_by_session_id(session_id)
            if detail.summary.status == "running" or (
                existing and any(round_rec.status == "running" for round_rec in existing.rounds)
            ):
                return launch_error("Session already has a running task.", busy=True)
            if existing and existing.session_id is None:
                return launch_error("Session not found.")

            workspace_dir = None
            for raw_path in (detail.summary.project_dir, existing.workspace_dir if existing else None):
                if not raw_path or not raw_path.strip():
                    continue
                with suppress(OSError, RuntimeError, ValueError):
                    candidate = Path(raw_path).expanduser()
                    if candidate.drive and str(candidate) == candidate.drive:
                        candidate = Path(candidate.drive + os.sep)
                    if candidate.is_dir():
                        workspace_dir = candidate.resolve()
                        break
            if workspace_dir is None:
                return launch_error("Session workspace directory does not exist.")

            isolation_mode = (existing.isolation_mode if existing else detail.summary.isolation_mode) or "workspace-write"
            return launch_delegation(
                workspace_dir=workspace_dir,
                prompt=prompt,
                isolation_mode=isolation_mode,
                title=detail.summary.title,
                web_search=web_search,
                session_id=session_id,
                reasoning_effort=reasoning_effort,
            )

    @mcp.tool(
        name="orbit_cancel_delegation",
        title="Cancel running Codex delegation",
        description="App-only tool: terminate a running delegated Codex task and its child process tree.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_cancel_delegation(
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> types.CallToolResult:
        res = cancel_delegation(
            delegation_store,
            session_id=session_id,
            run_id=run_id,
        )
        payload = {
            "cancelled": res.cancelled,
            "already_finished": res.already_finished,
            "session_id": res.session_id,
            "run_id": res.run_id,
            "terminated_pids": res.terminated_pids,
            "message": res.message,
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=res.message)],
            structured_content=payload,
        )

    @mcp.tool(
        name="orbit_get_quota",
        title="Get Codex remaining quota",
        description="App-only tool: read cached Codex remaining 5h/7d quota and reset times.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_get_quota(force_refresh: bool = False) -> types.CallToolResult:
        if force_refresh:
            data = _query_live_quota()
        else:
            data, is_fresh = _read_quota_without_live_query()
            if data is None:
                data = _query_live_quota()
            elif not is_fresh:
                _trigger_quota_refresh_async()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")],
            structured_content=data,
        )

    @mcp.tool(
        name="orbit_set_routing_mode",
        title="Set model routing policy",
        description="App-only tool: persist the model routing mode (silent_low_cost, ask, silent_best).",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_set_routing_mode(routing_mode: str) -> types.CallToolResult:
        saved = save_routing_mode(routing_mode, data_dir=resolved_data_dir)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=ROUTING_MODE_LABELS[saved])],
            structured_content={
                "routing_mode": saved,
                "routing_mode_label": ROUTING_MODE_LABELS[saved],
            },
        )

    @mcp.tool(
        name="orbit_set_ui_locale",
        title="Set Codex Orbit UI language",
        description="App-only tool: persist UI language preference (auto, zh-CN, zh-TW, en, ja).",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_set_ui_locale(ui_locale: str) -> types.CallToolResult:
        saved = save_ui_locale(ui_locale, data_dir=resolved_data_dir)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=saved)],
            structured_content={"ui_locale": saved},
        )

    def _settings_payload(settings: OrbitSettings) -> dict[str, Any]:
        return {
            **settings.to_dict(),
            "available_models": read_codex_model_catalog(store.sessions_dir.parent),
        }

    def settings_result(settings: OrbitSettings) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Codex Orbit settings")],
            structured_content=_settings_payload(settings),
        )

    @mcp.tool(
        name="orbit_get_settings",
        title="Get Codex Orbit settings",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_get_settings() -> types.CallToolResult:
        return settings_result(load_settings(resolved_data_dir))

    @mcp.tool(
        name="orbit_update_settings",
        title="Update Codex Orbit settings",
        description=(
            "Update the supplied settings. Pass an empty model name to restore its default, "
            "or an empty reasoning effort or 'auto' to restore policy selection."
        ),
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_update_settings(
        routing_mode: str | None = None,
        ui_locale: str | None = None,
        default_model: str | None = None,
        escalation_model: str | None = None,
        quota_guardrail_enabled: bool | None = None,
        quota_guardrail_threshold_pct: int | None = None,
        reasoning_effort: str | None = None,
    ) -> types.CallToolResult:
        updates = {
            "routing_mode": routing_mode,
            "ui_locale": ui_locale,
            "default_model": default_model,
            "escalation_model": escalation_model,
            "quota_guardrail_enabled": quota_guardrail_enabled,
            "quota_guardrail_threshold_pct": quota_guardrail_threshold_pct,
            "reasoning_effort": reasoning_effort,
        }
        fields = {name: value for name, value in updates.items() if value is not None}
        try:
            settings = save_settings(resolved_data_dir, **fields) if fields else load_settings(resolved_data_dir)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return settings_result(settings)

    @mcp.tool(
        name="orbit_run_doctor",
        title="Run Codex Orbit diagnostics",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_run_doctor() -> types.CallToolResult:
        report = run_doctor(data_dir=resolved_data_dir, codex_home=store.sessions_dir.parent)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Codex Orbit diagnostics")],
            structured_content=report,
        )

    @mcp.tool(
        name="orbit_prune_worktrees",
        title="Prune idle Codex Orbit worktrees",
        description="Remove idle delegation worktrees and their uncommitted files. Running delegations are skipped.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_prune_worktrees() -> types.CallToolResult:
        result = prune_idle_worktrees(delegation_store)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Removed {len(result['removed'])} worktrees")],
            structured_content=result,
        )

    @mcp.tool(
        name="orbit_get_overview",
        title="Get Codex Orbit profile overview and activity heatmap",
        description="App-only tool: compute Profile-style KPIs, 52-week AGY vs Direct Codex heatmap, and insights.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_get_overview(project_dir: str = "") -> types.CallToolResult:
        overview = build_profile_overview(
            store,
            data_dir=resolved_data_dir,
            project_dir=project_dir,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{overview['kpis']['total_sessions']} sessions")],
            structured_content=overview,
        )

    @mcp.tool(
        name="orbit_bootstrap",
        title="Load Codex Orbit app",
        description="Load sessions, projects, settings, and cached quota in one app call.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_bootstrap(
        query: str = "",
        limit: int = 100,
        include_overview: bool = False,
        project_dir: str = "",
    ) -> types.CallToolResult:
        payload = _session_list_payload(query, limit)
        quota, is_fresh = _read_quota_without_live_query()
        if not is_fresh:
            _trigger_quota_refresh_async()
        payload.update(
            settings=_settings_payload(load_settings(resolved_data_dir)),
            quota=quota,
            quota_stale=not is_fresh,
            overview=build_profile_overview(store, data_dir=resolved_data_dir, project_dir=project_dir)
            if include_overview else None,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{len(payload['sessions'])} sessions")],
            structured_content=payload,
        )

    if warmup:
        def _warm_caches() -> None:
            if read_cached_codex_quota(data_dir=resolved_data_dir) is None:
                _trigger_quota_refresh_async()
            store.list_sessions(limit=100)

        threading.Thread(target=_warm_caches, daemon=True).start()

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Orbit MCP App Server")
    parser.add_argument("--data-dir", "--data_dir", dest="data_dir", default=None)
    parser.add_argument("--html-path", "--html_path", dest="html_path", default=None)
    parser.add_argument("--sessions-dir", "--sessions_dir", dest="sessions_dir", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    server = create_server(
        sessions_dir=Path(args.sessions_dir) if args.sessions_dir else None,
        html_path=Path(args.html_path) if args.html_path else None,
        data_dir=Path(args.data_dir) if args.data_dir else None,
        write_pointer=True,
        warmup=True,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
