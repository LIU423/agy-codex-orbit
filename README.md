# Codex Orbit (`agy-codex-orbit`)

Google Antigravity 2.0 native MCP App plugin for zero-token live monitoring of local OpenAI Codex sessions, scoped task delegation, and live run control.

## Features

- **Zero-Token Live Monitoring & Profile Overview (`Codex Orbit` panel)**:
  - Mounted to both Antigravity's left sidebar (`global`) and right auxiliary pane (`aux_pane`), with full Chinese / English bilingual UI (`Auto` following host locale, `中文`, or `EN`).
  - All monitoring, overview, settings, quota, and control tools (`orbit_list_sessions`, `orbit_get_session_detail`, `orbit_poll_session_delta`, `orbit_cancel_delegation`, `orbit_get_quota`, `orbit_set_routing_mode`, `orbit_set_ui_locale`, `orbit_get_overview`) declare `visibility: ["app"]` and are hidden from Gemini.
  - **Default Profile Overview & 52-Week Activity Heatmap**: Inspired by Codex's Profile page, surfaces local Token/session/streak KPIs, a 52-week rounded-square calendar heatmap with **All / AGY-delegated / Direct Codex** source filters, **Tokens / Sessions** metric toggle, click-to-filter day drill-down, AGY vs. Direct split bar, Insights rows (most-used model & reasoning effort, routing mode, commands, hot files), and origin-badged recent sessions.
  - **Clean Reading Canvas, Collapsed Sidebar & Compact Commands**: Project groups start collapsed by default; each turn's command list collapses by default to a single muted grayscale row showing the latest command (expandable on demand); right auxiliary pane auto-focuses newly running delegated tasks when `open_codex_orbit(focus_running=True)` is invoked; preserves fold state across live incremental updates, follows host light/dark theme, displays cached live Codex 5h/7d quota, and handles large rollouts via `(mtime_ns, size)` caching + byte-offset tailing.
- **Model-Aware Delegation (`codex-delegation` skill + static `references/model_profile.md` + `server/delegate_cli.py`)**:
  - Encodes the current pairing workflow: **Gemini handles frontend/UI & orchestration; Codex (`gpt-6.2-sol` by default) handles backend/systems/tests/reviews** using the 5-part GPT-6.2 colleague brief.
  - **Three switchable model routing policies** (persisted in `${PLUGIN_DATA}/settings.json`, switchable from the Codex Orbit topbar or CLI `--set-routing-mode`):
    - **静默低成本模式 (`silent_low_cost`)**: Always silently uses `gpt-6.2-sol` (`low`/`medium`/`high`).
    - **问询模式 (`ask`, default)**: Uses `gpt-6.2-sol` silently for routine work; on complex Astra-worthy tasks, queries live Codex quota (`--plan-route --task-tier complex`) and asks the user before escalating to `gpt-6.2-astra`.
    - **静默最佳分配模式 (`silent_best`)**: Silently assigns the optimal model & effort (`gpt-6.2-astra` `high`/`xhigh` for standard/complex backend tasks) without cost gating.
  - **Same-workspace serial handoff, live web research & Codex Desktop sync**: Defaults to sharing the Antigravity workspace (`workspace-write`, `read-only` for review, `worktree` when editing in parallel, optional `--web-search` for live online research + Markdown report handoff in Git or non-Git workspaces), prefixes thread names with `agy: ` via official `thread/name/set`, and binds/creates the matching Codex Desktop project with backup & graceful degradation.
  - Returns only a compact JSON summary on completion without flooding model context with raw command stdout.
- **Live Controls & Conversation Handoff**:
  - **Cancel running delegation**: Terminates the active Codex process tree cleanly on Windows (`taskkill /T /F` + Win32 fallback) and marks metadata as `cancelled`.
  - **Conversation handoff (capability-gated)**: In views supporting conversation capabilities (`aux_pane` / inline), attach or send a compact session or per-round summary to the active conversation, and click changed files to open them in Antigravity's side pane (`com.google.antigravity/toggle-aux-pane`). Automatically hidden in `global` sidebar view.

## Local Development Setup (Windows)

1. **Python virtual environment** (Python 3.11+):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   ```
2. **Build single-file UI** (`dist/index.html`):
   ```powershell
   cd ui
   pnpm install
   pnpm run build
   ```
3. **Deploy plugin into Antigravity** (real directory in `~/.gemini/config/plugins/agy-codex-orbit/` pointing live at this repo's `.venv`, `server/main.py`, `skills/`, and `dist/index.html`):
   ```powershell
   .\scripts\dev-link.ps1
   ```
4. **Iterate quickly**:
   - **UI edits**: rebuild in `ui/` (`pnpm run build`), then reopen the **Codex Orbit** pane. No MCP server restart needed.
   - **Server edits**: run `.\scripts\refresh-mcp.ps1` from an Antigravity terminal to restart only `agy-codex-orbit_codex_orbit`.
   - **Skill/manifest edits**: re-run `.\scripts\dev-link.ps1`.

## Tests

All tests use synthetic JSONL fixtures, a synthetic local Codex CLI stub, and temporary subprocesses; no real `.codex` session data is ever written to the repository.

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
