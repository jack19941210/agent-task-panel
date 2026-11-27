"""跨平台运行时路径与能力描述。"""
from dataclasses import dataclass
import os
import ntpath
import platform
import posixpath
from typing import Mapping, Optional


@dataclass(frozen=True)
class PlatformRuntime:
    id: str
    home: str
    codex_dir: str
    claude_dir: str
    dsh_dir: str
    cursor_state_db: str
    cursor_projects_dir: str
    qoder_cn_projects_dir: str
    qoder_cn_db_paths: tuple
    workbuddy_db_paths: tuple
    config_path: str
    pin_flag: str
    pid_path: str
    supports_native_apps: bool


def _platform_id(system: str) -> str:
    value = system.strip().lower()
    if value == "darwin":
        return "macos"
    if value == "windows":
        return "windows"
    return "linux"


def _app_support_root(platform_id: str, home: str, env: Mapping[str, str], path_mod) -> str:
    """各平台的应用数据根目录（Electron / VS Code 系常用位置）。"""
    if platform_id == "macos":
        return path_mod.join(home, "Library", "Application Support")
    if platform_id == "windows":
        appdata = env.get("APPDATA") or path_mod.join(home, "AppData", "Roaming")
        return appdata
    return path_mod.join(home, ".config")


def detect_runtime(
    system: Optional[str] = None,
    home: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> PlatformRuntime:
    """构建当前平台的路径和能力集合；参数可用于无副作用测试。"""
    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    platform_id = _platform_id(system)
    path = ntpath if platform_id == "windows" else posixpath
    home_keys = ("USERPROFILE", "HOME") if platform_id == "windows" else ("HOME", "USERPROFILE")
    home = os.path.expanduser(home or env.get(home_keys[0]) or env.get(home_keys[1]) or "~")
    app_support = _app_support_root(platform_id, home, env, path)
    qoder_cn_root = env.get("QODERCN_CONFIG_DIR") or path.join(home, ".qoder-cn")
    qoder_db_tail = ("SharedClientCache", "cache", "db", "local.db")
    workbuddy_paths = ()
    if platform_id in ("macos", "windows"):
        workbuddy_paths = (path.join(home, ".workbuddy", "workbuddy.db"),)
    if platform_id == "windows":
        local_appdata = env.get("LOCALAPPDATA") or path.join(home, "AppData", "Local")
        workbuddy_paths += tuple(
            path.join(root, "WorkBuddy", "workbuddy.db")
            for root in dict.fromkeys((app_support, local_appdata))
        )
    return PlatformRuntime(
        id=platform_id,
        home=home,
        codex_dir=path.join(home, ".codex", "sessions"),
        claude_dir=path.join(home, ".claude", "projects"),
        dsh_dir=path.join(home, ".dsh", "sessions"),
        cursor_state_db=path.join(
            app_support, "Cursor", "User", "globalStorage", "state.vscdb"
        ),
        cursor_projects_dir=path.join(home, ".cursor", "projects"),
        qoder_cn_projects_dir=path.join(qoder_cn_root, "projects"),
        # 按观测命中率排序：配置目录 shared_client → 独立 App 数据目录
        qoder_cn_db_paths=(
            path.join(qoder_cn_root, "shared_client", "cache", "db", "local.db"),
            path.join(app_support, "QoderCN", *qoder_db_tail),
            path.join(app_support, "Qoder CN", *qoder_db_tail),
        ),
        workbuddy_db_paths=workbuddy_paths,
        config_path=path.join(home, ".agent-panel-config.json"),
        pin_flag=path.join(home, ".agent-panel-pin"),
        pid_path=path.join(home, ".agent-panel-server.pid"),
        supports_native_apps=platform_id == "macos",
    )
