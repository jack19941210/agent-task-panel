import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("agent_panel_server", ROOT / "src/server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

NOW = 1_780_000_000


class WorkBuddyScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.db = self.home / ".workbuddy" / "workbuddy.db"
        runtime = server.detect_runtime(home=str(self.home), env={})
        self.runtime = replace(runtime, workbuddy_db_paths=(str(self.db),))
        self.config = self.home / "panel-config.json"
        for patch in (
            mock.patch.object(server, "RUNTIME", self.runtime),
            mock.patch.object(server, "CONFIG_PATH", str(self.config)),
            mock.patch.object(server.time, "time", return_value=NOW),
            mock.patch.object(server, "_diagnostics", {}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def create_db(self, path=None, rows=(), wal=False):
        path = self.db if path is None else path
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        if wal:
            db.execute("PRAGMA journal_mode = WAL")
        db.execute("CREATE TABLE sessions (id TEXT, title TEXT, custom_title TEXT, cwd TEXT, status TEXT, "
                   "updated_at INTEGER, last_activity_at INTEGER, deleted_at INTEGER, is_playground INTEGER)")
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        return db

    def row(self, session_id, status="Completed", updated=NOW - 5, activity=None, deleted=None, playground=0):
        return (session_id, "原始标题", "自定义标题", str(self.home), status, updated, activity, deleted, playground)

    def diagnostic(self):
        return next(a for a in server.channel_diagnostics() if a["id"] == "workbuddy")

    def write_transcript(self, session_id, records):
        folder = self.db.parent / "projects" / "workspace-slug"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
        )

    def assistant_reply(self, session_id, text, status="completed"):
        return {"type": "message", "role": "assistant", "sessionId": session_id,
                "status": status, "content": [{"type": "output_text", "text": text}]}

    def test_working_turn_with_old_activity_then_finishes(self):
        session_id = "same-session"
        db = self.create_db(rows=[self.row("same-session", "Working", activity=(NOW - 90000) * 1000)], wal=True)
        user = {"type": "message", "role": "user", "sessionId": session_id}
        self.write_transcript(session_id, [self.assistant_reply(session_id, "旧回合内容"), user])
        running = server.normalize_task(server.scan_workbuddy()[0])
        self.assertEqual(running["display_status"], "running")
        self.assertEqual(running["recent"], "")
        self.write_transcript(session_id, [user, self.assistant_reply(session_id, "正在整理建议", "incomplete")])
        self.assertEqual(server.scan_workbuddy()[0]["recent"], "正在整理建议")

        db.execute("UPDATE sessions SET status = 'completed', last_activity_at = ?", (NOW * 1000,))
        db.commit()
        reply = "最终建议\n\n可执行步骤与验收标准"
        self.write_transcript(session_id, [user, self.assistant_reply(session_id, reply),
                                           {"type": "reasoning", "role": "assistant", "sessionId": session_id,
                                            "content": [{"type": "output_text", "text": "不要展示推理"}]},
                                           {"type": "function_call_result", "output": "不要展示工具结果"},
                                           self.assistant_reply("different-session", "不要展示其他会话")])
        done = server.normalize_task(server.scan_workbuddy()[0])
        self.assertEqual(done["display_status"], "done")
        self.assertEqual(done["recent"], "最终建议")
        self.assertEqual(done["reply_text"], reply)
        self.assertEqual(done["task_id"], running["task_id"])

    def test_expired_execution_signal_never_claims_completion(self):
        self.create_db(rows=[self.row("expired", "working", updated=NOW-server.ZOMBIE_DEFAULT-1),
                             self.row("planning", "planning"), self.row("streaming", "streaming")])
        tasks = {task["session_key"]: task for task in server.scan_workbuddy()}
        self.assertEqual(tasks["expired"]["status"], "idle")
        self.assertEqual(tasks["expired"]["display_status"], "unknown")
        self.assertEqual(tasks["planning"]["display_status"], "running")
        self.assertEqual(tasks["streaming"]["display_status"], "running")

    def test_partial_transcript_line_does_not_hide_last_reply(self):
        self.create_db(rows=[self.row("partial")])
        self.write_transcript("partial", [self.assistant_reply("partial", "最近回复")])
        transcript = self.db.parent / "projects" / "workspace-slug" / "partial.jsonl"
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"message",')
        self.assertEqual(server.scan_workbuddy()[0]["recent"], "最近回复")

    def test_pending_question_has_summary_and_clears_when_answered(self):
        session_id = "question-session"
        db = self.create_db(rows=[self.row(session_id, "pending")])
        question = {"type": "function_call", "sessionId": session_id, "callId": "ask-one",
                    "name": "AskUserQuestion", "arguments": json.dumps({"questions": [
                        {"question": "请选择测试结果", "options": [{"label": "成功"}]}]})}
        records = [{"type": "message", "role": "user", "sessionId": session_id}, question]
        self.write_transcript(session_id, records)
        waiting = server.scan_workbuddy()[0]
        self.assertEqual(waiting["display_status"], "waiting")
        self.assertEqual(waiting["recent"], "请选择测试结果")
        self.assertIn("请选择测试结果", waiting["status_reason"])
        self.assertEqual(waiting["link"], "workbuddy://chat/question-session")
        result = {"type": "function_call_result", "sessionId": session_id, "callId": "ask-one",
                  "output": "用户答案不应作为摘要"}
        self.write_transcript(session_id, records + [result])
        answered = server.scan_workbuddy()[0]
        self.assertEqual(answered["display_status"], "unknown")
        self.assertEqual(answered["recent"], "")
        db.execute("UPDATE sessions SET status = 'completed'")
        db.commit()
        self.write_transcript(session_id, records + [self.assistant_reply(session_id, "测试已结束")])
        self.assertEqual(server.scan_workbuddy()[0]["recent"], "测试已结束")
        self.assertEqual(server.scan_workbuddy()[0]["display_status"], "done")

    def test_scan_wal_recent_sessions_and_conservative_status(self):
        self.create_db(rows=[
            self.row("done", activity=(NOW - 1) * 1000), self.row("error", "error"),
            self.row("failed", "Failed"), self.row("stopped", "Terminated"),
            self.row("unknown", "Pending"), self.row("unverified", "Running"),
            self.row("old", updated=NOW - server.SCAN_AGE - 1),
            self.row("archived", "Archived"), self.row("deleted", deleted=NOW),
            self.row("playground", playground=1),
        ], wal=True)
        before = self.db.read_bytes()
        tasks = [server.normalize_task(task) for task in server.scan_workbuddy()]
        self.assertEqual(tasks[0]["mtime"], NOW - 1)
        self.assertEqual(tasks[0]["full_title"], "自定义标题")
        self.assertEqual(tasks[0]["cwd"], str(self.home))
        self.assertEqual(tasks[0]["link"], "workbuddy://chat/done")
        self.assertEqual({task["session_key"]: task["display_status"] for task in tasks}, {
            "done": "done", "error": "failed", "failed": "failed", "stopped": "idle",
            "unknown": "unknown", "unverified": "unknown", "playground": "done",
        })
        self.assertEqual(len({task["task_id"] for task in tasks}), 7)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.diagnostic()["state"], "ok")

    def test_automatic_detection_continues_past_an_incompatible_candidate(self):
        self.db.parent.mkdir()
        self.db.write_bytes(b"not the WorkBuddy database")
        fallback = self.home / "AppData # 用户" / "WorkBuddy" / "workbuddy.db"
        self.create_db(path=fallback, rows=[self.row("fallback")])
        runtime = replace(self.runtime, workbuddy_db_paths=(str(self.db), str(fallback)))
        with mock.patch.object(server, "RUNTIME", runtime):
            self.assertEqual(server.scan_workbuddy()[0]["session_key"], "fallback")

    @unittest.skipUnless(os.name == "nt", "requires Windows filesystem semantics")
    def test_windows_discovers_from_appdata_on_native_filesystem(self):
        roaming = self.home / "Roaming 用户 #1"
        runtime = server.detect_runtime(system="Windows", env={"USERPROFILE": str(self.home), "APPDATA": str(roaming)})
        self.create_db(path=roaming / "WorkBuddy" / "workbuddy.db", rows=[self.row("roaming")], wal=True)
        with mock.patch.object(server, "RUNTIME", runtime):
            self.assertEqual(server.scan_workbuddy()[0]["session_key"], "roaming")

    def test_explicit_directory_and_clear_switch_back_to_automatic(self):
        self.create_db(rows=[self.row("default")])
        custom = self.home / "自定义目录 #1"
        self.create_db(path=custom / "workbuddy.db", rows=[self.row("override")])
        server.save_config({"workbuddy_data_dir": str(custom), "hidden": ["cursor"]})
        self.assertEqual(server.scan_workbuddy()[0]["session_key"], "override")
        with self.assertRaises(ValueError):
            server.save_config({"workbuddy_data_dir": "relative/path"})
        self.assertEqual(server.load_config()["workbuddy_data_dir"], str(custom))
        server.save_config({"workbuddy_data_dir": str(custom / "wrong")})
        self.assertEqual(server.scan_workbuddy(), [])
        self.assertEqual(self.diagnostic()["state"], "not_found")
        server.save_config({"workbuddy_data_dir": ""})
        self.assertEqual(server.load_config(), {"hidden": ["cursor"]})
        self.assertEqual(server.scan_workbuddy()[0]["session_key"], "default")

    def test_distinct_diagnostics_survive_registry(self):
        with mock.patch.object(server, "SCANNERS", [("workbuddy", "scan_workbuddy")]):
            self.assertEqual(server.collect_all_tasks(), [])
            self.assertEqual(self.diagnostic()["state"], "not_found")
            self.assertFalse(self.db.exists())
            self.db.parent.mkdir()
            with closing(sqlite3.connect(self.db)) as db:
                db.execute("CREATE TABLE sessions (unrelated TEXT)")
            server.collect_all_tasks()
            self.assertEqual(self.diagnostic()["state"], "incompatible")
            with mock.patch.object(server, "read_workbuddy_sessions", side_effect=PermissionError):
                server.collect_all_tasks()
            self.assertEqual(self.diagnostic()["state"], "permission_denied")
            self.db.unlink()
            self.create_db(rows=[self.row("old", updated=NOW - server.SCAN_AGE - 1)])
            server.collect_all_tasks()
            self.assertEqual(self.diagnostic()["state"], "empty")

    def test_truthful_platform_support_and_windows_input(self):
        windows = server.detect_runtime(system="Windows", home=r"C:\Users\tester", env={})
        macos = server.detect_runtime(system="Darwin", home="/Users/tester", env={})
        linux = server.detect_runtime(system="Linux", home="/home/tester", env={})
        self.assertIn(server.WORKBUDDY_AGENT, server.all_agents(macos))
        self.assertIn(server.WORKBUDDY_AGENT, server.all_agents(windows))
        self.assertNotIn(server.WORKBUDDY_AGENT, server.all_agents(linux))
        with mock.patch.object(server, "RUNTIME", windows):
            self.assertEqual(server.normalize_workbuddy_dir(r"~\迁移数据"), r"C:\Users\tester\迁移数据")
            self.assertEqual(server.normalize_workbuddy_dir("D:/My Data"), r"D:\My Data")
            for invalid in (r"C:relative", r"\rooted", r"\\server\share", None):
                with self.assertRaises(ValueError):
                    server.normalize_workbuddy_dir(invalid)
        with mock.patch.object(server, "RUNTIME", linux), mock.patch.object(server, "read_workbuddy_sessions") as read:
            self.assertEqual(server.scan_workbuddy(), [])
            self.assertFalse(self.diagnostic()["supported"])
            read.assert_not_called()
