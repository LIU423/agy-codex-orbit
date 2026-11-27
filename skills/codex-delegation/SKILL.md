---
name: codex-delegation
description: Delegate backend coding, testing, debugging, read-only code review, or live web-search research tasks to local OpenAI Codex CLI using the static GPT-6.2 model routing & prompting profile, same-workspace serial handoff, Codex Desktop project + `agy:` thread naming, quota-aware Astra escalation, and zero-token monitoring in Codex Orbit.
---

# Codex Delegation Workflow

Use this skill to route backend/systems work to local OpenAI Codex CLI while Antigravity (Gemini) handles frontend/UI and orchestration, monitored at zero model tokens in **Codex Orbit**.

Always read [references/model_profile.md](references/model_profile.md) before drafting a delegation brief. **Never** run web searches for model benchmarks during routine delegation; only refresh `references/model_profile.md` when the user explicitly tells you the target model has updated.

## 1. Division of Labor & 3-Mode Model Routing

1. **Frontend vs. Backend**:
   - **Gemini (do directly)**: Frontend UI, CSS/layout, visual polish, component interaction, trivial 1–5 line edits, and user-facing orchestration.
   - **Codex (delegate)**: Backend architecture, APIs, business logic, parsers, persistence/SQL, CLI/systems tooling, complex debugging, backend tests, and independent read-only code reviews.
2. **Classify Task Tier & Check Active Routing Policy**:
   - Classify the delegated backend task as `--task-tier light | standard | complex` per [references/model_profile.md](references/model_profile.md).
   - **Fast single-step dispatch for `light` / `standard` tasks**: Skip `--plan-route` and invoke `delegate_cli.py` directly with `--task-tier light | standard` (omitting hardcoded `--model` / `--reasoning-effort` unless the user explicitly requested overrides). `delegate_cli.py` reads the persisted settings (`silent_low_cost` | `ask` | `silent_best`) and 60s cached quota guardrail automatically.
   - **Pre-flight check for `complex` tasks**: Run `--plan-route --task-tier complex --mode <mode>` first (fast via 60s disk quota cache) so `ask` mode confirms before drafting a long brief; or pass `--confirm-if-needed` in a single step so `delegate_cli.py` exits cleanly with `{"status": "needs_confirmation", ...}` without starting Codex when confirmation is needed:
     - **Windows (PowerShell)**:
       ```powershell
       $rt = Get-Content "$env:LOCALAPPDATA\agy-codex-orbit\runtime.json" -Raw | ConvertFrom-Json
       & $rt.python_bin -u $rt.delegate_cli --plan-route --task-tier complex --mode workspace-write
       ```
     - **macOS / Linux (`sh` / `bash` / `zsh`)**:
       ```bash
       eval "$(python3 -c 'import json, os, shlex; d=json.load(open(os.path.expanduser("~/.agy-codex-orbit/runtime.json"))); print(f"PY={shlex.quote(d[\"python_bin\"])} CLI={shlex.quote(d[\"delegate_cli\"])}")')"
       "$PY" -u "$CLI" --plan-route --task-tier complex --mode workspace-write
       ```
   - Follow the returned policy strictly:
     - **`silent_low_cost`（节能模式 / Eco）**: Dispatch silently with the returned default `model` (default `gpt-6.2-sol`) and effort; never escalate.
     - **`ask`（均衡模式 / Balanced — 默认）**: If `requires_confirmation` is `false`, dispatch silently with the returned `model` & `reasoning_effort`. If `requires_confirmation` is `true` (`complex` tier), **DO NOT dispatch yet** — present the recommended escalation model vs. default model together with the returned **Codex 5h & 7d remaining quota + reset times** (and any `guardrail_note` warning if 5h quota is below threshold), and let the user decide.
     - **`silent_best`（极致模式 / Max）**: Dispatch silently with the returned `model` & `reasoning_effort` without asking. Note: if the low-quota guardrail triggers (`guardrail_triggered: true`, 5h remaining below threshold), `--plan-route` and automatic live dispatch safely downgrade `model` to the configured default model while preserving the target `reasoning_effort`.
3. **Switching Policies, Models & Running Diagnostics via Chat**:
   - When the user asks to change routing policy, models, or guardrail settings, persist them immediately so future turns and the **Codex Orbit** UI stay synchronized:
     - `--set-routing-mode silent_low_cost | ask | silent_best`
     - `--set-reasoning-effort auto | low | medium | high | xhigh | max | ultra`
     - `--set-default-model <slug>` / `--set-escalation-model <slug>` (empty string restores default)
     - `--set-quota-guardrail on | off` / `--set-quota-threshold <5..50>`
     - `--get-settings` / `--doctor` / `--prune-worktrees`

## 2. Workspace Sharing, Project Binding & `agy:` Naming

1. **Serial Same-Workspace Default**:
   - When only one executor edits files at a time (e.g., Codex implements backend → finishes → Gemini builds frontend on top), run directly in the **exact Antigravity workspace directory** with `--mode workspace-write`.
   - For read-only code review / audits, ALWAYS use `--mode read-only`.
   - ONLY use `--mode worktree` when Gemini and Codex will **edit code in parallel at the same time** (or when the user explicitly requests branch/worktree isolation).
2. **Codex Desktop Project & `agy:` Window Title**:
   - ALWAYS pass `--title "<concise task name>"`. `delegate_cli.py` automatically prefixes it as `agy: <concise task name>` via Codex's official `thread/name/set` RPC, and binds the session into the matching Codex Desktop project for that workspace (creating the project cleanly if missing, with backup & graceful degradation).
3. **Auto-Open Right Side Pane, Zero Polling & No Auto-Merge/Push**:
   - Launch `delegate_cli.py` via `run_command` with `WaitMsBeforeAsync: 1000` and `NotificationTimeoutSeconds: 600`.
   - **Pop open the right auxiliary pane automatically**: In the same turn as launching `delegate_cli.py` (or right after it backgrounds), invoke `call_mcp_tool` directly without spending a turn on `read_file` for `open_codex_orbit.json`:
     - Server: `agy-codex-orbit_codex_orbit`, Tool: `open_codex_orbit`
     - Round 1 Arguments (JSON object): `{"focus_running": true, "workspace_dir": "<CWD>"}`
     - Follow-up Arguments (JSON object): `{"session_id": "<SESSION_ID>", "focus_running": true}`
     Antigravity auto-opens or updates the right **Codex Orbit** side pane to display live progress at zero model tokens.
   - Immediately **stop calling tools and wait** for Antigravity's completion notification. NEVER poll logs or status in a loop.
   - Never auto-merge or push without explicit user approval.

## 3. Dispatching Round 1 (5-Part GPT-6.2 Brief)

Pass the brief via stdin (PowerShell single-quoted here-string) so multiline Unicode prompts never break Windows quoting:

```powershell
$rt = Get-Content "$env:LOCALAPPDATA\agy-codex-orbit\runtime.json" -Raw | ConvertFrom-Json
$prompt = @'
Goal: <concrete backend deliverable>

Context & Contracts:
- Relevant files: ...
- Shared API / schema contract with frontend: ...

Boundaries (Do Not Touch):
- Do not modify frontend files under ui/ (owned by Gemini).
- Keep unrelated files untouched.

Evidence:
- <error logs, failing tests, or current schema>

Definition of Done & Review Criteria:
- Run `<verification command>` and verify all tests pass.
- Report changed files, contract notes for frontend integration, and any residual risks.
'@

$prompt | & $rt.python_bin -u $rt.delegate_cli `
  --cwd "<ANTIGRAVITY_WORKSPACE>" `
  --mode workspace-write `
  --task-tier standard `
  --title "<简洁任务名>" `
  --source-model "Gemini"
```

- Explicit `--model <slug>` and `--reasoning-effort <level>` overrides remain supported whenever the user specifies them (or after `ask` mode confirms escalation).
- For read-only review: change `--mode workspace-write` to `--mode read-only` (e.g. `--task-tier standard` or explicit `--reasoning-effort medium` / `high`).
- For live external web research (e.g., company/market/documentation research in non-Git or Git workspaces such as `Career`):
  - Pass `--web-search` (enables Codex's native live `web_search` tool via `codex --search`).
  - When Gemini needs to read and interpret a detailed report (>1,800 chars) for the user, use `--mode workspace-write` and instruct Codex to write the full cited Markdown report to a workspace path (e.g., `research/<topic>.md`). Non-Git workspaces work automatically (`--skip-git-repo-check`). Once Codex finishes, read that Markdown file directly in Antigravity and synthesize/explain it against the user's local context.
  - When the user prefers zero local files written, use `--mode read-only --web-search`; the user can inspect the full Markdown in **Codex Orbit** and send/attach excerpts back to chat.
- For multi-part independent backend work, add: `Use subagents as needed to divide and move independent parts forward in parallel.`

## 4. Multi-Turn Follow-Up (`第 N 轮派单`)

Reuse `session_id` returned in Round 1's compact JSON summary (additive delta instructions only):

```powershell
$rt = Get-Content "$env:LOCALAPPDATA\agy-codex-orbit\runtime.json" -Raw | ConvertFrom-Json
$followup = @'
Follow-up:
- Address edge case: ...
- Re-run pytest and report results.
'@

$followup | & $rt.python_bin -u $rt.delegate_cli `
  --session-id "<SESSION_ID_FROM_ROUND_1>" `
  --model gpt-6.2-sol `
  --reasoning-effort high `
  --source-model "Gemini"
```

## 5. Handling the Result

- Consume ONLY the compact JSON summary returned by `delegate_cli.py` (`status`, `session_id`, `title`, `model`, `reasoning_effort`, `changed_files`, `duration_ms`, `codex_project`, `final_summary`, `error`). Never dump raw command stdout into the conversation.
- After backend completion in the shared workspace, review the changed files/tests and proceed directly with frontend integration in Gemini if part of the user's request.
