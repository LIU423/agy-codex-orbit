"""Codex Orbit native Antigravity MCP App server (stdio)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure repository root is importable when launched directly via script path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp import types
from mcp.server.mcpserver import MCPServer

from server.delegation_store import DelegationStore, resolve_data_dir, runtime_pointer_path
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
        version="0.2.0",
    )

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
        description="Open the Codex Orbit panel to monitor local OpenAI Codex sessions in real time.",
        meta=PANE_META,
        structured_output=False,
    )
    def open_codex_orbit(
        session_id: str | None = None,
        query: str | None = None,
    ) -> types.CallToolResult:
        bootstrap = {
            "initial_session_id": session_id,
            "initial_query": query,
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
        payload = {
            "sessions": [s.model_dump(mode="json") for s in sessions],
            "sessions_dir": str(store.sessions_dir),
            "sessions_dir_exists": store.sessions_dir.exists() or len(sessions) > 0,
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
