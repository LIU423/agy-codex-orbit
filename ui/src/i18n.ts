import type {
  IsolationMode,
  ResolvedLocale,
  RoutingMode,
  SessionStatus,
  UiLocale,
} from "./types";

export function resolveLocale(setting: UiLocale | string, hostLocale?: string | null): ResolvedLocale {
  if (setting === "zh-CN" || setting === "zh") return "zh-CN";
  if (setting === "zh-TW") return "zh-TW";
  if (setting === "en" || setting === "ja") return setting;
  const raw = (hostLocale || navigator.language || "en").toLowerCase().replace(/_/g, "-");
  if (raw.startsWith("zh")) {
    if (raw.includes("tw") || raw.includes("hk") || raw.includes("mo") || raw.includes("hant")) {
      return "zh-TW";
    }
    return "zh-CN";
  }
  if (raw.startsWith("ja")) return "ja";
  return "en";
}

export function formatCompactNumber(n: number, locale: ResolvedLocale): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (locale === "zh-CN") {
    if (n >= 1e8) return `${(n / 1e8).toFixed(1)}亿`;
    if (n >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
    return n.toLocaleString("zh-CN");
  }
  if (locale === "zh-TW") {
    if (n >= 1e8) return `${(n / 1e8).toFixed(1)}億`;
    if (n >= 1e4) return `${(n / 1e4).toFixed(1)}萬`;
    return n.toLocaleString("zh-TW");
  }
  if (locale === "ja") {
    if (n >= 1e8) return `${(n / 1e8).toFixed(1)}億`;
    if (n >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
    return n.toLocaleString("ja-JP");
  }
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toLocaleString("en-US");
}

export function formatDays(days: number, locale: ResolvedLocale): string {
  if (locale === "zh-CN" || locale === "zh-TW") return `${days} 天`;
  if (locale === "ja") return `${days}日`;
  return days === 1 ? "1 day" : `${days} days`;
}

export function monthLabel(monthZeroBased: number, locale: ResolvedLocale): string {
  if (locale === "zh-CN" || locale === "zh-TW" || locale === "ja") return `${monthZeroBased + 1}月`;
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return names[monthZeroBased] ?? "";
}

export interface Strings {
  overview: string;
  searchPlaceholder: string;
  refreshSessions: string;
  toggleSidebar: string;
  noSessionsDir: string;
  noMatchingSessions: string;
  noLocalSessions: string;
  connecting: string;
  liveSync: string;
  syncError: string;
  autoFollow: string;
  loadingSession: string;
  emptySessionTurns: string;
  roundsLabel: string;
  commandsLabel: string;
  changedFilesLabel: string;
  durationLabel: string;
  thinkingSummary: (n: number) => string;
  commandExecutions: string;
  expandAllCommands: string;
  collapseCommands: string;
  liveBannerCta: string;
  noOutput: string;
  abortedPrefix: string;
  cancelTask: string;
  cancelling: string;
  attachSummary: string;
  attachSummaryTip: string;
  sendSummary: string;
  sendSummaryTip: string;
  handoffRound: string;
  handoffRoundTip: string;
  toastCancelled: string;
  toastAlreadyDone: string;
  toastCancelFailed: (e: string) => string;
  toastAttached: string;
  toastAttachFailed: (e: string) => string;
  toastSent: string;
  toastHostRejected: string;
  toastSendFailed: (e: string) => string;
  toastModeSwitched: (label: string) => string;
  toastSwitchFailed: (e: string) => string;
  routingPolicyAria: string;
  routingPolicyTip: string;
  languageAria: string;
  languageAuto: string;
  openFileTip: (path: string) => string;
  delegatedByTip: (src: string) => string;
  roundTag: (n: number, delegated: boolean) => string;
  roundsShort: (n: number) => string;
  quota5hTip: (rem: number, reset: string) => string;
  quota7dTip: (rem: number, reset: string) => string;
  quotaResetCreditsTip: (n: number) => string;
  antigravityQuotaNote: string;
  // Profile Overview & Heatmap
  allProjects: string;
  kpiTotalTokens: string;
  kpiAgyShare: string;
  kpiLongestTask: string;
  kpiLongestStreak: string;
  kpiCurrentStreak: string;
  heatmapTitle: string;
  originAll: string;
  originAgy: string;
  originDirect: string;
  metricTokens: string;
  metricSessions: string;
  heatmapLess: string;
  heatmapMore: string;
  heatmapAgyDotHint: string;
  heatmapTooltip: (date: string, totalSess: number, agySess: number, directSess: number, tokensStr: string) => string;
  heatmapEmptyDay: (date: string) => string;
  originSplitSummary: (agy: number, agyPct: number, direct: number, directPct: number) => string;
  insightsTitle: string;
  insightOrigin: string;
  insightTopModel: string;
  insightTopEffort: string;
  insightActiveProjects: string;
  insightTotalCommands: string;
  insightRoutingMode: string;
  insightModelBreakdown: string;
  worktreesCountLabel: string;
  settingsAria: string;
  settingsTitle: string;
  defaultModelLabel: string;
  escalationModelLabel: string;
  guardrailLabel: string;
  guardrailThresholdLabel: string;
  diagnosticsTitle: string;
  runDoctorBtn: string;
  runningDoctor: string;
  doctorAllOk: string;
  doctorHasWarnings: string;
  doctorHasErrors: string;
  pruneWorktreesBtn: string;
  pruningWorktrees: string;
  pruneResultToast: (removed: number, skipped: number) => string;
  quotaLowWarning: (threshold: number) => string;
  toastSettingsSaved: string;
  recentSessionsTitle: string;
  hotFilesTitle: string;
  touchedBySessions: (n: number) => string;
  originTagAgy: string;
  originTagDirect: string;
  clearDateFilter: string;
  noFilteredSessions: string;
  // Summary markdown handoff
  summarySessionHeading: (title: string) => string;
  summaryRoundHeading: (round: number) => string;
  summaryWorkspace: string;
  summaryIsolation: string;
  summaryStatus: string;
  summaryStats: (cmds: number, files: number, dur: string) => string;
  summaryFiles: string;
  summaryMoreFiles: (n: number) => string;
  summaryConclusion: string;
  summaryAborted: string;
}

const ZH_CN: Strings = {
  overview: "总览",
  searchPlaceholder: "搜索会话、项目或模型…",
  refreshSessions: "刷新会话列表",
  toggleSidebar: "切换侧边栏",
  noSessionsDir: "未找到 Codex 会话目录",
  noMatchingSessions: "无匹配会话",
  noLocalSessions: "暂无本地 Codex 会话",
  connecting: "连接中",
  liveSync: "实时同步",
  syncError: "同步异常",
  autoFollow: "自动跟随",
  loadingSession: "加载中…",
  emptySessionTurns: "该会话尚无执行记录",
  roundsLabel: "轮次",
  commandsLabel: "命令",
  changedFilesLabel: "改动文件",
  durationLabel: "耗时",
  thinkingSummary: (n) => `思考摘要 · ${n}`,
  commandExecutions: "命令执行",
  expandAllCommands: "展开全部",
  collapseCommands: "收起",
  liveBannerCta: "查看进度",
  noOutput: "(无输出)",
  abortedPrefix: "已中止：",
  cancelTask: "终止任务",
  cancelling: "终止中…",
  attachSummary: "附加摘要",
  attachSummaryTip: "将精炼摘要附加到下一次对话消息",
  sendSummary: "回传对话",
  sendSummaryTip: "将精炼摘要发送到当前对话",
  handoffRound: "回传本轮",
  handoffRoundTip: "将该轮精炼摘要回传当前对话",
  toastCancelled: "已终止任务",
  toastAlreadyDone: "任务已结束",
  toastCancelFailed: (e) => `终止失败：${e}`,
  toastAttached: "已附加到对话",
  toastAttachFailed: (e) => `附加失败：${e}`,
  toastSent: "已回传对话",
  toastHostRejected: "宿主拒绝回传",
  toastSendFailed: (e) => `回传失败：${e}`,
  toastModeSwitched: (label) => `已切换：${label}`,
  toastSwitchFailed: (e) => `切换失败：${e}`,
  routingPolicyAria: "模型调度策略",
  routingPolicyTip: "Codex 模型调度策略",
  languageAria: "界面语言",
  languageAuto: "自动",
  openFileTip: (p) => `在侧栏打开 ${p}`,
  delegatedByTip: (src) => `由 ${src} 派单`,
  roundTag: (n, delegated) => (delegated ? `第 ${n} 轮派单` : `第 ${n} 轮`),
  roundsShort: (n) => `${n} 轮`,
  quota5hTip: (rem, reset) => `Codex 5h 剩余 ${rem}%（重置：${reset}）`,
  quota7dTip: (rem, reset) => `Codex 7d 剩余 ${rem}%（重置：${reset}）`,
  quotaResetCreditsTip: (n) => `可用额度重置券：${n}`,
  antigravityQuotaNote: "Antigravity 暂无公开本机额度接口，请以客户端账户面板为准。",
  allProjects: "全部项目",
  kpiTotalTokens: "累计 Token 数",
  kpiAgyShare: "AGY 派单占比",
  kpiLongestTask: "最长任务用时",
  kpiLongestStreak: "最长连续天数",
  kpiCurrentStreak: "当前连续天数",
  heatmapTitle: "活动热力图",
  originAll: "全部",
  originAgy: "AGY 派单",
  originDirect: "自己发起",
  metricTokens: "Token",
  metricSessions: "会话数",
  heatmapLess: "少",
  heatmapMore: "多",
  heatmapAgyDotHint: "含 AGY 派单",
  heatmapTooltip: (d, tot, agy, dir, tok) =>
    `${d} · ${tot} 次会话（AGY 派单 ${agy} / 自己发起 ${dir}）· ${tok} Tokens`,
  heatmapEmptyDay: (d) => `${d} · 无活动`,
  originSplitSummary: (agy, agyPct, dir, dirPct) =>
    `AGY 派单 ${agy} (${agyPct}%) · 自己发起 ${dir} (${dirPct}%)`,
  insightsTitle: "洞察",
  insightOrigin: "会话来源分布",
  insightTopModel: "最常用的模型",
  insightTopEffort: "最常用的推理强度",
  insightActiveProjects: "活跃项目数",
  insightTotalCommands: "累计执行命令",
  insightRoutingMode: "当前调度策略",
  insightModelBreakdown: "模型用量分布",
  worktreesCountLabel: "托管 Worktree",
  settingsAria: "设置与诊断",
  settingsTitle: "调度设置与环境诊断",
  defaultModelLabel: "默认主力模型",
  escalationModelLabel: "复杂升级模型",
  guardrailLabel: "低额度自动保护",
  guardrailThresholdLabel: "5h 触发阈值",
  diagnosticsTitle: "环境自检",
  runDoctorBtn: "运行自检",
  runningDoctor: "检测中…",
  doctorAllOk: "环境正常",
  doctorHasWarnings: "存在提醒项",
  doctorHasErrors: "存在错误项",
  pruneWorktreesBtn: "清理空闲 Worktree",
  pruningWorktrees: "清理中…",
  pruneResultToast: (removed, skipped) => `已清理 ${removed} 个 worktree${skipped > 0 ? `，跳过 ${skipped} 个运行中` : ""}`,
  quotaLowWarning: (threshold) => `⚠ 5h 剩余额度低于 ${threshold}%，低额度保护已就绪。`,
  toastSettingsSaved: "设置已保存",
  recentSessionsTitle: "最近会话",
  hotFilesTitle: "高频改动文件",
  touchedBySessions: (n) => `${n} 个会话`,
  originTagAgy: "AGY 派单",
  originTagDirect: "自己发起",
  clearDateFilter: "清除日期筛选",
  noFilteredSessions: "当前筛选条件下暂无会话",
  summarySessionHeading: (t) => `### Codex Orbit 会话摘要：${t}`,
  summaryRoundHeading: (r) => `### Codex Orbit 摘要（第 ${r} 轮）`,
  summaryWorkspace: "工作区",
  summaryIsolation: "模式",
  summaryStatus: "状态",
  summaryStats: (c, f, d) => `${c} 条命令，${f} 个改动文件，耗时 ${d}`,
  summaryFiles: "改动文件",
  summaryMoreFiles: (n) => `… 及另外 ${n} 个文件`,
  summaryConclusion: "结论摘要",
  summaryAborted: "中止/错误",
};

const ZH_TW: Strings = {
  overview: "總覽",
  searchPlaceholder: "搜尋工作階段、專案或模型…",
  refreshSessions: "重新整理工作階段列表",
  toggleSidebar: "切換側邊欄",
  noSessionsDir: "找不到 Codex 工作階段目錄",
  noMatchingSessions: "無相符的工作階段",
  noLocalSessions: "尚無本機 Codex 工作階段",
  connecting: "連線中",
  liveSync: "即時同步",
  syncError: "同步異常",
  autoFollow: "自動跟隨",
  loadingSession: "載入中…",
  emptySessionTurns: "此工作階段尚無執行紀錄",
  roundsLabel: "輪次",
  commandsLabel: "指令",
  changedFilesLabel: "變更檔案",
  durationLabel: "耗時",
  thinkingSummary: (n) => `思考摘要 · ${n}`,
  commandExecutions: "指令執行",
  expandAllCommands: "展開全部",
  collapseCommands: "收合",
  liveBannerCta: "檢視進度",
  noOutput: "(無輸出)",
  abortedPrefix: "已中止：",
  cancelTask: "終止任務",
  cancelling: "終止中…",
  attachSummary: "附加摘要",
  attachSummaryTip: "將精簡摘要附加至下一則對話訊息",
  sendSummary: "回傳對話",
  sendSummaryTip: "將精簡摘要傳送至目前對話",
  handoffRound: "回傳本輪",
  handoffRoundTip: "將本輪精簡摘要回傳至目前對話",
  toastCancelled: "已終止任務",
  toastAlreadyDone: "任務已結束",
  toastCancelFailed: (e) => `終止失敗：${e}`,
  toastAttached: "已附加至對話",
  toastAttachFailed: (e) => `附加失敗：${e}`,
  toastSent: "已回傳對話",
  toastHostRejected: "宿主拒絕回傳",
  toastSendFailed: (e) => `回傳失敗：${e}`,
  toastModeSwitched: (label) => `已切換：${label}`,
  toastSwitchFailed: (e) => `切換失敗：${e}`,
  routingPolicyAria: "模型排程策略",
  routingPolicyTip: "Codex 模型排程策略",
  languageAria: "介面語言",
  languageAuto: "自動",
  openFileTip: (p) => `在側欄開啟 ${p}`,
  delegatedByTip: (src) => `由 ${src} 派單`,
  roundTag: (n, delegated) => (delegated ? `第 ${n} 輪派單` : `第 ${n} 輪`),
  roundsShort: (n) => `${n} 輪`,
  quota5hTip: (rem, reset) => `Codex 5h 剩餘 ${rem}%（重置：${reset}）`,
  quota7dTip: (rem, reset) => `Codex 7d 剩餘 ${rem}%（重置：${reset}）`,
  quotaResetCreditsTip: (n) => `可用額度重置券：${n}`,
  antigravityQuotaNote: "Antigravity 尚無公開本機額度介面，請以用戶端帳戶面板為準。",
  allProjects: "全部專案",
  kpiTotalTokens: "累計 Token 數",
  kpiAgyShare: "AGY 派單佔比",
  kpiLongestTask: "最長任務耗時",
  kpiLongestStreak: "最長連續天數",
  kpiCurrentStreak: "目前連續天數",
  heatmapTitle: "活動熱力圖",
  originAll: "全部",
  originAgy: "AGY 派單",
  originDirect: "自行發起",
  metricTokens: "Token",
  metricSessions: "工作階段數",
  heatmapLess: "少",
  heatmapMore: "多",
  heatmapAgyDotHint: "含 AGY 派單",
  heatmapTooltip: (d, tot, agy, dir, tok) =>
    `${d} · ${tot} 次工作階段（AGY 派單 ${agy} / 自行發起 ${dir}）· ${tok} Tokens`,
  heatmapEmptyDay: (d) => `${d} · 無活動`,
  originSplitSummary: (agy, agyPct, dir, dirPct) =>
    `AGY 派單 ${agy} (${agyPct}%) · 自行發起 ${dir} (${dirPct}%)`,
  insightsTitle: "洞察",
  insightOrigin: "工作階段來源分佈",
  insightTopModel: "最常用的模型",
  insightTopEffort: "最常用的推理強度",
  insightActiveProjects: "活躍專案數",
  insightTotalCommands: "累計執行指令",
  insightRoutingMode: "目前排程策略",
  insightModelBreakdown: "模型用量分佈",
  worktreesCountLabel: "託管 Worktree",
  settingsAria: "設定與診斷",
  settingsTitle: "排程設定與環境診斷",
  defaultModelLabel: "預設主力模型",
  escalationModelLabel: "複雜升級模型",
  guardrailLabel: "低額度自動保護",
  guardrailThresholdLabel: "5h 觸發門檻",
  diagnosticsTitle: "環境自檢",
  runDoctorBtn: "執行自檢",
  runningDoctor: "檢查中…",
  doctorAllOk: "環境正常",
  doctorHasWarnings: "有提醒項目",
  doctorHasErrors: "有錯誤項目",
  pruneWorktreesBtn: "清理閒置 Worktree",
  pruningWorktrees: "清理中…",
  pruneResultToast: (removed, skipped) => `已清理 ${removed} 個 worktree${skipped > 0 ? `，略過 ${skipped} 個執行中` : ""}`,
  quotaLowWarning: (threshold) => `⚠ 5h 剩餘額度低於 ${threshold}%，低額度保護已啟用。`,
  toastSettingsSaved: "設定已儲存",
  recentSessionsTitle: "最近工作階段",
  hotFilesTitle: "高頻變更檔案",
  touchedBySessions: (n) => `${n} 個工作階段`,
  originTagAgy: "AGY 派單",
  originTagDirect: "自行發起",
  clearDateFilter: "清除日期篩選",
  noFilteredSessions: "目前篩選條件下尚無工作階段",
  summarySessionHeading: (t) => `### Codex Orbit 工作階段摘要：${t}`,
  summaryRoundHeading: (r) => `### Codex Orbit 摘要（第 ${r} 輪）`,
  summaryWorkspace: "工作區",
  summaryIsolation: "模式",
  summaryStatus: "狀態",
  summaryStats: (c, f, d) => `${c} 條指令，${f} 個變更檔案，耗時 ${d}`,
  summaryFiles: "變更檔案",
  summaryMoreFiles: (n) => `… 及另外 ${n} 個檔案`,
  summaryConclusion: "結論摘要",
  summaryAborted: "中止/錯誤",
};

const EN: Strings = {
  overview: "Overview",
  searchPlaceholder: "Search sessions, projects, or models…",
  refreshSessions: "Refresh sessions",
  toggleSidebar: "Toggle sidebar",
  noSessionsDir: "Codex sessions directory not found",
  noMatchingSessions: "No matching sessions",
  noLocalSessions: "No local Codex sessions yet",
  connecting: "Connecting",
  liveSync: "Live",
  syncError: "Sync error",
  autoFollow: "Follow",
  loadingSession: "Loading…",
  emptySessionTurns: "No recorded turns in this session yet",
  roundsLabel: "Rounds",
  commandsLabel: "Commands",
  changedFilesLabel: "Files",
  durationLabel: "Duration",
  thinkingSummary: (n) => `Reasoning · ${n}`,
  commandExecutions: "Commands",
  expandAllCommands: "Show all",
  collapseCommands: "Collapse",
  liveBannerCta: "View live",
  noOutput: "(no output)",
  abortedPrefix: "Aborted: ",
  cancelTask: "Stop task",
  cancelling: "Stopping…",
  attachSummary: "Attach summary",
  attachSummaryTip: "Attach compact summary to your next chat message",
  sendSummary: "Send to chat",
  sendSummaryTip: "Send compact summary directly to active conversation",
  handoffRound: "Send round",
  handoffRoundTip: "Attach or send this round's compact summary",
  toastCancelled: "Task stopped",
  toastAlreadyDone: "Task already finished",
  toastCancelFailed: (e) => `Stop failed: ${e}`,
  toastAttached: "Attached to chat",
  toastAttachFailed: (e) => `Attach failed: ${e}`,
  toastSent: "Sent to chat",
  toastHostRejected: "Host rejected message",
  toastSendFailed: (e) => `Send failed: ${e}`,
  toastModeSwitched: (label) => `Policy: ${label}`,
  toastSwitchFailed: (e) => `Switch failed: ${e}`,
  routingPolicyAria: "Model routing policy",
  routingPolicyTip: "Codex model routing policy",
  languageAria: "Interface language",
  languageAuto: "Auto",
  openFileTip: (p) => `Open ${p} in side pane`,
  delegatedByTip: (src) => `Delegated by ${src}`,
  roundTag: (n, delegated) => (delegated ? `Round ${n} · Delegated` : `Round ${n}`),
  roundsShort: (n) => (n === 1 ? "1 round" : `${n} rounds`),
  quota5hTip: (rem, reset) => `Codex 5h remaining: ${rem}% (resets ${reset})`,
  quota7dTip: (rem, reset) => `Codex 7d remaining: ${rem}% (resets ${reset})`,
  quotaResetCreditsTip: (n) => `Available reset credits: ${n}`,
  antigravityQuotaNote: "Antigravity has no public local quota API; check your Antigravity account panel.",
  allProjects: "All projects",
  kpiTotalTokens: "Total Tokens",
  kpiAgyShare: "AGY Delegated",
  kpiLongestTask: "Longest Run",
  kpiLongestStreak: "Longest Streak",
  kpiCurrentStreak: "Current Streak",
  heatmapTitle: "Activity",
  originAll: "All",
  originAgy: "AGY Delegated",
  originDirect: "Direct Codex",
  metricTokens: "Tokens",
  metricSessions: "Sessions",
  heatmapLess: "Less",
  heatmapMore: "More",
  heatmapAgyDotHint: "Includes AGY",
  heatmapTooltip: (d, tot, agy, dir, tok) =>
    `${d} · ${tot} sessions (${agy} AGY / ${dir} Direct) · ${tok} tokens`,
  heatmapEmptyDay: (d) => `${d} · No activity`,
  originSplitSummary: (agy, agyPct, dir, dirPct) =>
    `AGY Delegated ${agy} (${agyPct}%) · Direct Codex ${dir} (${dirPct}%)`,
  insightsTitle: "Insights",
  insightOrigin: "Session Origin",
  insightTopModel: "Most Used Model",
  insightTopEffort: "Top Reasoning Effort",
  insightActiveProjects: "Active Projects",
  insightTotalCommands: "Commands Run",
  insightRoutingMode: "Routing Policy",
  insightModelBreakdown: "Model Distribution",
  worktreesCountLabel: "Managed Worktrees",
  settingsAria: "Settings & diagnostics",
  settingsTitle: "Routing & Diagnostics",
  defaultModelLabel: "Default model",
  escalationModelLabel: "Escalation model",
  guardrailLabel: "Low-quota guardrail",
  guardrailThresholdLabel: "5h threshold",
  diagnosticsTitle: "Environment Check",
  runDoctorBtn: "Run check",
  runningDoctor: "Checking…",
  doctorAllOk: "All checks passed",
  doctorHasWarnings: "Warnings found",
  doctorHasErrors: "Issues found",
  pruneWorktreesBtn: "Prune idle worktrees",
  pruningWorktrees: "Pruning…",
  pruneResultToast: (removed, skipped) => `Pruned ${removed} worktree(s)${skipped > 0 ? `, skipped ${skipped} running` : ""}`,
  quotaLowWarning: (threshold) => `⚠ 5h quota is below ${threshold}% guardrail threshold.`,
  toastSettingsSaved: "Settings saved",
  recentSessionsTitle: "Recent Sessions",
  hotFilesTitle: "Frequently Modified Files",
  touchedBySessions: (n) => (n === 1 ? "1 session" : `${n} sessions`),
  originTagAgy: "AGY",
  originTagDirect: "Direct",
  clearDateFilter: "Clear date",
  noFilteredSessions: "No sessions match the current filter",
  summarySessionHeading: (t) => `### Codex Orbit Summary: ${t}`,
  summaryRoundHeading: (r) => `### Codex Orbit Summary (Round ${r})`,
  summaryWorkspace: "Workspace",
  summaryIsolation: "Mode",
  summaryStatus: "Status",
  summaryStats: (c, f, d) => `${c} commands, ${f} changed files, ${d}`,
  summaryFiles: "Changed files",
  summaryMoreFiles: (n) => `… and ${n} more files`,
  summaryConclusion: "Conclusion",
  summaryAborted: "Aborted / Error",
};

const JA: Strings = {
  overview: "概要",
  searchPlaceholder: "セッション・プロジェクト・モデルを検索…",
  refreshSessions: "セッション一覧を更新",
  toggleSidebar: "サイドバーを切替",
  noSessionsDir: "Codex セッションディレクトリが見つかりません",
  noMatchingSessions: "一致するセッションはありません",
  noLocalSessions: "ローカルの Codex セッションがありません",
  connecting: "接続中",
  liveSync: "リアルタイム",
  syncError: "同期エラー",
  autoFollow: "自動追従",
  loadingSession: "読み込み中…",
  emptySessionTurns: "このセッションには実行履歴がありません",
  roundsLabel: "ラウンド",
  commandsLabel: "コマンド",
  changedFilesLabel: "変更ファイル",
  durationLabel: "所要時間",
  thinkingSummary: (n) => `思考メモ · ${n}`,
  commandExecutions: "コマンド実行",
  expandAllCommands: "すべて表示",
  collapseCommands: "折りたたむ",
  liveBannerCta: "進捗を見る",
  noOutput: "(出力なし)",
  abortedPrefix: "中断: ",
  cancelTask: "タスク停止",
  cancelling: "停止中…",
  attachSummary: "要約を添付",
  attachSummaryTip: "次のメッセージに簡潔な要約を添付",
  sendSummary: "チャットに送信",
  sendSummaryTip: "現在のチャットに簡潔な要約を送信",
  handoffRound: "この回を送信",
  handoffRoundTip: "このラウンドの要約をチャットへ連携",
  toastCancelled: "タスクを停止しました",
  toastAlreadyDone: "タスクは既に終了しています",
  toastCancelFailed: (e) => `停止に失敗: ${e}`,
  toastAttached: "チャットに添付しました",
  toastAttachFailed: (e) => `添付に失敗: ${e}`,
  toastSent: "チャットに送信しました",
  toastHostRejected: "ホストにより送信が拒否されました",
  toastSendFailed: (e) => `送信に失敗: ${e}`,
  toastModeSwitched: (label) => `ポリシー変更: ${label}`,
  toastSwitchFailed: (e) => `変更に失敗: ${e}`,
  routingPolicyAria: "モデル切替ポリシー",
  routingPolicyTip: "Codex モデル切替ポリシー",
  languageAria: "表示言語",
  languageAuto: "自動",
  openFileTip: (p) => `サイドペインで ${p} を開く`,
  delegatedByTip: (src) => `${src} から依頼`,
  roundTag: (n, delegated) => (delegated ? `第 ${n} 回・依頼` : `第 ${n} 回`),
  roundsShort: (n) => `${n}回`,
  quota5hTip: (rem, reset) => `Codex 5h 残り ${rem}%（リセット: ${reset}）`,
  quota7dTip: (rem, reset) => `Codex 7d 残り ${rem}%（リセット: ${reset}）`,
  quotaResetCreditsTip: (n) => `利用可能なリセットクレジット: ${n}`,
  antigravityQuotaNote: "Antigravity には公開のローカル残量 API がないため、クライアントのアカウント画面をご確認ください。",
  allProjects: "すべてのプロジェクト",
  kpiTotalTokens: "累計トークン",
  kpiAgyShare: "AGY 依頼比率",
  kpiLongestTask: "最長実行時間",
  kpiLongestStreak: "最長連続日数",
  kpiCurrentStreak: "現在の連続日数",
  heatmapTitle: "アクティビティ",
  originAll: "すべて",
  originAgy: "AGY 依頼",
  originDirect: "直接実行",
  metricTokens: "トークン",
  metricSessions: "セッション数",
  heatmapLess: "少",
  heatmapMore: "多",
  heatmapAgyDotHint: "AGY 依頼あり",
  heatmapTooltip: (d, tot, agy, dir, tok) =>
    `${d} · ${tot} 件（AGY 依頼 ${agy} / 直接実行 ${dir}）· ${tok} トークン`,
  heatmapEmptyDay: (d) => `${d} · アクティビティなし`,
  originSplitSummary: (agy, agyPct, dir, dirPct) =>
    `AGY 依頼 ${agy} (${agyPct}%) · 直接実行 ${dir} (${dirPct}%)`,
  insightsTitle: "インサイト",
  insightOrigin: "セッション内訳",
  insightTopModel: "最も使用したモデル",
  insightTopEffort: "最も使用した推論強度",
  insightActiveProjects: "アクティブプロジェクト",
  insightTotalCommands: "累計コマンド実行",
  insightRoutingMode: "現在のポリシー",
  insightModelBreakdown: "モデル利用内訳",
  worktreesCountLabel: "管理 Worktree",
  settingsAria: "設定と診断",
  settingsTitle: "設定と環境診断",
  defaultModelLabel: "標準モデル",
  escalationModelLabel: "上位モデル",
  guardrailLabel: "低残量ガードレール",
  guardrailThresholdLabel: "5h しきい値",
  diagnosticsTitle: "環境診断",
  runDoctorBtn: "診断を実行",
  runningDoctor: "診断中…",
  doctorAllOk: "環境は正常です",
  doctorHasWarnings: "警告があります",
  doctorHasErrors: "エラーがあります",
  pruneWorktreesBtn: "待機中 Worktree を削除",
  pruningWorktrees: "削除中…",
  pruneResultToast: (removed, skipped) => `${removed} 件の worktree を削除${skipped > 0 ? `（実行中 ${skipped} 件をスキップ）` : ""}`,
  quotaLowWarning: (threshold) => `⚠ 5h 残量が ${threshold}% 未満のためガードレールが有効です。`,
  toastSettingsSaved: "設定を保存しました",
  recentSessionsTitle: "最近のセッション",
  hotFilesTitle: "よく変更されるファイル",
  touchedBySessions: (n) => `${n} 件のセッション`,
  originTagAgy: "AGY 依頼",
  originTagDirect: "直接実行",
  clearDateFilter: "日付フィルタを解除",
  noFilteredSessions: "条件に一致するセッションはありません",
  summarySessionHeading: (t) => `### Codex Orbit セッション要約: ${t}`,
  summaryRoundHeading: (r) => `### Codex Orbit 要約（第 ${r} 回）`,
  summaryWorkspace: "ワークスペース",
  summaryIsolation: "モード",
  summaryStatus: "ステータス",
  summaryStats: (c, f, d) => `コマンド ${c} 件、変更ファイル ${f} 件、所要時間 ${d}`,
  summaryFiles: "変更ファイル",
  summaryMoreFiles: (n) => `… 他 ${n} 件`,
  summaryConclusion: "結論サマリー",
  summaryAborted: "中断 / エラー",
};

export function getStrings(locale: ResolvedLocale): Strings {
  if (locale === "zh-CN") return ZH_CN;
  if (locale === "zh-TW") return ZH_TW;
  if (locale === "ja") return JA;
  return EN;
}

export function statusLabel(status: SessionStatus, locale: ResolvedLocale): string {
  if (locale === "zh-CN") {
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
  if (locale === "zh-TW") {
    switch (status) {
      case "running":
        return "執行中";
      case "completed":
        return "已完成";
      case "aborted":
        return "已中止";
      default:
        return "閒置";
    }
  }
  if (locale === "ja") {
    switch (status) {
      case "running":
        return "実行中";
      case "completed":
        return "完了";
      case "aborted":
        return "中断";
      default:
        return "待機";
    }
  }
  switch (status) {
    case "running":
      return "Running";
    case "completed":
      return "Completed";
    case "aborted":
      return "Aborted";
    default:
      return "Idle";
  }
}

export function isolationLabel(mode: IsolationMode | null | undefined, locale: ResolvedLocale): string {
  if (!mode) return "";
  if (locale === "zh-CN") {
    switch (mode) {
      case "read-only":
        return "只读";
      case "worktree":
        return "Worktree";
      case "workspace-write":
        return "同目录";
    }
  }
  if (locale === "zh-TW") {
    switch (mode) {
      case "read-only":
        return "唯讀";
      case "worktree":
        return "Worktree";
      case "workspace-write":
        return "同目錄";
    }
  }
  if (locale === "ja") {
    switch (mode) {
      case "read-only":
        return "読取専用";
      case "worktree":
        return "Worktree";
      case "workspace-write":
        return "同フォルダ";
    }
  }
  switch (mode) {
    case "read-only":
      return "Read-only";
    case "worktree":
      return "Worktree";
    case "workspace-write":
      return "Shared cwd";
  }
}

export function routingModeLabel(mode: RoutingMode, locale: ResolvedLocale): string {
  if (locale === "zh-CN") {
    switch (mode) {
      case "silent_low_cost":
        return "节能";
      case "ask":
        return "均衡";
      case "silent_best":
        return "极致";
    }
  }
  if (locale === "zh-TW") {
    switch (mode) {
      case "silent_low_cost":
        return "節能";
      case "ask":
        return "均衡";
      case "silent_best":
        return "極致";
    }
  }
  if (locale === "ja") {
    switch (mode) {
      case "silent_low_cost":
        return "エコ";
      case "ask":
        return "バランス";
      case "silent_best":
        return "最高";
    }
  }
  switch (mode) {
    case "silent_low_cost":
      return "Eco";
    case "ask":
      return "Balanced";
    case "silent_best":
      return "Max";
  }
}

export function effortLabel(effort: string | null | undefined, locale: ResolvedLocale): string {
  if (!effort) return "-";
  if (locale === "en") return effort;
  if (locale === "zh-TW") {
    const twMap: Record<string, string> = {
      low: "低 (low)",
      medium: "中 (medium)",
      high: "高 (high)",
      xhigh: "極高 (xhigh)",
      max: "最大 (max)",
      ultra: "Ultra",
    };
    return twMap[effort.toLowerCase()] ?? effort;
  }
  if (locale === "ja") {
    const jaMap: Record<string, string> = {
      low: "低 (low)",
      medium: "中 (medium)",
      high: "高 (high)",
      xhigh: "超高 (xhigh)",
      max: "最大 (max)",
      ultra: "Ultra",
    };
    return jaMap[effort.toLowerCase()] ?? effort;
  }
  const map: Record<string, string> = {
    low: "低 (low)",
    medium: "中 (medium)",
    high: "高 (high)",
    xhigh: "极高 (xhigh)",
    max: "最大 (max)",
    ultra: "Ultra",
  };
  return map[effort.toLowerCase()] ?? effort;
}

export function formatRelativeTime(iso: string | null | undefined, locale: ResolvedLocale): string {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (diffSec < 60) {
    if (locale === "zh-CN") return "刚刚";
    if (locale === "zh-TW") return "剛剛";
    if (locale === "ja") return "たった今";
    return "just now";
  }
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  const d = new Date(ts);
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${m}-${day}`;
}
