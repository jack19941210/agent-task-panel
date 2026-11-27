import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from platforms import detect_runtime


class PlatformRuntimeTests(unittest.TestCase):
    def test_macos_paths_and_native_apps(self):
        runtime = detect_runtime(system="Darwin", home="/Users/example")

        self.assertEqual(runtime.id, "macos")
        self.assertTrue(runtime.supports_native_apps)
        self.assertEqual(runtime.codex_dir, "/Users/example/.codex/sessions")
        self.assertEqual(runtime.config_path, "/Users/example/.agent-panel-config.json")
        self.assertEqual(
            runtime.cursor_state_db,
            "/Users/example/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        )
        self.assertEqual(runtime.qoder_cn_projects_dir, "/Users/example/.qoder-cn/projects")
        self.assertEqual(runtime.workbuddy_db_paths, ("/Users/example/.workbuddy/workbuddy.db",))
        self.assertEqual(
            runtime.qoder_cn_db_paths[1],
            "/Users/example/Library/Application Support/QoderCN/SharedClientCache/cache/db/local.db",
        )

    def test_linux_paths_disable_native_apps(self):
        runtime = detect_runtime(system="Linux", home="/home/example")

        self.assertEqual(runtime.id, "linux")
        self.assertFalse(runtime.supports_native_apps)
        self.assertEqual(runtime.workbuddy_db_paths, ())
        self.assertEqual(runtime.claude_dir, "/home/example/.claude/projects")
        self.assertEqual(runtime.dsh_dir, "/home/example/.dsh/sessions")
        self.assertEqual(
            runtime.cursor_state_db,
            "/home/example/.config/Cursor/User/globalStorage/state.vscdb",
        )
        self.assertEqual(
            runtime.qoder_cn_db_paths[0],
            "/home/example/.qoder-cn/shared_client/cache/db/local.db",
        )

    def test_windows_uses_userprofile(self):
        runtime = detect_runtime(
            system="Windows",
            env={"HOME": r"D:\ShellHome", "USERPROFILE": r"C:\Users\example", "APPDATA": r"D:\Roaming", "LOCALAPPDATA": r"E:\Local"},
        )

        self.assertEqual(runtime.id, "windows")
        self.assertFalse(runtime.supports_native_apps)
        self.assertEqual(
            runtime.codex_dir,
            r"C:\Users\example\.codex\sessions",
        )
        self.assertEqual(
            runtime.cursor_state_db,
            r"D:\Roaming\Cursor\User\globalStorage\state.vscdb",
        )
        self.assertEqual(runtime.workbuddy_db_paths, (
            r"C:\Users\example\.workbuddy\workbuddy.db",
            r"D:\Roaming\WorkBuddy\workbuddy.db",
            r"E:\Local\WorkBuddy\workbuddy.db",
        ))

    def test_windows_workbuddy_defaults_without_appdata(self):
        runtime = detect_runtime(system="Windows", home=r"C:\Users\新 用户", env={})
        self.assertEqual(runtime.workbuddy_db_paths, (
            r"C:\Users\新 用户\.workbuddy\workbuddy.db",
            r"C:\Users\新 用户\AppData\Roaming\WorkBuddy\workbuddy.db",
            r"C:\Users\新 用户\AppData\Local\WorkBuddy\workbuddy.db",
        ))

    def test_qodercn_config_dir_override(self):
        runtime = detect_runtime(
            system="Darwin",
            home="/Users/example",
            env={"QODERCN_CONFIG_DIR": "/custom/qoder-cn"},
        )
        self.assertEqual(runtime.qoder_cn_projects_dir, "/custom/qoder-cn/projects")
        self.assertEqual(
            runtime.qoder_cn_db_paths[0],
            "/custom/qoder-cn/shared_client/cache/db/local.db",
        )


if __name__ == "__main__":
    unittest.main()
