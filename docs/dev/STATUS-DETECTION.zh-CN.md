# 任务状态判定

面板把每个会话归到三态之一：**执行中（running）/ 已完成（done）/ 空闲（idle）**。
原则：**能读到显式边界事件就用事件，读不到才用超时兜底。**

## 各渠道信号

| 渠道 | 执行中 | 已完成 | 空闲 | 数据来源 |
|---|---|---|---|---|
| Codex | 最后一个 `task_started` 晚于 `task_complete` | 最后一个事件是 `task_complete` / `turn_aborted`；同会话新 `task_started` 会关闭仍 open 的旧回合（Codex 有时不写旧回合 complete） | 回合开着但**本回合**超时无写入（不用整文件 mtime，避免同会话后续写入把旧回合刷成执行中） | `~/.codex/sessions/**/*.jsonl` |
| Claude Code | 最后一条是 user 消息，或最后一条 assistant 的 `stop_reason=tool_use` | 最后一条 assistant 的 `stop_reason=end_turn`/`stop_sequence` | 无有效信号且超时 | `~/.claude/projects/**/*.jsonl` |
| DeepSeek Harness | 有 `turn/start` 无 `turn/end` | 最后一个事件是 `turn/end` | 回合开着但超时 | `~/.dsh/sessions/**/session.v4.jsonl.zstd` |
| Cursor | `unfinishedRunAt`；或转录回合未闭合；或 `CURSOR_RUN_STICKY_SECS` 内刚见过 unfinished；或 checkpoint 仍在 `CURSOR_DONE_QUIET_SECS` 内 | sticky 过期 + unfinished 空 + 转录已 `turn_ended`/无转录 + checkpoint 静默；`turn_ended=error` → **failed** | `hasBlockingPendingActions` 或超时无写入 | `Cursor/.../state.vscdb` / `~/.cursor/projects/**/agent-transcripts`；有工程使用 `agentpanel://cursor?id=&cwd=`，无工程使用 `cursor://anysphere.cursor-deeplink/agent?id=` |
| Qoder CN | 最后是 user，或 `stop_reason=tool_use`，或 `state.json` 标 running | 最后是 assistant / state 标完成 | 回合开着但超时 | `~/.qoder-cn/projects/**/*.jsonl`（退回 `QoderCN/.../local.db`） |
| Cue | `agentStatus=running` | `agentStatus=stopped` | `agentStatus=waiting` | `SessionStore/sessions.sqlite` |
| Today | 最后一条消息 role=user | role=assistant 且 `wireState=completed` | 其他 | `chat_*.sqlite` |
| Grok Bot | `lastActivityAt` 在兜底窗口内 | 超过窗口 | `awaitingUserResponse` 为真 | `sand-client-persistence/*.blob` |

Grok 的 blob 里没有 role/完成字段（实测 `awaitingUserResponse` 全为 `null`），
只能靠活动时间兜底，属于已知精度上限。

## 自适应僵尸超时

有「执行中」信号、但会话长时间没有新写入时，说明进程可能已经退出：

```
阈值 = clamp(p90(该会话历史事件间隔) × 4, 600s, 3600s)
```

- 只用于**降级**（running → idle），不参与正常完成判断
- 下限 600s 来自实测：Codex 回合内静默 p95=287s、p99=1032s，
  阈值取 300s 会把 4.8% 的正常长任务误判成僵尸
- 历史样本不足 3 个间隔时用 900s 兜底
- 降级成 idle **不会**触发「任务完成」通知（只有 running → done 才通知）

## 缓存与状态计算

状态依赖「现在几点」（僵尸降级会随时间推进），所以**缓存里不能存状态**：

- 缓存只存解析事实（标题、`turn_open`、事件时间戳等）
- 状态在每次读取时重算，**缓存命中也一样**

`scan_dsh` 曾经把算好的 `task`（含 `status`）整个塞进 `_file_cache`，mtime 未变就直接返回。
后果：回合开着时进程崩了，首次扫描看到较新的 mtime 判为 running，之后文件不再更新 →
永远命中缓存 → 僵尸降级再也不会执行，任务一直显示执行中，直到 24 小时窗口把它扫出去。

`scan_codex` / `scan_claude` / `scan_dsh` / `scan_cursor` / `scan_qoder_cn` 现在都是同一套写法：
`parse_*` 只返回事实，`scan_*()` 每次算状态。新增渠道请照此实现。

## 通知

服务端 `_prev_tasks` 按稳定 `task_id` 对比上一轮状态，`running → done/idle/failed` 先进入
`_pending_finish`，连续确认后才写入 `finished`；中途回到 `running` 则取消。
普通渠道用 `FINISH_CONFIRM_HITS`（默认 2）；Cursor 的 `completed` 用
`CURSOR_COMPLETED_CONFIRM_HITS`（默认 3）。状态侧还有
`CURSOR_RUN_STICKY_SECS` / `CURSOR_DONE_QUIET_SECS`（约 20s 量级）挡住短 Thinking /
工具间隙；转录 `boundary=open` 仍优先保持执行中。菜单栏约 2.5s 轮询一次，
整体完成到通知大约十几秒内，而不是近一分钟。

桌面 App：由菜单栏原生定时拉 `/api/tasks` 发系统通知；面板 WebView 走
`/api/tasks?view=1`（不消费 finished），避免收起后面板 JS 被节流导致漏通知，
也避免和原生轮询抢事件。浏览器模式仍由页面自己消费 `finished`。

长任务在持续 `running` 超过约 10 分钟后，同一接口还会下发 `heartbeats`，原生侧弹
摘要卡（样式区分于完成/堵塞/失败）。终态事件同时写入今日时间线
（`GET /api/timeline`）。

## 扫描器插件化

任务聚合走 `SCANNERS` 注册表（函数名字符串 → `globals()` 解析），`collect_all_tasks()`
按表调用；单渠道异常不影响其它。新增渠道：实现 `scan_*`，往 `SCANNERS` append 一行，
并在 `ALL_AGENTS` / 配置 UI 补上展示名。单测用 `mock.patch.object(server, "scan_…")`
即可拦截。

## OpenAI 桌面端（Codex.app）

新版 `Codex.app` 的主进程可执行文件仍叫 `ChatGPT`（bundle id `com.openai.codex`）。
面板**不得**把「进程在线」标成 ChatGPT/执行中；真实 Codex 任务只来自
`~/.codex/sessions/**/*.jsonl` 的 `scan_codex`。24 小时内无会话时看板不出现 Codex 行是正常的。

Codex 当前只读取会话转录，不读取 App Server 的实时等待标志。普通工具调用、计划文字和执行超时不等同于等待确认。

## Cursor 待确认

工具审批使用 `hasBlockingPendingActions`。等待点击 Build 的计划使用 `hasPendingPlan`；仅当没有明确的执行信号或未关闭的回合时显示「等待确认」。

其它无本地明细的 App 占位若仍展示，状态为 `idle` + 文案「应用在线」，徽章「在线」，
**不计入**执行中/堵塞角标。

## Qoder CN 权限审批

桌面版扫描器从 Qoder CN 的本地运行日志识别实际发出的工具审批，再按会话和工具调用 ID 核对结构化会话事件。仅当请求所属进程仍在运行、审批未解决且回合未结束时显示「等待确认」。普通工具调用或没有明确审批证据的超时仍不会被标为等待确认。

## 已知噪音与过滤

Claude Code 会产生两类非用户任务的会话，都在 `parse_claude_file` 里过滤：

1. 子 agent 转录：文件名 `agent-*.jsonl`，且 `isSidechain=true`
2. 工作流 harness：cwd 落在 `~/.claude/projects` 内，或首条用户消息以
   `[Workflow harness — ` 开头；这类文件实测一分钟能刷出十几条

另外标题退化成 `-` 或命令路径回显（`/…/bin/…`）的会话也丢弃。

## 验证方法

```bash
cd <repo>
python3 - <<'PY'
import sys, collections
sys.path.insert(0, 'src'); import server
server._cache['mtime'] = 0
t = server.tasks()['tasks']
print(len(t), dict(collections.Counter(x['status'] for x in t)))
print(dict(collections.Counter(x['agent'] for x in t)))
PY
```

改完状态逻辑至少要确认三件事：总数稳定（不随时间暴涨）、三态分布合理、
没有 `Workflow harness` / 标题为 `-` 的条目。
