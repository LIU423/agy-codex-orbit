import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";
import { OrbitApi } from "./api";
import "./styles.css";
import type {
  AssistantBlock,
  CommandRecord,
  FileChangeRecord,
  OpenOrbitBootstrap,
  SessionDetail,
  SessionStatus,
  SessionSummary,
  TurnDetail,
} from "./types";

interface UiState {
  sessions: SessionSummary[];
  sessionsDir: string;
  sessionsDirExists: boolean;
  query: string;
  selectedSessionId: string | null;
  detail: SessionDetail | null;
  autoFollow: boolean;
  showFilesDrawer: boolean;
  sidebarOpen: boolean;
  collapsedGroups: Set<string>;
  syncState: "connecting" | "live" | "error";
  syncError: string | null;
  loadingDetail: boolean;
}

const state: UiState = {
  sessions: [],
  sessionsDir: "",
  sessionsDirExists: true,
  query: "",
  selectedSessionId: null,
  detail: null,
  autoFollow: true,
  showFilesDrawer: false,
  sidebarOpen: false,
  collapsedGroups: new Set<string>(),
  syncState: "connecting",
  syncError: null,
  loadingDetail: false,
};

let api: OrbitApi | null = null;
let pollTimer: number | null = null;
let pollInFlight = false;
let pollTick = 0;
let searchDebounce: number | null = null;
let listDirty = true;
let detailDirty = true;

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderSafeMarkdown(raw: string): string {
  const segments = raw.split(/```/);
  const htmlParts: string[] = [];
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    if (i % 2 === 1) {
      const nl = seg.indexOf("\n");
      const code = nl >= 0 ? seg.slice(nl + 1) : seg;
      htmlParts.push(`<pre><code>${escapeHtml(code)}</code></pre>`);
    } else {
      const paragraphs = seg
        .split(/\n{2,}/)
        .map((p) => p.trim())
        .filter(Boolean);
      for (const p of paragraphs) {
        const inline = escapeHtml(p)
          .replace(/`([^`\n]+)`/g, "<code>$1</code>")
          .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
          .replace(/\n/g, "<br>");
        htmlParts.push(`<p>${inline}</p>`);
      }
    }
  }
  return htmlParts.join("") || `<p>${escapeHtml(raw)}</p>`;
}

function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined || ms < 0) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const mins = Math.floor(secs / 60);
  const rem = Math.round(secs % 60);
  return `${mins}m ${rem}s`;
}

function formatRelativeTime(iso?: string | null): string {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  const d = new Date(ts);
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${m}-${day}`;
}

function statusLabel(status: SessionStatus): string {
  switch (status) {
    case "running":
      return "运行中";
    case "completed":
      return "已完成";
    case "aborted":
      return "已中止";
    default:
      return "空闲";
  }
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

function groupByProject(sessions: SessionSummary[]): Array<{
  projectDir: string;
  projectName: string;
  items: SessionSummary[];
}> {
  const map = new Map<string, { projectDir: string; projectName: string; items: SessionSummary[] }>();
  for (const s of sessions) {
    const key = s.project_dir || "(Unknown Workspace)";
    let entry = map.get(key);
    if (!entry) {
      entry = {
        projectDir: key,
        projectName: s.project_name || key,
        items: [],
      };
      map.set(key, entry);
    }
    entry.items.push(s);
  }
  return Array.from(map.values());
}

function renderSidebarList(): string {
  if (!state.sessionsDirExists) {
    return `<div class="empty-state"><strong>未找到 Codex 会话目录</strong><span>${escapeHtml(state.sessionsDir)}</span></div>`;
  }
  if (state.sessions.length === 0) {
    return `<div class="empty-state"><strong>${state.query ? "无匹配会话" : "暂无本地 Codex 会话"}</strong></div>`;
  }

  const groups = groupByProject(state.sessions);
  return groups
    .map((g) => {
      const collapsed = state.collapsedGroups.has(g.projectDir);
      const itemsHtml = collapsed
        ? ""
        : g.items
            .map((s) => {
              const active = s.session_id === state.selectedSessionId ? " active" : "";
              const modelChip = s.model
                ? `<span class="meta-chip" title="${escapeHtml(s.model)}">${escapeHtml(s.model)}</span>`
                : "";
              const roundText = `${s.round_count} 轮`;
              const timeText = formatRelativeTime(s.updated_at);
              return `
                <button class="session-item${active}" data-session-id="${escapeHtml(s.session_id)}" type="button">
                  <div class="session-row-top">
                    <span class="dot ${s.status}" aria-label="${statusLabel(s.status)}"></span>
                    <span class="session-title">${escapeHtml(s.title)}</span>
                  </div>
                  <div class="session-meta">
                    ${modelChip}
                    <span>${roundText}</span>
                    <span class="meta-time">${escapeHtml(timeText)}</span>
                  </div>
                </button>`;
            })
            .join("");

      return `
        <div class="group-block">
          <button class="group-header" data-group-dir="${escapeHtml(g.projectDir)}" title="${escapeHtml(g.projectDir)}" type="button">
            <span class="group-name">${collapsed ? "▸" : "▾"} ${escapeHtml(g.projectName)}</span>
            <span class="group-count">${g.items.length}</span>
          </button>
          ${itemsHtml}
        </div>`;
    })
    .join("");
}

function renderCommandRow(turnId: string, cmd: CommandRecord): string {
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
    : `<pre class="cmd-output">(无输出)</pre>`;

  return `
    <details class="cmd-row" data-fold-key="cmd:${escapeHtml(turnId)}:${escapeHtml(cmd.id)}">
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
  return `<div class="${cls}">${renderSafeMarkdown(blk.text)}</div>`;
}

function renderFileChip(fc: FileChangeRecord): string {
  const op = fileOpLabel(fc.change_type);
  return `<span class="file-chip" title="${escapeHtml(fc.path)}"><span class="file-op ${op.cls}">${op.text}</span><span>${escapeHtml(shortFileName(fc.path))}</span></span>`;
}

function renderTurnCard(turn: TurnDetail, isLatest: boolean): string {
  const roundLabel = turn.is_delegated
    ? `第 ${turn.round_index} 轮派单`
    : `第 ${turn.round_index} 轮`;
  const sourceBadge = turn.source_model
    ? `<span class="source-tag">${escapeHtml(turn.source_model)}</span>`
    : "";
  const promptOneLine = turn.prompt
    ? turn.prompt.replace(/\s+/g, " ").trim()
    : "(无输入正文)";
  const modelMeta = turn.model ? `<span>${escapeHtml(turn.model)}</span>` : "";
  const durMeta = turn.duration_ms ? `<span>${formatDuration(turn.duration_ms)}</span>` : "";

  const reasoningHtml =
    turn.reasoning_summaries.length > 0
      ? `
        <details class="reasoning-box" data-fold-key="reasoning:${escapeHtml(turn.turn_id)}">
          <summary>
            <span class="chevron">▸</span>
            <span>思考摘要（${turn.reasoning_summaries.length}）</span>
          </summary>
          <div class="reasoning-list">
            ${turn.reasoning_summaries.map((r) => `<div>${escapeHtml(r)}</div>`).join("")}
          </div>
        </details>`
      : "";

  const messagesHtml = turn.assistant_messages.map(renderAssistantBlock).join("");

  const commandsHtml =
    turn.commands.length > 0
      ? `
        <div class="cmd-section">
          <div class="cmd-header">
            <span>命令执行</span>
            <span>${turn.commands.length}</span>
          </div>
          ${turn.commands.map((c) => renderCommandRow(turn.turn_id, c)).join("")}
        </div>`
      : "";

  const filesHtml =
    turn.changed_files.length > 0
      ? `<div class="turn-files">${turn.changed_files.map(renderFileChip).join("")}</div>`
      : "";

  const errorHtml = turn.error_reason
    ? `<div class="error-banner">已中止：${escapeHtml(turn.error_reason)}</div>`
    : "";

  const openAttr = isLatest && !turn.assistant_messages.length && !turn.commands.length ? " open" : "";

  return `
    <article class="turn-card" data-turn-id="${escapeHtml(turn.turn_id)}">
      <details class="prompt-details" data-fold-key="prompt:${escapeHtml(turn.turn_id)}"${openAttr}>
        <summary class="prompt-summary">
          <span class="chevron">▸</span>
          <span class="round-tag">${roundLabel}</span>
          ${sourceBadge}
          <span class="prompt-preview">${escapeHtml(promptOneLine)}</span>
          <span class="turn-right-meta">
            ${modelMeta}
            ${durMeta}
            <span class="dot dot-inline ${turn.status}" title="${statusLabel(turn.status)}"></span>
          </span>
        </summary>
        <div class="prompt-body">${escapeHtml(turn.prompt || "(无输入正文)")}</div>
      </details>
      <div class="turn-body">
        ${errorHtml}
        ${reasoningHtml}
        ${messagesHtml}
        ${commandsHtml}
        ${filesHtml}
      </div>
    </article>`;
}

function renderDetailPane(): string {
  const d = state.detail;
  const syncBadgeHtml =
    state.syncState === "error"
      ? `<span class="sync-badge error" title="${escapeHtml(state.syncError || "")}">同步异常</span>`
      : `<span class="sync-badge"><span class="dot dot-inline ${d?.summary.status === "running" ? "running" : "completed"}"></span><span>${state.syncState === "connecting" ? "连接中" : "实时同步"}</span></span>`;

  const topTitle = d ? d.summary.title : "Codex Orbit";
  const topSubParts: string[] = [];
  if (d) {
    topSubParts.push(d.summary.project_dir);
    if (d.summary.git_branch) topSubParts.push(`@${d.summary.git_branch}`);
  }
  const topSub = topSubParts.join("  ·  ");

  const topbar = `
    <header class="topbar">
      <div class="topbar-left">
        <button class="icon-btn sidebar-toggle" id="btn-sidebar" type="button" aria-label="切换会话列表">☰</button>
        <div class="topbar-title-wrap">
          <div class="topbar-title" title="${escapeHtml(topTitle)}">${escapeHtml(topTitle)}</div>
          ${topSub ? `<div class="topbar-sub" title="${escapeHtml(topSub)}">${escapeHtml(topSub)}</div>` : ""}
        </div>
      </div>
      <div class="topbar-controls">
        ${syncBadgeHtml}
        <label class="toggle-label">
          <input id="chk-autofollow" type="checkbox" ${state.autoFollow ? "checked" : ""} />
          <span>自动跟随</span>
        </label>
      </div>
    </header>`;

  if (state.loadingDetail && !d) {
    return `${topbar}<div class="empty-state"><strong>加载会话中…</strong></div>`;
  }

  if (!d) {
    return `${topbar}<div class="empty-state"><strong>选择左侧会话查看实时执行详情</strong></div>`;
  }

  const totalDurationMs = d.turns.reduce((acc, t) => acc + (t.duration_ms || 0), 0);
  const summaryBar = `
    <div class="summary-bar">
      <span class="status-pill ${d.summary.status}">
        <span class="dot dot-inline ${d.summary.status}"></span>
        <span>${statusLabel(d.summary.status)}</span>
      </span>
      <span class="metric">轮次 <strong>${d.turns.length}</strong></span>
      <span class="metric">命令 <strong>${d.total_commands}</strong></span>
      <button class="metric-btn" id="btn-files-drawer" type="button">
        改动文件 <strong>${d.total_changed_files}</strong>${d.total_changed_files > 0 ? (state.showFilesDrawer ? " ▴" : " ▾") : ""}
      </button>
      <span class="metric">耗时 <strong>${formatDuration(totalDurationMs || d.summary.duration_ms)}</strong></span>
    </div>`;

  const filesDrawer =
    state.showFilesDrawer && d.all_changed_files.length > 0
      ? `<div class="files-drawer">${d.all_changed_files.map(renderFileChip).join("")}</div>`
      : "";

  const turnsHtml =
    d.turns.length > 0
      ? d.turns.map((t, idx) => renderTurnCard(t, idx === d.turns.length - 1)).join("")
      : `<div class="empty-state"><strong>会话尚无执行记录</strong></div>`;

  return `${topbar}${summaryBar}${filesDrawer}<section class="detail-scroll" id="detail-scroll">${turnsHtml}</section>`;
}

function mountShell(): void {
  const root = document.getElementById("app");
  if (!root) return;
  root.innerHTML = `
    <div class="shell" id="shell">
      <aside class="sidebar">
        <div class="sidebar-top">
          <input id="search-input" class="search-input" type="search" placeholder="搜索会话、目录或模型…" autocomplete="off" />
          <button id="btn-refresh" class="icon-btn" type="button" title="刷新会话列表" aria-label="刷新会话列表">↻</button>
        </div>
        <div id="session-groups" class="session-groups"></div>
      </aside>
      <main id="main-pane" class="main"></main>
    </div>`;

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
  });

  document.getElementById("session-groups")?.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement | null;
    if (!target) return;
    const groupBtn = target.closest<HTMLElement>("[data-group-dir]");
    if (groupBtn) {
      const dir = groupBtn.dataset.groupDir || "";
      if (state.collapsedGroups.has(dir)) state.collapsedGroups.delete(dir);
      else state.collapsedGroups.add(dir);
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
    if (target.closest("#btn-files-drawer")) {
      if (state.detail && state.detail.all_changed_files.length > 0) {
        state.showFilesDrawer = !state.showFilesDrawer;
        detailDirty = true;
        render();
      }
    }
  });

  document.getElementById("main-pane")?.addEventListener("change", (ev) => {
    const target = ev.target as HTMLInputElement | null;
    if (target?.id === "chk-autofollow") {
      state.autoFollow = target.checked;
      if (state.autoFollow) scrollDetailToBottom();
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
  if (listDirty) {
    const groupsEl = document.getElementById("session-groups");
    if (groupsEl) groupsEl.innerHTML = renderSidebarList();
    listDirty = false;
  }
  if (detailDirty) {
    const mainEl = document.getElementById("main-pane");
    const prevScroll = document.getElementById("detail-scroll")?.scrollTop ?? 0;
    const openFoldKeys = new Set<string>();
    if (mainEl) {
      mainEl.querySelectorAll<HTMLDetailsElement>("details[data-fold-key][open]").forEach((el) => {
        if (el.dataset.foldKey) openFoldKeys.add(el.dataset.foldKey);
      });
      mainEl.innerHTML = renderDetailPane();
      if (openFoldKeys.size > 0) {
        mainEl.querySelectorAll<HTMLDetailsElement>("details[data-fold-key]").forEach((el) => {
          if (el.dataset.foldKey && openFoldKeys.has(el.dataset.foldKey)) {
            el.open = true;
          }
        });
      }
    }
    detailDirty = false;
    if (scrollBottom && state.autoFollow) {
      scrollDetailToBottom();
    } else {
      const scroller = document.getElementById("detail-scroll");
      if (scroller && !scrollBottom) scroller.scrollTop = prevScroll;
    }
  }
}

async function selectSession(sessionId: string): Promise<void> {
  if (!api) return;
  state.selectedSessionId = sessionId;
  state.showFilesDrawer = false;
  state.loadingDetail = true;
  listDirty = true;
  detailDirty = true;
  render();

  try {
    const detail = await api.getSessionDetail(sessionId);
    if (state.selectedSessionId === sessionId) {
      state.detail = detail;
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
    const res = await api.listSessions(state.query, 80);
    state.sessions = res.sessions;
    state.sessionsDir = res.sessions_dir;
    state.sessionsDirExists = res.sessions_dir_exists;
    state.syncState = "live";
    state.syncError = null;
    listDirty = true;

    const hasCurrent =
      state.selectedSessionId &&
      state.sessions.some((s) => s.session_id === state.selectedSessionId);

    if (!state.selectedSessionId && state.sessions.length > 0) {
      await selectSession(state.sessions[0].session_id);
      return;
    }

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
  const delta = await api.pollSessionDelta(sid, state.detail.cursor);
  if (!delta || state.selectedSessionId !== sid) return;

  state.syncState = "live";
  state.syncError = null;

  if (!delta.changed) return;

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
    const idx = state.sessions.findIndex((s) => s.session_id === sid);
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
  const delayMs = activeRunning ? 1500 : 3000;
  pollTimer = window.setTimeout(() => {
    void runPollTick();
  }, delayMs);
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
    if (pollTick % 2 === 0 || !state.selectedSessionId) {
      await refreshSessions(false);
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
  if (data.initial_session_id) {
    void selectSession(data.initial_session_id);
  }
}

async function boot(): Promise<void> {
  mountShell();
  render();

  const app = new App({ name: "Codex Orbit", version: "0.1.0" }, {});
  api = new OrbitApi(app);

  // Register handlers before connect() so no initial notification is missed.
  app.onhostcontextchanged = (ctx) => {
    if (ctx.theme) applyDocumentTheme(ctx.theme);
    if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  };

  app.ontoolinput = ({ arguments: args }) => {
    const a = args as { session_id?: string; query?: string } | undefined;
    if (a?.query) {
      state.query = a.query;
      const input = document.getElementById("search-input") as HTMLInputElement | null;
      if (input) input.value = a.query;
    }
    if (a?.session_id) {
      state.selectedSessionId = a.session_id;
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

  await refreshSessions(false);
  if (state.selectedSessionId && !state.detail) {
    await selectSession(state.selectedSessionId);
  }
  scheduleNextPoll();
}

void boot();
