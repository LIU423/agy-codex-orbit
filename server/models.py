"""Typed data models for Codex Orbit session monitoring and delegation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SessionStatus = Literal["running", "completed", "aborted", "idle"]
TurnStatus = Literal["running", "completed", "aborted", "idle"]
IsolationMode = Literal["read-only", "worktree", "workspace-write"]
DelegationStatus = Literal["running", "completed", "failed", "cancelled"]


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


class DelegationRoundRecord(BaseModel):
    round_index: int
    run_id: str
    source_model: str = "Gemini"
    title: str | None = None
    prompt: str = ""
    isolation_mode: IsolationMode = "workspace-write"
    workspace_dir: str = ""
    worktree_path: str | None = None
    worktree_branch: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    status: DelegationStatus = "running"
    pid: int | None = None
    child_pid: int | None = None
    started_at: str
    ended_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    turn_id: str | None = None
    command_count: int = 0
    changed_files: list[str] = Field(default_factory=list)
    final_message: str = ""
    error_summary: str | None = None


class DelegationSessionRecord(BaseModel):
    record_id: str
    session_id: str | None = None
    title: str | None = None
    workspace_dir: str
    worktree_path: str | None = None
    worktree_branch: str | None = None
    isolation_mode: IsolationMode = "workspace-write"
    rounds: list[DelegationRoundRecord] = Field(default_factory=list)
    updated_at: str


class TurnDetail(BaseModel):
    round_index: int
    turn_id: str
    status: TurnStatus = "completed"
    model: str | None = None
    effort: str | None = None
    is_delegated: bool = False
    source_model: str | None = None
    delegation_round: int | None = None
    isolation_mode: IsolationMode | None = None
    worktree_path: str | None = None
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
    reasoning_effort: str | None = None
    originator: str | None = None
    status: SessionStatus = "completed"
    round_count: int = 0
    command_count: int = 0
    changed_file_count: int = 0
    total_tokens: int = 0
    started_at: str | None = None
    updated_at: str | None = None
    duration_ms: int | None = None
    file_size: int = 0
    is_delegated: bool = False
    source_model: str | None = None
    isolation_mode: IsolationMode | None = None
    worktree_path: str | None = None
    active_run_id: str | None = None


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
