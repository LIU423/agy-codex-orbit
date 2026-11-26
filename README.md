# Codex Orbit (`agy-codex-orbit`)

Google Antigravity 2.0 native MCP App plugin for zero-token live monitoring of local OpenAI Codex sessions and scoped task delegation.

## Features

- **Zero-Token Monitoring (`Codex Orbit` panel)**: Mounted to both Antigravity's left sidebar (`global`) and right auxiliary pane (`aux_pane`). All session listing, detail reads, and incremental byte-offset polling run through `visibility: ["app"]` tools hidden from Gemini.
- **Scoped Delegation (`codex-delegation` skill + `server/delegate_cli.py`)**:
  - Wraps `codex exec --json` and `codex exec resume <session_id> --json` with stdin prompt passing (Windows UTF-8 safe).
  - Enforces workspace isolation (`read-only` for reviews; dedicated git `worktree` under `${PLUGIN_DATA}/worktrees/` automatically reused across follow-up rounds).
  - Automatically discovers local `codex.exe` on Windows (env override `CODEX_ORBIT_CODEX_BIN`, `PATH`, Microsoft Store `OpenAI.Codex`, or local `.codex`/IDE bundled binaries).
  - Returns only a compact JSON summary on completion without flooding model context with raw command stdout.

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

All tests use synthetic JSONL fixtures and a synthetic local Codex CLI stub; no real `.codex` session data is ever written to the repository.

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
