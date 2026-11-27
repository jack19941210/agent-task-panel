#!/usr/bin/env python3
# 本地任务聚合服务：扫描 Codex / Claude Code 会话，输出 JSON
import json, os, glob, time, re, struct, sqlite3, subprocess, datetime, hashlib
import csv, ntpath, posixpath, stat
import sys
from contextlib import closing
from functools import lru_cache
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qs
from platforms import detect_runtime

RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(RESOURCE_DIR, "agent-task-dashboard.html")

RUNTIME = detect_runtime()
HOME = RUNTIME.home
CODEX_DIR = RUNTIME.codex_dir
CLAUDE_DIR = RUNTIME.claude_dir
DSH_DIR = RUNTIME.dsh_dir
CURSOR_STATE_DB = RUNTIME.cursor_state_db
CURSOR_PROJECTS_DIR = RUNTIME.cursor_projects_dir
QODER_CN_PROJECTS_DIR = RUNTIME.qoder_cn_projects_dir
QODER_CN_DB_PATHS = RUNTIME.qoder_cn_db_paths
PIN_FLAG = RUNTIME.pin_flag
CONFIG_PATH = RUNTIME.config_path
PID_PATH = RUNTIME.pid_path
API_VERSION = 1
TASK_STATUSES = {"running", "done", "idle", "failed"}
PORT = int(os.environ.get("AGENT_PANEL_PORT", "8765"))

# 构建标识：App 启动时用同一个算法算自己 bundle 里 server.py 的指纹并传进来。
# 端口上若跑着指纹不同的旧进程（重装后常见），App 会杀掉它再拉起新服务，
# 否则面板会静默显示旧代码产出的数据。
def build_id(path):
    h = 0xcbf29ce484222325
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                for b in chunk:
                    h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    except OSError:
        return "0"
    return "%016x" % h

BUILD_ID = "0"
SCAN_AGE = 24 * 3600     # 只看 24 小时内的会话

# 僵尸判定：有"执行中"信号但会话已无进展 → 进程已崩/已退出，降级 idle。
# 阈值按会话自身的事件间隔自适应（p90 × 4），并用上下限夹住。
# 实测回合内静默 p95=287s / p99=1032s，所以下限取 600s，避免把长构建误判成僵尸。
ZOMBIE_MIN = 600
ZOMBIE_MAX = 3600
ZOMBIE_FACTOR = 4
ZOMBIE_DEFAULT = 900     # 历史样本不足时的兜底

_cache = {"data": [], "mtime": 0.0}   # 短缓存，避免轮询间隔重复扫描
CACHE_TTL_SECS = 1.5
# 完成通知：服务端对比 running→done，避免面板关窗时 WKWebView 节流漏发
# 完成通知按稳定 task_id 对比，避免同一 Agent 下重复标题互相覆盖。
_prev_tasks = {}
# 终态确认：Cursor 等渠道在回合间隙会短暂丢掉「执行中」信号，直接发通知会误报。
# 连续 N 次扫描仍为终态才下发 finished；中途回到 running 则取消。
FINISH_CONFIRM_HITS = 2
# Cursor 完成：多确认几轮防 Thinking 误报，但不宜太高（×轮询间隔会明显拖通知）
CURSOR_COMPLETED_CONFIRM_HITS = 3
_pending_finish = {}  # task_id -> {kind, agent, title, hits}
# Cursor：unfinished 清空后还要等 checkpoint 静默（覆盖短工具间隙）
# 过长会让面板长期停在「执行中」；转录 boundary=open 仍能挡住真间隙
CURSOR_DONE_QUIET_SECS = 18
# 见过 unfinished 后短暂保持 running，覆盖短 Thinking；过长则真完成也像在跑
CURSOR_RUN_STICKY_SECS = 22
_cursor_unfinished_seen = {}  # composerId -> 最近一次见到 unfinishedRunAt 的 epoch
# hasPendingPlan 残留：记下首次见到时的回合数；若之后又开了新回合或跑过 unfinished，不再当「等 Build」
_cursor_pending_plan_at = {}  # composerId -> 首次见到 pending plan 时的转录回合数
_cursor_pending_plan_ran = set()  # 在 pending plan 期间见过 unfinished 的 composerId
# 长任务心跳：持续 running 超过该秒数后，每 HEARTBEAT_SECS 提醒一次
HEARTBEAT_SECS = 600
_running_since = {}   # task_id -> 首次见到 running 的 epoch
_heartbeat_sent = {}  # task_id -> 上次心跳 epoch
# 跨渠道时间线（本地落盘，仅保留近期）
TIMELINE_PATH = os.path.join(HOME, ".agent-panel-timeline.json")
TIMELINE_MAX = 200
_timeline = None  # lazy load
_diagnostics = {}
_scan_errors = {}


def record_scan_error(agent, exc):
    _scan_errors[agent] = type(exc).__name__


def channel_diagnostics():
    channels = []
    supported = {agent["id"] for agent in all_agents(RUNTIME)}
    for a in BASE_AGENTS + [WORKBUDDY_AGENT] + NATIVE_APP_AGENTS:
        scanner = "apps" if a in NATIVE_APP_AGENTS else a["id"]
        info = dict(_diagnostics.get(scanner, {"state": "unchecked"}))
        if scanner == "apps" and info["state"] != "unchecked":
            count = sum(t["agent"] == a["id"] for t in _cache["data"])
            info.update(state="ok" if count else "empty", count=count)
        error = _scan_errors.get(a["id"]) or _scan_errors.get(scanner)
        if error:
            info.update(state="error", error=error)
        channels.append({**a, **info, "supported": a["id"] in supported})
    return channels

NOTIFICATION_DEFAULTS = {
    "completed": True, "blocked": True, "failed": True,
    "heartbeat": False, "summary": True, "focus": True,
}
DISPLAY_STATUSES = {"running", "done", "waiting", "failed", "idle", "unknown", "online"}


def save_config(patch):
    """Validate and merge settings; an invalid patch never truncates existing settings."""
    if not isinstance(patch, dict):
        raise ValueError("配置格式无效")
    cfg = load_config()
    if "hidden" in patch:
        hidden = patch["hidden"]
        if not isinstance(hidden, list) or any(not isinstance(v, str) for v in hidden):
            raise ValueError("渠道列表无效")
        cfg["hidden"] = sorted(set(hidden))
    if "notifications" in patch:
        prefs = patch["notifications"]
        if not isinstance(prefs, dict) or any(k not in NOTIFICATION_DEFAULTS or type(v) is not bool for k, v in prefs.items()):
            raise ValueError("通知设置无效")
        cfg["notifications"] = {**NOTIFICATION_DEFAULTS, **cfg.get("notifications", {}), **prefs}
    if "project_aliases" in patch:
        aliases = patch["project_aliases"]
        if not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 80 for k, v in aliases.items()):
            raise ValueError("工程别名无效（最多 80 字）")
        cfg["project_aliases"] = aliases
    if "workbuddy_data_dir" in patch:
        directory = normalize_workbuddy_dir(patch["workbuddy_data_dir"])
        if directory:
            cfg["workbuddy_data_dir"] = directory
        else:
            cfg.pop("workbuddy_data_dir", None)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    _cache["mtime"] = 0
    return cfg


def notification_preferences():
    return {**NOTIFICATION_DEFAULTS, **load_config().get("notifications", {})}


def set_execution_status(task, mtime, threshold, now=None):
    task["status"] = demote_if_zombie("running", mtime, threshold, now=now)
    if task["status"] == "idle":
        task["display_status"] = "unknown"
        task["status_reason"] = "执行信号已过期，暂时无法确认是否仍在运行；请打开原会话检查。"


# 增量缓存：文件路径 -> (mtime, 解析结果)；未变化的文件直接复用
_file_cache = {"codex": {}, "claude": {}, "dsh": {}, "cursor": {}, "qoder-cn": {}}

def first_text(text):
    return re.sub(r"\s+", " ", str(text)).strip()[:60]


def compress_text(text, limit=180):
    """结论摘要用：压空白，保留更长可读句，不被标题 60 字限制卡住。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def format_conclusion(text, stats=""):
    """全渠道统一结论型摘要：取首段可读句 + 可选改动统计。"""
    para = ""
    for line in str(text or "").splitlines():
        raw = line.strip()
        # 跳过空行、代码围栏、纯 Markdown 标题（# / ## …）
        if not raw or raw.startswith("```") or raw.startswith("#"):
            if para:
                break
            continue
        para = raw if not para else para + " " + raw
        if len(para) >= 160:
            break
    summary = compress_text(para or text, 180)
    stats = str(stats or "").strip()
    if summary and stats:
        return f"{summary}（{stats}）"
    return summary or stats


def assistant_text(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, dict)
                     and block.get("type") in (None, "text", "output_text")
                     and isinstance(block.get("text"), str)).strip()


def user_prompt_text(text):
    """提取用户真正输入，去掉 Cursor/Codex 注入的展示上下文。"""
    value = str(text or "").strip()
    if "<user_query>" in value:
        value = value.split("<user_query>", 1)[1].split("</user_query>", 1)[0]
    value = re.sub(r"<uploaded_documents>.*?</uploaded_documents>", "", value, flags=re.S)
    value = re.sub(r"<timestamp>.*?</timestamp>", "", value, flags=re.S)
    value = re.sub(r"<[^>]+>", "", value)
    if "## My request:" in value:
        value = value.split("## My request:", 1)[1]
    if value.lstrip().startswith(("# Files mentioned", "<INSTRUCTIONS>", "<environment_context>")):
        return ""
    return value.strip()


def visible_reply_lines(text):
    fence = ""
    for line in text.splitlines():
        line = line.strip()
        marker = line[:3]
        if fence:
            if line.startswith(fence):
                fence = ""
        elif marker in ("```", "~~~"):
            fence = marker
        elif not line.startswith(">"):
            yield line


def reply_request(text):
    lines = [line.replace("**", "").replace("`", "") for line in visible_reply_lines(text) if line]
    for line in reversed(lines[-8:]):
        if re.search(r"如果(?:你)?(?:需要|愿意|想)|如需|\bif (?:needed|you (?:want|would like))\b", line, re.I):
            continue
        if re.search(r"(?:等你|收到你(?:的)?|你(?:先|来)?|请你|请)(?:回复|确认|选择|回答)[^。！？；\n]{0,70}(?:之后|以后|后|再)[，,\s]*(?:我(?:就|再|才|会)?\s*)?(?:继续|开始|执行|进入|进行|改完|再做)", line):
            return compress_text(line)
        if re.search(r"\b(?:once|after) you (?:reply|confirm|choose|answer)\b[^.!?]{0,80}\b(?:I(?:['’]ll| will) (?:continue|proceed|start)|we can (?:continue|proceed))\b", line, re.I):
            return compress_text(line)
    return ""


HANDOFF_LABELS = {
    "changes": r"改动(?:内容|说明)?|(?:已(?:直接)?)修改|修改(?:内容|说明)|已完成的改动|changes(?: made)?|what changed",
    "verification": r"(?:测试与验证|测试|验证)(?:结果|情况)?|verification|tests?(?: run| results)?|validation",
    "next_step": r"下一步(?:计划)?|待办|待处理|剩余(?:工作|事项)|next steps?|remaining(?: work)?",
}
HANDOFF_HEADINGS = {key: re.compile(rf"^(?:{labels})(?:\s*[:：]\s*(.*)|\s*)$", re.I)
                    for key, labels in HANDOFF_LABELS.items()}


def handoff_summary(reply, request):
    sections = {}
    active = None
    for line in visible_reply_lines(reply):
        if not line:
            if sections.get(active):
                active = None
            continue
        content = re.sub(r"^(?:#{1,6}|[-*+] |\d+[.)] )\s*", "", line).replace("**", "").strip()
        for key, pattern in HANDOFF_HEADINGS.items():
            match = pattern.fullmatch(content)
            if match:
                active = key
                if match.group(1):
                    sections.setdefault(key, []).append(match.group(1))
                break
        else:
            if (line.startswith("#") or (line.startswith("**") and line.endswith("**"))
                    or re.search(r"^(?:如果(?:你)?(?:需要|愿意|想)|如需|if (?:needed|you (?:want|would like)))", content, re.I)):
                active = None
            elif active:
                sections.setdefault(active, []).append(content)
    if request:
        sections.setdefault("next_step", []).append(request)
    result = {}
    for key, parts in sections.items():
        text = " · ".join(dict.fromkeys(part for part in parts if part))
        if text:
            result[key] = text if len(text) <= 360 else text[:359].rstrip() + "…"
    return result


def task_id_for(*parts):
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def normalize_task(task):
    """把各渠道的扫描结果收敛成跨平台客户端可依赖的任务契约。"""
    agent = str(task.get("agent") or "").strip()
    if not agent:
        return None
    title = first_text(task.get("title") or "") or "未命名任务"
    cwd = str(task.get("cwd") or "")
    link = str(task.get("link") or "")
    status = str(task.get("status") or "idle")
    if status not in TASK_STATUSES:
        status = "idle"
    try:
        mtime = float(task.get("mtime") or 0)
    except (TypeError, ValueError):
        mtime = 0.0
    # session_key（如 Cursor composerId）优先：避免 cwd/标题抖动导致 task_id 换号、漏掉完成通知
    session_key = str(task.get("session_key") or "").strip()
    if session_key:
        identity = (agent, session_key)
    else:
        identity = (agent, link, cwd, title)
    normalized = {
        **task,
        "task_id": task_id_for(*identity),
        "agent": agent,
        "title": title,
        "full_title": str(task.get("full_title") or task.get("user_prompt") or task.get("title") or title),
        "cwd": cwd,
        "status": status,
        "mtime": mtime,
        "link": link,
        "recent": str(task.get("recent") or ""),
        "recent_at": str(task.get("recent_at") or ""),
    }
    if task.get("user_prompt"):
        normalized["user_prompt"] = str(task["user_prompt"])
    display = task.get("display_status", status)
    if str(task.get("recent") or "").startswith("应用在线"):
        display = "online"
    if display not in DISPLAY_STATUSES:
        display = "unknown"
    normalized["display_status"] = display
    normalized["status_reason"] = str(task.get("status_reason") or {
        "running": "检测到执行信号，最近活动时间见下方。",
        "done": "当前回合已结束，请查看结果确认是否达到目标。",
        "waiting": "检测到需要用户确认的操作。",
        "failed": "检测到执行失败，请打开原会话检查。",
        "idle": "当前没有明确的执行信号。",
        "unknown": "现有数据不足以确认任务状态。",
        "online": "仅检测到应用在线，无法读取任务进度。",
    }[display])
    if display == "done":
        reply = normalized.get("reply_text") or ""
        # awaiting_user=False：同会话已被后续回合接上的历史卡片，不再占「待回复」
        request = reply_request(reply) if task.get("awaiting_user", True) else ""
        if request:
            normalized["reply_needed"] = request
        handoff = {**task.get("handoff", {}), **handoff_summary(reply, request)}
        if handoff:
            normalized["handoff"] = handoff
    return normalized


def parse_ts(value):
    """ISO8601 → epoch 秒；无法解析返回 None。"""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def zombie_threshold(timestamps):
    """按会话自身的事件间隔算僵尸阈值：p90 * 4，夹在 600~3600 秒之间。

    自适应只用于识别"有执行中信号但已无写入"的僵尸进程，
    不参与正常完成判断——完成一律由各渠道的显式边界事件决定。
    """
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:]) if 0 < b - a < 3600]
    if len(gaps) < 3:
        return ZOMBIE_DEFAULT
    gaps.sort()
    p90 = gaps[int(len(gaps) * 0.9)]
    return max(ZOMBIE_MIN, min(ZOMBIE_MAX, p90 * ZOMBIE_FACTOR))


def demote_if_zombie(status, mtime, threshold, now=None):
    """有执行中信号但长时间无写入 → idle（进程大概率已退出）。"""
    if status != "running":
        return status
    now = time.time() if now is None else now
    return "idle" if now - mtime > threshold else status


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clean_title(text):
    # 跳过注入的 AGENTS.md / system-reminder 等非用户输入
    text = str(text)
    s = text.lstrip()
    if s.startswith(("# AGENTS.md", "<INSTRUCTIONS>", "<system-reminder>", "<environment_context>", "<external_", "# Files mentioned")):
        return None
    # markdown 前缀 / 系统角色文本 / 报错日志开头的"消息"不是合理任务名
    if s.startswith(("## ", "# ", "**", "You are ", "<", "{", "```")):
        return None
    if s.startswith("<in-app-browser-context") or "source=\"ambient-ui-state\"" in s[:200]:
        # 环境状态块后面跟着真实请求，取正文末段
        tail = re.split(r"</in-app-browser-context>", s)[-1].strip()
        if tail and not tail.startswith("<"):
            return first_text(tail)
        return None
    return re.sub(r"\s+", " ", text).strip()[:2000]

# ============ App 型 agent ============
# 进程名 -> (显示名, deep link, 是否有本地明细)
# Today 两个版本：进程名 Today 2 = 国内版；两个同时在跑才区分 id，单独在跑统称 today
APP_AGENTS = {
    "ChatGPT": ("ChatGPT", "codex://", False),
    "Grok Bot": ("Grok Bot", "sand://", False),
    "Muse": ("Muse", "hatch://", False),
    "Cue": ("Cue", "cue://", True),
    "Today": ("Today", "today://", True),
    "Today 2": ("Today 国内", "today://", False),
}

# 本地会话型渠道在所有平台可用。
BASE_AGENTS = [
    {"id": "codex", "name": "Codex"},
    {"id": "claude", "name": "Claude Code"},
    {"id": "deepseek", "name": "DeepSeek Harness"},
    {"id": "cursor", "name": "Cursor"},
    {"id": "qoder-cn", "name": "Qoder CN"},
]

WORKBUDDY_AGENT = {"id": "workbuddy", "name": "WorkBuddy"}

# 这些条目依赖 macOS 应用目录和进程名。
NATIVE_APP_AGENTS = [
    {"id": "chatgpt", "name": "ChatGPT"},
    {"id": "grok-bot", "name": "Grok Bot"},
    {"id": "muse", "name": "Muse"},
    {"id": "cue", "name": "Cue"},
    {"id": "today", "name": "Today（单版本时）"},
    {"id": "today-intl", "name": "Today Intl"},
    {"id": "today-cn", "name": "Today CN"},
]

def all_agents(runtime=RUNTIME):
    agents = list(BASE_AGENTS)
    if runtime.workbuddy_db_paths:
        agents.append(WORKBUDDY_AGENT)
    if runtime.supports_native_apps:
        agents.extend(NATIVE_APP_AGENTS)
    return agents


# 全渠道清单（配置面板用）：即使当前无任务也应可配置
ALL_AGENTS = all_agents()

def proc_alive(name):
    if not RUNTIME.supports_native_apps:
        return False
    r = subprocess.run(["pgrep", "-x", name], capture_output=True)
    if r.returncode == 0:
        return True
    r = subprocess.run(["pgrep", "-f", f"Applications/{name}.app"], capture_output=True)
    return r.returncode == 0


def proc_executable_paths(name):
    """返回进程可执行路径列表（用于区分同名二进制所属 App）。"""
    if not RUNTIME.supports_native_apps:
        return []
    r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    paths = []
    for pid in r.stdout.split():
        info = subprocess.run(
            ["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True)
        path = (info.stdout or "").strip()
        if path:
            paths.append(path)
    return paths


def resolve_openai_desktop_agent():
    """ChatGPT / Codex 桌面端：Codex.app 的主进程仍叫 ChatGPT。

    按可执行路径判断：落在 Codex.app 内 → 记为 codex；否则 chatgpt。
    """
    paths = proc_executable_paths("ChatGPT")
    if any("/Codex.app/" in p for p in paths):
        return "codex", "Codex", "codex://"
    if any("/ChatGPT.app/" in p for p in paths):
        return "chatgpt", "ChatGPT", "codex://"
    # pgrep -x 未命中时，再按 bundle 路径兜底
    if proc_alive("Codex") or subprocess.run(
            ["pgrep", "-f", "Applications/Codex.app/"], capture_output=True).returncode == 0:
        return "codex", "Codex", "codex://"
    if paths or proc_alive("ChatGPT"):
        return "chatgpt", "ChatGPT", "codex://"
    return None


def _cue_str(blob, key):
    # Cue 自定义二进制：'c'+len(字节) 后跟 UTF-16LE 文本；'"'+len 后跟 ASCII
    i = blob.find(key)
    if i < 0:
        return None
    j = i + len(key)
    if blob[j:j+1] == b"\x00":
        j += 1
    try:
        if blob[j:j+1] == b"c":
            n = blob[j+1]
            return blob[j+2:j+2+n].decode("utf-16-le", errors="ignore").strip() or None
        if blob[j:j+1] == b'"':
            n = blob[j+1]
            return blob[j+2:j+2+n].decode("utf-8", errors="ignore").strip() or None
    except Exception:
        return None
    return None


def scan_cue():
    # cache_events: statusUpdate(agentStatus/brief/description) + subtaskLiveStatus(text)
    db = os.path.expanduser("~/Library/Application Support/Cue/SessionStore/sessions.sqlite")
    sessions = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for sid, ts, rec in con.execute(
                "SELECT session, timestamp, record FROM cache_events ORDER BY timestamp"):
            st = sessions.setdefault(sid, {"mtime": 0})
            st["mtime"] = max(st["mtime"], ts / 1000)
            if rec.find(b"statusUpdate") > 0:
                s = _cue_str(rec, b"agentStatus")
                if s:
                    st["status"] = s
                for k, key in (("brief", b"brief"), ("desc", b"description")):
                    v = _cue_str(rec, key)
                    if v:
                        st[k] = v
            if rec.find(b"subtaskLiveStatus") > 0:
                t = _cue_str(rec, b"text")
                if t:
                    st["live"] = t
        for meta, in con.execute("SELECT metadata FROM cache_sessions"):
            m = re.search(rb"[A-Za-z0-9]{21}", meta[:60])
            t = _cue_str(meta, b"title")
            if m and t:
                sid = m.group(0).decode()
                if sid in sessions:
                    sessions[sid]["title"] = t
        con.close()
    except Exception:
        return []
    out = []
    now = time.time()
    for sid, st in sessions.items():
        if now - st["mtime"] > SCAN_AGE or not (st.get("title") or st.get("brief")):
            continue
        # Cue 自带 agentStatus 三态：running / waiting / stopped
        agent_status = st.get("status", "")
        if agent_status == "running":
            status = "running"
        elif agent_status == "waiting":
            # 等用户或等外部资源，不是完成——旧代码把它算成 done，是假通知来源
            status = "idle"
        elif agent_status == "stopped":
            status = "done"
        else:
            # 没有状态事件的老会话：只能靠写入时间兜底
            status = "running" if now - st["mtime"] < ZOMBIE_DEFAULT else "idle"
        recent = st.get("live") or st.get("desc") or st.get("brief") or ""
        # title 缺失时用会话 id 短码，brief 留给副标题（brief 是状态描述不是标题）
        out.append({"agent": "cue", "title": st.get("title") or "Cue · " + sid[:6],
                    "cwd": "", "status": status,
                    "mtime": st["mtime"], "link": "cue://", "recent": recent, "recent_at": ""})
    return out


def scan_grok():
    # Electron blob 持久化：JSON rows 含会话标题/最近消息/活动时间
    d = os.path.expanduser("~/Library/Application Support/Grok Bot/sand-client-persistence")
    files = sorted(glob.glob(os.path.join(d, "*.blob")), key=os.path.getmtime)
    rows = {}
    for f in files[-8:]:
        try:
            for r in json.loads(open(f, "rb").read()).get("value", {}).get("rows", []):
                if isinstance(r, dict) and r.get("id"):
                    rows[r["id"]] = r
        except Exception:
            continue
    out = []
    now = time.time()
    for r in rows.values():
        act = r.get("lastActivityAt") or r.get("updatedAt")
        if not act:
            continue
        mtime = act / 1000 if act > 1e12 else act
        if now - mtime > SCAN_AGE:
            continue
        last = r.get("lastEntry") or {}
        recent = first_text(last.get("text") or (last.get("sessionPreview") or {}).get("text", "")) if isinstance(last, dict) else ""
        # Grok 的 blob 里没有 role/完成字段（awaitingUserResponse 实测全为 null），
        # 只有一个 lastActivityAt。用僵尸兜底窗口判断：窗口内算执行中，超过算已完成。
        if r.get("awaitingUserResponse"):
            status = "idle"
        elif now - mtime < ZOMBIE_DEFAULT:
            status = "running"
        else:
            status = "done"
        out.append({"agent": "grok-bot", "title": first_text(r.get("name") or r.get("title") or "Grok 会话"),
                    "cwd": "", "status": status, "mtime": mtime, "link": "sand://",
                    "recent": recent, "recent_at": ""})
    return out

def scan_apps():
    if not RUNTIME.supports_native_apps:
        return []
    out = []
    today_intl_alive = proc_alive("Today")
    today_cn_alive = proc_alive("Today 2")
    today_split = today_intl_alive and today_cn_alive
    # Today: chat sqlite 最近活动（文件名含设备 hash，动态发现）
    today_dbs = sorted(
        glob.glob(os.path.expanduser(
            "~/Library/Application Support/ai.today.macos.app/chat_*.sqlite")),
        key=os.path.getmtime)
    today_db = today_dbs[-1] if today_dbs else None
    today_title, today_mtime = None, 0
    today_last_role, today_wire_state = "", ""
    try:
        con = sqlite3.connect(f"file:{today_db}?mode=ro", uri=True)
        for (payload,) in con.execute(
                "SELECT payload FROM chatMessage ORDER BY messageIndex DESC LIMIT 40"):
            try:
                d = json.loads(payload)
            except Exception:
                continue
            ts = d.get("createdAt", 0) / 1000
            if ts > today_mtime:
                today_mtime = ts
                # 最后一条消息的角色：assistant 已回复=完成，user 等回复=执行中
                today_last_role = d.get("role") or ""
                today_wire_state = d.get("wireState") or ""
            if d.get("role") == "user" and not today_title:
                parts = d.get("parts") or [{}]
                t = parts[0].get("text", "")
                if t and not t.startswith("<"):
                    today_title = first_text(t)
        con.close()
    except Exception:
        pass
    for proc, (name, link, detail) in APP_AGENTS.items():
        if proc == "ChatGPT":
            # OpenAI 桌面端（Codex.app）主进程仍叫 ChatGPT。真实任务只来自
            # ~/.codex/sessions 的 scan_codex；进程在线不等于有任务，禁止再塞占位卡。
            continue
        alive = proc_alive(proc)
        if not alive:
            continue
        if proc == "Today" and today_mtime:
            age = time.time() - today_mtime
            if today_last_role == "assistant" and today_wire_state == "completed":
                status = "done"
            elif today_last_role == "user":
                # 用户发了消息、助手还没回：执行中（超时则降级 idle）
                status = demote_if_zombie("running", today_mtime, ZOMBIE_DEFAULT)
            else:
                status = "idle"
            agent_id = "today-intl" if today_split else "today"
            display = "Today Intl" if today_split else "Today"
            out.append({"agent": agent_id, "title": today_title or "今日助手", "cwd": "",
                        "status": status, "mtime": today_mtime,
                        # 两个版本都注册 today://，点击必须带 bundle id 才能落到正确 App
                        "link": "today://" if not today_split else "today://?bundle=ai.today.macos.app"})
        else:
            if proc == "Today 2":
                agent_id, display = ("today-cn", "Today CN") if today_split else ("today", "Today")
                # 单版本时装的是哪个就是哪个：Today 2 存在则 bundle 固定
                today_link = "today://?bundle=ai.today.macos.app.cn"
            else:
                agent_id = proc.lower().replace(" ", "-")
                display = name
                today_link = link
            # 无本地明细的 app：只能确认进程活跃，记 idle「在线」，不占执行中名额
            out.append({"agent": agent_id, "title": f"{display} 在线", "cwd": "",
                        "status": "idle", "mtime": time.time(),
                        "link": today_link, "recent": "应用在线 · 无本地任务明细", "recent_at": ""})
    # Cue / Grok 有本地明细：扫描真实会话（进程退出后 24h 记录仍可见）
    out.extend(scan_cue())
    out.extend(scan_grok())
    # 有真实明细的渠道去掉"应用在线"占位条目，避免重复
    return _drop_online_placeholders(out)


ONLINE_PLACEHOLDER_RECENT = "应用在线 · 无本地任务明细"


def _drop_online_placeholders(tasks):
    """同一 agent 已有真实会话时，丢掉「应用在线」占位。"""
    real = {t["agent"] for t in tasks if t.get("recent") != ONLINE_PLACEHOLDER_RECENT}
    return [
        t for t in tasks
        if t["agent"] not in real or t.get("recent") != ONLINE_PLACEHOLDER_RECENT
    ]


def _dsh_proc_alive():
    # 主进程名带空格，pgrep -x 匹配不到；按路径匹配
    r = subprocess.run(["pgrep", "-f", "DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness$"],
                       capture_output=True)
    return r.returncode == 0


def parse_dsh_file(f):
    """解析 dsh 会话文件，只返回解析事实，不返回 status。

    status 依赖"现在几点"（僵尸降级），必须每次读取时重算；
    缓存里存算好的 status 的话，mtime 不变就永远是那份快照。
    """
    title, recent = None, ""
    turn_open, turn_ts = False, []
    try:
        result = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=10)
        if result.returncode != 0:
            record_scan_error("deepseek", RuntimeError("decompression failed"))
        raw = result.stdout
    except Exception as exc:
        record_scan_error("deepseek", exc)
        raw = b""
    for line in raw.decode("utf-8", "ignore").splitlines():
        head = line[:600]
        if not ('"session/title"' in head or '"user/message"' in head
                or '"turn/start"' in head or '"turn/end"' in head):
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        t, d = j.get("type"), j.get("data") or {}
        ts = parse_ts(j.get("timestamp") or j.get("time") or d.get("timestamp"))
        if ts:
            turn_ts.append(ts)
        if t == "session/title" and not title and d.get("title"):
            title = first_text(d["title"])
        elif t == "user/message" and not recent:
            parts = d.get("content") or [{}]
            txt = parts[0].get("text", "") if isinstance(parts[0], dict) else ""
            # 跳过运行时上下文/系统注入的伪用户消息
            if txt and not txt.startswith(("<", "Current runtime", "Current DSH")):
                recent = first_text(txt)
        elif t == "turn/start":
            turn_open = True
        elif t == "turn/end":
            turn_open = False
    return {"title": title, "recent": recent,
            "turn_open": turn_open, "turn_ts": tuple(turn_ts)}


def scan_dsh():
    # DeepSeek Harness 会话：zstd 压缩的 v4 jsonl；turn/start → turn/end 是回合边界
    out = []
    now = time.time()
    for f in glob.glob(os.path.join(DSH_DIR, "*", "*", "session.v4.jsonl.zstd")):
        mtime = os.path.getmtime(f)
        if now - mtime > SCAN_AGE:
            continue
        cached = _file_cache["dsh"].get(f)
        if cached and cached[0] == mtime:
            facts = cached[1]
        else:
            facts = parse_dsh_file(f)
            _file_cache["dsh"][f] = (mtime, facts)
        title, recent = facts["title"], facts["recent"]
        if not (title or recent):
            continue
        if facts["turn_open"]:
            # 回合开着但久无写入 → 进程大概率已退出。
            # 每次读取都重算（缓存命中也一样），否则僵尸永远降不下来。
            status = demote_if_zombie("running", mtime, zombie_threshold(facts["turn_ts"]))
        else:
            status = "done"
        out.append({"agent": "deepseek", "title": title or recent or "DeepSeek 会话",
                    "cwd": "", "status": status,
                    "session_key": f,
                    "display_status": "unknown" if status == "idle" else status,
                    "mtime": mtime, "link": "dsh://",
                    "recent": recent if title else "", "recent_at": ""})
    return out


CODEX_THREAD_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def parse_codex_file(f):
    filename_id = re.search(rf"-({CODEX_THREAD_ID})(?:_{CODEX_THREAD_ID})?\.jsonl$", os.path.basename(f), re.I)
    thread_id = ""
    turns, current_key, turn_counter = {}, None, 0
    cwd = None

    def ensure(key, turn_id=""):
        if key not in turns:
            turns[key] = {"turn_key": turn_id or key, "title": None, "user_prompt": "",
                          "complete_at": "", "last_ev": "", "recent": "", "recent_at": "",
                          "reply_text": "", "turn_open": False, "ev_ts": [], "mtime": 0}
        return turns[key]

    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                head = line[:600]
                if not ((not thread_id and 'session_meta' in head)
                        or 'turn_context' in head or 'user_message' in head or 'item_completed' in head
                        or 'agent_message' in head or ('response_item' in head and '"assistant"' in head)
                        or 'task_complete' in head or 'task_started' in head or 'turn_aborted' in head):
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                ts = j.get("timestamp", "")
                p = j.get("payload", {}) or {}
                if j.get("type") == "session_meta":
                    meta_id = p.get("id")
                    if not thread_id and isinstance(meta_id, str) and re.fullmatch(CODEX_THREAD_ID, meta_id, re.I):
                        thread_id = meta_id.lower()
                    continue
                ts_epoch = parse_ts(ts)
                if j.get("type") == "turn_context" and not cwd:
                    cwd = p.get("cwd")
                item = (p.get("item") or {}) if p.get("type") == "item_completed" else {}
                turn_id = p.get("turn_id") or p.get("turnId") or item.get("turn_id")
                if turn_id:
                    current_key = str(turn_id)
                if j.get("type") == "event_msg" and p.get("type") == "task_started" and not turn_id:
                    turn_counter += 1
                    current_key = f"turn-{turn_counter}"
                if current_key is None:
                    current_key = "legacy"
                turn = ensure(current_key, str(turn_id or current_key))
                if ts_epoch:
                    turn["ev_ts"].append(ts_epoch)
                    turn["mtime"] = max(turn["mtime"], ts_epoch)
                    turn["last_ev"] = ts
                if j.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
                    text = assistant_text(p.get("content"))
                    if text:
                        turn["recent"], turn["recent_at"] = first_text(text), ts
                        if p.get("phase") in (None, "final_answer"):
                            turn["reply_text"] = text
                if j.get("type") == "event_msg":
                    pt = p.get("type")
                    item_type = item.get("type")
                    if pt == "task_started":
                        # 同会话新回合开始时，显式关闭仍 open 的旧回合。
                        # Codex 偶发不写旧回合的 task_complete；若不关闭，后面
                        # scan 又用文件 mtime 刷新该回合，会永远显示「执行中」，
                        # 并在你发新任务时把它顶到列表最前、再误触发完成通知。
                        for key, other in turns.items():
                            if key == current_key or not other["turn_open"]:
                                continue
                            other["turn_open"] = False
                            if not other.get("complete_at"):
                                other["complete_at"] = other.get("last_ev") or ts
                        turn["turn_open"] = True
                        turn["complete_at"] = ""
                        turn["reply_text"] = ""
                    elif pt == "task_complete":
                        turn["turn_open"] = False
                        turn["complete_at"] = ts
                        final = p.get("last_agent_message")
                        if isinstance(final, str) and final.strip():
                            turn["reply_text"] = final.strip()
                            turn["recent_at"] = ts
                    elif pt == "turn_aborted":
                        # 用户中断不代表任务完成。
                        turn["turn_open"] = False
                        turn["complete_at"] = ""
                        turn["reply_text"] = ""
                    if pt == "user_message" or item_type in ("UserMessage", "userMessage") or (item_type == "message" and item.get("role") == "user"):
                        turn["reply_text"], turn["complete_at"] = "", ""
                        text = p.get("message", "") if pt == "user_message" else _message_text(item.get("content"))
                        prompt = user_prompt_text(text)
                        if prompt:
                            turn["user_prompt"] = prompt
                            turn["title"] = first_text(prompt)
                            turn["recent"], turn["recent_at"] = first_text(prompt), ts
                    if pt == "agent_message" or item_type in ("AgentMessage", "agentMessage") or (item_type == "message" and item.get("role") == "assistant"):
                        text = assistant_text(p.get("message") if pt == "agent_message" else item.get("content"))
                        if text:
                            turn["recent"], turn["recent_at"] = first_text(text), ts
                            if (p if pt == "agent_message" else item).get("phase") in (None, "final_answer"):
                                turn["reply_text"] = text
    except OSError as exc:
        record_scan_error("codex", exc)
    thread_id = thread_id or (filename_id.group(1).lower() if filename_id else "")
    fallback = "/".join((cwd or "").rstrip("/").split("/")[-2:]) if cwd else "Codex 会话"
    output = []
    for turn in turns.values():
        if not (turn["title"] or turn["user_prompt"] or turn["reply_text"]):
            continue
        output.append({"agent": "codex", "title": turn["title"] or fallback,
                       "full_title": turn["user_prompt"] or turn["title"] or fallback,
                       "user_prompt": turn["user_prompt"], "cwd": cwd or "",
                       "mtime": turn["mtime"] or os.path.getmtime(f),
                       "link": "codex://threads/" + thread_id if thread_id else "",
                       "turn_key": turn["turn_key"], "complete_at": turn["complete_at"],
                       "last_ev": turn["last_ev"], "turn_open": turn["turn_open"],
                       "ev_ts": turn["ev_ts"], "recent": turn["recent"],
                       "recent_at": turn["recent_at"],
                       "reply_text": turn["reply_text"] if turn["complete_at"] else ""})
    return output

def scan_codex():
    now = time.time()
    out = []
    for f in glob.glob(os.path.join(CODEX_DIR, "**/*.jsonl"), recursive=True):
        mtime = os.path.getmtime(f)
        if now - mtime > SCAN_AGE:
            continue
        cached = _file_cache["codex"].get(f)
        if cached and cached[0] == mtime:
            r = cached[1]
        else:
            r = parse_codex_file(f)
            _file_cache["codex"][f] = (mtime, r)
        # 同文件多回合：只有时间上最新的那条才保留「待回复」
        latest_key = None
        latest_rank = None
        for t in r:
            rank = (parse_ts(t.get("last_ev")) or 0.0, float(t.get("mtime") or 0.0))
            if latest_rank is None or rank >= latest_rank:
                latest_rank = rank
                latest_key = t.get("turn_key")
        for t in r:
            t = dict(t)
            file_mtime = os.path.getmtime(f)
            turn_mtime = float(t.get("mtime") or 0) or file_mtime
            # 已拆出的历史回合保留自身最后事件时间；只有「本回合仍在写」时
            # 才抬升到文件 mtime。同会话后续回合的写入绝不能刷新旧 open 回合，
            # 否则缺 task_complete 的卡片会永远像「刚刚还在跑」。
            if t["turn_open"] and file_mtime - turn_mtime < 30:
                t["mtime"] = max(turn_mtime, file_mtime)
            elif not t.get("mtime"):
                t["mtime"] = file_mtime
            else:
                t["mtime"] = turn_mtime
            t["session_key"] = f + "#" + str(t.get("turn_key") or "legacy")
            t["awaiting_user"] = t.get("turn_key") == latest_key
            if t["turn_open"]:
                # 僵尸判定看本回合最后事件，不看整文件 mtime
                set_execution_status(t, turn_mtime, zombie_threshold(t["ev_ts"]))
            elif t["complete_at"]:
                t["status"] = "done"
                t["recent"] = format_conclusion(t["reply_text"] or t["recent"])
            else:
                # 没有回合边界的旧会话（未产生 task_started/task_complete）
                t["status"] = "idle"
                t["display_status"] = "unknown"
                t["status_reason"] = "会话缺少回合边界，无法确认是否执行中。"
            t.pop("turn_open", None)
            t.pop("ev_ts", None)
            out.append(t)
    return out

def _parse_claude_legacy(f):
    title, cwd, last_user_ts, last_asst_ts = None, None, "", ""
    recent, recent_at = "", ""
    reply_text = ""
    # 最后一条消息的角色 + 最后一条 assistant 的 stop_reason 决定回合状态：
    # end_turn/stop_sequence = 已回复完；tool_use = 还在跑工具；最后是 user = 等回复
    last_role, last_stop_reason = "", ""
    ev_ts = []
    is_sidechain = False
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                head = line[:300]
                if not ('cwd' in head or '"user"' in head or '"assistant"' in head or 'summary' in head):
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                t = j.get("type")
                if j.get("isSidechain") is True:
                    is_sidechain = True
                if cwd is None:
                    cwd = j.get("cwd")
                ts = j.get("timestamp", "")
                ts_epoch = parse_ts(ts)
                if ts_epoch:
                    ev_ts.append(ts_epoch)
                if t == "user":
                    last_role = "user"
                    reply_text = ""
                    c = (j.get("message") or {}).get("content")
                    if isinstance(c, str) and not title and not c.startswith("<"):
                        title = re.sub(r"\s+", " ", c).strip()[:2000]
                    if ts:
                        last_user_ts = ts
                elif t == "assistant" and ts:
                    last_role = "assistant"
                    last_stop_reason = (j.get("message") or {}).get("stop_reason") or ""
                    last_asst_ts = ts
                    reply_text = assistant_text((j.get("message") or {}).get("content"))
                    if reply_text:
                        recent, recent_at = first_text(reply_text), ts
    except OSError as exc:
        record_scan_error("claude", exc)
    if not title:
        return []
    if is_sidechain:
        return []
    # 工作流 harness 会持续生成两类噪音会话：
    #   1) cwd 落在 ~/.claude/projects 里的元会话（"关于会话的会话"）
    #   2) 首条用户消息以 [Workflow harness — …] 开头的派发任务
    # 实测这类文件一分钟能刷出十几条，会把任务列表冲垮。
    if cwd and os.path.abspath(cwd).startswith(os.path.abspath(CLAUDE_DIR)):
        return []
    if title.startswith("[Workflow harness"):
        return []
    # 标题退化成 "-" 之类的噪音（首条消息是 shell 回显）
    if len(title.strip(" -\t")) < 2:
        return []
    # 首条消息是命令路径回显（如 claude 自身报错 "fork: Resource temporarily unavailable"）
    if title.startswith("/") and "/bin/" in title[:80]:
        return []
    return [{"agent": "claude", "title": title, "cwd": cwd or "", "mtime": os.path.getmtime(f),
             "link": "", "last_user": last_user_ts, "last_asst": last_asst_ts,
             "last_role": last_role, "stop_reason": last_stop_reason, "ev_ts": ev_ts,
             "recent": recent, "recent_at": recent_at,
             "reply_text": reply_text if last_stop_reason in ("end_turn", "stop_sequence") else ""}]


def parse_claude_file(f):
    """Claude transcript：每个 user 消息开始一个独立回合。"""
    cwd, is_sidechain = None, False
    turns, current = [], None
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("isSidechain") is True:
                    is_sidechain = True
                if cwd is None and record.get("cwd"):
                    cwd = record["cwd"]
                kind = record.get("type")
                message = record.get("message") if isinstance(record.get("message"), dict) else record
                content = message.get("content") if isinstance(message, dict) else None
                text = _message_text(content)
                if kind == "user":
                    prompt = user_prompt_text(text)
                    if not prompt:
                        continue
                    if current:
                        turns.append(current)
                    current = {"title": first_text(prompt), "user_prompt": prompt, "recent": first_text(prompt),
                               "reply_text": "", "last_role": "user", "stop_reason": "", "ev_ts": [],
                               "turn_key": f"turn-{len(turns) + 1}"}
                elif kind == "assistant" and current:
                    current["last_role"] = "assistant"
                    current["stop_reason"] = message.get("stop_reason") or record.get("stop_reason") or ""
                    text = assistant_text(content)
                    if text:
                        current["reply_text"] = text
                        current["recent"] = first_text(text)
    except OSError as exc:
        record_scan_error("claude", exc)
        return []
    if current:
        turns.append(current)
    if is_sidechain:
        return []
    mtime = os.path.getmtime(f)
    return [{"agent": "claude", "title": turn["title"], "full_title": turn["user_prompt"],
             "user_prompt": turn["user_prompt"], "cwd": cwd or "", "mtime": mtime,
             "link": "", "last_role": turn["last_role"], "stop_reason": turn["stop_reason"],
             "ev_ts": turn["ev_ts"], "recent": turn["recent"], "recent_at": "",
             "reply_text": turn["reply_text"] if turn["stop_reason"] in ("end_turn", "stop_sequence") else "",
             "turn_key": turn["turn_key"]} for turn in turns if turn["title"]]

def scan_claude():
    now = time.time()
    out = []
    for f in glob.glob(os.path.join(CLAUDE_DIR, "**/*.jsonl"), recursive=True):
        # 子 agent 的转录单独写成 agent-*.jsonl，不是用户任务，混进来会让列表翻倍
        if os.path.basename(f).startswith("agent-"):
            continue
        mtime = os.path.getmtime(f)
        if now - mtime > SCAN_AGE:
            continue
        cached = _file_cache["claude"].get(f)
        if cached and cached[0] == mtime:
            r = cached[1]
        else:
            r = parse_claude_file(f)
            _file_cache["claude"][f] = (mtime, r)
        for t in r:
            t = dict(t)
            t["session_key"] = f + "#" + str(t.get("turn_key") or "legacy")
            last_role, stop_reason = t["last_role"], t["stop_reason"]
            if last_role == "assistant" and stop_reason in ("end_turn", "stop_sequence"):
                t["status"] = "done"
                t["recent"] = format_conclusion(t["reply_text"] or t["recent"])
            elif last_role == "user" or stop_reason == "tool_use":
                # 用户已发消息等回复，或还在跑工具：执行中；久无写入则进程已退
                set_execution_status(t, t["mtime"], zombie_threshold(t["ev_ts"]))
            else:
                t["status"] = "idle"
            t.pop("last_role", None)
            t.pop("stop_reason", None)
            t.pop("ev_ts", None)
            out.append(t)
    return out


def _epoch_from_ms(value):
    """毫秒/秒时间戳 → epoch 秒。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if n > 1e12:
        return n / 1000.0
    return n


def _cursor_cwd(workspace):
    if not isinstance(workspace, dict):
        return ""
    uri = workspace.get("uri")
    if isinstance(uri, dict):
        return str(uri.get("fsPath") or uri.get("path") or "")
    if isinstance(uri, str) and uri.startswith("file://"):
        return uri[7:]
    return ""


def _cursor_transcript_path(composer_id):
    """~/.cursor/projects/**/agent-transcripts/<id>/<id>.jsonl"""
    if not composer_id:
        return ""
    pattern = os.path.join(CURSOR_PROJECTS_DIR, "*", "agent-transcripts", str(composer_id), f"{composer_id}.jsonl")
    hits = glob.glob(pattern)
    return hits[0] if hits else ""


def _cursor_read_transcript_tail(composer_id, max_bytes=256_000):
    """读转录文件末尾文本；返回 (mtime, text) 或 (0, '')."""
    path = _cursor_transcript_path(composer_id)
    if not path or not os.path.isfile(path):
        return 0.0, ""
    try:
        mtime = os.path.getmtime(path)
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                fh.readline()
            data = fh.read().decode("utf-8", errors="ignore")
        return mtime, data
    except OSError:
        return 0.0, ""


def _cursor_transcript_boundary(composer_id):
    """转录尾部回合边界：open=仍在跑/刚发了下一条；closed=最后事件是 turn_ended；unknown=无转录。

    这是避免「工具间隙 unfinished 被清空」和「上一轮结束后下一轮尚未打上 unfinished」
    误报完成的关键信号。
    """
    cache_key = f"boundary:{composer_id}"
    mtime, data = _cursor_read_transcript_tail(composer_id)
    if not data:
        return "unknown"
    cached = _file_cache["cursor"].get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    last = "unknown"
    for line in data.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        if j.get("type") == "turn_ended":
            last = "closed"
            continue
        role = j.get("role")
        if role == "user":
            last = "open"
            continue
        if role == "assistant":
            last = "open"
            continue
    _file_cache["cursor"][cache_key] = (mtime, last)
    return last


def _cursor_transcript_turns(composer_id):
    """按每条 user 消息和 turn_ended 切分 Cursor 会话。"""
    file_mtime, data = _cursor_read_transcript_tail(composer_id, max_bytes=8_000_000)
    if not data:
        return []
    turns, current = [], None
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        record_time = _cursor_record_time(record)
        if record.get("type") == "turn_ended":
            if current:
                current["turn_open"] = False
                current["turn_status"] = str(record.get("status") or "")
                if record_time:
                    current["mtime"] = max(current["mtime"], record_time)
                turns.append(current)
                current = None
            continue
        role = record.get("role")
        content = (record.get("message") or {}).get("content")
        if role == "user":
            prompt = user_prompt_text(_message_text(content))
            if prompt:
                if current:
                    turns.append(current)
                current = {"user_prompt": prompt, "title": first_text(prompt), "reply_text": "",
                           "recent": first_text(prompt), "turn_open": True, "turn_status": "",
                           "turn_key": f"turn-{len(turns) + 1}",
                           "mtime": record_time or file_mtime}
        elif role == "assistant" and current:
            text = assistant_text(content)
            if text:
                current["reply_text"] = text
                current["recent"] = format_conclusion(text)
                if record_time:
                    current["mtime"] = max(current["mtime"], record_time)
    if current:
        turns.append(current)
    return turns


def _cursor_record_time(record):
    """提取 Cursor 转录记录时间；兼容显式字段和用户消息中的时间注入。"""
    for key in ("timestamp", "time", "createdAt", "created_at", "updatedAt", "updated_at"):
        value = record.get(key)
        if value:
            parsed = parse_ts(value) if isinstance(value, str) else _epoch_from_ms(value)
            if parsed:
                return parsed
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    content = message.get("content") if isinstance(message, dict) else None
    text = _message_text(content)
    match = re.search(
        r"<timestamp>[^,]+,\s*([A-Za-z]+\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s+[AP]M)\s+\(UTC([+-]\d{1,2})(?::?(\d{2}))?\)</timestamp>",
        text,
    )
    if not match:
        return 0.0
    try:
        local = datetime.datetime.strptime(match.group(1), "%b %d, %Y, %I:%M %p")
        offset = int(match.group(2)) * 60 + int(match.group(3) or 0)
        if int(match.group(2)) < 0:
            offset = -abs(offset)
        return local.replace(tzinfo=datetime.timezone(datetime.timedelta(minutes=offset))).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _cursor_last_assistant_text(composer_id):
    """读转录末尾，取最后一条助手纯文本（真正的回合结果，而不是 Edited xxx）。"""
    mtime, data = _cursor_read_transcript_tail(composer_id)
    if not data:
        return ""
    cache_key = f"assistant:{composer_id}"
    cached = _file_cache["cursor"].get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1] or ""
    last = ""
    for line in data.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        if j.get("role") == "user":
            last = ""
        if j.get("role") != "assistant":
            continue
        msg = j.get("message") if isinstance(j.get("message"), dict) else j
        content = msg.get("content") if isinstance(msg, dict) else None
        text = assistant_text(content)
        # 跳过纯 tool_use 块
        if not text:
            continue
        last = text
    _file_cache["cursor"][cache_key] = (mtime, last)
    return last


def _cursor_plan_followed_by_newer_turn(turns):
    """计划确认卡提出后，若用户又开了一轮并已答完，则 hasPendingPlan 多半是残留。

    真在等 Build 时，最新回合就是提出计划的那一轮，不会再多出已闭合的后续回合。
    """
    if len(turns) < 2:
        return False
    latest = turns[-1]
    if latest.get("turn_open"):
        return False
    plan_idx = None
    for index, turn in enumerate(turns):
        # 只看助手侧文案，避免后续用户标题复述「计划确认」把索引推到最后一轮
        text = " ".join(str(turn.get(key) or "") for key in ("reply_text", "recent"))
        if re.search(r"计划确认|确认卡|点击\s*Build|pending plan|用本计划|Plan\s*模式|hasPendingPlan", text, re.I):
            plan_idx = index
            break
    if plan_idx is None:
        return False
    return plan_idx < len(turns) - 1


def _cursor_change_stats(files_changed, lines_added, lines_removed):
    stats_parts = []
    try:
        fc, la, lr = int(files_changed or 0), int(lines_added or 0), int(lines_removed or 0)
    except (TypeError, ValueError):
        fc = la = lr = 0
    if fc > 0:
        stats_parts.append(f"{fc} 个文件")
    if la or lr:
        stats_parts.append(f"+{la}/-{lr}")
    return " · ".join(stats_parts)


def _scan_cursor_headers():
    """从 Cursor state.vscdb 的 composerHeaders 读 Agent 会话。"""
    db = CURSOR_STATE_DB
    if not os.path.isfile(db):
        return []
    now = time.time()
    out = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT composerId, createdAt, lastUpdatedAt, isArchived, isSubagent, value "
            "FROM composerHeaders"
        ).fetchall()
        con.close()
    except Exception as exc:
        record_scan_error("cursor", exc)
        return []
    for cid, created_at, updated_at, archived, is_sub, raw in rows:
        if archived or is_sub:
            continue
        try:
            j = json.loads(raw) if raw else {}
        except Exception:
            continue
        if j.get("isDraft") and not j.get("unfinishedRunAt") and not j.get("name"):
            continue
        mtime = _epoch_from_ms(updated_at or j.get("lastUpdatedAt") or created_at or j.get("createdAt"))
        if mtime and now - mtime > SCAN_AGE:
            continue
        title = first_text(j.get("name") or "") or first_text(j.get("subtitle") or "")
        if not title:
            continue
        cwd = _cursor_cwd(j.get("workspaceIdentifier"))
        unfinished = j.get("unfinishedRunAt")
        boundary = _cursor_transcript_boundary(cid)
        transcript_turns = _cursor_transcript_turns(cid)
        # hasPendingPlan 在用户已点 Build / 继续发消息后常会残留。
        # 用「首次见到时的回合数」「pending 期间是否跑过 unfinished」
        # 「转录里提出计划后又完成了更新的用户回合」判断是否已被后续动作取代。
        cid_key = str(cid)
        if j.get("hasPendingPlan") is True:
            turn_n = len(transcript_turns)
            _cursor_pending_plan_at.setdefault(cid_key, turn_n)
            if unfinished:
                _cursor_pending_plan_ran.add(cid_key)
            plan_superseded = (
                turn_n > _cursor_pending_plan_at[cid_key]
                or cid_key in _cursor_pending_plan_ran
                or _cursor_plan_followed_by_newer_turn(transcript_turns)
            )
            pending_plan = (not unfinished and boundary != "open" and not plan_superseded)
        else:
            _cursor_pending_plan_at.pop(cid_key, None)
            _cursor_pending_plan_ran.discard(cid_key)
            pending_plan = False
        waiting_reason = ""
        if j.get("hasBlockingPendingActions"):
            waiting_reason = "Cursor 正在等待你确认操作。"
        elif pending_plan:
            waiting_reason = "Cursor 已生成计划，正在等待你点击 Build。"
        blocked = bool(waiting_reason)
        # checkpoint 往往比 unfinishedRunAt / lastUpdatedAt 更勤；用来识别回合间隙误清空
        ckpt = _epoch_from_ms(
            j.get("conversationCheckpointLastUpdatedAt")
            or updated_at
            or j.get("lastUpdatedAt")
            or created_at
        )
        activity = max(mtime or 0.0, ckpt or 0.0) or now
        subtitle = first_text(j.get("subtitle") or "")
        # 终态或接近终态时才读转录摘要，避免每个 running 会话扫大 JSONL
        want_summary = (not unfinished) or blocked
        reply_text = _cursor_last_assistant_text(cid) if want_summary else ""
        stats = _cursor_change_stats(j.get("filesChangedCount"), j.get("totalLinesAdded"), j.get("totalLinesRemoved")) if want_summary else ""
        recent = format_conclusion(reply_text or subtitle, stats) if want_summary else subtitle
        # 缓存只存事实；状态每次重算（含僵尸降级）
        cache_key = f"header:{cid}"
        facts = {
            "title": title,
            "cwd": cwd,
            "recent": recent,
            "unfinished": bool(unfinished),
            "blocked": blocked,
            "mtime": activity,
            "ckpt": ckpt or 0.0,
        }
        facts["boundary"] = boundary
        _file_cache["cursor"][cache_key] = (mtime, facts)
        quiet = (now - facts["ckpt"]) if facts["ckpt"] else 9999
        if facts["blocked"]:
            status = "idle"
        elif facts["unfinished"]:
            _cursor_unfinished_seen[str(cid)] = now
            status = demote_if_zombie("running", facts["mtime"], ZOMBIE_DEFAULT, now=now)
        else:
            sticky_at = _cursor_unfinished_seen.get(str(cid), 0.0)
            sticky = sticky_at and (now - sticky_at) < CURSOR_RUN_STICKY_SECS
            if boundary == "open" or sticky or quiet < CURSOR_DONE_QUIET_SECS:
                # 无 unfinished 但仍可能在 Thinking / 工具间隙：
                # - 转录回合未闭合
                # - 刚见过 unfinished（sticky，覆盖长 Thinking 清空）
                # - checkpoint 静默未超时
                status = demote_if_zombie("running", facts["mtime"], ZOMBIE_DEFAULT, now=now)
                if not facts["recent"]:
                    facts["recent"] = subtitle
            else:
                # 仅当：无 unfinished + sticky 过期 + 转录已闭合/未知 + 静默超时 → 完成
                status = "done"
                _cursor_unfinished_seen.pop(str(cid), None)
        # agentpanel://cursor：由原生壳打开 Cursor IDE 工程；附带会话 id 便于后续聚焦
        link = _cursor_ide_link(cid, facts["cwd"])
        turns = transcript_turns or [{"title": facts["title"], "user_prompt": "",
                                     "recent": facts["recent"], "reply_text": reply_text,
                                     "turn_open": status == "running", "turn_status": "success" if status == "done" else ""}]
        for index, turn in enumerate(reversed(turns)):
            is_current = index == 0
            # unfinishedRunAt / checkpoint 属于整个 composer。新回合刚开始时，
            # 转录可能还没写入新的 user 消息；此时最后一个已闭合回合仍必须保持 done，
            # 不能把会话级 running 错套到上一回合。
            turn_is_open = bool(turn.get("turn_open"))
            # 等 Build / 工具审批：会话级 blocked，即便转录回合已闭合也要保持 idle+waiting，
            # 不能因为 turn_open=False 被压成 done。
            if is_current and facts["blocked"]:
                turn_status = "idle"
            elif is_current and turn_is_open:
                turn_status = status
            else:
                turn_status = "done"
            turn_display = ("waiting" if facts["blocked"] and is_current else
                            "unknown" if turn_status == "idle" else turn_status)
            turn_reply = turn.get("reply_text", "") if turn_status == "done" else ""
            turn_stats = stats if is_current else ""
            out.append({
                "agent": "cursor",
                "title": turn.get("title") or facts["title"],
                "full_title": turn.get("user_prompt") or turn.get("title") or facts["title"],
                "user_prompt": turn.get("user_prompt", ""),
                "cwd": facts["cwd"], "status": turn_status,
                "display_status": turn_display,
                "status_reason": (waiting_reason if is_current and facts["blocked"] else
                                  "Cursor 最后一次执行信号已过期，无法确认是否仍在执行。" if turn_display == "unknown" else ""),
                "mtime": facts["mtime"] if is_current else (turn.get("mtime") or facts["mtime"]),
                "link": link, "session_key": f"{cid}#{turn.get('turn_key', f'turn-{index + 1}')}",
                "turn_key": turn.get("turn_key", f"turn-{index + 1}"),
                "recent": (format_conclusion(turn.get("reply_text") or turn.get("recent") or facts["recent"], turn_stats)
                           if is_current and turn_status == "done" else turn.get("recent") or (facts["recent"] if is_current else "")),
                "recent_at": "", "reply_text": turn_reply,
                # 只有最新回合才进「待回复」；同会话继续提问后，旧闸语句卡片应退出待处理
                "awaiting_user": is_current,
                **({"handoff": {"changes": stats}} if is_current and turn_status == "done" and stats else {}),
            })
    return out


def _cursor_ide_link(composer_id, cwd=""):
    """工程会话交给 IDE 桥接；无工程会话使用 Cursor 原生链接。"""
    if not cwd and composer_id:
        return "cursor://anysphere.cursor-deeplink/agent?" + urlencode({"id": composer_id})
    params = {}
    if composer_id:
        params["id"] = composer_id
    if cwd:
        params["cwd"] = cwd
    if not params:
        return "cursor://"
    return "agentpanel://cursor?" + urlencode(params)


def _message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("text"):
                parts.append(str(blk["text"]))
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return ""


def parse_cursor_transcript(f):
    """Cursor agent-transcripts JSONL：role + turn_ended 边界。"""
    title, recent, last_role = None, "", ""
    reply_text = ""
    turn_open, turn_status = False, ""
    ev_ts = []
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("type") == "turn_ended":
                    turn_open = False
                    turn_status = str(j.get("status") or "")
                    continue
                role = j.get("role")
                if role not in ("user", "assistant"):
                    continue
                last_role = role
                content = (j.get("message") or {}).get("content")
                if role == "user":
                    text = user_prompt_text(_message_text(content))
                    text = clean_title(text)
                    turn_open = True
                    reply_text = ""
                    if text and not title:
                        title = text
                    if text:
                        recent = text
                elif (text := assistant_text(content)):
                    reply_text = text
                    recent = format_conclusion(text)
                # 转录无稳定时间戳字段时用文件 mtime 兜底
    except OSError:
        return []
    if not title:
        return []
    return [{
        "title": title, "recent": recent, "last_role": last_role,
        "turn_open": turn_open, "turn_status": turn_status, "ev_ts": ev_ts, "reply_text": reply_text,
    }]


def _scan_cursor_transcripts():
    now = time.time()
    out = []
    pattern = os.path.join(CURSOR_PROJECTS_DIR, "*", "agent-transcripts", "*", "*.jsonl")
    for f in glob.glob(pattern):
        mtime = os.path.getmtime(f)
        if now - mtime > SCAN_AGE:
            continue
        cached = _file_cache["cursor"].get(f)
        if cached and cached[0] == mtime:
            facts = cached[1]
        else:
            parsed = parse_cursor_transcript(f)
            facts = parsed[0] if parsed else None
            _file_cache["cursor"][f] = (mtime, facts)
        if not facts:
            continue
        if facts["turn_open"] or facts["last_role"] == "user":
            status = demote_if_zombie("running", mtime, zombie_threshold(facts["ev_ts"]), now=now)
        elif facts["turn_status"] in ("success", "error", "aborted") or facts["last_role"] == "assistant":
            status = "done"
        else:
            status = "idle"
        # 项目目录名 Users-foo-bar → 粗略 cwd 兜底
        proj = f.split(os.sep)
        try:
            idx = proj.index("projects")
            slug = proj[idx + 1]
            cwd = "/" + slug.replace("-", "/") if slug.startswith("Users-") else slug
        except (ValueError, IndexError):
            cwd = ""
        # 转录文件名即 composer/session id
        sid = os.path.splitext(os.path.basename(f))[0]
        link = _cursor_ide_link(sid, cwd)
        if facts.get("turn_status") == "error":
            status = "failed"
        turns = _cursor_transcript_turns(sid)
        if not turns:
            turns = [{"title": facts["title"], "user_prompt": facts["title"], "recent": facts["recent"],
                      "reply_text": facts["reply_text"], "turn_status": facts["turn_status"],
                      "turn_open": facts["turn_open"]}]
        for index, turn in enumerate(reversed(turns)):
            is_current = index == 0
            turn_status = status if is_current and turn.get("turn_open", False) else "done"
            if is_current and turn.get("turn_status") == "error":
                turn_status = "failed"
            out.append({
                "agent": "cursor", "title": turn.get("title") or facts["title"],
                "full_title": turn.get("user_prompt") or turn.get("title") or facts["title"],
                "user_prompt": turn.get("user_prompt", ""), "cwd": cwd,
                "status": turn_status,
                "mtime": mtime if is_current else (turn.get("mtime") or mtime),
                "link": link, "display_status": turn_status,
                "session_key": f"{sid}#{turn.get('turn_key', f'turn-{index + 1}')}",
                "turn_key": turn.get("turn_key", f"turn-{index + 1}"),
            "recent": (format_conclusion(turn.get("reply_text") or turn.get("recent", ""))
                       if turn_status == "done" else turn.get("recent", "")), "recent_at": "",
                "reply_text": turn.get("reply_text", "") if turn_status == "done" else "",
            })
    return out


def scan_cursor():
    """Cursor：优先 composerHeaders（有标题/工作区/`unfinishedRunAt`）；无库时退回 transcripts。"""
    headers = _scan_cursor_headers()
    if headers:
        return headers
    return _scan_cursor_transcripts()


def _qoder_state_status(state_path):
    """读会话旁路 state.json；字段因版本而异，保守识别执行中。"""
    try:
        with open(state_path, errors="ignore") as fh:
            st = json.load(fh)
    except Exception:
        return None
    if not isinstance(st, dict):
        return None
    for key in ("status", "agentStatus", "state", "sessionStatus"):
        val = st.get(key)
        if isinstance(val, str):
            low = val.lower()
            if low in ("running", "in_progress", "in-progress", "busy", "streaming", "generating"):
                return "running"
            if low in ("done", "completed", "success", "idle", "stopped", "finished"):
                return "done" if low in ("done", "completed", "success", "finished") else "idle"
    if st.get("isRunning") is True or st.get("running") is True:
        return "running"
    return None


def qoder_session_link(session_id):
    if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return "qoder-cn://chat/open?" + urlencode({"sessionId": session_id})
    return ""


def parse_qoder_cn_file(f):
    """Qoder CN 转录：Claude 兼容 JSONL（type/role + stop_reason）。"""
    title, cwd, recent, session_id = None, None, "", ""
    reply_text = ""
    last_role, last_stop_reason = "", ""
    turns, current = [], None
    ev_ts = []
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                t = j.get("type") or j.get("role")
                if not session_id and qoder_session_link(j.get("sessionId")):
                    session_id = j["sessionId"]
                if cwd is None and j.get("cwd"):
                    cwd = j.get("cwd")
                ts = j.get("timestamp") or j.get("gmt_create")
                ts_epoch = parse_ts(ts) if isinstance(ts, str) else _epoch_from_ms(ts) if ts else None
                if ts_epoch:
                    ev_ts.append(ts_epoch)
                msg = j.get("message") if isinstance(j.get("message"), dict) else j
                content = msg.get("content") if isinstance(msg, dict) else None
                text = _message_text(content) if content is not None else ""
                if not text and isinstance(j.get("content"), str):
                    text = j["content"]
                if t == "user":
                    prompt = user_prompt_text(text)
                    if not prompt:
                        continue
                    if current:
                        turns.append(current)
                    current = {"title": first_text(prompt), "user_prompt": prompt, "reply_text": "",
                               "recent": first_text(prompt), "last_stop_reason": "",
                               "turn_key": f"turn-{len(turns) + 1}"}
                    title = title or first_text(prompt)
                    last_role, reply_text = "user", ""
                elif t == "assistant":
                    last_role = "assistant"
                    last_stop_reason = ""
                    if isinstance(msg, dict):
                        last_stop_reason = msg.get("stop_reason") or j.get("stop_reason") or ""
                    reply_text = assistant_text(content or j.get("content"))
                    if current:
                        current["last_stop_reason"] = last_stop_reason
                    if reply_text:
                        recent = first_text(reply_text)
                        if current:
                            current["reply_text"] = reply_text
                            current["recent"] = recent
    except OSError:
        return []
    if current:
        turns.append(current)
    if not title:
        return []
    if title.startswith("[Workflow harness"):
        return []
    state_status = None
    sid = os.path.splitext(os.path.basename(f))[0]
    state_path = os.path.join(os.path.dirname(f), sid, "state.json")
    if os.path.isfile(state_path):
        state_status = _qoder_state_status(state_path)
    if not turns:
        turns = [{"title": title, "user_prompt": title or "Qoder CN 会话", "recent": recent,
                  "reply_text": reply_text, "last_stop_reason": last_stop_reason, "turn_key": "legacy"}]
    turn = turns[-1]
    return [{"title": turn["title"], "user_prompt": turn["user_prompt"], "cwd": cwd or "",
             "recent": turn["recent"], "last_role": last_role, "stop_reason": turn["last_stop_reason"],
             "state_status": state_status, "ev_ts": ev_ts, "session_id": session_id or sid,
             "reply_text": turn["reply_text"], "turn_key": turn["turn_key"], "turns": turns}]


def qoder_runner_alive(pid):
    windows = RUNTIME.id == "windows"
    command = (["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"] if windows
               else ["ps", "-p", str(pid), "-o", "comm="])
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    if windows:
        return any(len(row) >= 2 and row[1] == str(pid)
                   and row[0].lower() in {"qoder cn.exe", "qodercn.exe", "qodercli.exe"}
                   for row in csv.reader(result.stdout.splitlines()))
    return Path(result.stdout.strip()).name.lower() in {"qoder cn", "qodercli"}


def qoder_log_tail(path, size, limit):
    with path.open("rb") as stream:
        stream.seek(max(0, size - limit))
        tail = stream.read(limit)
    return tail.partition(b"\n")[2] if size > limit else tail


@lru_cache(maxsize=16)
def qoder_permission_dispatches(path, version, size):
    tail = qoder_log_tail(path, size, 512_000).decode("utf-8", errors="replace")
    blocks = re.findall(r"(?m)^\S+[^\n]*?\[thread-session\] permission ask dispatched \{\r?\n((?:[ \t]+[^\n]*\n){1,12})\}", tail)
    requests = []
    for block in blocks:
        fields = dict(re.findall(r"(?m)^\s*(sessionId|toolUseId): '([\w-]+)'", block))
        if fields.keys() == {"sessionId", "toolUseId"}:
            name = re.search(r"(?m)^\s*toolName: '([A-Za-z0-9_-]{1,64})'", block)
            requests.append((fields["sessionId"], fields["toolUseId"], name[1] if name else ""))
    return requests


@lru_cache(maxsize=64)
def qoder_segment_permission_events(path, version, size):
    events = []
    for line in qoder_log_tail(path, size, 512_000).splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind, turn, tool = event.get("type"), event.get("turn_id"), event.get("tool_call_id")
        if isinstance(turn, str) and (kind == "turn.finished" or (
            kind in ("tool.requested", "permission.resolved", "tool.execution.finished") and isinstance(tool, str)
        )):
            events.append((kind, turn, tool))
    return events


def qoder_waiting_sessions(projects, now):
    logs = Path(projects).parent / "logs"
    pending = {}
    for log in (logs / "runs").glob("*/qodercli.log"):
        process = re.search(r"-p([1-9][0-9]*)$", log.parent.name)
        if not process:
            continue
        try:
            info = log.stat()
            if now - info.st_mtime > SCAN_AGE:
                continue
            dispatches = qoder_permission_dispatches(log, info.st_mtime_ns, info.st_size)
            if not dispatches or not qoder_runner_alive(int(process[1])):
                continue
            for session, tool, name in dispatches:
                pending.setdefault(session, {})[tool] = name
        except OSError:
            continue
    waiting = {}
    for session, tools in pending.items():
        requests, settled, finished = set(), set(), set()
        try:
            segments = sorted((logs / "sessions").glob(f"*/{session}/segments/*.jsonl"))
            for segment in segments[-8:]:
                info = segment.stat()
                for kind, turn, tool in qoder_segment_permission_events(segment, info.st_mtime_ns, info.st_size):
                    if kind == "turn.finished":
                        finished.add(turn)
                    elif tool in tools:
                        if kind == "tool.requested":
                            requests.add((turn, tool))
                        else:
                            settled.add((turn, tool))
        except OSError:
            continue
        open_tools = [tools[tool] for turn, tool in requests - settled if turn not in finished]
        if open_tools:
            names = sorted({name for name in open_tools if name})
            if len(names) == 1 and all(name in {"Bash", "Shell", "Terminal"} for name in open_tools):
                waiting[session] = "等待批准执行命令（" + names[0] + "）。"
            elif names and all(open_tools):
                waiting[session] = "等待确认工具操作（" + "、".join(names[:3]) + ("等" if len(names) > 3 else "") + "）。"
            else:
                waiting[session] = "等待确认工具操作。"
    return waiting


def _scan_qoder_cn_jsonl():
    now = time.time()
    out = []
    root = QODER_CN_PROJECTS_DIR
    if not os.path.isdir(root):
        return out
    waiting = qoder_waiting_sessions(root, now)
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        parts = f.replace("\\", "/").split("/")
        if "subagents" in parts or os.path.basename(f).startswith("agent-"):
            continue
        # IDE 精简转录目录 transcript/ 也扫；桌面富转录在项目根
        mtime = os.path.getmtime(f)
        if now - mtime > SCAN_AGE:
            continue
        cached = _file_cache["qoder-cn"].get(f)
        if cached and cached[0] == mtime:
            facts = cached[1]
        else:
            parsed = parse_qoder_cn_file(f)
            facts = parsed[0] if parsed else None
            _file_cache["qoder-cn"][f] = (mtime, facts)
        if not facts:
            continue
        session_id = facts["session_id"]
        pending_approval = session_id in waiting
        if pending_approval:
            status = "idle"
        elif facts.get("state_status") == "running":
            status = demote_if_zombie("running", mtime, zombie_threshold(facts["ev_ts"]), now=now)
        elif facts.get("state_status") in ("done", "idle"):
            status = facts["state_status"]
        elif facts["last_role"] == "user" or facts["stop_reason"] == "tool_use":
            status = demote_if_zombie("running", mtime, zombie_threshold(facts["ev_ts"]), now=now)
        elif facts["last_role"] == "assistant":
            status = "done"
        else:
            status = "idle"
        turns = facts.get("turns") or [facts]
        for index, turn in enumerate(reversed(turns)):
            current_turn = index == 0
            turn_status = status if current_turn else "done"
            turn_display = turn_status
            if pending_approval and current_turn:
                turn_display = "waiting"
            elif turn_status == "idle" and current_turn and facts.get("state_status") != "idle" and (
                facts.get("state_status") == "running" or facts["last_role"] == "user" or facts["stop_reason"] == "tool_use"
            ):
                turn_display = "unknown"
            reply_text = turn.get("reply_text", "") if turn_display == "done" and turn.get("last_stop_reason", facts["stop_reason"]) != "tool_use" else ""
            out.append({
                "agent": "qoder-cn", "title": turn["title"], "full_title": turn["user_prompt"],
                "user_prompt": turn["user_prompt"], "cwd": facts["cwd"],
                "status": turn_status, "mtime": mtime - index * 0.001,
                "link": qoder_session_link(session_id),
                "session_key": f + "#" + turn.get("turn_key", "legacy"),
                "turn_key": turn.get("turn_key", "legacy"), "display_status": turn_display,
                "status_reason": waiting.get(session_id, "") if current_turn else "",
                "recent": format_conclusion(reply_text or turn.get("recent", "")) if turn_display == "done" else turn.get("recent", ""),
                "recent_at": "", "reply_text": reply_text,
            })
    return out


def _table_columns(con, table):
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _scan_qoder_cn_sqlite():
    """旧版 / IDE SharedClientCache local.db：chat_session + chat_message。"""
    now = time.time()
    out = []
    for db in QODER_CN_DB_PATHS:
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except Exception as exc:
            record_scan_error("qoder-cn", exc)
            continue
        cols = _table_columns(con, "chat_session")
        msg_cols = _table_columns(con, "chat_message")
        if not cols or "session_id" not in cols and "id" not in cols:
            con.close()
            continue
        sid_col = "session_id" if "session_id" in cols else "id"
        title_col = next((c for c in ("title", "name", "session_name", "topic") if c in cols), None)
        project_col = next((c for c in ("project_path", "project_name", "cwd", "workspace") if c in cols), None)
        time_col = next((c for c in ("gmt_modified", "updated_at", "gmt_create", "created_at") if c in cols), None)
        select = [sid_col]
        if title_col:
            select.append(title_col)
        if project_col:
            select.append(project_col)
        if time_col:
            select.append(time_col)
        try:
            sessions = con.execute(f"SELECT {', '.join(select)} FROM chat_session").fetchall()
        except Exception as exc:
            record_scan_error("qoder-cn", exc)
            con.close()
            continue
        for row in sessions:
            data = dict(zip(select, row))
            sid = data[sid_col]
            title = first_text(data.get(title_col) or "") if title_col else ""
            cwd = str(data.get(project_col) or "") if project_col else ""
            mtime = _epoch_from_ms(data.get(time_col)) if time_col else 0.0
            recent, last_role = "", ""
            reply_text = ""
            if msg_cols and "session_id" in msg_cols:
                try:
                    msgs = con.execute(
                        "SELECT role, content, gmt_create FROM chat_message "
                        "WHERE session_id=? ORDER BY gmt_create DESC LIMIT 8",
                        (sid,),
                    ).fetchall()
                except Exception:
                    msgs = []
                for role, content, gmt in msgs:
                    if not mtime:
                        mtime = _epoch_from_ms(gmt)
                    if isinstance(content, str):
                        text = first_text(clean_title(content) or content)
                    else:
                        text = first_text(_message_text(content))
                    if not last_role and role in ("user", "assistant"):
                        last_role = role
                        if role == "assistant":
                            reply_text = assistant_text(content)
                    if text and not recent:
                        recent = text
                    if role == "user" and text and not title:
                        title = text
                    if title and recent and last_role:
                        break
            if not title:
                continue
            if mtime and now - mtime > SCAN_AGE:
                continue
            if last_role == "user":
                status = demote_if_zombie("running", mtime or now, ZOMBIE_DEFAULT, now=now)
            elif last_role == "assistant":
                status = "done"
            else:
                status = "idle"
            out.append({
                "agent": "qoder-cn", "title": title, "cwd": cwd,
                "status": status, "mtime": mtime or now, "link": qoder_session_link(sid),
                "session_key": str(sid),
                "display_status": "unknown" if status == "idle" and last_role == "user" else status,
                "recent": recent if recent != title else "", "recent_at": "",
                "reply_text": reply_text if status == "done" else "",
            })
        con.close()
    return out


def scan_qoder_cn():
    """Qoder CN：优先 ~/.qoder-cn/projects JSONL；无转录时退回 SharedClientCache SQLite。"""
    tasks_jsonl = _scan_qoder_cn_jsonl()
    if tasks_jsonl:
        return tasks_jsonl
    return _scan_qoder_cn_sqlite()


def normalize_workbuddy_dir(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError("WorkBuddy 数据目录无效")
    value = value.strip()
    if not value:
        return ""
    on_windows = RUNTIME.id == "windows"
    path = ntpath if on_windows else posixpath
    if value == "~":
        value = RUNTIME.home
    elif value.startswith("~/") or (on_windows and value.startswith("~\\")):
        value = path.join(RUNTIME.home, value[2:])
    if on_windows:
        absolute = re.match(r"^[a-zA-Z]:[/\\]", value) is not None
    else:
        absolute = path.isabs(value)
    if not absolute or not RUNTIME.workbuddy_db_paths:
        raise ValueError("请输入本机 WorkBuddy 数据目录的绝对路径")
    return path.normpath(value)


@lru_cache(maxsize=256)
def workbuddy_transcript_facts(path, session_id, version, size):
    limit = 256_000
    with path.open("rb") as transcript:
        offset = max(0, size - limit)
        transcript.seek(offset)
        tail = transcript.read(limit)
    if offset:
        tail = tail.partition(b"\n")[2]
    reply_text, questions = "", {}
    for line in tail.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("sessionId") != session_id:
            continue
        kind, call_id = event.get("type"), event.get("callId")
        if isinstance(call_id, str):
            if kind == "function_call_result":
                questions.pop(call_id, None)
            elif kind == "function_call" and event.get("name") == "AskUserQuestion":
                arguments = event.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (ValueError, RecursionError):
                        arguments = None
                items = arguments.get("questions") if isinstance(arguments, dict) else None
                text = next((item["question"] for item in items if isinstance(item, dict)
                             and isinstance(item.get("question"), str) and item["question"].strip()), "") if isinstance(items, list) else ""
                questions[call_id] = compress_text(text)
        if kind != "message":
            continue
        if event.get("role") == "user":
            reply_text = ""
            questions.clear()
            continue
        blocks = event.get("content")
        if event.get("role") != "assistant" or not isinstance(blocks, list):
            continue
        text = "\n".join(block["text"] for block in blocks if isinstance(block, dict)
                         and block.get("type") == "output_text" and isinstance(block.get("text"), str))
        reply_text = text.strip() or reply_text
    return reply_text, bool(questions), next(reversed(questions.values()), "")


def workbuddy_turns(data_dir, session_id):
    candidates = list(Path(data_dir).glob(f"projects/*/{session_id}.jsonl"))
    if not candidates:
        return []
    try:
        records = []
        for line in candidates[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            event = json.loads(line)
            if event.get("sessionId") == session_id:
                records.append(event)
    except (OSError, ValueError):
        return []
    turns, current = [], None
    for event in records:
        if event.get("role") == "user":
            prompt = user_prompt_text("\n".join(block.get("text", "") for block in event.get("content", [])
                                               if isinstance(block, dict)))
            if prompt:
                if current:
                    turns.append(current)
                current = {"user_prompt": prompt, "title": first_text(prompt), "reply_text": "",
                           "turn_key": f"turn-{len(turns) + 1}"}
        elif event.get("role") == "assistant" and current:
            text = "\n".join(block.get("text", "") for block in event.get("content", [])
                              if isinstance(block, dict) and block.get("type") == "output_text")
            if text:
                current["reply_text"] = text.strip()
    if current:
        turns.append(current)
    return turns


def workbuddy_session_link(session_id):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", session_id) and ".." not in session_id:
        return "workbuddy://chat/" + session_id
    return ""


def workbuddy_recent_facts(data_dir, session_id):
    missing = "", False, ""
    if not workbuddy_session_link(session_id):
        return missing
    try:
        candidates = []
        projects = data_dir / "projects"
        if projects.is_symlink():
            return missing
        for path in projects.glob(f"*/{session_id}.jsonl"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not path.parent.is_symlink():
                candidates.append((info.st_mtime_ns, info.st_size, path))
        if candidates:
            stamp, size, path = max(candidates)
            return workbuddy_transcript_facts(path, session_id, stamp, size)
    except OSError:
        return missing
    return missing


def read_workbuddy_sessions(db_path, now):
    if not stat.S_ISREG(db_path.stat().st_mode):
        raise FileNotFoundError(db_path)
    with db_path.open("rb"):
        pass
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("PRAGMA trusted_schema = OFF")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sessions' AND type = 'table'").fetchone():
            return None
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        clocks = [name for name in ("last_activity_at", "updated_at", "created_at") if name in columns]
        if not {"id", "title", "status"} <= columns or not clocks:
            return None
        optional = [name if name in columns else f"NULL AS {name}" for name in ("custom_title", "cwd")]
        timestamps = []
        for name in clocks:
            value = f"COALESCE(CAST({name} AS REAL), 0)"
            timestamps.append(f"CASE WHEN {value} >= 100000000000 THEN {value} / 1000.0 ELSE {value} END")
        activity = "MAX(0, " + ", ".join(timestamps) + ")"
        filters = ["id IS NOT NULL", "LOWER(TRIM(COALESCE(status, ''))) != 'archived'", f"{activity} >= ?"]
        if "deleted_at" in columns:
            filters.append("deleted_at IS NULL")
        query = f"SELECT id, title, status, {', '.join(optional)}, {activity} AS activity FROM sessions WHERE {' AND '.join(filters)} ORDER BY activity DESC"
        rows = db.execute(query, (now - SCAN_AGE,)).fetchall()
    tasks = []
    for row in rows:
        if not isinstance(row["id"], str) or not row["id"].strip() or not isinstance(row["activity"], (int, float)):
            continue
        source_status = str(row["status"] or "").strip().lower()
        reply_text, has_question, question = workbuddy_recent_facts(db_path.parent, row["id"])
        summary = format_conclusion(reply_text)
        turns = workbuddy_turns(db_path.parent, row["id"])
        if not turns:
            turns = [{"user_prompt": str(row["custom_title"] or row["title"] or "WorkBuddy 会话"),
                      "title": first_text(row["custom_title"] or row["title"] or "WorkBuddy 会话"),
                      "reply_text": reply_text, "turn_key": "legacy"}]
        display, reason = {
            "working": ("running", "WorkBuddy 已记录任务执行中。"),
            "planning": ("running", "WorkBuddy 已记录任务正在规划。"),
            "streaming": ("running", "WorkBuddy 已记录任务正在生成回复。"),
            "completed": ("done", "WorkBuddy 已记录当前回合结束。"),
            "failed": ("failed", "WorkBuddy 已记录执行失败。"),
            "error": ("failed", "WorkBuddy 已记录执行错误。"),
            "terminated": ("idle", "WorkBuddy 已记录会话终止，不视为完成或失败。"),
        }.get(source_status, ("unknown", "WorkBuddy 未提供可确认的回合状态，请在应用内查看。"))
        if source_status == "waiting_permission":
            display, reason = "waiting", "WorkBuddy 正在等待你确认工具操作。"
            summary = "等待确认工具操作"
        elif source_status in ("pending", "waiting_question") and has_question:
            display, reason = "waiting", "WorkBuddy 正在等待你回答问题。"
            if question:
                reason += " " + question
            summary = question or "等待你回答问题"
        for index, turn in enumerate(reversed(turns)):
            current_turn = index == 0
            turn_display = display if current_turn else "done"
            turn_key = turn["turn_key"]
            session_key = row["id"] if turn_key == "legacy" else f"{row['id']}#{turn_key}"
            task = {
                "agent": "workbuddy", "session_key": session_key,
                "turn_key": turn_key, "title": turn["title"], "full_title": turn["user_prompt"],
                "user_prompt": turn["user_prompt"], "cwd": row["cwd"] or "",
                "mtime": row["activity"] - index * 0.001,
                "status": turn_display if turn_display in TASK_STATUSES else "idle",
                "display_status": turn_display, "status_reason": reason if current_turn else "",
                "link": workbuddy_session_link(row["id"]),
                "recent": format_conclusion(turn.get("reply_text") or summary) if turn_display == "done" else summary,
                "reply_text": turn.get("reply_text", "") if turn_display == "done" else "",
            }
            if turn_display == "running":
                set_execution_status(task, row["activity"], ZOMBIE_DEFAULT, now=now)
            tasks.append(task)
    return tasks


def scan_workbuddy():
    if not RUNTIME.workbuddy_db_paths:
        return []
    state = "not_found"
    try:
        override = normalize_workbuddy_dir(load_config().get("workbuddy_data_dir", ""))
    except ValueError:
        _diagnostics["workbuddy"] = {"state": "invalid_path", "count": 0, "checked_at": time.time()}
        return []
    candidates = (Path(override) / "workbuddy.db",) if override else map(Path, RUNTIME.workbuddy_db_paths)
    for path in candidates:
        try:
            tasks = read_workbuddy_sessions(path, time.time())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except PermissionError:
            state = "permission_denied"
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xff
            if code in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT):
                state = "incompatible"
            elif code in (sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH, sqlite3.SQLITE_READONLY):
                state = "permission_denied"
            else:
                state = "unreadable"
        except OSError:
            state = "unreadable"
        else:
            if tasks is not None:
                state = "ok" if tasks else "empty"
                break
            state = "incompatible"
    else:
        tasks = []
    _diagnostics["workbuddy"] = {"state": state, "count": len(tasks), "checked_at": time.time()}
    return tasks


# 扫描器注册表：新增渠道只需 append，不必改 tasks()/task_stats() 拼接逻辑
# 存函数名而非函数对象，便于单测 mock.patch.object(server, "scan_codex", ...) 生效
SCANNERS = [
    ("codex", "scan_codex"),
    ("claude", "scan_claude"),
    ("apps", "scan_apps"),
    ("deepseek", "scan_dsh"),
    ("cursor", "scan_cursor"),
    ("qoder-cn", "scan_qoder_cn"),
    ("workbuddy", "scan_workbuddy"),
]


def collect_all_tasks():
    """按注册表聚合；单个扫描器异常不影响其它渠道。"""
    # 用 globals() 解析，兼容测试里 spec_from_file_location 加载（__name__ 未必在 sys.modules）
    g = globals()
    all_t = []
    _scan_errors.clear()
    for name, attr in SCANNERS:
        _diagnostics.pop(name, None)
        try:
            fn = g[attr]
            found = fn() or []
            all_t.extend(found)
            _diagnostics.setdefault(name, {"state": "ok" if found else "empty", "count": len(found), "checked_at": time.time()})
        except Exception as exc:
            _diagnostics[name] = {"state": "error", "count": 0, "checked_at": time.time(), "error": type(exc).__name__}
            continue
    all_t = [t for t in (normalize_task(task) for task in all_t) if t is not None]
    # 跨扫描器再去一次占位：如 Codex.app 在线占位 vs scan_codex 真实会话
    all_t = _drop_online_placeholders(all_t)
    return sorted(all_t, key=lambda task: -task["mtime"])


def _merge_codex_segments(tasks):
    sessions, result = {}, []
    for task in tasks:
        if task["agent"] == "codex" and re.fullmatch(rf"codex://threads/{CODEX_THREAD_ID}", task["link"]):
            sessions.setdefault((task["link"], task.get("turn_key") or "legacy"), []).append(task)
        else:
            result.append(task)
    for (link, turn_key), segments in sessions.items():
        ordered = sorted(segments, key=lambda task: (
            parse_ts(task.get("last_ev")) or task["mtime"], task["mtime"], task.get("session_key", "")
        ), reverse=True)
        current = ordered[0]
        cwd = next((task["cwd"] for task in ordered if task["cwd"]), "")
        result.append({**current, "task_id": task_id_for("codex", link, str(turn_key)), "cwd": cwd})
    return sorted(result, key=lambda task: -task["mtime"])


def _load_timeline():
    global _timeline
    if _timeline is not None:
        return _timeline
    try:
        with open(TIMELINE_PATH, encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
        _timeline = data if isinstance(data, list) else []
    except Exception:
        _timeline = []
    return _timeline


def _save_timeline():
    try:
        with open(TIMELINE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_timeline[-TIMELINE_MAX:], fh, ensure_ascii=False)
    except OSError:
        pass


def _repair_codex_timeline_links(tasks):
    links = {t["task_id"]: t["link"] for t in tasks if t["agent"] == "codex" and t["link"].startswith("codex://threads/")}
    changed = False
    if links:
        for event in _load_timeline():
            if event.get("agent") == "codex" and event.get("link") == "codex://":
                link = links.get(event.get("task_id"))
                if link:
                    event["link"] = link
                    changed = True
    if changed:
        _save_timeline()


def _append_timeline(events):
    if not events:
        return
    tl = _load_timeline()
    now = time.time()
    for e in events:
        tl.append({
            "ts": now,
            "task_id": e.get("task_id", ""),
            "agent": e.get("agent", ""),
            "title": e.get("title", ""),
            "kind": e.get("kind", "completed"),
            "recent": e.get("recent", ""),
            "cwd": e.get("cwd", ""),
            "link": e.get("link", ""),
            **({"reply_text": e["reply_text"]} if e.get("reply_text") else {}),
            **({"handoff": e["handoff"]} if e.get("handoff") else {}),
        })
    del tl[:-TIMELINE_MAX]
    _save_timeline()


def timeline_today(day=None):
    """今日跨渠道时间线（按时间倒序）。"""
    tl = _load_timeline()
    date = datetime.date.fromisoformat(day) if day else datetime.date.today()
    start = datetime.datetime.combine(date, datetime.time.min).timestamp()
    end = datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time.min).timestamp()
    items = [e for e in tl if start <= float(e.get("ts") or 0) < end]
    items.sort(key=lambda x: -float(x.get("ts") or 0))
    return {"api_version": API_VERSION, "items": items}


def _cached_tasks(now):
    if now - _cache["mtime"] >= CACHE_TTL_SECS:
        scanned = collect_all_tasks()
        _repair_codex_timeline_links(scanned)
        _cache.update(data=_merge_codex_segments(scanned), mtime=now)
    return _cache["data"]


def tasks(consume_finished=True):
    """聚合任务列表。

    consume_finished=True（默认）：推进完成状态机并返回 finished，供原生通知消费。
    consume_finished=False：只刷新/返回列表，不碰 _prev_tasks / _pending_finish，
    给面板 WebView 用，避免和原生轮询抢 finished。
    """
    now = time.time()
    data = _cached_tasks(now)
    hidden = set(load_config().get("hidden", []))
    visible = [t for t in data if t["agent"] not in hidden]
    if not consume_finished:
        return {"api_version": API_VERSION, "tasks": visible, "finished": [], "heartbeats": [], "notifications": notification_preferences()}

    # 状态对比用全量数据（隐藏渠道也该发完成通知）
    now_tasks = {t["task_id"]: t for t in data}

    # 维护 running 时长，供心跳
    for task_id, current in list(_running_since.items()):
        cur = now_tasks.get(task_id)
        if not cur or cur["status"] != "running":
            _running_since.pop(task_id, None)
            _heartbeat_sent.pop(task_id, None)
    for task_id, current in now_tasks.items():
        if current["status"] == "running":
            _running_since.setdefault(task_id, now)

    # 已回到执行中或任务消失 → 取消待确认终态（回合间隙抖动）
    for task_id in list(_pending_finish):
        current = now_tasks.get(task_id)
        if not current or current["status"] == "running":
            _pending_finish.pop(task_id, None)

    # running → 终态：先进入待确认，不立刻通知
    for task_id, previous in _prev_tasks.items():
        current = now_tasks.get(task_id)
        if previous["status"] != "running" or not current:
            continue
        kind = _finish_kind(current.get("display_status", current["status"]))
        if not kind or task_id in _pending_finish:
            continue
        _pending_finish[task_id] = {
            "kind": kind,
            "agent": current["agent"],
            "title": current["title"],
            "hits": 0,
        }

    # 连续命中终态才下发；kind 随当前状态刷新（done/idle/failed）
    finished = []
    for task_id, pend in list(_pending_finish.items()):
        current = now_tasks.get(task_id)
        if not current:
            _pending_finish.pop(task_id, None)
            continue
        kind = _finish_kind(current.get("display_status", current["status"]))
        if not kind:
            _pending_finish.pop(task_id, None)
            continue
        pend["kind"] = kind
        pend["agent"] = current["agent"]
        pend["title"] = current["title"]
        pend["hits"] += 1
        # Cursor 完成：多等几轮，Thinking 间隙恢复 running 时可取消；堵塞/失败仍用短确认
        need_hits = (
            CURSOR_COMPLETED_CONFIRM_HITS
            if current["agent"] == "cursor" and kind == "completed"
            else FINISH_CONFIRM_HITS
        )
        if pend["hits"] < need_hits:
            continue
        if current["agent"] not in hidden:
            finished.append({
                "task_id": task_id,
                "agent": current["agent"],
                "title": current["title"],
                "kind": kind,
                # 摘要卡用：最近进展 / 工程路径 / 跳转，便于一眼看到「做成啥样了」
                "recent": str(current.get("recent") or ""),
                "cwd": str(current.get("cwd") or ""),
                "link": str(current.get("link") or ""),
                "revision": str(current.get("complete_at") or current.get("recent_at") or current.get("mtime") or ""),
                **({"reply_text": current["reply_text"]} if kind == "completed" and current.get("reply_text") else {}),
                **({"handoff": current["handoff"]} if kind == "completed" and current.get("handoff") else {}),
            })
        _pending_finish.pop(task_id, None)

    _append_timeline(finished)

    # 长任务心跳：仍在跑且超过 HEARTBEAT_SECS，按间隔下发
    heartbeats = []
    for task_id, current in now_tasks.items():
        if current["status"] != "running" or current["agent"] in hidden:
            continue
        since = _running_since.get(task_id, now)
        if now - since < HEARTBEAT_SECS:
            continue
        last_hb = _heartbeat_sent.get(task_id, since)
        if now - last_hb < HEARTBEAT_SECS:
            continue
        _heartbeat_sent[task_id] = now
        mins = int((now - since) / 60)
        heartbeats.append({
            "task_id": task_id,
            "agent": current["agent"],
            "title": current["title"],
            "kind": "heartbeat",
            "recent": str(current.get("recent") or "") or f"已连续执行约 {mins} 分钟",
            "cwd": str(current.get("cwd") or ""),
            "link": str(current.get("link") or ""),
            "running_mins": mins,
            "revision": str(current.get("recent_at") or current.get("mtime") or ""),
        })

    _prev_tasks.clear()
    _prev_tasks.update({t["task_id"]: t for t in data})
    return {
        "api_version": API_VERSION,
        "tasks": visible,
        "finished": finished,
        "heartbeats": heartbeats,
        "notifications": notification_preferences(),
    }


def _finish_kind(status):
    if status == "done":
        return "completed"
    if status == "failed":
        return "failed"
    if status == "waiting":
        return "blocked"
    return None


def task_stats():
    """轻量统计：刷新任务缓存，但不推进 _prev_tasks / finished，避免菜单栏轮询吞掉通知。"""
    data = _cached_tasks(time.time())
    hidden = set(load_config().get("hidden", []))
    visible = [t for t in data if t["agent"] not in hidden]
    running = sum(
        1 for t in visible
        if t["status"] == "running"
        and not str(t.get("recent") or "").startswith("应用在线")
    )
    return {"api_version": API_VERSION, "running": running}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            with open(HTML_PATH, "rb") as fh:
                self._send(200, fh.read().decode("utf-8"), ctype="text/html; charset=utf-8")
        elif path == "/api/tasks":
            qs = urlparse(self.path).query
            # view=1：面板只读列表，不消费 finished（由原生菜单栏轮询负责通知）
            view_only = any(p in ("view=1", "view=true") for p in qs.split("&") if p)
            result = tasks(consume_finished=not view_only)
            self._send(200, json.dumps({**result, "pin": os.path.exists(PIN_FLAG)}))
        elif path == "/api/stats":
            self._send(200, json.dumps(task_stats()))
        elif path == "/api/timeline":
            try:
                day = parse_qs(urlparse(self.path).query).get("date", [None])[0]
                self._send(200, json.dumps(timeline_today(day)))
            except ValueError:
                self._send(400, json.dumps({"error": "日期格式应为 YYYY-MM-DD"}))
        elif path == "/api/diagnostics":
            task_stats()
            self._send(200, json.dumps({
                "api_version": API_VERSION,
                "channels": channel_diagnostics(),
            }))
        elif path == "/api/config":
            self._send(200, json.dumps({
                "api_version": API_VERSION,
                "agents": ALL_AGENTS,
                "scanners": [name for name, _ in SCANNERS],
                **load_config(),
                "notifications": notification_preferences(),
            }))
        elif path == "/api/health":
            self._send(200, json.dumps({
                "api_version": API_VERSION,
                "platform": RUNTIME.id,
                "pid": os.getpid(),
                "build": BUILD_ID,
            }))
        else:
            self._send(404, "{}")
    def do_POST(self):
        if urlparse(self.path).path == "/api/pin":
            if os.path.exists(PIN_FLAG):
                os.remove(PIN_FLAG)
            else:
                open(PIN_FLAG, "w").close()
            self._send(200, json.dumps({"ok": True}))
        elif urlparse(self.path).path == "/api/config":
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                save_config(d)
                self._send(200, json.dumps({"ok": True}))
            except json.JSONDecodeError:
                self._send(400, json.dumps({"ok": False, "error": "设置格式无效"}))
            except ValueError as exc:
                self._send(400, json.dumps({"ok": False, "error": str(exc)}))
            except TypeError:
                self._send(400, json.dumps({"ok": False, "error": "设置格式无效"}))
            except OSError:
                self._send(500, json.dumps({"ok": False, "error": "无法保存设置，请检查配置文件权限"}))
        else:
            self._send(404, "{}")

if __name__ == "__main__":
    BUILD_ID = build_id(os.path.abspath(__file__))
    if "--build" in sys.argv:
        try:
            BUILD_ID = sys.argv[sys.argv.index("--build") + 1] or BUILD_ID
        except IndexError:
            pass
    try:
        with open(PID_PATH, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
