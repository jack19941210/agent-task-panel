# 本地 API 契约 v1

该契约是 macOS 面板与未来 Windows/Linux 客户端共用的数据边界。核心字段在 v1 内保持向后兼容；
渠道可以附加更多字段，但客户端不能依赖未写入本契约的字段。

## `GET /api/tasks`

```json
{
  "api_version": 1,
  "pin": true,
  "tasks": [
    {
      "task_id": "d5f3c31f52b6a63c",
      "agent": "codex",
      "title": "修复发布校验",
      "cwd": "/path/to/project",
      "status": "running",
      "mtime": 1793257504.38,
      "link": "codex://threads/example",
      "recent": "正在运行 CI 检查",
      "recent_at": "2026-10-29T07:04:46.655Z"
    }
  ],
  "finished": [
    {
      "task_id": "d5f3c31f52b6a63c",
      "agent": "codex",
      "title": "修复发布校验",
      "kind": "completed",
      "recent": "CI 已通过",
      "cwd": "/Users/me/proj",
      "link": "codex://threads/example"
    }
  ],
  "heartbeats": [
    {
      "task_id": "d5f3c31f52b6a63c",
      "agent": "codex",
      "title": "修复发布校验",
      "kind": "heartbeat",
      "recent": "仍在跑测试",
      "cwd": "/Users/me/proj",
      "link": "codex://threads/example",
      "running_mins": 12
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `api_version` | integer | 契约版本，当前为 `1` |
| `pin` | boolean | 面板是否置顶 |
| `tasks` | array | 当前可见任务 |
| `finished` | array | 本轮从 `running` 变为终态的任务，每个转换只下发一次 |
| `finished[].kind` | enum | `completed`（完成）/ `blocked`（堵塞停滞）/ `failed`（失败）；可选，旧客户端可忽略 |
| `finished[].recent` | string | 最近进展摘要，供完成弹卡一眼看到「做成啥样了」 |
| `finished[].cwd` / `link` | string | 工程路径与跳转；可选 |
| `heartbeats` | array | 长任务仍在 `running` 时按间隔下发的心跳事件（默认约 10 分钟）；`view=1` 时恒为空 |
| `heartbeats[].kind` | string | 固定为 `heartbeat` |
| `heartbeats[].running_mins` | integer | 已连续执行约多少分钟 |
| `task_id` | string | 稳定 16 位标识。Codex 有有效会话 ID 时按会话派生，不随日志分段改变；否则优先使用扫描器的会话键 |
| `agent` | string | 渠道 id，与 `GET /api/config` 一致 |
| `title` | string | 主标题 |
| `full_title` | string | 可选，完整的用户任务描述；界面可用短标题展示、详情保留全文 |
| `user_prompt` | string | 可选，本回合用户原始输入。优先于 Agent 生成的英文标题用于识别任务 |
| `turn_key` | string | 可选，渠道内回合 ID；同一会话多次输入必须区分 |
| `cwd` | string | 工作目录；应用型 Agent 可为空 |
| `status` | enum | `running` / `done` / `idle` / `failed` |
| `mtime` | number | 最近活动 Unix 时间戳 |
| `link` | string | 点击任务时使用的 deep link；可为空 |
| `recent` | string | 副标题或最近一条进展（结论型压缩） |
| `recent_at` | string | 最近进展的 ISO8601 时间；未知时为空字符串 |
| `reply_text` | string | 可选，本轮可读取的助手回复原文，保留换行；不是整段会话，也不含推理或工具输出 |
| `reply_needed` | string | 可选，已结束回合里明确要求用户回复后才继续的文字；只影响面板「待回复」，不改变状态枚举或通知 |
| `handoff` | object | 可选交接摘要，含 `changes` / `verification` / `next_step`。来自明确的回复小节、待回复文本或渠道改动统计；不把缺失验证推断为通过 |

## `GET /api/stats`

返回 `{ "api_version": 1, "running": N }`。只刷新任务缓存、**不**推进 `finished` 状态机。菜单栏角标应轮询本接口，避免吞掉完成通知。

机器可读定义：[`docs/api/tasks-v1.schema.json`](../api/tasks-v1.schema.json)。

## `GET /api/timeline`

返回今日跨渠道事件时间线（完成 / 堵塞 / 失败），按时间倒序：

```json
{
  "api_version": 1,
  "items": [
    {
      "ts": 1793257504.38,
      "task_id": "d5f3c31f52b6a63c",
      "agent": "codex",
      "title": "修复发布校验",
      "kind": "completed",
      "recent": "CI 已通过",
      "cwd": "/Users/me/proj",
      "link": "codex://threads/example"
    }
  ]
}
```

本地落盘 `~/.agent-panel-timeline.json`，最多保留最近约 200 条。新完成事件在有数据时带 `reply_text` 和 `handoff`，用于查看当时的回复和交接摘要；只有短摘要的旧事件不补写新回复。会话分段合并不合并时间线中的回合事件。

## `GET /api/config`

返回 `api_version`、可选渠道清单 `agents`、扫描器注册表 `scanners` 和当前隐藏渠道 `hidden`。
`scanners` 为服务端 `SCANNERS` 列表中的渠道名（如 `codex`、`cursor`），新增扫描器只需往注册表 append。

## `GET /api/health`

返回 `api_version`、`platform`、`pid` 和构建指纹 `build`。`platform` 当前取值是
`macos`、`windows` 或 `linux`。

## 兼容规则

- 新增可选字段属于兼容变更。
- 删除字段、改名或修改状态枚举属于破坏性变更，必须提升 `api_version`。
- 同一 `task_id` 在一次运行内应保持稳定，用于完成通知和客户端选择状态。


## 体验字段与设置（兼容 v1）

保留 `status` 的四个旧枚举，新增可选 `display_status`：
`running / done / waiting / failed / idle / unknown / online`。
新客户端优先使用此字段：只有明确等待确认的信号才映射为 `waiting`；
执行信号超时为 `unknown`，不能当作堵塞或完成通知。`done` 表示当前回合结束，不保证用户目标达成。
`status_reason` 提供可读依据，`full_title` 保留扫描器提供的完整标题（旧数据源可能已截断）。

`finished[].revision` / `heartbeats[].revision` 用于稍后提醒时核对是否仍是同一份结果。
`notifications` 随 tasks/config 返回，默认 `completed/blocked/failed/summary/focus=true`、`heartbeat=false`。
显示渠道和通知偏好分开保存；隐藏渠道不通知。过滤通知不影响时间线留存。

`POST /api/config` 以补丁形式合并 `hidden`、`notifications`、`project_aliases`，验证后原子保存。
无效字段值返回 400，文件写入失败返回 500；不会用不完整的设置覆盖已有配置。
`project_aliases` 为原始工程路径到显示名的映射，显示名最多 80 字。

macOS 和 Windows 可通过 `workbuddy_data_dir` 覆盖 WorkBuddy 自动发现位置：传入包含 `workbuddy.db` 的本机绝对目录，空字符串清除覆盖设置。状态来自兼容的 `sessions` 表（过期的执行状态降为未知）；如果有匹配的 `projects/<workspace>/<id>.jsonl`，`recent` 取转录尾部最近一轮的助手可见回复摘要。`GET /api/diagnostics` 的 WorkBuddy 状态区分 `not_found`（未找到）、`permission_denied`（无读取权限）、`incompatible`（格式不兼容）、`unreadable`（其他读取失败）、`invalid_path`（路径无效）、`empty`（兼容但近 24 小时无记录）和 `ok`。

`GET /api/timeline?date=YYYY-MM-DD` 查询本地时区的某一天；不传日期为今天，非法日期返回 400。
仍仅保留最近 200 条记录，不保证任意历史日期都有完整数据。

`GET /api/diagnostics` 返回 `channels[]`：`id/name/supported/state/count/checked_at/error`。
`state` 为 `ok / empty / error / unchecked`；错误仅暴露异常类型，不返回会话内容。
未解析到数据不保证应用未安装；部分历史格式可能缺少可诊断的错误信号。

视图和折叠状态保存在客户端本地存储。面板不维护已读、未读或待处理状态；
点击卡片直接跳回原 Agent，展示状态仅取自任务数据。
