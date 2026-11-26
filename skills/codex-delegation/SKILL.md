---
name: codex-delegation
description: Delegate scoped coding, refactoring, testing, or read-only code review tasks to local OpenAI Codex CLI with workspace isolation, multi-turn resume, zero-token live monitoring in Codex Orbit, and compact summary return.
---

# Codex Delegation Workflow

Use this skill when delegating a well-scoped medium or large coding, testing, debugging, or independent code review task to local OpenAI Codex CLI while keeping Gemini's context window clean and monitoring progress in **Codex Orbit**.

## 1. When to Delegate

- **Delegate**: multi-file feature implementation, isolated refactoring, comprehensive test suites, or independent read-only code review with clear inputs and acceptance criteria.
- **Do directly (do not delegate)**: trivial 1–5 line edits, quick file reads, or tasks with ambiguous requirements that first need clarification from the user.

## 2. Hard Isolation & Zero-Polling Rules

1. **Workspace isolation is mandatory**:
   - **Code review / audit / investigation**: ALWAYS use `--mode read-only`.
   - **Code changes inside a Git repository**: ALWAYS use `--mode worktree`. The wrapper creates a dedicated git worktree under `${PLUGIN_DATA}/worktrees/` on round 1 and automatically reuses the same worktree on follow-up rounds, keeping the user's main working tree untouched.
   - **In-place writes (`--mode workspace-write`)**: ONLY when the target is not a Git repository or the user explicitly asks to edit the current working tree directly.
2. **Zero polling**:
   - Launch `delegate_cli.py` via `run_command` with `WaitMsBeforeAsync: 5000` and `NotificationTimeoutSeconds: 600`.
   - After launching, **stop calling tools and wait** for Antigravity's automatic background task completion notification. NEVER poll logs, status, or files in a loop.
   - Users monitor real-time progress at zero model token cost in the **Codex Orbit** side pane (you may optionally call `open_codex_orbit` once when starting if the user asked to see the panel).
3. **Never auto-merge or push**:
   - After a `worktree` round completes, inspect the returned `changed_files` and `worktree_path` (e.g., `git -C <worktree_path> diff --stat`) and report findings/changes to the user before merging into the main branch.

## 3. Dispatching Round 1 (New Session)

Pass the prompt through **stdin** (using a PowerShell single-quoted here-string) so multiline Unicode prompts never hit Windows command-line quoting or length limits.

Locate the plugin Python interpreter and `delegate_cli.py` automatically from Antigravity's Codex Orbit runtime pointer (`%LOCALAPPDATA%\agy-codex-orbit\runtime.json`, written when the plugin starts):

```powershell
$rt = Get-Content "$env:LOCALAPPDATA\agy-codex-orbit\runtime.json" -Raw | ConvertFrom-Json
$prompt = @'
Task: <clear scoped objective>

Context & constraints:
- Target files / modules: ...
- Do not modify unrelated files.
- Acceptance criteria & verification command: ...
'@

$prompt | & $rt.python_bin -u $rt.delegate_cli `
  --cwd "<TARGET_WORKSPACE>" `
  --mode worktree `
  --source-model "Gemini"
```

For a **read-only code review**, change `--mode worktree` to `--mode read-only`.

## 4. Multi-Turn Follow-Up (`第 N 轮派单`)

`delegate_cli.py` prints a compact JSON summary containing `session_id`, `round`, `worktree_path`, `changed_files`, `command_count`, `duration_ms`, and `final_summary`.

To send follow-up instructions to the **same** Codex session (preserving conversation context and reusing the exact same git worktree), pass `--session-id <session_id>`:

```powershell
$rt = Get-Content "$env:LOCALAPPDATA\agy-codex-orbit\runtime.json" -Raw | ConvertFrom-Json
$followup = @'
Round 2 follow-up:
- Address the edge case in ...
- Re-run pytest and report results.
'@

$followup | & $rt.python_bin -u $rt.delegate_cli `
  --session-id "<SESSION_ID_FROM_ROUND_1>" `
  --source-model "Gemini"
```

## 5. Handling the Result

- Consume ONLY the compact JSON summary returned by `delegate_cli.py` (status, duration, changed files, final conclusion/error). Do not dump full raw command stdout into the conversation.
- If `status` is `"failed"` or `"cancelled"`, report the concise `error` and `session_id` so the user can inspect full logs in **Codex Orbit** or decide whether to resume.
