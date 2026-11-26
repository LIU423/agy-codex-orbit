"""Cached Codex session directory scanner and incremental byte-offset tailer."""

from __future__ import annotations

import json
import os
from pathlib import Path

from server.models import FileChangeRecord, SessionDelta, SessionDetail, SessionSummary
from server.rollout_parser import RolloutParseState


def default_codex_home() -> Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".codex"


class SessionStore:
    """Indexes local `%USERPROFILE%\\.codex\\sessions\\...\\rollout-*.jsonl` files."""

    def __init__(
        self,
        sessions_dir: Path | None = None,
        session_index_path: Path | None = None,
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

        self._states_by_path: dict[Path, RolloutParseState] = {}
        self._path_by_session_id: dict[str, Path] = {}
        self._index_titles: dict[str, str] = {}
        self._index_mtime_ns: int = -1
        self._index_size: int = -1

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

    def list_sessions(
        self,
        *,
        query: str = "",
        limit: int = 80,
        now_ts: float | None = None,
    ) -> list[SessionSummary]:
        self._refresh_session_index()
        files = self._discover_rollout_files()
        q = query.strip().lower()
        summaries: list[SessionSummary] = []

        # When filtering by query, inspect up to 200 recent files; otherwise cap early.
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
            if q:
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
                        ],
                    )
                ).lower()
                if q not in haystack:
                    continue
            summaries.append(summary)
            if len(summaries) >= limit:
                break

        return summaries

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
    ) -> SessionDetail | None:
        self._refresh_session_index()
        path = self._resolve_session_path(session_id)
        if path is None:
            return None
        state = self._ensure_state(path)
        return state.build_detail(
            indexed_title=self._index_titles.get(state.session_id),
            now_ts=now_ts,
        )

    def poll_session_delta(
        self,
        session_id: str,
        after_cursor: int = 0,
        *,
        now_ts: float | None = None,
    ) -> SessionDelta | None:
        self._refresh_session_index()
        path = self._resolve_session_path(session_id)
        if path is None:
            return None

        state = self._states_by_path.get(path)
        if state is None:
            state = RolloutParseState(path=path)
            self._states_by_path[path] = state

        changed, was_reset, dirty_ids = state.feed_file()
        self._path_by_session_id[state.session_id] = path

        # Client cursor lags behind cached server offset or file reset/truncated.
        client_behind = after_cursor < state.offset
        client_ahead = after_cursor > state.offset
        if not changed and not client_behind and not client_ahead:
            return SessionDelta(
                session_id=state.session_id,
                changed=False,
                reset=False,
                cursor=state.offset,
                file_size=state.file_size,
            )

        need_full_turns = was_reset or client_ahead or (after_cursor == 0) or (client_behind and not dirty_ids)
        if need_full_turns:
            updated_turns = state.build_turns(now_ts=now_ts)
        else:
            updated_turns = state.build_turns(only_ids=set(dirty_ids), now_ts=now_ts)

        all_turns = state.build_turns(now_ts=now_ts)
        unique_files: dict[str, FileChangeRecord] = {}
        for t in all_turns:
            for fc in t.changed_files:
                unique_files[fc.path] = fc

        summary = state.build_summary(
            indexed_title=self._index_titles.get(state.session_id),
            now_ts=now_ts,
        )
        return SessionDelta(
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
        )
