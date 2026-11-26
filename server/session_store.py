"""Cached Codex session directory scanner, byte-offset tailer, and delegation merger."""

from __future__ import annotations

import json
import os
from pathlib import Path

from server.delegation_store import DelegationStore
from server.models import (
    AssistantBlock,
    DelegationRoundRecord,
    DelegationSessionRecord,
    FileChangeRecord,
    SessionDelta,
    SessionDetail,
    SessionStatus,
    SessionSummary,
    TurnDetail,
    TurnStatus,
)
from server.rollout_parser import RolloutParseState, _normalize_project_dir


def default_codex_home() -> Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".codex"


def _map_delegation_status(status: str) -> SessionStatus:
    if status == "running":
        return "running"
    if status == "completed":
        return "completed"
    if status in ("cancelled", "failed"):
        return "aborted"
    return "idle"


def _round_to_synthetic_turn(rnd: DelegationRoundRecord, round_index: int) -> TurnDetail:
    status: TurnStatus = _map_delegation_status(rnd.status)
    messages: list[AssistantBlock] = []
    if rnd.final_message:
        messages.append(
            AssistantBlock(
                id=f"delegation-final:{rnd.run_id}",
                phase="final",
                text=rnd.final_message,
                timestamp=rnd.ended_at or rnd.started_at,
            )
        )
    files = [FileChangeRecord(path=p, change_type="update") for p in rnd.changed_files]
    return TurnDetail(
        round_index=round_index,
        turn_id=rnd.turn_id or f"delegation:{rnd.run_id}",
        status=status,
        model=rnd.model,
        is_delegated=True,
        source_model=rnd.source_model,
        delegation_round=rnd.round_index,
        isolation_mode=rnd.isolation_mode,
        worktree_path=rnd.worktree_path,
        prompt=rnd.prompt,
        started_at=rnd.started_at,
        completed_at=rnd.ended_at,
        duration_ms=rnd.duration_ms,
        assistant_messages=messages,
        changed_files=files,
        command_count=rnd.command_count,
        changed_file_count=len(files),
        error_reason=rnd.error_summary,
    )


def _synthetic_summary_from_delegation(rec: DelegationSessionRecord) -> SessionSummary:
    project_dir, project_name = _normalize_project_dir(rec.workspace_dir)
    latest = rec.rounds[-1] if rec.rounds else None
    first = rec.rounds[0] if rec.rounds else None

    title = (rec.title or "").strip()
    if not title and first and first.prompt.strip():
        first_line = first.prompt.strip().splitlines()[0].strip()
        title = first_line[:88] + ("..." if len(first_line) > 88 else "")
    if not title:
        title = f"Delegation {rec.record_id[:10]}"

    unique_files = {fp for r in rec.rounds for fp in r.changed_files}
    active_run_id = latest.run_id if (latest and latest.status == "running") else None

    return SessionSummary(
        session_id=rec.session_id or rec.record_id,
        title=title,
        project_dir=project_dir,
        project_name=project_name,
        git_branch=rec.worktree_branch,
        model=latest.model if latest else None,
        originator="Codex Orbit",
        status=_map_delegation_status(latest.status) if latest else "idle",
        round_count=len(rec.rounds),
        command_count=sum(r.command_count for r in rec.rounds),
        changed_file_count=len(unique_files),
        started_at=first.started_at if first else rec.updated_at,
        updated_at=rec.updated_at,
        duration_ms=latest.duration_ms if latest else None,
        file_size=0,
        is_delegated=True,
        source_model=latest.source_model if latest else "Gemini",
        isolation_mode=rec.isolation_mode,
        worktree_path=rec.worktree_path,
        active_run_id=active_run_id,
    )


def _synthetic_detail_from_delegation(rec: DelegationSessionRecord) -> SessionDetail:
    summary = _synthetic_summary_from_delegation(rec)
    turns = [_round_to_synthetic_turn(rnd, idx + 1) for idx, rnd in enumerate(rec.rounds)]
    unique_files: dict[str, FileChangeRecord] = {}
    for t in turns:
        for fc in t.changed_files:
            unique_files[fc.path] = fc
    return SessionDetail(
        summary=summary,
        turns=turns,
        cursor=0,
        file_mtime_ns=0,
        file_size=0,
        total_commands=sum(t.command_count for t in turns),
        total_changed_files=len(unique_files),
        all_changed_files=list(unique_files.values()),
    )


def _merge_turns_with_delegation(
    turns: list[TurnDetail],
    rec: DelegationSessionRecord | None,
) -> list[TurnDetail]:
    if rec is None or not rec.rounds:
        return turns

    merged = [t.model_copy() for t in turns]
    matched_turn_indices: set[int] = set()
    unmatched_rounds: list[DelegationRoundRecord] = []

    # Pass 1: exact turn_id match
    for rnd in rec.rounds:
        hit_idx = None
        if rnd.turn_id:
            for idx, turn in enumerate(merged):
                if idx not in matched_turn_indices and turn.turn_id == rnd.turn_id:
                    hit_idx = idx
                    break
        if hit_idx is not None:
            matched_turn_indices.add(hit_idx)
            merged[hit_idx] = _apply_round_to_turn(merged[hit_idx], rnd)
        else:
            unmatched_rounds.append(rnd)

    # Pass 2: match remaining rounds chronologically to unmatched rollout turns
    available_indices = [i for i in range(len(merged)) if i not in matched_turn_indices]
    pending_rounds: list[DelegationRoundRecord] = []
    for rnd in unmatched_rounds:
        if available_indices:
            idx = available_indices.pop(0)
            matched_turn_indices.add(idx)
            merged[idx] = _apply_round_to_turn(merged[idx], rnd)
        else:
            pending_rounds.append(rnd)

    # Pass 3: append still-starting rounds that haven't flushed a turn into rollout yet
    for rnd in pending_rounds:
        merged.append(_round_to_synthetic_turn(rnd, len(merged) + 1))

    return merged


def _apply_round_to_turn(turn: TurnDetail, rnd: DelegationRoundRecord) -> TurnDetail:
    updates: dict[str, object] = {
        "is_delegated": True,
        "source_model": rnd.source_model,
        "delegation_round": rnd.round_index,
        "isolation_mode": rnd.isolation_mode,
        "worktree_path": rnd.worktree_path,
    }
    if not turn.prompt and rnd.prompt:
        updates["prompt"] = rnd.prompt
    if rnd.status == "running":
        updates["status"] = "running"
    elif rnd.status == "cancelled" and turn.status != "aborted":
        updates["status"] = "aborted"
        updates["error_reason"] = rnd.error_summary or "Cancelled by user."
    return turn.model_copy(update=updates)


def _merge_summary_with_delegation(
    summary: SessionSummary,
    rec: DelegationSessionRecord | None,
) -> SessionSummary:
    if rec is None or not rec.rounds:
        return summary
    latest = rec.rounds[-1]
    updates: dict[str, object] = {
        "is_delegated": True,
        "source_model": latest.source_model,
        "isolation_mode": rec.isolation_mode,
        "worktree_path": rec.worktree_path,
        "round_count": max(summary.round_count, len(rec.rounds)),
    }
    if rec.title and not summary.title.lower().startswith("agy:"):
        updates["title"] = rec.title
    if rec.workspace_dir:
        project_dir, project_name = _normalize_project_dir(rec.workspace_dir)
        updates["project_dir"] = project_dir
        updates["project_name"] = project_name
    if rec.worktree_branch and not summary.git_branch:
        updates["git_branch"] = rec.worktree_branch
    if latest.status == "running":
        updates["status"] = "running"
        updates["active_run_id"] = latest.run_id
    elif latest.status == "cancelled":
        updates["status"] = "aborted"
    if rec.updated_at and (not summary.updated_at or rec.updated_at > summary.updated_at):
        updates["updated_at"] = rec.updated_at
    return summary.model_copy(update=updates)


class SessionStore:
    """Indexes local `%USERPROFILE%\\.codex\\sessions\\...\\rollout-*.jsonl` files + delegations."""

    def __init__(
        self,
        sessions_dir: Path | None = None,
        session_index_path: Path | None = None,
        delegation_store: DelegationStore | None = None,
    ) -> None:
        codex_home = default_codex_home()
        env_sessions = os.environ.get("CODEX_ORBIT_SESSIONS_DIR")
        if sessions_dir is not None:
            self.sessions_dir = Path(sessions_dir)
        elif env_sessions:
            self.sessions_dir = Path(env_sessions)
        else:
            self.sessions_dir = codex_home / "sessions"

        self.session_index_path = (
            Path(session_index_path)
            if session_index_path is not None
            else (self.sessions_dir.parent / "session_index.jsonl")
        )
        self.delegation_store = delegation_store

        self._states_by_path: dict[Path, RolloutParseState] = {}
        self._path_by_session_id: dict[str, Path] = {}
        self._index_titles: dict[str, str] = {}
        self._index_mtime_ns: int = -1
        self._index_size: int = -1

    def _delegations_map(self) -> dict[str, DelegationSessionRecord]:
        if self.delegation_store is None:
            return {}
        by_key: dict[str, DelegationSessionRecord] = {}
        for rec in self.delegation_store.load_all(verify_liveness=True):
            key = rec.session_id or rec.record_id
            by_key[key] = rec
            by_key[rec.record_id] = rec
        return by_key

    def _get_delegation(self, session_id: str) -> DelegationSessionRecord | None:
        if self.delegation_store is None:
            return None
        return self.delegation_store.get_by_session_id(session_id, verify_liveness=True)

    def _refresh_session_index(self) -> None:
        if not self.session_index_path.exists():
            return
        try:
            st = self.session_index_path.stat()
        except OSError:
            return
        if st.st_mtime_ns == self._index_mtime_ns and st.st_size == self._index_size:
            return
        titles: dict[str, str] = {}
        try:
            with self.session_index_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = rec.get("id")
                    name = rec.get("thread_name")
                    if isinstance(sid, str) and isinstance(name, str) and name.strip():
                        titles[sid] = name.strip()
        except OSError:
            return
        self._index_titles = titles
        self._index_mtime_ns = st.st_mtime_ns
        self._index_size = st.st_size

    def _discover_rollout_files(self) -> list[tuple[int, Path]]:
        if not self.sessions_dir.exists():
            return []
        entries: list[tuple[int, Path]] = []
        for path in self.sessions_dir.rglob("rollout-*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((st.st_mtime_ns, path))
        entries.sort(key=lambda item: item[0], reverse=True)
        return entries

    def _ensure_state(self, path: Path) -> RolloutParseState:
        state = self._states_by_path.get(path)
        if state is None:
            state = RolloutParseState(path=path)
            self._states_by_path[path] = state
        state.feed_file()
        self._path_by_session_id[state.session_id] = path
        return state

    @staticmethod
    def _matches_query(summary: SessionSummary, q: str) -> bool:
        if not q:
            return True
        haystack = " ".join(
            filter(
                None,
                [
                    summary.title,
                    summary.project_dir,
                    summary.project_name,
                    summary.model,
                    summary.git_branch,
                    summary.session_id,
                    summary.source_model,
                    summary.isolation_mode,
                ],
            )
        ).lower()
        return q in haystack

    def list_sessions(
        self,
        *,
        query: str = "",
        limit: int = 80,
        now_ts: float | None = None,
    ) -> list[SessionSummary]:
        self._refresh_session_index()
        files = self._discover_rollout_files()
        delegations = self._delegations_map()
        q = query.strip().lower()
        summaries: list[SessionSummary] = []
        seen_session_ids: set[str] = set()

        scan_cap = max(limit, 200) if q else max(1, limit)
        for _, path in files[:scan_cap]:
            try:
                state = self._ensure_state(path)
            except OSError:
                continue
            summary = state.build_summary(
                indexed_title=self._index_titles.get(state.session_id),
                now_ts=now_ts,
            )
            delegation = delegations.get(state.session_id)
            summary = _merge_summary_with_delegation(summary, delegation)
            seen_session_ids.add(summary.session_id)
            if self._matches_query(summary, q):
                summaries.append(summary)

        # Include delegated runs not yet flushed to a rollout file.
        seen_records: set[str] = set()
        for rec in delegations.values():
            if rec.record_id in seen_records:
                continue
            seen_records.add(rec.record_id)
            key = rec.session_id or rec.record_id
            if key in seen_session_ids:
                continue
            synthetic = _synthetic_summary_from_delegation(rec)
            if self._matches_query(synthetic, q):
                summaries.append(synthetic)

        summaries.sort(key=lambda s: s.updated_at or "", reverse=True)
        return summaries[:limit]

    def _resolve_session_path(self, session_id: str) -> Path | None:
        cached = self._path_by_session_id.get(session_id)
        if cached is not None and cached.exists():
            return cached

        for _, path in self._discover_rollout_files():
            if session_id in path.name:
                self._ensure_state(path)
                return path

        for _, path in self._discover_rollout_files():
            try:
                state = self._ensure_state(path)
            except OSError:
                continue
            if state.session_id == session_id:
                return path
        return None

    def get_session_detail(
        self,
        session_id: str,
        *,
        now_ts: float | None = None,
    ) -> tuple[SessionDetail | None, str]:
        self._refresh_session_index()
        delegation = self._get_delegation(session_id)
        meta_ts = delegation.updated_at if delegation else ""
        resolved_sid = delegation.session_id if (delegation and delegation.session_id) else session_id

        path = self._resolve_session_path(resolved_sid)
        if path is None:
            if delegation is not None:
                return _synthetic_detail_from_delegation(delegation), meta_ts
            return None, ""

        state = self._ensure_state(path)
        detail = state.build_detail(
            indexed_title=self._index_titles.get(state.session_id),
            now_ts=now_ts,
        )
        merged_turns = _merge_turns_with_delegation(detail.turns, delegation)
        merged_summary = _merge_summary_with_delegation(detail.summary, delegation)
        detail = detail.model_copy(
            update={
                "summary": merged_summary,
                "turns": merged_turns,
            }
        )
        return detail, meta_ts

    def poll_session_delta(
        self,
        session_id: str,
        after_cursor: int = 0,
        *,
        after_meta_ts: str = "",
        now_ts: float | None = None,
    ) -> tuple[SessionDelta | None, str]:
        self._refresh_session_index()
        delegation = self._get_delegation(session_id)
        meta_ts = delegation.updated_at if delegation else ""
        meta_changed = bool(meta_ts and meta_ts != after_meta_ts)
        resolved_sid = delegation.session_id if (delegation and delegation.session_id) else session_id

        path = self._resolve_session_path(resolved_sid)
        if path is None:
            if delegation is None:
                return None, ""
            if not meta_changed and after_meta_ts == meta_ts:
                return (
                    SessionDelta(
                        session_id=resolved_sid,
                        changed=False,
                        reset=False,
                        cursor=0,
                        file_size=0,
                    ),
                    meta_ts,
                )
            synth = _synthetic_detail_from_delegation(delegation)
            return (
                SessionDelta(
                    session_id=synth.summary.session_id,
                    changed=True,
                    reset=True,
                    cursor=0,
                    file_size=0,
                    summary=synth.summary,
                    updated_turns=synth.turns,
                    total_commands=synth.total_commands,
                    total_changed_files=synth.total_changed_files,
                    all_changed_files=synth.all_changed_files,
                ),
                meta_ts,
            )

        state = self._states_by_path.get(path)
        if state is None:
            state = RolloutParseState(path=path)
            self._states_by_path[path] = state

        changed, was_reset, dirty_ids = state.feed_file()
        self._path_by_session_id[state.session_id] = path

        client_behind = after_cursor < state.offset
        client_ahead = after_cursor > state.offset
        if not changed and not client_behind and not client_ahead and not meta_changed:
            return (
                SessionDelta(
                    session_id=state.session_id,
                    changed=False,
                    reset=False,
                    cursor=state.offset,
                    file_size=state.file_size,
                ),
                meta_ts,
            )

        all_turns = _merge_turns_with_delegation(state.build_turns(now_ts=now_ts), delegation)
        need_full_turns = (
            was_reset
            or client_ahead
            or (after_cursor == 0)
            or meta_changed
            or (client_behind and not dirty_ids)
        )
        if need_full_turns:
            updated_turns = all_turns
        else:
            dirty_set = set(dirty_ids)
            updated_turns = [t for t in all_turns if t.turn_id in dirty_set]

        unique_files: dict[str, FileChangeRecord] = {}
        for t in all_turns:
            for fc in t.changed_files:
                unique_files[fc.path] = fc

        summary = state.build_summary(
            indexed_title=self._index_titles.get(state.session_id),
            now_ts=now_ts,
        )
        summary = _merge_summary_with_delegation(summary, delegation)
        return (
            SessionDelta(
                session_id=state.session_id,
                changed=True,
                reset=need_full_turns,
                cursor=state.offset,
                file_size=state.file_size,
                summary=summary,
                updated_turns=updated_turns,
                total_commands=sum(t.command_count for t in all_turns),
                total_changed_files=len(unique_files),
                all_changed_files=list(unique_files.values()),
            ),
            meta_ts,
        )
