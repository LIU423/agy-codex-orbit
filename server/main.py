"""Codex Orbit native Antigravity MCP App server (stdio)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Ensure repository root is importable when launched directly via script path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp import types
from mcp.server.mcpserver import MCPServer

from server.codex_desktop import query_codex_quota
from server.delegation_store import DelegationStore, resolve_data_dir, runtime_pointer_path
from server.overview_builder import build_profile_overview
from server.process_control import cancel_delegation
from server.routing_policy import (
    ROUTING_MODE_LABELS,
    load_routing_mode,
    load_ui_locale,
    save_routing_mode,
    save_ui_locale,
)
from server.session_store import SessionStore

APP_URI = "ui://codex-orbit/app.html"
APP_MIME = "text/html;profile=mcp-app"

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
        pointer_file = runtime_pointer_path()
        pointer_file.parent.mkdir(parents=True, exist_ok=True)
        pointer_file.write_text(
            json.dumps(
                {
                    "data_dir": str(data_dir.resolve()),
                    "python_bin": str(Path(sys.executable).resolve()),
                    "delegate_cli": str((_REPO_ROOT / "server" / "delegate_cli.py").resolve()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # Non-fatal: server continues operating with its configured data_dir.
        pass


def create_server(
    *,
    sessions_dir: Path | None = None,
    session_index_path: Path | None = None,
    html_path: Path | None = None,
    data_dir: Path | None = None,
    write_pointer: bool = False,
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
        version="0.6.1",
    )
    quota_cache: dict[str, Any] = {"ts": 0.0, "data": None}

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
        if resolved_session_id is None and focus_running:
            sessions = store.list_sessions(limit=50)
            running = [s for s in sessions if s.status == "running"]
            if workspace_dir:
                norm_target = os.path.normcase(os.path.normpath(workspace_dir))
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

    @mcp.tool(
        name="orbit_list_sessions",
        title="List Codex sessions",
        description="App-only tool: list local Codex sessions and active delegations with optional search filter.",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_list_sessions(query: str = "", limit: int = 80) -> types.CallToolResult:
        clamped_limit = max(1, min(limit, 200))
        sessions = store.list_sessions(query=query, limit=clamped_limit)
        active_mode = load_routing_mode(data_dir=resolved_data_dir)
        ui_locale = load_ui_locale(data_dir=resolved_data_dir)
        payload = {
            "sessions": [s.model_dump(mode="json") for s in sessions],
            "sessions_dir": str(store.sessions_dir),
            "sessions_dir_exists": store.sessions_dir.exists() or len(sessions) > 0,
            "routing_mode": active_mode,
            "ui_locale": ui_locale,
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{len(sessions)} sessions")],
            structured_content=payload,
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
        now = time.monotonic()
        if not force_refresh and quota_cache["data"] is not None and (now - quota_cache["ts"]) < 60.0:
            data = quota_cache["data"]
        else:
            data = query_codex_quota(codex_home=store.sessions_dir.parent)
            quota_cache["data"] = data
            quota_cache["ts"] = now
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
        description="App-only tool: persist UI language preference (auto, zh, en).",
        meta=APP_ONLY_META,
        structured_output=False,
    )
    def orbit_set_ui_locale(ui_locale: str) -> types.CallToolResult:
        saved = save_ui_locale(ui_locale, data_dir=resolved_data_dir)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=saved)],
            structured_content={"ui_locale": saved},
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
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
