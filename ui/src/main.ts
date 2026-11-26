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
  FileChangeRecord,
  HeatmapMetric,
  OpenOrbitBootstrap,
  OriginFilter,
  ProfileOverviewResponse,
  QuotaStatus,
  ResolvedLocale,
  RoutingMode,
  SessionDetail,
  SessionSummary,
  TurnDetail,
  UiLocale,
} from "./types";

interface UiState {
  sessions: SessionSummary[];
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
}

const state: UiState = {
  sessions: [],
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
  document.documentElement.lang = loc === "zh" ? "zh-CN" : "en";
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
    const roundNo = turn.delegation_round ?? turn.round_index;
    lines.push(str.summaryRoundHeading(roundNo));
  } else {
    lines.push(str.summarySessionHeading(s.title));
  }

  lines.push(`- **Session ID**: \`${s.session_id}\``);
  lines.push(`- **${str.summaryWorkspace}**: \`${s.worktree_path || s.project_dir}\``);
  if (s.isolation_mode) {
    lines.push(`- **${str.summaryIsolation}**: ${isolationLabel(s.isolation_mode, loc)}`);
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

function groupByProject(sessions: SessionSummary[]): Array<{
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
  return Array.from(map.values());
}

function renderSidebarList(): string {
  const loc = activeLocale();
  const str = getStrings(loc);

  if (!state.sessionsDirExists) {
    return `<div class="empty-state"><strong>${escapeHtml(str.noSessionsDir)}</strong><span>${escapeHtml(state.sessionsDir)}</span></div>`;
  }
  if (state.sessions.length === 0) {
    return `<div class="empty-state"><strong>${escapeHtml(state.query ? str.noMatchingSessions : str.noLocalSessions)}</strong></div>`;
  }

  const isSearching = state.query.trim().length > 0;
  const groups = groupByProject(state.sessions);

  return groups
    .map((g) => {
      const expanded = isSearching || state.expandedGroups.has(g.projectDir);
      const itemsHtml = !expanded
        ? ""
        : `<div class="group-items">${g.items
            .map((s) => {
              const active = s.session_id === state.selectedSessionId ? " active" : "";
              const delegatedChip = s.is_delegated
                ? `<span class="meta-chip delegated" title="${escapeHtml(str.delegatedByTip(s.source_model || "Gemini"))}">AGY</span>`
                : "";
              const modelChip = s.model
                ? `<span class="meta-chip" title="${escapeHtml(s.model)}">${escapeHtml(s.model)}</span>`
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
                    <span>${escapeHtml(str.roundsShort(s.round_count))}</span>
                    <span class="meta-time">${escapeHtml(timeText)}</span>
                  </div>
                </button>`;
            })
            .join("")}</div>`;

      const runningDot = g.hasRunning ? `<span class="dot dot-inline running"></span>` : "";

      return `
        <div class="group-block">
          <button class="group-header" data-group-dir="${escapeHtml(g.projectDir)}" title="${escapeHtml(g.projectDir)}" type="button" aria-expanded="${expanded}">
            <span class="group-chevron">${expanded ? "▾" : "▸"}</span>
            <span class="group-name">${escapeHtml(g.projectName)}</span>
            ${runningDot}
            <span class="group-count">${g.items.length}</span>
          </button>
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
    { value: "zh", label: "中文" },
    { value: "en", label: "EN" },
  ];
  const optionsHtml = opts
    .map(
      (o) =>
        `<option value="${o.value}"${state.uiLocale === o.value ? " selected" : ""}>${escapeHtml(o.label)}</option>`,
    )
    .join("");
  return `<select id="sel-ui-locale" class="ctrl-select" aria-label="${escapeHtml(str.languageAria)}" title="${escapeHtml(str.languageAria)}">${optionsHtml}</select>`;
}

function renderQuotaBadge(): string {
  const loc = activeLocale();
  const str = getStrings(loc);
  const q = state.quota?.codex;
  if (!q || !q.available) return "";
  const parts: string[] = [];
  const tipLines: string[] = [];
  if (q.primary) {
    parts.push(`5h ${Math.round(q.primary.remaining_percent)}%`);
    tipLines.push(
      str.quota5hTip(q.primary.remaining_percent, q.primary.resets_at_local || q.primary.resets_in || "-"),
    );
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
  return `<button class="quota-badge" id="btn-refresh-quota" type="button" title="${escapeHtml(tipLines.join("\n"))}">${escapeHtml(parts.join(" · "))}</button>`;
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
      </div>
    </header>`;
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
              <span class="insight-val">${escapeHtml(routingModeLabel(state.routingMode, loc))}</span>
            </div>
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
  const displayRound = turn.delegation_round ?? turn.round_index;
  const roundLabel = str.roundTag(displayRound, turn.is_delegated);
  const sourceBadge = turn.source_model
    ? `<span class="source-tag">${escapeHtml(turn.source_model)}</span>`
    : "";
  const isoText = isolationLabel(turn.isolation_mode, loc);
  const isoBadge = isoText
    ? `<span class="iso-tag" title="${escapeHtml(turn.worktree_path || isoText)}">${escapeHtml(isoText)}</span>`
    : "";

  const modelMeta = turn.model ? `<span>${escapeHtml(turn.model)}</span>` : "";
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

  const errorHtml = turn.error_reason
    ? `<div class="error-banner">${escapeHtml(str.abortedPrefix + turn.error_reason)}</div>`
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
    </div>`;
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
          <button id="btn-refresh" class="icon-btn" type="button" title="${escapeHtml(str.refreshSessions)}" aria-label="${escapeHtml(str.refreshSessions)}">↻</button>
        </div>
        <div id="session-groups" class="session-groups"></div>
      </aside>
      <main id="main-pane" class="main"></main>
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

  document.getElementById("session-groups")?.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
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

async function refreshSessions(forceDetailPoll: boolean): Promise<void> {
  if (!api) return;
  try {
    const res = await api.listSessions(state.query, 100);
    const prevRunningIds = state.seenRunningIds;
    const nextRunning = res.sessions.filter((s) => s.status === "running");
    const newlyRunning = nextRunning.filter((s) => !prevRunningIds.has(s.session_id));

    state.sessions = res.sessions;
    state.sessionsDir = res.sessions_dir;
    state.sessionsDirExists = res.sessions_dir_exists;
    state.seenRunningIds = new Set(nextRunning.map((s) => s.session_id));

    if (res.routing_mode && res.routing_mode !== state.routingMode) {
      state.routingMode = res.routing_mode;
      detailDirty = true;
    }
    if (res.ui_locale && res.ui_locale !== state.uiLocale) {
      state.uiLocale = res.ui_locale;
      applyLocaleToDocument();
      detailDirty = true;
    }
    state.syncState = "live";
    state.syncError = null;
    listDirty = true;

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
  const delayMs = state.focusRunningPending ? 800 : activeRunning ? 1500 : 3000;
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

  const app = new App({ name: "Codex Orbit", version: "0.6.1" }, {});
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

  await refreshSessions(false);
  if (state.selectedSessionId) {
    await selectSession(state.selectedSessionId);
  } else if (isAuxPaneSurface()) {
    const activeRunning = findBestRunningSession(state.sessions, state.preferredWorkspace);
    if (activeRunning) {
      state.focusRunningPending = false;
      await selectSession(activeRunning.session_id);
    } else {
      await refreshProfileOverview();
    }
  } else {
    await refreshProfileOverview();
  }
  void refreshQuota(false);
  scheduleNextPoll();
}

void boot();
