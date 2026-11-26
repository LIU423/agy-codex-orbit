"""Unit tests for M2 Codex locator, delegation metadata store, and delegate CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.codex_locator import CodexBinaryNotFoundError, locate_codex_binary
from server.delegate_cli import parse_args, run_delegation
from server.delegation_store import DelegationStore
from server.session_store import SessionStore

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "rollout-2026-11-26T10-00-00-11111111-2222-3333-4444-555555555555.jsonl"
)


def _create_fake_codex_cmd(tmp_path: Path, session_id: str) -> Path:
    """Create a cross-platform fake codex stub executable (Windows .cmd + python script)."""
    stub_py = tmp_path / "fake_codex.py"
    stub_py.write_text(
        f"""
import json, pathlib, sys

args = sys.argv[1:]
prompt = sys.stdin.read().strip()

out_file = None
for idx, a in enumerate(args):
    if a in ("-o", "--output-last-message") and idx + 1 < len(args):
        out_file = pathlib.Path(args[idx + 1])

is_resume = "resume" in args
turn_id = "turn-002" if is_resume else "turn-001"

# Create a file when not in read-only mode to verify worktree isolation
if "read-only" not in " ".join(args):
    target = pathlib.Path.cwd() / ("round2.txt" if is_resume else "round1.txt")
    target.write_text(prompt, encoding="utf-8")

events = [
    {{"type": "thread.started", "thread_id": "{session_id}"}},
    {{"type": "turn.started", "turn_id": turn_id}},
    {{"type": "item.completed", "item": {{"type": "command_execution", "command": "pytest -q", "exit_code": 0}}}},
    {{"type": "item.completed", "item": {{"type": "agent_message", "text": f"Completed {{turn_id}} for: {{prompt[:40]}}"}}}},
    {{"type": "turn.completed"}},
]
for ev in events:
    print(json.dumps(ev), flush=True)

if out_file:
    out_file.write_text(f"Completed {{turn_id}} for: {{prompt[:40]}}", encoding="utf-8")
""",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        wrapper = tmp_path / "fake_codex.cmd"
        wrapper.write_text(f'@echo off\n"{sys.executable}" "{stub_py}" %*\n', encoding="utf-8")
        return wrapper
    wrapper = tmp_path / "fake_codex.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _init_temp_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(repo_dir), check=True)
    (repo_dir / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)


def test_codex_locator_explicit_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "codex.exe"
    fake_bin.write_text("stub", encoding="utf-8")

    assert locate_codex_binary(fake_bin) == fake_bin.resolve()

    monkeypatch.setenv("CODEX_ORBIT_CODEX_BIN", str(fake_bin))
    assert locate_codex_binary() == fake_bin.resolve()

    with pytest.raises(CodexBinaryNotFoundError):
        locate_codex_binary(tmp_path / "missing-codex.exe")


def test_delegate_cli_worktree_isolation_and_multi_turn_resume(tmp_path: Path) -> None:
    repo_dir = tmp_path / "demo-repo"
    _init_temp_git_repo(repo_dir)
    data_dir = tmp_path / "plugin-data"
    sid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    fake_codex = _create_fake_codex_cmd(tmp_path, sid)

    # Round 1: worktree isolation
    args1 = parse_args(
        [
            "--cwd",
            str(repo_dir),
            "--mode",
            "worktree",
            "--source-model",
            "Gemini 2.5 Pro",
            "--prompt",
            "Implement round 1 change",
            "--data-dir",
            str(data_dir),
            "--codex-bin",
            str(fake_codex),
        ]
    )
    code1, summary1 = run_delegation(args1)
    assert code1 == 0
    assert summary1["status"] == "completed"
    assert summary1["session_id"] == sid
    assert summary1["round"] == 1
    assert summary1["isolation_mode"] == "worktree"
    assert summary1["command_count"] == 1
    assert "round1.txt" in summary1["changed_files"]
    assert summary1["worktree_path"] is not None
    assert Path(summary1["worktree_path"]).is_dir()
    # Main working tree must remain untouched!
    assert not (repo_dir / "round1.txt").exists()

    # Round 2: resume same session, automatically reuses the exact same worktree
    args2 = parse_args(
        [
            "--session-id",
            sid,
            "--source-model",
            "Gemini 2.5 Pro",
            "--prompt",
            "Follow up in round 2",
            "--data-dir",
            str(data_dir),
            "--codex-bin",
            str(fake_codex),
        ]
    )
    code2, summary2 = run_delegation(args2)
    assert code2 == 0
    assert summary2["round"] == 2
    assert summary2["isolation_mode"] == "worktree"
    assert summary2["worktree_path"] == summary1["worktree_path"]
    assert "round2.txt" in summary2["changed_files"]
    assert not (repo_dir / "round2.txt").exists()

    # Verify DelegationStore persisted both rounds cleanly under session_id
    store = DelegationStore(data_dir=data_dir)
    rec = store.get_by_session_id(sid, verify_liveness=False)
    assert rec is not None
    assert len(rec.rounds) == 2
    assert [r.round_index for r in rec.rounds] == [1, 2]
    assert rec.rounds[0].source_model == "Gemini 2.5 Pro"


def test_session_store_merges_pending_and_bound_delegations(tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin-data"
    sessions_dir = tmp_path / "sessions" / "2026" / "11" / "26"
    sessions_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE_PATH, sessions_dir / FIXTURE_PATH.name)

    dstore = DelegationStore(data_dir=data_dir)
    bound_sid = "11111111-2222-3333-4444-555555555555"
    dstore.start_round(
        run_id="run-bound-1",
        workspace_dir="D:\\demo-workspace\\sample-app",
        prompt="Delegated prompt round 1",
        isolation_mode="worktree",
        source_model="Gemini",
        session_id=bound_sid,
        worktree_path="D:\\tmp\\wt-1",
        worktree_branch="codex-orbit/run-bound-1",
        pid=os.getpid(),
    )
    dstore.update_round("run-bound-1", turn_id="turn-001", status="completed", ended=True)

    # Also create a pending delegation not yet written to any rollout file
    dstore.start_round(
        run_id="run-pending-1",
        workspace_dir="D:\\demo-workspace\\brand-new",
        prompt="Read-only audit pending rollout flush",
        isolation_mode="read-only",
        source_model="Gemini",
        pid=os.getpid(),
    )

    sstore = SessionStore(sessions_dir=tmp_path / "sessions", delegation_store=dstore)
    listed = sstore.list_sessions()
    by_id = {s.session_id: s for s in listed}

    # Pending run appears immediately
    assert "run-pending-1" in by_id
    assert by_id["run-pending-1"].is_delegated is True
    assert by_id["run-pending-1"].status == "running"
    assert by_id["run-pending-1"].isolation_mode == "read-only"

    # Bound rollout session is enriched with delegation metadata
    assert bound_sid in by_id
    assert by_id[bound_sid].is_delegated is True
    assert by_id[bound_sid].source_model == "Gemini"
    assert by_id[bound_sid].isolation_mode == "worktree"

    detail, meta_ts = sstore.get_session_detail(bound_sid)
    assert detail is not None
    assert meta_ts != ""
    assert detail.turns[0].is_delegated is True
    assert detail.turns[0].delegation_round == 1
    assert detail.turns[0].source_model == "Gemini"

    # Starting Round 2 triggers delta via meta_ts even before rollout grows
    dstore.start_round(
        run_id="run-bound-2",
        workspace_dir="D:\\demo-workspace\\sample-app",
        prompt="Round 2 follow-up prompt",
        isolation_mode="worktree",
        source_model="Gemini",
        session_id=bound_sid,
        pid=os.getpid(),
    )
    delta, meta_ts_2 = sstore.poll_session_delta(
        bound_sid,
        after_cursor=detail.cursor,
        after_meta_ts=meta_ts,
    )
    assert delta is not None
    assert delta.changed is True
    assert meta_ts_2 != meta_ts
    assert len(delta.updated_turns) == 2
    assert delta.updated_turns[1].delegation_round == 2
    assert delta.updated_turns[1].status == "running"
