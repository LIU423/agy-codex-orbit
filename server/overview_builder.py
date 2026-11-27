"""Builds Codex Profile-style overview KPIs, 52-week activity heatmap, and AGY vs Direct insights."""

from __future__ import annotations

import os
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from server.delegation_store import DelegationStore
from server.routing_policy import RoutingMode, load_routing_mode
from server.session_store import SessionStore


def _iso_to_local_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().date().isoformat()
    except ValueError:
        return None


def _epoch_to_local_date(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    try:
        val = float(ts)
        if val > 1e12:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc).astimezone().date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _short_file_label(file_path: str) -> str:
    norm = file_path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if len(parts) <= 2:
        return norm
    return "/".join(parts[-2:])


def _compute_streaks(active_dates: set[str], today: date) -> tuple[int, int]:
    """Return `(longest_streak_days, current_streak_days)`."""
    if not active_dates:
        return 0, 0
    parsed = sorted(date.fromisoformat(d) for d in active_dates)
    longest = 1
    run = 1
    for i in range(1, len(parsed)):
        if (parsed[i] - parsed[i - 1]).days == 1:
            run += 1
            if run > longest:
                longest = run
        else:
            run = 1

    # Current streak: allow today or yesterday as most recent active day.
    current = 0
    cursor = today
    if cursor.isoformat() not in active_dates:
        cursor = today - timedelta(days=1)
    while cursor.isoformat() in active_dates:
        current += 1
        cursor -= timedelta(days=1)

    return longest, current


def _read_sqlite_historical_threads(
    codex_home: Path,
    known_ids: set[str],
    project_filter: str,
) -> list[dict[str, Any]]:
    """Read-only supplement of historical top-level Codex threads from `state_5.sqlite`."""
    db_path = codex_home / "state_5.sqlite"
    if not db_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        cols = {r[1] for r in cur.execute('PRAGMA table_info("threads")')}
        if not {"id", "created_at", "cwd"}.issubset(cols):
            conn.close()
            return []

        sel_source = "source" if "source" in cols else "NULL"
        sel_tokens = "tokens_used" if "tokens_used" in cols else "0"
        sel_model = "model" if "model" in cols else "NULL"
        sel_effort = "reasoning_effort" if "reasoning_effort" in cols else "NULL"
        sel_name = "name" if "name" in cols else "NULL"
        sel_updated = "updated_at" if "updated_at" in cols else "created_at"

        rows = cur.execute(
            f"SELECT id, cwd, {sel_source}, {sel_tokens}, {sel_model}, {sel_effort}, {sel_name}, created_at, {sel_updated} FROM threads"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    pf = project_filter.strip().lower()
    for tid, cwd, source, tokens_used, model, effort, name, created_at, updated_at in rows:
        if not isinstance(tid, str) or tid in known_ids:
            continue
        # Skip internal subagent child threads so we don't inflate top-level session counts.
        if isinstance(source, str) and "subagent" in source.lower():
            continue
        cwd_str = (cwd or "").strip().rstrip("\\/")
        if pf and cwd_str.lower() != pf:
            continue
        title = (name or "").strip()
        is_agy = title.lower().startswith("agy:")
        day_str = _epoch_to_local_date(updated_at or created_at)
        out.append(
            {
                "session_id": tid,
                "project_dir": cwd_str or "(Unknown Workspace)",
                "is_delegated": is_agy,
                "total_tokens": max(0, int(tokens_used or 0)),
                "model": model if isinstance(model, str) and model else None,
                "reasoning_effort": effort if isinstance(effort, str) and effort else None,
                "date": day_str,
                "rounds": 1,
                "duration_ms": 0,
            }
        )
    return out


def build_profile_overview(
    store: SessionStore,
    *,
    data_dir: Path | str | None = None,
    project_dir: str = "",
    today_override: date | None = None,
) -> dict[str, Any]:
    """Assemble Profile-style KPIs, 52-week AGY-vs-Direct heatmap, insights, and recent sessions."""
    all_summaries = store.list_sessions(limit=500)
    today = today_override or datetime.now().date()
    proj_filter = project_dir.strip().lower()

    # Project dropdown options (always unfiltered)
    project_counts: dict[str, dict[str, Any]] = {}
    for s in all_summaries:
        entry = project_counts.get(s.project_dir)
        if entry is None:
            entry = {
                "project_dir": s.project_dir,
                "project_name": s.project_name,
                "session_count": 0,
                "running_count": 0,
                "agy_count": 0,
                "direct_count": 0,
            }
            project_counts[s.project_dir] = entry
        entry["session_count"] += 1
        if s.status == "running":
            entry["running_count"] += 1
        if s.is_delegated:
            entry["agy_count"] += 1
        else:
            entry["direct_count"] += 1

    filtered = (
        [s for s in all_summaries if s.project_dir.lower() == proj_filter]
        if proj_filter
        else list(all_summaries)
    )

    known_ids = {s.session_id for s in all_summaries}
    historical = _read_sqlite_historical_threads(
        store.sessions_dir.parent,
        known_ids,
        project_dir,
    )

    daily: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "tokens": 0,
            "agy_tokens": 0,
            "direct_tokens": 0,
            "sessions": 0,
            "agy_sessions": 0,
            "direct_sessions": 0,
            "rounds": 0,
            "agy_rounds": 0,
            "direct_rounds": 0,
        }
    )

    model_counts: Counter[str] = Counter()
    model_tokens: Counter[str] = Counter()
    effort_counts: Counter[str] = Counter()
    file_sessions: dict[str, set[str]] = defaultdict(set)
    file_display: dict[str, str] = {}

    total_tokens = 0
    agy_tokens_total = 0
    direct_tokens_total = 0
    agy_sessions = 0
    direct_sessions = 0
    longest_duration_ms = 0
    total_commands = 0
    active_sessions = 0

    recent_items: list[dict[str, Any]] = []

    for s in filtered:
        day = _iso_to_local_date(s.updated_at or s.started_at)
        tokens = max(0, int(s.total_tokens or 0))
        rounds = max(0, int(s.round_count or 0))
        total_tokens += tokens
        total_commands += max(0, int(s.command_count or 0))
        if s.status == "running":
            active_sessions += 1
        if s.duration_ms and s.duration_ms > longest_duration_ms:
            longest_duration_ms = s.duration_ms

        if s.is_delegated:
            agy_sessions += 1
            agy_tokens_total += tokens
        else:
            direct_sessions += 1
            direct_tokens_total += tokens

        if s.model:
            model_counts[s.model] += 1
            model_tokens[s.model] += tokens
        if s.reasoning_effort:
            effort_counts[s.reasoning_effort] += 1

        if day:
            b = daily[day]
            b["tokens"] += tokens
            b["sessions"] += 1
            b["rounds"] += rounds
            if s.is_delegated:
                b["agy_tokens"] += tokens
                b["agy_sessions"] += 1
                b["agy_rounds"] += rounds
            else:
                b["direct_tokens"] += tokens
                b["direct_sessions"] += 1
                b["direct_rounds"] += rounds

        # Hot files from cached rollout state (O(1))
        rollout_path = store._path_by_session_id.get(s.session_id)
        state = store._states_by_path.get(rollout_path) if rollout_path else None
        if state is not None:
            for tid in state.turn_order:
                for fp in state.turns_by_id[tid].changed_files_by_path.keys():
                    fkey = fp.lower() if os.name == "nt" else fp
                    file_sessions[fkey].add(s.session_id)
                    file_display.setdefault(fkey, fp)

        if len(recent_items) < 60:
            item = s.model_dump(mode="json")
            item["date"] = day
            item["origin"] = "agy" if s.is_delegated else "direct"
            recent_items.append(item)

    # Fold read-only historical SQLite threads into heatmap & insights
    for h in historical:
        tokens = h["total_tokens"]
        total_tokens += tokens
        if h["is_delegated"]:
            agy_sessions += 1
            agy_tokens_total += tokens
        else:
            direct_sessions += 1
            direct_tokens_total += tokens
        if h["model"]:
            model_counts[h["model"]] += 1
            model_tokens[h["model"]] += tokens
        if h["reasoning_effort"]:
            effort_counts[h["reasoning_effort"]] += 1
        day = h["date"]
        if day:
            b = daily[day]
            b["tokens"] += tokens
            b["sessions"] += 1
            b["rounds"] += 1
            if h["is_delegated"]:
                b["agy_tokens"] += tokens
                b["agy_sessions"] += 1
                b["agy_rounds"] += 1
            else:
                b["direct_tokens"] += tokens
                b["direct_sessions"] += 1
                b["direct_rounds"] += 1

    # Heatmap window: past 365 days
    cutoff = (today - timedelta(days=364)).isoformat()
    heatmap_days = [
        {"date": d, **vals}
        for d, vals in sorted(daily.items())
        if d >= cutoff
    ]

    peak_day_tokens = max((d["tokens"] for d in heatmap_days), default=0)
    peak_day_sessions = max((d["sessions"] for d in heatmap_days), default=0)
    longest_streak, current_streak = _compute_streaks(set(daily.keys()), today)

    total_sessions_all = agy_sessions + direct_sessions
    agy_share_pct = round((agy_sessions / total_sessions_all) * 100, 1) if total_sessions_all > 0 else 0.0

    def _top_stat(counter: Counter[str]) -> dict[str, Any] | None:
        if not counter:
            return None
        total_c = sum(counter.values())
        name, cnt = counter.most_common(1)[0]
        return {"name": name, "count": cnt, "share_pct": round((cnt / total_c) * 100)}

    ranked_files = sorted(file_sessions.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:12]
    hot_files = [
        {
            "path": file_display[fk],
            "short_name": _short_file_label(file_display[fk]),
            "session_count": len(sids),
        }
        for fk, sids in ranked_files
    ]

    ranked_models = sorted(
        model_counts, key=lambda model: (-model_counts[model], -model_tokens[model], model)
    )[:6]
    known_model_sessions = sum(model_counts.values())
    model_breakdown = [
        {
            "name": model,
            "count": model_counts[model],
            "share_pct": round(100 * model_counts[model] / known_model_sessions, 1),
            "tokens": model_tokens[model],
        }
        for model in ranked_models
    ]

    worktrees_dir = DelegationStore(data_dir).worktrees_dir
    try:
        worktree_count = sum(path.is_dir() for path in worktrees_dir.iterdir())
    except OSError:
        worktree_count = 0

    routing_mode: RoutingMode = load_routing_mode(data_dir=data_dir)

    return {
        "project_filter": project_dir,
        "today": today.isoformat(),
        "projects": sorted(
            project_counts.values(),
            key=lambda p: (-p["running_count"], -p["session_count"], p["project_name"].lower()),
        ),
        "kpis": {
            "total_tokens": total_tokens,
            "agy_tokens": agy_tokens_total,
            "direct_tokens": direct_tokens_total,
            "peak_day_tokens": peak_day_tokens,
            "peak_day_sessions": peak_day_sessions,
            "longest_duration_ms": longest_duration_ms,
            "longest_streak_days": longest_streak,
            "current_streak_days": current_streak,
            "total_sessions": total_sessions_all,
            "agy_sessions": agy_sessions,
            "direct_sessions": direct_sessions,
            "agy_share_pct": agy_share_pct,
            "active_sessions": active_sessions,
            "total_projects": len(project_counts),
            "total_commands": total_commands,
        },
        "heatmap_days": heatmap_days,
        "insights": {
            "top_model": _top_stat(model_counts),
            "model_breakdown": model_breakdown,
            "top_effort": _top_stat(effort_counts),
            "routing_mode": routing_mode,
            "active_projects": len(project_counts),
            "total_commands": total_commands,
            "unique_changed_files": len(file_sessions),
            "worktree_count": worktree_count,
        },
        "recent_sessions": recent_items,
        "hot_files": hot_files,
    }
