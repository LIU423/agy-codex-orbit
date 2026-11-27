<div align="center">
  <img src="./assets/logo.svg" alt="Codex Orbit logo" width="64" height="64" />
  <h1>Codex Orbit</h1>
  <p>
    <strong>面向 Google Antigravity 2.0 的雙 Agent 原生協作與零 Token 即時監控駕駛艙</strong>
  </p>
  <p>
    <a href="./README.md">English</a> · <a href="./README.zh-CN.md">简体中文</a> · <strong>繁體中文</strong> · <a href="./README.ja.md">日本語</a>
  </p>
  <p>
    <a href="#為什麼選擇-codex-orbit"><img src="https://img.shields.io/badge/Antigravity-2.0%20MCP%20App-2563eb?style=flat-square" height="20" alt="Antigravity 2.0 MCP App" /></a> <a href="#快速開始"><img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-475569?style=flat-square" height="20" alt="Cross-platform" /></a> <a href="#核心特色"><img src="https://img.shields.io/badge/Version-0.7.0-0ea5e9?style=flat-square" height="20" alt="Version 0.7.0" /></a> <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-16a34a?style=flat-square" height="20" alt="MIT License" /></a>
  </p>
</div>

![Codex Orbit 全貌示範](./assets/screenshots/codex-orbit-demo.gif)

*一次看全：52 週 AGY 派單與自行發起活動熱力圖、模型分佈、低額度保護與 7 項環境自檢、即時多輪監控及深淺色多語言介面——全程零宿主模型 Token 消耗。*

---

## 為什麼選擇 Codex Orbit？

Codex Orbit 讓 **Antigravity 中的 Gemini** 將編碼、測試、除錯與審查任務交給本機 **OpenAI Codex CLI**。你可以在 Orbit 面板即時查看進度（無需在對話中輪詢），對話只會收到精簡結果，需要跟進時直接續接原本的 Codex 工作階段。

```mermaid
flowchart LR
  subgraph Control["任務派單鏈路（Gemini ↔ 包裝器）"]
    G["Gemini (Antigravity)"] -->|"任務簡報 + 執行模式"| W["delegate_cli.py"]
    W -->|"exec / exec resume + 沙箱"| C["本機 Codex CLI"]
    C -->|"JSONL 事件 + 最終訊息"| W
    W -->|"精簡 JSON 結果"| G
  end
  subgraph Monitor["MCP 面板監控（App-Only 工具）"]
    R["rollout-*.jsonl（位元組游標）+ state_5.sqlite"] --> M["Codex Orbit MCP 服務"]
    Q["codex app-server（5h/7d 額度）+ models_cache.json"] --> M
    W -->|"寫入委派紀錄與 Desktop 同步"| R
    M <-->|"App-Only 輪詢與控制（對 Gemini 不可見）"| UI["Orbit 面板（簡中 / 繁中 / EN / 日本語）"]
    UI -.->|"可選：由使用者主動回傳摘要"| G
  end
```

### 與其他方案的定位比較

不同工具解決的是不同層級的問題：API 閘道負責轉發與路由模型請求；Agent 委派工具負責跨端調起助手；**Codex Orbit** 專注於把 **Antigravity 內嵌即時面板、派單前額度護欄，以及本機全量 Codex 歷史檢視** 整合在一起：

| 比較項目 | 模型 API 閘道<br/>*(OpenRouter / LiteLLM / CCR / CLIProxyAPI)* | Agent 委派工具<br/>*(PAL MCP / `codex-mcp` / `codex-plugin-cc`)* | **Codex Orbit (`agy-codex-orbit`)** |
| :--- | :--- | :--- | :--- |
| **工作單位** | 路由**單次 HTTP 模型請求**（選路、協議轉換、故障回退；用戶端仍保留各自的工具迴圈） | 從宿主調起**另一套 Agent/CLI**（多款支援工作階段 ID、子 CLI 或背景任務） | **面向 Antigravity 的 Codex 任務駕駛艙**：封裝 `codex exec` / `resume`、沙箱選項與 `--search` |
| **進度觀測與上下文** | 閘道側提供紀錄／用量介面；模型上下文佔用取決於呼叫端 | 通常在宿主對話內透過工具回傳值或狀態輪詢取得結果 | **獨立面板外置觀測**：面板輪詢與控制皆宣告 `visibility: ["app"]`（看進度不耗模型 Token），主對話只收簡短 JSON |
| **認證與額度可見性** | 支援 API Key、Credits 以及 OAuth／訂閱上游接入 | 多數沿用本機 CLI 登入或設定的 API Key | **沿用本機 Codex 登入**，並在 Orbit 頂欄直覺顯示目前可取得的 5h/7d 額度視窗 |
| **派單前額度策略** | 側重限流重試、憑證輪詢與上游模型回退 | 多數按明確指定或角色選模 | **節能 / 均衡 / 極致**：`極致` 在 5h 額度低於門檻時於派單前改用預設模型（保留推理強度）；`均衡` 複雜任務先提醒確認 |
| **歷史紀錄與 Desktop 聯動** | 閘道級請求紀錄與統計 | 按各自外掛或橋接工作階段 ID 續接 | **52 週統一 Profile 檢視**（合併 AGY 派單與自行發起的本機紀錄），並在相容的 Codex Desktop 中自動歸類 `agy:` 執行緒 |
| **工作區與控制** | 由實際執行的用戶端管理工作區與程序 | 常見支援唯讀審查、背景取消或 worktree 封裝 | 支援**同目錄**、**唯讀**、**獨立 Git worktree**，面板一鍵終止受管程序樹並安全清理閒置 worktree |

---

## 核心特色

### 1. 面板外置即時監控 & 52 週 Profile 總覽
- **雙入口掛載**：同時掛載於 Antigravity 左側欄（`global`）與右側輔助欄（`aux_pane`），當 Gemini 呼叫 `open_codex_orbit(focus_running=True)` 時自動聚焦執行中的派單任務；面板輪詢與控制工具皆宣告 `visibility: ["app"]`，檢視進度不消耗宿主模型 Token。
- **Profile 總覽與熱力圖**：呈現累計 Token、AGY 派單佔比、最長任務耗時、連續活躍天數、**52 週日曆熱力圖**（支援 `全部 / AGY 派單 / 自行發起` 與 `Token / 工作階段數` 切換、點擊日期下鑽篩選）、**模型用量分佈**與高頻變更檔案。
- **清爽閱讀畫布**：可收合的 Prompt 與思考摘要、GFM Markdown 渲染、預設收合為單行灰階的指令列，以及點擊即可跳轉至 Antigravity 編輯器的變更檔案標籤。
- **多語言介面**：支援 `自動`（跟隨宿主語言）、`简体中文`、`繁體中文`、`EN`、`日本語`。

### 2. 額度感知三檔排程 & 低額度保護
可隨時從頂部下拉選單或對話切換策略；**預設主力模型**（`gpt-6.2-sol`）與**複雜升級模型**（`gpt-6.2-astra`）皆可在 **⚙ 設定** 中依據本機 `~/.codex/models_cache.json` 目錄自由切換。

| 任務難度 | 節能 (`silent_low_cost`) | 均衡 (`ask`，預設) | 極致 (`silent_best`) |
| :--- | :--- | :--- | :--- |
| **輕量 (`light`)** | 預設模型 (`low`/`medium`) | 預設模型 (`medium`) | 預設模型 (`medium`) |
| **一般 (`standard`)** | 預設模型 (`medium`) | 預設模型 (`medium` 審查 / `high` 開發) | 升級模型 (`high`) * |
| **複雜 (`complex`)** | 預設模型 (`high`) | **附即時 5h/7d 額度請使用者拍板** | 升級模型 (`xhigh`) * |

*\* **低額度自動保護**（預設於 5h 剩餘 `< 15%` 時觸發，門檻可於 `5%–50%` 調整）：`極致` 模式下自動回退至預設模型並保留目標推理強度；`均衡` 模式下於升級確認中附帶低額度提醒；頂部額度徽章自動轉為琥珀色／紅色預警。*

### 3. 工作區模式、聯網研究與即時控制
- **同目錄交接 (`workspace-write`，預設)**：依循分工約定前後端循序開發時，Codex 直接在目前 Antigravity 工作區寫入程式碼，省去額外合併步驟。
- **唯讀審查 (`read-only`)**：獨立稽核變更或排查問題，Codex 沙箱不修改工作區檔案。
- **並行隔離 (`worktree`)**：於 `${PLUGIN_DATA}/worktrees` 下自動建立獨立 Git worktree，多輪 `resume` 自動沿用同一隔離目錄，結束後可透過 **⚙ 設定 → 清理閒置 Worktree** 或 `--prune-worktrees` 一鍵安全清理。
- **聯網研究 (`--web-search`)**：啟用 Codex 原生 `--search` 工具，於 Git 或非 Git 目錄中完成即時調查並輸出附來源引用的 Markdown 報告。
- **即時終止與按需回傳**：一鍵終止由 Orbit 派出的執行中任務程序樹，並可將整場或單輪精簡摘要附加／回傳至目前對話。

### 4. 與 Codex Desktop 銜接
派單工作階段會嘗試透過 `codex app-server`（`thread/name/set`）自動加上 `agy: ` 前綴，綁定或建立對應的工作區專案與群組，並在執行結束後提升閉合 `exec` rollout 的可見性，便於在相容的 Codex Desktop 環境中繼續檢視或接管*（若 Codex Desktop 當時已開啟，新建專案可能需要重啟用戶端後重新整理顯示）*。

---

## 快速開始

### 前置需求
1. **Google Antigravity 2.0**
2. 已安裝並登入 **OpenAI Codex CLI**（或 Codex Desktop / VS Code Codex 擴充功能）
3. **Python 3.11+** *（一般使用者無需安裝 Node.js 或 `pnpm`，套件已內建編譯完成的單檔前端 `dist/index.html`）*

### 一鍵安裝

**Windows (PowerShell)：**
```powershell
git clone https://github.com/LIU423/agy-codex-orbit.git
cd agy-codex-orbit
.\scripts\install.ps1
```
> 若 `python` 不在 `PATH` 中，可傳入 `-PythonBin C:\Path\To\python.exe`。

**macOS / Linux (`sh` / `bash` / `zsh`)：**
```bash
git clone https://github.com/LIU423/agy-codex-orbit.git
cd agy-codex-orbit
./scripts/install.sh
```
> 若需指定 Python 直譯器，可執行 `PYTHON_BIN=/path/to/python3 ./scripts/install.sh`。

安裝腳本會將外掛部署至 `~/.gemini/config/plugins/agy-codex-orbit/`，在外掛目錄內建立獨立 `.venv`、產生 `mcp_config.json` 並自動執行 `--doctor` 環境自檢。若面板未立即顯示，請在 Antigravity 中重新載入外掛或重啟用戶端。

### 健康檢查
- **介面內檢查**：點擊頂部 **⚙** 設定圖示 → **執行自檢**。
- **命令列檢查**：
  ```powershell
  # Windows
  ~/.gemini/config/plugins/agy-codex-orbit/.venv/Scripts/python.exe -u ~/.gemini/config/plugins/agy-codex-orbit/server/delegate_cli.py --doctor
  ```
  ```bash
  # macOS / Linux
  ~/.gemini/config/plugins/agy-codex-orbit/.venv/bin/python -u ~/.gemini/config/plugins/agy-codex-orbit/server/delegate_cli.py --doctor
  ```

---

## 常見對話用法

在 Antigravity 對話中直接以自然語言指揮即可：

- **開啟監控面板**：*「打開 Codex Orbit，查看最近的 Codex 工作階段與剩餘額度。」*
- **前後端分工協作**：*「讓 Codex 在目前工作區完成 `/api/webhooks` 驗簽邏輯與 pytest 單元測試，完成後由你接上前端表單。」*
- **獨立唯讀審查**：*「讓 Codex 以唯讀模式審查目前未提交的變更，重點檢查競態與例外處理。」*
- **並行 Worktree 開發**：*「把解析器重構放到隔離 git worktree 讓 Codex 處理，我們這邊繼續調整前端。」*
- **聯網調研並撰寫報告**：*「讓 Codex 開啟聯網搜尋研究最新 MCP Apps 規範變更，將附來源引用的報告寫到 `research/mcp-apps.md`。」*
- **切換排程策略**：*「切換到節能模式」* / *「切換到均衡模式」* / *「切換到極致模式」*。

---

## 設定與疑難排解

### 設定項目與 CLI 參數
所有設定持久化於 `${PLUGIN_DATA}/settings.json`，可在 **⚙ 設定** 彈窗或透過 `delegate_cli.py` 修改：

| 設定 / CLI 參數 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `--set-routing-mode` | `ask` | `silent_low_cost`（節能）、`ask`（均衡）、`silent_best`（極致） |
| `--set-default-model` | `gpt-6.2-sol` | 預設主力模型（傳入空字串恢復預設） |
| `--set-escalation-model` | `gpt-6.2-astra` | 複雜／極致模式升級模型（傳入空字串恢復預設） |
| `--set-quota-guardrail` | `on` | `on` / `off`，開關 5h 低額度自動保護 |
| `--set-quota-threshold` | `15` | 5h 剩餘額度觸發門檻百分比（`5`–`50`） |
| `--get-settings` / `--doctor` / `--prune-worktrees` | — | 檢視設定、執行 7 項環境自檢、清理閒置 worktree |

### 環境變數
- `CODEX_ORBIT_CODEX_BIN`：當 `codex` 不在預設路徑時，明確指定執行檔路徑。
- `CODEX_HOME`：覆寫 Codex 設定目錄（預設 `~/.codex`）。
- `CODEX_ORBIT_DATA_DIR`：覆寫外掛資料儲存目錄。

### 常見問題
- **`codex_cli` 檢查失敗**：請確認已安裝並登入 Codex CLI，或設定 `CODEX_ORBIT_CODEX_BIN`。
- **額度顯示來自 rollout 快取**：即時額度優先透過 `codex app-server` 讀取；離線或未登入時會自動回退讀取最新本機 rollout 中的 `rate_limits`。
- **原始碼開發聯調模式**：開發者請執行 `.\scripts\dev-link.ps1`（Windows）或 `./scripts/dev-link.sh`（macOS/Linux），詳見 [開發文件](./docs/DEVELOPMENT.md)。

---

## 開發與測試

UI 建置（`ui/`）、開發模式連結（`dev-link.ps1` / `dev-link.sh`）、MCP 熱重新載入（`refresh-mcp.ps1`）以及 72 項完整合成單元測試（`pytest -v`）說明，請參閱 [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md)。

## 授權條款

採用 [MIT License](./LICENSE) 授權釋出。
