import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";
import { z } from "zod";
import { OrbitApi } from "./api";
import { renderActivityHeatmap } from "./heatmap";
import { escapeHtml, renderSafeMarkdown } from "./markdown";
import {
  effortLabel,
  effortShortLabel,
  formatCompactNumber,
  formatDays,
  formatRelativeTime,
  getStrings,
  isolationLabel,
  resolveLocale,
  routingModeLabel,
  statusLabel,
} from "./i18n";
import "./styles.css";
import type {
  AssistantBlock,
  CommandRecord,
  DoctorReport,
  FileChangeRecord,
  HeatmapMetric,
  IsolationMode,
  ModelCatalogEntry,
  OpenOrbitBootstrap,
  OriginFilter,
  ProfileOverviewResponse,
  ProjectEntry,
  QuotaStatus,
  ReasoningEffort,
  ReasoningEffortSetting,
  ResolvedLocale,
  RoutingMode,
  SessionDetail,
  SessionSummary,
  TurnDetail,
  UiLocale,
} from "./types";

interface UiState {
  sessions: SessionSummary[];
  projects: ProjectEntry[];
  sessionsDir: string;
  sessionsDirExists: boolean;
  query: string;
  selectedSessionId: string | null;
  detail: SessionDetail | null;
  detailMetaTs: string;
  overview: ProfileOverviewResponse | null;
  overviewProjectFilter: string;
  originFilter: OriginFilter;
  heatmapMetric: HeatmapMetric;
  selectedHeatmapDate: string | null;
  loadingOverview: boolean;
  autoFollow: boolean;
  showFilesDrawer: boolean;
  sidebarOpen: boolean;
  expandedGroups: Set<string>;
  expandedCommandTurns: Set<string>;
  focusRunningPending: boolean;
  focusRunningTicks: number;
  preferredWorkspace: string | null;
  seenRunningIds: Set<string>;
  syncState: "connecting" | "live" | "error";
  syncError: string | null;
  loadingDetail: boolean;
  cancellingRunId: string | null;
  canSendMessage: boolean;
  canUpdateContext: boolean;
  actionToast: string | null;
  quota: QuotaStatus | null;
  routingMode: RoutingMode;
  uiLocale: UiLocale;
  hostLocale: string | null;
  defaultModel: string;
  escalationModel: string;
  reasoningEffort: ReasoningEffortSetting;
  quotaGuardrailEnabled: boolean;
  quotaGuardrailThresholdPct: number;
  availableModels: ModelCatalogEntry[];
  settingsOpen: boolean;
  doctorReport: DoctorReport | null;
  runningDoctor: boolean;
  pruningWorktrees: boolean;
  followupDraft: string;
  followupWebSearch: boolean;
  followupEffort: ReasoningEffortSetting;
  sendingFollowup: boolean;
  newProjectOpen: boolean;
  newProjectDir: string;
  newProjectCreateMissing: boolean;
  newProjectSubmitting: boolean;
  newProjectError: string | null;
  newChatProject: { projectDir: string; projectName: string } | null;
  newChatTitle: string;
  newChatPrompt: string;
  newChatMode: IsolationMode;
  newChatEffort: ReasoningEffortSetting;
  newChatWebSearch: boolean;
  newChatSubmitting: boolean;
  newChatError: string | null;
}

const state: UiState = {
  sessions: [],
  projects: [],
  sessionsDir: "",
  sessionsDirExists: true,
  query: "",
  selectedSessionId: null,
  detail: null,
  detailMetaTs: "",
  overview: null,
  overviewProjectFilter: "",
  originFilter: "all",
  heatmapMetric: "tokens",
  selectedHeatmapDate: null,
  loadingOverview: false,
  autoFollow: true,
  showFilesDrawer: false,
  sidebarOpen: false,
  expandedGroups: new Set<string>(),
  expandedCommandTurns: new Set<string>(),
  focusRunningPending: false,
  focusRunningTicks: 0,
  preferredWorkspace: null,
  seenRunningIds: new Set<string>(),
  syncState: "connecting",
  syncError: null,
  loadingDetail: false,
  cancellingRunId: null,
  canSendMessage: false,
  canUpdateContext: false,
  actionToast: null,
  quota: null,
  routingMode: "ask",
  uiLocale: "auto",
  hostLocale: null,
  defaultModel: "gpt-6.2-sol",
  escalationModel: "gpt-6.2-astra",
  reasoningEffort: "auto",
  quotaGuardrailEnabled: true,
  quotaGuardrailThresholdPct: 15,
  availableModels: [],
  settingsOpen: false,
  doctorReport: null,
  runningDoctor: false,
  pruningWorktrees: false,
  followupDraft: "",
  followupWebSearch: false,
  followupEffort: "auto",
  sendingFollowup: false,
  newProjectOpen: false,
  newProjectDir: "",
  newProjectCreateMissing: true,
  newProjectSubmitting: false,
  newProjectError: null,
  newChatProject: null,
  newChatTitle: "",
  newChatPrompt: "",
  newChatMode: "workspace-write",
  newChatEffort: "auto",
  newChatWebSearch: false,
  newChatSubmitting: false,
  newChatError: null,
};

const toggleAuxPaneResultSchema = z.object({ isError: z.boolean().optional() }).passthrough();

let appInstance: App | null = null;
let api: OrbitApi | null = null;
let pollTimer: number | null = null;
let pollInFlight = false;
let pollTick = 0;
let searchDebounce: number | null = null;
let toastTimer: number | null = null;
let listDirty = true;
let detailDirty = true;
let hmScrollInitialized = false;

function activeLocale(): ResolvedLocale {
  return resolveLocale(state.uiLocale, state.hostLocale);
}

function isAuxPaneSurface(): boolean {
  return state.canSendMessage || state.canUpdateContext;
}

function normalizePathForMatch(p: string | null | undefined): string {
  if (!p) return "";
  return p.trim().replace(/[\\/]+/g, "/").replace(/\/+$/, "").toLowerCase();
}

function findBestRunningSession(
  sessions: SessionSummary[],
  preferredWorkspace?: string | null,
): SessionSummary | null {
  const running = sessions.filter((s) => s.status === "running");
  if (running.length === 0) return null;
  const target = normalizePathForMatch(preferredWorkspace);
  let candidates = running;
  if (target) {
    const matched = running.filter(
      (s) =>
        normalizePathForMatch(s.project_dir) === target ||
        normalizePathForMatch(s.worktree_path) === target,
    );
    if (matched.length > 0) candidates = matched;
  }
  const sorted = [...candidates].sort((a, b) => {
    const da = a.is_delegated ? 0 : 1;
    const db = b.is_delegated ? 0 : 1;
    if (da !== db) return da - db;
    return (b.updated_at || "").localeCompare(a.updated_at || "");
  });
  return sorted[0] ?? null;
}

function applyLocaleToDocument(): void {
  const loc = activeLocale();
  document.documentElement.lang = loc;
  document.documentElement.dataset.lang = loc;
  updateSidebarChrome();
}

function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined || ms <= 0) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) {
    const rem = Math.round(secs % 60);
    return `${mins}m ${rem}s`;
  }
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hrs}h ${remMins}m`;
}

function shortFileName(filePath: string): string {
  const parts = filePath.replace(/\\/g, "/").split("/");
  if (parts.length <= 2) return filePath;
  return parts.slice(-2).join("/");
}

function fileOpLabel(changeType: string): { text: string; cls: string } {
  const low = changeType.toLowerCase();
  if (low.includes("add") || low.includes("create")) return { text: "A", cls: "add" };
  if (low.includes("del") || low.includes("rem")) return { text: "D", cls: "delete" };
  return { text: "M", cls: "update" };
}

function toFileUri(filePath: string, baseDir?: string | null): string | null {
  let target = filePath.trim();
  if (!target) return null;
  const isWinAbs = /^[A-Za-z]:[\\/]/.test(target);
  const isPosixAbs = target.startsWith("/");
  if (!isWinAbs && !isPosixAbs && baseDir && !baseDir.startsWith("(")) {
    const cleanBase = baseDir.replace(/[\\/]+$/, "");
    target = `${cleanBase}/${target}`;
  }
  const normalized = target.replace(/\\/g, "/");
  if (/^[A-Za-z]:\//.test(normalized)) {
    const encoded = normalized
      .split("/")
      .map((seg, i) => (i === 0 ? seg : encodeURIComponent(seg)))
      .join("/");
    return `file:///${encoded}`;
  }
  if (normalized.startsWith("/")) {
    const encoded = normalized
      .split("/")
      .map((seg) => encodeURIComponent(seg))
      .join("/");
    return `file://${encoded}`;
  }
  return null;
}

function buildCompactSummary(detail: SessionDetail, turn?: TurnDetail): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const s = detail.summary;
  const targetTurn = turn ?? detail.turns[detail.turns.length - 1];
  const lines: string[] = [];

  if (turn) {
    lines.push(str.summaryRoundHeading(turn.round_index));
  } else {
    lines.push(str.summarySessionHeading(s.title));
  }

  lines.push(`- **Session ID**: \`${s.session_id}\``);
  lines.push(`- **${str.summaryWorkspace}**: \`${s.worktree_path || s.project_dir}\``);
  if (s.isolation_mode) {
    lines.push(`- **${str.summaryIsolation}**: ${isolationLabel(s.isolation_mode, loc)}`);
  }
  const model = targetTurn?.model || s.model;
  if (model) {
    lines.push(`- **Model**: \`${model}\``);
  }
  const effort = targetTurn?.effort || s.reasoning_effort;
  if (effort) {
    lines.push(`- **${str.summaryEffort}**: ${effortLabel(effort, loc)}`);
  }
  lines.push(`- **${str.summaryStatus}**: ${statusLabel(targetTurn ? targetTurn.status : s.status, loc)}`);

  const cmdCount = turn ? turn.command_count : detail.total_commands;
  const files = turn ? turn.changed_files : detail.all_changed_files;
  const durationMs = turn
    ? turn.duration_ms
    : detail.turns.reduce((acc, t) => acc + (t.duration_ms || 0), 0) || s.duration_ms;

  lines.push(`- ${str.summaryStats(cmdCount, files.length, formatDuration(durationMs))}`);

  if (files.length > 0) {
    const list = files
      .slice(0, 25)
      .map((f) => `  - [${fileOpLabel(f.change_type).text}] \`${f.path}\``)
      .join("\n");
    lines.push(
      `- **${str.summaryFiles}**:\n${list}${files.length > 25 ? `\n  - ${str.summaryMoreFiles(files.length - 25)}` : ""}`,
    );
  }

  if (targetTurn) {
    const finalBlocks = targetTurn.assistant_messages.filter((m) => m.phase !== "commentary");
    const lastBlock =
      finalBlocks[finalBlocks.length - 1] ??
      targetTurn.assistant_messages[targetTurn.assistant_messages.length - 1];
    if (lastBlock?.text) {
      const clipped =
        lastBlock.text.length > 1000
          ? `${lastBlock.text.slice(0, 1000)}... [truncated]`
          : lastBlock.text;
      lines.push(`\n**${str.summaryConclusion}**:\n${clipped}`);
    } else if (targetTurn.error_reason) {
      lines.push(`\n**${str.summaryAborted}**: ${targetTurn.error_reason}`);
    }
  }

  return lines.join("\n");
}

function showToast(msg: string): void {
  state.actionToast = msg;
  detailDirty = true;
  render();
  if (toastTimer !== null) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    state.actionToast = null;
    detailDirty = true;
    render();
  }, 2400);
}

function groupByProject(
  sessions: SessionSummary[],
  projects: ProjectEntry[] = [],
): Array<{
  projectDir: string;
  projectName: string;
  items: SessionSummary[];
  hasRunning: boolean;
}> {
  const map = new Map<
    string,
    { projectDir: string; projectName: string; items: SessionSummary[]; hasRunning: boolean }
  >();
  for (const s of sessions) {
    const key = s.project_dir || "(Unknown Workspace)";
    let entry = map.get(key);
    if (!entry) {
      entry = {
        projectDir: key,
        projectName: s.project_name || key,
        items: [],
        hasRunning: false,
      };
      map.set(key, entry);
    }
    entry.items.push(s);
    if (s.status === "running") entry.hasRunning = true;
  }
  for (const p of projects) {
    const key = p.project_dir;
    if (!key) continue;
    const exists = Array.from(map.keys()).some(
      (k) => k.toLowerCase() === key.toLowerCase(),
    );
    if (!exists) {
      map.set(key, {
        projectDir: key,
        projectName: p.project_name || key,
        items: [],
        hasRunning: false,
      });
    }
  }
  return Array.from(map.values());
}

function renderSidebarList(): string {
  const loc = activeLocale();
  const str = getStrings(loc);

  if (!state.sessionsDirExists && state.projects.length === 0) {
    return `<div class="empty-state"><strong>${escapeHtml(str.noSessionsDir)}</strong><span>${escapeHtml(state.sessionsDir)}</span></div>`;
  }
  if (state.sessions.length === 0 && state.projects.length === 0) {
    return `<div class="empty-state"><strong>${escapeHtml(state.query ? str.noMatchingSessions : str.noLocalSessions)}</strong></div>`;
  }

  const isSearching = state.query.trim().length > 0;
  const groups = groupByProject(state.sessions, state.projects);

  return groups
    .map((g) => {
      const expanded = isSearching || state.expandedGroups.has(g.projectDir);
      const itemsHtml = !expanded
        ? ""
        : g.items.length === 0
          ? `<div class="group-empty">${escapeHtml(str.emptyProjectHint)}</div>`
          : `<div class="group-items">${g.items
              .map((s) => {
                const active = s.session_id === state.selectedSessionId ? " active" : "";
                const delegatedChip = s.is_delegated
                  ? `<span class="meta-chip delegated" title="${escapeHtml(str.delegatedByTip(s.source_model || "Gemini"))}">AGY</span>`
                  : "";
                const modelChip = s.model
                  ? `<span class="meta-chip" title="${escapeHtml(s.model)}">${escapeHtml(s.model)}</span>`
                  : "";
                const effortChip = s.reasoning_effort
                  ? `<span class="meta-chip effort-chip" title="${escapeHtml(`${str.reasoningEffortAria}: ${effortLabel(s.reasoning_effort, loc)}`)}">${escapeHtml(effortShortLabel(s.reasoning_effort, loc))}</span>`
                  : "";
                const timeText = formatRelativeTime(s.updated_at, loc);
                return `
                  <button class="session-item${active}" data-session-id="${escapeHtml(s.session_id)}" type="button">
                    <div class="session-row-top">
                      <span class="dot ${s.status}" aria-label="${escapeHtml(statusLabel(s.status, loc))}"></span>
                      <span class="session-title">${escapeHtml(s.title)}</span>
                    </div>
                    <div class="session-meta">
                      ${delegatedChip}
                      ${modelChip}
                      ${effortChip}
                      <span>${escapeHtml(str.roundsShort(s.round_count))}</span>
                      <span class="meta-time">${escapeHtml(timeText)}</span>
                    </div>
                  </button>`;
              })
              .join("")}</div>`;

      const runningDot = g.hasRunning ? `<span class="dot dot-inline running"></span>` : "";
      const canCreateChat = Boolean(g.projectDir && g.projectDir !== "(Unknown Workspace)");
      const newChatBtn = canCreateChat
        ? `<button class="group-new-chat-btn" data-new-chat-dir="${escapeHtml(g.projectDir)}" data-new-chat-name="${escapeHtml(g.projectName)}" title="${escapeHtml(str.newChatInProject(g.projectName))}" aria-label="${escapeHtml(str.newChatInProject(g.projectName))}" type="button">+</button>`
        : "";

      return `
        <div class="group-block">
          <div class="group-header-row">
            <button class="group-header" data-group-dir="${escapeHtml(g.projectDir)}" title="${escapeHtml(g.projectDir)}" type="button" aria-expanded="${expanded}">
              <span class="group-chevron">${expanded ? "▾" : "▸"}</span>
              <span class="group-name">${escapeHtml(g.projectName)}</span>
              ${runningDot}
              <span class="group-count">${g.items.length}</span>
            </button>
            ${newChatBtn}
          </div>
          ${itemsHtml}
        </div>`;
    })
    .join("");
}

function renderRoutingSelect(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const modes: RoutingMode[] = ["silent_low_cost", "ask", "silent_best"];
  const optionsHtml = modes
    .map(
      (m) =>
        `<option value="${m}"${state.routingMode === m ? " selected" : ""}>${escapeHtml(routingModeLabel(m, loc))}</option>`,
    )
    .join("");
  return `<select id="sel-routing-mode" class="ctrl-select" aria-label="${escapeHtml(str.routingPolicyAria)}" title="${escapeHtml(str.routingPolicyTip)}">${optionsHtml}</select>`;
}

function renderLocaleSelect(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const opts: Array<{ value: UiLocale; label: string }> = [
    { value: "auto", label: str.languageAuto },
    { value: "zh-CN", label: "简体中文" },
    { value: "zh-TW", label: "繁體中文" },
    { value: "en", label: "EN" },
    { value: "ja", label: "日本語" },
  ];
  const optionsHtml = opts
    .map(
      (o) =>
        `<option value="${o.value}"${state.uiLocale === o.value ? " selected" : ""}>${escapeHtml(o.label)}</option>`,
    )
    .join("");
  return `<select id="sel-ui-locale" class="ctrl-select" aria-label="${escapeHtml(str.languageAria)}" title="${escapeHtml(str.languageAria)}">${optionsHtml}</select>`;
}

function renderModelOptions(selectedSlug: string): string {
  const seen = new Set<string>();
  const items: Array<{ slug: string; label: string }> = [];
  for (const m of state.availableModels) {
    if (!m.slug || seen.has(m.slug)) continue;
    seen.add(m.slug);
    items.push({
      slug: m.slug,
      label: m.display_name && m.display_name !== m.slug ? `${m.display_name} (${m.slug})` : m.slug,
    });
  }
  if (selectedSlug && !seen.has(selectedSlug)) {
    items.unshift({ slug: selectedSlug, label: selectedSlug });
  }
  return items
    .map(
      (item) =>
        `<option value="${escapeHtml(item.slug)}"${item.slug === selectedSlug ? " selected" : ""}>${escapeHtml(item.label)}</option>`,
    )
    .join("");
}

const ALL_EFFORTS: ReasoningEffort[] = ["low", "medium", "high", "xhigh", "max", "ultra"];

function getSupportedEfforts(modelSlug?: string | null): ReasoningEffort[] {
  if (modelSlug) {
    const found = state.availableModels.find((m) => m.slug === modelSlug);
    if (found && found.efforts.length > 0) {
      const valid = found.efforts.filter((e): e is ReasoningEffort =>
        (ALL_EFFORTS as readonly string[]).includes(e),
      );
      if (valid.length > 0) return valid;
    }
  }
  return ALL_EFFORTS;
}

function renderEffortOptions(
  selected: ReasoningEffortSetting,
  modelSlug?: string | null,
  compactAuto = false,
): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const autoText = compactAuto
    ? state.reasoningEffort !== "auto"
      ? str.reasoningEffortAutoWithDefault(effortShortLabel(state.reasoningEffort, loc))
      : str.reasoningEffortAutoShort
    : str.reasoningEffortAuto;

  const efforts = [...getSupportedEfforts(modelSlug)];
  if (selected !== "auto" && !efforts.includes(selected)) {
    efforts.push(selected);
  }

  return [
    `<option value="auto"${selected === "auto" ? " selected" : ""}>${escapeHtml(autoText)}</option>`,
    ...efforts.map((eff) => {
      const label = compactAuto ? effortShortLabel(eff, loc) : effortLabel(eff, loc);
      return `<option value="${escapeHtml(eff)}"${selected === eff ? " selected" : ""}>${escapeHtml(label)}</option>`;
    }),
  ].join("");
}

function renderSettingsPopover(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const thresholds = Array.from(new Set([10, 15, 20, 30, state.quotaGuardrailThresholdPct])).sort((a, b) => a - b);
  const thresholdOpts = thresholds
    .map(
      (v) =>
        `<option value="${v}"${state.quotaGuardrailThresholdPct === v ? " selected" : ""}>&lt; ${v}%</option>`,
    )
    .join("");

  let doctorHtml = "";
  if (state.doctorReport) {
    const hasWarn = state.doctorReport.checks.some((c) => c.status === "warning");
    const statusCls = !state.doctorReport.ok ? "error" : hasWarn ? "warning" : "ok";
    const statusSummary = !state.doctorReport.ok
      ? str.doctorHasErrors
      : hasWarn
        ? str.doctorHasWarnings
        : str.doctorAllOk;
    doctorHtml = `
      <div class="doctor-summary ${statusCls}">${escapeHtml(statusSummary)}</div>
      <div class="doctor-list">
        ${state.doctorReport.checks
          .map(
            (c) => `
              <div class="doctor-item" title="${escapeHtml(c.detail)}">
                <span class="doctor-pill ${c.status}">${escapeHtml(c.status)}</span>
                <div class="doctor-item-text">
                  <div class="doctor-item-id">${escapeHtml(c.id)}</div>
                  <div class="doctor-item-msg">${escapeHtml(c.summary)}</div>
                </div>
              </div>`,
          )
          .join("")}
      </div>`;
  }

  const worktreeCount = state.overview?.insights.worktree_count ?? 0;

  return `
    <div class="settings-popover" id="settings-popover" role="dialog" aria-label="${escapeHtml(str.settingsTitle)}">
      <div class="settings-header">
        <span class="settings-title">${escapeHtml(str.settingsTitle)}</span>
        <button class="icon-btn" id="btn-close-settings" type="button" aria-label="Close">×</button>
      </div>
      <div class="settings-section">
        <label class="settings-field">
          <span>${escapeHtml(str.defaultModelLabel)}</span>
          <select id="sel-default-model" class="ctrl-select">${renderModelOptions(state.defaultModel)}</select>
        </label>
        <label class="settings-field">
          <span>${escapeHtml(str.escalationModelLabel)}</span>
          <select id="sel-escalation-model" class="ctrl-select">${renderModelOptions(state.escalationModel)}</select>
        </label>
        <label class="settings-field">
          <span>${escapeHtml(str.reasoningEffortLabel)}</span>
          <select id="sel-default-effort" class="ctrl-select">${renderEffortOptions(state.reasoningEffort, state.defaultModel, false)}</select>
        </label>
        <div class="settings-inline-row">
          <label class="toggle-label">
            <input id="chk-guardrail" type="checkbox" ${state.quotaGuardrailEnabled ? "checked" : ""} />
            <span>${escapeHtml(str.guardrailLabel)}</span>
          </label>
          <select id="sel-guardrail-threshold" class="ctrl-select" aria-label="${escapeHtml(str.guardrailThresholdLabel)}"${state.quotaGuardrailEnabled ? "" : " disabled"}>${thresholdOpts}</select>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-actions-row">
          <button class="action-btn" id="btn-run-doctor" type="button"${state.runningDoctor ? " disabled" : ""}>${escapeHtml(state.runningDoctor ? str.runningDoctor : str.runDoctorBtn)}</button>
          <button class="action-btn" id="btn-prune-worktrees" type="button"${state.pruningWorktrees ? " disabled" : ""}>${escapeHtml(state.pruningWorktrees ? str.pruningWorktrees : str.pruneWorktreesBtn)}${worktreeCount > 0 ? ` (${worktreeCount})` : ""}</button>
        </div>
        ${doctorHtml}
      </div>
    </div>`;
}

function renderQuotaBadge(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const q = state.quota?.codex;
  if (!q || !q.available) return "";
  const parts: string[] = [];
  const tipLines: string[] = [];
  let warnClass = "";
  if (q.primary) {
    parts.push(`5h ${Math.round(q.primary.remaining_percent)}%`);
    tipLines.push(
      str.quota5hTip(q.primary.remaining_percent, q.primary.resets_at_local || q.primary.resets_in || "-"),
    );
    if (state.quotaGuardrailEnabled && q.primary.remaining_percent < state.quotaGuardrailThresholdPct) {
      warnClass = q.primary.remaining_percent < 5 ? " critical" : " warn";
      tipLines.push(str.quotaLowWarning(state.quotaGuardrailThresholdPct));
    }
  }
  if (q.secondary) {
    parts.push(`7d ${Math.round(q.secondary.remaining_percent)}%`);
    tipLines.push(
      str.quota7dTip(q.secondary.remaining_percent, q.secondary.resets_at_local || q.secondary.resets_in || "-"),
    );
  }
  if (typeof q.reset_credits_available === "number") {
    tipLines.push(str.quotaResetCreditsTip(q.reset_credits_available));
  }
  tipLines.push(str.antigravityQuotaNote);
  if (parts.length === 0) return "";
  return `<button class="quota-badge${warnClass}" id="btn-refresh-quota" type="button" title="${escapeHtml(tipLines.join("\n"))}">${escapeHtml(parts.join(" · "))}</button>`;
}

function renderTopbar(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const d = state.detail;

  const syncBadgeHtml =
    state.syncState === "error"
      ? `<span class="sync-badge error" title="${escapeHtml(state.syncError || "")}">${escapeHtml(str.syncError)}</span>`
      : `<span class="sync-badge"><span class="dot dot-inline ${d?.summary.status === "running" ? "running" : "completed"}"></span><span>${escapeHtml(state.syncState === "connecting" ? str.connecting : str.liveSync)}</span></span>`;

  const breadcrumb = d
    ? `
      <nav class="breadcrumb">
        <button class="breadcrumb-link" id="btn-breadcrumb-home" type="button">${escapeHtml(str.overview)}</button>
        <span class="breadcrumb-sep">/</span>
        <span class="breadcrumb-current" title="${escapeHtml(d.summary.project_dir)}">${escapeHtml(d.summary.project_name)}</span>
      </nav>`
    : `<nav class="breadcrumb"><span class="breadcrumb-current">${escapeHtml(str.overview)}</span></nav>`;

  const followToggle = d
    ? `
      <label class="toggle-label">
        <input id="chk-autofollow" type="checkbox" ${state.autoFollow ? "checked" : ""} />
        <span>${escapeHtml(str.autoFollow)}</span>
      </label>`
    : "";

  return `
    <header class="topbar">
      <div class="topbar-left">
        <button class="icon-btn sidebar-toggle" id="btn-sidebar" type="button" aria-label="${escapeHtml(str.toggleSidebar)}">☰</button>
        ${breadcrumb}
      </div>
      <div class="topbar-controls">
        ${renderRoutingSelect()}
        ${renderQuotaBadge()}
        ${renderLocaleSelect()}
        ${syncBadgeHtml}
        ${followToggle}
        <button class="icon-btn topbar-icon${state.settingsOpen ? " active" : ""}" id="btn-settings" type="button" aria-label="${escapeHtml(str.settingsAria)}" title="${escapeHtml(str.settingsAria)}" aria-expanded="${state.settingsOpen}">⚙</button>
      </div>
    </header>
    ${state.settingsOpen ? renderSettingsPopover() : ""}`;
}

function renderProfileOverviewPane(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const ov = state.overview;

  if (state.loadingOverview && !ov) {
    return `${renderTopbar()}<div class="empty-state"><strong>${escapeHtml(str.loadingSession)}</strong></div>`;
  }
  if (!ov) {
    return `${renderTopbar()}<div class="empty-state"><strong>${escapeHtml(str.noLocalSessions)}</strong></div>`;
  }

  const k = ov.kpis;
  const projectOptions = [
    `<option value="">${escapeHtml(str.allProjects)}</option>`,
    ...ov.projects.map(
      (p) =>
        `<option value="${escapeHtml(p.project_dir)}"${state.overviewProjectFilter === p.project_dir ? " selected" : ""}>${escapeHtml(p.project_name)} (${p.session_count})</option>`,
    ),
  ].join("");

  const agyPct = k.total_sessions > 0 ? Math.round((k.agy_sessions / k.total_sessions) * 100) : 0;
  const directPct = k.total_sessions > 0 ? 100 - agyPct : 0;

  const heatmapHtml = renderActivityHeatmap({
    todayIso: ov.today,
    days: ov.heatmap_days,
    origin: state.originFilter,
    metric: state.heatmapMetric,
    selectedDate: state.selectedHeatmapDate,
    locale: loc,
    weeksCount: 52,
  });

  const filteredSessions = ov.recent_sessions.filter((s) => {
    if (state.originFilter === "agy" && s.origin !== "agy") return false;
    if (state.originFilter === "direct" && s.origin !== "direct") return false;
    if (state.selectedHeatmapDate && s.date !== state.selectedHeatmapDate) return false;
    return true;
  });

  const recentRowsHtml =
    filteredSessions.length > 0
      ? filteredSessions
          .slice(0, 20)
          .map((s) => {
            const originBadge =
              s.origin === "agy"
                ? `<span class="origin-pill agy">${escapeHtml(str.originTagAgy)}</span>`
                : `<span class="origin-pill direct">${escapeHtml(str.originTagDirect)}</span>`;
            const metaBits: string[] = [s.project_name];
            if (s.model) metaBits.push(s.model);
            if (s.reasoning_effort) metaBits.push(effortShortLabel(s.reasoning_effort, loc));
            metaBits.push(str.roundsShort(s.round_count));
            if (s.total_tokens > 0) metaBits.push(`${formatCompactNumber(s.total_tokens, loc)} tok`);
            return `
              <button class="ov-row" data-open-session="${escapeHtml(s.session_id)}" type="button">
                <span class="dot dot-inline ${s.status}"></span>
                ${originBadge}
                <div class="ov-row-main">
                  <div class="ov-row-title">${escapeHtml(s.title)}</div>
                  <div class="ov-row-sub">${escapeHtml(metaBits.join(" · "))}</div>
                </div>
                <span class="ov-row-right">${escapeHtml(formatRelativeTime(s.updated_at, loc))}</span>
              </button>`;
          })
          .join("")
      : `<div class="empty-state"><strong>${escapeHtml(str.noFilteredSessions)}</strong></div>`;

  const topModelText = ov.insights.top_model
    ? `${ov.insights.top_model.name} · ${ov.insights.top_model.share_pct}%`
    : "-";
  const topEffortText = ov.insights.top_effort
    ? `${effortLabel(ov.insights.top_effort.name, loc)} · ${ov.insights.top_effort.share_pct}%`
    : "-";

  const dateFilterPill = state.selectedHeatmapDate
    ? `<button class="date-filter-pill" id="btn-clear-date" type="button" title="${escapeHtml(str.clearDateFilter)}">${escapeHtml(state.selectedHeatmapDate)} ×</button>`
    : "";

  const hotFilesSection =
    ov.hot_files.length > 0
      ? `
        <section class="profile-section">
          <div class="section-bar">
            <h2 class="section-title">${escapeHtml(str.hotFilesTitle)}</h2>
          </div>
          <div class="hot-file-list">
            ${ov.hot_files
              .map(
                (f) =>
                  `<span class="hot-file-pill" title="${escapeHtml(f.path)}"><span>${escapeHtml(f.short_name)}</span><span class="hot-file-cnt">${escapeHtml(str.touchedBySessions(f.session_count))}</span></span>`,
              )
              .join("")}
          </div>
        </section>`
      : "";

  const liveRunning = findBestRunningSession(state.sessions, state.preferredWorkspace);
  const liveBannerHtml = liveRunning
    ? `
      <button class="live-banner" data-open-session="${escapeHtml(liveRunning.session_id)}" type="button">
        <span class="dot dot-inline running"></span>
        <span class="live-banner-status">${escapeHtml(statusLabel("running", loc))}</span>
        <span class="live-banner-title">${escapeHtml(liveRunning.title)}</span>
        <span class="live-banner-cta">${escapeHtml(str.liveBannerCta)} →</span>
      </button>`
    : "";

  const breakdown = ov.insights.model_breakdown ?? [];
  const breakdownHtml =
    breakdown.length > 0
      ? `
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightModelBreakdown)}</span>
              <div class="insight-val insight-val-stack">
                <span>${escapeHtml(breakdown.slice(0, 3).map((m) => `${m.name} ${Math.round(m.share_pct)}%`).join(" · "))}</span>
                ${
                  breakdown.length > 1
                    ? `<div class="model-dist-bar" aria-hidden="true">${breakdown
                        .slice(0, 4)
                        .map(
                          (m, idx) =>
                            `<span class="model-dist-seg seg-${idx}" style="width:${Math.max(2, m.share_pct)}%" title="${escapeHtml(`${m.name}: ${m.count} (${m.share_pct}%)`)}"></span>`,
                        )
                        .join("")}</div>`
                    : ""
                }
              </div>
            </div>`
      : "";

  const worktreesHtml =
    (ov.insights.worktree_count ?? 0) > 0
      ? `
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.worktreesCountLabel)}</span>
              <span class="insight-val">${ov.insights.worktree_count}</span>
            </div>`
      : "";

  return `
    ${renderTopbar()}
    <div class="overview-scroll">
      <div class="profile-canvas">
        <div class="profile-header">
          <h1 class="profile-heading">Codex Orbit</h1>
          <select id="sel-overview-project" class="ctrl-select" aria-label="${escapeHtml(str.allProjects)}">${projectOptions}</select>
        </div>

        ${liveBannerHtml}

        <div class="kpi-strip">
          <div class="kpi-cell">
            <div class="kpi-value">${escapeHtml(formatCompactNumber(k.total_tokens, loc))}</div>
            <div class="kpi-label">${escapeHtml(str.kpiTotalTokens)}</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-value">${agyPct}%</div>
            <div class="kpi-label">${escapeHtml(str.kpiAgyShare)}</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-value">${escapeHtml(formatDuration(k.longest_duration_ms))}</div>
            <div class="kpi-label">${escapeHtml(str.kpiLongestTask)}</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-value">${escapeHtml(formatDays(k.longest_streak_days, loc))}</div>
            <div class="kpi-label">${escapeHtml(str.kpiLongestStreak)}</div>
          </div>
          <div class="kpi-cell">
            <div class="kpi-value">${escapeHtml(formatDays(k.current_streak_days, loc))}</div>
            <div class="kpi-label">${escapeHtml(str.kpiCurrentStreak)}</div>
          </div>
        </div>

        <section class="profile-section">
          <div class="section-bar">
            <h2 class="section-title">${escapeHtml(str.heatmapTitle)}</h2>
            <div class="text-tabs">
              <button class="text-tab${state.originFilter === "all" ? " active" : ""}" data-origin-filter="all" type="button">${escapeHtml(str.originAll)}</button>
              <button class="text-tab${state.originFilter === "agy" ? " active" : ""}" data-origin-filter="agy" type="button">${escapeHtml(str.originAgy)}</button>
              <button class="text-tab${state.originFilter === "direct" ? " active" : ""}" data-origin-filter="direct" type="button">${escapeHtml(str.originDirect)}</button>
              <span class="tab-divider" aria-hidden="true"></span>
              <button class="text-tab${state.heatmapMetric === "tokens" ? " active" : ""}" data-hm-metric="tokens" type="button">${escapeHtml(str.metricTokens)}</button>
              <button class="text-tab${state.heatmapMetric === "sessions" ? " active" : ""}" data-hm-metric="sessions" type="button">${escapeHtml(str.metricSessions)}</button>
            </div>
          </div>

          ${heatmapHtml}

          <div class="origin-split">
            <div class="origin-bar" role="img" aria-label="${escapeHtml(str.originSplitSummary(k.agy_sessions, agyPct, k.direct_sessions, directPct))}">
              <div class="origin-bar-agy" style="width:${agyPct}%"></div>
              <div class="origin-bar-direct" style="width:${directPct}%"></div>
            </div>
            <div class="origin-split-meta">
              <div class="origin-dots">
                <span class="origin-dot-label"><span class="origin-swatch agy"></span>${escapeHtml(str.originAgy)} ${k.agy_sessions} (${agyPct}%)</span>
                <span class="origin-dot-label"><span class="origin-swatch direct"></span>${escapeHtml(str.originDirect)} ${k.direct_sessions} (${directPct}%)</span>
              </div>
              <span>${k.total_sessions} ${escapeHtml(str.metricSessions)}</span>
            </div>
          </div>
        </section>

        <section class="profile-section">
          <div class="section-bar">
            <h2 class="section-title">${escapeHtml(str.insightsTitle)}</h2>
          </div>
          <div class="insights-list">
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightOrigin)}</span>
              <span class="insight-val">${escapeHtml(str.originSplitSummary(k.agy_sessions, agyPct, k.direct_sessions, directPct))}</span>
            </div>
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightTopModel)}</span>
              <span class="insight-val">${escapeHtml(topModelText)}</span>
            </div>
            ${breakdownHtml}
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightTopEffort)}</span>
              <span class="insight-val">${escapeHtml(topEffortText)}</span>
            </div>
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightActiveProjects)}</span>
              <span class="insight-val">${ov.insights.active_projects}</span>
            </div>
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightTotalCommands)}</span>
              <span class="insight-val">${ov.insights.total_commands.toLocaleString()}</span>
            </div>
            <div class="insight-row">
              <span class="insight-key">${escapeHtml(str.insightRoutingMode)}</span>
              <span class="insight-val">${escapeHtml(state.reasoningEffort !== "auto" ? `${routingModeLabel(state.routingMode, loc)} · ${effortShortLabel(state.reasoningEffort, loc)}` : routingModeLabel(state.routingMode, loc))}</span>
            </div>
            ${worktreesHtml}
          </div>
        </section>

        <section class="profile-section">
          <div class="section-bar">
            <h2 class="section-title">${escapeHtml(str.recentSessionsTitle)}</h2>
            <div class="text-tabs">
              ${dateFilterPill}
              <button class="text-tab${state.originFilter === "all" ? " active" : ""}" data-origin-filter="all" type="button">${escapeHtml(str.originAll)}</button>
              <button class="text-tab${state.originFilter === "agy" ? " active" : ""}" data-origin-filter="agy" type="button">${escapeHtml(str.originAgy)}</button>
              <button class="text-tab${state.originFilter === "direct" ? " active" : ""}" data-origin-filter="direct" type="button">${escapeHtml(str.originDirect)}</button>
            </div>
          </div>
          <div class="ov-list">${recentRowsHtml}</div>
        </section>

        ${hotFilesSection}
      </div>
    </div>`;
}

function renderCommandRow(turnId: string, cmd: CommandRecord, muted = false): string {
  const str = getStrings(activeLocale());
  const exitCls =
    cmd.exit_code === null || cmd.exit_code === undefined
      ? "pending"
      : cmd.exit_code === 0
        ? "ok"
        : "fail";
  const exitText =
    cmd.exit_code === null || cmd.exit_code === undefined ? "…" : `exit ${cmd.exit_code}`;
  const outHtml = cmd.output_preview
    ? `<pre class="cmd-output">${escapeHtml(cmd.output_preview)}</pre>`
    : `<pre class="cmd-output">${escapeHtml(str.noOutput)}</pre>`;

  return `
    <details class="cmd-row${muted ? " cmd-row-muted" : ""}" data-fold-key="cmd:${escapeHtml(turnId)}:${escapeHtml(cmd.id)}">
      <summary>
        <span class="chevron">▸</span>
        <span class="cmd-text" title="${escapeHtml(cmd.command)}">${escapeHtml(cmd.command)}</span>
        <span class="cmd-exit ${exitCls}">${exitText}</span>
        <span class="cmd-dur">${formatDuration(cmd.duration_ms)}</span>
      </summary>
      ${outHtml}
    </details>`;
}

function renderAssistantBlock(blk: AssistantBlock): string {
  const cls = blk.phase === "commentary" ? "msg-block commentary" : "msg-block";
  const canOpenFile = isAuxPaneSurface();
  return `<div class="${cls}">${renderSafeMarkdown(blk.text, { canOpenFile })}</div>`;
}

function renderFileChip(fc: FileChangeRecord): string {
  const str = getStrings(activeLocale());
  const op = fileOpLabel(fc.change_type);
  const canOpenFile = state.canUpdateContext || state.canSendMessage;
  if (canOpenFile) {
    return `<button class="file-chip clickable" data-open-file="${escapeHtml(fc.path)}" title="${escapeHtml(str.openFileTip(fc.path))}" type="button"><span class="file-op ${op.cls}">${op.text}</span><span>${escapeHtml(shortFileName(fc.path))}</span></button>`;
  }
  return `<span class="file-chip" title="${escapeHtml(fc.path)}"><span class="file-op ${op.cls}">${op.text}</span><span>${escapeHtml(shortFileName(fc.path))}</span></span>`;
}

function renderTurnSection(turn: TurnDetail, isLatest: boolean, sessionId: string): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const roundLabel = str.roundTag(turn.round_index, turn.is_delegated);
  const sourceBadge = turn.source_model
    ? `<span class="source-tag">${escapeHtml(turn.source_model)}</span>`
    : "";
  const isoText = isolationLabel(turn.isolation_mode, loc);
  const isoBadge = isoText
    ? `<span class="iso-tag" title="${escapeHtml(turn.worktree_path || isoText)}">${escapeHtml(isoText)}</span>`
    : "";

  const modelMeta = turn.model ? `<span>${escapeHtml(turn.model)}</span>` : "";
  const effortMeta = turn.effort
    ? `<span class="effort-chip" title="${escapeHtml(`${str.reasoningEffortAria}: ${effortLabel(turn.effort, loc)}`)}">${escapeHtml(effortShortLabel(turn.effort, loc))}</span>`
    : "";
  const durMeta = turn.duration_ms ? `<span>${formatDuration(turn.duration_ms)}</span>` : "";

  const canHandoff = state.canUpdateContext || state.canSendMessage;
  const turnHandoffBtn = canHandoff
    ? `<button class="turn-action-btn" data-handoff-turn="${escapeHtml(turn.turn_id)}" type="button" title="${escapeHtml(str.handoffRoundTip)}">${escapeHtml(str.handoffRound)}</button>`
    : "";

  const trimmedPrompt = (turn.prompt || "").trim();
  const openPromptAttr =
    isLatest && !turn.assistant_messages.length && !turn.commands.length ? " open" : "";
  const promptHtml = trimmedPrompt
    ? `
      <details class="prompt-callout" data-fold-key="prompt:${escapeHtml(turn.turn_id)}"${openPromptAttr}>
        <summary>
          <span class="chevron">▸</span>
          <span class="prompt-preview">${escapeHtml(trimmedPrompt.replace(/\s+/g, " "))}</span>
        </summary>
        <div class="prompt-body">${escapeHtml(trimmedPrompt)}</div>
      </details>`
    : "";

  const reasoningHtml =
    turn.reasoning_summaries.length > 0
      ? `
        <details class="reasoning-box" data-fold-key="reasoning:${escapeHtml(turn.turn_id)}">
          <summary>
            <span class="chevron">▸</span>
            <span>${escapeHtml(str.thinkingSummary(turn.reasoning_summaries.length))}</span>
          </summary>
          <div class="reasoning-list">
            ${turn.reasoning_summaries.map((r) => `<div>${escapeHtml(r)}</div>`).join("")}
          </div>
        </details>`
      : "";

  const messagesHtml = turn.assistant_messages.map(renderAssistantBlock).join("");

  const cmdTurnKey = `${sessionId}:${turn.turn_id}`;
  const isCmdExpanded = state.expandedCommandTurns.has(cmdTurnKey);
  let commandsHtml = "";
  if (turn.commands.length === 1) {
    commandsHtml = `
      <div class="cmd-section">
        <div class="cmd-header">
          <span class="cmd-header-left">
            <span>${escapeHtml(str.commandExecutions)}</span>
            <span class="cmd-count">1</span>
          </span>
        </div>
        ${renderCommandRow(turn.turn_id, turn.commands[0], true)}
      </div>`;
  } else if (turn.commands.length > 1) {
    const latestCmd = turn.commands[turn.commands.length - 1];
    const rowsHtml = isCmdExpanded
      ? turn.commands.map((c) => renderCommandRow(turn.turn_id, c, false)).join("")
      : renderCommandRow(turn.turn_id, latestCmd, true);
    commandsHtml = `
      <div class="cmd-section">
        <button class="cmd-header cmd-header-toggle${isCmdExpanded ? " expanded" : ""}" data-toggle-cmds="${escapeHtml(cmdTurnKey)}" type="button" aria-expanded="${isCmdExpanded}">
          <span class="cmd-header-left">
            <span class="cmd-chevron">${isCmdExpanded ? "▾" : "▸"}</span>
            <span>${escapeHtml(str.commandExecutions)}</span>
            <span class="cmd-count">${turn.commands.length}</span>
          </span>
          <span class="cmd-toggle-hint">${escapeHtml(isCmdExpanded ? str.collapseCommands : str.expandAllCommands)}</span>
        </button>
        ${rowsHtml}
      </div>`;
  }

  const filesHtml =
    turn.changed_files.length > 0
      ? `<div class="turn-files">${turn.changed_files.map(renderFileChip).join("")}</div>`
      : "";

  const errorText =
    turn.error_code === "thread_writer_busy"
      ? str.writerBusyHint
      : turn.error_reason;
  const errorHtml = errorText
    ? `<div class="error-banner">${escapeHtml(str.abortedPrefix + errorText)}</div>`
    : "";

  return `
    <section class="turn-section" data-turn-id="${escapeHtml(turn.turn_id)}">
      <div class="turn-header">
        <div class="turn-header-left">
          <span class="dot dot-inline ${turn.status}" title="${escapeHtml(statusLabel(turn.status, loc))}"></span>
          <span class="round-tag">${escapeHtml(roundLabel)}</span>
          ${sourceBadge}
          ${isoBadge}
        </div>
        <div class="turn-header-right">
          ${turnHandoffBtn}
          ${modelMeta}
          ${effortMeta}
          ${durMeta}
        </div>
      </div>
      ${promptHtml}
      <div class="turn-content">
        ${errorHtml}
        ${reasoningHtml}
        ${messagesHtml}
        ${commandsHtml}
        ${filesHtml}
      </div>
    </section>`;
}

function renderFollowupComposer(d: SessionDetail): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const isRunning = d.summary.status === "running" || Boolean(d.summary.active_run_id);
  const disabled = isRunning || state.sendingFollowup;
  const canSend = !disabled && state.followupDraft.trim().length > 0;
  const placeholder = state.sendingFollowup
    ? str.followupSendingPlaceholder
    : isRunning
      ? str.followupRunningPlaceholder
      : str.followupPlaceholder;

  return `
    <div class="followup-dock">
      <div class="followup-box${disabled ? " disabled" : ""}">
        <textarea
          id="followup-input"
          class="followup-input"
          rows="1"
          placeholder="${escapeHtml(placeholder)}"
          aria-label="${escapeHtml(str.followupSendAria)}"
          ${disabled ? "disabled" : ""}
        >${escapeHtml(state.followupDraft)}</textarea>
        <div class="followup-footer">
          <div class="followup-footer-left">
            <select
              id="sel-followup-effort"
              class="ctrl-select followup-effort-select"
              aria-label="${escapeHtml(str.reasoningEffortAria)}"
              title="${escapeHtml(str.reasoningEffortTip)}"
              ${disabled ? "disabled" : ""}
            >${renderEffortOptions(state.followupEffort, d.summary.model, true)}</select>
            <label class="toggle-label followup-web-toggle" title="${escapeHtml(str.followupWebSearchTip)}">
              <input id="chk-followup-web" type="checkbox" ${state.followupWebSearch ? "checked" : ""} ${disabled ? "disabled" : ""} />
              <span>${escapeHtml(str.followupWebSearch)}</span>
            </label>
          </div>
          <button
            id="btn-send-followup"
            class="followup-send-btn"
            type="button"
            aria-label="${escapeHtml(str.followupSendAria)}"
            title="${escapeHtml(str.followupSendAria)}"
            ${canSend ? "" : "disabled"}
          >↑</button>
        </div>
      </div>
    </div>`;
}

function renderSessionDetailPane(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const d = state.detail;

  if (state.loadingDetail && !d) {
    return `${renderTopbar()}<div class="empty-state"><strong>${escapeHtml(str.loadingSession)}</strong></div>`;
  }
  if (!d) {
    return renderProfileOverviewPane();
  }

  const totalDurationMs = d.turns.reduce((acc, t) => acc + (t.duration_ms || 0), 0);
  const canCancel = Boolean(d.summary.active_run_id && d.summary.status === "running");
  const isCancelling = Boolean(canCancel && state.cancellingRunId === d.summary.active_run_id);

  const actionButtons: string[] = [];
  if (state.actionToast) {
    actionButtons.push(`<span class="action-toast">${escapeHtml(state.actionToast)}</span>`);
  }
  if (canCancel) {
    actionButtons.push(
      `<button class="action-btn danger" id="btn-cancel-run" type="button" ${isCancelling ? "disabled" : ""}>${escapeHtml(isCancelling ? str.cancelling : str.cancelTask)}</button>`,
    );
  }
  if (state.canUpdateContext) {
    actionButtons.push(
      `<button class="action-btn" id="btn-attach-summary" type="button" title="${escapeHtml(str.attachSummaryTip)}">${escapeHtml(str.attachSummary)}</button>`,
    );
  }
  if (state.canSendMessage) {
    actionButtons.push(
      `<button class="action-btn" id="btn-send-summary" type="button" title="${escapeHtml(str.sendSummaryTip)}">${escapeHtml(str.sendSummary)}</button>`,
    );
  }

  const originBadge = d.summary.is_delegated
    ? `<span class="origin-pill agy">${escapeHtml(str.originTagAgy)}</span>`
    : `<span class="origin-pill direct">${escapeHtml(str.originTagDirect)}</span>`;

  const pathParts: string[] = [d.summary.project_dir];
  if (d.summary.git_branch) pathParts.push(`@${d.summary.git_branch}`);
  const iso = isolationLabel(d.summary.isolation_mode, loc);
  if (iso) pathParts.push(iso);
  if (d.summary.model) pathParts.push(d.summary.model);

  const filesDrawer =
    state.showFilesDrawer && d.all_changed_files.length > 0
      ? `<div class="files-drawer">${d.all_changed_files.map(renderFileChip).join("")}</div>`
      : "";

  const turnsHtml =
    d.turns.length > 0
      ? `<div class="turn-list">${d.turns.map((t, idx) => renderTurnSection(t, idx === d.turns.length - 1, d.summary.session_id)).join("")}</div>`
      : `<div class="empty-state"><strong>${escapeHtml(str.emptySessionTurns)}</strong></div>`;

  const effortPill = d.summary.reasoning_effort
    ? `<span class="effort-pill" title="${escapeHtml(`${str.reasoningEffortAria}: ${effortLabel(d.summary.reasoning_effort, loc)}`)}">${escapeHtml(effortShortLabel(d.summary.reasoning_effort, loc))}</span>`
    : "";

  return `
    ${renderTopbar()}
    <div class="detail-scroll" id="detail-scroll">
      <div class="reading-canvas">
        <div class="doc-header">
          <h1 class="doc-title">${escapeHtml(d.summary.title)}</h1>
          <div class="doc-path-row">${originBadge}<span>${escapeHtml(pathParts.join("  ·  "))}</span></div>
          <div class="doc-props">
            <span class="status-pill ${d.summary.status}">
              <span class="dot dot-inline ${d.summary.status}"></span>
              <span>${escapeHtml(statusLabel(d.summary.status, loc))}</span>
            </span>
            ${effortPill}
            <span class="metric">${escapeHtml(str.roundsLabel)} <strong>${d.turns.length}</strong></span>
            <span class="metric">${escapeHtml(str.commandsLabel)} <strong>${d.total_commands}</strong></span>
            <button class="metric-btn" id="btn-files-drawer" type="button">
              ${escapeHtml(str.changedFilesLabel)} <strong>${d.total_changed_files}</strong>${d.total_changed_files > 0 ? (state.showFilesDrawer ? " ▴" : " ▾") : ""}
            </button>
            <span class="metric">${escapeHtml(str.durationLabel)} <strong>${formatDuration(totalDurationMs || d.summary.duration_ms)}</strong></span>
            ${actionButtons.length > 0 ? `<div class="summary-actions">${actionButtons.join("")}</div>` : ""}
          </div>
          ${filesDrawer}
        </div>
        ${turnsHtml}
      </div>
    </div>
    ${renderFollowupComposer(d)}`;
}

function renderMainPane(): string {
  if (!state.selectedSessionId) {
    return renderProfileOverviewPane();
  }
  return renderSessionDetailPane();
}

function updateSidebarChrome(): void {
  const str = getStrings(activeLocale());
  const homeBtn = document.getElementById("btn-nav-home");
  if (homeBtn) {
    homeBtn.classList.toggle("active", state.selectedSessionId === null);
    const labelSpan = homeBtn.querySelector<HTMLElement>(".nav-home-label");
    if (labelSpan) labelSpan.textContent = str.overview;
  }
  const searchInput = document.getElementById("search-input") as HTMLInputElement | null;
  if (searchInput) searchInput.placeholder = str.searchPlaceholder;
  const refreshBtn = document.getElementById("btn-refresh");
  if (refreshBtn) {
    refreshBtn.title = str.refreshSessions;
    refreshBtn.setAttribute("aria-label", str.refreshSessions);
  }
  const newProjBtn = document.getElementById("btn-new-project");
  if (newProjBtn) {
    newProjBtn.title = str.newProject;
    newProjBtn.setAttribute("aria-label", str.newProject);
  }
}

function renderOrbitModal(): string {
  const loc = activeLocale();
  const str = getStrings(loc);

  if (state.newProjectOpen) {
    const errHtml = state.newProjectError
      ? `<div class="orbit-modal-error">${escapeHtml(state.newProjectError)}</div>`
      : "";
    return `
      <div id="orbit-modal-backdrop" class="orbit-modal-backdrop">
        <div class="orbit-modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(str.newProjectTitle)}">
          <div class="orbit-modal-header">
            <h3>${escapeHtml(str.newProjectTitle)}</h3>
            <button id="btn-close-orbit-modal" class="icon-btn" type="button" aria-label="${escapeHtml(str.cancelBtn)}">×</button>
          </div>
          <div class="orbit-modal-body">
            <label class="orbit-field">
              <span>${escapeHtml(str.projectPathLabel)}</span>
              <input id="new-project-dir-input" class="orbit-input" type="text" value="${escapeHtml(state.newProjectDir)}" placeholder="${escapeHtml(str.projectPathPlaceholder)}" autocomplete="off" />
            </label>
            <label class="orbit-check">
              <input id="new-project-create-missing" type="checkbox"${state.newProjectCreateMissing ? " checked" : ""} />
              <span>${escapeHtml(str.createDirIfMissing)}</span>
            </label>
            ${errHtml}
          </div>
          <div class="orbit-modal-footer">
            <button id="btn-cancel-orbit-modal" class="btn-secondary" type="button">${escapeHtml(str.cancelBtn)}</button>
            <button id="btn-submit-new-project" class="btn-primary" type="button"${state.newProjectSubmitting || !state.newProjectDir.trim() ? " disabled" : ""}>${escapeHtml(str.createProjectBtn)}</button>
          </div>
        </div>
      </div>`;
  }

  if (state.newChatProject) {
    const errHtml = state.newChatError
      ? `<div class="orbit-modal-error">${escapeHtml(state.newChatError)}</div>`
      : "";
    const modes: IsolationMode[] = ["workspace-write", "read-only", "worktree"];
    const modeOptions = modes
      .map(
        (m) =>
          `<option value="${m}"${state.newChatMode === m ? " selected" : ""}>${escapeHtml(isolationLabel(m, loc) || m)}</option>`,
      )
      .join("");
    return `
      <div id="orbit-modal-backdrop" class="orbit-modal-backdrop">
        <div class="orbit-modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(str.newChatTitle)}">
          <div class="orbit-modal-header">
            <div class="orbit-modal-title-wrap">
              <h3>${escapeHtml(str.newChatTitle)}</h3>
              <span class="orbit-modal-sub" title="${escapeHtml(state.newChatProject.projectDir)}">${escapeHtml(state.newChatProject.projectName)}</span>
            </div>
            <button id="btn-close-orbit-modal" class="icon-btn" type="button" aria-label="${escapeHtml(str.cancelBtn)}">×</button>
          </div>
          <div class="orbit-modal-body">
            <label class="orbit-field">
              <span>${escapeHtml(str.sessionTitleLabel)}</span>
              <input id="new-chat-title-input" class="orbit-input" type="text" value="${escapeHtml(state.newChatTitle)}" placeholder="${escapeHtml(str.sessionTitlePlaceholder)}" autocomplete="off" />
            </label>
            <label class="orbit-field">
              <span>${escapeHtml(str.sessionPromptLabel)}</span>
              <textarea id="new-chat-prompt-input" class="orbit-textarea" rows="4" placeholder="${escapeHtml(str.sessionPromptPlaceholder)}">${escapeHtml(state.newChatPrompt)}</textarea>
            </label>
            <div class="orbit-modal-row">
              <label class="orbit-field orbit-field-inline">
                <span>${escapeHtml(str.sessionModeLabel)}</span>
                <select id="new-chat-mode-select" class="ctrl-select">${modeOptions}</select>
              </label>
              <label class="orbit-field orbit-field-inline">
                <span>${escapeHtml(str.reasoningEffortAria)}</span>
                <select id="new-chat-effort-select" class="ctrl-select">${renderEffortOptions(state.newChatEffort, state.defaultModel, false)}</select>
              </label>
              <label class="orbit-check">
                <input id="new-chat-web-search" type="checkbox"${state.newChatWebSearch ? " checked" : ""} />
                <span>${escapeHtml(str.followupWebSearch)}</span>
              </label>
            </div>
            ${errHtml}
          </div>
          <div class="orbit-modal-footer">
            <button id="btn-cancel-orbit-modal" class="btn-secondary" type="button">${escapeHtml(str.cancelBtn)}</button>
            <button id="btn-submit-new-chat" class="btn-primary" type="button"${state.newChatSubmitting || !state.newChatPrompt.trim() ? " disabled" : ""}>${escapeHtml(str.startChatBtn)}</button>
          </div>
        </div>
      </div>`;
  }

  return "";
}

function renderModalRoot(): void {
  const root = document.getElementById("orbit-modal-root");
  if (!root) return;
  root.innerHTML = renderOrbitModal();
}

async function handleCreateProject(): Promise<void> {
  if (!api || state.newProjectSubmitting) return;
  const dir = state.newProjectDir.trim();
  if (!dir) return;
  const str = getStrings(activeLocale());
  state.newProjectSubmitting = true;
  state.newProjectError = null;
  renderModalRoot();
  try {
    const res = await api.createProject(dir, state.newProjectCreateMissing);
    if (!res.created || !res.project_dir) {
      state.newProjectError = res.error || "Could not add project.";
      return;
    }
    state.newProjectOpen = false;
    state.newProjectDir = "";
    state.expandedGroups.add(res.project_dir);
    showToast(str.toastProjectCreated(res.project_name || res.project_dir));
    await refreshSessions(false);
  } catch (err) {
    state.newProjectError = err instanceof Error ? err.message : String(err);
  } finally {
    state.newProjectSubmitting = false;
    renderModalRoot();
  }
}

async function handleCreateSession(): Promise<void> {
  if (!api || !state.newChatProject || state.newChatSubmitting) return;
  const prompt = state.newChatPrompt.trim();
  if (!prompt) return;
  const str = getStrings(activeLocale());
  const projectDir = state.newChatProject.projectDir;
  state.newChatSubmitting = true;
  state.newChatError = null;
  renderModalRoot();
  try {
    const res = await api.createSession(
      projectDir,
      prompt,
      state.newChatTitle.trim(),
      state.newChatMode,
      state.newChatWebSearch,
      state.newChatEffort,
    );
    if (!res.started || !res.run_id) {
      state.newChatError = res.error || "Could not start conversation.";
      return;
    }
    state.newChatProject = null;
    state.newChatTitle = "";
    state.newChatPrompt = "";
    state.newChatEffort = "auto";
    state.expandedGroups.add(projectDir);
    state.autoFollow = true;
    showToast(str.toastSessionStarted);
    await refreshSessions(false);
    const created = state.sessions.find(
      (s) => s.active_run_id === res.run_id || s.session_id === res.run_id,
    );
    await selectSession(created?.session_id || res.run_id);
    scheduleNextPoll();
  } catch (err) {
    state.newChatError = err instanceof Error ? err.message : String(err);
  } finally {
    state.newChatSubmitting = false;
    renderModalRoot();
  }
}

async function handleCancelActiveRun(): Promise<void> {
  if (!api || !state.detail?.summary.active_run_id) return;
  const str = getStrings(activeLocale());
  const runId = state.detail.summary.active_run_id;
  const sessionId = state.detail.summary.session_id;
  state.cancellingRunId = runId;
  detailDirty = true;
  render();

  try {
    const res = await api.cancelDelegation(sessionId, runId);
    showToast(res.cancelled ? str.toastCancelled : str.toastAlreadyDone);
    await pollActiveDetail();
    await refreshSessions(false);
  } catch (err) {
    showToast(str.toastCancelFailed(err instanceof Error ? err.message : String(err)));
  } finally {
    state.cancellingRunId = null;
    detailDirty = true;
    render();
  }
}

async function handleAttachSummary(turn?: TurnDetail): Promise<void> {
  if (!appInstance || !state.detail || !state.canUpdateContext) return;
  const str = getStrings(activeLocale());
  const markdown = buildCompactSummary(state.detail, turn);
  try {
    await appInstance.updateModelContext({
      content: [{ type: "text", text: markdown }],
    });
    showToast(str.toastAttached);
  } catch (err) {
    showToast(str.toastAttachFailed(err instanceof Error ? err.message : String(err)));
  }
}

async function handleSendSummary(turn?: TurnDetail): Promise<void> {
  if (!appInstance || !state.detail || !state.canSendMessage) return;
  const str = getStrings(activeLocale());
  const markdown = buildCompactSummary(state.detail, turn);
  try {
    const res = await appInstance.sendMessage({
      role: "user",
      content: [{ type: "text", text: markdown }],
    });
    showToast(res?.isError ? str.toastHostRejected : str.toastSent);
  } catch (err) {
    showToast(str.toastSendFailed(err instanceof Error ? err.message : String(err)));
  }
}

async function handleOpenFile(filePath: string): Promise<void> {
  if (!appInstance || !state.detail) return;
  const baseDir = state.detail.summary.worktree_path || state.detail.summary.project_dir;
  const uri = toFileUri(filePath, baseDir);
  if (!uri) return;
  try {
    await appInstance.request(
      {
        method: "com.google.antigravity/toggle-aux-pane",
        params: { tabType: "fileView", uri },
      } as unknown as Parameters<App["request"]>[0],
      toggleAuxPaneResultSchema,
    );
  } catch {
    // Gracefully ignore on surfaces without aux-pane file view.
  }
}

async function handleRoutingModeChange(nextMode: RoutingMode): Promise<void> {
  if (!api) return;
  const loc = activeLocale();
  const str = getStrings(loc);
  const prev = state.routingMode;
  state.routingMode = nextMode;
  detailDirty = true;
  render();
  try {
    const res = await api.setRoutingMode(nextMode);
    state.routingMode = res.routing_mode;
    showToast(str.toastModeSwitched(routingModeLabel(res.routing_mode, loc)));
  } catch (err) {
    state.routingMode = prev;
    showToast(str.toastSwitchFailed(err instanceof Error ? err.message : String(err)));
  }
}

async function handleUiLocaleChange(nextLocale: UiLocale): Promise<void> {
  if (!api) return;
  state.uiLocale = nextLocale;
  applyLocaleToDocument();
  listDirty = true;
  detailDirty = true;
  render();
  try {
    await api.setUiLocale(nextLocale);
  } catch {
    // Keep local selection even if persistence fails transiently.
  }
}

function applySettingsSnapshot(s: {
  routing_mode?: RoutingMode;
  ui_locale?: UiLocale;
  default_model?: string;
  escalation_model?: string;
  reasoning_effort?: ReasoningEffortSetting;
  quota_guardrail_enabled?: boolean;
  quota_guardrail_threshold_pct?: number;
  available_models?: ModelCatalogEntry[];
}): void {
  let localeChanged = false;
  if (s.routing_mode && s.routing_mode !== state.routingMode) {
    state.routingMode = s.routing_mode;
    detailDirty = true;
  }
  if (s.ui_locale && s.ui_locale !== state.uiLocale) {
    state.uiLocale = s.ui_locale;
    localeChanged = true;
    detailDirty = true;
  }
  if (s.default_model && s.default_model !== state.defaultModel) {
    state.defaultModel = s.default_model;
    detailDirty = true;
  }
  if (s.escalation_model && s.escalation_model !== state.escalationModel) {
    state.escalationModel = s.escalation_model;
    detailDirty = true;
  }
  if (s.reasoning_effort && s.reasoning_effort !== state.reasoningEffort) {
    state.reasoningEffort = s.reasoning_effort;
    detailDirty = true;
  }
  if (
    typeof s.quota_guardrail_enabled === "boolean" &&
    s.quota_guardrail_enabled !== state.quotaGuardrailEnabled
  ) {
    state.quotaGuardrailEnabled = s.quota_guardrail_enabled;
    detailDirty = true;
  }
  if (
    typeof s.quota_guardrail_threshold_pct === "number" &&
    s.quota_guardrail_threshold_pct !== state.quotaGuardrailThresholdPct
  ) {
    state.quotaGuardrailThresholdPct = s.quota_guardrail_threshold_pct;
    detailDirty = true;
  }
  if (s.available_models !== undefined) {
    state.availableModels = s.available_models;
    detailDirty = true;
  }
  if (localeChanged) {
    applyLocaleToDocument();
  }
}

async function refreshSettings(): Promise<void> {
  if (!api) return;
  try {
    const s = await api.getSettings();
    applySettingsSnapshot(s);
    render();
  } catch {
    // Non-fatal: keep defaults when host/tool call fails transiently.
  }
}

async function handleUpdateSettings(patch: {
  default_model?: string;
  escalation_model?: string;
  reasoning_effort?: ReasoningEffortSetting;
  quota_guardrail_enabled?: boolean;
  quota_guardrail_threshold_pct?: number;
}): Promise<void> {
  if (!api) return;
  const str = getStrings(activeLocale());
  try {
    const updated = await api.updateSettings(patch);
    state.defaultModel = updated.default_model;
    state.escalationModel = updated.escalation_model;
    state.reasoningEffort = updated.reasoning_effort ?? state.reasoningEffort;
    state.quotaGuardrailEnabled = updated.quota_guardrail_enabled;
    state.quotaGuardrailThresholdPct = updated.quota_guardrail_threshold_pct;
    state.availableModels = updated.available_models ?? state.availableModels;
    detailDirty = true;
    render();
    showToast(str.toastSettingsSaved);
  } catch (err) {
    showToast(str.toastSwitchFailed(err instanceof Error ? err.message : String(err)));
  }
}

async function handleRunDoctor(): Promise<void> {
  if (!api || state.runningDoctor) return;
  state.runningDoctor = true;
  detailDirty = true;
  render();
  try {
    state.doctorReport = await api.runDoctor();
  } catch (err) {
    const str = getStrings(activeLocale());
    showToast(str.toastSwitchFailed(err instanceof Error ? err.message : String(err)));
  } finally {
    state.runningDoctor = false;
    detailDirty = true;
    render();
  }
}

async function handlePruneWorktrees(): Promise<void> {
  if (!api || state.pruningWorktrees) return;
  const str = getStrings(activeLocale());
  state.pruningWorktrees = true;
  detailDirty = true;
  render();
  try {
    const res = await api.pruneWorktrees();
    showToast(str.pruneResultToast(res.removed.length, res.skipped_running.length));
    if (!state.selectedSessionId) {
      await refreshProfileOverview();
    }
  } catch (err) {
    showToast(str.toastSwitchFailed(err instanceof Error ? err.message : String(err)));
  } finally {
    state.pruningWorktrees = false;
    detailDirty = true;
    render();
  }
}

async function handleSendFollowup(): Promise<void> {
  if (!api || !state.detail || state.sendingFollowup) return;
  const text = state.followupDraft.trim();
  if (!text) return;
  const isRunning = state.detail.summary.status === "running" || Boolean(state.detail.summary.active_run_id);
  if (isRunning) return;

  const str = getStrings(activeLocale());
  const sessionId = state.detail.summary.session_id;
  state.sendingFollowup = true;
  detailDirty = true;
  render();

  try {
    const res = await api.sendFollowup(
      sessionId,
      text,
      state.followupWebSearch,
      state.followupEffort,
    );
    if (!res.started) {
      showToast(str.toastFollowupFailed(res.error || "Unknown error"));
      return;
    }
    state.followupDraft = "";
    state.autoFollow = true;
    showToast(str.toastFollowupStarted);
    await pollActiveDetail();
    await refreshSessions(false);
    scheduleNextPoll();
  } catch (err) {
    showToast(str.toastFollowupFailed(err instanceof Error ? err.message : String(err)));
  } finally {
    state.sendingFollowup = false;
    detailDirty = true;
    render(state.autoFollow);
  }
}

function openOverview(): void {
  state.selectedSessionId = null;
  state.detail = null;
  state.sidebarOpen = false;
  hmScrollInitialized = false;
  listDirty = true;
  detailDirty = true;
  updateSidebarChrome();
  render();
  void refreshProfileOverview();
}

function mountShell(): void {
  const root = document.getElementById("app");
  if (!root) return;
  const str = getStrings(activeLocale());

  root.innerHTML = `
    <div class="shell" id="shell">
      <aside class="sidebar">
        <div class="sidebar-nav">
          <button id="btn-nav-home" class="nav-home-btn active" type="button">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true">
              <rect x="2" y="2" width="4.5" height="4.5" rx="1"/>
              <rect x="9.5" y="2" width="4.5" height="4.5" rx="1"/>
              <rect x="2" y="9.5" width="4.5" height="4.5" rx="1"/>
              <rect x="9.5" y="9.5" width="4.5" height="4.5" rx="1"/>
            </svg>
            <span class="nav-home-label">${escapeHtml(str.overview)}</span>
          </button>
        </div>
        <div class="sidebar-top">
          <input id="search-input" class="search-input" type="search" placeholder="${escapeHtml(str.searchPlaceholder)}" autocomplete="off" />
          <button id="btn-new-project" class="icon-btn" type="button" title="${escapeHtml(str.newProject)}" aria-label="${escapeHtml(str.newProject)}">+</button>
          <button id="btn-refresh" class="icon-btn" type="button" title="${escapeHtml(str.refreshSessions)}" aria-label="${escapeHtml(str.refreshSessions)}">↻</button>
        </div>
        <div id="session-groups" class="session-groups"></div>
      </aside>
      <main id="main-pane" class="main"></main>
      <div id="orbit-modal-root"></div>
    </div>`;

  document.getElementById("btn-nav-home")?.addEventListener("click", () => {
    openOverview();
  });

  const searchInput = document.getElementById("search-input") as HTMLInputElement;
  searchInput.addEventListener("input", () => {
    state.query = searchInput.value;
    if (searchDebounce !== null) window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      void refreshSessions(false);
    }, 180);
  });

  document.getElementById("btn-refresh")?.addEventListener("click", () => {
    void refreshSessions(true);
    if (!state.selectedSessionId) void refreshProfileOverview();
  });

  document.getElementById("btn-new-project")?.addEventListener("click", () => {
    state.newChatProject = null;
    state.newProjectOpen = true;
    state.newProjectError = null;
    if (!state.newProjectDir && state.preferredWorkspace) {
      state.newProjectDir = state.preferredWorkspace;
    }
    renderModalRoot();
    window.setTimeout(() => {
      document.getElementById("new-project-dir-input")?.focus();
    }, 0);
  });

  document.getElementById("session-groups")?.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    const newChatBtn = target.closest<HTMLElement>("[data-new-chat-dir]");
    if (newChatBtn) {
      const dir = newChatBtn.dataset.newChatDir || "";
      const name = newChatBtn.dataset.newChatName || dir;
      if (dir) {
        state.newProjectOpen = false;
        state.newChatProject = { projectDir: dir, projectName: name };
        state.newChatError = null;
        renderModalRoot();
        window.setTimeout(() => {
          document.getElementById("new-chat-prompt-input")?.focus();
        }, 0);
      }
      return;
    }
    const groupBtn = target.closest<HTMLElement>("[data-group-dir]");
    if (groupBtn) {
      const dir = groupBtn.dataset.groupDir || "";
      if (state.expandedGroups.has(dir)) state.expandedGroups.delete(dir);
      else state.expandedGroups.add(dir);
      listDirty = true;
      render();
      return;
    }
    const sessionBtn = target.closest<HTMLElement>("[data-session-id]");
    if (sessionBtn) {
      const sid = sessionBtn.dataset.sessionId;
      if (sid) {
        state.sidebarOpen = false;
        void selectSession(sid);
      }
    }
  });

  document.getElementById("main-pane")?.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;

    if (target.closest("#btn-sidebar")) {
      state.sidebarOpen = !state.sidebarOpen;
      document.getElementById("shell")?.classList.toggle("sidebar-open", state.sidebarOpen);
      return;
    }
    if (state.sidebarOpen) {
      state.sidebarOpen = false;
      document.getElementById("shell")?.classList.remove("sidebar-open");
    }
    const toggleCmdsBtn = target.closest<HTMLElement>("[data-toggle-cmds]");
    if (toggleCmdsBtn) {
      const key = toggleCmdsBtn.dataset.toggleCmds;
      if (key) {
        if (state.expandedCommandTurns.has(key)) state.expandedCommandTurns.delete(key);
        else state.expandedCommandTurns.add(key);
        detailDirty = true;
        render();
      }
      return;
    }
    if (target.closest("#btn-breadcrumb-home")) {
      openOverview();
      return;
    }
    if (target.closest("#btn-files-drawer")) {
      if (state.detail && state.detail.all_changed_files.length > 0) {
        state.showFilesDrawer = !state.showFilesDrawer;
        detailDirty = true;
        render();
      }
      return;
    }
    if (target.closest("#btn-cancel-run")) {
      void handleCancelActiveRun();
      return;
    }
    if (target.closest("#btn-attach-summary")) {
      void handleAttachSummary();
      return;
    }
    if (target.closest("#btn-send-summary")) {
      void handleSendSummary();
      return;
    }
    if (target.closest("#btn-refresh-quota")) {
      void refreshQuota(true);
      return;
    }
    if (target.closest("#btn-settings")) {
      state.settingsOpen = !state.settingsOpen;
      if (state.settingsOpen && state.availableModels.length === 0) {
        void refreshSettings();
      }
      detailDirty = true;
      render();
      return;
    }
    if (target.closest("#btn-close-settings")) {
      state.settingsOpen = false;
      detailDirty = true;
      render();
      return;
    }
    if (target.closest("#btn-run-doctor")) {
      void handleRunDoctor();
      return;
    }
    if (target.closest("#btn-prune-worktrees")) {
      void handlePruneWorktrees();
      return;
    }
    if (target.closest("#btn-send-followup")) {
      void handleSendFollowup();
      return;
    }
    if (target.closest("#btn-clear-date")) {
      state.selectedHeatmapDate = null;
      detailDirty = true;
      render();
      return;
    }

    const originBtn = target.closest<HTMLElement>("[data-origin-filter]");
    if (originBtn) {
      const next = originBtn.dataset.originFilter as OriginFilter;
      if (next && next !== state.originFilter) {
        state.originFilter = next;
        detailDirty = true;
        render();
      }
      return;
    }

    const metricBtn = target.closest<HTMLElement>("[data-hm-metric]");
    if (metricBtn) {
      const next = metricBtn.dataset.hmMetric as HeatmapMetric;
      if (next && next !== state.heatmapMetric) {
        state.heatmapMetric = next;
        detailDirty = true;
        render();
      }
      return;
    }

    const hmCell = target.closest<HTMLElement>("[data-hm-date]");
    if (hmCell) {
      const d = hmCell.dataset.hmDate || null;
      state.selectedHeatmapDate = state.selectedHeatmapDate === d ? null : d;
      detailDirty = true;
      render();
      return;
    }

    const openSessBtn = target.closest<HTMLElement>("[data-open-session]");
    if (openSessBtn) {
      const sid = openSessBtn.dataset.openSession;
      if (sid) void selectSession(sid);
      return;
    }

    const handoffBtn = target.closest<HTMLElement>("[data-handoff-turn]");
    if (handoffBtn && state.detail) {
      ev.preventDefault();
      ev.stopPropagation();
      const turnId = handoffBtn.dataset.handoffTurn;
      const turn = state.detail.turns.find((t) => t.turn_id === turnId);
      if (turn) {
        if (state.canUpdateContext) void handleAttachSummary(turn);
        else if (state.canSendMessage) void handleSendSummary(turn);
      }
      return;
    }

    const extLink = target.closest<HTMLElement>("[data-open-url]");
    if (extLink) {
      ev.preventDefault();
      const url = extLink.dataset.openUrl;
      if (url && appInstance) {
        void appInstance.openLink({ url }).catch(() => {});
      }
      return;
    }

    const fileBtn = target.closest<HTMLElement>("[data-open-file]");
    if (fileBtn) {
      const fp = fileBtn.dataset.openFile;
      if (fp) void handleOpenFile(fp);
    }
  });

  document.getElementById("main-pane")?.addEventListener("change", (ev) => {
    const target = ev.target as HTMLInputElement | HTMLSelectElement | null;
    if (!target) return;
    if (target.id === "chk-autofollow" && "checked" in target) {
      state.autoFollow = target.checked;
      if (state.autoFollow) scrollDetailToBottom();
      return;
    }
    if (target.id === "sel-routing-mode") {
      void handleRoutingModeChange(target.value as RoutingMode);
      return;
    }
    if (target.id === "sel-ui-locale") {
      void handleUiLocaleChange(target.value as UiLocale);
      return;
    }
    if (target.id === "sel-overview-project") {
      state.overviewProjectFilter = target.value;
      state.selectedHeatmapDate = null;
      void refreshProfileOverview();
      return;
    }
    if (target.id === "sel-default-model") {
      state.defaultModel = target.value;
      void handleUpdateSettings({ default_model: target.value });
      return;
    }
    if (target.id === "sel-escalation-model") {
      state.escalationModel = target.value;
      void handleUpdateSettings({ escalation_model: target.value });
      return;
    }
    if (target.id === "sel-default-effort") {
      const val = target.value as ReasoningEffortSetting;
      state.reasoningEffort = val;
      detailDirty = true;
      render();
      void handleUpdateSettings({ reasoning_effort: val });
      return;
    }
    if (target.id === "chk-guardrail" && "checked" in target) {
      state.quotaGuardrailEnabled = target.checked;
      detailDirty = true;
      render();
      void handleUpdateSettings({ quota_guardrail_enabled: target.checked });
      return;
    }
    if (target.id === "sel-guardrail-threshold") {
      const val = Number.parseInt(target.value, 10);
      if (Number.isFinite(val)) {
        state.quotaGuardrailThresholdPct = val;
        void handleUpdateSettings({ quota_guardrail_threshold_pct: val });
      }
      return;
    }
    if (target.id === "sel-followup-effort") {
      state.followupEffort = target.value as ReasoningEffortSetting;
      return;
    }
    if (target.id === "chk-followup-web" && "checked" in target) {
      state.followupWebSearch = target.checked;
    }
  });

  document.getElementById("main-pane")?.addEventListener("input", (ev) => {
    const target = ev.target as HTMLTextAreaElement | null;
    if (target?.id === "followup-input") {
      state.followupDraft = target.value;
      target.style.height = "auto";
      target.style.height = `${Math.min(target.scrollHeight, 120)}px`;
      const sendBtn = document.getElementById("btn-send-followup") as HTMLButtonElement | null;
      if (sendBtn) {
        const isRunning =
          state.detail?.summary.status === "running" || Boolean(state.detail?.summary.active_run_id);
        sendBtn.disabled = isRunning || state.sendingFollowup || !state.followupDraft.trim();
      }
    }
  });

  document.getElementById("main-pane")?.addEventListener("keydown", (ev) => {
    const target = ev.target as HTMLTextAreaElement | null;
    if (
      target?.id === "followup-input" &&
      ev.key === "Enter" &&
      !ev.shiftKey &&
      !ev.isComposing
    ) {
      ev.preventDefault();
      void handleSendFollowup();
    }
  });

  document.addEventListener("click", (ev) => {
    if (!state.settingsOpen) return;
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (!target.closest("#settings-popover") && !target.closest("#btn-settings")) {
      state.settingsOpen = false;
      detailDirty = true;
      render();
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      if (state.newProjectOpen || state.newChatProject) {
        state.newProjectOpen = false;
        state.newChatProject = null;
        renderModalRoot();
        return;
      }
      if (state.settingsOpen) {
        state.settingsOpen = false;
        detailDirty = true;
        render();
      }
    }
  });

  const modalRoot = document.getElementById("orbit-modal-root");
  modalRoot?.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (
      target.id === "orbit-modal-backdrop" ||
      target.closest("#btn-close-orbit-modal") ||
      target.closest("#btn-cancel-orbit-modal")
    ) {
      state.newProjectOpen = false;
      state.newChatProject = null;
      renderModalRoot();
      return;
    }
    if (target.closest("#btn-submit-new-project")) {
      void handleCreateProject();
      return;
    }
    if (target.closest("#btn-submit-new-chat")) {
      void handleCreateSession();
    }
  });
  modalRoot?.addEventListener("input", (ev) => {
    const target = ev.target as HTMLInputElement | HTMLTextAreaElement | null;
    if (!target) return;
    if (target.id === "new-project-dir-input") {
      state.newProjectDir = target.value;
      const btn = document.getElementById("btn-submit-new-project") as HTMLButtonElement | null;
      if (btn) btn.disabled = state.newProjectSubmitting || !state.newProjectDir.trim();
    } else if (target.id === "new-chat-title-input") {
      state.newChatTitle = target.value;
    } else if (target.id === "new-chat-prompt-input") {
      state.newChatPrompt = target.value;
      const btn = document.getElementById("btn-submit-new-chat") as HTMLButtonElement | null;
      if (btn) btn.disabled = state.newChatSubmitting || !state.newChatPrompt.trim();
    }
  });
  modalRoot?.addEventListener("change", (ev) => {
    const target = ev.target as HTMLInputElement | HTMLSelectElement | null;
    if (!target) return;
    if (target.id === "new-project-create-missing") {
      state.newProjectCreateMissing = (target as HTMLInputElement).checked;
    } else if (target.id === "new-chat-mode-select") {
      state.newChatMode = target.value as IsolationMode;
    } else if (target.id === "new-chat-effort-select") {
      state.newChatEffort = target.value as ReasoningEffortSetting;
    } else if (target.id === "new-chat-web-search") {
      state.newChatWebSearch = (target as HTMLInputElement).checked;
    }
  });
  modalRoot?.addEventListener("keydown", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    if (target.id === "new-project-dir-input" && ev.key === "Enter" && !ev.isComposing) {
      ev.preventDefault();
      void handleCreateProject();
    } else if (
      target.id === "new-chat-prompt-input" &&
      ev.key === "Enter" &&
      !ev.shiftKey &&
      !ev.isComposing
    ) {
      ev.preventDefault();
      void handleCreateSession();
    }
  });
}

function scrollDetailToBottom(): void {
  const scroller = document.getElementById("detail-scroll");
  if (scroller) {
    scroller.scrollTop = scroller.scrollHeight;
  }
}

function render(scrollBottom = false): void {
  const shell = document.getElementById("shell");
  if (shell) {
    shell.classList.toggle("sidebar-open", state.sidebarOpen);
  }
  updateSidebarChrome();

  if (listDirty) {
    const groupsEl = document.getElementById("session-groups");
    if (groupsEl) groupsEl.innerHTML = renderSidebarList();
    listDirty = false;
  }

  if (detailDirty) {
    const mainEl = document.getElementById("main-pane");
    const prevScroll =
      document.getElementById("detail-scroll")?.scrollTop ??
      document.querySelector(".overview-scroll")?.scrollTop ??
      0;
    const prevHmScrollEl = document.querySelector<HTMLElement>(".hm-scroll");
    const prevHmScrollLeft = prevHmScrollEl ? prevHmScrollEl.scrollLeft : null;
    const openFoldKeys = new Set<string>();
    const activeEl = document.activeElement as HTMLTextAreaElement | null;
    const wasFollowupFocused = activeEl?.id === "followup-input";
    const selStart = wasFollowupFocused ? activeEl.selectionStart : null;
    const selEnd = wasFollowupFocused ? activeEl.selectionEnd : null;
    if (mainEl) {
      mainEl.querySelectorAll<HTMLDetailsElement>("details[data-fold-key][open]").forEach((el) => {
        if (el.dataset.foldKey) openFoldKeys.add(el.dataset.foldKey);
      });
      mainEl.innerHTML = renderMainPane();
      if (openFoldKeys.size > 0) {
        mainEl.querySelectorAll<HTMLDetailsElement>("details[data-fold-key]").forEach((el) => {
          if (el.dataset.foldKey && openFoldKeys.has(el.dataset.foldKey)) {
            el.open = true;
          }
        });
      }
      const nextFollowup = document.getElementById("followup-input") as HTMLTextAreaElement | null;
      if (nextFollowup) {
        if (state.followupDraft) {
          nextFollowup.style.height = "auto";
          nextFollowup.style.height = `${Math.min(nextFollowup.scrollHeight, 120)}px`;
        }
        if (wasFollowupFocused && !nextFollowup.disabled) {
          nextFollowup.focus();
          if (selStart !== null && selEnd !== null) {
            nextFollowup.setSelectionRange(selStart, selEnd);
          }
        }
      }
    }
    detailDirty = false;
    if (state.selectedSessionId) {
      if (scrollBottom && state.autoFollow) {
        scrollDetailToBottom();
      } else {
        const scroller = document.getElementById("detail-scroll");
        if (scroller && !scrollBottom) scroller.scrollTop = prevScroll;
      }
    } else {
      const ovScroller = document.querySelector(".overview-scroll");
      if (ovScroller) ovScroller.scrollTop = prevScroll;
      const hmScroller = document.querySelector<HTMLElement>(".hm-scroll");
      if (hmScroller) {
        if (!hmScrollInitialized || prevHmScrollLeft === null) {
          hmScroller.scrollLeft = hmScroller.scrollWidth;
          hmScrollInitialized = true;
        } else {
          hmScroller.scrollLeft = prevHmScrollLeft;
        }
      }
    }
  }
}

async function refreshProfileOverview(): Promise<void> {
  if (!api) return;
  state.loadingOverview = true;
  if (!state.overview) {
    detailDirty = true;
    render();
  }
  try {
    state.overview = await api.getProfileOverview(state.overviewProjectFilter);
    state.syncState = "live";
    state.syncError = null;
  } catch (err) {
    state.syncState = "error";
    state.syncError = err instanceof Error ? err.message : String(err);
  } finally {
    state.loadingOverview = false;
    if (!state.selectedSessionId) {
      detailDirty = true;
      render();
    }
  }
}

async function selectSession(sessionId: string): Promise<void> {
  if (!api) return;
  state.selectedSessionId = sessionId;
  state.showFilesDrawer = false;
  state.followupEffort = "auto";
  state.loadingDetail = true;
  state.detailMetaTs = "";

  const matched = state.sessions.find((s) => s.session_id === sessionId);
  if (matched?.project_dir) {
    state.expandedGroups.add(matched.project_dir);
  }

  listDirty = true;
  detailDirty = true;
  render();

  try {
    const res = await api.getSessionDetail(sessionId);
    if (state.selectedSessionId === sessionId) {
      state.detail = res?.detail ?? null;
      state.detailMetaTs = res?.metaTs ?? "";
      if (state.detail?.summary.project_dir) {
        state.expandedGroups.add(state.detail.summary.project_dir);
        listDirty = true;
      }
      state.syncState = "live";
      state.syncError = null;
    }
  } catch (err) {
    state.syncState = "error";
    state.syncError = err instanceof Error ? err.message : String(err);
  } finally {
    state.loadingDetail = false;
    detailDirty = true;
    render(state.autoFollow);
  }
}

function applySessionsSnapshot(res: {
  sessions: SessionSummary[];
  projects?: ProjectEntry[];
  sessions_dir: string;
  sessions_dir_exists: boolean;
}): { prevRunningIds: Set<string>; newlyRunning: SessionSummary[] } {
  const prevRunningIds = state.seenRunningIds;
  const nextRunning = res.sessions.filter((s) => s.status === "running");
  const newlyRunning = nextRunning.filter((s) => !prevRunningIds.has(s.session_id));

  state.sessions = res.sessions;
  state.projects = res.projects ?? [];
  state.sessionsDir = res.sessions_dir;
  state.sessionsDirExists = res.sessions_dir_exists;
  state.seenRunningIds = new Set(nextRunning.map((s) => s.session_id));
  state.syncState = "live";
  state.syncError = null;
  listDirty = true;
  return { prevRunningIds, newlyRunning };
}

async function refreshSessions(forceDetailPoll: boolean): Promise<void> {
  if (!api) return;
  try {
    const res = await api.listSessions(state.query, 100);
    const { prevRunningIds, newlyRunning } = applySessionsSnapshot(res);
    applySettingsSnapshot(res);

    // Auto-latch onto a running session when focus_running was requested
    // or when a brand-new running task starts while viewing the right aux pane.
    let autoSelectedId: string | null = null;
    if (state.focusRunningPending) {
      const target = findBestRunningSession(state.sessions, state.preferredWorkspace);
      if (target) {
        state.focusRunningPending = false;
        state.focusRunningTicks = 0;
        if (state.selectedSessionId !== target.session_id) {
          autoSelectedId = target.session_id;
        }
      } else {
        state.focusRunningTicks += 1;
        if (state.focusRunningTicks >= 20) {
          state.focusRunningPending = false;
          state.focusRunningTicks = 0;
        }
      }
    } else if (
      state.autoFollow &&
      isAuxPaneSurface() &&
      newlyRunning.length > 0 &&
      state.detail?.summary.status !== "running"
    ) {
      const target = findBestRunningSession(newlyRunning, state.preferredWorkspace);
      if (target && state.selectedSessionId !== target.session_id) {
        autoSelectedId = target.session_id;
      }
    }

    if (autoSelectedId) {
      await selectSession(autoSelectedId);
      return;
    }

    // Refresh overview banner if currently on Overview and running status changed.
    if (!state.selectedSessionId && (newlyRunning.length > 0 || prevRunningIds.size !== state.seenRunningIds.size)) {
      detailDirty = true;
    }

    const hasCurrent =
      state.selectedSessionId &&
      state.sessions.some((s) => s.session_id === state.selectedSessionId);

    if (forceDetailPoll && state.selectedSessionId && hasCurrent) {
      await pollActiveDetail();
    }
    render();
  } catch (err) {
    state.syncState = "error";
    state.syncError = err instanceof Error ? err.message : String(err);
    detailDirty = true;
    render();
  }
}

async function pollActiveDetail(): Promise<void> {
  if (!api || !state.selectedSessionId || !state.detail) return;
  const sid = state.selectedSessionId;
  const polled = await api.pollSessionDelta(sid, state.detail.cursor, state.detailMetaTs);
  if (!polled || state.selectedSessionId !== sid) return;
  const { delta, metaTs } = polled;

  state.syncState = "live";
  state.syncError = null;
  state.detailMetaTs = metaTs;

  if (!delta.changed) return;

  if (delta.session_id && delta.session_id !== sid) {
    state.selectedSessionId = delta.session_id;
    state.seenRunningIds.add(delta.session_id);
    const oldPrefix = `${sid}:`;
    const newPrefix = `${delta.session_id}:`;
    const migrated: string[] = [];
    for (const k of state.expandedCommandTurns) {
      if (k.startsWith(oldPrefix)) {
        migrated.push(k);
      }
    }
    for (const k of migrated) {
      state.expandedCommandTurns.delete(k);
      state.expandedCommandTurns.add(`${newPrefix}${k.slice(oldPrefix.length)}`);
    }
  }

  if (delta.reset) {
    state.detail = {
      ...state.detail,
      summary: delta.summary ?? state.detail.summary,
      turns: delta.updated_turns,
      cursor: delta.cursor,
      file_size: delta.file_size,
      total_commands: delta.total_commands,
      total_changed_files: delta.total_changed_files,
      all_changed_files: delta.all_changed_files,
    };
  } else {
    const byId = new Map<string, TurnDetail>(
      state.detail.turns.map((t) => [t.turn_id, t]),
    );
    const order = state.detail.turns.map((t) => t.turn_id);
    for (const updated of delta.updated_turns) {
      if (!byId.has(updated.turn_id)) order.push(updated.turn_id);
      byId.set(updated.turn_id, updated);
    }
    const mergedTurns = order
      .map((id) => byId.get(id))
      .filter((t): t is TurnDetail => Boolean(t));

    state.detail = {
      ...state.detail,
      summary: delta.summary ?? state.detail.summary,
      turns: mergedTurns,
      cursor: delta.cursor,
      file_size: delta.file_size,
      total_commands: delta.total_commands,
      total_changed_files: delta.total_changed_files,
      all_changed_files: delta.all_changed_files,
    };
  }

  if (delta.summary) {
    const idx = state.sessions.findIndex(
      (s) => s.session_id === sid || (delta.session_id && s.session_id === delta.session_id),
    );
    if (idx >= 0) {
      state.sessions[idx] = delta.summary;
      listDirty = true;
    }
  }
  detailDirty = true;
  render(true);
}

function scheduleNextPoll(): void {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  const activeRunning =
    state.detail?.summary.status === "running" ||
    state.sessions.some((s) => s.status === "running");
  const delayMs = state.focusRunningPending
    ? (state.focusRunningTicks < 5 ? 400 : 800)
    : activeRunning
      ? 1500
      : 3000;
  pollTimer = window.setTimeout(() => {
    void runPollTick();
  }, delayMs);
}

async function refreshQuota(forceRefresh = false): Promise<void> {
  if (!api) return;
  try {
    state.quota = await api.getQuota(forceRefresh);
    detailDirty = true;
    render();
  } catch {
    // Non-fatal: quota pill stays hidden when unavailable.
  }
}

async function runPollTick(): Promise<void> {
  if (pollInFlight || !api) {
    scheduleNextPoll();
    return;
  }
  pollInFlight = true;
  pollTick += 1;
  try {
    if (state.selectedSessionId && state.detail) {
      await pollActiveDetail();
    }
    if (state.focusRunningPending || pollTick % 2 === 0) {
      await refreshSessions(false);
    }
    if (!state.selectedSessionId && pollTick % 5 === 0) {
      await refreshProfileOverview();
    }
    if (pollTick % 25 === 0) {
      await refreshQuota(false);
    }
  } catch (err) {
    state.syncState = "error";
    state.syncError = err instanceof Error ? err.message : String(err);
    detailDirty = true;
    render();
  } finally {
    pollInFlight = false;
    scheduleNextPoll();
  }
}

function applyBootstrap(data: OpenOrbitBootstrap | undefined): void {
  if (!data) return;
  if (data.initial_query) {
    state.query = data.initial_query;
    const input = document.getElementById("search-input") as HTMLInputElement | null;
    if (input) input.value = data.initial_query;
  }
  if (data.preferred_workspace !== undefined) {
    state.preferredWorkspace = data.preferred_workspace || null;
  }
  if (data.initial_session_id) {
    state.focusRunningPending = false;
    state.focusRunningTicks = 0;
    void selectSession(data.initial_session_id);
    return;
  }
  if (data.focus_running) {
    const running = findBestRunningSession(state.sessions, state.preferredWorkspace);
    if (running) {
      state.focusRunningPending = false;
      state.focusRunningTicks = 0;
      void selectSession(running.session_id);
    } else {
      state.focusRunningPending = true;
      state.focusRunningTicks = 0;
      scheduleNextPoll();
      void refreshSessions(false);
    }
  }
}

async function boot(): Promise<void> {
  applyLocaleToDocument();
  mountShell();
  render();

  const app = new App({ name: "Codex Orbit", version: "0.1.0" }, {});
  appInstance = app;
  api = new OrbitApi(app);

  // Register handlers before connect() so no initial notification is missed.
  app.onhostcontextchanged = (ctx) => {
    if (ctx.theme) applyDocumentTheme(ctx.theme);
    if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
    if (ctx.locale && ctx.locale !== state.hostLocale) {
      state.hostLocale = ctx.locale;
      applyLocaleToDocument();
      listDirty = true;
      detailDirty = true;
      render();
    }
  };

  app.ontoolinput = ({ arguments: args }) => {
    const a = args as
      | { session_id?: string; query?: string; focus_running?: boolean; workspace_dir?: string }
      | undefined;
    if (a?.query) {
      state.query = a.query;
      const input = document.getElementById("search-input") as HTMLInputElement | null;
      if (input) input.value = a.query;
    }
    if (a?.workspace_dir) {
      state.preferredWorkspace = a.workspace_dir;
    }
    if (a?.session_id) {
      state.selectedSessionId = a.session_id;
    } else if (a?.focus_running) {
      state.focusRunningPending = true;
      state.focusRunningTicks = 0;
    }
  };

  app.ontoolresult = (result) => {
    const sc = (result as { structuredContent?: OpenOrbitBootstrap }).structuredContent;
    applyBootstrap(sc);
  };

  await app.connect();
  const ctx = app.getHostContext();
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
  if (ctx?.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx?.locale) {
    state.hostLocale = ctx.locale;
    applyLocaleToDocument();
  }

  const caps = app.getHostCapabilities();
  state.canSendMessage = Boolean(caps?.message);
  state.canUpdateContext = Boolean(caps?.updateModelContext);

  const includeOverview = !state.selectedSessionId && !state.focusRunningPending;
  let bootQuotaStale = false;
  let bootHasQuota = false;
  try {
    const res = await api.bootstrap(
      state.query,
      100,
      includeOverview,
      state.overviewProjectFilter,
    );
    applySessionsSnapshot(res);
    applySettingsSnapshot(res.settings);
    if (res.quota) {
      state.quota = res.quota;
      bootHasQuota = true;
      detailDirty = true;
    }
    bootQuotaStale = Boolean(res.quota_stale);
    if (res.overview) {
      state.overview = res.overview;
      detailDirty = true;
    }
    render();
  } catch (err) {
    state.syncState = "error";
    state.syncError = err instanceof Error ? err.message : String(err);
    detailDirty = true;
    render();
  }

  if (state.selectedSessionId) {
    await selectSession(state.selectedSessionId);
  } else if (isAuxPaneSurface()) {
    const activeRunning = findBestRunningSession(state.sessions, state.preferredWorkspace);
    if (activeRunning) {
      state.focusRunningPending = false;
      state.focusRunningTicks = 0;
      await selectSession(activeRunning.session_id);
    } else if (!state.overview) {
      render();
    }
  } else if (!state.overview) {
    render();
  }

  if (bootQuotaStale || !bootHasQuota) {
    void refreshQuota(false);
  }
  scheduleNextPoll();
}

void boot();
