# Model Routing & Prompting Profile (`agy-codex-orbit`)

> **Static Profile Policy**: This profile is analyzed **once** and reused across all delegations. Do **not** search the web or re-benchmark models during normal tasks. Only re-analyze and update this file when the user explicitly states that the target model has updated (e.g., "对方模型更新了" / "更新模型画像").

- **Profile Version**: `2026-11-26.1`
- **Antigravity Host Model**: Gemini (Frontend UI / UX / Orchestration)
- **Local Codex Catalog Verified** (`~/.codex/models_cache.json`):
  - `gpt-6.2-sol` (efforts: `low`, `medium`, `high`, `xhigh`, `max`, `ultra`) — **Default delegation model**
  - `gpt-6.2-astra` (efforts: `low`, `medium`, `high`, `xhigh`, `max`, `ultra`) — **Opt-in escalation only (requires user confirmation + quota readout)**
  - `gpt-6.1-terra` (visual understanding / computer use; not needed for standard backend tasks)
- **Primary References**:
  - [Prompting GPT-6.2 in Codex | ChatGPT Learn](https://learn.chatgpt.com/guides/prompting-gpt-6-2-astra?site_locale=en)
  - GPT-6.2 Astra vs. GPT-6.2 Sol coding & cost benchmark comparisons (`2026-11`)

---

## 1. Current Pairing Division of Labor

| Area | Owner | Rationale |
| :--- | :--- | :--- |
| **Frontend & UI** (HTML/CSS/layout, visual polish, component interactivity, theme integration, UX iteration) | **Gemini (Antigravity)** | Faster visual/design iteration directly in Antigravity with immediate user feedback. |
| **Backend & Systems** (architecture, APIs, business logic, data models, parsers, SQL/persistence, CLI/automation, complex debugging, unit/integration tests, read-only code review) | **GPT-6.2 in Codex** | Strong agentic tool execution, terminal-driven debugging, AST/refactor precision, and self-verified backend delivery. |
| **Shared Interface Contracts** (JSON schemas, TypeScript types, REST/MCP payloads, file ownership boundaries) | **Agreed upfront by Gemini, enforced in brief** | Prevents integration drift when Gemini builds frontend on top of GPT's backend. |

---

## 2. Three Model Routing Policies (Persisted in `${PLUGIN_DATA}/settings.json`)

The user can switch among 3 policies at any time either in the **Codex Orbit** panel header dropdown or via chat (`delegate_cli.py --set-routing-mode <mode>`):

| Policy (`routing_mode`) | Display Name | Behavior |
| :--- | :--- | :--- |
| **`silent_low_cost`** | 节能 (Eco) | Always silently dispatches **`gpt-6.2-sol`** with conservative effort (`low`/`medium` for light/standard, `high` for complex). Never escalates to Astra and never interrupts the user for confirmation. |
| **`ask`** *(Default)* | 均衡 (Balanced) | Silently uses **`gpt-6.2-sol`** (`medium`/`high`) for `light`/`standard` tasks. For `complex` tasks (Astra escalation candidates), **NEVER silently uses Astra** — queries live Codex quota & reset times (`--plan-route --task-tier complex`) and asks the user to choose between `gpt-6.2-astra` and `gpt-6.2-sol`. |
| **`silent_best`** | 极致 (Max) | Silently selects the optimal model & effort without cost limits or confirmation prompts: `light` → `gpt-6.2-sol` (`medium`), `standard` backend/review → `gpt-6.2-astra` (`high`), `complex` architecture/cross-module debugging → `gpt-6.2-astra` (`xhigh`; `max`/`ultra` only if multi-cause exhaustive verification is truly needed). |

### Model Characteristics

### `gpt-6.2-sol` (Default Cost-Efficient Workhorse)
- **Strengths**: ~4× cheaper and far more generous 5h/7d Codex plan headroom than Astra; very close to Astra (~0.5 pt gap on focused code-generation suites like FrontierCode 2.0); fast time-to-first-token; naturally concise output.
- **Weaknesses**: Slightly less resilient than Astra on deeply cascading multi-service root-cause debugging or huge cross-cutting architecture overhauls.
- **Usage Rule**: Always explicitly set `--reasoning-effort` (or pass `--task-tier`) so tasks never accidentally inherit `ultra` from local `config.toml`.

### `gpt-6.2-astra` (Flagship Deep Reasoning)
- **Strengths**: Top-tier deep reasoning (~97% GPQA Diamond) and #1-tier long-horizon agentic execution (Terminal-Bench / complex multi-service debugging / intricate concurrency & security audits).
- **Weaknesses**: ~4× cost / rapid Codex quota consumption; long time-to-first-token latency at `xhigh`/`max`/`ultra`.
- **Escalation Behavior**:
  - Under **`silent_low_cost`**: Do not use Astra.
  - Under **`ask` (default)**: Run `delegate_cli.py --plan-route --task-tier complex` (which automatically fetches live Codex 5h & 7d remaining quota + local reset time and Antigravity quota note), explain why Astra is recommended, and wait for user approval before dispatching.
  - Under **`silent_best`**: Dispatch `gpt-6.2-astra` directly (`high` for `standard`, `xhigh` for `complex`) without asking.

### Task Tier × Policy Matrix

| Task Tier (`--task-tier`) | Examples | `silent_low_cost` | `ask` *(Default)* | `silent_best` |
| :--- | :--- | :--- | :--- | :--- |
| **`light`** | Codebase survey, quick lookup, simple single-file helper | `sol` (`low`/`medium`) | `sol` (`medium`) | `sol` (`medium`) |
| **`standard`** | Multi-file backend feature/API/CLI, test suite, standard read-only review | `sol` (`medium`) | `sol` (`medium` review / `high` code) | `astra` (`high`) |
| **`complex`** | Major architecture redesign, subtle concurrency/race/protocol bug, cascading cross-service debugging, high-stakes security/migration audit | `sol` (`high`) | **Ask user + quota readout** (`astra` `high`/`xhigh` vs `sol` `xhigh`) | `astra` (`xhigh`) |

---

## 3. GPT-6.2 Codex Prompting Rules

1. **Brief Like a Senior Colleague (5-Part Structure)**:
   - **Goal**: Concrete deliverable and backend scope.
   - **Context & Contracts**: Relevant files, tech stack, exact JSON/API/TypeScript contracts shared with Gemini's frontend.
   - **Boundaries (Do Not Touch)**: Explicitly list frontend UI directories/files owned by Gemini and unrelated files that must stay untouched.
   - **Evidence**: Failing test output, error traces, schema snippets, or repro steps.
   - **Definition of Done & Review Criteria**: Exact test/lint commands Codex must run and what you will inspect on review.
2. **Do Not Over-Prompt Conciseness**: GPT-6.2 is already tuned to be direct and concise. Omit redundant phrases like *"be concise"* or *"keep it brief"* and invest prompt space in invariants, edge cases, and verification criteria.
3. **Explicitly Authorize Subagents for Multi-Part Backend Work**: When a task has 2+ independent backend areas (e.g., auditing separate modules or implementing service + migration + tests), include:
   > *"Use subagents as needed to divide and move independent parts forward in parallel."*
4. **Additive Multi-Turn Follow-Ups**: On follow-up rounds (`--session-id`), append focused deltas (failing case, contract tweak, review findings) rather than rewriting the original prompt.

---

## 4. Manual Profile Refresh Protocol

Trigger **only** when the user explicitly indicates a model update (e.g., "对方模型更新了", "Codex 出了新模型", "更新模型画像"):
1. Inspect local `%USERPROFILE%\.codex\models_cache.json` for updated model slugs and supported `reasoning_effort` levels.
2. Search official OpenAI Codex prompting guides and independent coding/cost benchmarks for the newly released models.
3. Update this file (`references/model_profile.md`) with new `profile_version`, `last_analyzed`, strengths/weaknesses, and routing matrix.
4. Re-run `scripts/dev-link.ps1` so the installed plugin picks up the updated reference file.
