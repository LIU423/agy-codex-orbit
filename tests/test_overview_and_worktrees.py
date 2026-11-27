"""Regression coverage for model totals and managed worktree cleanup."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from server.delegation_store import DelegationStore, prune_idle_worktrees, run_git
from server.overview_builder import build_profile_overview
from server.rollout_parser import STALE_RUNNING_SECONDS, RolloutParseState
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


def _rollout_line(event_type: str, payload: dict) -> str:
    return json.dumps({"type": event_type, "timestamp": "2026-11-27T09:00:00Z", "payload": payload}) + "\n"


def _write_summary_rollout(
    sessions_dir: Path, session_id: str, *, source: str = "cli", changed_file: str = "src/core.py",
) -> Path:
    path = sessions_dir / f"rollout-{session_id}.jsonl"
    path.write_text(
        _rollout_line("session_meta", {"id": session_id, "cwd": str(sessions_dir.parent), "source": source})
        + _rollout_line("turn_context", {"turn_id": "turn-1", "model": "old-model"})
        + _rollout_line("event_msg", {"turn_id": "turn-1", "type": "task_started"})
        + _rollout_line("event_msg", {
            "turn_id": "turn-1", "type": "item_completed",
            "item": {"type": "FileChange", "changes": {changed_file: {"type": "update"}}},
        }),
        encoding="utf-8",
    )
    return path


def test_summary_cache_hit_reuses_rollouts_for_overview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delegations = DelegationStore(tmp_path / "data")
    sessions_dir = tmp_path / "codex" / "sessions"
    sessions_dir.mkdir(parents=True)
    rollout = _write_summary_rollout(sessions_dir, "direct")
    _write_summary_rollout(sessions_dir, "child", source="subagent", changed_file="src/child.py")
    index = sessions_dir.parent / "session_index.jsonl"
    index.write_text(json.dumps({"id": "direct", "thread_name": "agy: Previous title"}) + "\n", encoding="utf-8")

    store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    cold_overview = build_profile_overview(store, data_dir=delegations.data_dir)
    assert cold_overview["recent_sessions"][0]["title"] == "agy: Previous title"
    assert cold_overview["hot_files"] == [{"path": "src/core.py", "short_name": "src/core.py", "session_count": 1}]

    cache_path = delegations.data_dir / "session_summary_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["schema_version"] == 1
    entries = cache["entries"]
    assert {entry["session_id"]: entry["is_subagent"] for entry in entries.values()} == {"direct": False, "child": True}
    direct_entry = entries[os.path.normcase(os.path.abspath(rollout))]
    assert direct_entry["summary"]["title"] == "Session direct"
    assert direct_entry["summary"]["is_delegated"] is False
    assert direct_entry["changed_file_paths"] == ["src/core.py"]

    def unexpected_io(*args, **kwargs):
        pytest.fail("a summary cache hit should not parse a rollout or rewrite the cache")

    monkeypatch.setattr(RolloutParseState, "feed_file", unexpected_io)
    monkeypatch.setattr("server.session_store._atomic_write_json", unexpected_io)
    index.write_text(json.dumps({"id": "direct", "thread_name": "Current title"}) + "\n", encoding="utf-8")
    cached_store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    assert cached_store.get_session_changed_files("direct") == ["src/core.py"]
    [summary] = cached_store.list_sessions(query="current title")
    assert (summary.title, summary.is_delegated, summary.source_model) == ("Current title", False, None)
    assert cached_store._path_by_session_id["direct"] == rollout

    at_mtime = rollout.stat().st_mtime_ns / 1e9
    assert cached_store.list_sessions(now_ts=at_mtime)[0].status == "running"
    assert cached_store.list_sessions(now_ts=at_mtime + STALE_RUNNING_SECONDS + 1)[0].status == "idle"
    assert build_profile_overview(cached_store, data_dir=delegations.data_dir)["hot_files"] == cold_overview["hot_files"]
    index.unlink()
    assert cached_store.list_sessions()[0].title == "Session direct"


@pytest.mark.parametrize("changed_field,warm", [("mtime", False), ("mtime", True), ("size", False)])
def test_summary_cache_invalidates_when_rollout_changes(tmp_path: Path, changed_field: str, warm: bool) -> None:
    delegations = DelegationStore(tmp_path / "data")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    path = _write_summary_rollout(sessions_dir, "direct")
    original = path.stat()
    store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    assert store.list_sessions()[0].model == "old-model"

    if changed_field == "mtime":
        path.write_text(path.read_text(encoding="utf-8").replace("old-model", "new-model"), encoding="utf-8")
        mtime_ns = original.st_mtime_ns + 1_000_000_000
        assert path.stat().st_size == original.st_size
    else:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_rollout_line("turn_context", {"turn_id": "turn-2", "model": "new-model"}))
        mtime_ns = original.st_mtime_ns
    os.utime(path, ns=(original.st_atime_ns, mtime_ns))
    if warm:
        # A same-size edit seen first by the tailer must still invalidate the persistent summary.
        assert store.get_session_detail("direct")[0] is not None
    else:
        store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)

    assert store.list_sessions()[0].model == "new-model"
    assert SessionStore(sessions_dir=sessions_dir, delegation_store=delegations).list_sessions()[0].model == "new-model"


def test_summary_cache_uses_latest_delegation_and_exact_round_count(tmp_path: Path) -> None:
    delegations = DelegationStore(tmp_path / "data")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    _write_summary_rollout(sessions_dir, "direct")
    store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    assert store.list_sessions()[0].round_count == 1

    delegations.start_round(
        run_id="run-new", session_id="direct", workspace_dir=str(tmp_path / "workspace"),
        title="Follow up", prompt="Continue working", isolation_mode="workspace-write", source_model="Orbit",
        model="followup-model", reasoning_effort="high", pid=os.getpid(),
    )
    cached_store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    [summary] = cached_store.list_sessions()
    detail, _ = cached_store.get_session_detail("direct")
    assert detail is not None and summary == detail.summary
    assert (summary.round_count, summary.status, summary.active_run_id) == (2, "running", "run-new")
    assert (summary.title, summary.model, summary.reasoning_effort) == ("Follow up", "followup-model", "high")


def test_summary_cache_persists_only_500_newest_entries(tmp_path: Path) -> None:
    delegations = DelegationStore(tmp_path / "data")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for i in range(501):
        path = sessions_dir / f"rollout-{i}.jsonl"
        path.write_text(_rollout_line("session_meta", {"id": str(i)}), encoding="utf-8")
        mtime_ns = 1_795_773_600_000_000_000 + i * 1_000_000_000
        os.utime(path, ns=(mtime_ns, mtime_ns))

    store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    assert len(store.list_sessions(limit=501)) == 501
    cache = json.loads((delegations.data_dir / "session_summary_cache.json").read_text(encoding="utf-8"))
    assert {entry["session_id"] for entry in cache["entries"].values()} == {str(i) for i in range(1, 501)}


def test_summary_cache_and_tailer_share_a_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delegations = DelegationStore(tmp_path / "data")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    _write_summary_rollout(sessions_dir, "direct")
    store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
    original_feed = RolloutParseState.feed_file
    list_feeding, release_list, detail_started, detail_feeding = (Event() for _ in range(4))

    def feed(state: RolloutParseState):
        if list_feeding.is_set():
            detail_feeding.set()
        else:
            list_feeding.set()
            assert release_list.wait(5)
        return original_feed(state)

    def get_detail():
        detail_started.set()
        return store.get_session_detail("direct")[0]

    monkeypatch.setattr(RolloutParseState, "feed_file", feed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        listed = pool.submit(store.list_sessions)
        assert list_feeding.wait(5)
        try:
            detail_result = pool.submit(get_detail)
            assert detail_started.wait(5)
            assert not detail_feeding.wait(0.1)
        finally:
            release_list.set()
        detail = detail_result.result(timeout=5)
        assert detail_feeding.is_set()
        assert detail is not None and listed.result(timeout=5) == [detail.summary]
    delta, _ = store.poll_session_delta("direct")
    assert delta is not None and delta.summary == detail.summary


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
