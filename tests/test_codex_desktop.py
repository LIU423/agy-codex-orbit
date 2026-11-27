"""Unit tests for Codex Desktop project sync, `agy:` thread naming, and quota parsing."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from server import codex_desktop
from server.codex_desktop import (
    ensure_codex_project_and_bind,
    format_agy_title,
    query_codex_quota,
    read_cached_codex_quota,
    read_rollout_fallback_quota_status,
    set_codex_thread_name,
    write_cached_codex_quota,
)


def _init_fake_codex_home(tmp_path: Path) -> Path:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True)

    # Create synthetic state_5.sqlite matching Codex Desktop schema
    db_path = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            metadata TEXT,
            position INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE project_roots (
            project_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            path TEXT NOT NULL
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            name TEXT,
            project_id TEXT,
            source TEXT,
            rollout_path TEXT
        );
        """
    )
    rollout1 = codex_home / "sessions" / "rollout-sess-1.jsonl"
    rollout1.parent.mkdir(parents=True, exist_ok=True)
    rollout1.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sess-1", "source": "exec"}})
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
        + "\n",
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO threads (id, name, project_id, source, rollout_path) VALUES (?, ?, ?, ?, ?)",
        ("sess-1", "", None, "exec", str(rollout1)),
    )
    conn.execute(
        "INSERT INTO threads (id, name, project_id, source, rollout_path) VALUES (?, ?, ?, ?, ?)",
        ("sess-2", "", None, "exec", None),
    )
    conn.commit()
    conn.close()

    # Create synthetic .codex-global-state.sqlite (current Codex Desktop store)
    gs_db_path = codex_home / ".codex-global-state.sqlite"
    gs_conn = sqlite3.connect(str(gs_db_path))
    gs_conn.executescript(
        """
        CREATE TABLE settings (
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (scope, key)
        );
        """
    )
    gs_conn.execute("INSERT INTO settings VALUES ('global', 'local-projects', '{}')")
    gs_conn.execute("INSERT INTO settings VALUES ('global', 'project-order', '[]')")
    gs_conn.execute("INSERT INTO settings VALUES ('global', 'thread-project-assignments', '{}')")
    gs_conn.execute("INSERT INTO settings VALUES ('global', 'thread-project-membership-host-ids', '{}')")
    gs_conn.execute("INSERT INTO settings VALUES ('global', 'projectless-thread-ids', '[\"sess-1\"]')")
    gs_conn.execute(
        "INSERT INTO settings VALUES ('global', 'app-server-project-id-by-legacy-project-id-by-host', ?)",
        (json.dumps({f"local:{codex_home}": {}}),),
    )
    gs_conn.commit()
    gs_conn.close()

    # Create synthetic .codex-global-state.json (legacy fallback)
    gs = {
        "local-projects": {},
        "project-order": [],
        "thread-project-assignments": {},
        "projectless-thread-ids": ["sess-1"],
    }
    (codex_home / ".codex-global-state.json").write_text(json.dumps(gs, indent=2), encoding="utf-8")
    return codex_home


def test_format_agy_title_normalizes_prefix() -> None:
    assert format_agy_title("Build parser API") == "agy: Build parser API"
    assert format_agy_title("agy: Build parser API") == "agy: Build parser API"
    assert format_agy_title("AGY:   Fix   spacing  ") == "agy: Fix spacing"
    assert format_agy_title("") == "agy: Codex Task"


def test_ensure_codex_project_creates_reuses_and_backs_up(tmp_path: Path) -> None:
    codex_home = _init_fake_codex_home(tmp_path)
    workspace = tmp_path / "my-backend-app"
    workspace.mkdir()

    # First call: project missing -> creates project, backs up JSON, binds sess-1
    res1 = ensure_codex_project_and_bind(workspace, "sess-1", codex_home=codex_home, allow_create=True)
    assert res1["created"] is True
    assert res1["bound"] is True
    assert res1["project_id"] is not None
    assert (codex_home / ".codex-global-state.json.agy-orbit.bak").is_file()

    gs_data = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
    pid = res1["project_id"]
    assert pid in gs_data["local-projects"]
    assert pid in gs_data["project-order"]
    assert gs_data["thread-project-assignments"]["sess-1"] == {"projectKind": "local", "projectId": pid}
    assert "sess-1" not in gs_data["projectless-thread-ids"]

    conn = sqlite3.connect(str(codex_home / "state_5.sqlite"))
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    assert conn.execute("SELECT position FROM projects WHERE id = ?", (pid,)).fetchone()[0] == 0
    assert conn.execute("SELECT project_id, source FROM threads WHERE id = 'sess-1'").fetchone() == (pid, "vscode")
    conn.close()
    rollout1_lines = (codex_home / "sessions" / "rollout-sess-1.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(rollout1_lines[0])["payload"]["source"] == "vscode"
    assert len(rollout1_lines) == 2

    # Verify .codex-global-state.sqlite synced & backed up cleanly
    assert (codex_home / ".codex-global-state.sqlite.agy-orbit.bak").is_file()
    gs_conn = sqlite3.connect(str(codex_home / ".codex-global-state.sqlite"))
    lp = json.loads(gs_conn.execute("SELECT value FROM settings WHERE scope='global' AND key='local-projects'").fetchone()[0])
    po = json.loads(gs_conn.execute("SELECT value FROM settings WHERE scope='global' AND key='project-order'").fetchone()[0])
    ta = json.loads(gs_conn.execute("SELECT value FROM settings WHERE scope='global' AND key='thread-project-assignments'").fetchone()[0])
    pl = json.loads(gs_conn.execute("SELECT value FROM settings WHERE scope='global' AND key='projectless-thread-ids'").fetchone()[0])
    idmap = json.loads(gs_conn.execute("SELECT value FROM settings WHERE scope='global' AND key='app-server-project-id-by-legacy-project-id-by-host'").fetchone()[0])
    gs_conn.close()
    assert pid in lp
    assert po[0] == pid
    assert ta["sess-1"] == {"projectKind": "local", "projectId": pid}
    assert "sess-1" not in pl
    assert any(m.get(pid) == pid for m in idmap.values())

    # Creating a second new project places it at position 0 (top of sidebar) and shifts older ones down
    workspace2 = tmp_path / "second-project"
    workspace2.mkdir()
    res_new = ensure_codex_project_and_bind(workspace2, None, codex_home=codex_home, allow_create=True)
    assert res_new["created"] is True
    pid2 = res_new["project_id"]
    conn = sqlite3.connect(str(codex_home / "state_5.sqlite"))
    assert conn.execute("SELECT position FROM projects WHERE id = ?", (pid2,)).fetchone()[0] == 0
    assert conn.execute("SELECT position FROM projects WHERE id = ?", (pid,)).fetchone()[0] == 1
    conn.close()
    gs_after = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
    assert gs_after["project-order"][0] == pid2

    # Second call with same workspace (different case on Windows) -> reuses project, no duplicate
    workspace_again = str(workspace).upper() if sys.platform == "win32" else workspace
    res2 = ensure_codex_project_and_bind(workspace_again, "sess-2", codex_home=codex_home, allow_create=True)
    assert res2["created"] is False
    assert res2["bound"] is True
    assert res2["project_id"] == pid

    conn = sqlite3.connect(str(codex_home / "state_5.sqlite"))
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
    assert conn.execute("SELECT project_id FROM threads WHERE id = 'sess-2'").fetchone()[0] == pid
    conn.close()


def test_ensure_codex_project_degrades_gracefully_on_schema_drift(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / ".codex-global-state.json").write_text('{"local-projects": "unexpected-type"}', encoding="utf-8")

    workspace = tmp_path / "drift-repo"
    workspace.mkdir()
    res = ensure_codex_project_and_bind(workspace, "sess-x", codex_home=codex_home)
    assert res["created"] is False
    assert "warnings" in res and len(res["warnings"]) >= 1
    # Original file must remain intact
    assert (codex_home / ".codex-global-state.json").read_text(encoding="utf-8") == '{"local-projects": "unexpected-type"}'


def test_set_thread_name_fallback_and_quota_rollout_fallback(tmp_path: Path, monkeypatch) -> None:
    codex_home = _init_fake_codex_home(tmp_path)
    res = set_codex_thread_name("sess-1", "后端权限重构", codex_home=codex_home, use_rpc=False)
    assert res["title"] == "agy: 后端权限重构"
    index_lines = (codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(index_lines[-1])["thread_name"] == "agy: 后端权限重构"

    conn = sqlite3.connect(str(codex_home / "state_5.sqlite"))
    assert conn.execute("SELECT name FROM threads WHERE id = 'sess-1'").fetchone()[0] == "agy: 后端权限重构"
    conn.close()

    # Rollout cache fallback for quota when codex binary is unavailable
    day_dir = codex_home / "sessions" / "2026" / "11" / "26"
    day_dir.mkdir(parents=True)
    rollout = day_dir / "rollout-2026-11-26T10-00-00-11111111-2222-3333-4444-555555555555.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "plus",
                        "primary": {"used_percent": 15.0, "window_minutes": 300, "resets_at": 1800000000},
                        "secondary": {"used_percent": 22.5, "window_minutes": 10080, "resets_at": 1800500000},
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    data_dir = tmp_path / "data"
    quota = query_codex_quota(codex_bin=tmp_path / "missing-codex.exe", codex_home=codex_home, data_dir=data_dir)
    assert quota["codex"]["available"] is True
    assert quota["codex"]["source"] == "rollout_cache"
    assert quota["codex"]["primary"]["remaining_percent"] == 85.0
    assert quota["codex"]["secondary"]["remaining_percent"] == 77.5
    assert quota["antigravity"]["available"] is False
    assert not (data_dir / "quota_cache.json").exists()

    def unexpected_binary_lookup(*args, **kwargs):
        pytest.fail("Rollout fallback must not locate or launch Codex.")

    monkeypatch.setattr(codex_desktop, "locate_codex_binary", unexpected_binary_lookup)
    fallback = read_rollout_fallback_quota_status(str(codex_home))
    assert fallback is not None
    assert fallback["codex"]["source"] == "rollout_cache"
    assert fallback["codex"]["primary"]["remaining_percent"] == 85.0
    assert fallback["antigravity"] == quota["antigravity"]
    assert read_rollout_fallback_quota_status(tmp_path / "missing-home") is None


def test_quota_disk_cache_freshness_and_unavailable_write(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CODEX_ORBIT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(codex_desktop.time, "time", lambda: 400.0)
    data = {"codex": {"available": True, "source": "app_server"}, "antigravity": {"available": False}}

    write_cached_codex_quota({"codex": {"available": False}})
    path = data_dir / "quota_cache.json"
    assert not path.exists()
    write_cached_codex_quota(data)
    assert json.loads(path.read_text(encoding="utf-8")) == {"fetched_at": 400.0, "data": data}
    assert read_cached_codex_quota(data_dir=str(data_dir)) == (data, True)

    monkeypatch.setattr(codex_desktop.time, "time", lambda: 460.0)
    write_cached_codex_quota({"codex": {"available": False}})
    assert read_cached_codex_quota() == (data, True)
    assert read_cached_codex_quota(max_age_seconds=59.0) is None

    monkeypatch.setattr(codex_desktop.time, "time", lambda: 461.0)
    assert read_cached_codex_quota() is None
    assert read_cached_codex_quota(allow_stale=True) == (data, False)


@pytest.mark.parametrize(
    "payload",
    [
        "bad JSON",
        "[]",
        '{"fetched_at": "invalid", "data": {"codex": {"available": true}}}',
        '{"fetched_at": true, "data": {"codex": {"available": true}}}',
        '{"fetched_at": NaN, "data": {"codex": {"available": true}}}',
        '{"fetched_at": 123, "data": {"codex": {"available": false}}}',
        '{"fetched_at": 123, "data": {"codex": []}}',
    ],
)
def test_quota_disk_cache_ignores_invalid_entries(tmp_path: Path, payload: str) -> None:
    (tmp_path / "quota_cache.json").write_text(payload, encoding="utf-8")
    assert read_cached_codex_quota(data_dir=tmp_path, allow_stale=True) is None


def test_quota_query_uses_fresh_disk_cache_and_replaces_it_after_live_query(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    codex_home = tmp_path / "codex-home"
    binary = tmp_path / "codex"
    calls = []
    monkeypatch.setattr(codex_desktop, "locate_codex_binary", lambda explicit: binary)

    def read_rate_limits(codex_bin, method, params, *, timeout, codex_home):
        assert codex_bin == binary
        assert method == "account/rateLimits/read"
        calls.append(codex_home)
        return {"rateLimits": {"primary": {"usedPercent": 10 * len(calls)}}}

    monkeypatch.setattr(codex_desktop, "_call_app_server_rpc", read_rate_limits)
    monkeypatch.setattr(codex_desktop.time, "time", lambda: 100.0)

    first = query_codex_quota(codex_home=codex_home, data_dir=data_dir, use_cache=True)
    assert first["codex"]["primary"]["remaining_percent"] == 90.0
    assert read_cached_codex_quota(data_dir=data_dir) == (first, True)
    assert query_codex_quota(codex_home=codex_home, data_dir=data_dir, use_cache=True) == first
    assert calls == [codex_home]

    second = query_codex_quota(codex_home=codex_home, data_dir=data_dir)
    assert second["codex"]["primary"]["remaining_percent"] == 80.0
    assert calls == [codex_home, codex_home]
    assert read_cached_codex_quota(data_dir=data_dir) == (second, True)

    monkeypatch.setattr(codex_desktop.time, "time", lambda: 130.0)
    third = query_codex_quota(codex_home=codex_home, data_dir=data_dir, use_cache=True, max_age_seconds=10)
    assert third["codex"]["primary"]["remaining_percent"] == 70.0
    assert calls == [codex_home, codex_home, codex_home]


def test_quota_query_keeps_live_result_when_cache_write_fails(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    previous = {"codex": {"available": True}}
    write_cached_codex_quota(previous, data_dir=data_dir)

    def replace(source, destination):
        raise PermissionError("quota cache replacement failed")

    monkeypatch.setattr(codex_desktop.os, "replace", replace)
    monkeypatch.setattr(codex_desktop, "locate_codex_binary", lambda explicit: tmp_path / "codex")
    monkeypatch.setattr(codex_desktop, "_call_app_server_rpc", lambda *args, **kwargs: {})

    quota = query_codex_quota(data_dir=data_dir)
    assert quota["codex"]["source"] == "app_server"
    assert read_cached_codex_quota(data_dir=data_dir) == (previous, True)
    assert list(data_dir.iterdir()) == [data_dir / "quota_cache.json"]
