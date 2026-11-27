# Development & Testing Guide

## Architecture Overview

- **`server/main.py`**: MCP App server (`stdio`). Exposes `open_codex_orbit` to Gemini and 13 `visibility: ["app"]` tools (`orbit_list_sessions`, `orbit_get_session_detail`, `orbit_poll_session_delta`, `orbit_send_followup`, `orbit_cancel_delegation`, `orbit_get_quota`, `orbit_set_routing_mode`, `orbit_set_ui_locale`, `orbit_get_settings`, `orbit_update_settings`, `orbit_run_doctor`, `orbit_prune_worktrees`, `orbit_get_overview`) to the UI pane.
- **`server/rollout_parser.py` & `server/session_store.py`**: `(mtime_ns, size)` caching, byte-offset incremental tailing of `~/.codex/sessions/**/rollout-*.jsonl`, internal subagent rollout deduplication, and merging with `${PLUGIN_DATA}/delegations/*.json`.
- **`server/delegate_cli.py`**: Invoked by Gemini via `run_command`. Executes `codex exec --json` / `codex exec resume --json`, manages `worktree` isolation, applies model routing & low-quota guardrails, syncs Codex Desktop metadata, and prints a compact JSON summary.
- **`server/routing_policy.py`**: Persistent `OrbitSettings` (`settings.json`), 3-mode routing (`silent_low_cost`, `ask`, `silent_best`), configurable models, and low-quota guardrail logic.
- **`server/codex_desktop.py`**: Reads `~/.codex/models_cache.json`, queries live 5h/7d quota (`account/rateLimits/read`), sets `agy: ` titles (`thread/name/set`), binds projects/sections, and safely promotes rollout `session_meta.source` after exit.
- **`server/doctor.py`**: 7-point environment and installation diagnostics (`--doctor` / `orbit_run_doctor`).
- **`ui/`**: TypeScript + Vite single-file MCP App UI (`dist/index.html`) using `@modelcontextprotocol/ext-apps`.

---

## Local Dev Setup

### 1. Python Virtual Environment (3.11+)

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

**macOS / Linux:**
```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
```

### 2. Build Single-File UI (`dist/index.html`)

```bash
cd ui
pnpm install
pnpm run build
```

### 3. Link Repo Live into Antigravity

Unlike `install.ps1` / `install.sh` (which copy a standalone bundle), dev-linking points Antigravity directly at this repository's `.venv`, `server/main.py`, and `dist/index.html`:

- **Windows:** `.\scripts\dev-link.ps1`
- **macOS / Linux:** `./scripts/dev-link.sh`

### 4. Fast Iteration Loop
- **UI changes**: Run `pnpm run build` inside `ui/`, then close and reopen the **Codex Orbit** pane (no MCP restart needed).
- **Server changes**: To restart `agy-codex-orbit_codex_orbit`, run `.\scripts\refresh-mcp.ps1` on Windows or `./scripts/refresh-mcp.sh` on macOS/Linux from an Antigravity terminal. The shell script uses `curl` and `python3` (override with `PYTHON_BIN`).
- **Manifest / skill changes**: Re-run `.\scripts\dev-link.ps1` (or `./scripts/dev-link.sh`).

---

## Running Tests

The test suite runs offline against synthetic JSONL fixtures, temporary SQLite/JSON stores, and synthetic Codex CLI stubs — no real `.codex` session data is modified.

```powershell
# Windows
.\.venv\Scripts\python.exe -m pytest -v
```

```bash
# macOS / Linux
./.venv/bin/python -m pytest -v
```
