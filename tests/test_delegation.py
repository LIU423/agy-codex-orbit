"""Unit tests for M2 Codex locator, delegation metadata store, and delegate CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server import delegate_cli
from server.codex_desktop import write_cached_codex_quota
from server.codex_locator import CodexBinaryNotFoundError, locate_codex_binary
from server.delegate_cli import parse_args, run_delegation
from server.delegation_store import DelegationStore
from server.routing_policy import save_settings
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
sys.stdin.reconfigure(encoding="utf-8")
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


@pytest.mark.parametrize(
    "enabled, flags, expected_model, expected_probe",
    [
        (True, [], "custom-default", True),
        (True, ["--model", "explicit-model"], "explicit-model", False),
        (True, ["--dry-run"], "custom-escalation", False),
        (False, [], "custom-escalation", False),
    ],
)
def test_live_max_guardrail_only_checks_automatic_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    flags: list[str],
    expected_model: str,
    expected_probe: bool,
) -> None:
    data_dir = tmp_path / "plugin-data"
    save_settings(
        data_dir, routing_mode="silent_best", default_model="custom-default",
        escalation_model="custom-escalation", quota_guardrail_enabled=enabled,
    )
    sid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    fake_codex = _create_fake_codex_cmd(tmp_path, sid)
    probes: list[Path] = []

    def query(*, codex_bin: Path, data_dir: Path, use_cache: bool, max_age_seconds: float) -> dict:
        assert data_dir == tmp_path / "plugin-data"
        assert use_cache is True
        assert max_age_seconds == 60.0
        probes.append(codex_bin)
        return {"codex": {"available": True, "primary": {"remaining_percent": 0}}}

    monkeypatch.setattr(delegate_cli, "query_codex_quota", query)
    code, result = run_delegation(
        parse_args([
            "--cwd", str(tmp_path), "--mode", "read-only", "--task-tier", "complex",
            "--prompt", "Verify selected model", "--codex-bin", str(fake_codex),
            "--data-dir", str(data_dir), "--no-desktop-project-sync", *flags,
        ])
    )
    assert code == 0
    assert result["model"] == expected_model
    assert result["reasoning_effort"] == "xhigh"
    assert probes == ([fake_codex.resolve()] if expected_probe else [])

    if "--dry-run" in flags:
        assert result["command"][result["command"].index("-m") + 1] == expected_model
    else:
        assert result["guardrail_triggered"] is expected_probe
        record = DelegationStore(data_dir).get_by_session_id(sid)
        assert record is not None
        assert record.rounds[0].model == expected_model


def test_confirm_if_needed_returns_one_line_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "plugin-data"
    save_settings(data_dir, routing_mode="ask", default_model="default-model", escalation_model="best-model")
    quota = {"codex": {"available": True, "primary": {"remaining_percent": 5}}}
    write_cached_codex_quota(quota, data_dir=data_dir)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Verify the routing decision", encoding="utf-8")

    def unexpected_launch(*args, **kwargs):
        pytest.fail("Confirmation must return before preparing a worktree or executing Codex")

    monkeypatch.setattr(delegate_cli, "_prepare_worktree", unexpected_launch)
    monkeypatch.setattr(delegate_cli.subprocess, "Popen", unexpected_launch)
    with pytest.raises(SystemExit) as exit_info:
        delegate_cli.main([
            "--confirm-if-needed", "--task-tier", "complex", "--mode", "worktree",
            "--prompt-file", str(prompt_file), "--delete-prompt-file", "--cwd", str(tmp_path),
            "--data-dir", str(data_dir), "--codex-bin", str(tmp_path / "missing-codex"),
        ])

    assert exit_info.value.code == 0
    stdout = capsys.readouterr().out
    result = json.loads(stdout)
    assert stdout == json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
    assert result["status"] == "needs_confirmation"
    assert result["requires_confirmation"] is True
    assert result["routing_mode"] == "ask"
    assert result["task_tier"] == "complex"
    assert result["model"] == result["fallback_model"] == "default-model"
    assert result["recommended_model"] == "best-model"
    assert result["guardrail_triggered"] is True
    assert result["quota"] == quota
    assert prompt_file.read_text(encoding="utf-8") == "Verify the routing decision"
    assert not (data_dir / "worktrees").exists()
    assert not (data_dir / "delegations").exists()


@pytest.mark.parametrize(
    "routing_mode, task_tier, flags, expected_status, expected_model",
    [
        ("ask", "standard", ["--confirm-if-needed"], "completed", "default-model"),
        ("silent_low_cost", "complex", ["--confirm-if-needed"], "completed", "default-model"),
        ("silent_best", "complex", ["--confirm-if-needed"], "completed", "best-model"),
        ("ask", "complex", ["--confirm-if-needed", "--model", "explicit-model"], "completed", "explicit-model"),
        ("ask", "complex", ["--confirm-if-needed", "--dry-run"], "dry_run", "default-model"),
        ("ask", "complex", [], "completed", "default-model"),
    ],
)
def test_confirm_if_needed_dispatches_when_confirmation_is_not_required(
    tmp_path: Path, routing_mode: str, task_tier: str, flags: list[str], expected_status: str, expected_model: str,
) -> None:
    data_dir = tmp_path / "plugin-data"
    save_settings(data_dir, routing_mode=routing_mode, default_model="default-model", escalation_model="best-model")
    write_cached_codex_quota(
        {"codex": {"available": True, "primary": {"remaining_percent": 80}}}, data_dir=data_dir,
    )
    sid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    fake_codex = _create_fake_codex_cmd(tmp_path, sid)

    code, result = run_delegation(parse_args([
        "--task-tier", task_tier, "--mode", "read-only", "--cwd", str(tmp_path), "--prompt", "Verify the dispatch",
        "--data-dir", str(data_dir), "--codex-bin", str(fake_codex), "--no-desktop-project-sync", *flags,
    ]))

    assert code == 0
    assert result["status"] == expected_status
    assert result["model"] == expected_model
    record = DelegationStore(data_dir).get_by_session_id(sid)
    if expected_status == "completed":
        assert record is not None
        assert record.rounds[0].model == expected_model
    else:
        assert record is None


def test_plan_route_reuses_cached_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_ORBIT_DATA_DIR", str(tmp_path))
    save_settings(routing_mode="silent_best", default_model="default-model", escalation_model="best-model")
    quota = {"codex": {"available": True, "primary": {"remaining_percent": 5}}}
    write_cached_codex_quota(quota, data_dir=tmp_path)

    def unexpected_launch(*args, **kwargs):
        pytest.fail("Route planning must reuse the fresh quota cache")

    monkeypatch.setattr(delegate_cli.subprocess, "Popen", unexpected_launch)
    code, result = run_delegation(parse_args([
        "--plan-route", "--confirm-if-needed", "--task-tier", "standard",
        "--codex-bin", str(tmp_path / "missing-codex"),
    ]))

    assert code == 0
    assert "status" not in result
    assert result["quota"] == quota
    assert result["guardrail_triggered"] is True
    assert result["model"] == "default-model"


def test_delegate_cli_worktree_isolation_and_multi_turn_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
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
            "--title",
            "Parser unit tests",
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
    assert summary1["title"] == "agy: Parser unit tests"
    assert summary1["model"] == "gpt-6.2-sol"
    assert summary1["reasoning_effort"] == "high"
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
    assert rec.title == "agy: Parser unit tests"
    assert len(rec.rounds) == 2
    assert [r.round_index for r in rec.rounds] == [1, 2]
    assert rec.rounds[0].source_model == "Gemini 2.5 Pro"


@pytest.mark.parametrize("codex_available", [True, False])
def test_delegate_cli_reuses_registered_followup_and_deletes_prompt_file(
    tmp_path: Path, codex_available: bool,
) -> None:
    session_id = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    data_dir = tmp_path / "plugin-data"
    store = DelegationStore(data_dir)
    prompt = "Continue with café\nand summarize."
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    for run_id in ("run-original", "run-followup"):
        store.start_round(
            run_id=run_id,
            session_id=session_id,
            workspace_dir=str(tmp_path),
            prompt=prompt,
            isolation_mode="workspace-write",
            source_model="Orbit",
        )
    store.update_round("run-original", status="completed", ended=True)

    fake_codex = _create_fake_codex_cmd(tmp_path, session_id) if codex_available else tmp_path / "missing.exe"
    code, summary = run_delegation(parse_args([
        "--session-id", session_id, "--run-id", "run-followup", "--cwd", str(tmp_path),
        "--prompt-file", str(prompt_path), "--delete-prompt-file", "--data-dir", str(data_dir),
        "--codex-bin", str(fake_codex), "--source-model", "Orbit", "--no-desktop-project-sync",
    ]))

    assert not prompt_path.exists()
    record = store.get_by_session_id(session_id)
    assert record is not None
    assert [rnd.run_id for rnd in record.rounds] == ["run-original", "run-followup"]
    followup = record.rounds[-1]
    assert followup.round_index == 2
    assert followup.pid == os.getpid()
    assert followup.ended_at
    if codex_available:
        assert code == 0
        assert summary["run_id"] == "run-followup"
        assert followup.status == "completed"
        assert followup.turn_id == "turn-002"
        assert followup.model == summary["model"]
        assert (tmp_path / "round2.txt").read_text(encoding="utf-8") == prompt
    else:
        assert code == 2
        assert followup.status == "failed"
        assert followup.error_summary == summary["error"]


def test_delegate_cli_binds_reserved_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    data_dir = tmp_path / "plugin-data"
    store = DelegationStore(data_dir)
    fake_codex = _create_fake_codex_cmd(tmp_path, session_id)
    prompt_file = tmp_path / "new.prompt"
    prompt_file.write_text("Start a conversation", encoding="utf-8")
    store.start_round(
        run_id="run-new", workspace_dir=str(tmp_path), prompt="Start a conversation",
        isolation_mode="read-only", source_model="Orbit", title="New conversation",
    )
    title_sync = []
    bindings = []
    monkeypatch.setattr(
        delegate_cli, "set_codex_thread_name",
        lambda sid, title, **kwargs: title_sync.append((sid, title)) or {},
    )
    monkeypatch.setattr(
        delegate_cli, "ensure_codex_project_and_bind",
        lambda path, sid, **kwargs: bindings.append((path, sid, kwargs["promote_source"])) or {},
    )

    code, summary = run_delegation(parse_args([
        "--run-id", "run-new", "--cwd", str(tmp_path), "--mode", "read-only", "--data-dir", str(data_dir),
        "--codex-bin", str(fake_codex), "--title", "New conversation", "--source-model", "Orbit",
        "--prompt-file", str(prompt_file), "--delete-prompt-file",
    ]))

    assert code == 0
    assert summary["session_id"] == session_id
    assert not prompt_file.exists()
    [record] = store.load_all()
    assert record.record_id == record.session_id == session_id
    [rnd] = record.rounds
    assert (rnd.run_id, rnd.status, rnd.turn_id) == ("run-new", "completed", "turn-001")
    assert (session_id, "agy: New conversation") in title_sync
    assert (tmp_path.resolve(), session_id, True) in bindings


@pytest.mark.parametrize("source", ["event", "stderr", "unrelated"])
def test_delegate_cli_classifies_thread_writer_conflict(tmp_path: Path, source: str) -> None:
    session_id = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    data_dir = tmp_path / "plugin-data"
    store = DelegationStore(data_dir)
    fake_codex = _create_fake_codex_cmd(tmp_path, session_id)
    fake_output = {
        "event": 'print(json.dumps({"type": "error", "message": "x" * 700 + " Thread already has an active writer"}))',
        "stderr": 'print("thread/resume: Thread-store conflict\\n" + "x" * 4100, file=sys.stderr)\nsys.exit(1)',
        "unrelated": 'print("thread/resume: Thread not found", file=sys.stderr)\nsys.exit(1)',
    }
    (tmp_path / "fake_codex.py").write_text(
        "import json, sys\nassert 'resume' in sys.argv\nsys.stdin.read()\n" + fake_output[source],
        encoding="utf-8",
    )
    store.start_round(
        run_id="run-conflict", session_id=session_id, workspace_dir=str(tmp_path),
        prompt="Continue", isolation_mode="read-only", source_model="Orbit",
    )

    code, summary = run_delegation(parse_args([
        "--session-id", session_id, "--run-id", "run-conflict", "--cwd", str(tmp_path), "--mode", "read-only",
        "--data-dir", str(data_dir), "--codex-bin", str(fake_codex), "--prompt", "Continue",
        "--no-desktop-project-sync",
    ]))

    assert code == 1
    [rnd] = store.get_by_session_id(session_id).rounds
    assert rnd.status == "failed"
    assert rnd.error_summary == summary["error"]
    if source == "unrelated":
        assert rnd.error_code is None
        assert rnd.error_summary == "thread/resume: Thread not found"
    else:
        assert rnd.error_code == summary["error_code"] == "thread_writer_busy"
        assert "close/unload the thread in Codex Desktop, then retry" in rnd.error_summary


@pytest.mark.parametrize("saved_effort,explicit_effort,expected_effort", [
    ("auto", None, "high"),
    ("max", None, "max"),
    ("max", "medium", "medium"),
])
def test_delegate_cli_same_workspace_default_and_effort_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved_effort: str,
    explicit_effort: str | None,
    expected_effort: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    repo_dir = tmp_path / "shared-repo"
    _init_temp_git_repo(repo_dir)
    sid = "77777777-aaaa-bbbb-cccc-dddddddddddd"
    fake_codex = _create_fake_codex_cmd(tmp_path, sid)
    data_dir = tmp_path / "data"
    save_settings(data_dir, reasoning_effort=saved_effort)

    args = [
        "--cwd", str(repo_dir),
        "--prompt", "Goal: Build shared backend endpoint",
        "--data-dir", str(data_dir),
        "--codex-bin", str(fake_codex),
    ]
    if explicit_effort is not None:
        args.extend(["--reasoning-effort", explicit_effort])
    code, summary = run_delegation(parse_args(args))
    assert code == 0
    assert summary["isolation_mode"] == "workspace-write"
    assert summary["worktree_path"] is None
    assert summary["model"] == "gpt-6.2-sol"
    assert summary["reasoning_effort"] == expected_effort
    [round_rec] = DelegationStore(data_dir).get_by_session_id(sid).rounds
    assert round_rec.reasoning_effort == expected_effort
    assert summary["title"] == "agy: Build shared backend endpoint"
    assert summary["web_search"] is False
    assert (repo_dir / "round1.txt").is_file()


def test_delegate_cli_web_search_and_non_git_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    non_git_dir = tmp_path / "Career"
    non_git_dir.mkdir(parents=True)
    sid = "88888888-aaaa-bbbb-cccc-dddddddddddd"
    fake_codex = _create_fake_codex_cmd(tmp_path, sid)

    # Dry-run checks exact command construction for Round 1 with --web-search
    dry_args = parse_args(
        [
            "--cwd",
            str(non_git_dir),
            "--mode",
            "workspace-write",
            "--web-search",
            "--title",
            "Research Acme Corp",
            "--prompt",
            "Research Acme Corp and save report to research/acme.md",
            "--data-dir",
            str(tmp_path / "data"),
            "--codex-bin",
            str(fake_codex),
            "--dry-run",
        ]
    )
    dry_code, dry_plan = run_delegation(dry_args)
    assert dry_code == 0
    assert dry_plan["web_search"] is True
    assert dry_plan["command"][1:3] == ["--search", "exec"]

    # Dry-run checks Round 2 resume with --web-search
    resume_dry = parse_args(
        [
            "--cwd",
            str(non_git_dir),
            "--session-id",
            sid,
            "--web-search",
            "--prompt",
            "Dig deeper into recent financials",
            "--data-dir",
            str(tmp_path / "data"),
            "--codex-bin",
            str(fake_codex),
            "--dry-run",
        ]
    )
    res_code, res_plan = run_delegation(resume_dry)
    assert res_code == 0
    assert res_plan["web_search"] is True
    assert res_plan["command"][1:4] == ["--search", "exec", "resume"]

    # Full run in non-Git workspace succeeds cleanly with --web-search
    live_args = parse_args(
        [
            "--cwd",
            str(non_git_dir),
            "--mode",
            "read-only",
            "--web-search",
            "--title",
            "Research Acme Corp",
            "--prompt",
            "Research Acme Corp",
            "--data-dir",
            str(tmp_path / "data"),
            "--codex-bin",
            str(fake_codex),
        ]
    )
    code, summary = run_delegation(live_args)
    assert code == 0
    assert summary["status"] == "completed"
    assert summary["web_search"] is True
    assert summary["title"] == "agy: Research Acme Corp"


@pytest.mark.parametrize("rollout_has_metadata", [False, True])
def test_session_store_merges_pending_and_bound_delegations(tmp_path: Path, rollout_has_metadata: bool) -> None:
    data_dir = tmp_path / "plugin-data"
    sessions_dir = tmp_path / "sessions" / "2026" / "11" / "26"
    sessions_dir.mkdir(parents=True)
    records = [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()]
    if not rollout_has_metadata:
        for record in records:
            if record["type"] == "turn_context":
                record["payload"].pop("model", None)
                record["payload"].pop("effort", None)
    (sessions_dir / FIXTURE_PATH.name).write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )

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
        model="delegation-model",
        reasoning_effort="medium",
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
        reasoning_effort="ultra",
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
    assert by_id["run-pending-1"].reasoning_effort == "ultra"
    assert [s.session_id for s in sstore.list_sessions(query=" ULTRA ")] == ["run-pending-1"]
    pending, _ = sstore.get_session_detail("run-pending-1")
    assert pending is not None
    assert pending.summary.reasoning_effort == pending.turns[0].effort == "ultra"

    # Bound rollout session is enriched with delegation metadata
    assert bound_sid in by_id
    assert by_id[bound_sid].is_delegated is True
    assert by_id[bound_sid].source_model == "Gemini"
    assert by_id[bound_sid].isolation_mode == "worktree"
    expected_model = "gpt-5.3-codex" if rollout_has_metadata else "delegation-model"
    expected_effort = "high" if rollout_has_metadata else "medium"
    assert (by_id[bound_sid].model, by_id[bound_sid].reasoning_effort) == (expected_model, expected_effort)

    detail, meta_ts = sstore.get_session_detail(bound_sid)
    assert detail is not None
    assert meta_ts != ""
    assert detail.turns[0].is_delegated is True
    assert detail.turns[0].delegation_round == 1
    assert detail.turns[0].source_model == "Gemini"
    assert (detail.turns[0].model, detail.turns[0].effort) == (expected_model, expected_effort)

    # Starting Round 2 triggers delta via meta_ts even before rollout grows
    dstore.start_round(
        run_id="run-bound-2",
        workspace_dir="D:\\demo-workspace\\sample-app",
        prompt="Round 2 follow-up prompt",
        isolation_mode="worktree",
        source_model="Gemini",
        session_id=bound_sid,
        model="followup-model",
        reasoning_effort="xhigh",
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
    assert delta.summary.model == delta.updated_turns[1].model == "followup-model"
    assert delta.summary.reasoning_effort == delta.updated_turns[1].effort == "xhigh"
    assert [s.session_id for s in sstore.list_sessions(query="xhigh")] == [bound_sid]


@pytest.mark.parametrize("matched", [False, True])
def test_session_store_counts_failed_orbit_round_in_desktop_timeline(tmp_path, monkeypatch, matched) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    rollout = sessions_dir / FIXTURE_PATH.name
    shutil.copyfile(FIXTURE_PATH, rollout)
    session_id = "11111111-2222-3333-4444-555555555555"
    prompt = "Continue working"

    def append_desktop_turn(turn_id: str, timestamp: str) -> None:
        events = (
            ("turn_context", {"turn_id": turn_id}),
            ("event_msg", {"type": "task_started", "turn_id": turn_id}),
            ("event_msg", {
                "type": "item_completed", "turn_id": turn_id,
                "item": {"type": "UserMessage", "content": [{"type": "text", "text": prompt}]},
            }),
            ("event_msg", {"type": "task_complete", "turn_id": turn_id}),
        )
        with rollout.open("a", encoding="utf-8") as fh:
            for event_type, payload in events:
                fh.write(json.dumps({"timestamp": timestamp, "type": event_type, "payload": payload}) + "\n")

    append_desktop_turn("turn-002", "2026-11-26T10:10:00.200Z")
    monkeypatch.setattr("server.delegation_store.utc_now_iso", lambda: "2026-11-26T10:10:00.300+00:00")
    delegations = DelegationStore(tmp_path / "data")
    delegations.start_round(
        run_id="run-conflict", session_id=session_id, workspace_dir=str(tmp_path),
        prompt=prompt, isolation_mode="workspace-write", source_model="Orbit",
    )
    delegations.update_round(
        "run-conflict", status="failed", turn_id="turn-002" if matched else None,
        error_code="thread_writer_busy", error_summary="Close the thread in Codex Desktop and retry.", ended=True,
    )
    sessions = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)

    [summary] = sessions.list_sessions()
    detail, meta_ts = sessions.get_session_detail(session_id)
    delta, _ = sessions.poll_session_delta(session_id)
    assert summary.round_count == detail.summary.round_count == delta.summary.round_count == (2 if matched else 3)
    assert [turn.round_index for turn in detail.turns] == list(range(1, summary.round_count + 1))
    assert delta.updated_turns == detail.turns
    failed = detail.turns[-1]
    assert failed.status == "aborted"
    assert failed.error_code == "thread_writer_busy"
    assert failed.error_reason == "Close the thread in Codex Desktop and retry."
    assert failed.delegation_round == 1
    if not matched:
        assert [turn.turn_id for turn in detail.turns] == ["turn-001", "turn-002", "delegation:run-conflict"]
        assert all(not turn.is_delegated for turn in detail.turns[:-1])

        newer_file = sessions_dir / "rollout-2026-11-26T10-05-00-direct.jsonl"
        newer_file.write_text(json.dumps({
            "type": "session_meta", "timestamp": "2026-11-26T10:05:00Z",
            "payload": {"id": "direct", "cwd": str(tmp_path)},
        }) + "\n", encoding="utf-8")
        newer_mtime = rollout.stat().st_mtime_ns + 1_000_000_000
        os.utime(newer_file, ns=(newer_mtime, newer_mtime))
        fresh_store = SessionStore(sessions_dir=sessions_dir, delegation_store=delegations)
        [limited] = fresh_store.list_sessions(limit=1)
        assert limited.session_id == session_id
        assert limited.round_count == 3

        append_desktop_turn("turn-003", "2026-11-26T10:20:00.200Z")
        changed, _ = sessions.poll_session_delta(session_id, after_cursor=detail.cursor, after_meta_ts=meta_ts)
        assert changed.reset
        assert changed.summary.round_count == 4
        assert [turn.round_index for turn in changed.updated_turns] == [1, 2, 3, 4]
        assert changed.updated_turns[-1].error_code == "thread_writer_busy"
