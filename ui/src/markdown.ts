/**
 * Zero-dependency, XSS-safe GitHub Flavored Markdown (GFM) renderer for Codex replies.
 * Supports headings, GFM tables, fenced code blocks, lists (ordered/unordered/task),
 * blockquotes, horizontal rules, inline code, bold/italic/strikethrough, external links,
 * and Codex-style local file links (e.g. [file.py](/D:/repo/file.py:123)).
 */

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

interface RenderMarkdownOptions {
  canOpenFile?: boolean;
}

const TABLE_SEP_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const FENCE_RE = /^(\s*)(`{3,}|~{3,})([^\n]*)$/;
const HEADING_RE = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const HR_RE = /^\s*(?:[-*_]\s*){3,}$/;
const UL_RE = /^\s*[-*+]\s+(.*)$/;
const OL_RE = /^\s*\d+\.\s+(.*)$/;
const BQ_RE = /^\s*>\s?(.*)$/;

function splitTableRow(line: string): string[] {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|") && !trimmed.endsWith("\\|")) trimmed = trimmed.slice(0, -1);

  const cells: string[] = [];
  let cur = "";
  let inCode = false;
  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i];
    if (ch === "\\" && i + 1 < trimmed.length && trimmed[i + 1] === "|") {
      cur += "|";
      i += 1;
      continue;
    }
    if (ch === "`") {
      inCode = !inCode;
      cur += ch;
      continue;
    }
    if (ch === "|" && !inCode) {
      cells.push(cur.trim());
      cur = "";
      continue;
    }
    cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}

function parseTableAlignments(sepLine: string): Array<"left" | "center" | "right" | ""> {
  return splitTableRow(sepLine).map((cell) => {
    const c = cell.trim();
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  });
}

function normalizeLocalFileTarget(rawTarget: string): { filePath: string; displayTarget: string } | null {
  let target = rawTarget.trim();
  if (!target) return null;
  if (/^https?:\/\//i.test(target) || /^mailto:/i.test(target)) return null;
  if (/^javascript:/i.test(target) || /^data:/i.test(target)) return null;

  if (/^file:\/\//i.test(target)) {
    target = target.replace(/^file:\/\//i, "");
    try {
      target = decodeURIComponent(target);
    } catch {
      // keep raw
    }
  }
  // Codex frequently emits Windows paths as `/D:/repo/file.py:123`
  if (/^\/[A-Za-z]:[\\/]/.test(target)) {
    target = target.slice(1);
  }
  const displayTarget = target;
  // Strip trailing :line(:col) or #L123-L456 for opening the file itself.
  const withoutAnchor = target.replace(/#L\d+(?:-L?\d+)?$/i, "");
  const withoutLine = withoutAnchor.replace(/:\d+(?::\d+)?$/, "");
  if (!withoutLine) return null;
  return { filePath: withoutLine, displayTarget };
}

function renderInline(raw: string, opts: RenderMarkdownOptions): string {
  // 1. Extract inline code spans before escaping/transforming so markdown chars inside code stay intact.
  const codeTokens: string[] = [];
  const withCodePlaceholders = raw.replace(/(`+)([^\n]*?)\1/g, (_m, _ticks: string, body: string) => {
    const idx = codeTokens.length;
    codeTokens.push(`<code>${escapeHtml(body)}</code>`);
    return `\u0000CODE${idx}\u0000`;
  });

  // 2. Extract markdown links [label](url) before HTML escaping so URLs stay clean.
  const linkTokens: string[] = [];
  const withLinkPlaceholders = withCodePlaceholders.replace(
    /\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
    (_m, labelRaw: string, targetRaw: string) => {
      const idx = linkTokens.length;
      const safeLabel = renderSimpleEmphasis(escapeHtml(labelRaw));
      if (/^https?:\/\//i.test(targetRaw)) {
        const safeUrl = escapeHtml(targetRaw);
        linkTokens.push(
          `<a class="md-link" href="${safeUrl}" data-open-url="${safeUrl}" title="${safeUrl}">${safeLabel}</a>`,
        );
      } else {
        const local = normalizeLocalFileTarget(targetRaw);
        if (local) {
          const safePath = escapeHtml(local.filePath);
          const safeTip = escapeHtml(local.displayTarget);
          if (opts.canOpenFile) {
            linkTokens.push(
              `<button class="md-file-link" type="button" data-open-file="${safePath}" title="${safeTip}">${safeLabel}</button>`,
            );
          } else {
            linkTokens.push(`<code class="md-file-ref" title="${safeTip}">${safeLabel}</code>`);
          }
        } else {
          linkTokens.push(safeLabel);
        }
      }
      return `\u0000LINK${idx}\u0000`;
    },
  );

  // 3. Escape remaining raw HTML and apply bold / italic / strikethrough.
  let html = renderSimpleEmphasis(escapeHtml(withLinkPlaceholders));

  // 4. Restore link & code tokens.
  html = html.replace(/\u0000LINK(\d+)\u0000/g, (_m, i: string) => linkTokens[Number(i)] ?? "");
  html = html.replace(/\u0000CODE(\d+)\u0000/g, (_m, i: string) => codeTokens[Number(i)] ?? "");
  return html;
}

function renderSimpleEmphasis(escaped: string): string {
  return escaped
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/(^|[\s([{"'（【])\*([^*\n]+?)\*(?=[\s.,;:!?)\]}"'）】]|$)/g, "$1<em>$2</em>");
}

function isBlockStart(lines: string[], idx: number): boolean {
  const line = lines[idx];
  if (!line.trim()) return true;
  if (FENCE_RE.test(line)) return true;
  if (HEADING_RE.test(line)) return true;
  if (HR_RE.test(line)) return true;
  if (BQ_RE.test(line)) return true;
  if (UL_RE.test(line) || OL_RE.test(line)) return true;
  if (line.includes("|") && idx + 1 < lines.length && TABLE_SEP_RE.test(lines[idx + 1])) {
    return true;
  }
  return false;
}

function renderBlocks(lines: string[], opts: RenderMarkdownOptions): string {
  const out: string[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line
    if (!line.trim()) {
      i += 1;
      continue;
    }

    // Fenced code block (``` or ~~~)
    const fenceMatch = FENCE_RE.exec(line);
    if (fenceMatch) {
      const fenceChar = fenceMatch[2][0];
      const fenceLen = fenceMatch[2].length;
      const lang = fenceMatch[3].trim().split(/\s+/)[0] || "";
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length) {
        const closeMatch = /^\s*(`{3,}|~{3,})\s*$/.exec(lines[i]);
        if (closeMatch && closeMatch[1][0] === fenceChar && closeMatch[1].length >= fenceLen) {
          i += 1;
          break;
        }
        codeLines.push(lines[i]);
        i += 1;
      }
      const langAttr = lang ? ` data-lang="${escapeHtml(lang)}"` : "";
      out.push(`<pre${langAttr}><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    // ATX Heading (# .. ######)
    const headingMatch = HEADING_RE.exec(line);
    if (headingMatch) {
      const level = Math.min(6, Math.max(1, headingMatch[1].length));
      out.push(`<h${level} class="md-h md-h${level}">${renderInline(headingMatch[2], opts)}</h${level}>`);
      i += 1;
      continue;
    }

    // Horizontal rule
    if (HR_RE.test(line)) {
      out.push('<hr class="md-hr" />');
      i += 1;
      continue;
    }

    // GFM Table (header row + separator row + body rows)
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const headers = splitTableRow(line);
      const aligns = parseTableAlignments(lines[i + 1]);
      i += 2;
      const bodyRows: string[][] = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        if (HEADING_RE.test(lines[i]) || FENCE_RE.test(lines[i])) break;
        bodyRows.push(splitTableRow(lines[i]));
        i += 1;
      }
      const colCount = Math.max(headers.length, aligns.length);
      const theadHtml = headers
        .map((cell, ci) => {
          const align = aligns[ci] ? ` style="text-align:${aligns[ci]}"` : "";
          return `<th${align}>${renderInline(cell, opts)}</th>`;
        })
        .join("");
      const tbodyHtml = bodyRows
        .map((row) => {
          const cells: string[] = [];
          for (let ci = 0; ci < colCount; ci++) {
            const align = aligns[ci] ? ` style="text-align:${aligns[ci]}"` : "";
            cells.push(`<td${align}>${renderInline(row[ci] ?? "", opts)}</td>`);
          }
          return `<tr>${cells.join("")}</tr>`;
        })
        .join("");
      out.push(
        `<div class="md-table-wrap"><table class="md-table"><thead><tr>${theadHtml}</tr></thead><tbody>${tbodyHtml}</tbody></table></div>`,
      );
      continue;
    }

    // Blockquote
    if (BQ_RE.test(line)) {
      const quoteLines: string[] = [];
      while (i < lines.length) {
        const m = BQ_RE.exec(lines[i]);
        if (!m) break;
        quoteLines.push(m[1]);
        i += 1;
      }
      out.push(`<blockquote class="md-quote">${renderBlocks(quoteLines, opts)}</blockquote>`);
      continue;
    }

    // Unordered list
    if (UL_RE.test(line)) {
      const items: string[] = [];
      while (i < lines.length) {
        const m = UL_RE.exec(lines[i]);
        if (!m) break;
        let itemText = m[1];
        i += 1;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !UL_RE.test(lines[i]) && !OL_RE.test(lines[i])) {
          itemText += "\n" + lines[i].trim();
          i += 1;
        }
        const taskMatch = /^\[( |x|X)\]\s+(.*)$/.exec(itemText);
        if (taskMatch) {
          const checked = taskMatch[1].toLowerCase() === "x";
          items.push(
            `<li class="md-task-item"><span class="md-task-box${checked ? " checked" : ""}" aria-hidden="true">${checked ? "✓" : ""}</span><span>${renderInline(taskMatch[2], opts)}</span></li>`,
          );
        } else {
          items.push(`<li>${renderInline(itemText, opts).replace(/\n/g, "<br>")}</li>`);
        }
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    // Ordered list
    if (OL_RE.test(line)) {
      const items: string[] = [];
      while (i < lines.length) {
        const m = OL_RE.exec(lines[i]);
        if (!m) break;
        let itemText = m[1];
        i += 1;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !UL_RE.test(lines[i]) && !OL_RE.test(lines[i])) {
          itemText += "\n" + lines[i].trim();
          i += 1;
        }
        items.push(`<li>${renderInline(itemText, opts).replace(/\n/g, "<br>")}</li>`);
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    // Paragraph (gather lines until blank line or next block construct)
    const paraLines: string[] = [line];
    i += 1;
    while (i < lines.length && !isBlockStart(lines, i)) {
      paraLines.push(lines[i]);
      i += 1;
    }
    const paraHtml = paraLines.map((l) => renderInline(l, opts)).join("<br>");
    out.push(`<p>${paraHtml}</p>`);
  }

  return out.join("");
}

export function renderSafeMarkdown(raw: string, opts: RenderMarkdownOptions = {}): string {
  const normalized = raw.replace(/\r\n?/g, "\n");
  const rendered = renderBlocks(normalized.split("\n"), opts);
  return rendered || `<p>${escapeHtml(raw)}</p>`;
}
