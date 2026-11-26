"""Unit and integration tests for M3 process-tree cancellation and edge cases."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from server.delegate_cli import parse_args, run_delegation
from server.delegation_store import DelegationStore, is_process_alive
from server.process_control import cancel_delegation


def test_cancel_delegation_terminates_process_tree_and_is_idempotent(tmp_path: Path) -> None:
    """Spawn a real child + grandchild process tree and verify clean termination."""
    grandchild_pid_file = tmp_path / "grandchild.pid"
    child_script = tmp_path / "spawn_tree.py"
    child_script.write_text(
        f"""
import pathlib, subprocess, sys, time
gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(gc.pid), encoding="utf-8")
time.sleep(60)
""",
        encoding="utf-8",
    )

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    child_proc = subprocess.Popen(
        [sys.executable, str(child_script)],
        creationflags=creationflags,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not grandchild_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert grandchild_pid_file.is_file(), "Grandchild process failed to start"
        grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())

        assert is_process_alive(child_proc.pid)
        assert is_process_alive(grandchild_pid)

        store = DelegationStore(data_dir=tmp_path / "plugin-data")
        sid = "cccccccc-1111-2222-3333-444444444444"
        store.start_round(
            run_id="run-cancel-1",
            workspace_dir=str(tmp_path),
            prompt="Long-running task",
            isolation_mode="read-only",
            source_model="Gemini",
            session_id=sid,
            pid=None,
        )
        store.update_round("run-cancel-1", child_pid=child_proc.pid)

        res1 = cancel_delegation(store, session_id=sid, run_id="run-cancel-1")
        assert res1.cancelled is True
        assert res1.already_finished is False
        assert child_proc.pid in res1.terminated_pids

        # Verify both child and grandchild are gone
        child_proc.wait(timeout=5)
        wait_gc = time.monotonic() + 3.0
        while is_process_alive(grandchild_pid) and time.monotonic() < wait_gc:
            time.sleep(0.05)
        assert not is_process_alive(child_proc.pid)
        assert not is_process_alive(grandchild_pid)

        rec = store.get_by_session_id(sid, verify_liveness=False)
        assert rec is not None
        assert rec.rounds[0].status == "cancelled"

        # Second cancel call must be cleanly idempotent
        res2 = cancel_delegation(store, session_id=sid, run_id="run-cancel-1")
        assert res2.cancelled is False
        assert res2.already_finished is True
    finally:
        if child_proc.poll() is None:
            child_proc.kill()


def test_delegate_cli_missing_codex_binary_returns_clean_error(tmp_path: Path) -> None:
    args = parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--mode",
            "read-only",
            "--prompt",
            "Check status",
            "--data-dir",
            str(tmp_path / "data"),
            "--codex-bin",
            str(tmp_path / "nonexistent-codex.exe"),
        ]
    )
    code, summary = run_delegation(args)
    assert code == 2
    assert summary["status"] == "failed"
    assert "not found" in summary["error"].lower()


def _make_stub_codex(tmp_path: Path, name: str, py_body: str) -> Path:
    stub_py = tmp_path / f"{name}.py"
    stub_py.write_text(py_body, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = tmp_path / f"{name}.cmd"
        wrapper.write_text(f'@echo off\n"{sys.executable}" "{stub_py}" %*\n', encoding="utf-8")
        return wrapper
    wrapper = tmp_path / f"{name}.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def test_delegate_cli_heavy_stderr_does_not_deadlock(tmp_path: Path) -> None:
    """Emit >128 KB to stderr before stdout JSON events; file-backed stderr must never deadlock."""
    sid = "dddddddd-1111-2222-3333-444444444444"
    fake_codex = _make_stub_codex(
        tmp_path,
        "heavy_stderr_codex",
        f"""
import json, pathlib, sys
args = sys.argv[1:]
_ = sys.stdin.read()
# Write 200 KB to stderr (far exceeding Windows 4 KB anonymous pipe buffer)
sys.stderr.write("WARN verbose subagent debug line\\n" * 6500)
sys.stderr.flush()
out_file = None
for idx, a in enumerate(args):
    if a in ("-o", "--output-last-message") and idx + 1 < len(args):
        out_file = pathlib.Path(args[idx + 1])
for ev in (
    {{"type": "thread.started", "thread_id": "{sid}"}},
    {{"type": "turn.started", "turn_id": "t1"}},
    {{"type": "item.completed", "item": {{"type": "agent_message", "text": "Heavy stderr ok"}}}},
    {{"type": "turn.completed"}},
):
    print(json.dumps(ev), flush=True)
if out_file:
    out_file.write_text("Heavy stderr ok", encoding="utf-8")
""",
    )
    args = parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--mode",
            "read-only",
            "--prompt",
            "Test heavy stderr",
            "--data-dir",
            str(tmp_path / "data"),
            "--codex-bin",
            str(fake_codex),
            "--no-desktop-project-sync",
            "--timeout",
            "15",
        ]
    )
    code, summary = run_delegation(args)
    assert code == 0
    assert summary["status"] == "completed"
    assert summary["final_summary"] == "Heavy stderr ok"


def test_delegate_cli_cleans_up_lingering_process_after_turn_completed(tmp_path: Path) -> None:
    """If Codex hangs on shutdown after emitting turn.completed, delegate_cli completes cleanly after grace period."""
    sid = "eeeeeeee-1111-2222-3333-444444444444"
    pid_file = tmp_path / "lingering.pid"
    fake_codex = _make_stub_codex(
        tmp_path,
        "lingering_codex",
        f"""
import json, os, pathlib, sys, time
args = sys.argv[1:]
_ = sys.stdin.read()
pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
out_file = None
for idx, a in enumerate(args):
    if a in ("-o", "--output-last-message") and idx + 1 < len(args):
        out_file = pathlib.Path(args[idx + 1])
for ev in (
    {{"type": "thread.started", "thread_id": "{sid}"}},
    {{"type": "turn.started", "turn_id": "t1"}},
    {{"type": "item.completed", "item": {{"type": "agent_message", "text": "Report written cleanly"}}}},
    {{"type": "turn.completed"}},
):
    print(json.dumps(ev), flush=True)
if out_file:
    out_file.write_text("Report written cleanly", encoding="utf-8")
# Simulate Tokio / multi-agent shutdown hang after completion
time.sleep(60)
""",
    )
    args = parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--mode",
            "read-only",
            "--prompt",
            "Test post-turn hang cleanup",
            "--data-dir",
            str(tmp_path / "data"),
            "--codex-bin",
            str(fake_codex),
            "--no-desktop-project-sync",
            "--post-turn-grace",
            "1.0",
            "--timeout",
            "20",
        ]
    )
    t0 = time.monotonic()
    code, summary = run_delegation(args)
    elapsed = time.monotonic() - t0
    assert code == 0
    assert summary["status"] == "completed"
    assert summary["final_summary"] == "Report written cleanly"
    assert summary["warning"] is not None
    assert elapsed < 10.0

    # Verify lingering codex process was actually terminated
    lingering_pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 3.0
    while is_process_alive(lingering_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not is_process_alive(lingering_pid)
