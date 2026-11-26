"""Persistent delegation metadata store backed by `${PLUGIN_DATA}`."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.models import (
    DelegationRoundRecord,
    DelegationSessionRecord,
    DelegationStatus,
    IsolationMode,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_pointer_path() -> Path:
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        return Path(local_app) / "agy-codex-orbit" / "runtime.json"
    return Path.home() / ".agy-codex-orbit" / "runtime.json"


def resolve_data_dir(explicit: Path | str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    env_dir = os.environ.get("CODEX_ORBIT_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    ptr = runtime_pointer_path()
    if ptr.is_file():
        try:
            data = json.loads(ptr.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("data_dir"), str) and data["data_dir"].strip():
                return Path(data["data_dir"]).expanduser().resolve()
        except (OSError, json.JSONDecodeError):
            pass

    return runtime_pointer_path().parent / "data"


def is_process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # Error 5 (ACCESS_DENIED) still means the process exists.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


class DelegationStore:
    """Stores delegation run and multi-turn metadata in `${PLUGIN_DATA}/delegations`."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.delegations_dir = self.data_dir / "delegations"
        self.worktrees_dir = self.data_dir / "worktrees"

    def _record_path(self, record_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in record_id)
        return self.delegations_dir / f"{safe}.json"

    def _save(self, record: DelegationSessionRecord) -> DelegationSessionRecord:
        record.updated_at = utc_now_iso()
        _atomic_write_json(self._record_path(record.record_id), record.model_dump(mode="json"))
        return record

    def load_all(self, *, verify_liveness: bool = True) -> list[DelegationSessionRecord]:
        if not self.delegations_dir.exists():
            return []
        records: list[DelegationSessionRecord] = []
        for path in self.delegations_dir.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                rec = DelegationSessionRecord.model_validate(raw)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if verify_liveness:
                rec = self._reconcile_liveness(rec)
            records.append(rec)
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records

    def _reconcile_liveness(self, record: DelegationSessionRecord) -> DelegationSessionRecord:
        changed = False
        for rnd in record.rounds:
            if rnd.status == "running":
                alive = is_process_alive(rnd.child_pid) or is_process_alive(rnd.pid)
                if not alive:
                    rnd.status = "failed"
                    rnd.ended_at = rnd.ended_at or utc_now_iso()
                    if not rnd.error_summary:
                        rnd.error_summary = "Delegation process exited unexpectedly."
                    changed = True
        if changed:
            try:
                self._save(record)
            except OSError:
                pass
        return record

    def get_by_session_id(
        self,
        session_id: str,
        *,
        verify_liveness: bool = True,
    ) -> DelegationSessionRecord | None:
        direct_path = self._record_path(session_id)
        if direct_path.is_file():
            try:
                rec = DelegationSessionRecord.model_validate(
                    json.loads(direct_path.read_text(encoding="utf-8"))
                )
                return self._reconcile_liveness(rec) if verify_liveness else rec
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        for rec in self.load_all(verify_liveness=verify_liveness):
            if rec.session_id == session_id or rec.record_id == session_id:
                return rec
        return None

    def get_by_run_id(self, run_id: str) -> DelegationSessionRecord | None:
        direct_path = self._record_path(run_id)
        if direct_path.is_file():
            try:
                return DelegationSessionRecord.model_validate(
                    json.loads(direct_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        for rec in self.load_all(verify_liveness=False):
            if any(r.run_id == run_id for r in rec.rounds):
                return rec
        return None

    def start_round(
        self,
        *,
        run_id: str,
        workspace_dir: str,
        prompt: str,
        isolation_mode: IsolationMode,
        source_model: str = "Gemini",
        title: str | None = None,
        session_id: str | None = None,
        worktree_path: str | None = None,
        worktree_branch: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        pid: int | None = None,
    ) -> tuple[DelegationSessionRecord, DelegationRoundRecord]:
        existing = self.get_by_session_id(session_id, verify_liveness=False) if session_id else None
        now = utc_now_iso()

        if existing is not None:
            round_index = len(existing.rounds) + 1
            eff_worktree = worktree_path or existing.worktree_path
            eff_branch = worktree_branch or existing.worktree_branch
            rnd = DelegationRoundRecord(
                round_index=round_index,
                run_id=run_id,
                source_model=source_model,
                title=title or existing.title,
                prompt=prompt,
                isolation_mode=isolation_mode,
                workspace_dir=workspace_dir or existing.workspace_dir,
                worktree_path=eff_worktree,
                worktree_branch=eff_branch,
                model=model,
                reasoning_effort=reasoning_effort,
                status="running",
                pid=pid,
                started_at=now,
            )
            existing.rounds.append(rnd)
            if title and not existing.title:
                existing.title = title
            if eff_worktree:
                existing.worktree_path = eff_worktree
            if eff_branch:
                existing.worktree_branch = eff_branch
            existing.isolation_mode = isolation_mode
            self._save(existing)
            return existing, rnd

        record_id = session_id or run_id
        rnd = DelegationRoundRecord(
            round_index=1,
            run_id=run_id,
            source_model=source_model,
            title=title,
            prompt=prompt,
            isolation_mode=isolation_mode,
            workspace_dir=workspace_dir,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            status="running",
            pid=pid,
            started_at=now,
        )
        record = DelegationSessionRecord(
            record_id=record_id,
            session_id=session_id,
            title=title,
            workspace_dir=workspace_dir,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            isolation_mode=isolation_mode,
            rounds=[rnd],
            updated_at=now,
        )
        self._save(record)
        return record, rnd

    def bind_session_id(self, run_id: str, session_id: str) -> DelegationSessionRecord | None:
        if not session_id:
            return None
        current = self.get_by_run_id(run_id)
        if current is None:
            return None
        if current.session_id == session_id and current.record_id == session_id:
            return current

        old_path = self._record_path(current.record_id)
        target = self.get_by_session_id(session_id, verify_liveness=False)
        if target is not None and target.record_id != current.record_id:
            # Merge rounds into existing session record.
            existing_run_ids = {r.run_id for r in target.rounds}
            for rnd in current.rounds:
                if rnd.run_id not in existing_run_ids:
                    rnd.round_index = len(target.rounds) + 1
                    target.rounds.append(rnd)
            if current.worktree_path and not target.worktree_path:
                target.worktree_path = current.worktree_path
                target.worktree_branch = current.worktree_branch
            self._save(target)
            if old_path.exists() and old_path != self._record_path(target.record_id):
                try:
                    old_path.unlink()
                except OSError:
                    pass
            return target

        current.record_id = session_id
        current.session_id = session_id
        self._save(current)
        new_path = self._record_path(session_id)
        if old_path.exists() and old_path != new_path:
            try:
                old_path.unlink()
            except OSError:
                pass
        return current

    def update_round(
        self,
        run_id: str,
        *,
        child_pid: int | None = None,
        turn_id: str | None = None,
        status: DelegationStatus | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        command_count: int | None = None,
        changed_files: list[str] | None = None,
        final_message: str | None = None,
        error_summary: str | None = None,
        ended: bool = False,
    ) -> DelegationSessionRecord | None:
        rec = self.get_by_run_id(run_id)
        if rec is None:
            return None
        target_round = next((r for r in rec.rounds if r.run_id == run_id), None)
        if target_round is None:
            return None

        if child_pid is not None:
            target_round.child_pid = child_pid
        if turn_id is not None:
            target_round.turn_id = turn_id
        if status is not None:
            target_round.status = status
        if exit_code is not None:
            target_round.exit_code = exit_code
        if duration_ms is not None:
            target_round.duration_ms = duration_ms
        if command_count is not None:
            target_round.command_count = command_count
        if changed_files is not None:
            target_round.changed_files = changed_files
        if final_message is not None:
            target_round.final_message = final_message
        if error_summary is not None:
            target_round.error_summary = error_summary
        if ended:
            target_round.ended_at = utc_now_iso()

        return self._save(rec)
