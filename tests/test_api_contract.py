import importlib.util
import pathlib
import sys
import tempfile
import json
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("agent_panel_server", ROOT / "src/server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
from platforms import detect_runtime


class NormalizeTaskTests(unittest.TestCase):
    def test_normalizes_core_fields_and_preserves_extensions(self):
        task = server.normalize_task({
            "agent": "codex",
            "title": "  Fix   release  ",
            "cwd": None,
            "status": "unexpected",
            "mtime": "12.5",
            "link": None,
            "recent": None,
            "complete_at": "2026-10-29T00:00:00Z",
        })

        self.assertEqual(task["agent"], "codex")
        self.assertEqual(task["title"], "Fix release")
        self.assertEqual(task["cwd"], "")
        self.assertEqual(task["status"], "idle")
        self.assertEqual(task["mtime"], 12.5)
        self.assertEqual(task["link"], "")
        self.assertEqual(task["recent"], "")
        self.assertEqual(task["recent_at"], "")
        self.assertEqual(task["complete_at"], "2026-10-29T00:00:00Z")
        self.assertRegex(task["task_id"], r"^[0-9a-f]{16}$")

    def test_task_id_distinguishes_duplicate_titles(self):
        base = {"agent": "codex", "title": "好", "status": "running", "mtime": 1}
        first = server.normalize_task({**base, "link": "codex://threads/one"})
        second = server.normalize_task({**base, "link": "codex://threads/two"})

        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["task_id"], server.normalize_task({
            **base,
            "link": "codex://threads/one",
        })["task_id"])

    def test_missing_agent_is_rejected(self):
        self.assertIsNone(server.normalize_task({"title": "orphan"}))


class FormatConclusionTests(unittest.TestCase):
    def test_takes_first_paragraph_and_stats(self):
        text = "# 标题\n\n修好了登录闪退。\n\n下一步再看支付。\n"
        self.assertEqual(
            server.format_conclusion(text, stats="+3 −1"),
            "修好了登录闪退。（+3 −1）",
        )

    def test_empty_falls_back_to_stats(self):
        self.assertEqual(server.format_conclusion("", stats="+2"), "+2")


class CodexLinkTests(unittest.TestCase):
    def test_codex_thread_link_prefers_metadata_and_supports_both_filename_formats(self):
        thread_id = "11111111-1111-4111-8111-111111111111"
        segment_id = "22222222-2222-4222-8222-222222222222"
        for filename_id, suffix, meta_id, expected in [
            (thread_id, "", None, thread_id),
            (thread_id, "_" + segment_id, thread_id, thread_id),
            (thread_id, "_" + segment_id, "invalid", thread_id),
            (segment_id, "_" + thread_id, thread_id, thread_id),
            ("no-id", "", thread_id, thread_id),
            ("no-id", "", None, None),
        ]:
            with self.subTest(filename_id=filename_id, suffix=suffix, meta_id=meta_id), tempfile.TemporaryDirectory() as tmp:
                rollout = pathlib.Path(tmp) / f"rollout-2026-11-22T19-18-57-{filename_id}{suffix}.jsonl"
                events = [{"type": "session_meta", "payload": {"id": meta_id}},
                          {"type": "event_msg", "payload": {"type": "user_message", "message": "git pull"}}]
                rollout.write_text("\n".join(map(json.dumps, events)))
                task, = server.parse_codex_file(str(rollout))
                self.assertEqual(task["link"], f"codex://threads/{expected}" if expected else "")


class TurnTaskTests(unittest.TestCase):
    def test_codex_file_emits_one_task_per_turn_and_keeps_long_prompt(self):
        thread = "11111111-1111-4111-8111-111111111111"
        prompt_a = "中文任务 A：" + "请检查并修复这个功能。" * 20
        prompt_b = "中文任务 B：继续执行第二项工作。"
        records = [
            {"type": "session_meta", "payload": {"id": thread}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-a"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn-a", "item": {"type": "UserMessage", "content": [{"text": prompt_a}]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "turn_id": "turn-a", "content": [{"type": "output_text", "text": "已完成 A"}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-a", "last_agent_message": "已完成 A"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-b"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn-b", "item": {"type": "UserMessage", "content": [{"text": prompt_b}]}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-b", "last_agent_message": "已完成 B"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / f"rollout-2026-11-24T09-00-00-{thread}.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
            tasks = server.scan_codex_file_for_test(str(path)) if hasattr(server, "scan_codex_file_for_test") else server.parse_codex_file(str(path))
        self.assertEqual(len(tasks), 2)
        self.assertEqual([task["turn_key"] for task in tasks], ["turn-a", "turn-b"])
        self.assertEqual(tasks[0]["user_prompt"], prompt_a)
        self.assertEqual(tasks[0]["title"], prompt_a[:60])
        self.assertEqual(tasks[1]["user_prompt"], prompt_b)

    def test_codex_turn_mtime_is_not_overwritten_by_file_mtime(self):
        thread = "11111111-1111-4111-8111-111111111111"
        records = [
            {"type": "session_meta", "payload": {"id": thread}},
            {"type": "event_msg", "timestamp": "2026-11-23T01:00:00Z",
             "payload": {"type": "task_started", "turn_id": "turn-a"}},
            {"type": "event_msg", "timestamp": "2026-11-23T01:00:01Z",
             "payload": {"type": "user_message", "turn_id": "turn-a", "message": "first"}},
            {"type": "event_msg", "timestamp": "2026-11-23T01:01:00Z",
             "payload": {"type": "task_complete", "turn_id": "turn-a", "last_agent_message": "done a"}},
            {"type": "event_msg", "timestamp": "2026-11-23T02:00:00Z",
             "payload": {"type": "task_started", "turn_id": "turn-b"}},
            {"type": "event_msg", "timestamp": "2026-11-23T02:00:01Z",
             "payload": {"type": "user_message", "turn_id": "turn-b", "message": "second"}},
            {"type": "event_msg", "timestamp": "2026-11-23T02:01:00Z",
             "payload": {"type": "task_complete", "turn_id": "turn-b", "last_agent_message": "done b"}},
        ]
        file_mtime = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / f"rollout-{thread}.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
            with mock.patch.object(server, "CODEX_DIR", tmp), \
                 mock.patch.object(server.os.path, "getmtime", return_value=file_mtime), \
                 mock.patch.object(server.time, "time", return_value=file_mtime + 1):
                tasks = server.scan_codex()
        self.assertEqual(len(tasks), 2)
        self.assertLess(tasks[0]["mtime"], tasks[1]["mtime"])
        self.assertEqual(tasks[0]["status"], "done")

    def test_codex_new_turn_closes_previous_open_turn_without_complete(self):
        """缺 task_complete 的旧回合，在同会话新 task_started 后应结束，不能一直执行中。"""
        thread = "11111111-1111-4111-8111-111111111111"
        records = [
            {"type": "session_meta", "payload": {"id": thread}},
            {"type": "event_msg", "timestamp": "2026-11-20T08:00:00Z",
             "payload": {"type": "task_started", "turn_id": "turn-old"}},
            {"type": "event_msg", "timestamp": "2026-11-20T08:00:01Z",
             "payload": {"type": "user_message", "turn_id": "turn-old",
                         "message": "这种不在工程中的会话 没办法跳转到ide吗："}},
            {"type": "event_msg", "timestamp": "2026-11-20T08:10:00Z",
             "payload": {"type": "item_completed", "turn_id": "turn-old",
                         "item": {"type": "AgentMessage", "phase": "final_answer",
                                  "content": [{"type": "text", "text": "探针确认：先改本地标记"}]}}},
            # 旧回合没有 task_complete；同会话直接开了新回合
            {"type": "event_msg", "timestamp": "2026-11-24T03:54:40Z",
             "payload": {"type": "task_started", "turn_id": "turn-new"}},
            {"type": "event_msg", "timestamp": "2026-11-24T03:54:41Z",
             "payload": {"type": "user_message", "turn_id": "turn-new", "message": "继续下一个问题"}},
            {"type": "event_msg", "timestamp": "2026-11-24T03:55:03Z",
             "payload": {"type": "task_complete", "turn_id": "turn-new",
                         "last_agent_message": "新回合已完成"}},
        ]
        file_mtime = 1_800_000_000.0  # 远新于旧回合，模拟同会话后续写入
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / f"rollout-{thread}.jsonl"
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
            with mock.patch.object(server, "CODEX_DIR", tmp), \
                 mock.patch.object(server.os.path, "getmtime", return_value=file_mtime), \
                 mock.patch.object(server.time, "time", return_value=file_mtime + 1):
                tasks = {t["turn_key"]: t for t in server.scan_codex()}
        self.assertEqual(tasks["turn-old"]["status"], "done")
        self.assertLess(tasks["turn-old"]["mtime"], file_mtime - 1000)
        self.assertEqual(tasks["turn-new"]["status"], "done")
        self.assertIn("探针确认", tasks["turn-old"]["recent"])

    def test_cursor_prompt_is_the_task_identity_not_translated_summary(self):
        prompt = "中文任务：" + "请保留完整用户描述。" * 20
        task = server.normalize_task({"agent": "cursor", "status": "done", "link": "cursor://x",
                                      "session_key": "cursor#turn-2", "title": prompt[:60],
                                      "full_title": prompt, "user_prompt": prompt})
        self.assertEqual(task["title"], prompt[:60])
        self.assertEqual(task["full_title"], prompt)
        self.assertEqual(task["user_prompt"], prompt)


class ReplyTests(unittest.TestCase):
    def test_handoff_uses_explicit_sections_and_preserves_failed_verification(self):
        reply = ("已处理。\n\n**已直接修改**\n- 调整登录表单的校验规则。\n\n"
                 "## 验证\n- `pytest`：12 通过，1 失败。\n\n"
                 "下一步：修复登录超时测试。\n\n> 验证：全部通过。")
        task = server.normalize_task({"agent": "codex", "status": "done", "reply_text": reply})
        self.assertEqual(task["handoff"], {
            "changes": "调整登录表单的校验规则。",
            "verification": "`pytest`：12 通过，1 失败。",
            "next_step": "修复登录超时测试。",
        })

    def test_handoff_does_not_infer_verification_or_count_optional_offers(self):
        reply = ("已完成修改。\n\n改动：更新配置。\n\n如果你需要，我可以运行测试。\n"
                 "```markdown\n验证：全部通过。\n```\n> 下一步：部署。")
        task = server.normalize_task({"agent": "codex", "status": "done", "reply_text": reply})
        self.assertEqual(task["handoff"], {"changes": "更新配置。"})

    def test_codex_final_answer_is_kept_and_old_answer_clears_on_user_reply(self):
        reply = "**已直接修改**\n\n" + "测试通过。" * 20 + "\n\n请选择 A/B/C。\n你回复 A/B/C 后我再继续。"
        with tempfile.TemporaryDirectory() as tmp:
            rollout = pathlib.Path(tmp) / "rollout.jsonl"
            stamp = "2026-11-23T02:00:00Z"
            records = [
                ("event_msg", {"type": "task_started"}),
                ("event_msg", {"type": "user_message", "message": "核对表单"}),
                ("event_msg", {"type": "item_completed", "item": {"type": "CommandExecution", "content": [{"text": "内部工具输出"}]}}),
                ("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "text", "text": reply}]}}),
                ("event_msg", {"type": "task_complete", "last_agent_message": reply}),
            ]

            def write_records():
                rollout.write_text("\n".join(json.dumps({"type": kind, "payload": payload, "timestamp": stamp}, ensure_ascii=False)
                                             for kind, payload in records), encoding="utf-8")

            write_records()
            with mock.patch.object(server, "CODEX_DIR", tmp):
                task = server.normalize_task(server.scan_codex()[0])
                self.assertEqual(task["reply_text"], reply)
                self.assertEqual(task["status"], "done")
                self.assertEqual(task["reply_needed"], "你回复 A/B/C 后我再继续。")
                self.assertNotIn("回复 A/B/C", task["recent"])
                records.append(("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": "选 B"}]}}))
                write_records()
                server._file_cache["codex"].pop(str(rollout), None)
                task, = server.scan_codex()
                self.assertFalse(task.get("reply_needed"))
                self.assertFalse(task.get("reply_text"))

    def test_only_explicit_decision_that_gates_work_counts(self):
        for text in ["已完成。如果需要，我也可以继续优化。", "请确认一下效果。", "如果你想，回复 A/B 后我再继续。",
                     "```\n请选择 A/B。\n你回复 A/B 后我再继续。\n```",
                     "> 请选择 A/B。\n> 你回复 A/B 后我再继续。"]:
            with self.subTest(text=text):
                self.assertFalse(server.reply_request(text))


class ScannerRegistryTests(unittest.TestCase):
    def test_scanners_are_named_and_resolvable(self):
        names = [name for name, _ in server.SCANNERS]
        self.assertIn("codex", names)
        self.assertIn("cursor", names)
        for name, attr in server.SCANNERS:
            self.assertTrue(callable(getattr(server, attr)), msg=f"{name}->{attr}")

    def test_collect_all_tasks_isolates_scanner_errors(self):
        good = {
            "agent": "codex",
            "title": "ok",
            "status": "running",
            "mtime": 1,
            "link": "codex://x",
        }
        with mock.patch.object(server, "scan_codex", return_value=[good]), \
             mock.patch.object(server, "scan_claude", side_effect=RuntimeError("boom")), \
             mock.patch.object(server, "scan_apps", return_value=[]), \
             mock.patch.object(server, "scan_dsh", return_value=[]), \
             mock.patch.object(server, "scan_cursor", return_value=[]), \
             mock.patch.object(server, "scan_workbuddy", return_value=[]), \
             mock.patch.object(server, "scan_qoder_cn", return_value=[]):
            tasks = server.collect_all_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["agent"], "codex")

class TasksResponseTests(unittest.TestCase):
    def setUp(self):
        server._cache = {"data": [], "mtime": 0}
        server._prev_tasks.clear()
        server._pending_finish.clear()
        server._running_since.clear()
        server._heartbeat_sent.clear()
        server._timeline = []
        self.patches = [
            mock.patch.object(server, "load_config", return_value={"hidden": []}),
            mock.patch.object(server, "_save_timeline"),
            mock.patch.object(server, "scan_claude", return_value=[]),
            mock.patch.object(server, "scan_apps", return_value=[]),
            mock.patch.object(server, "scan_dsh", return_value=[]),
            mock.patch.object(server, "scan_cursor", return_value=[]),
            mock.patch.object(server, "scan_qoder_cn", return_value=[]),
            mock.patch.object(server, "scan_workbuddy", return_value=[]),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        server._prev_tasks.clear()
        server._pending_finish.clear()
        server._running_since.clear()
        server._heartbeat_sent.clear()
        server._timeline = []

    def _poll_until_finished(self, task, hits=None):
        """终态需连续确认才下发 finished。"""
        result = None
        n = hits if hits is not None else server.FINISH_CONFIRM_HITS
        for _ in range(n):
            server._cache["mtime"] = 0
            with mock.patch.object(server, "scan_codex", return_value=[task]):
                result = server.tasks()
        return result

    @staticmethod
    def codex_task(status="running"):
        return {
            "agent": "codex",
            "title": "好",
            "cwd": "/tmp/project",
            "status": status,
            "mtime": 10,
            "link": "codex://threads/one",
            "recent": "working",
            "recent_at": "",
        }

    def test_response_is_versioned_and_normalized(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            result = server.tasks()

        self.assertEqual(result["api_version"], 1)
        self.assertEqual(result["finished"], [])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["status"], "running")

    def test_finished_transition_uses_stable_task_id(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            first = server.tasks()
        task_id = first["tasks"][0]["task_id"]

        second = self._poll_until_finished(self.codex_task("done"))
        self.assertEqual(second["finished"], [{
            "task_id": task_id,
            "agent": "codex",
            "title": "好",
            "kind": "completed",
            "recent": "working",
            "cwd": "/tmp/project",
            "link": "codex://threads/one",
            "revision": "10.0",
        }])

    def test_running_to_idle_does_not_claim_blocked(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            server.tasks()
        result = self._poll_until_finished(self.codex_task("idle"))
        self.assertEqual(result["finished"], [])

    def test_explicit_waiting_emits_blocked_kind(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            server.tasks()
        result = self._poll_until_finished({**self.codex_task("idle"), "display_status": "waiting"})
        self.assertEqual(result["finished"][0]["kind"], "blocked")

    def test_unknown_does_not_emit_false_completion(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            server.tasks()
        result = self._poll_until_finished({**self.codex_task("idle"), "display_status": "unknown"})
        self.assertEqual(result["finished"], [])

    def test_brief_done_blip_does_not_notify(self):
        """执行中短暂变 done 又回到 running → 不应发完成通知。"""
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            server.tasks()
        server._cache["mtime"] = 0
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task("done")]):
            blip = server.tasks()
        self.assertEqual(blip["finished"], [])
        server._cache["mtime"] = 0
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            back = server.tasks()
        self.assertEqual(back["finished"], [])
        self.assertEqual(server._pending_finish, {})

    def test_stats_does_not_consume_finished(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            server.tasks()
        server._cache["mtime"] = 0
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task("done")]):
            # 菜单栏先打 stats，不应吃掉 pending / finished
            server.task_stats()
        result = self._poll_until_finished(self.codex_task("done"))
        self.assertEqual(len(result["finished"]), 1)
        self.assertEqual(result["finished"][0]["kind"], "completed")

    def test_finished_appends_timeline_and_view_skips_heartbeats(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            first = server.tasks()
        task_id = first["tasks"][0]["task_id"]
        reply = "已更新测试。\n\n你回复 A/B 后我再继续。"
        result = self._poll_until_finished({**self.codex_task("done"), "reply_text": reply, "reply_needed": "你回复 A/B 后我再继续。"})
        self.assertEqual(result["heartbeats"], [])
        self.assertEqual(result["finished"][0]["kind"], "completed")
        self.assertEqual(result["finished"][0]["reply_text"], reply)
        today = server.timeline_today()
        self.assertEqual(today["api_version"], 1)
        self.assertTrue(any(e["task_id"] == task_id and e["kind"] == "completed" and e["reply_text"] == reply for e in today["items"]))

        server._cache["mtime"] = 0
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            view = server.tasks(consume_finished=False)
        self.assertEqual(view["finished"], [])
        self.assertEqual(view["heartbeats"], [])

    def test_codex_segments_share_a_card_and_notify_once_per_observed_round(self):
        link = "codex://threads/11111111-1111-4111-8111-111111111111"
        first = {**self.codex_task(), "link": link, "session_key": "segment-a"}
        second = {**first, "session_key": "segment-b", "cwd": "", "mtime": 20, "status": "done",
                  "complete_at": "2026-11-23T02:01:00Z", "reply_text": "改动：修改登录校验。"}

        def poll(segments):
            server._cache["mtime"] = 0
            with mock.patch.object(server, "scan_codex", return_value=segments):
                return server.tasks()

        task_id = poll([first])["tasks"][0]["task_id"]
        result = poll([first, second])
        self.assertEqual(result["finished"], [])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["task_id"], task_id)
        self.assertEqual(result["tasks"][0]["cwd"], first["cwd"])
        event, = poll([first, second])["finished"]
        self.assertEqual((event["task_id"], event["kind"]), (task_id, "completed"))
        self.assertEqual(poll([first, second])["finished"], [])
        self.assertEqual(server._timeline[0]["handoff"], {"changes": "修改登录校验。"})

        third = {**first, "session_key": "segment-c", "mtime": 30}
        poll([second, third])
        third = {**third, "mtime": 31, "status": "done", "complete_at": "2026-11-23T02:02:00Z",
                 "reply_text": "验证：单元测试通过。"}
        self.assertEqual(poll([second, third])["finished"], [])
        self.assertEqual(len(poll([second, third])["finished"]), 1)
        self.assertEqual(len(server._timeline), 2)
        self.assertEqual(server._timeline[0]["reply_text"], second["reply_text"])
        self.assertEqual(server._timeline[1]["reply_text"], third["reply_text"])

    def test_existing_codex_timeline_link_is_repaired_by_task_id(self):
        current = self.codex_task("done")
        task_id = server.normalize_task(current)["task_id"]
        server._timeline = [
            {"agent": "codex", "task_id": task_id, "ts": server.time.time(), "link": "codex://"},
            {"agent": "codex", "task_id": "other", "ts": server.time.time(), "link": "codex://"},
        ]
        with mock.patch.object(server, "scan_codex", return_value=[current]):
            server.tasks(consume_finished=False)
        self.assertEqual(server._timeline[0]["link"], current["link"])
        self.assertEqual(server._timeline[1]["link"], "codex://")
        server._save_timeline.assert_called_once_with()

    def test_long_running_emits_heartbeat(self):
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            first = server.tasks()
        task_id = first["tasks"][0]["task_id"]
        # 伪造「已跑超过心跳阈值」
        server._running_since[task_id] = 1.0
        server._heartbeat_sent.clear()
        server._cache["mtime"] = 0
        with mock.patch.object(server, "scan_codex", return_value=[self.codex_task()]):
            again = server.tasks()
        self.assertEqual(len(again["heartbeats"]), 1)
        self.assertEqual(again["heartbeats"][0]["kind"], "heartbeat")
        self.assertEqual(again["heartbeats"][0]["task_id"], task_id)



class ExperienceContractTests(unittest.TestCase):
    def test_timeout_is_unknown_without_breaking_v1_status(self):
        task = {"agent": "codex", "title": "work"}
        server.set_execution_status(task, 10, 600, now=1000)
        normalized = server.normalize_task(task)
        self.assertEqual(normalized["status"], "idle")
        self.assertEqual(normalized["display_status"], "unknown")
        self.assertIn("过期", normalized["status_reason"])

    def test_settings_merge_and_invalid_update_preserves_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "CONFIG_PATH", str(pathlib.Path(tmp) / "config.json")):
            server.save_config({"hidden": ["cursor"], "project_aliases": {"/a": "工程"}})
            server.save_config({"notifications": {"heartbeat": True}})
            config = server.load_config()
            self.assertEqual(config["hidden"], ["cursor"])
            self.assertEqual(config["project_aliases"], {"/a": "工程"})
            self.assertTrue(config["notifications"]["completed"])
            self.assertTrue(config["notifications"]["focus"])
            before = pathlib.Path(server.CONFIG_PATH).read_text()
            with self.assertRaises(ValueError):
                server.save_config({"notifications": {"completed": "yes"}})
            self.assertEqual(pathlib.Path(server.CONFIG_PATH).read_text(), before)

    def test_history_is_bounded_to_requested_day(self):
        import datetime
        start = datetime.datetime(2026, 11, 1).timestamp()
        with mock.patch.object(server, "_timeline", [{"ts": start-1}, {"ts": start}, {"ts": start+86400}]):
            self.assertEqual(server.timeline_today("2026-11-01")["items"], [{"ts": start}])
            with self.assertRaises(ValueError):
                server.timeline_today("invalid")

class OpenAIDesktopResolveTests(unittest.TestCase):
    def test_codex_app_binary_named_chatgpt_maps_to_codex(self):
        with mock.patch.object(
            server, "proc_executable_paths",
            return_value=["/Applications/Codex.app/Contents/MacOS/ChatGPT"],
        ):
            self.assertEqual(
                server.resolve_openai_desktop_agent(),
                ("codex", "Codex", "codex://"),
            )

    def test_chatgpt_app_maps_to_chatgpt(self):
        with mock.patch.object(
            server, "proc_executable_paths",
            return_value=["/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"],
        ):
            self.assertEqual(
                server.resolve_openai_desktop_agent()[0],
                "chatgpt",
            )

    def test_drop_online_placeholder_when_real_session_exists(self):
        tasks = [
            {"agent": "codex", "recent": server.ONLINE_PLACEHOLDER_RECENT, "title": "Codex 在线"},
            {"agent": "codex", "recent": "修好了", "title": "真实会话"},
            {"agent": "muse", "recent": server.ONLINE_PLACEHOLDER_RECENT, "title": "Muse 在线"},
        ]
        kept = server._drop_online_placeholders(tasks)
        self.assertEqual([t["title"] for t in kept], ["真实会话", "Muse 在线"])


class PlatformGuardTests(unittest.TestCase):
    def test_native_apps_are_skipped_outside_macos(self):
        runtime = detect_runtime(system="Linux", home="/tmp/example")
        with mock.patch.object(server, "RUNTIME", runtime):
            self.assertEqual(server.scan_apps(), [])


if __name__ == "__main__":
    unittest.main()
