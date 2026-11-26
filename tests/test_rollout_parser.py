"""Unit tests for Codex rollout parser and incremental session store."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from server.rollout_parser import RolloutParseState
from server.session_store import SessionStore

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "rollout-2026-11-26T10-00-00-11111111-2222-3333-4444-555555555555.jsonl"
)


def test_parse_structured_rollout_fixture() -> None:
    state = RolloutParseState(path=FIXTURE_PATH)
    changed, was_reset, dirty = state.feed_file()

    assert changed is True
    assert was_reset is False
    assert dirty == ["turn-001"]

    detail = state.build_detail(now_ts=time.time())
    assert detail.summary.session_id == "11111111-2222-3333-4444-555555555555"
    assert detail.summary.project_name == "sample-app"
    assert detail.summary.git_branch == "feat/demo"
    assert detail.summary.model == "gpt-5.3-codex"
    assert detail.summary.status == "completed"
    assert detail.summary.round_count == 1
    assert detail.total_commands == 1
    assert detail.total_changed_files == 2

    turn = detail.turns[0]
    assert turn.prompt == "Implement unit tests for parser module"
    assert turn.reasoning_summaries == ["Inspecting parser structure before adding tests."]
    assert len(turn.assistant_messages) == 2
    assert turn.assistant_messages[0].phase == "commentary"
    assert turn.assistant_messages[1].phase == "final"
    assert turn.commands[0].command == "pytest -q"
    assert turn.commands[0].exit_code == 0
    assert turn.commands[0].duration_ms == 1250
    assert turn.duration_ms == 3250


def test_incremental_tail_and_incomplete_trailing_line(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions" / "2026" / "11" / "26"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / FIXTURE_PATH.name
    shutil.copyfile(FIXTURE_PATH, rollout)

    store = SessionStore(sessions_dir=tmp_path / "sessions")
    detail, _ = store.get_session_detail("11111111-2222-3333-4444-555555555555")
    assert detail is not None
    cursor_1 = detail.cursor

    # Append a half-written JSON line (no trailing newline) -> must not break or advance cursor.
    partial_event = {
        "timestamp": "2026-11-26T10:01:00.000Z",
        "ordinal": 12,
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "turn-002", "started_at": 1795600860},
    }
    raw_line = json.dumps(partial_event)
    split_at = len(raw_line) // 2
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(raw_line[:split_at])

    delta_partial, _ = store.poll_session_delta("11111111-2222-3333-4444-555555555555", cursor_1)
    assert delta_partial is not None
    assert delta_partial.changed is False
    assert delta_partial.cursor == cursor_1

    # Complete the line with newline -> delta emits turn-002 running.
    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(raw_line[split_at:] + "\n")

    delta_full, _ = store.poll_session_delta(
        "11111111-2222-3333-4444-555555555555",
        cursor_1,
        now_ts=time.time(),
    )
    assert delta_full is not None
    assert delta_full.changed is True
    assert delta_full.reset is False
    assert delta_full.cursor > cursor_1
    assert len(delta_full.updated_turns) == 1
    assert delta_full.updated_turns[0].turn_id == "turn-002"
    assert delta_full.updated_turns[0].status == "running"


def test_response_item_fallback_and_session_index_search(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    day_dir = codex_home / "sessions" / "2026" / "11" / "26"
    day_dir.mkdir(parents=True)

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    rollout = day_dir / f"rollout-2026-11-26T11-00-00-{sid}.jsonl"
    records = [
        {
            "timestamp": "2026-11-26T11:00:00Z",
            "type": "session_meta",
            "payload": {"session_id": sid, "cwd": "D:\\work\\alpha-service", "originator": "codex_cli_rs"},
        },
        {
            "timestamp": "2026-11-26T11:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "o3"},
        },
        {
            "timestamp": "2026-11-26T11:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Inspect build script"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "t1"},
            },
        },
        {
            "timestamp": "2026-11-26T11:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "exec",
                "input": "git status --short",
                "internal_chat_message_metadata_passthrough": {"turn_id": "t1"},
            },
        },
        {
            "timestamp": "2026-11-26T11:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": [{"type": "input_text", "text": "Exit code: 0\nWall time: 0.4 seconds\nOutput:\nclean"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "t1"},
            },
        },
        {
            "timestamp": "2026-11-26T11:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Workspace is clean."}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "t1"},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    index_file = codex_home / "session_index.jsonl"
    index_file.write_text(
        json.dumps({"id": sid, "thread_name": "Build Script Audit", "updated_at": "2026-11-26T11:00:04Z"}) + "\n",
        encoding="utf-8",
    )

    store = SessionStore(sessions_dir=codex_home / "sessions", session_index_path=index_file)
    found = store.list_sessions(query="audit")
    assert len(found) == 1
    assert found[0].title == "Build Script Audit"
    assert found[0].project_name == "alpha-service"

    detail, _ = store.get_session_detail(sid)
    assert detail is not None
    assert detail.turns[0].prompt == "Inspect build script"
    assert detail.turns[0].commands[0].command == "git status --short"
    assert detail.turns[0].commands[0].exit_code == 0
    assert detail.turns[0].commands[0].duration_ms == 400
    assert detail.turns[0].assistant_messages[0].text == "Workspace is clean."
