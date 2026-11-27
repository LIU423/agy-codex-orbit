"""Protocol-level tests verifying MCP App metadata, visibility, and resources."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server.codex_desktop import write_cached_codex_quota
from server.delegation_store import DelegationStore
from server.main import APP_MIME, APP_URI, _write_runtime_pointer, create_server
from server.routing_policy import MODEL_ASTRA, MODEL_SOL, save_settings

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
    assert server.version == "0.1.0"

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
            "orbit_bootstrap",
            "orbit_list_sessions",
            "orbit_create_project",
            "orbit_create_session",
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


def _quota(source: str) -> dict:
    return {"codex": {"available": True, "source": source}, "antigravity": {"available": False}}


def _write_stale_quota(data_dir: Path, data: dict) -> None:
    data_dir.mkdir(exist_ok=True)
    fetched_at = (datetime.now() - timedelta(minutes=2)).timestamp()
    (data_dir / "quota_cache.json").write_text(
        json.dumps({"fetched_at": fetched_at, "data": data}), encoding="utf-8",
    )


def test_quota_uses_fresh_disk_and_memory_and_can_force_refresh(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    disk = _quota("disk")
    live = _quota("live")
    write_cached_codex_quota(disk, data_dir=data_dir)
    queries = []

    def query(**kwargs):
        queries.append(kwargs)
        write_cached_codex_quota(live, data_dir=kwargs["data_dir"])
        return live

    monkeypatch.setattr("server.main.query_codex_quota", query)
    server = create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir)

    async def verify() -> None:
        assert (await server.call_tool("orbit_get_quota", {})).structured_content == disk
        assert queries == []
        result = await server.call_tool("orbit_get_quota", {"force_refresh": True})
        assert result.structured_content == live
        assert json.loads((data_dir / "quota_cache.json").read_text(encoding="utf-8"))["data"] == live
        (data_dir / "quota_cache.json").unlink()
        assert (await server.call_tool("orbit_get_quota", {})).structured_content == live
        assert queries == [{"codex_home": tmp_path, "data_dir": data_dir, "use_cache": False}]

    asyncio.run(verify())


@pytest.mark.parametrize("source", ["disk", "rollout", "memory", "missing"])
def test_quota_returns_without_waiting_and_deduplicates_refresh(tmp_path, monkeypatch, source) -> None:
    data_dir = tmp_path / "data"
    expected = None if source == "missing" else _quota(source)
    if source == "disk":
        _write_stale_quota(data_dir, expected)
    monkeypatch.setattr(
        "server.main.read_rollout_fallback_quota_status", lambda home: expected if source == "rollout" else None,
    )
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr("server.main.time", clock)
    server = create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir)
    if source == "memory":
        # Seed the in-memory cache via the synchronous cold path, without a disk write.
        monkeypatch.setattr("server.main.query_codex_quota", lambda **kwargs: expected)
        result = asyncio.run(server.call_tool("orbit_get_quota", {}))
        assert result.structured_content == expected
        clock.monotonic = lambda: 160.0

    started = threading.Event()
    release = threading.Event()
    workers = []
    live = _quota("refreshed")

    def query(**kwargs):
        worker = threading.current_thread()
        assert worker.daemon
        assert kwargs == {"codex_home": tmp_path, "data_dir": data_dir, "use_cache": False}
        workers.append(worker)
        started.set()
        assert release.wait(5), "Quota tool waited for the live query"
        return live

    monkeypatch.setattr("server.main.query_codex_quota", query)

    async def verify() -> None:
        if expected is not None:
            result = await server.call_tool("orbit_get_quota", {})
            assert result.structured_content == expected
        for _ in range(3):
            result = await server.call_tool("orbit_bootstrap", {})
            assert result.structured_content["quota"] == expected
            assert result.structured_content["quota_stale"] is True
        assert started.wait(5)
        assert len(workers) == 1

    try:
        asyncio.run(verify())
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()
    result = asyncio.run(server.call_tool("orbit_bootstrap", {}))
    assert result.structured_content["quota"] == live
    assert result.structured_content["quota_stale"] is False


@pytest.mark.parametrize("has_stale_disk", [True, False])
def test_bootstrap_keeps_quota_stale_when_live_query_falls_back(tmp_path, monkeypatch, has_stale_disk) -> None:
    data_dir = tmp_path / "data"
    if has_stale_disk:
        expected = _quota("disk")
        _write_stale_quota(data_dir, expected)
        live_result = {**expected, "codex": {"available": False, "error": "Offline"}}
    else:
        expected = live_result = _quota("rollout_cache")

    monkeypatch.setattr("server.main.query_codex_quota", lambda **kwargs: live_result)
    server = create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir)

    async def verify() -> None:
        result = await server.call_tool("orbit_get_quota", {"force_refresh": True})
        assert result.structured_content == live_result
        result = await server.call_tool("orbit_bootstrap", {})
        assert result.structured_content["quota"] == expected
        assert result.structured_content["quota_stale"] is True

    asyncio.run(verify())


def test_bootstrap_matches_existing_tools_and_optionally_loads_overview(tmp_path, followup_session, monkeypatch) -> None:
    sessions_dir, _ = followup_session
    data_dir = tmp_path / "data"
    disk = _quota("disk")
    write_cached_codex_quota(disk, data_dir=data_dir)
    project_dir = tmp_path / "example-project"
    project_dir.mkdir()
    (tmp_path / "models_cache.json").write_text('{"models": [{"slug": "custom-sol"}]}', encoding="utf-8")
    save_settings(data_dir, ui_locale="ja")

    def unexpected_query(**kwargs):
        pytest.fail("Bootstrap with fresh disk quota queried app-server")

    monkeypatch.setattr("server.main.query_codex_quota", unexpected_query)
    server = create_server(sessions_dir=sessions_dir, data_dir=data_dir)

    async def verify() -> None:
        await server.call_tool("orbit_create_project", {"workspace_dir": str(project_dir)})
        settings = (await server.call_tool("orbit_get_settings", {})).structured_content
        assert settings["ui_locale"] == "ja"
        assert [item["slug"] for item in settings["available_models"]] == ["custom-sol"]
        result = await server.call_tool("orbit_bootstrap", {})
        listed = (await server.call_tool("orbit_list_sessions", {})).structured_content
        assert result.structured_content == {
            **{key: listed[key] for key in ("sessions", "projects", "sessions_dir", "sessions_dir_exists")},
            "settings": settings,
            "quota": disk,
            "quota_stale": False,
            "overview": None,
        }
        result = await server.call_tool("orbit_bootstrap", {
            "query": "EXAMPLE-PROJECT", "limit": 1, "include_overview": True, "project_dir": str(tmp_path),
        })
        assert result.structured_content["projects"] == listed["projects"]
        assert result.structured_content["sessions"] == []
        overview = result.structured_content["overview"]
        assert overview["project_filter"] == str(tmp_path)
        assert overview["kpis"]["total_sessions"] == 1

    asyncio.run(verify())


@pytest.mark.parametrize("has_fresh_quota", [True, False])
def test_server_warmup_runs_in_daemon_threads(tmp_path, monkeypatch, has_fresh_quota) -> None:
    data_dir = tmp_path / "data"
    if has_fresh_quota:
        write_cached_codex_quota(_quota("disk"), data_dir=data_dir)
    else:
        _write_stale_quota(data_dir, _quota("disk"))
    sessions_ready = threading.Event()
    quota_ready = threading.Event()
    session_workers = []
    quota_workers = []

    def list_sessions(store, *, limit):
        assert limit == 100
        session_workers.append(threading.current_thread())
        sessions_ready.set()
        return []

    def query(**kwargs):
        quota_workers.append(threading.current_thread())
        quota_ready.set()
        return _quota("live")

    monkeypatch.setattr("server.main.SessionStore.list_sessions", list_sessions)
    monkeypatch.setattr("server.main.query_codex_quota", query)
    create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir, warmup=True)
    assert sessions_ready.wait(5)
    if not has_fresh_quota:
        assert quota_ready.wait(5)
    for worker in session_workers + quota_workers:
        worker.join(timeout=5)
        assert worker.daemon and not worker.is_alive()
    assert len(session_workers) == 1
    assert len(quota_workers) == (0 if has_fresh_quota else 1)


@pytest.mark.parametrize("bind_session_id", [True, False])
def test_open_focus_running_uses_matching_delegation_before_listing(tmp_path, monkeypatch, bind_session_id) -> None:
    data_dir = tmp_path / "data"
    delegation_store = DelegationStore(data_dir)
    workspace = tmp_path / "target"
    session_id = "11111111-2222-3333-4444-555555555555" if bind_session_id else None
    delegation_store.start_round(
        run_id="run-target", session_id=session_id, workspace_dir=str(workspace), prompt="Review",
        source_model="Orbit", isolation_mode="workspace-write", pid=os.getpid(),
    )
    delegation_store.start_round(
        run_id="run-other", workspace_dir=str(tmp_path / "other"), prompt="Other review",
        source_model="Orbit", isolation_mode="workspace-write", pid=os.getpid(),
    )

    def unexpected_list(*args, **kwargs):
        pytest.fail("Opening an active delegation should skip the rollout scan")

    monkeypatch.setattr("server.main.SessionStore.list_sessions", unexpected_list)
    server = create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir)
    result = asyncio.run(server.call_tool("open_codex_orbit", {
        "focus_running": True, "workspace_dir": str(workspace / ".." / workspace.name),
    }))
    assert result.structured_content["initial_session_id"] == (session_id or "run-target")


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
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
            assert not (kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
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


def test_create_project_persists_and_lists_empty_projects(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    sessions_dir = tmp_path / "sessions"
    server = create_server(sessions_dir=sessions_dir, data_dir=data_dir)
    existing = tmp_path / "Existing Folder"
    existing.mkdir()
    missing = tmp_path / "nested" / "New Folder"
    file_path = tmp_path / "file.txt"
    file_path.touch()
    monkeypatch.setenv("USERPROFILE" if sys.platform == "win32" else "HOME", str(tmp_path))

    async def verify() -> None:
        async def create(workspace_dir: str, **kwargs) -> dict:
            result = await server.call_tool("orbit_create_project", {"workspace_dir": workspace_dir, **kwargs})
            return result.structured_content

        assert (await server.call_tool("orbit_list_sessions", {})).structured_content["projects"] == []
        for path in (" ", str(file_path), str(missing)):
            failed = await create(path)
            assert failed.keys() == {"created", "error"}
            assert failed["created"] is False
            assert failed["error"]
        assert (await create(str(file_path), create_if_missing=True))["created"] is False
        assert not missing.exists()
        assert not (data_dir / "projects.json").exists()

        expected = {"created": True, "project_dir": str(existing.resolve()), "project_name": existing.name}
        assert await create(f"~/{existing.name}") == expected
        registry = data_dir / "projects.json"
        first_entry = registry.read_bytes()
        assert await create(str(existing / ".." / existing.name)) == expected
        assert registry.read_bytes() == first_entry
        assert await create(str(missing), create_if_missing=True) == {
            "created": True, "project_dir": str(missing.resolve()), "project_name": missing.name,
        }
        assert missing.is_dir()

        reopened = create_server(sessions_dir=sessions_dir, data_dir=data_dir)
        listed = (await reopened.call_tool("orbit_list_sessions", {"limit": 1})).structured_content
        assert listed["sessions"] == []
        assert listed["projects"] == json.loads(registry.read_text(encoding="utf-8"))["projects"]
        assert [project["project_name"] for project in listed["projects"]] == [existing.name, missing.name]
        for project, path in zip(listed["projects"], (existing, missing), strict=True):
            assert project.keys() == {"project_dir", "project_name", "created_at"}
            assert project["project_dir"] == str(path.resolve())
            assert datetime.fromisoformat(project["created_at"]).tzinfo is not None

        filtered = (await reopened.call_tool("orbit_list_sessions", {"query": " NEW FOLDER "})).structured_content
        assert filtered["projects"] == listed["projects"][1:]

    asyncio.run(verify())


def test_create_session_rejects_invalid_input(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    server = create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir)
    file_path = tmp_path / "file.txt"
    file_path.touch()

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("Invalid session attempted to spawn a process")

    monkeypatch.setattr("server.main.subprocess.Popen", unexpected_spawn)

    async def verify() -> None:
        for overrides, expected_error in (
            ({"workspace_dir": " "}, "Workspace directory is required."),
            ({"workspace_dir": str(tmp_path / "missing")}, "not an existing directory"),
            ({"workspace_dir": str(file_path)}, "not an existing directory"),
            ({"prompt": " \t\n "}, "Prompt is required."),
            ({"mode": "danger-full-access"}, "Mode must be"),
        ):
            args = {"workspace_dir": str(tmp_path), "prompt": "Start", **overrides}
            result = (await server.call_tool("orbit_create_session", args)).structured_content
            assert result.keys() == {"started", "error"}
            assert result["started"] is False
            assert expected_error in result["error"]
        assert DelegationStore(data_dir).load_all() == []

    asyncio.run(verify())


@pytest.mark.parametrize(
    "mode,title,web_search,spawn_fails",
    [(None, "", False, False), ("read-only", "  --Review  ", True, False), ("worktree", "Review", True, True)],
)
def test_create_session_launch_and_pending_visibility(tmp_path, monkeypatch, mode, title, web_search, spawn_fails) -> None:
    data_dir = tmp_path / "data"
    store = DelegationStore(data_dir)
    server = create_server(sessions_dir=tmp_path / "sessions", data_dir=data_dir)
    prompt = f"\n  {'a' * 70}\nSecond line\n"
    expected_title = title.strip() or "a" * 60
    spawned = []

    class BackgroundProcess:
        def wait(self) -> int:
            return 0

    def spawn(command, **kwargs):
        run_id = command[command.index("--run-id") + 1]
        prompt_path = Path(command[command.index("--prompt-file") + 1])
        spawned.append((run_id, prompt_path))
        assert command[:2] == [sys.executable, "-u"]
        assert Path(command[2]).name == "delegate_cli.py"
        assert "--session-id" not in command
        assert "--no-desktop-project-sync" not in command
        assert "--delete-prompt-file" in command
        assert ("--web-search" in command) is web_search
        if expected_title.startswith("-"):
            assert f"--title={expected_title}" in command
        else:
            assert command[command.index("--title") + 1] == expected_title
        for flag, expected in (
            ("--cwd", str(tmp_path.resolve())), ("--mode", mode or "workspace-write"),
            ("--data-dir", str(data_dir.resolve())), ("--source-model", "Orbit"),
        ):
            assert command[command.index(flag) + 1] == expected
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is (sys.platform != "win32")
        if sys.platform == "win32":
            assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
            assert not (kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
        assert prompt_path.read_text(encoding="utf-8") == prompt
        record = store.get_by_run_id(run_id)
        assert record.session_id is None
        [rnd] = record.rounds
        assert (rnd.run_id, rnd.status, rnd.prompt, rnd.title) == (run_id, "running", prompt, expected_title)
        if spawn_fails:
            raise OSError("spawn failed")
        return BackgroundProcess()

    monkeypatch.setattr("server.main.subprocess.Popen", spawn)

    async def verify() -> None:
        args = {"workspace_dir": str(tmp_path), "prompt": prompt, "title": title, "web_search": web_search}
        if mode is not None:
            args["mode"] = mode
        result = (await server.call_tool("orbit_create_session", args)).structured_content
        [(run_id, prompt_path)] = spawned
        if spawn_fails:
            assert result == {"started": False, "error": "Could not start session: spawn failed"}
            [rnd] = store.get_by_run_id(run_id).rounds
            assert rnd.status == "failed"
            assert rnd.ended_at and rnd.error_summary == result["error"]
            assert not prompt_path.exists()
            return

        assert result == {"started": True, "run_id": run_id, "workspace_dir": str(tmp_path.resolve()), "title": expected_title}
        listed = (await server.call_tool("orbit_list_sessions", {})).structured_content
        [session] = listed["sessions"]
        assert (session["session_id"], session["active_run_id"], session["round_count"]) == (run_id, run_id, 1)
        assert session["status"] == "running"
        detail = (await server.call_tool("orbit_get_session_detail", {"session_id": run_id})).structured_content
        assert detail["found"] is True
        assert detail["detail"]["summary"] == session
        assert detail["detail"]["turns"][0]["prompt"] == prompt

        thread_id = "11111111-2222-3333-4444-555555555555"
        store.bind_session_id(run_id, thread_id)
        listed = (await server.call_tool("orbit_list_sessions", {})).structured_content
        assert [session["session_id"] for session in listed["sessions"]] == [thread_id]
        alias = (await server.call_tool("orbit_get_session_detail", {"session_id": run_id})).structured_content
        assert alias["detail"]["summary"]["session_id"] == thread_id
        delta = (await server.call_tool("orbit_poll_session_delta", {
            "session_id": run_id, "after_meta_ts": detail["meta_ts"],
        })).structured_content["delta"]
        assert delta["changed"] and delta["reset"]
        assert delta["session_id"] == thread_id

    asyncio.run(verify())


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
        assert initial["reasoning_effort"] == "auto"
        assert [model["slug"] for model in initial["available_models"]] == ["custom-sol"]

        updates = {
            "routing_mode": "silent_best",
            "ui_locale": "ja-JP",
            "default_model": "custom-sol",
            "escalation_model": "custom-astra",
            "quota_guardrail_enabled": False,
            "quota_guardrail_threshold_pct": 35,
            "reasoning_effort": " HIGH ",
        }
        updated = (await server.call_tool("orbit_update_settings", updates)).structured_content
        assert updated == {**initial, **updates, "ui_locale": "ja", "reasoning_effort": "high"}
        with pytest.raises(ToolError, match="Invalid reasoning_effort"):
            await server.call_tool("orbit_update_settings", {"reasoning_effort": "extreme", "ui_locale": "en"})
        assert (await server.call_tool("orbit_get_settings", {})).structured_content == updated
        reset = (await server.call_tool("orbit_update_settings", {
            "default_model": "",
            "escalation_model": None,
            "reasoning_effort": "",
        })).structured_content
        assert reset == {**updated, "default_model": MODEL_SOL, "reasoning_effort": "auto"}
        assert (await server.call_tool("orbit_update_settings", {})).structured_content == reset
        await server.call_tool("orbit_update_settings", {"reasoning_effort": "max"})
        assert (await server.call_tool("orbit_update_settings", {"reasoning_effort": None})).structured_content == {
            **reset, "reasoning_effort": "max",
        }
        auto_reset = await server.call_tool("orbit_update_settings", {"reasoning_effort": " AUTO "})
        assert auto_reset.structured_content == reset
        listed = (await server.call_tool("orbit_list_sessions", {})).structured_content
        settings_fields = set(reset) - {"available_models"}
        assert {name: listed[name] for name in settings_fields} == {name: reset[name] for name in settings_fields}
        assert (await server.call_tool("orbit_run_doctor", {})).structured_content == {"ok": True, "checks": []}

        pruned = (await server.call_tool("orbit_prune_worktrees", {})).structured_content
        assert pruned == {"removed": [str(idle)], "skipped_running": [], "errors": []}
        assert not idle.exists()

    asyncio.run(verify())


@pytest.mark.parametrize("tool", ["orbit_create_session", "orbit_send_followup"])
@pytest.mark.parametrize("requested,saved,expected_flag,expected_pending", [
    (" ULTRA ", "max", "ultra", "ultra"),
    (" AuTo ", "max", None, "max"),
    ("", "max", None, "max"),
    (None, "auto", None, None),
    ("extreme", "max", None, None),
])
def test_session_launch_reasoning_effort(
    tmp_path, followup_session, monkeypatch, tool, requested, saved, expected_flag, expected_pending,
) -> None:
    sessions_dir, session_id = followup_session
    data_dir = tmp_path / "data"
    save_settings(data_dir, reasoning_effort=saved)
    store = DelegationStore(data_dir)
    server = create_server(sessions_dir=sessions_dir, data_dir=data_dir)
    spawned = []

    class BackgroundProcess:
        def wait(self) -> int:
            return 0

    def spawn(command, **kwargs):
        run_id = command[command.index("--run-id") + 1]
        spawned.append(run_id)
        [round_rec] = store.get_by_run_id(run_id).rounds
        assert round_rec.reasoning_effort == expected_pending
        if expected_flag is None:
            assert "--reasoning-effort" not in command
        else:
            assert command[command.index("--reasoning-effort") + 1] == expected_flag
        return BackgroundProcess()

    monkeypatch.setattr("server.main.subprocess.Popen", spawn)

    async def verify() -> None:
        args = {"prompt": "Review the tests", "reasoning_effort": requested}
        if tool == "orbit_send_followup":
            args["session_id"] = session_id
        else:
            args["workspace_dir"] = str(tmp_path)
        result = (await server.call_tool(tool, args)).structured_content
        if requested == "extreme":
            assert result.keys() == {"started", "error"}
            assert result["started"] is False
            assert "Invalid reasoning_effort" in result["error"]
            assert spawned == store.load_all() == []
            return

        [run_id] = spawned
        assert result["started"] is True
        assert result["run_id"] == run_id
        lookup_id = result.get("session_id", run_id)
        session = await server.call_tool("orbit_get_session_detail", {"session_id": lookup_id})
        detail = session.structured_content["detail"]
        assert detail["turns"][-1]["effort"] == expected_pending
        if expected_pending is not None:
            assert detail["summary"]["reasoning_effort"] == expected_pending

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
