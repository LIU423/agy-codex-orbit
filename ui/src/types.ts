export type SessionStatus = "running" | "completed" | "aborted" | "idle";
export type IsolationMode = "read-only" | "worktree" | "workspace-write";
export type RoutingMode = "silent_low_cost" | "ask" | "silent_best";
export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max" | "ultra";
export type ReasoningEffortSetting = "auto" | ReasoningEffort;
export type UiLocale = "auto" | "zh-CN" | "zh-TW" | "en" | "ja";
export type ResolvedLocale = "zh-CN" | "zh-TW" | "en" | "ja";
export type OriginFilter = "all" | "agy" | "direct";
export type HeatmapMetric = "tokens" | "sessions";

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
  error_code?: string | null;
  token_usage?: TokenUsageSummary | null;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  project_dir: string;
  project_name: string;
  git_branch?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  originator?: string | null;
  status: SessionStatus;
  round_count: number;
  command_count: number;
  changed_file_count: number;
  total_tokens: number;
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

export interface OverviewSessionRow extends SessionSummary {
  date?: string | null;
  origin: "agy" | "direct";
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

export interface ProjectEntry {
  project_dir: string;
  project_name: string;
  created_at?: string;
}

export interface ListSessionsResponse {
  sessions: SessionSummary[];
  projects?: ProjectEntry[];
  sessions_dir: string;
  sessions_dir_exists: boolean;
  routing_mode?: RoutingMode;
  ui_locale?: UiLocale;
  default_model?: string;
  escalation_model?: string;
  quota_guardrail_enabled?: boolean;
  quota_guardrail_threshold_pct?: number;
  reasoning_effort?: ReasoningEffortSetting;
}

export interface ModelCatalogEntry {
  slug: string;
  display_name: string;
  description: string;
  efforts: string[];
  default_effort?: string | null;
}

export interface OrbitSettingsResponse {
  routing_mode: RoutingMode;
  ui_locale: UiLocale;
  default_model: string;
  escalation_model: string;
  quota_guardrail_enabled: boolean;
  quota_guardrail_threshold_pct: number;
  reasoning_effort: ReasoningEffortSetting;
  available_models: ModelCatalogEntry[];
}

export interface OrbitBootstrapResponse {
  sessions: SessionSummary[];
  projects?: ProjectEntry[];
  sessions_dir: string;
  sessions_dir_exists: boolean;
  settings: OrbitSettingsResponse;
  quota: QuotaStatus | null;
  quota_stale: boolean;
  overview: ProfileOverviewResponse | null;
}

export interface DoctorCheckItem {
  id: string;
  status: "ok" | "warning" | "error" | "skipped";
  summary: string;
  detail: string;
}

export interface DoctorReport {
  ok: boolean;
  platform: string;
  checks: DoctorCheckItem[];
  settings: Omit<OrbitSettingsResponse, "available_models">;
  available_models: ModelCatalogEntry[];
}

export interface PruneWorktreesResult {
  removed: string[];
  skipped_running: string[];
  errors: string[];
}

export interface SendFollowupResult {
  started: boolean;
  session_id?: string;
  run_id?: string;
  busy?: boolean;
  error?: string;
}

export interface CreateProjectResult {
  created: boolean;
  project_dir?: string;
  project_name?: string;
  error?: string;
}

export interface CreateSessionResult {
  started: boolean;
  run_id?: string;
  workspace_dir?: string;
  title?: string;
  error?: string;
}

export interface OpenOrbitBootstrap {
  initial_session_id?: string | null;
  initial_query?: string | null;
  focus_running?: boolean;
  preferred_workspace?: string | null;
  sessions_dir?: string;
}

export interface CancelDelegationResult {
  cancelled: boolean;
  already_finished: boolean;
  session_id?: string | null;
  run_id?: string | null;
  terminated_pids: number[];
  message: string;
}

export interface QuotaWindow {
  used_percent: number;
  remaining_percent: number;
  window_minutes?: number | null;
  resets_at?: number | null;
  resets_at_local?: string | null;
  resets_in?: string | null;
}

export interface QuotaStatus {
  codex: {
    available: boolean;
    source?: string;
    plan_type?: string | null;
    primary?: QuotaWindow | null;
    secondary?: QuotaWindow | null;
    reset_credits_available?: number | null;
    error?: string | null;
  };
  antigravity: {
    available: boolean;
    note?: string;
  };
}

export interface ProjectStatEntry {
  project_dir: string;
  project_name: string;
  session_count: number;
  running_count: number;
  agy_count: number;
  direct_count: number;
}

export interface HeatmapDayEntry {
  date: string;
  tokens: number;
  agy_tokens: number;
  direct_tokens: number;
  sessions: number;
  agy_sessions: number;
  direct_sessions: number;
  rounds: number;
  agy_rounds: number;
  direct_rounds: number;
}

export interface TopStatEntry {
  name: string;
  count: number;
  share_pct: number;
}

export interface ModelBreakdownEntry {
  name: string;
  count: number;
  share_pct: number;
  tokens: number;
}

export interface HotFileEntry {
  path: string;
  short_name: string;
  session_count: number;
}

export interface ProfileOverviewResponse {
  project_filter: string;
  today: string;
  projects: ProjectStatEntry[];
  kpis: {
    total_tokens: number;
    agy_tokens: number;
    direct_tokens: number;
    peak_day_tokens: number;
    peak_day_sessions: number;
    longest_duration_ms: number;
    longest_streak_days: number;
    current_streak_days: number;
    total_sessions: number;
    agy_sessions: number;
    direct_sessions: number;
    agy_share_pct: number;
    active_sessions: number;
    total_projects: number;
    total_commands: number;
  };
  heatmap_days: HeatmapDayEntry[];
  insights: {
    top_model?: TopStatEntry | null;
    model_breakdown?: ModelBreakdownEntry[];
    top_effort?: TopStatEntry | null;
    routing_mode: RoutingMode;
    active_projects: number;
    total_commands: number;
    unique_changed_files: number;
    worktree_count?: number;
  };
  recent_sessions: OverviewSessionRow[];
  hot_files: HotFileEntry[];
}
