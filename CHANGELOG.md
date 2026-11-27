# Changelog

All notable changes to this project will be documented in this file.


## [Unreleased]

### Experience

- macOS / Windows 新增 WorkBuddy 渠道：只读自动发现本地数据库，支持自定义目录、状态诊断和近 24 小时会话元数据。
- WorkBuddy 识别数据库中的执行状态，并从匹配的本地转录提取最近一轮助手回复摘要。
- WorkBuddy 显示待回答问题及其摘要；WorkBuddy 和 Qoder CN 支持跳转到原会话。

- macOS App/DMG 内置 Cursor 桥接 VSIX，首次使用自动检查、安装或升级，无需用户单独下载扩展；安装失败明确反馈。

- macOS Cursor 增加本地扩展桥接，按工程和 composer ID 在 IDE 侧栏定位具体会话；等待执行回执，超时明确提示。

- Cursor 默认跳转到 IDE 工程，不再随后自动打开 Agents；详情提供独立 IDE/会话入口。已有显式跳转偏好保留。
- Cursor 无工程会话通过原生链接直达 Agents，包含时间线里的旧会话链接，无需桥接。

- 状态视图优先展示等待确认和失败，并提供「待处理」筛选；待处理按实际状态计算，无需手动标记。
- 执行中采用两行列表，回合结束默认折叠，仅在线的应用独立展示，不参与任务计数。
- 空状态直接显示渠道检测结果与操作提示；断连时保留最近任务并标记数据可能过期。
- 任务卡可展开查看状态依据、摘要和工程路径；支持复制路径和键盘操作。
- 保留 API v1 状态字段，新增展示状态；失败不计入完成，过期执行信号显示“状态未知”。
- 统一渠道、通知、外观和原生选项入口，新增渠道诊断、工程别名、历史日期查询。
- 普通完成默认保留提醒、摘要和窗口唤起；可独立关闭唤起，心跳提醒默认关闭。
- 同轮多个完成事件合并系统通知；macOS 稍后提醒会核对当前任务状态与结果版本。
- 修复连续失败提示和设置保存后的刷新；保留卡片节点与展开状态，避免轮询打断阅读。
- Windows/Linux 新增独立完成通知轮询，关闭面板后保留托盘监听。

### Added
- Cursor and Qoder CN coding-agent channels on all platforms. Cursor reads
  `composerHeaders` from `state.vscdb` (`unfinishedRunAt` → running) and falls back to
  `~/.cursor/projects/**/agent-transcripts`. Qoder CN scans
  `~/.qoder-cn/projects/**/*.jsonl` (honoring `QODERCN_CONFIG_DIR`) and falls back to the
  SharedClientCache `local.db` layouts used by the standalone app.
- `/api/stats` for menu-bar running counts without consuming `finished` events.
- System notifications for `running → done/idle/failed` (`kind`: completed / blocked / failed),
  including when the panel is in the foreground.
- Cursor task cards deep-link to `cursor://anysphere.cursor-deeplink/agent?id=<composerId>`.
- Unified conclusion-style `recent` summaries across Codex / Claude / Cursor / Qoder CN.
- Configurable Cursor jump mode (IDE / Agents / IDE then Agents) in the menu-bar settings.
- Finish banner chrome by kind (completed / blocked / failed / heartbeat), plus「稍后提醒」.
- Long-running `heartbeats` on `/api/tasks` and today’s cross-channel `/api/timeline`.
- Panel: group tasks by project `cwd`, density toggle (compact/full), timeline view.
- Scanner registry (`SCANNERS` + `collect_all_tasks`); `/api/config` exposes `scanners`.

### Fixed
- Menu bar polling no longer calls `/api/tasks` and silently drops completion notifications.
- Default panel UX: opaque background, interactive (no hover fade), top-right placement.

## [0.4.1] - 2026-11-07

### Fixed
- CI no longer fails before running a single step on macOS. All workflows moved from the retired
  `macos-14` arm64 image to `macos-15`, and `tests/test_release_metadata.py` now rejects retired
  macOS runner labels so the next retirement fails loudly in the test suite instead of silently
  blocking merges.
- DeepSeek Harness sessions no longer stay stuck on `running` after the process dies. `scan_dsh`
  cached the finished task dict, status included, and returned it verbatim on every cache hit, so
  the zombie demotion to `idle` never ran again once the file stopped being written. The cache now
  stores only parsed facts (`title`, `recent`, `turn_open`, turn timestamps) and the status is
  recomputed on each read, matching `scan_codex` and `scan_claude`.

## [0.4.0] - 2026-10-29

### Added
- Public project website with a product overview, agent support matrix, installation guide,
  and an interactive clear-mode preview.
- Versioned local API contract for future Windows/Linux clients, including a stable `task_id`,
  machine-readable JSON Schema, and contract tests.
- Platform runtime abstraction for macOS, Windows, and Linux plus cross-platform backend CI.
- Windows x64 and Linux x64 desktop builds that reuse the shared task API and panel while the
  macOS app keeps its native Swift window layer.
- One release tag now produces macOS, Windows and Linux installers plus a combined
  `SHA256SUMS` file.
- Search discovery files and Cloudflare Pages security/cache headers for the public website.
- Automatic Cloudflare Pages deployment from GitHub Actions whenever website files under `docs/`
  land on `main`, with a Pages-only API token stored as a repository secret.
- CodeQL analysis for Swift, Python, and GitHub Actions workflows on pull requests, `main`,
  and a weekly schedule.
- Release-package verification for SHA256, DMG contents, code signature, universal architectures,
  minimum macOS version, packaged Windows/Linux backends, and uploaded GitHub Release assets.

### Changed
- Only macOS has been tested in real use. Windows and Linux are CI-built preview packages
  with packaging smoke tests, but no real-device validation yet.
- Cloudflare Pages deployment uses `cloudflare/wrangler-action@v4`.
- Dependabot vulnerability alerts and automated security fixes are enabled.
- On Windows and Linux the backend now exposes local-session channels only; macOS-native app
  detection remains isolated behind the platform capability flag.
- Windows and Linux packages bundle the Python backend with PyInstaller, so users do not need a
  separate Python installation.

### Fixed
- Tasks with identical agent and title no longer overwrite each other's completion-notification
  state; transitions now use a stable `task_id`.

## [0.3.3] - 2026-10-28

### Added
- `/api/health` reports the server's build fingerprint. On launch the app compares it with its own
  bundle and replaces a stale server process, so reinstalling never leaves the panel showing data
  produced by the previous version.

### Changed
- Task state is now derived from each channel's own turn boundaries instead of a fixed 120-second
  file-mtime window:
  Codex `task_started`/`task_complete`, Claude Code `stop_reason`, DeepSeek `turn/start`→`turn/end`,
  Cue `agentStatus`, Today `wireState` + last message role.
- Turns that stay open with no progress fall back to an adaptive timeout
  (`p90 gap × 4`, clamped to 600–3600 s) instead of a hard-coded value. Measured intra-turn silence
  reaches p95 287 s / p99 1032 s, so the old 120 s window misreported long builds as finished.
- Release builds are Universal (arm64 + x86_64) and declare macOS 12.0 as the minimum.

### Fixed
- Codex sessions whose last events were `token_count` after `task_complete` were reported as
  unfinished; the state is now decided by event order, not timestamp comparison.
- Cue sessions in `waiting` state were reported as completed and produced false "task finished"
  notifications.
- Claude Code sub-agent transcripts (`agent-*.jsonl`, `isSidechain`) and workflow-harness meta
  sessions (cwd inside `~/.claude/projects`, `[Workflow harness …]` prompts) flooded the list as
  duplicate tasks. Both are filtered now.

## [0.3.2] - 2026-10-28

### Added
- `/api/tasks` now returns a `finished` array: the server detects running→done transitions
  (including while the panel window is closed) and delivers each one exactly once.

### Changed
- Finish notifications are triggered server-side instead of in the front end. WKWebView throttles
  background JS timers, so the old client-side diff silently dropped notifications whenever the
  panel window was hidden.

### Fixed
- DeepSeek session scanning now caches parsed snapshots keyed by file mtime (same as Codex and
  Claude Code) instead of decompressing every zstd file on every 3 s poll.
- Clicking a Today card now routes to the exact app when both builds are installed: the link
  carries `?bundle=<bundle-id>` and the Swift shell opens it with
  `NSWorkspace.open(_:withApplicationAt:)` instead of letting macOS guess among apps registered
  for the `today://` scheme.

## [0.3.1] - 2026-10-28

### Added
- Per-agent accent colors on task dots now cover every channel, including Codex (teal) and
  Claude Code (brand orange). The running-state blink is driven by the status class, so it
  survives per-channel overrides, and unknown agents fall back to gray instead of invisible.
- DeepSeek Harness support: sessions are read from `~/.dsh/sessions` (zstd-compressed v4 JSONL).
  The panel shows the LLM session title plus the first real user message; a session written to
  within 120 s counts as running.
- Today channel splitting: when both `Today.app` and `Today 2.app` (the CN build) are running the
  panel shows **Today Intl** and **Today CN** with distinct colors; with only one installed the
  channel is simply **Today**.

### Changed
- Noi was removed from the channel list — it is a browser shell, not an agent, and only produced
  a meaningless "app online" row. Orphan Dayflow / Mole CSS rules (never real channels) were
  dropped from the front end.

### Fixed
- Dayflow / Mole display-name mappings that had leaked into the front end were removed along with
  their orphan styles.

## [0.3.0] - 2026-10-27

### Added
- Click-through mode: the panel no longer swallows clicks when you are working in another app.
  Three-state pointer policy (`passthrough` / `armed` / `active`): passes clicks through by
  default, fades to 12% while the pointer crosses it (still passing through), and only becomes
  interactive when explicitly summoned.
- Global hotkey `⌥⌘A` to make the panel interactive / return it to click-through. Registered via
  Carbon `RegisterEventHotKey`, so it needs no Accessibility permission.
- Menu bar items: "Make panel interactive (⌥⌘A)" and "Auto yield (stay out of the way)". The menu
  bar icon switches between filled (interactive) and dimmed (click-through).
- `.stationary` collection behavior so the panel no longer slides with Space switches.

### Fixed
- **Settings were never persisted.** `UserDefaults(suiteName:)` was passed the app's own bundle
  identifier, which macOS ignores ("Using your own bundle identifier as an NSUserDefaults suite
  name does not make sense and will not work"). This silently broke *Launch at login* too. Now
  uses `UserDefaults.standard`.
- Timers driving the pointer state machine never fired while the pointer hovered the panel:
  `Timer.scheduledTimer` runs in the default run loop mode, which is suspended in event-tracking
  mode. Timers are now added to `.common`.
- Panel could get stuck at ~0.3 alpha ("visible but unclickable") when two `NSAnimationContext`
  animations overlapped. Alpha is now assigned directly instead of animated.
- Occasional (~1 in 20) failure when summoning the panel with the hotkey: window properties were
  written before the state changed, letting a concurrent mouse event revert them. State is now
  switched first, and all property writes go through `applyActive()` / `applyPassthrough()`.


## [0.2.3] - 2026-10-26

### Added
- Standard macOS app menu bar (About / Settings ⌘, / Refresh data ⌘R / Hide / Quit; Window and Help menus with ⌘M show panel)

### Fixed
- Repo restructure follow-up: panel.html is a bundled asset and now lives in `resources/`
- Removed the "Other AI apps" section from the READMEs (Noi is not an agent)
## [0.2.2] - 2026-10-26

### Added
- Window position memory: the panel reopens exactly where you left it (standard NSWindow frame autosave)

### Changed
- App icon: custom macOS-style squircle icon (task cards + status dots) replacing the placeholder

## [0.2.1] - 2026-10-26

### Fixed
- Data service auto-restart: a watchdog health-checks the local API every 5 s and relaunches server.py if it dies; previously a crashed server left the panel stuck on "service not responding" until manual restart
- Error banner now shows the correct command (`python3 server.py` from the repo root)

### Changed
- Menu bar icon replaced with a monochrome SF Symbol template image that adapts to light/dark menu bars; running-task count stays as text next to it

## [0.2.0] - 2026-10-25

### Added
- Menu bar item (🤖) showing live running-task count, polling the local API every 10 s
- Menu bar menu: show panel / pin toggle / launch-at-login toggle / refresh data / quit
- Closing the window now hides the panel instead of quitting while the menu bar item is active; reopen it from the menu bar

## [0.1.0] - 2026-10-25

First working release, daily-driver quality.

### Added
- Floating panel (Swift NSPanel) aggregating AI agent task status from local session files
- Real session detail scanning:
  - Codex (`~/.codex/sessions` JSONL): status from task_complete marker + file activity, recent progress as subtitle
  - Claude Code (`~/.claude/projects` JSONL): last user/assistant timestamps, recent reply as subtitle
  - Cue (`SessionStore/sessions.sqlite`): custom binary format reverse-engineered — agentStatus / brief / live subtask text
  - Grok Bot (`sand-client-persistence/*.blob`): session list with titles and last message
- Online-only agents (Muse, ChatGPT, Today, Noi): process detection with honest "app online, no local detail" subtitle
- Traffic-light status dots (green=running blink, gray=done, yellow=idle) with per-agent accent colors
- Three-state stat filters (running / done / idle), click again to clear
- Pin-to-top toggle (global floating level, verified via CGWindowList)
- Background modes: frost → clear → solid, persisted in localStorage
- Transparent mode: system traffic lights and all buttons auto-hide; hover re-shows (native NSTrackingArea + WKWebView message channel)
- Per-channel visibility config (`~/.agent-panel-config.json`) covering all 8 agents, including ones with no current tasks
- Click-through to agent via URL schemes (codex://, cue://, sand://, hatch://, today://, noi://), custom schemes intercepted in NavDelegate so the panel WebView never navigates away
- System notification when a task transitions running → done
- Launch-at-login toggle (LaunchAgent) and data-service refresh from Dock menu

### Fixed
- Pin watcher deallocated (weak self) so pinning silently never worked
- Drag strip hitTest swallowed clicks on stat cards below the titlebar
- Same-frame `location.href` deep links crashed the panel WebView
- Duplicate placeholder entries when a detail-scanned channel also matched process detection
- Server 500 after removing legacy Cue parsing (leftover reference)
- Inline opacity on pin/background buttons defeating hover-reveal CSS in transparent mode

### Known limitations
- Muse / ChatGPT / Today / Noi run tasks server-side; no local task detail available
- Session titles derive from the first user message; injected context (AGENTS.md etc.) is filtered heuristically
