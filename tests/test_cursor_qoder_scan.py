import importlib.util
from contextlib import closing
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("agent_panel_server", ROOT / "src/server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)

T0 = 1_700_000_000.0


class CursorScanTests(unittest.TestCase):
    def setUp(self):
        server._file_cache["cursor"].clear()
        server._cursor_unfinished_seen.clear()
        server._cursor_pending_plan_at.clear()
        server._cursor_pending_plan_ran.clear()
        self.addCleanup(server._file_cache["cursor"].clear)
        self.addCleanup(server._cursor_unfinished_seen.clear)
        self.addCleanup(server._cursor_pending_plan_at.clear)
        self.addCleanup(server._cursor_pending_plan_ran.clear)

    def test_header_without_workspace_links_to_cursor_agents(self):
        with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(pathlib.Path(tmp) / "state.vscdb")) as db:
            db_path = pathlib.Path(tmp) / "state.vscdb"
            db.execute("CREATE TABLE composerHeaders (composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                       "isArchived INTEGER, isSubagent INTEGER, value TEXT)")
            session_id = "11111111-1111-4111-8111-111111111111"
            stamp = int(T0 * 1000)
            db.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                       (session_id, stamp, stamp, 0, 0, json.dumps({"name": "No workspace"})))
            db.commit()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db_path)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 60):
                task = server.scan_cursor()[0]
            self.assertEqual(task["cwd"], "")
            self.assertEqual(task["link"], f"cursor://anysphere.cursor-deeplink/agent?id={session_id}")

    def test_header_emits_each_user_turn_with_original_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "state.vscdb"
            projects = pathlib.Path(tmp) / "projects" / "Users-me-app" / "agent-transcripts" / "multi"
            projects.mkdir(parents=True)
            prompt_a = "中文任务 A：" + "请保留完整描述。" * 20
            prompt_b = "中文任务 B：继续检查第二项。"
            (projects / "multi.jsonl").write_text("\n".join([
                json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": prompt_a}]}}),
                json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "A done"}]}}),
                json.dumps({"type": "turn_ended", "status": "success"}),
                json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": prompt_b}]}}),
                json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "B done"}]}}),
                json.dumps({"type": "turn_ended", "status": "success"}),
            ]) + "\n", encoding="utf-8")
            with mock.patch.object(server, "CURSOR_STATE_DB", str(pathlib.Path(tmp) / "missing.db")), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(pathlib.Path(tmp) / "projects")), \
                 mock.patch.object(server.time, "time", return_value=T0 + 60):
                tasks = server.scan_cursor()
            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0]["user_prompt"], prompt_b)
            self.assertEqual(tasks[1]["user_prompt"], prompt_a)
            self.assertEqual(tasks[0]["status"], "done")
            self.assertEqual(tasks[1]["status"], "done")

    def test_pending_build_plan_is_waiting_after_turn_ends(self):
        with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(pathlib.Path(tmp) / "state.vscdb")) as db:
            db_path = pathlib.Path(tmp) / "state.vscdb"
            db.execute("CREATE TABLE composerHeaders (composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                       "isArchived INTEGER, isSubagent INTEGER, value TEXT)")
            stamp = int(T0 * 1000)
            header = {"name": "Review plan", "hasPendingPlan": True, "hasBlockingPendingActions": False}
            db.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)", ("plan", stamp, stamp, 0, 0, json.dumps(header)))
            db.commit()
            server._cursor_pending_plan_at.clear()
            server._cursor_pending_plan_ran.clear()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db_path)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 60), \
                 mock.patch.object(server, "_cursor_transcript_boundary", return_value="closed"):
                task = server.scan_cursor()[0]
                self.assertEqual(task["display_status"], "waiting")
                self.assertEqual(task["status"], "idle")
                self.assertIn("Build", task["status_reason"])
                header["unfinishedRunAt"] = stamp
                db.execute("UPDATE composerHeaders SET value = ?", (json.dumps(header),))
                db.commit()
                self.assertEqual(server.scan_cursor()[0]["display_status"], "running")
                header.pop("unfinishedRunAt")
                header["hasPendingPlan"] = False
                server._cursor_unfinished_seen.clear()
                db.execute("UPDATE composerHeaders SET value = ?", (json.dumps(header),))
                db.commit()
                self.assertEqual(server.scan_cursor()[0]["display_status"], "done")

    def test_pending_plan_clears_after_newer_completed_turn(self):
        """同会话继续提问并答完后，残留的 hasPendingPlan 不应再占待确认。"""
        with tempfile.TemporaryDirectory() as tmp:
            projects = pathlib.Path(tmp) / "projects"
            tid = "plan-follow"
            tdir = projects / "Users-me-app" / "agent-transcripts" / tid
            tdir.mkdir(parents=True)
            (tdir / f"{tid}.jsonl").write_text("\n".join([
                json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "做个计划"}]}}),
                json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "已生成计划确认卡，请点击 Build。"}]}}),
                json.dumps({"type": "turn_ended", "status": "success"}),
                json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "先继续测别的"}]}}),
                json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "已按计划执行：确认 = 继续测试。"}]}}),
                json.dumps({"type": "turn_ended", "status": "success"}),
            ]) + "\n", encoding="utf-8")
            db = pathlib.Path(tmp) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE composerHeaders (composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                        "isArchived INTEGER, isSubagent INTEGER, value TEXT)")
            header = {
                "name": "做个计划",
                "hasPendingPlan": True,
                "lastUpdatedAt": int((T0 + 50) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 50) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                        (tid, int(T0 * 1000), int((T0 + 50) * 1000), 0, 0, json.dumps(header)))
            con.commit()
            con.close()
            server._cursor_pending_plan_at.clear()
            server._cursor_pending_plan_ran.clear()
            server._file_cache["cursor"].clear()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 80):
                tasks = [server.normalize_task(t) for t in server.scan_cursor()]
            self.assertTrue(tasks)
            self.assertNotEqual(tasks[0]["display_status"], "waiting")
            self.assertFalse(any(t.get("display_status") == "waiting" for t in tasks))

    def test_older_turn_reply_needed_clears_when_session_continues(self):
        """同会话新回合开始后，旧回合的闸语句不再进入待回复。"""
        with tempfile.TemporaryDirectory() as tmp:
            projects = pathlib.Path(tmp) / "projects"
            tid = "reply-follow"
            tdir = projects / "Users-me-app" / "agent-transcripts" / tid
            tdir.mkdir(parents=True)
            (tdir / f"{tid}.jsonl").write_text("\n".join([
                json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "先问一句"}]}}),
                json.dumps({"role": "assistant", "message": {"content": [
                    {"type": "text", "text": "请选择 A/B。\n你回复 A/B 后我再继续。"}]}}),
                json.dumps({"type": "turn_ended", "status": "success"}),
                json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "又问一句"}]}}),
                json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "第二轮已答完。"}]}}),
                json.dumps({"type": "turn_ended", "status": "success"}),
            ]) + "\n", encoding="utf-8")
            db = pathlib.Path(tmp) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE composerHeaders (composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                        "isArchived INTEGER, isSubagent INTEGER, value TEXT)")
            header = {
                "name": "先问一句",
                "lastUpdatedAt": int((T0 + 50) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 5) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                        (tid, int(T0 * 1000), int((T0 + 50) * 1000), 0, 0, json.dumps(header)))
            con.commit()
            con.close()
            server._file_cache["cursor"].clear()
            server._cursor_unfinished_seen.clear()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 80):
                tasks = [server.normalize_task(t) for t in server.scan_cursor()]
            by_title = {t["title"]: t for t in tasks}
            self.assertNotIn("reply_needed", by_title["先问一句"])
            self.assertNotIn("reply_needed", by_title["又问一句"])

    def test_headers_unfinished_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER, value TEXT)"
            )
            payload = {
                "composerId": "c1",
                "name": "Fix login",
                "subtitle": "Edited auth.ts",
                "unfinishedRunAt": int((T0 + 5) * 1000),
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "workspaceIdentifier": {
                    "uri": {"fsPath": "/Users/me/proj", "path": "/Users/me/proj"}
                },
            }
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                ("c1", int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(payload)),
            )
            con.commit()
            con.close()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_cursor()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["agent"], "cursor")
            self.assertEqual(tasks[0]["status"], "running")
            self.assertEqual(tasks[0]["cwd"], "/Users/me/proj")
            self.assertTrue(tasks[0]["link"].startswith("agentpanel://cursor?"))
            self.assertIn("id=c1", tasks[0]["link"])
            self.assertIn("cwd=", tasks[0]["link"])

    def test_new_session_run_signal_does_not_reopen_previous_closed_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = pathlib.Path(tmp) / "projects"
            transcript_dir = projects / "Users-me-app" / "agent-transcripts" / "c4"
            transcript_dir.mkdir(parents=True)
            transcript = transcript_dir / "c4.jsonl"
            transcript.write_text("\n".join(json.dumps(row) for row in [
                {"role": "user", "message": {"content": [{"type": "text", "text": "previous task"}]}},
                {"role": "assistant", "message": {"content": [{"type": "text", "text": "previous done"}]}},
                {"type": "turn_ended", "status": "success"},
            ]) + "\n", encoding="utf-8")
            db = pathlib.Path(tmp) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE composerHeaders (composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                        "isArchived INTEGER, isSubagent INTEGER, value TEXT)")
            header = {
                "name": "previous task",
                "unfinishedRunAt": int((T0 + 5) * 1000),
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 5) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                        ("c4", int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(header)))
            con.commit()
            con.close()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_cursor()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "previous task")
            self.assertEqual(tasks[0]["status"], "done")
            self.assertEqual(tasks[0]["display_status"], "done")

    def test_headers_finished_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER, value TEXT)"
            )
            payload = {
                "name": "Done task",
                "subtitle": "ok",
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 5) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                ("c2", int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(payload)),
            )
            con.commit()
            con.close()
            # checkpoint 已静默超过 CURSOR_DONE_QUIET_SECS 且无 open 转录 → done
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(pathlib.Path(tmp) / "projects")), \
                 mock.patch.object(server.time, "time", return_value=T0 + 5 + server.CURSOR_DONE_QUIET_SECS + 1):
                tasks = server.scan_cursor()
            self.assertEqual(tasks[0]["status"], "done")

    def test_headers_sticky_running_after_unfinished_clears(self):
        """长 Thinking：unfinished 清空且 turn_ended，但 sticky 窗口内仍算执行中。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "state.vscdb"
            projects = pathlib.Path(tmp) / "projects"
            tid = "sticky1"
            tdir = projects / "Users-me-app" / "agent-transcripts" / tid
            tdir.mkdir(parents=True)
            (tdir / f"{tid}.jsonl").write_text(
                "\n".join([
                    json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "go"}]}}),
                    json.dumps({"role": "assistant", "message": {"content": [
                        {"type": "text", "text": "中间结论，其实还没做完"}
                    ]}}),
                    json.dumps({"type": "turn_ended", "status": "success"}),
                ]) + "\n",
                encoding="utf-8",
            )
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER, value TEXT)"
            )
            # 先带 unfinished 扫一次，写入 sticky
            payload_run = {
                "name": "Long think",
                "subtitle": "Edited a.py",
                "unfinishedRunAt": int((T0 + 5) * 1000),
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 5) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                (tid, int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(payload_run)),
            )
            con.commit()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                self.assertEqual(server.scan_cursor()[0]["status"], "running")

            # 清掉 unfinished，checkpoint 也早已静默；若无 sticky 会误报 done
            payload_gap = dict(payload_run)
            del payload_gap["unfinishedRunAt"]
            con.execute("UPDATE composerHeaders SET value=? WHERE composerId=?",
                        (json.dumps(payload_gap), tid))
            con.commit()
            con.close()
            server._file_cache["cursor"].clear()
            gap_t = T0 + 10 + 20  # 仍在 STICKY 内，但已超过 DONE_QUIET
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=gap_t):
                tasks = server.scan_cursor()
            self.assertEqual(tasks[0]["status"], "running")

            # sticky 过期后应变为 done
            server._file_cache["cursor"].clear()
            after = T0 + 10 + server.CURSOR_RUN_STICKY_SECS + server.CURSOR_DONE_QUIET_SECS + 5
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=after):
                tasks = server.scan_cursor()
            self.assertEqual(tasks[0]["status"], "done")

    def test_headers_stays_running_when_transcript_turn_still_open(self):
        """unfinished 已空但转录末尾不是 turn_ended（工具间隙）→ 仍 running。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "state.vscdb"
            projects = pathlib.Path(tmp) / "projects"
            tid = "open1"
            tdir = projects / "Users-me-app" / "agent-transcripts" / tid
            tdir.mkdir(parents=True)
            (tdir / f"{tid}.jsonl").write_text(
                "\n".join([
                    json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "go"}]}}),
                    json.dumps({"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell"}]}}),
                ]) + "\n",
                encoding="utf-8",
            )
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER, value TEXT)"
            )
            payload = {
                "name": "Still tooling",
                "subtitle": "Edited a.py",
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 5) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                (tid, int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(payload)),
            )
            con.commit()
            con.close()
            # checkpoint 已静默很久，若只看 quiet 会误判 done；转录 open 应拦住
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 5 + server.CURSOR_DONE_QUIET_SECS + 30):
                tasks = server.scan_cursor()
            self.assertEqual(tasks[0]["status"], "running")

    def test_headers_done_prefers_assistant_transcript_summary(self):
        """完成态 recent 应取助手最后回复，而不是 Edited xxx。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "state.vscdb"
            projects = pathlib.Path(tmp) / "projects"
            tid = "sum1"
            tdir = projects / "Users-me-app" / "agent-transcripts" / tid
            tdir.mkdir(parents=True)
            reply = ("已重新打包 DMG，路径在 dist/App.dmg\n\n验证结果：签名通过。\n\n"
                     "**需要你确认（再继续选区 2）**\n\n请选择：\n- A：只补视觉红星\n- B：红星 + 空值校验\n"
                     "- C：保持现状\n\n你回复 A/B/C 后我改完就进选区 2（查询态）。")
            (tdir / f"{tid}.jsonl").write_text(
                "\n".join([
                    json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "do it"}]}}),
                    json.dumps({"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell"}]}}),
                    json.dumps({"role": "assistant", "message": {"content": [
                        {"type": "text", "text": reply}
                    ]}}),
                    json.dumps({"type": "turn_ended", "status": "success"}),
                ]) + "\n",
                encoding="utf-8",
            )
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER, value TEXT)"
            )
            payload = {
                "name": "Pack dmg",
                "subtitle": "Edited main.swift",
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 5) * 1000),
                "filesChangedCount": 3,
                "totalLinesAdded": 40,
                "totalLinesRemoved": 2,
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                (tid, int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(payload)),
            )
            con.commit()
            con.close()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(projects)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 5 + server.CURSOR_DONE_QUIET_SECS + 1):
                tasks = server.scan_cursor()
                with (tdir / f"{tid}.jsonl").open("a", encoding="utf-8") as transcript:
                    transcript.write(json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "选 B"}]}}) + "\n")
                server._file_cache["cursor"].clear()
                answered = server.normalize_task(server.scan_cursor()[0])
            self.assertEqual(tasks[0]["status"], "done")
            self.assertIn("重新打包 DMG", tasks[0]["recent"])
            self.assertNotIn("Edited main.swift", tasks[0]["recent"])
            self.assertIn("3 个文件", tasks[0]["recent"])
            self.assertNotIn("验证结果", tasks[0]["recent"])
            self.assertEqual(tasks[0]["reply_text"], reply)
            self.assertTrue(server.normalize_task(tasks[0])["reply_needed"].startswith("你回复 A/B/C 后"))
            self.assertNotIn("reply_needed", answered)
            self.assertFalse(answered["reply_text"])

    def test_headers_cleared_unfinished_but_recent_checkpoint_stays_running(self):
        """回合间隙 unfinishedRunAt 被清空、但 checkpoint 仍在写 → 仍算执行中。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "state.vscdb"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE composerHeaders ("
                "composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
                "isArchived INTEGER, isSubagent INTEGER, value TEXT)"
            )
            payload = {
                "name": "Still going",
                "subtitle": "tool",
                "lastUpdatedAt": int((T0 + 5) * 1000),
                "conversationCheckpointLastUpdatedAt": int((T0 + 9) * 1000),
                "workspaceIdentifier": {"uri": {"fsPath": "/repo"}},
            }
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?)",
                ("c3", int(T0 * 1000), int((T0 + 5) * 1000), 0, 0, json.dumps(payload)),
            )
            con.commit()
            con.close()
            with mock.patch.object(server, "CURSOR_STATE_DB", str(db)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_cursor()
            self.assertEqual(tasks[0]["status"], "running")

    def test_transcript_fallback_when_no_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "projects" / "Users-me-app" / "agent-transcripts" / "s1"
            root.mkdir(parents=True)
            f = root / "s1.jsonl"
            lines = [
                {"role": "user", "message": {"content": [{"type": "text", "text": "hello world"}]}},
                {"role": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"type": "turn_ended", "status": "success"},
            ]
            f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            with mock.patch.object(server, "CURSOR_STATE_DB", str(pathlib.Path(tmp) / "missing.db")), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(pathlib.Path(tmp) / "projects")), \
                 mock.patch.object(server.os.path, "getmtime", return_value=T0 + 5), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_cursor()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["status"], "done")
            self.assertIn("hello", tasks[0]["title"])

    def test_transcript_turns_use_embedded_user_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "projects" / "Users-me-app" / "agent-transcripts" / "dated"
            root.mkdir(parents=True)
            f = root / "dated.jsonl"
            lines = [
                {"role": "user", "message": {"content": [{"type": "text", "text":
                    "<timestamp>Monday, Nov 23, 2026, 10:00 AM (UTC+8)</timestamp>\n<user_query>first</user_query>"}]}},
                {"role": "assistant", "message": {"content": [{"type": "text", "text": "first done"}]}},
                {"type": "turn_ended", "status": "success"},
                {"role": "user", "message": {"content": [{"type": "text", "text":
                    "<timestamp>Monday, Nov 23, 2026, 11:30 AM (UTC+8)</timestamp>\n<user_query>second</user_query>"}]}},
                {"role": "assistant", "message": {"content": [{"type": "text", "text": "second done"}]}},
                {"type": "turn_ended", "status": "success"},
            ]
            f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            with mock.patch.object(server, "CURSOR_STATE_DB", str(pathlib.Path(tmp) / "missing.db")), \
                 mock.patch.object(server, "CURSOR_PROJECTS_DIR", str(pathlib.Path(tmp) / "projects")), \
                 mock.patch.object(server.time, "time", return_value=T0 + 60):
                tasks = server.scan_cursor()
            self.assertEqual(len(tasks), 2)
            self.assertGreater(tasks[0]["mtime"], tasks[1]["mtime"])
            self.assertEqual(tasks[1]["mtime"], server.parse_ts("2026-11-23T02:00:00Z"))


class QoderCnScanTests(unittest.TestCase):
    def setUp(self):
        server._file_cache["qoder-cn"].clear()
        self.addCleanup(server._file_cache["qoder-cn"].clear)

    def test_explicit_approval_survives_transcript_timeout_until_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            transcript = project / "session-a.jsonl"
            transcript.write_text('\n'.join(map(json.dumps, [
                {"type": "user", "message": {"content": "check build"}},
                {"type": "assistant", "message": {"stop_reason": "tool_use"}},
            ])), encoding="utf-8")
            run = root / "logs" / "runs" / "test-p1234"
            run.mkdir(parents=True)
            run_log = run / "qodercli.log"
            run_log.write_text(
                "2026-11-20T14:41:08.056+08:00 INFO  debug.message [thread-session] permission ask dispatched {\n"
                "  sessionId: 'session-a',\n  toolName: 'Bash',\n  toolUseId: 'shell-call'\n}\n", encoding="utf-8",
            )
            os.utime(run_log, (T0, T0))
            segments = root / "logs" / "sessions" / "demo" / "session-a" / "segments"
            segments.mkdir(parents=True)
            log = segments / "test.jsonl"
            request = {"type": "tool.requested", "turn_id": "turn-a", "tool_call_id": "shell-call"}
            log.write_text(json.dumps(request) + "\n", encoding="utf-8")
            with mock.patch.object(server, "QODER_CN_PROJECTS_DIR", str(root / "projects")), \
                 mock.patch.object(server, "QODER_CN_DB_PATHS", ()), \
                 mock.patch.object(server, "qoder_runner_alive", return_value=True) as alive, \
                 mock.patch.object(server.os.path, "getmtime", return_value=T0), \
                 mock.patch.object(server.time, "time", return_value=T0 + 1800):
                task = server.scan_qoder_cn()[0]
                self.assertEqual(task["display_status"], "waiting")
                self.assertEqual(task["status_reason"], "等待批准执行命令（Bash）。")
                self.assertEqual(task["link"], "qoder-cn://chat/open?sessionId=session-a")
                alive.return_value = False
                self.assertEqual(server.scan_qoder_cn()[0]["display_status"], "unknown")
                alive.return_value = True
                log.write_text(json.dumps({**request, "type": "turn.finished"}) + "\n" + json.dumps(request) + "\n", encoding="utf-8")
                self.assertNotEqual(server.scan_qoder_cn()[0]["display_status"], "waiting")
                log.write_text(json.dumps(request) + "\n", encoding="utf-8")
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({**request, "type": "permission.resolved"}) + "\n")
                self.assertNotEqual(server.scan_qoder_cn()[0]["display_status"], "waiting")

    def test_jsonl_user_last_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp) / "projects" / "demo"
            proj.mkdir(parents=True)
            f = proj / "sess-1.jsonl"
            lines = [
                {"type": "user", "cwd": "/repo", "timestamp": "2026-11-07T07:00:00Z",
                 "message": {"content": "implement feature"}},
            ]
            f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            with mock.patch.object(server, "QODER_CN_PROJECTS_DIR", str(pathlib.Path(tmp) / "projects")), \
                 mock.patch.object(server, "QODER_CN_DB_PATHS", ()), \
                 mock.patch.object(server.os.path, "getmtime", return_value=T0 + 5), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_qoder_cn()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["agent"], "qoder-cn")
            self.assertEqual(tasks[0]["status"], "running")
            self.assertEqual(tasks[0]["cwd"], "/repo")

    def test_jsonl_assistant_end_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp) / "projects" / "demo"
            proj.mkdir(parents=True)
            f = proj / "sess-2.jsonl"
            reply = "已修改。\n\n请回复 A/B 后我再继续。"
            lines = [
                {"type": "user", "cwd": "/repo", "timestamp": "2026-11-07T07:00:00Z",
                 "message": {"content": "question"}},
                {"type": "assistant", "timestamp": "2026-11-07T07:01:00Z",
                 "message": {"content": [{"type": "text", "text": reply}],
                             "stop_reason": "end_turn"}},
            ]
            f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            with mock.patch.object(server, "QODER_CN_PROJECTS_DIR", str(pathlib.Path(tmp) / "projects")), \
                 mock.patch.object(server, "QODER_CN_DB_PATHS", ()), \
                 mock.patch.object(server.os.path, "getmtime", return_value=T0 + 5), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_qoder_cn()
            self.assertEqual(tasks[0]["status"], "done")
            self.assertEqual(tasks[0]["reply_text"], reply)
            self.assertEqual(server.normalize_task(tasks[0])["reply_needed"], "请回复 A/B 后我再继续。")

    def test_sqlite_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "local.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE chat_session ("
                "session_id TEXT, title TEXT, project_name TEXT, gmt_modified INTEGER)"
            )
            con.execute(
                "CREATE TABLE chat_message ("
                "id TEXT, session_id TEXT, role TEXT, content TEXT, gmt_create INTEGER)"
            )
            con.execute(
                "INSERT INTO chat_session VALUES (?,?,?,?)",
                ("s1", "Old title", "demo", int((T0 + 5) * 1000)),
            )
            con.execute(
                "INSERT INTO chat_message VALUES (?,?,?,?,?)",
                ("m1", "s1", "assistant", "done reply", int((T0 + 5) * 1000)),
            )
            con.commit()
            con.close()
            with mock.patch.object(server, "QODER_CN_PROJECTS_DIR", str(pathlib.Path(tmp) / "missing")), \
                 mock.patch.object(server, "QODER_CN_DB_PATHS", (str(db),)), \
                 mock.patch.object(server.time, "time", return_value=T0 + 10):
                tasks = server.scan_qoder_cn()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["status"], "done")
            self.assertEqual(tasks[0]["title"], "Old title")
            self.assertEqual(tasks[0]["link"], "qoder-cn://chat/open?sessionId=s1")


if __name__ == "__main__":
    unittest.main()
