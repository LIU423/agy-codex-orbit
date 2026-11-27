"""Tests for Codex Profile-style overview/heatmap builder, locale persistence, and AGENTS filter."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from server.delegation_store import DelegationStore
from server.overview_builder import build_profile_overview
from server.routing_policy import DEFAULT_UI_LOCALE, load_ui_locale, save_ui_locale
from server.session_store import SessionStore


def test_ui_locale_persistence(tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin-data"
    assert load_ui_locale(data_dir) == DEFAULT_UI_LOCALE
    assert save_ui_locale("en", data_dir) == "en"
    assert load_ui_locale(data_dir) == "en"
    assert save_ui_locale("zh", data_dir) == "zh"
    assert load_ui_locale(data_dir) == "zh"
    assert save_ui_locale("ja", data_dir) == "ja"
    assert load_ui_locale(data_dir) == "ja"
    with pytest.raises(ValueError):
        save_ui_locale("fr", data_dir)


def test_profile_overview_splits_agy_vs_direct_and_computes_streaks(tmp_path: Path) -> None:
    now_local = datetime.now().astimezone()
    today_local = now_local.date()
    yesterday_local = today_local - timedelta(days=1)
    tz = now_local.tzinfo
    t1_ts = datetime(today_local.year, today_local.month, today_local.day, 10, 0, tzinfo=tz).isoformat()
    t2_ts = datetime(yesterday_local.year, yesterday_local.month, yesterday_local.day, 10, 5, tzinfo=tz).isoformat()

    day_dir = tmp_path / "sessions" / f"{today_local.year:04d}" / f"{today_local.month:02d}" / f"{today_local.day:02d}"
    day_dir.mkdir(parents=True)

    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"

    # Session 1: AGY-delegated session today (also tests AGENTS.md boilerplate filtering)
    r1 = day_dir / f"rollout-{today_local.isoformat()}T10-00-00-{s1}.jsonl"
    r1.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "timestamp": t1_ts,
                    "type": "session_meta",
                    "payload": {"session_id": s1, "cwd": "D:\\work\\orbit-demo"},
                },
                {
                    "timestamp": t1_ts,
                    "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-6.2-sol", "effort": "high"},
                },
                {
                    "timestamp": t1_ts,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md instructions for D:\\work\\orbit-demo"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "t1"},
                    },
                },
                {
                    "timestamp": t1_ts,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 120000}},
                    },
                },
                {
                    "timestamp": t1_ts,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "t1", "duration_ms": 5000},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Session 2: Direct Codex session yesterday (creates a 2-day streak!)
    r2 = day_dir / f"rollout-{yesterday_local.isoformat()}T10-05-00-{s2}.jsonl"
    r2.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "timestamp": t2_ts,
                    "type": "session_meta",
                    "payload": {"session_id": s2, "cwd": "D:\\work\\orbit-demo"},
                },
                {
                    "timestamp": t2_ts,
                    "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-6.2-astra", "effort": "medium"},
                },
                {
                    "timestamp": t2_ts,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 80000}},
                    },
                },
                {
                    "timestamp": t2_ts,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "t1", "duration_ms": 9000},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    data_dir = tmp_path / "plugin-data"
    dstore = DelegationStore(data_dir=data_dir)
    dstore.start_round(
        run_id="run-agy-1",
        workspace_dir="D:\\work\\orbit-demo",
        prompt="Build backend endpoint",
        isolation_mode="workspace-write",
        title="agy: Build backend endpoint",
        session_id=s1,
        pid=os.getpid(),
    )
    dstore.update_round("run-agy-1", status="completed", ended=True)

    store = SessionStore(sessions_dir=tmp_path / "sessions", delegation_store=dstore)

    detail1, _ = store.get_session_detail(s1)
    assert detail1 is not None
    assert detail1.turns[0].prompt == "Build backend endpoint"  # AGENTS.md filtered, delegation prompt kept!

    ov = build_profile_overview(store, data_dir=data_dir, today_override=today_local)
    k = ov["kpis"]
    assert k["total_sessions"] == 2
    assert k["agy_sessions"] == 1
    assert k["direct_sessions"] == 1
    assert k["agy_share_pct"] == 50.0
    assert k["total_tokens"] == 200000
    assert k["agy_tokens"] == 120000
    assert k["direct_tokens"] == 80000
    assert k["longest_duration_ms"] == 9000
    assert k["longest_streak_days"] == 2
    assert k["current_streak_days"] == 2

    origins = {r["session_id"]: r["origin"] for r in ov["recent_sessions"]}
    assert origins[s1] == "agy"
    assert origins[s2] == "direct"
