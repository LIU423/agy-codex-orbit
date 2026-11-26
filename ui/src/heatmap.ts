import { formatCompactNumber, getStrings, monthLabel } from "./i18n";
import type {
  HeatmapDayEntry,
  HeatmapMetric,
  OriginFilter,
  ResolvedLocale,
} from "./types";

function escapeAttr(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toLocalDateStr(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function parseIsoDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map((v) => Number(v));
  return new Date(y, (m || 1) - 1, d || 1);
}

function dayMetricValue(
  day: HeatmapDayEntry | undefined,
  origin: OriginFilter,
  metric: HeatmapMetric,
): number {
  if (!day) return 0;
  if (metric === "sessions") {
    if (origin === "agy") return day.agy_sessions;
    if (origin === "direct") return day.direct_sessions;
    return day.sessions;
  }
  if (origin === "agy") return day.agy_tokens;
  if (origin === "direct") return day.direct_tokens;
  return day.tokens;
}

function levelForValue(value: number, maxVal: number): 0 | 1 | 2 | 3 | 4 {
  if (value <= 0 || maxVal <= 0) return 0;
  const ratio = value / maxVal;
  if (ratio <= 0.18) return 1;
  if (ratio <= 0.42) return 2;
  if (ratio <= 0.72) return 3;
  return 4;
}

export function renderActivityHeatmap(params: {
  todayIso: string;
  days: HeatmapDayEntry[];
  origin: OriginFilter;
  metric: HeatmapMetric;
  selectedDate: string | null;
  locale: ResolvedLocale;
  weeksCount?: number;
}): string {
  const { todayIso, days, origin, metric, selectedDate, locale } = params;
  const weeks = params.weeksCount ?? 52;
  const str = getStrings(locale);

  const byDate = new Map<string, HeatmapDayEntry>(days.map((d) => [d.date, d]));
  const today = parseIsoDate(todayIso);

  // End on Saturday of today's week so every column has 7 rows (Sun..Sat), matching Codex Profile.
  const endDate = new Date(today);
  endDate.setDate(today.getDate() + (6 - today.getDay()));

  const startDate = new Date(endDate);
  startDate.setDate(endDate.getDate() - (weeks * 7 - 1));

  // Compute max value across visible cells for normalized 1..4 intensity levels.
  let maxVal = 0;
  const cells: Array<{
    dateStr: string;
    future: boolean;
    entry?: HeatmapDayEntry;
    value: number;
    weekIndex: number;
  }> = [];

  const monthMarkers: Array<{ weekIndex: number; label: string }> = [];
  let lastMonth = -1;

  for (let i = 0; i < weeks * 7; i++) {
    const cur = new Date(startDate);
    cur.setDate(startDate.getDate() + i);
    const dateStr = toLocalDateStr(cur);
    const weekIndex = Math.floor(i / 7);
    const isRow0 = i % 7 === 0;
    if (isRow0) {
      const m = cur.getMonth();
      if (m !== lastMonth) {
        monthMarkers.push({ weekIndex, label: monthLabel(m, locale) });
        lastMonth = m;
      }
    }
    const future = cur.getTime() > today.getTime();
    const entry = future ? undefined : byDate.get(dateStr);
    const value = future ? 0 : dayMetricValue(entry, origin, metric);
    if (value > maxVal) maxVal = value;
    cells.push({ dateStr, future, entry, value, weekIndex });
  }

  const paletteCls = origin === "agy" ? "palette-agy" : "palette-blue";

  const cellsHtml = cells
    .map((c) => {
      if (c.future) {
        return `<span class="hm-cell future" aria-hidden="true"></span>`;
      }
      const lvl = levelForValue(c.value, maxVal);
      const hasAgy = origin === "all" && Boolean(c.entry && c.entry.agy_sessions > 0);
      const isSelected = selectedDate === c.dateStr;
      const tip = c.entry
        ? str.heatmapTooltip(
            c.dateStr,
            c.entry.sessions,
            c.entry.agy_sessions,
            c.entry.direct_sessions,
            formatCompactNumber(c.entry.tokens, locale),
          )
        : str.heatmapEmptyDay(c.dateStr);
      const classes = [
        "hm-cell",
        `lvl-${lvl}`,
        hasAgy ? "has-agy" : "",
        isSelected ? "selected" : "",
      ]
        .filter(Boolean)
        .join(" ");
      return `<button class="${classes}" data-hm-date="${c.dateStr}" type="button" title="${escapeAttr(tip)}" aria-label="${escapeAttr(tip)}"></button>`;
    })
    .join("");

  // Build aligned month labels along the bottom matching column positions.
  const monthsHtml = monthMarkers
    .map((m) => {
      const pct = (m.weekIndex / weeks) * 100;
      return `<span class="hm-month" style="left:${pct.toFixed(2)}%">${escapeAttr(m.label)}</span>`;
    })
    .join("");

  const agyHintHtml =
    origin === "all"
      ? `<span class="hm-legend-sep">·</span><span class="hm-legend-item"><span class="hm-swatch lvl-2 has-agy"></span>${escapeAttr(str.heatmapAgyDotHint)}</span>`
      : "";

  return `
    <div class="hm-wrap ${paletteCls}">
      <div class="hm-scroll">
        <div class="hm-inner" style="--hm-weeks:${weeks}">
          <div class="hm-grid">${cellsHtml}</div>
          <div class="hm-months">${monthsHtml}</div>
        </div>
      </div>
      <div class="hm-footer">
        <div class="hm-legend">
          <span>${escapeAttr(str.heatmapLess)}</span>
          <span class="hm-swatch lvl-0"></span>
          <span class="hm-swatch lvl-1"></span>
          <span class="hm-swatch lvl-2"></span>
          <span class="hm-swatch lvl-3"></span>
          <span class="hm-swatch lvl-4"></span>
          <span>${escapeAttr(str.heatmapMore)}</span>
          ${agyHintHtml}
        </div>
      </div>
    </div>`;
}
