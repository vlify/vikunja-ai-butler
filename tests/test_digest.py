"""Unit tests for daily evening summary digest, timezone conversion, and fallback."""

import os
import unittest
import tempfile
from typing import Any, Dict, List, Optional

from butler.digest import (
    format_completed_tasks,
    format_pending_tasks,
    build_digest_ai_insight,
    run_digest,
)
from butler.vikunja_client import VikunjaClient
from butler.llm_runner import LLMRunner, LLMTimeoutError, LLMExecutionError


class MockDigestVikunjaClient(VikunjaClient):
    def __init__(self, tasks: List[Dict[str, Any]], done_tasks: List[Dict[str, Any]]):
        super().__init__(base_url="http://127.0.0.1:3456", token="mock_token")
        self._tasks = tasks
        self._done_tasks = done_tasks

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._tasks

    def get_done_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._done_tasks


class MockDigestLLMRunner(LLMRunner):
    def __init__(self, output_text: str = "", fail_with: Optional[Exception] = None):
        super().__init__(command_template="mock_cmd")
        self.output_text = output_text
        self.fail_with = fail_with

    def run(self, prompt: str) -> str:
        if self.fail_with:
            raise self.fail_with
        return self.output_text


class TestDigest(unittest.TestCase):
    def test_completed_tasks_timezone_formatting(self):
        # 15:30 UTC on 2026-09-02 is 23:30 CST on 2026-09-02
        # 01:00 UTC on 2026-09-02 is 09:00 CST on 2026-09-02
        # 16:30 UTC on 2026-09-02 is 00:30 CST on 2026-09-03 (should be excluded)
        done_tasks = [
            {"id": 1, "title": "Evening task", "done": True, "done_at": "2026-09-02T15:30:00Z"},
            {"id": 2, "title": "Morning task", "done": True, "done_at": "2026-09-02T01:00:00Z"},
            {"id": 3, "title": "Next day task", "done": True, "done_at": "2026-09-02T16:30:00Z"},
        ]

        text, count = format_completed_tasks(done_tasks, "2026-09-02", tz_name="Asia/Shanghai")
        self.assertEqual(count, 2)
        self.assertIn("## ✅ 今日已完成 (2 条)", text)
        self.assertIn("- [x] Morning task (完成于 09:00)", text)
        self.assertIn("- [x] Evening task (完成于 23:30)", text)
        self.assertNotIn("Next day task", text)

    def test_pending_tasks_grouped_by_project(self):
        tasks = [
            {"id": 10, "title": "Inbox item", "project_id": 1, "done": False},
            {"id": 20, "title": "Action item", "project_id": 5, "done": False},
            {"id": 30, "title": "Project item", "project_id": 6, "done": False},
            {"id": 40, "title": "Waiting item", "project_id": 7, "done": False},
            {"id": 50, "title": "Someday item", "project_id": 8, "done": False},  # Should be excluded
            {"id": 60, "title": "Done item", "project_id": 5, "done": True},      # Should be excluded
        ]
        allowed = {1: "Inbox", 5: "Single Action", 6: "Project", 7: "Waiting For"}
        order = [1, 5, 6, 7]

        text = format_pending_tasks(tasks, allowed, order)
        self.assertIn("## 📋 今日待办", text)
        self.assertIn("### Inbox", text)
        self.assertIn("- [ ] Inbox item", text)
        self.assertIn("### Single Action", text)
        self.assertIn("- [ ] Action item", text)
        self.assertIn("### Project", text)
        self.assertIn("- [ ] Project item", text)
        self.assertIn("### Waiting For", text)
        self.assertIn("- [ ] Waiting item", text)
        self.assertNotIn("Someday item", text)
        self.assertNotIn("Done item", text)

    def test_empty_data_sources_graceful_degradation(self):
        # Empty completed
        comp_text, comp_count = format_completed_tasks([], "2026-09-02")
        self.assertEqual(comp_count, 0)
        self.assertIn("今日暂无完成记录——一份清单的意义在于划掉它", comp_text)

        # Empty pending
        pend_text = format_pending_tasks([], {1: "Inbox"}, [1])
        self.assertIn("（今日无未完成待办事项）", pend_text)

    def test_ai_insight_fallback_on_llm_failure(self):
        # LLM Timeout
        llm_timeout = MockDigestLLMRunner(fail_with=LLMTimeoutError("timed out"))
        out = build_digest_ai_insight(llm_timeout, "2026-09-02", "comp", "pend", "aw")
        self.assertIn("⚠️ AI 点评生成失败 (退出状态码: 124)", out)

        # LLM Non-zero exit code
        llm_fail = MockDigestLLMRunner(fail_with=LLMExecutionError("failed", exit_code=1))
        out2 = build_digest_ai_insight(llm_fail, "2026-09-02", "comp", "pend", "aw")
        self.assertIn("⚠️ AI 点评生成失败 (退出状态码: 1)", out2)

    def test_run_digest_end_to_end_dry_run(self):
        done_tasks = [
            {"id": 1, "title": "Buy groceries", "done": True, "done_at": "2026-09-02T10:00:00Z"},
        ]
        pending_tasks = [
            {"id": 2, "title": "Review pull request", "project_id": 5, "done": False},
        ]
        client = MockDigestVikunjaClient(pending_tasks, done_tasks)
        llm = MockDigestLLMRunner(output_text="Great progress today!")

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            backup_file = tf.name

        try:
            os.environ["BACKUP_FILE"] = backup_file
            ret = run_digest(
                dry_run=True,
                target_date="2026-09-02",
                client=client,
                llm=llm,
            )
            self.assertEqual(ret, 0)

            # Check backup file written
            self.assertTrue(os.path.exists(backup_file))
            with open(backup_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("🌙 晚间总结 · 2026-09-02(周三)", content)
            self.assertIn("Buy groceries", content)
            self.assertIn("Review pull request", content)
            self.assertIn("Great progress today!", content)
        finally:
            if os.path.exists(backup_file):
                os.remove(backup_file)


    def test_send_himalaya_email_success(self):
        from unittest.mock import patch, MagicMock
        from butler.digest import send_himalaya_email

        cfg = {
            "type": "himalaya",
            "account": "qq",
            "from": "bot@example.com",
            "to": "user@example.com",
            "bin": "himalaya",
        }

        with patch("shutil.which", return_value="/usr/bin/himalaya"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
                send_himalaya_email(cfg, "Test Subject", "Test Body Content")

                self.assertEqual(mock_run.call_count, 1)
                cmd = mock_run.call_args[0][0]
                self.assertEqual(cmd[0], "himalaya")
                self.assertEqual(cmd[1:5], ["message", "send", "--account", "qq"])
                self.assertEqual(cmd[5], "--")
                self.assertTrue(cmd[6].endswith(".eml"))

    def test_send_himalaya_email_nested_config_compatibility(self):
        from unittest.mock import patch, MagicMock
        from butler.digest import send_himalaya_email

        nested_cfg = {
            "type": "himalaya",
            "himalaya": {
                "account": "work",
                "mail_from": "nested_from@example.com",
                "mail_to": "nested_to@example.com",
                "bin": "custom-himalaya",
            },
        }

        with patch("shutil.which", return_value="/usr/local/bin/custom-himalaya"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="Sent", stderr="")
                send_himalaya_email(nested_cfg, "Nested Subject", "Nested Body")

                cmd = mock_run.call_args[0][0]
                self.assertEqual(cmd[0], "custom-himalaya")
                self.assertEqual(cmd[3], "--account")
                self.assertEqual(cmd[4], "work")

    def test_run_digest_himalaya_failure_fallback_and_backup(self):
        from unittest.mock import patch, MagicMock

        done_tasks = [{"id": 1, "title": "Done task", "done": True, "done_at": "2026-09-02T10:00:00Z"}]
        client = MockDigestVikunjaClient([], done_tasks)

        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            backup_file = tf.name

        try:
            os.environ["BACKUP_FILE"] = backup_file
            mock_cfg = {
                "timezone": "Asia/Shanghai",
                "gtd": {"inbox_project_id": 1, "allowed_target_projects": {1: "Inbox"}, "digest_project_order": [1]},
                "backup": {"enabled": True, "file_path": backup_file},
                "notifier": {
                    "type": "himalaya",
                    "account": "qq",
                    "from": "bot@example.com",
                    "to": "user@example.com",
                },
            }

            with patch("butler.digest.load_config", return_value=mock_cfg):
                with patch("shutil.which", return_value="/usr/bin/himalaya"):
                    with patch("subprocess.run") as mock_run:
                        # Simulate himalaya network or auth error
                        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="SMTP error: timeout")
                        ret = run_digest(dry_run=False, target_date="2026-09-02", client=client)

                        # Must return non-zero exit code on failure
                        self.assertNotEqual(ret, 0)

                        # Backup file must still exist and contain the report
                        self.assertTrue(os.path.exists(backup_file))
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            content = bf.read()
                        self.assertIn("🌙 晚间总结 · 2026-09-02", content)
                        self.assertIn("Done task", content)
        finally:
            if os.path.exists(backup_file):
                os.remove(backup_file)


if __name__ == "__main__":
    unittest.main()

