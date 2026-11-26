export type SessionStatus = "running" | "completed" | "aborted" | "idle";
export type IsolationMode = "read-only" | "worktree" | "workspace-write";

export interface CommandRecord {
  id: string;
  command: string;
  cwd?: string | null;
  status: string;
  exit_code?: number | null;
  duration_ms?: number | null;
  output_preview: string;
  output_truncated: boolean;
  started_at_ms?: number | null;
  completed_at_ms?: number | null;
}

export interface FileChangeRecord {
  path: string;
  change_type: string;
  move_path?: string | null;
}

export interface AssistantBlock {
  id: string;
  phase: string;
  text: string;
  timestamp?: string | null;
}

export interface TokenUsageSummary {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}

export interface TurnDetail {
  round_index: number;
  turn_id: string;
  status: SessionStatus;
  model?: string | null;
  effort?: string | null;
  is_delegated: boolean;
  source_model?: string | null;
  delegation_round?: number | null;
  isolation_mode?: IsolationMode | null;
  worktree_path?: string | null;
  prompt: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  reasoning_summaries: string[];
  assistant_messages: AssistantBlock[];
  commands: CommandRecord[];
  changed_files: FileChangeRecord[];
  command_count: number;
  changed_file_count: number;
  error_reason?: string | null;
  token_usage?: TokenUsageSummary | null;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  project_dir: string;
  project_name: string;
  git_branch?: string | null;
  model?: string | null;
  originator?: string | null;
  status: SessionStatus;
  round_count: number;
  command_count: number;
  changed_file_count: number;
  started_at?: string | null;
  updated_at?: string | null;
  duration_ms?: number | null;
  file_size: number;
  is_delegated: boolean;
  source_model?: string | null;
  isolation_mode?: IsolationMode | null;
  worktree_path?: string | null;
  active_run_id?: string | null;
}

export interface SessionDetail {
  summary: SessionSummary;
  turns: TurnDetail[];
  cursor: number;
  file_mtime_ns: number;
  file_size: number;
  total_commands: number;
  total_changed_files: number;
  all_changed_files: FileChangeRecord[];
}

export interface SessionDelta {
  session_id: string;
  changed: boolean;
  reset: boolean;
  cursor: number;
  file_size: number;
  summary?: SessionSummary | null;
  updated_turns: TurnDetail[];
  total_commands: number;
  total_changed_files: number;
  all_changed_files: FileChangeRecord[];
}

export interface ListSessionsResponse {
  sessions: SessionSummary[];
  sessions_dir: string;
  sessions_dir_exists: boolean;
}

export interface OpenOrbitBootstrap {
  initial_session_id?: string | null;
  initial_query?: string | null;
  sessions_dir?: string;
}
