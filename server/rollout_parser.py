"""Incremental parser for Codex rollout-*.jsonl session logs.

Supports:
- Structured `event_msg` streams (`task_started`, `task_complete`, `turn_aborted`,
  `thread_settings_applied`, `token_count`, `item_completed` with `UserMessage`,
  `AgentMessage`, `Reasoning`, `CommandExecution`, `FileChange`).
- Legacy / fallback `response_item` parsing when a rollout lacks `item_completed`.
- Byte-offset incremental tailing that safely ignores incomplete trailing lines.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.models import (
    AssistantBlock,
    CommandRecord,
    FileChangeRecord,
    SessionDetail,
    SessionStatus,
    SessionSummary,
    TokenUsageSummary,
    TurnDetail,
    TurnStatus,
)

MAX_OUTPUT_PREVIEW_CHARS = 4000
MAX_PROMPT_CHARS = 12000
MAX_ASSISTANT_TEXT_CHARS = 24000
STALE_RUNNING_SECONDS = 600

_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_SKIP_TYPE_MARKERS = (
    b'"type":"world_state"',
    b'"type": "world_state"',
    b'"type":"token_usage_record"',
    b'"type": "token_usage_record"',
    b'"type":"inter_agent_communication_metadata"',
    b'"type": "inter_agent_communication_metadata"',
)
_EXEC_TOOL_NAMES = {"exec", "exec_command", "shell", "shell_command"}
_EXIT_CODE_RE = re.compile(r"(?:Exit code|Process exited with code|exit_code)\s*[:=]\s*(-?\d+)")
_WALL_TIME_RE = re.compile(r"Wall time:\s*([0-9]+(?:\.[0-9]+)?)\s*seconds?", re.IGNORECASE)
_SYSTEM_PROMPT_PREFIXES = (
    "<environment_context>",
    "# agents.md instructions",
    "<instructions>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<user_instructions>",
    "<developer_instructions>",
)


def _is_system_boilerplate(text: str) -> bool:
    low = text.lstrip().lower()
    return any(low.startswith(prefix) for prefix in _SYSTEM_PROMPT_PREFIXES)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n... [truncated]", True


def _epoch_to_iso(value: int | float | None) -> str | None:
    if value is None:
        return None
    try:
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _mtime_ns_to_iso(mtime_ns: int) -> str | None:
    if mtime_ns <= 0:
        return None
    return _epoch_to_iso(mtime_ns / 1e9)


def _extract_uuid_from_path(path: Path) -> str:
    match = _UUID_RE.search(path.name)
    if match:
        return match.group(1)
    return path.stem


def _normalize_project_dir(cwd: str | None) -> tuple[str, str]:
    if not cwd or not cwd.strip():
        return "(Unknown Workspace)", "Unknown Workspace"
    cleaned = cwd.strip().rstrip("\\/")
    if not cleaned:
        return cwd.strip(), cwd.strip()
    name = os.path.basename(cleaned) or cleaned
    return cleaned, name


def _format_command(command: Any, parsed_cmd: Any = None) -> str:
    if isinstance(command, list) and command:
        parts = [str(p) for p in command if p is not None]
        if (
            len(parts) >= 3
            and parts[0].lower().endswith(("powershell.exe", "pwsh.exe", "powershell", "pwsh", "bash", "sh", "cmd.exe", "cmd"))
            and parts[1].lower() in ("-command", "-c", "/c", "-lc")
        ):
            return " ".join(parts[2:]).strip()
        if (
            len(parts) >= 4
            and parts[0].lower().endswith(("powershell.exe", "pwsh.exe", "powershell", "pwsh"))
            and parts[2].lower() in ("-command", "-c")
        ):
            return " ".join(parts[3:]).strip()
        return " ".join(parts).strip()
    if isinstance(command, str) and command.strip():
        return command.strip()
    if isinstance(parsed_cmd, list) and parsed_cmd:
        first = parsed_cmd[0]
        if isinstance(first, dict) and isinstance(first.get("cmd"), str):
            return first["cmd"].strip()
    return "(command)"


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif isinstance(item, str) and item:
            parts.append(item)
    return "\n".join(parts).strip()


def _duration_ms_from_obj(duration: Any, started_ms: Any = None, completed_ms: Any = None) -> int | None:
    if isinstance(duration, dict):
        secs = duration.get("secs")
        nanos = duration.get("nanos", 0)
        if isinstance(secs, (int, float)):
            return max(0, int(secs * 1000 + (nanos or 0) // 1_000_000))
    if isinstance(duration, (int, float)):
        return max(0, int(duration))
    if isinstance(started_ms, (int, float)) and isinstance(completed_ms, (int, float)):
        return max(0, int(completed_ms - started_ms))
    return None


@dataclass
class _TurnAccumulator:
    round_index: int
    turn_id: str
    status: TurnStatus = "completed"
    model: str | None = None
    effort: str | None = None
    prompt: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    error_reason: str | None = None
    token_usage: TokenUsageSummary | None = None
    reasoning_by_id: dict[str, str] = field(default_factory=dict)
    reasoning_order: list[str] = field(default_factory=list)
    messages_by_id: dict[str, AssistantBlock] = field(default_factory=dict)
    message_order: list[str] = field(default_factory=list)
    commands_by_id: dict[str, CommandRecord] = field(default_factory=dict)
    command_order: list[str] = field(default_factory=list)
    changed_files_by_path: dict[str, FileChangeRecord] = field(default_factory=dict)
    fallback_commands_by_id: dict[str, CommandRecord] = field(default_factory=dict)
    fallback_command_order: list[str] = field(default_factory=list)
    fallback_messages_by_id: dict[str, AssistantBlock] = field(default_factory=dict)
    fallback_message_order: list[str] = field(default_factory=list)
    fallback_reasoning_by_id: dict[str, str] = field(default_factory=dict)
    fallback_reasoning_order: list[str] = field(default_factory=list)
    fallback_prompt: str = ""
    has_structured_items: bool = False
    dirty: bool = True

    def to_model(self, *, use_fallback: bool, stale_idle: bool) -> TurnDetail:
        status = self.status
        if status == "running" and stale_idle:
            status = "idle"

        structured_messages = [
            self.messages_by_id[i] for i in self.message_order if i in self.messages_by_id
        ]
        fallback_messages = [
            self.fallback_messages_by_id[i]
            for i in self.fallback_message_order
            if i in self.fallback_messages_by_id
        ]

        if not use_fallback:
            prompt = self.prompt or self.fallback_prompt
            reasoning = [self.reasoning_by_id[i] for i in self.reasoning_order if self.reasoning_by_id.get(i)]
            messages = structured_messages
            commands = [self.commands_by_id[i] for i in self.command_order if i in self.commands_by_id]
        else:
            prompt = self.prompt or self.fallback_prompt
            reasoning = [
                self.fallback_reasoning_by_id[i]
                for i in self.fallback_reasoning_order
                if self.fallback_reasoning_by_id.get(i)
            ]
            messages = fallback_messages or structured_messages
            commands = [
                self.fallback_commands_by_id[i]
                for i in self.fallback_command_order
                if i in self.fallback_commands_by_id
            ]

        changed_files = list(self.changed_files_by_path.values())
        return TurnDetail(
            round_index=self.round_index,
            turn_id=self.turn_id,
            status=status,
            model=self.model,
            effort=self.effort,
            prompt=prompt,
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_ms=self.duration_ms,
            reasoning_summaries=reasoning,
            assistant_messages=messages,
            commands=commands,
            changed_files=changed_files,
            command_count=len(commands),
            changed_file_count=len(changed_files),
            error_reason=self.error_reason,
            token_usage=self.token_usage,
        )


@dataclass
class RolloutParseState:
    path: Path
    session_id: str = ""
    cwd: str | None = None
    git_branch: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    originator: str | None = None
    started_at: str | None = None
    last_event_at: str | None = None
    total_tokens: int = 0
    offset: int = 0
    file_mtime_ns: int = 0
    file_size: int = 0
    has_structured_items: bool = False
    turns_by_id: dict[str, _TurnAccumulator] = field(default_factory=dict)
    turn_order: list[str] = field(default_factory=list)
    active_turn_id: str | None = None
    spawned_thread_ids: list[str] = field(default_factory=list)
    _synthetic_turn_seq: int = 0

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = _extract_uuid_from_path(self.path)

    def _get_or_create_turn(self, turn_id: str | None, *, timestamp: str | None = None) -> _TurnAccumulator:
        if not turn_id:
            if self.active_turn_id and self.active_turn_id in self.turns_by_id:
                return self.turns_by_id[self.active_turn_id]
            self._synthetic_turn_seq += 1
            turn_id = f"turn-{self._synthetic_turn_seq}"

        turn = self.turns_by_id.get(turn_id)
        if turn is None:
            turn = _TurnAccumulator(
                round_index=len(self.turn_order) + 1,
                turn_id=turn_id,
                model=self.model,
                started_at=timestamp,
            )
            self.turns_by_id[turn_id] = turn
            self.turn_order.append(turn_id)
        self.active_turn_id = turn_id
        return turn

    def feed_file(self) -> tuple[bool, bool, list[str]]:
        """Read any newly appended complete lines from disk.

        Returns `(changed, was_reset, dirty_turn_ids)`.
        """
        stat = self.path.stat()
        mtime_ns = stat.st_mtime_ns
        size = stat.st_size

        was_reset = False
        if size < self.offset:
            self._reset()
            was_reset = True

        if size == self.offset and mtime_ns == self.file_mtime_ns:
            return False, False, []

        self.file_mtime_ns = mtime_ns
        self.file_size = size

        if size == self.offset:
            return False, False, []

        for turn in self.turns_by_id.values():
            turn.dirty = False

        parsed_any = False
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            while True:
                line_start = fh.tell()
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # Incomplete line still being flushed by Codex; retry on next poll.
                    fh.seek(line_start)
                    break
                self.offset = fh.tell()
                stripped = raw.strip()
                if not stripped:
                    continue
                if any(marker in stripped for marker in _SKIP_TYPE_MARKERS):
                    continue
                try:
                    rec = json.loads(stripped.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    self._apply_record(rec)
                    parsed_any = True

        dirty_ids = [tid for tid in self.turn_order if self.turns_by_id[tid].dirty]
        return (parsed_any or was_reset), was_reset, dirty_ids

    def _reset(self) -> None:
        self.cwd = None
        self.git_branch = None
        self.model = None
        self.reasoning_effort = None
        self.originator = None
        self.started_at = None
        self.last_event_at = None
        self.total_tokens = 0
        self.offset = 0
        self.has_structured_items = False
        self.turns_by_id.clear()
        self.turn_order.clear()
        self.active_turn_id = None
        self.spawned_thread_ids.clear()
        self._synthetic_turn_seq = 0

    def _apply_record(self, rec: dict[str, Any]) -> None:
        rec_type = rec.get("type")
        timestamp = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
        if timestamp:
            if not self.started_at:
                self.started_at = timestamp
            self.last_event_at = timestamp

        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return

        if rec_type == "session_meta":
            self._handle_session_meta(payload, timestamp)
        elif rec_type == "turn_context":
            self._handle_turn_context(payload, timestamp)
        elif rec_type == "event_msg":
            self._handle_event_msg(payload, timestamp)
        elif rec_type == "response_item":
            self._handle_response_item(payload, timestamp)

    def _handle_session_meta(self, pl: dict[str, Any], timestamp: str | None) -> None:
        sid = pl.get("session_id") or pl.get("id")
        if isinstance(sid, str) and sid:
            self.session_id = sid
        cwd = pl.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwd = cwd
        originator = pl.get("originator")
        if isinstance(originator, str) and originator:
            self.originator = originator
        meta_ts = pl.get("timestamp")
        if isinstance(meta_ts, str) and meta_ts:
            self.started_at = meta_ts
        elif timestamp and not self.started_at:
            self.started_at = timestamp
        git = pl.get("git")
        if isinstance(git, dict) and isinstance(git.get("branch"), str):
            self.git_branch = git["branch"]

    def _handle_turn_context(self, pl: dict[str, Any], timestamp: str | None) -> None:
        cwd = pl.get("cwd")
        if isinstance(cwd, str) and cwd and not self.cwd:
            self.cwd = cwd
        model = pl.get("model")
        if isinstance(model, str) and model:
            self.model = model
        effort = pl.get("effort")
        if isinstance(effort, str) and effort:
            self.reasoning_effort = effort
        turn_id = pl.get("turn_id") if isinstance(pl.get("turn_id"), str) else None
        if turn_id:
            turn = self._get_or_create_turn(turn_id, timestamp=timestamp)
            if isinstance(model, str) and model:
                turn.model = model
            if isinstance(effort, str) and effort:
                turn.effort = effort
            turn.dirty = True

    def _handle_event_msg(self, pl: dict[str, Any], timestamp: str | None) -> None:
        ev_type = pl.get("type")
        turn_id = pl.get("turn_id") if isinstance(pl.get("turn_id"), str) else None

        if ev_type == "thread_settings_applied":
            settings = pl.get("thread_settings")
            if isinstance(settings, dict):
                if isinstance(settings.get("model"), str) and settings["model"]:
                    self.model = settings["model"]
                if isinstance(settings.get("reasoning_effort"), str) and settings["reasoning_effort"]:
                    self.reasoning_effort = settings["reasoning_effort"]
                if isinstance(settings.get("cwd"), str) and settings["cwd"] and not self.cwd:
                    self.cwd = settings["cwd"]
            return

        if ev_type == "task_started":
            turn = self._get_or_create_turn(turn_id, timestamp=timestamp)
            turn.status = "running"
            started_iso = _epoch_to_iso(pl.get("started_at")) or timestamp
            if started_iso and not turn.started_at:
                turn.started_at = started_iso
            if self.model and not turn.model:
                turn.model = self.model
            turn.dirty = True
            return

        if ev_type == "task_complete":
            turn = self._get_or_create_turn(turn_id, timestamp=timestamp)
            turn.status = "completed"
            turn.completed_at = _epoch_to_iso(pl.get("completed_at")) or timestamp
            if isinstance(pl.get("duration_ms"), (int, float)):
                turn.duration_ms = int(pl["duration_ms"])
            last_msg = pl.get("last_agent_message")
            if isinstance(last_msg, str) and last_msg.strip() and not turn.messages_by_id:
                text, _ = _truncate(last_msg.strip(), MAX_ASSISTANT_TEXT_CHARS)
                blk_id = f"final:{turn.turn_id}"
                turn.messages_by_id[blk_id] = AssistantBlock(
                    id=blk_id, phase="final", text=text, timestamp=turn.completed_at
                )
                turn.message_order.append(blk_id)
            turn.dirty = True
            return

        if ev_type == "turn_aborted":
            turn = self._get_or_create_turn(turn_id, timestamp=timestamp)
            turn.status = "aborted"
            turn.completed_at = timestamp
            reason = pl.get("reason")
            if isinstance(reason, str) and reason:
                turn.error_reason = reason
            turn.dirty = True
            return

        if ev_type == "token_count":
            info = pl.get("info")
            if isinstance(info, dict):
                total = info.get("total_token_usage") or info.get("last_token_usage")
                if isinstance(total, dict):
                    tot_val = int(total.get("total_tokens") or 0)
                    if tot_val > self.total_tokens:
                        self.total_tokens = tot_val
                    if self.active_turn_id and self.active_turn_id in self.turns_by_id:
                        turn = self.turns_by_id[self.active_turn_id]
                        turn.token_usage = TokenUsageSummary(
                            input_tokens=int(total.get("input_tokens") or 0),
                            cached_input_tokens=int(total.get("cached_input_tokens") or 0),
                            output_tokens=int(total.get("output_tokens") or 0),
                            reasoning_output_tokens=int(total.get("reasoning_output_tokens") or 0),
                            total_tokens=tot_val,
                        )
                        turn.dirty = True
            return

        if ev_type == "item_completed":
            item = pl.get("item")
            if not isinstance(item, dict):
                return
            self.has_structured_items = True
            turn = self._get_or_create_turn(turn_id, timestamp=timestamp)
            turn.has_structured_items = True
            self._handle_completed_item(
                turn,
                item,
                started_at_ms=pl.get("started_at_ms"),
                completed_at_ms=pl.get("completed_at_ms"),
                timestamp=timestamp,
            )
            turn.dirty = True

    def _handle_completed_item(
        self,
        turn: _TurnAccumulator,
        item: dict[str, Any],
        *,
        started_at_ms: Any,
        completed_at_ms: Any,
        timestamp: str | None,
    ) -> None:
        itype = item.get("type")
        item_id = str(item.get("id") or f"{itype}:{len(turn.command_order) + len(turn.message_order)}")

        if itype == "UserMessage":
            text = _extract_content_text(item.get("content"))
            if text and not _is_system_boilerplate(text):
                clipped, _ = _truncate(text, MAX_PROMPT_CHARS)
                if not turn.prompt:
                    turn.prompt = clipped
                elif clipped not in turn.prompt:
                    turn.prompt = f"{turn.prompt}\n\n{clipped}"
            return

        if itype == "SubAgentActivity":
            child_tid = item.get("agent_thread_id")
            if isinstance(child_tid, str) and child_tid and child_tid not in self.spawned_thread_ids:
                self.spawned_thread_ids.append(child_tid)
            return

        if itype == "Reasoning":
            summaries = item.get("summary_text")
            if isinstance(summaries, list):
                joined = "\n".join(s.strip() for s in summaries if isinstance(s, str) and s.strip())
                if joined:
                    if item_id not in turn.reasoning_by_id:
                        turn.reasoning_order.append(item_id)
                    turn.reasoning_by_id[item_id] = joined
            return

        if itype == "AgentMessage":
            text = _extract_content_text(item.get("content"))
            if text:
                clipped, _ = _truncate(text, MAX_ASSISTANT_TEXT_CHARS)
                phase = item.get("phase") if isinstance(item.get("phase"), str) else "final"
                if item_id not in turn.messages_by_id:
                    turn.message_order.append(item_id)
                turn.messages_by_id[item_id] = AssistantBlock(
                    id=item_id,
                    phase=phase,
                    text=clipped,
                    timestamp=timestamp,
                )
            return

        if itype == "CommandExecution":
            cmd_str = _format_command(item.get("command"), item.get("parsed_cmd"))
            raw_out = item.get("aggregated_output")
            preview, truncated = _truncate(raw_out if isinstance(raw_out, str) else "", MAX_OUTPUT_PREVIEW_CHARS)
            exit_code = item.get("exit_code")
            record = CommandRecord(
                id=item_id,
                command=cmd_str,
                cwd=item.get("cwd") if isinstance(item.get("cwd"), str) else None,
                status=str(item.get("status") or "completed"),
                exit_code=int(exit_code) if isinstance(exit_code, int) else None,
                duration_ms=_duration_ms_from_obj(item.get("duration"), started_at_ms, completed_at_ms),
                output_preview=preview,
                output_truncated=truncated,
                started_at_ms=int(started_at_ms) if isinstance(started_at_ms, (int, float)) else None,
                completed_at_ms=int(completed_at_ms) if isinstance(completed_at_ms, (int, float)) else None,
            )
            if item_id not in turn.commands_by_id:
                turn.command_order.append(item_id)
            turn.commands_by_id[item_id] = record
            return

        if itype == "FileChange":
            changes = item.get("changes")
            if isinstance(changes, dict):
                for file_path, meta in changes.items():
                    if not isinstance(file_path, str):
                        continue
                    change_type = "update"
                    move_path = None
                    if isinstance(meta, dict):
                        if isinstance(meta.get("type"), str):
                            change_type = meta["type"]
                        if isinstance(meta.get("move_path"), str):
                            move_path = meta["move_path"]
                    turn.changed_files_by_path[file_path] = FileChangeRecord(
                        path=file_path,
                        change_type=change_type,
                        move_path=move_path,
                    )

    def _handle_response_item(self, pl: dict[str, Any], timestamp: str | None) -> None:
        ptype = pl.get("type")
        passthrough = pl.get("internal_chat_message_metadata_passthrough")
        turn_id = (
            passthrough.get("turn_id")
            if isinstance(passthrough, dict) and isinstance(passthrough.get("turn_id"), str)
            else None
        )
        turn = self._get_or_create_turn(turn_id, timestamp=timestamp)

        if ptype == "message":
            role = pl.get("role")
            text = _extract_content_text(pl.get("content"))
            if not text:
                return
            if role == "user":
                if not turn.fallback_prompt and not _is_system_boilerplate(text):
                    turn.fallback_prompt, _ = _truncate(text, MAX_PROMPT_CHARS)
                    turn.dirty = True
            elif role == "assistant":
                msg_id = str(pl.get("id") or f"resp:{len(turn.fallback_message_order)}")
                clipped, _ = _truncate(text, MAX_ASSISTANT_TEXT_CHARS)
                phase = pl.get("phase") if isinstance(pl.get("phase"), str) else "final"
                if msg_id not in turn.fallback_messages_by_id:
                    turn.fallback_message_order.append(msg_id)
                turn.fallback_messages_by_id[msg_id] = AssistantBlock(
                    id=msg_id, phase=phase, text=clipped, timestamp=timestamp
                )
                turn.dirty = True
            return

        if ptype == "reasoning":
            summary = pl.get("summary")
            if isinstance(summary, list):
                parts = [
                    x.get("text", "").strip()
                    for x in summary
                    if isinstance(x, dict) and isinstance(x.get("text"), str) and x.get("text", "").strip()
                ]
                if parts:
                    rid = str(pl.get("id") or f"reas:{len(turn.fallback_reasoning_order)}")
                    if rid not in turn.fallback_reasoning_by_id:
                        turn.fallback_reasoning_order.append(rid)
                    turn.fallback_reasoning_by_id[rid] = "\n".join(parts)
                    turn.dirty = True
            return

        if ptype in ("custom_tool_call", "function_call"):
            name = pl.get("name")
            if name not in _EXEC_TOOL_NAMES:
                return
            call_id = str(pl.get("call_id") or pl.get("id") or f"cmd:{len(turn.fallback_command_order)}")
            raw_in = pl.get("input") if ptype == "custom_tool_call" else pl.get("arguments")
            cmd_str = self._parse_fallback_exec_input(raw_in)
            if call_id not in turn.fallback_commands_by_id:
                turn.fallback_command_order.append(call_id)
            turn.fallback_commands_by_id[call_id] = CommandRecord(
                id=call_id,
                command=cmd_str,
                status=str(pl.get("status") or "running"),
            )
            turn.dirty = True
            return

        if ptype in ("custom_tool_call_output", "function_call_output"):
            call_id = pl.get("call_id")
            if not isinstance(call_id, str) or call_id not in turn.fallback_commands_by_id:
                return
            raw_out = _extract_content_text(pl.get("output"))
            exit_code = None
            duration_ms = None
            m_exit = _EXIT_CODE_RE.search(raw_out)
            if m_exit:
                exit_code = int(m_exit.group(1))
            m_wall = _WALL_TIME_RE.search(raw_out)
            if m_wall:
                duration_ms = int(float(m_wall.group(1)) * 1000)
            preview, truncated = _truncate(raw_out, MAX_OUTPUT_PREVIEW_CHARS)
            existing = turn.fallback_commands_by_id[call_id]
            turn.fallback_commands_by_id[call_id] = existing.model_copy(
                update={
                    "status": "completed" if (exit_code in (None, 0)) else "failed",
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "output_preview": preview,
                    "output_truncated": truncated,
                }
            )
            turn.dirty = True

    @staticmethod
    def _parse_fallback_exec_input(raw_in: Any) -> str:
        if isinstance(raw_in, str):
            text = raw_in.strip()
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        for key in ("cmd", "command", "script"):
                            val = parsed.get(key)
                            if val:
                                return _format_command(val)
                except json.JSONDecodeError:
                    pass
            first_line = text.splitlines()[0] if text else "(command)"
            return first_line[:240]
        return _format_command(raw_in)

    def _is_stale_running(self, now_ts: float | None = None) -> bool:
        if self.file_mtime_ns <= 0:
            return False
        now = time.time() if now_ts is None else now_ts
        age = now - (self.file_mtime_ns / 1e9)
        return age > STALE_RUNNING_SECONDS

    def build_turns(
        self,
        *,
        only_ids: set[str] | None = None,
        now_ts: float | None = None,
    ) -> list[TurnDetail]:
        stale_idle = self._is_stale_running(now_ts)
        use_fallback = not self.has_structured_items
        result: list[TurnDetail] = []
        for tid in self.turn_order:
            if only_ids is not None and tid not in only_ids:
                continue
            result.append(
                self.turns_by_id[tid].to_model(use_fallback=use_fallback, stale_idle=stale_idle)
            )
        return result

    def build_summary(
        self,
        *,
        indexed_title: str | None = None,
        now_ts: float | None = None,
    ) -> SessionSummary:
        turns = self.build_turns(now_ts=now_ts)
        project_dir, project_name = _normalize_project_dir(self.cwd)

        status: SessionStatus = "idle"
        if turns:
            latest = turns[-1]
            status = latest.status

        total_cmds = sum(t.command_count for t in turns)
        unique_files: dict[str, FileChangeRecord] = {}
        for t in turns:
            for fc in t.changed_files:
                unique_files[fc.path] = fc

        title = (indexed_title or "").strip()
        if not title:
            for t in turns:
                if t.prompt.strip():
                    first_line = t.prompt.strip().splitlines()[0].strip()
                    title = first_line[:88] + ("..." if len(first_line) > 88 else "")
                    break
        if not title:
            title = f"Session {self.session_id[:8]}"

        latest_duration = turns[-1].duration_ms if turns else None
        total_duration = sum(t.duration_ms for t in turns if t.duration_ms) or latest_duration
        updated_at = self.last_event_at or _mtime_ns_to_iso(self.file_mtime_ns)
        inferred_delegated = title.lower().startswith("agy:")

        return SessionSummary(
            session_id=self.session_id,
            title=title,
            project_dir=project_dir,
            project_name=project_name,
            git_branch=self.git_branch,
            model=self.model or (turns[-1].model if turns else None),
            reasoning_effort=self.reasoning_effort or (turns[-1].effort if turns else None),
            originator=self.originator,
            status=status,
            round_count=len(turns),
            command_count=total_cmds,
            changed_file_count=len(unique_files),
            total_tokens=self.total_tokens,
            started_at=self.started_at,
            updated_at=updated_at,
            duration_ms=total_duration,
            file_size=self.file_size,
            is_delegated=inferred_delegated,
            source_model="Gemini" if inferred_delegated else None,
        )

    def build_detail(
        self,
        *,
        indexed_title: str | None = None,
        now_ts: float | None = None,
    ) -> SessionDetail:
        turns = self.build_turns(now_ts=now_ts)
        summary = self.build_summary(indexed_title=indexed_title, now_ts=now_ts)
        unique_files: dict[str, FileChangeRecord] = {}
        for t in turns:
            for fc in t.changed_files:
                unique_files[fc.path] = fc

        return SessionDetail(
            summary=summary,
            turns=turns,
            cursor=self.offset,
            file_mtime_ns=self.file_mtime_ns,
            file_size=self.file_size,
            total_commands=sum(t.command_count for t in turns),
            total_changed_files=len(unique_files),
            all_changed_files=list(unique_files.values()),
        )
