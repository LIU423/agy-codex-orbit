"""Protocol-level tests verifying MCP App metadata, visibility, and resources."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from server.delegation_store import DelegationStore
from server.main import APP_MIME, APP_URI, _write_runtime_pointer, create_server
from server.routing_policy import MODEL_ASTRA, MODEL_SOL

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
    assert server.version == "0.7.0"

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
            "orbit_send_followup",
            "orbit_cancel_delegation",
            "orbit_get_quota",
            "orbit_set_routing_mode",
            "orbit_set_ui_locale",
            "orbit_get_overview",
            "orbit_get_settings",
            "orbit_update_settings",
            "orbit_run_doctor",
            "orbit_prune_worktrees",
        ):
            assert app_only_name in by_name
            meta = by_name[app_only_name].meta or {}
            assert meta["ui"]["visibility"] == ["app"]
        assert {
            tool.name for tool in tools if "app" not in (tool.meta or {}).get("ui", {}).get("visibility", [])
        } == {"open_codex_orbit"}

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
        locale_res = await server.call_tool("orbit_set_ui_locale", {"ui_locale": "zh-CN"})
        assert locale_res.structured_content["ui_locale"] == "zh-CN"
        locale_tw_res = await server.call_tool("orbit_set_ui_locale", {"ui_locale": "zh-TW"})
        assert locale_tw_res.structured_content["ui_locale"] == "zh-TW"
        locale_legacy_res = await server.call_tool("orbit_set_ui_locale", {"ui_locale": "zh"})
        assert locale_legacy_res.structured_content["ui_locale"] == "zh-CN"
        locale_ja_res = await server.call_tool("orbit_set_ui_locale", {"ui_locale": "ja"})
        assert locale_ja_res.structured_content["ui_locale"] == "ja"
        list_after = await server.call_tool("orbit_list_sessions", {})
        assert list_after.structured_content["routing_mode"] == "silent_low_cost"
        assert list_after.structured_content["ui_locale"] == "ja"
        ov_res = await server.call_tool("orbit_get_overview", {})
        assert ov_res.structured_content["kpis"]["total_sessions"] == 1

    asyncio.run(_verify())


@pytest.fixture
def followup_session(tmp_path: Path) -> tuple[Path, str]:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    meta_line, *events = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    meta = json.loads(meta_line)
    meta["payload"]["cwd"] = str(tmp_path)
    (sessions_dir / FIXTURE_PATH.name).write_text("\n".join([json.dumps(meta), *events, ""]), encoding="utf-8")
    return sessions_dir, meta["payload"]["id"]


def test_send_followup_rejects_invalid_and_running_sessions(tmp_path, followup_session, monkeypatch) -> None:
    sessions_dir, session_id = followup_session
    data_dir = tmp_path / "data"
    server = create_server(sessions_dir=sessions_dir, data_dir=data_dir)

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("Invalid follow-up attempted to spawn a process")

    monkeypatch.setattr("server.main.subprocess.Popen", unexpected_spawn)

    async def verify() -> None:
        async def followup(sid: str, prompt: str) -> dict:
            result = await server.call_tool("orbit_send_followup", {"session_id": sid, "prompt": prompt})
            return result.structured_content

        assert await followup("  ", "Continue") == {"started": False, "error": "Session ID is required."}
        assert await followup(session_id, " \t ") == {"started": False, "error": "Prompt is required."}
        assert await followup("missing", "Continue") == {"started": False, "error": "Session not found."}

        store = DelegationStore(data_dir)
        store.start_round(
            run_id="run-active",
            session_id=session_id,
            workspace_dir=str(tmp_path),
            prompt="In progress",
            isolation_mode="read-only",
            pid=os.getpid(),
        )
        assert await followup(session_id, "Continue") == {
            "started": False,
            "busy": True,
            "error": "Session already has a running task.",
        }

    asyncio.run(verify())


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_send_followup_registers_round_before_background_spawn(
    tmp_path, followup_session, monkeypatch, spawn_fails
) -> None:
    sessions_dir, session_id = followup_session
    data_dir = tmp_path / "data"
    store = DelegationStore(data_dir)
    server = create_server(sessions_dir=sessions_dir, data_dir=data_dir)
    prompt = "Continue with café\nand summarize."
    allow_exit = threading.Event()
    spawned = []

    class BackgroundProcess:
        def wait(self) -> None:
            allow_exit.wait(timeout=10)

    def spawn(command, **kwargs):
        run_id = command[command.index("--run-id") + 1]
        prompt_path = Path(command[command.index("--prompt-file") + 1])
        spawned.append((run_id, prompt_path))
        assert command[:2] == [sys.executable, "-u"]
        assert Path(command[2]).name == "delegate_cli.py"
        assert command[command.index("--session-id") + 1] == session_id
        assert command[command.index("--cwd") + 1] == str(tmp_path)
        assert command[command.index("--mode") + 1] == "workspace-write"
        assert command[command.index("--data-dir") + 1] == str(data_dir)
        assert command[command.index("--source-model") + 1] == "Orbit"
        assert "--delete-prompt-file" in command
        assert ("--web-search" in command) is (not spawn_fails)
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is (sys.platform != "win32")
        if sys.platform == "win32":
            assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert prompt_path.read_text(encoding="utf-8") == prompt
        record = store.get_by_session_id(session_id)
        assert [(rnd.run_id, rnd.status) for rnd in record.rounds] == [(run_id, "running")]
        if spawn_fails:
            raise OSError("spawn failed")
        return BackgroundProcess()

    monkeypatch.setattr("server.main.subprocess.Popen", spawn)

    async def verify() -> None:
        args = {"session_id": f" {session_id} ", "prompt": f" {prompt} ", "web_search": not spawn_fails}
        result = await server.call_tool("orbit_send_followup", args)
        [(run_id, prompt_path)] = spawned
        record = store.get_by_run_id(run_id)
        [round_rec] = record.rounds
        if spawn_fails:
            assert result.structured_content == {"started": False, "error": "Could not start follow-up: spawn failed"}
            assert round_rec.status == "failed"
            assert round_rec.ended_at
            assert not prompt_path.exists()
            return

        assert result.content[0].text == "Follow-up started."
        assert result.structured_content == {"started": True, "session_id": session_id, "run_id": run_id}
        assert (await server.call_tool("orbit_send_followup", args)).structured_content["busy"] is True
        poll = await server.call_tool("orbit_poll_session_delta", {"session_id": session_id})
        delta = poll.structured_content["delta"]
        assert delta["summary"]["status"] == "running"
        assert delta["summary"]["active_run_id"] == run_id
        assert delta["updated_turns"][0]["status"] == "completed"
        assert delta["updated_turns"][-1]["status"] == "running"
        assert delta["updated_turns"][-1]["prompt"] == prompt

    try:
        asyncio.run(verify())
    finally:
        allow_exit.set()


def test_app_settings_diagnostics_and_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    data_dir = tmp_path / "data"
    idle = data_dir / "worktrees" / "idle"
    idle.mkdir(parents=True)
    (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "custom-sol"}]}), encoding="utf-8")
    server = create_server(sessions_dir=home / "sessions", data_dir=data_dir)

    def fake_doctor(*, data_dir: Path, codex_home: Path) -> dict:
        assert data_dir == tmp_path / "data"
        assert codex_home == home
        return {"ok": True, "checks": []}

    monkeypatch.setattr("server.main.run_doctor", fake_doctor)

    async def verify() -> None:
        initial = (await server.call_tool("orbit_get_settings", {})).structured_content
        assert initial["default_model"] == MODEL_SOL
        assert initial["escalation_model"] == MODEL_ASTRA
        assert [model["slug"] for model in initial["available_models"]] == ["custom-sol"]

        updates = {
            "routing_mode": "silent_best",
            "ui_locale": "ja-JP",
            "default_model": "custom-sol",
            "escalation_model": "custom-astra",
            "quota_guardrail_enabled": False,
            "quota_guardrail_threshold_pct": 35,
        }
        updated = (await server.call_tool("orbit_update_settings", updates)).structured_content
        assert updated == {**initial, **updates, "ui_locale": "ja"}
        reset = (await server.call_tool("orbit_update_settings", {
            "default_model": "",
            "escalation_model": None,
        })).structured_content
        assert reset == {**updated, "default_model": MODEL_SOL}
        assert (await server.call_tool("orbit_update_settings", {})).structured_content == reset
        listed = (await server.call_tool("orbit_list_sessions", {})).structured_content
        settings_fields = set(reset) - {"available_models"}
        assert {name: listed[name] for name in settings_fields} == {name: reset[name] for name in settings_fields}
        assert (await server.call_tool("orbit_run_doctor", {})).structured_content == {"ok": True, "checks": []}

        pruned = (await server.call_tool("orbit_prune_worktrees", {})).structured_content
        assert pruned == {"removed": [str(idle)], "skipped_running": [], "errors": []}
        assert not idle.exists()

    asyncio.run(verify())


def test_runtime_pointer_preserves_virtualenv_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pointer = tmp_path / "runtime.json"
    data_dir = tmp_path / "data"
    executable = tmp_path / ".venv" / "bin" / "python"
    base_executable = tmp_path / "base" / "python"
    resolve = Path.resolve

    def resolve_executable(path: Path, *args, **kwargs) -> Path:
        return base_executable if path == executable else resolve(path, *args, **kwargs)

    monkeypatch.setattr("server.main.runtime_pointer_path", lambda: pointer)
    monkeypatch.setattr("server.main.sys.executable", str(executable))
    monkeypatch.setattr(Path, "resolve", resolve_executable)

    _write_runtime_pointer(data_dir)

    runtime = json.loads(pointer.read_text(encoding="utf-8"))
    assert runtime["python_bin"] == str(executable)
    assert runtime["data_dir"] == str(data_dir.resolve())
