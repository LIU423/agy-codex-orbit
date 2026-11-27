"""Protocol-level tests verifying MCP App metadata, visibility, and resources."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from server.main import APP_MIME, APP_URI, create_server

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "rollout-2026-11-26T10-00-00-11111111-2222-3333-4444-555555555555.jsonl"
)


def test_mcp_app_metadata_and_zero_token_visibility(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions" / "2026" / "11" / "26"
    sessions_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE_PATH, sessions_dir / FIXTURE_PATH.name)

    fake_html = tmp_path / "app.html"
    fake_html.write_text("<!doctype html><html><body>Codex Orbit Test</body></html>", encoding="utf-8")

    server = create_server(
        sessions_dir=tmp_path / "sessions",
        html_path=fake_html,
        data_dir=tmp_path / "plugin-data",
    )

    async def _verify() -> None:
        tools = await server.list_tools()
        by_name = {t.name: t for t in tools}

        assert "open_codex_orbit" in by_name
        open_meta = by_name["open_codex_orbit"].meta or {}
        assert open_meta["ui"]["resourceUri"] == APP_URI
        entrypoints = open_meta["com.google.antigravity/ui"]["entrypoints"]
        assert {"type": "aux_pane", "title": "Codex Orbit"} in entrypoints
        assert {"type": "global", "title": "Codex Orbit"} in entrypoints

        for app_only_name in (
            "orbit_list_sessions",
            "orbit_get_session_detail",
            "orbit_poll_session_delta",
            "orbit_cancel_delegation",
            "orbit_get_quota",
            "orbit_set_routing_mode",
            "orbit_set_ui_locale",
            "orbit_get_overview",
        ):
            assert app_only_name in by_name
            meta = by_name[app_only_name].meta or {}
            assert meta["ui"]["visibility"] == ["app"]

        resources = await server.list_resources()
        ui_res = next(r for r in resources if str(r.uri) == APP_URI)
        assert ui_res.mime_type == APP_MIME

        # Empty-argument entrypoint launch must succeed cleanly.
        open_res = await server.call_tool("open_codex_orbit", {})
        assert open_res.structured_content["initial_session_id"] is None
        assert open_res.structured_content["focus_running"] is False

        # focus_running bootstrap flags should pass through cleanly when idle.
        focus_res = await server.call_tool(
            "open_codex_orbit",
            {"focus_running": True, "workspace_dir": "D:\\SampleRepo"},
        )
        assert focus_res.structured_content["focus_running"] is True
        assert focus_res.structured_content["preferred_workspace"] == "D:\\SampleRepo"

        list_res = await server.call_tool("orbit_list_sessions", {})
        sessions = list_res.structured_content["sessions"]
        assert len(sessions) == 1
        sid = sessions[0]["session_id"]

        detail_res = await server.call_tool("orbit_get_session_detail", {"session_id": sid})
        assert detail_res.structured_content["found"] is True
        cursor = detail_res.structured_content["detail"]["cursor"]
        assert cursor > 0

        delta_res = await server.call_tool(
            "orbit_poll_session_delta",
            {"session_id": sid, "after_cursor": cursor},
        )
        assert delta_res.structured_content["found"] is True
        assert delta_res.structured_content["delta"]["changed"] is False

        cancel_res = await server.call_tool(
            "orbit_cancel_delegation",
            {"session_id": sid},
        )
        assert cancel_res.structured_content["cancelled"] is False
        assert cancel_res.structured_content["already_finished"] is True

        mode_res = await server.call_tool(
            "orbit_set_routing_mode",
            {"routing_mode": "silent_low_cost"},
        )
        assert mode_res.structured_content["routing_mode"] == "silent_low_cost"
        locale_res = await server.call_tool("orbit_set_ui_locale", {"ui_locale": "en"})
        assert locale_res.structured_content["ui_locale"] == "en"
        locale_ja_res = await server.call_tool("orbit_set_ui_locale", {"ui_locale": "ja"})
        assert locale_ja_res.structured_content["ui_locale"] == "ja"
        list_after = await server.call_tool("orbit_list_sessions", {})
        assert list_after.structured_content["routing_mode"] == "silent_low_cost"
        assert list_after.structured_content["ui_locale"] == "ja"
        ov_res = await server.call_tool("orbit_get_overview", {})
        assert ov_res.structured_content["kpis"]["total_sessions"] == 1

    asyncio.run(_verify())
