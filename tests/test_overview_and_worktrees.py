"""Regression coverage for model totals and managed worktree cleanup."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from server.delegation_store import DelegationStore, prune_idle_worktrees, run_git
from server.overview_builder import build_profile_overview
from server.session_store import SessionStore


def test_model_breakdown_combines_rollouts_and_sqlite(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    delegations = DelegationStore(data_dir)
    (delegations.worktrees_dir / "first" / "nested").mkdir(parents=True)
    (delegations.worktrees_dir / "second").mkdir()
    (delegations.worktrees_dir / "file.txt").touch()
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions"
    sessions_dir.mkdir(parents=True)
    project_dir = str(tmp_path / "project")
    session_id = "11111111-2222-3333-4444-555555555555"
    events = [
        ("session_meta", {"id": session_id, "cwd": project_dir}),
        ("turn_context", {"turn_id": "t1", "model": "most-used"}),
        ("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 120}}}),
    ]
    rollout = sessions_dir / f"rollout-2026-11-27T09-00-00-{session_id}.jsonl"
    rollout.write_text(
        "".join(
            json.dumps({"type": event_type, "timestamp": "2026-11-27T09:00:00Z", "payload": payload}) + "\n"
            for event_type, payload in events
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT, created_at INTEGER, cwd TEXT, source TEXT, tokens_used INTEGER, model TEXT)"
        )
        conn.executemany(
            "INSERT INTO threads VALUES (?, 1795770000, ?, ?, ?, ?)",
            [
                (session_id, project_dir, "cli", 8000, "most-used"),
                ("history", project_dir, "cli", 80, "most-used"),
                ("runner-up", project_dir, "cli", 1000, "runner-up"),
                ("history-unknown-model", project_dir, "cli", 25, None),
                ("child-thread", project_dir, "subagent", 5000, "child"),
                ("another-project", str(tmp_path / "other"), "cli", 4500, "other-model"),
                *[(f"m{i}", project_dir, "cli", i, f"model-{i}") for i in range(1, 6)],
            ],
        )

    store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    overview = build_profile_overview(store, data_dir=data_dir, project_dir=project_dir)
    insights = overview["insights"]
    assert insights["worktree_count"] == 2
    assert insights["model_breakdown"] == [
        {"name": "most-used", "count": 2, "share_pct": 25.0, "tokens": 200},
        {"name": "runner-up", "count": 1, "share_pct": 12.5, "tokens": 1000},
        *[{"name": f"model-{i}", "count": 1, "share_pct": 12.5, "tokens": i} for i in range(5, 1, -1)],
    ]
    assert overview["kpis"]["total_tokens"] == 1240


def test_empty_overview_has_no_models_or_worktrees(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path / "sessions")
    insights = build_profile_overview(store, data_dir=tmp_path / "data")["insights"]
    assert insights["model_breakdown"] == []
    assert insights["worktree_count"] == 0


def _git(repo: Path, *args: str) -> str:
    result = run_git(repo, list(args))
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_prune_unregisters_idle_worktrees_and_skips_running(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "-c", "user.name=Orbit", "-c", "user.email=orbit@example.test", "commit", "--allow-empty", "-m", "init")
    store = DelegationStore(tmp_path / "data")
    store.worktrees_dir.mkdir(parents=True)
    idle = store.worktrees_dir / "completed"
    orphan = store.worktrees_dir / "no-record"
    active = store.worktrees_dir / "running"
    for worktree in (idle, orphan, active):
        _git(repo, "worktree", "add", "--detach", str(worktree))
    (idle / "untracked.txt").touch()
    leftover = store.worktrees_dir / "leftover"
    leftover.mkdir()
    (leftover / "stale.txt").touch()
    (store.worktrees_dir / "keep-file.txt").touch()

    for worktree in (active, idle):
        store.start_round(
            run_id=worktree.name,
            workspace_dir=str(repo),
            prompt="Test",
            isolation_mode="worktree",
            worktree_path=str(worktree),
        )
    store.update_round(idle.name, status="completed")

    result = prune_idle_worktrees(store)
    assert set(result["removed"]) == {str(idle), str(orphan), str(leftover)}
    assert result["skipped_running"] == [str(active)]
    assert result["errors"] == []
    assert {path.name for path in store.worktrees_dir.iterdir()} == {active.name, "keep-file.txt"}
    assert len(_git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 3
    assert prune_idle_worktrees(store) == {"removed": [], "skipped_running": [str(active)], "errors": []}


@pytest.mark.parametrize("root_is_link", [False, True])
def test_prune_refuses_redirected_directories(tmp_path: Path, root_is_link: bool) -> None:
    store = DelegationStore(tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").touch()
    if root_is_link:
        store.worktrees_dir.parent.mkdir()
        link = store.worktrees_dir
    else:
        store.worktrees_dir.mkdir(parents=True)
        link = store.worktrees_dir / "link"
    if os.name == "nt":
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)

    result = prune_idle_worktrees(store)
    assert result["removed"] == result["skipped_running"] == []
    assert len(result["errors"]) == 1
    assert (outside / "keep.txt").is_file()


def test_prune_missing_root_is_idempotent(tmp_path: Path) -> None:
    store = DelegationStore(tmp_path / "data")
    assert prune_idle_worktrees(store) == {"removed": [], "skipped_running": [], "errors": []}
