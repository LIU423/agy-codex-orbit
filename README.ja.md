<div align="center">
  <img src="./assets/logo.svg" alt="Codex Orbit logo" width="64" height="64" />
  <h1>Codex Orbit</h1>
  <p>
    <strong>Google Antigravity 2.0 向け Codex 委任ハーネス ＆ ゼロトークン監視コックピット</strong>
  </p>
  <p>
    <a href="./README.md">English</a> · <a href="./README.zh-CN.md">简体中文</a> · <a href="./README.zh-TW.md">繁體中文</a> · <strong>日本語</strong>
  </p>
  <p>
    <a href="#codex-orbit-が選ばれる理由"><img src="https://img.shields.io/badge/Antigravity-2.0%20MCP%20App-2563eb?style=flat-square" height="20" alt="Antigravity 2.0 MCP App" /></a> <a href="#クイックスタート"><img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-475569?style=flat-square" height="20" alt="Windows | macOS | Linux" /></a> <a href="#主な機能"><img src="https://img.shields.io/badge/Version-0.1.0-0ea5e9?style=flat-square" height="20" alt="Version 0.1.0" /></a> <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-16a34a?style=flat-square" height="20" alt="MIT License" /></a> <a href="https://github.com/LIU423/agy-codex-orbit/actions/workflows/ci.yml"><img src="https://github.com/LIU423/agy-codex-orbit/actions/workflows/ci.yml/badge.svg?branch=main" height="20" alt="CI" /></a>
  </p>
</div>

**Codex Orbit** は、Google Antigravity 2.0 上の Gemini からローカルの **OpenAI Codex CLI** へコーディング・テスト・調査・レビューを委任するための MCP App プラグインです。Skill ベースのモデルルーティング、Git worktree 隔離、そしてホストモデルのトークンを一切消費しないライブ監視ダッシュボードを備えています。

![Codex Orbit 全体デモ](./assets/screenshots/codex-orbit-demo.gif)

*52 週間 AGY 依頼・直接実行ヒートマップ、モデル利用内訳、5h/7d 残量ガードレール、7 項目環境診断、ライブストリーム監視、ライト／ダーク多言語 UI。*

---

## Codex Orbit が選ばれる理由

エージェント間のタスク委任では、進捗確認のポーリングでホスト側のトークンを消費したり、裏で走るワーカーの状態が見えなくなったりしがちです。Codex Orbit は**実行**と**観測**を分離します。Gemini は `delegate_cli.py` 経由でスコープ付きタスクを投入し、進捗確認や追加指示は Orbit パネル（`visibility: ["app"]`）上で完結するため、Gemini には完了時のコンパクトな JSON 要約だけが返ります。

```mermaid
flowchart LR
  subgraph Control["タスク委任フロー（Gemini ↔ ラッパー）"]
    G["Gemini (Antigravity)"] -->|"タスク指示 + 実行モード"| W["delegate_cli.py"]
    W -->|"exec / exec resume + サンドボックス"| C["ローカル Codex CLI"]
    C -->|"JSONL イベント + 最終出力"| W
    W -->|"コンパクトな JSON 結果"| G
  end
  subgraph Monitor["MCP パネル監視（App-Only ツール）"]
    R["rollout-*.jsonl（バイトカーソル）+ state_5.sqlite"] --> M["Codex Orbit MCP サーバー"]
    Q["codex app-server（5h/7d 残量）+ models_cache.json"] --> M
    W -->|"実行メタデータ記録 ＆ Desktop 連携"| R
    M <-->|"App-Only ポーリング ＆ 制御（Gemini から不可視）"| UI["Orbit パネル（EN / 简中 / 繁中 / 日本語）"]
    UI -.->|"任意：ユーザーが要約をチャットへ連携"| G
  end
```

### 他ツールとの比較

| 比較項目 | モデル API ゲートウェイ<br/>*(OpenRouter / LiteLLM / CCR / CLIProxyAPI)* | エージェント委任ツール<br/>*(PAL MCP / `codex-mcp` / `codex-plugin-cc`)* | **Codex Orbit (`agy-codex-orbit`)** |
| :--- | :--- | :--- | :--- |
| **処理単位** | **HTTP モデルリクエスト単位**でルーティング（プロバイダ切替・形式変換・フォールバック） | ホストから**別エージェント／CLI タスク**を呼び出し | **Antigravity → Codex コックピット**：`codex exec` / `resume`、サンドボックス、`--search` を統合 |
| **進捗確認とコンテキスト** | ゲートウェイ側にログあり。コンテキスト消費はクライアント依存 | ホスト会話内のツール返却やステータスポーリングで取得 | **会話外の専用パネルで監視**：監視・追指示は `visibility: ["app"]`（ホストトークン 0）、チャットには短い JSON のみ返却 |
| **認証とクォータ** | API キー、クレジット、OAuth／サブスク上流 | ローカル CLI 認証や設定済み API キーを再利用 | **ローカル Codex ログインを再利用**し、ライブの 5h/7d 残量ウィンドウをヘッダーに表示 |
| **残量連動ルーティング** | レート制限リトライ、キーローテーション、フォールバック | 明示指定や静的な役割マッピング | **エコ / バランス / 最高**：`最高` は 5h 残量がしきい値未満なら起動前に標準モデルへ切替（推論強度は維持）、`バランス` は昇格前に確認 |
| **履歴と Desktop 連携** | ゲートウェイ単位のリクエストログ | 各ブリッジ／プラグイン単位のセッション再開 | **52 週間統合プロフィール**（AGY 依頼＋直接実行をローカル JSONL/SQLite から集計）＋ Codex Desktop での `agy:` タイトル・プロジェクト紐付け |
| **ワークスペースと制御** | 呼び出し元クライアント側で管理 | 読取専用レビュー、バックグラウンド停止、worktree ラッパー | **同一フォルダ**・**読取専用**・**Git worktree**、プロセスツリー停止ボタン、待機中 worktree の一括削除 |

---

## 主な機能

### 1. 会話外ライブ監視 ＆ 52 週間プロフィール概要
- **2 か所への統合表示と直接フォローアップ**：Antigravity の左サイドバー（`global`）と右補助ペイン（`aux_pane`）の両方に表示され、Gemini が `open_codex_orbit(focus_running=True)` を呼ぶと実行中タスクを自動フォーカスします。狭い右サイドペインではセッション一覧がドロワーに収納され、下部に **Codex 直接フォローアップ入力欄**（`orbit_send_followup`）が常駐します。監視も追加指示も `visibility: ["app"]` のためホストモデルのトークンを消費しません。
- **プロフィール統計 ＆ ヒートマップ**：累計トークン、AGY 依頼比率、最長実行時間、連続稼働日数、**52 週間ヒートマップ**（`すべて / AGY 依頼 / 直接実行`・`トークン / セッション数` 切替、日付クリック絞り込み）、**モデル利用内訳**、よく変更されるファイルを集計。
- **読みやすいキャンバス**：プロンプトや思考メモの折りたたみ、GFM Markdown 表示、1 行に集約されたコマンドログ、クリックで Antigravity に開く変更ファイルチップを搭載。
- **多言語 UI**：`自動`（ホスト言語に追従）、`EN`、`简体中文`、`繁體中文`、`日本語`。

<p align="center">
  <img src="./assets/screenshots/sidepane-followup.png" width="380" alt="Antigravity の右補助ペインにドッキングされた Codex Orbit と下部の Codex 直接フォローアップ入力欄" />
</p>

### 2. クォータ連動 3 段階ルーティング ＆ 低残量ガードレール
ヘッダーのドロップダウンまたはチャットからいつでも切り替え可能です。**標準モデル**（`gpt-6.2-sol`）と**上位モデル**（`gpt-6.2-astra`）は **⚙ 設定** から `~/.codex/models_cache.json` 内のモデルへ変更できます。

| タスク難易度 | エコ (`silent_low_cost`) | バランス (`ask`・初期値) | 最高 (`silent_best`) |
| :--- | :--- | :--- | :--- |
| **軽量 (`light`)** | 標準モデル (`low`/`medium`) | 標準モデル (`medium`) | 標準モデル (`medium`) |
| **標準 (`standard`)** | 標準モデル (`medium`) | 標準モデル (レビュー `medium` / 実装 `high`) | 上位モデル (`high`) * |
| **複雑 (`complex`)** | 標準モデル (`high`) | **5h/7d 残量を表示してユーザー確認** | 上位モデル (`xhigh`) * |

*\* **低残量ガードレール**（初期値 5h 残量 `< 15%`、`5%–50%` で調整可能）：`最高` では推論強度を維持したまま標準モデルへ自動切替、`バランス` では昇格確認時に注意を表示、ヘッダーのクォータバッジが警告色に変化します。*

### 3. ワークスペースモード、Web 検索 ＆ ライブ制御
- **同一フォルダ連携 (`workspace-write`・初期値)**：Gemini と Codex が順番に編集する場合、マージ不要で現在のワークスペースに直接反映します。
- **読取専用レビュー (`read-only`)**：ワークスペースのファイルを変更せずに独立したコードレビューや調査を実行します。
- **並列隔離 (`worktree`)**：`${PLUGIN_DATA}/worktrees` に独立した Git worktree を作成し、複数ターンで再利用します。待機中の worktree は **⚙ 設定 → 待機中 Worktree を削除** または `--prune-worktrees` で整理できます。
- **ライブ Web リサーチ (`--web-search`)**：Git 管理外のフォルダでも `codex --search` による調査を行い、出典付き Markdown レポートを保存します。
- **プロセスツリー制御 ＆ 要約連携**：Orbit 管理下の実行中プロセスツリーを停止し、必要なセッション／ラウンド要約だけをチャットへ返せます。

### 4. Codex Desktop 連携
依頼されたセッションには `thread/name/set` により `agy: ` プレフィックスを付与し、対応プロジェクト・セクションへ紐付けた上で終了後に可視性を引き上げるため、対応した Codex Desktop 環境でそのまま続きを確認できます*（Codex Desktop が起動中の場合、新規プロジェクトの表示には再起動が必要になることがあります）*。

---

## クイックスタート

### 前提条件
1. **Google Antigravity 2.0**
2. **OpenAI Codex CLI**（または Codex Desktop / VS Code Codex 拡張）がインストール・サインイン済み
3. **Python 3.11 以上** *（Node.js や `pnpm` は不要です。ビルド済み `dist/index.html` を同梱しています）*

### インストール

**Windows (PowerShell):**
```powershell
git clone https://github.com/LIU423/agy-codex-orbit.git
cd agy-codex-orbit
.\scripts\install.ps1
```
> `python` に PATH が通っていない場合は `-PythonBin C:\Path\To\python.exe` を指定してください。

**macOS / Linux (`sh` / `bash` / `zsh`):**
```bash
git clone https://github.com/LIU423/agy-codex-orbit.git
cd agy-codex-orbit
./scripts/install.sh
```
> 別の Python を使う場合は `PYTHON_BIN=/path/to/python3 ./scripts/install.sh` を実行してください。

インストーラーは `~/.gemini/config/plugins/agy-codex-orbit/` へプラグインを配置し、専用 `.venv` の作成・`mcp_config.json` の生成・`--doctor` 環境診断まで自動で行います。**Codex Orbit** が表示されない場合はプラグインを再読み込みするか Antigravity を再起動してください。

### 動作確認 (`--doctor`)
- **UI から**: ヘッダー右上の **⚙** → **診断を実行**
- **ターミナルから**:
  ```powershell
  # Windows
  ~/.gemini/config/plugins/agy-codex-orbit/.venv/Scripts/python.exe -u ~/.gemini/config/plugins/agy-codex-orbit/server/delegate_cli.py --doctor
  ```
  ```bash
  # macOS / Linux
  ~/.gemini/config/plugins/agy-codex-orbit/.venv/bin/python -u ~/.gemini/config/plugins/agy-codex-orbit/server/delegate_cli.py --doctor
  ```

---

## 活用例

Antigravity のチャットから自然な言葉で指示できます：

- **ライブ監視を開く**：*「Codex Orbit を開いて、最近の Codex セッションとクォータ残量を確認したい。」*
- **バックエンド・フロントエンド分業**：*「このワークスペースで `/api/webhooks` の署名検証と pytest を Codex に実装させ、完了後にフロントエンドのフォームを接続して。」*
- **読取専用レビュー**：*「未コミットの変更を Codex に read-only モードでレビューさせ、競合状態や例外処理の漏れを確認して。」*
- **並列 Worktree**：*「パーサーのリファクタリングは分離した git worktree で Codex に任せ、こちらでは UI を進めよう。」*
- **Web 調査レポート**：*「Codex の Web 検索を使って最新の MCP Apps 仕様変更を調査し、`research/mcp-apps.md` に出典付きでまとめて。」*
- **ルーティング切替**：*「エコモードに切り替えて」* / *「バランスモードにして」* / *「最高モードにして」*。

---

## 設定とトラブルシューティング

### 設定項目 ＆ CLI フラグ
設定は `${PLUGIN_DATA}/settings.json` に永続化され、**⚙ 設定** または `delegate_cli.py` から変更できます：

| 設定 / CLI フラグ | 初期値 | 内容 |
| :--- | :--- | :--- |
| `--set-routing-mode` | `ask` | `silent_low_cost`（エコ）、`ask`（バランス）、`silent_best`（最高） |
| `--set-default-model` | `gpt-6.2-sol` | 標準モデルのスラッグ（`""` でリセット） |
| `--set-escalation-model` | `gpt-6.2-astra` | 複雑タスク／最高モード用の上位モデル（`""` でリセット） |
| `--set-quota-guardrail` | `on` | `on` / `off`（5h 低残量ガードレールの有効化／無効化） |
| `--set-quota-threshold` | `15` | ガードレール発動の 5h 残量しきい値（`5`–`50`%） |
| `--get-settings` / `--doctor` / `--prune-worktrees` | — | 設定取得、7 項目環境診断、待機中 worktree の一括削除 |

### 環境変数
- `CODEX_ORBIT_CODEX_BIN`：`codex` 実行ファイルの明示パス
- `CODEX_HOME`：Codex 設定ディレクトリの上書き（既定 `~/.codex`）
- `CODEX_ORBIT_DATA_DIR`：プラグインデータディレクトリの上書き

### よくある質問
- **`codex_cli` がエラーになる**：Codex CLI がインストール・ログイン済みか確認するか、`CODEX_ORBIT_CODEX_BIN` を指定してください。
- **クォータが rollout キャッシュ表示になる**：オフラインや未認証時は、`codex app-server` の代わりに最新のローカル rollout 内の `rate_limits` を参照します。
- **開発用リンクでの利用**：`.\scripts\dev-link.ps1`（Windows）または `./scripts/dev-link.sh`（macOS/Linux）を使用してください。詳細は [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md) を参照してください。

---

## 開発とテスト

UI ビルド（`ui/`）、開発用リンク（`dev-link.ps1` / `dev-link.sh`）、自動テスト（`pytest -v`）の詳細は [docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md) をご覧ください。

Antigravity ターミナルから MCP サーバーを再読み込みするには：
- **Windows**：`.\scripts\refresh-mcp.ps1`
- **macOS / Linux**：`./scripts/refresh-mcp.sh`

## ロードマップ

- PyPI で公開し、`uvx` や `pipx` でワンコマンドでセットアップできるようにする。
- ダッシュボードで Git worktree の差分を確認し、マージ・破棄・クリーンアップをワンクリックで行えるようにする。
- 委任タスクごとにタイムアウト、ステップ数と予算の上限を設定し、実行中に中止や指示変更を行えるようにする。
- Antigravity と Codex に加え、Claude Code や汎用 MCP クライアント向けの差し替え可能なエージェントアダプターを用意する。

## ライセンス

[MIT License](./LICENSE) のもとで公開されています。
