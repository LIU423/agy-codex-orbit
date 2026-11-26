"""Typed data models for Codex Orbit session monitoring."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SessionStatus = Literal["running", "completed", "aborted", "idle"]
TurnStatus = Literal["running", "completed", "aborted", "idle"]


class CommandRecord(BaseModel):
    id: str
    command: str
    cwd: str | None = None
    status: str = "completed"
    exit_code: int | None = None
    duration_ms: int | None = None
    output_preview: str = ""
    output_truncated: bool = False
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


class FileChangeRecord(BaseModel):
    path: str
    change_type: str = "update"
    move_path: str | None = None


class AssistantBlock(BaseModel):
    id: str
    phase: str = "final"
    text: str
    timestamp: str | None = None


class TokenUsageSummary(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0


class TurnDetail(BaseModel):
    round_index: int
    turn_id: str
    status: TurnStatus = "completed"
    model: str | None = None
    effort: str | None = None
    is_delegated: bool = False
    source_model: str | None = None
    prompt: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    reasoning_summaries: list[str] = Field(default_factory=list)
    assistant_messages: list[AssistantBlock] = Field(default_factory=list)
    commands: list[CommandRecord] = Field(default_factory=list)
    changed_files: list[FileChangeRecord] = Field(default_factory=list)
    command_count: int = 0
    changed_file_count: int = 0
    error_reason: str | None = None
    token_usage: TokenUsageSummary | None = None


class SessionSummary(BaseModel):
    session_id: str
    title: str
    project_dir: str
    project_name: str
    git_branch: str | None = None
    model: str | None = None
    originator: str | None = None
    status: SessionStatus = "completed"
    round_count: int = 0
    command_count: int = 0
    changed_file_count: int = 0
    started_at: str | None = None
    updated_at: str | None = None
    duration_ms: int | None = None
    file_size: int = 0
    is_delegated: bool = False
    source_model: str | None = None


class SessionDetail(BaseModel):
    summary: SessionSummary
    turns: list[TurnDetail] = Field(default_factory=list)
    cursor: int = 0
    file_mtime_ns: int = 0
    file_size: int = 0
    total_commands: int = 0
    total_changed_files: int = 0
    all_changed_files: list[FileChangeRecord] = Field(default_factory=list)


class SessionDelta(BaseModel):
    session_id: str
    changed: bool
    reset: bool = False
    cursor: int = 0
    file_size: int = 0
    summary: SessionSummary | None = None
    updated_turns: list[TurnDetail] = Field(default_factory=list)
    total_commands: int = 0
    total_changed_files: int = 0
    all_changed_files: list[FileChangeRecord] = Field(default_factory=list)
