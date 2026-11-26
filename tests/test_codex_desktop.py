"""Unit tests for Codex Desktop project sync, `agy:` thread naming, and quota parsing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from server.codex_desktop import (
    ensure_codex_project_and_bind,
    format_agy_title,
    query_codex_quota,
    set_codex_thread_name,
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
            project_id TEXT
        );
        """
    )
    conn.execute("INSERT INTO threads (id, name, project_id) VALUES (?, ?, ?)", ("sess-1", "", None))
    conn.execute("INSERT INTO threads (id, name, project_id) VALUES (?, ?, ?)", ("sess-2", "", None))
    conn.commit()
    conn.close()

    # Create synthetic .codex-global-state.json
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
    assert conn.execute("SELECT project_id FROM threads WHERE id = 'sess-1'").fetchone()[0] == pid
    conn.close()

    # Second call with same workspace (different case on Windows) -> reuses project, no duplicate
    res2 = ensure_codex_project_and_bind(str(workspace).upper(), "sess-2", codex_home=codex_home, allow_create=True)
    assert res2["created"] is False
    assert res2["bound"] is True
    assert res2["project_id"] == pid

    conn = sqlite3.connect(str(codex_home / "state_5.sqlite"))
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
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


def test_set_thread_name_fallback_and_quota_rollout_fallback(tmp_path: Path) -> None:
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

    quota = query_codex_quota(codex_bin=tmp_path / "missing-codex.exe", codex_home=codex_home)
    assert quota["codex"]["available"] is True
    assert quota["codex"]["source"] == "rollout_cache"
    assert quota["codex"]["primary"]["remaining_percent"] == 85.0
    assert quota["codex"]["secondary"]["remaining_percent"] == 77.5
    assert quota["antigravity"]["available"] is False
