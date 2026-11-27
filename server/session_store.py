"""Cached Codex session directory scanner, byte-offset tailer, and delegation merger."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ValidationError

from server.delegation_store import DelegationStore, _atomic_write_json
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
from server.rollout_parser import STALE_RUNNING_SECONDS, RolloutParseState, _normalize_project_dir


SUMMARY_CACHE_SCHEMA_VERSION = 1
SUMMARY_CACHE_LIMIT = 500


class _CachedRolloutSummary(BaseModel):
    mtime_ns: int
    size: int
    is_subagent: bool
    session_id: str
    summary: SessionSummary
    changed_file_paths: list[str]


def _summary_cache_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _rollout_changed_files(state: RolloutParseState) -> list[str]:
    return list(dict.fromkeys(
        path for turn_id in state.turn_order for path in state.turns_by_id[turn_id].changed_files_by_path
    ))


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
        effort=rnd.reasoning_effort,
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
        error_code=rnd.error_code,
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
        reasoning_effort=latest.reasoning_effort if latest else None,
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
    """Keep rollout order, append unmatched rounds, and number the resulting timeline."""
    if rec is None or not rec.rounds:
        return turns

    merged = turns.copy()
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
        idx = next(iter(available_indices), None)
        if rnd.source_model == "Orbit":
            # A follow-up in a direct Codex session can have earlier, unmatched turns.
            # Rollout timestamps can be rounded to whole seconds.
            idx = next((
                i for i in available_indices
                if rnd.error_code != "thread_writer_busy"
                and merged[i].started_at
                and merged[i].started_at[:19] >= rnd.started_at[:19]
                and merged[i].prompt.strip() == rnd.prompt.strip()
            ), None)
        if idx is None:
            pending_rounds.append(rnd)
            continue
        available_indices.remove(idx)
        merged[idx] = _apply_round_to_turn(merged[idx], rnd)

    # Pass 3: append rounds without a matching rollout turn, including failed attempts.
    for rnd in pending_rounds:
        merged.append(_round_to_synthetic_turn(rnd, len(merged) + 1))

    return [
        turn if turn.round_index == index else turn.model_copy(update={"round_index": index})
        for index, turn in enumerate(merged, start=1)
    ]


def _apply_round_to_turn(turn: TurnDetail, rnd: DelegationRoundRecord) -> TurnDetail:
    updates: dict[str, object] = {
        "is_delegated": True,
        "source_model": rnd.source_model,
        "delegation_round": rnd.round_index,
        "isolation_mode": rnd.isolation_mode,
        "worktree_path": rnd.worktree_path,
        "error_code": rnd.error_code,
    }
    if not turn.prompt and rnd.prompt:
        updates["prompt"] = rnd.prompt
    if not turn.model and rnd.model:
        updates["model"] = rnd.model
    if not turn.effort and rnd.reasoning_effort:
        updates["effort"] = rnd.reasoning_effort
    if rnd.status == "running":
        updates["status"] = "running"
    elif rnd.status == "cancelled" and turn.status != "aborted":
        updates["status"] = "aborted"
        updates["error_reason"] = rnd.error_summary or "Cancelled by user."
    elif rnd.status == "failed" and rnd.error_code:
        updates["status"] = "aborted"
        updates["error_reason"] = rnd.error_summary or turn.error_reason
    return turn.model_copy(update=updates)


def _merge_summary_with_delegation(
    summary: SessionSummary,
    rec: DelegationSessionRecord | None,
    round_count: int,
) -> SessionSummary:
    if rec is None or not rec.rounds:
        return summary
    latest = rec.rounds[-1]
    updates: dict[str, object] = {
        "is_delegated": True,
        "source_model": latest.source_model,
        "isolation_mode": rec.isolation_mode,
        "worktree_path": rec.worktree_path,
        "round_count": round_count,
    }
    if rec.title and not summary.title.lower().startswith("agy:"):
        updates["title"] = rec.title
    if rec.workspace_dir:
        project_dir, project_name = _normalize_project_dir(rec.workspace_dir)
        updates["project_dir"] = project_dir
        updates["project_name"] = project_name
    if rec.worktree_branch and not summary.git_branch:
        updates["git_branch"] = rec.worktree_branch
    if latest.model and (latest.status == "running" or not summary.model):
        updates["model"] = latest.model
    if latest.reasoning_effort and (latest.status == "running" or not summary.reasoning_effort):
        updates["reasoning_effort"] = latest.reasoning_effort
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

        self._cache_lock = threading.RLock()
        self._states_by_path: dict[Path, RolloutParseState] = {}
        self._path_by_session_id: dict[str, Path] = {}
        self._index_titles: dict[str, str] = {}
        self._index_mtime_ns: int = -1
        self._index_size: int = -1
        self._summary_cache_path = (
            delegation_store.data_dir / "session_summary_cache.json" if delegation_store is not None else None
        )
        self._summary_cache: dict[str, _CachedRolloutSummary] | None = None
        self._summary_cache_dirty = False

    def _load_summary_cache(self) -> dict[str, _CachedRolloutSummary]:
        if self._summary_cache is not None:
            return self._summary_cache
        self._summary_cache = {}
        if self._summary_cache_path is None:
            return self._summary_cache
        try:
            payload = json.loads(self._summary_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._summary_cache
        if not isinstance(payload, dict) or payload.get("schema_version") != SUMMARY_CACHE_SCHEMA_VERSION:
            return self._summary_cache
        entries = payload.get("entries")
        if isinstance(entries, dict):
            for path, raw in entries.items():
                try:
                    entry = _CachedRolloutSummary.model_validate(raw)
                except ValidationError:
                    continue
                if entry.session_id == entry.summary.session_id:
                    self._summary_cache[path] = entry
        return self._summary_cache

    def _save_summary_cache(self) -> None:
        if not self._summary_cache_dirty or self._summary_cache_path is None:
            return
        entries = self._load_summary_cache()
        newest = sorted(entries, key=lambda path: entries[path].mtime_ns, reverse=True)[:SUMMARY_CACHE_LIMIT]
        try:
            _atomic_write_json(self._summary_cache_path, {
                "schema_version": SUMMARY_CACHE_SCHEMA_VERSION,
                "entries": {path: entries[path].model_dump(mode="json") for path in newest},
            })
        except OSError:
            return  # The cache is optional. Retry on the next scan.
        self._summary_cache = {path: entries[path] for path in newest}
        self._summary_cache_dirty = False

    def _remember_rollout_summary(self, state: RolloutParseState, summary: SessionSummary | None = None) -> None:
        if self._summary_cache_path is None or state.offset > state.file_size:
            return
        entries = self._load_summary_cache()
        path = _summary_cache_key(state.path)
        entry = entries.get(path)
        if entry is not None and (entry.mtime_ns, entry.size) == (state.file_mtime_ns, state.file_size):
            return
        if summary is None:
            summary = state.build_summary(now_ts=state.file_mtime_ns / 1e9)
        entries[path] = _CachedRolloutSummary(
            mtime_ns=state.file_mtime_ns,
            size=state.file_size,
            is_subagent=state.is_subagent,
            session_id=state.session_id,
            summary=summary,
            changed_file_paths=_rollout_changed_files(state),
        )
        self._summary_cache_dirty = True

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
        # Orbit launches return a run ID before Codex assigns the canonical thread ID.
        if session_id.startswith("run-"):
            delegation = self.delegation_store.get_by_run_id(session_id, verify_liveness=True)
            if delegation is not None:
                return delegation
        return self.delegation_store.get_by_session_id(session_id, verify_liveness=True)

    def _refresh_session_index(self) -> None:
        try:
            st = self.session_index_path.stat()
        except FileNotFoundError:
            self._index_titles = {}
            self._index_mtime_ns = self._index_size = -1
            return
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
                    if not isinstance(rec, dict):
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

    def _discover_rollout_files(self) -> list[tuple[os.stat_result, Path]]:
        if not self.sessions_dir.exists():
            return []
        entries: list[tuple[os.stat_result, Path]] = []
        for path in self.sessions_dir.rglob("rollout-*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((st, path))
        entries.sort(key=lambda item: item[0].st_mtime_ns, reverse=True)
        return entries

    def _ensure_state(self, path: Path, *, summary_stat: os.stat_result | None = None) -> RolloutParseState:
        state = self._states_by_path.get(path)
        if state is not None and self._summary_cache_path is not None and summary_stat is not None:
            if state.file_size == summary_stat.st_size:
                entry = self._load_summary_cache().get(_summary_cache_key(path))
                cached_mtime = entry.mtime_ns if entry is not None and entry.size == summary_stat.st_size else None
                if state.file_mtime_ns != summary_stat.st_mtime_ns or cached_mtime not in (None, summary_stat.st_mtime_ns):
                    # The byte-offset tailer cannot detect a same-size rewrite. Reparse before persisting it.
                    state = None
        if state is None:
            state = RolloutParseState(path=path)
        state.feed_file()
        self._states_by_path[path] = state
        self._remember_session_path(path, state.session_id, state.is_subagent)
        return state

    def _remember_session_path(self, path: Path, session_id: str, is_subagent: bool) -> None:
        if not is_subagent or session_id not in self._path_by_session_id:
            self._path_by_session_id[session_id] = path

    def _apply_summary_context(
        self, summary: SessionSummary, mtime_ns: int, now_ts: float | None,
    ) -> SessionSummary:
        # Index titles determine the inferred delegation flag used by the rollout parser.
        updates: dict[str, object] = {}
        title = self._index_titles.get(summary.session_id)
        if title:
            is_delegated = title.lower().startswith("agy:")
            updates.update(title=title, is_delegated=is_delegated, source_model="Gemini" if is_delegated else None)
        if summary.status == "running" and mtime_ns > 0:
            now = time.time() if now_ts is None else now_ts
            if now - mtime_ns / 1e9 > STALE_RUNNING_SECONDS:
                updates["status"] = "idle"
        return summary.model_copy(update=updates)

    def _build_rollout_summary(
        self,
        state: RolloutParseState,
        delegation: DelegationSessionRecord | None,
        now_ts: float | None,
    ) -> SessionSummary:
        # Persist the rollout's original status so cached running sessions age the same way as live ones.
        turns = state.build_turns(now_ts=state.file_mtime_ns / 1e9)
        raw_summary = state.build_summary(turns=turns)
        self._remember_rollout_summary(state, raw_summary)
        summary = self._apply_summary_context(raw_summary, state.file_mtime_ns, now_ts)
        round_count = len(_merge_turns_with_delegation(turns, delegation))
        return _merge_summary_with_delegation(summary, delegation, round_count)

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
                    summary.reasoning_effort,
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
        with self._cache_lock:
            return self._list_sessions(query=query, limit=limit, now_ts=now_ts)

    def _list_sessions(self, *, query: str, limit: int, now_ts: float | None) -> list[SessionSummary]:
        self._refresh_session_index()
        files = self._discover_rollout_files()
        delegations = self._delegations_map()
        cached_summaries = self._load_summary_cache()
        q = query.strip().lower()
        summaries: list[SessionSummary] = []
        seen_session_ids: set[str] = set()

        scan_cap = max(limit * 3, 300) if q else max(limit * 3, 240)
        for stat, path in files[:scan_cap]:
            summary: SessionSummary | None = None
            state = self._states_by_path.get(path)
            entry = cached_summaries.get(_summary_cache_key(path)) if cached_summaries else None
            if state is None and entry is not None and (entry.mtime_ns, entry.size) == (stat.st_mtime_ns, stat.st_size):
                self._remember_session_path(path, entry.session_id, entry.is_subagent)
                if entry.is_subagent or entry.session_id in seen_session_ids:
                    continue
                delegation = delegations.get(entry.session_id)
                # Matching existing turns and synthetic delegation rounds requires the rollout timeline.
                if delegation is None or not delegation.rounds:
                    summary = self._apply_summary_context(entry.summary, entry.mtime_ns, now_ts)
            if summary is None:
                try:
                    state = self._ensure_state(path, summary_stat=stat)
                except OSError:
                    continue
                # Hide internal Codex subagent child rollouts from the top-level list.
                if state.is_subagent or state.session_id in seen_session_ids:
                    self._remember_rollout_summary(state)
                    continue
                summary = self._build_rollout_summary(state, delegations.get(state.session_id), now_ts)
            seen_session_ids.add(summary.session_id)
            if self._matches_query(summary, q):
                summaries.append(summary)
                if not q and len(summaries) >= limit:
                    break

        # A recent delegation can bring an older rollout into the list.
        cutoff = min((summary.updated_at or "" for summary in summaries), default="") if len(summaries) >= limit else ""
        seen_records: set[str] = set()
        for rec in delegations.values():
            if rec.record_id in seen_records:
                continue
            seen_records.add(rec.record_id)
            key = rec.session_id or rec.record_id
            if key in seen_session_ids:
                continue
            summary = _synthetic_summary_from_delegation(rec)
            if (summary.updated_at or "") < cutoff or not self._matches_query(summary, q):
                continue
            if rec.session_id:
                path = self._path_by_session_id.get(key)
                if path is None or not path.exists():
                    path = next((path for _, path in files if key in path.name), None)
                if path is not None:
                    with suppress(OSError):
                        state = self._ensure_state(path, summary_stat=path.stat())
                        if state.session_id == key and not state.is_subagent:
                            summary = self._build_rollout_summary(state, rec, now_ts)
            if self._matches_query(summary, q):
                summaries.append(summary)

        summaries.sort(key=lambda s: s.updated_at or "", reverse=True)
        self._save_summary_cache()
        return summaries[:limit]

    def get_session_changed_files(self, session_id: str) -> list[str]:
        with self._cache_lock:
            path = self._path_by_session_id.get(session_id)
            state = self._states_by_path.get(path) if path is not None else None
            if state is not None and state.session_id == session_id:
                return _rollout_changed_files(state)

            entries = self._load_summary_cache()
            if path is not None:
                key = _summary_cache_key(path)
                candidates = [(key, entries[key])] if key in entries else []
            else:
                sessions_dir = Path(_summary_cache_key(self.sessions_dir))
                candidates = sorted(
                    ((key, entry) for key, entry in entries.items()
                     if entry.session_id == session_id and Path(key).is_relative_to(sessions_dir)),
                    key=lambda item: (not item[1].is_subagent, item[1].mtime_ns),
                    reverse=True,
                )
            for key, entry in candidates:
                if entry.session_id != session_id:
                    continue
                with suppress(OSError):
                    stat = Path(key).stat()
                    if (entry.mtime_ns, entry.size) == (stat.st_mtime_ns, stat.st_size):
                        return entry.changed_file_paths.copy()
            return []

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
        with self._cache_lock:
            return self._get_session_detail(session_id, now_ts=now_ts)

    def _get_session_detail(self, session_id: str, *, now_ts: float | None) -> tuple[SessionDetail | None, str]:
        self._refresh_session_index()
        delegation = self._get_delegation(session_id)
        meta_ts = delegation.updated_at if delegation else ""
        resolved_sid = delegation.session_id if (delegation and delegation.session_id) else session_id

        path = None if delegation and delegation.session_id is None else self._resolve_session_path(resolved_sid)
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
        merged_summary = _merge_summary_with_delegation(detail.summary, delegation, len(merged_turns))
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
        with self._cache_lock:
            return self._poll_session_delta(session_id, after_cursor, after_meta_ts=after_meta_ts, now_ts=now_ts)

    def _poll_session_delta(
        self, session_id: str, after_cursor: int, *, after_meta_ts: str, now_ts: float | None,
    ) -> tuple[SessionDelta | None, str]:
        self._refresh_session_index()
        delegation = self._get_delegation(session_id)
        meta_ts = delegation.updated_at if delegation else ""
        meta_changed = bool(meta_ts and meta_ts != after_meta_ts)
        resolved_sid = delegation.session_id if (delegation and delegation.session_id) else session_id

        path = None if delegation and delegation.session_id is None else self._resolve_session_path(resolved_sid)
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

        rollout_turns = state.build_turns(now_ts=now_ts)
        all_turns = _merge_turns_with_delegation(rollout_turns, delegation)
        # Rollout updates can shift synthetic rounds or replace a prompt-matched turn ID.
        synthetic_timeline = len(all_turns) > len(rollout_turns) or (
            delegation is not None
            and any(rnd.turn_id is None for rnd in delegation.rounds)
        )
        need_full_turns = (
            was_reset
            or client_ahead
            or (after_cursor == 0)
            or meta_changed
            or (client_behind and (not dirty_ids or synthetic_timeline))
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
            turns=rollout_turns,
        )
        summary = _merge_summary_with_delegation(summary, delegation, len(all_turns))
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
