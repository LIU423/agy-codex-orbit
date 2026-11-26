# Codex Orbit (`agy-codex-orbit`)

Google Antigravity 2.0 native MCP App plugin for zero-token live monitoring of local OpenAI Codex sessions and scoped task delegation.

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
3. **Deploy plugin into Antigravity** (real directory in `~/.gemini/config/plugins/agy-codex-orbit/` pointing live at this repo's `.venv`, `server/main.py`, and `dist/index.html`):
   ```powershell
   .\scripts\dev-link.ps1
   ```
4. **Iterate quickly**:
   - **UI edits**: rebuild in `ui/` (`pnpm run build`), then reopen the **Codex Orbit** pane. No MCP server restart needed.
   - **Server edits**: run `.\scripts\refresh-mcp.ps1` from an Antigravity terminal to restart only `agy-codex-orbit_codex_orbit`.

## Tests

All tests use synthetic JSONL fixtures in `tests/fixtures/` and never write local `.codex` session data into the repository.

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
