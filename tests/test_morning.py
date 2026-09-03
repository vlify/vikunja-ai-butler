"""
Unit tests and edge fixtures for Vikunja AI Butler Morning Module (SPEC-morning.md v2.1).

Covers:
- §5 TDD Verification Contracts (9 test cases)
- §6 Edge Fixtures (TSV malformed, 24h boundary, dedup boundary, translation boundary, delivery boundary)
"""

import os
import re
import sys
import json
import unittest
import datetime
from unittest.mock import patch, MagicMock, call
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

# Ensure project root in sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm_runner import LLMRunner, LLMTimeoutError, LLMExecutionError

# Morning and Matrix modules to be tested
from butler.matrix import (
    send_matrix_message,
    deliver_morning_report,
    MatrixDeliveryError,
)
from butler.morning import (
    MorningItem,
    fetch_github_notifications,
    parse_github_tsv_line,
    fetch_gmail_messages,
    parse_gmail_envelope,
    translate_items_batch,
    reconcile_with_vikunja,
    format_morning_report,
    run_morning,
    GITHUB_REASON_MAP,
    ACTIONABLE_REASONS,
)


class MockVikunjaClient(VikunjaClient):
    """Mock VikunjaClient for morning tests."""
    def __init__(self, tasks: Optional[List[Dict[str, Any]]] = None):
        super().__init__(base_url="https://vikunja.example.com", token="mock_secret_token_123")
        self._tasks: List[Dict[str, Any]] = tasks if tasks is not None else []
        self.created_tasks: List[Dict[str, Any]] = []

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._tasks

    def create_task(
        self,
        project_id: int,
        title: str,
        description: Optional[str] = None,
        parent_task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        task = {
            "id": 100 + len(self.created_tasks) + 1,
            "project_id": project_id,
            "title": title,
            "description": description,
            "done": False,
        }
        self.created_tasks.append(task)
        return task


class MockLLMRunner(LLMRunner):
    """Mock LLMRunner for morning tests."""
    def __init__(
        self,
        output_text: str = "",
        fail_with: Optional[Exception] = None,
        call_tracker: Optional[List[str]] = None,
    ):
        super().__init__(command_template="mock_cmd")
        self.output_text = output_text
        self.fail_with = fail_with
        self.call_tracker = call_tracker if call_tracker is not None else []

    def run(self, prompt: str) -> str:
        self.call_tracker.append(prompt)
        if self.fail_with:
            raise self.fail_with
        return self.output_text


class TestMorningContracts(unittest.TestCase):
    """Test suite covering §5 contracts and §6 edge fixtures."""

    # ----------------------------------------------------------------------
    # 1. test_github_notifications_parsing_and_reason_mapping
    # ----------------------------------------------------------------------
    def test_github_notifications_parsing_and_reason_mapping(self):
        """Contract 1: GitHub notifications parsing, reason mapping, and env isolation."""
        tsv_output = (
            "2026-09-03T01:00:00Z\tmention\tvlify/vikunja-ai-butler\tFix memory leak\thttps://github.com/vlify/vikunja-ai-butler/pull/1\n"
            "2026-09-03T02:00:00Z\treview_requested\tvlify/test-repo\tAdd morning spec\thttps://github.com/vlify/test-repo/pull/2\n"
            "2026-09-03T03:00:00Z\tsubscribe\tother/repo\tRelease v1.0\thttps://github.com/other/repo/releases/tag/v1.0\n"
            "2026-09-03T04:00:00Z\tassign\tvlify/assigned-repo\tTask assigned\thttps://github.com/vlify/assigned-repo/issues/10\n"
            "2026-09-03T05:00:00Z\tteam_mention\tvlify/team-repo\tTeam help\thttps://github.com/vlify/team-repo/issues/20\n"
            "2026-09-03T06:00:00Z\tauthor\tvlify/my-repo\tNew comment by author\thttps://github.com/vlify/my-repo/issues/30\n"
            "2026-09-03T07:00:00Z\tcomment\tvlify/comment-repo\tDiscussion ongoing\thttps://github.com/vlify/comment-repo/issues/40\n"
            "2026-09-03T08:00:00Z\tci_activity\tvlify/ci-repo\tBuild succeeded\thttps://github.com/vlify/ci-repo/actions/runs/100\n"
            "2026-09-03T09:00:00Z\tunknown_reason\tvlify/odd-repo\tOdd event\thttps://github.com/vlify/odd-repo/issues/99\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=tsv_output, stderr="")
            items, ok = fetch_github_notifications(gh_bin="/usr/bin/gh")

            self.assertTrue(ok)
            self.assertEqual(len(items), 9)

            # Assert env isolation in command
            called_cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(called_cmd) if isinstance(called_cmd, list) else str(called_cmd)
            self.assertIn("env -u GIT_DIR -u GIT_WORK_TREE", cmd_str)

            # Check mapping and actionability
            reasons_found = {item.reason: (item.reason_cn, item.is_actionable) for item in items}
            self.assertEqual(reasons_found["mention"], ("提及", True))
            self.assertEqual(reasons_found["review_requested"], ("请求审查", True))
            self.assertEqual(reasons_found["assign"], ("指派", True))
            self.assertEqual(reasons_found["team_mention"], ("团队提及", True))

            # Non-actionable reasons
            self.assertEqual(reasons_found["subscribe"], ("订阅", False))
            self.assertEqual(reasons_found["author"], ("作者动态", False))
            self.assertEqual(reasons_found["comment"], ("评论", False))
            self.assertEqual(reasons_found["ci_activity"], ("CI", False))
            # Unknown reason defaults to info-only
            self.assertFalse(reasons_found["unknown_reason"][1])

    # ----------------------------------------------------------------------
    # 2. test_himalaya_mail_search_time_filtering_and_fallback
    # ----------------------------------------------------------------------
    def test_himalaya_mail_search_time_filtering_and_fallback(self):
        """Contract 2: Himalaya mail search 24h filter and empty-result fallback list."""
        cst = ZoneInfo("Asia/Shanghai")
        now_dt = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=cst)

        # 1) Search returns empty output -> Fallback list executes
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="ID | FLAGS | SUBJECT | FROM | DATE\n", stderr=""),
            ]
            items, ok = fetch_gmail_messages(
                himalaya_bin="himalaya",
                account="gmail",
                now_dt=now_dt,
            )
            self.assertTrue(ok)
            self.assertEqual(len(items), 0)
            self.assertEqual(mock_run.call_count, 2)
            cmd2 = mock_run.call_args_list[1][0][0]
            cmd2_str = " ".join(cmd2) if isinstance(cmd2, list) else str(cmd2)
            self.assertIn("envelope list", cmd2_str)

        # 2) Search command fails with exit code 1 -> list also fails -> ok=False ("不可用")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr="Network error"),
                MagicMock(returncode=1, stdout="", stderr="Network error"),
            ]
            items, ok = fetch_gmail_messages(
                himalaya_bin="himalaya",
                account="gmail",
                now_dt=now_dt,
            )
            self.assertFalse(ok)
            self.assertEqual(len(items), 0)

        # 3) Himalaya search returns envelopes with varying dates
        search_table = (
            "1\t\tDiscussion on issue #4\tnotifications@gitlab.com\t03/09/2026 02:00\n"
            "2\t\tOld notification\tnotifications@codeberg.org\t01/09/2026 12:00\n"
            "3\t\tv1.2.0 released\treply@greasyfork.org\t03/09/2026 08:00\n"
        )
        msg_1_body = "User commented: please review the patch at https://gitlab.com/group/repo/-/merge_requests/10"
        msg_3_body = "New script version v1.2.0 is published at https://greasyfork.org/scripts/42"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=search_table, stderr=""),
                MagicMock(returncode=0, stdout=msg_1_body, stderr=""),
                MagicMock(returncode=0, stdout=msg_3_body, stderr=""),
            ]
            items, ok = fetch_gmail_messages(
                himalaya_bin="himalaya",
                account="gmail",
                now_dt=now_dt,
            )
            self.assertTrue(ok)
            self.assertEqual(len(items), 2)  # item 2 filtered out due to >24h

            # item 1 is actionable
            self.assertEqual(items[0].source, "GitLab")
            self.assertTrue(items[0].is_actionable)
            self.assertIn("gitlab.com", items[0].url)

            # item 3 is release -> info
            self.assertEqual(items[1].source, "Greasyfork")
            self.assertFalse(items[1].is_actionable)

    # ----------------------------------------------------------------------
    # 3. test_safety_read_only_invariants
    # ----------------------------------------------------------------------
    def test_safety_read_only_invariants(self):
        """Contract 3: Read-only safety. No PUT /notifications, no flag seen, no token in logs."""
        # 1) In GitHub notification fetcher, assert no PUT /notifications
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fetch_github_notifications()
            called_cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(called_cmd) if isinstance(called_cmd, list) else str(called_cmd)
            self.assertNotIn("PUT", cmd_str)
            self.assertIn("'/notifications?per_page=30'", cmd_str)

        # 2) In Himalaya mail search, assert no 'flag seen' in write operations
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_gmail_messages()
            for c in mock_run.call_args_list:
                call_args = c[0][0]
                cmd_s = " ".join(call_args) if isinstance(call_args, list) else str(call_args)
                self.assertNotIn("flag add seen", cmd_s)
                self.assertNotIn("envelope delete", cmd_s)

        # 3) Matrix token does not leak into logs/exceptions
        secret_token = "syt_SECRET_MATRIX_TOKEN_xyz987"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.side_effect = Exception("Connection refused to matrix.org")
            mock_urlopen.side_effect = mock_resp
            with self.assertRaises(MatrixDeliveryError) as cm:
                send_matrix_message(
                    homeserver_url="https://matrix.org",
                    room_id="!HOvvSGQLISurrWtLMx:matrix.org",
                    token=secret_token,
                    body="Test message",
                )
            self.assertNotIn(secret_token, str(cm.exception))

    # ----------------------------------------------------------------------
    # 4. test_idempotent_task_creation_dedup
    # ----------------------------------------------------------------------
    def test_idempotent_task_creation_dedup(self):
        """Contract 4: Deduplication. Items already in Vikunja are marked ⟳, not created.
        --dry-run intercepts create_task completely."""
        existing_tasks = [
            {
                "id": 1,
                "title": "[GitHub] Fix memory leak",
                "description": "https://github.com/vlify/vikunja-ai-butler/pull/1",
                "done": False,
            },
            {
                "id": 2,
                "title": "[Greasyfork] Update userscript helper",
                "description": "https://greasyfork.org/scripts/123",
                "done": False,
            }
        ]
        client = MockVikunjaClient(tasks=existing_tasks)

        item1 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/vikunja-ai-butler",
            title="Fix memory leak",
            translated_title="修复内存泄漏",
            url="https://github.com/vlify/vikunja-ai-butler/pull/1",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        item2 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/vikunja-ai-butler",
            title="Brand new feature request",
            translated_title="全新特性支持",
            url="https://github.com/vlify/vikunja-ai-butler/issues/99",
            updated_at=datetime.datetime(2026, 9, 3, 9, 0),
            reason="assign",
            reason_cn="指派",
            is_actionable=True,
        )

        # 1) Real run (dry_run=False)
        actionable_items = [item1, item2]
        pending_titles, ok = reconcile_with_vikunja(
            actionable_items=actionable_items,
            client=client,
            target_project_id=1,
            dry_run=False,
        )
        self.assertTrue(ok)
        self.assertEqual(item1.status, "⟳已在待办")
        self.assertEqual(item2.status, "✅已入待办")
        self.assertEqual(len(client.created_tasks), 1)
        self.assertEqual(client.created_tasks[0]["title"], "[GitHub] 全新特性支持")
        self.assertEqual(client.created_tasks[0]["description"], "https://github.com/vlify/vikunja-ai-butler/issues/99")

        # 2) Dry-run (dry_run=True): create_task MUST be intercepted (0 creations)
        client2 = MockVikunjaClient(tasks=existing_tasks)
        item3 = MorningItem(
            source="GitLab",
            repo_or_project="group/project",
            title="MR needs review",
            translated_title="MR 需要审查",
            url="https://gitlab.com/group/project/-/merge_requests/5",
            updated_at=datetime.datetime(2026, 9, 3, 9, 30),
            reason="review_requested",
            reason_cn="请求审查",
            is_actionable=True,
        )
        pending_titles2, ok2 = reconcile_with_vikunja(
            actionable_items=[item3],
            client=client2,
            target_project_id=1,
            dry_run=True,
        )
        self.assertTrue(ok2)
        self.assertEqual(item3.status, "✅已入待办(DRY-RUN,未实际创建)")
        self.assertEqual(len(client2.created_tasks), 0)  # INTERCEPTED!

    # ----------------------------------------------------------------------
    # 5. test_uncertain_action_report_only
    # ----------------------------------------------------------------------
    def test_uncertain_action_report_only(self):
        """Contract 5: Uncertain items report only, never enter Vikunja."""
        uncertain_item = MorningItem(
            source="GitHub",
            repo_or_project="some/repo",
            title="Uncertain notification",
            translated_title="不确定的通知",
            url="https://github.com/some/repo/issues/1",
            updated_at=datetime.datetime(2026, 9, 3, 10, 0),
            reason="merged",  # Unknown reason
            reason_cn="合并",
            is_actionable=False,  # Fallback to info-only
        )

        client = MockVikunjaClient()
        actionable_items = []
        info_items = [uncertain_item]

        reconcile_with_vikunja(actionable_items=actionable_items, client=client, dry_run=False)
        self.assertEqual(len(client.created_tasks), 0)

        report = format_morning_report(
            target_date="2026-09-03",
            actionable_items=actionable_items,
            info_items=info_items,
            pending_tasks=["Existing task"],
            gh_ok=True,
            mail_ok=True,
            vikunja_ok=True,
        )
        self.assertIn("📰 **纯信息**", report)
        self.assertIn("不确定的通知", report)
        self.assertNotIn("✅已入待办", report)
        self.assertNotIn("⟳已在待办", report)

    # ----------------------------------------------------------------------
    # 6. test_llm_title_translation_and_fallback
    # ----------------------------------------------------------------------
    def test_llm_title_translation_and_fallback(self):
        """Contract 6: Single batch LLM call and fallback to original English."""
        item1 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo1",
            title="Fix async event loop timeout bug",
            url="https://github.com/vlify/repo1/issues/1",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        item2 = MorningItem(
            source="GitLab",
            repo_or_project="vlify/repo2",
            title="Release candidate v2.0 available",
            url="https://gitlab.com/vlify/repo2/-/tags/v2.0",
            updated_at=datetime.datetime(2026, 9, 3, 8, 30),
            reason="comment",
            reason_cn="评论",
            is_actionable=False,
        )

        call_tracker = []
        # Case A: Successful single batch call
        llm_output = json.dumps([
            {"index": 1, "translation": "修复异步事件循环超时缺陷"},
            {"index": 2, "translation": "候选发布版本 v2.0 可用"},
        ])
        llm = MockLLMRunner(output_text=llm_output, call_tracker=call_tracker)
        translate_items_batch([item1, item2], llm)

        self.assertEqual(len(call_tracker), 1)  # Single batch call!
        self.assertEqual(item1.translated_title, "修复异步事件循环超时缺陷")
        self.assertEqual(item2.translated_title, "候选发布版本 v2.0 可用")

        # Case B: LLM timeout / failure -> fallback to original title
        item3 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo3",
            title="Add retry logic to webhook caller",
            url="https://github.com/vlify/repo3/pull/5",
            updated_at=datetime.datetime(2026, 9, 3, 9, 0),
            reason="review_requested",
            reason_cn="请求审查",
            is_actionable=True,
        )
        failing_llm = MockLLMRunner(fail_with=LLMTimeoutError("LLM timed out"))
        translate_items_batch([item3], failing_llm)
        self.assertEqual(item3.translated_title, "Add retry logic to webhook caller")

    # ----------------------------------------------------------------------
    # 7. test_markdown_formatting_strictness
    # ----------------------------------------------------------------------
    def test_markdown_formatting_strictness(self):
        """Contract 7: Markdown formatting strictness. Bold index **1)**, blank lines, NO '- ' lists."""
        act_item = MorningItem(
            source="GitHub",
            repo_or_project="vlify/vikunja-ai-butler",
            title="Bug in morning digest",
            translated_title="晨间播报中的错误",
            url="https://github.com/vlify/vikunja-ai-butler/issues/10",
            updated_at=datetime.datetime(2026, 9, 3, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
            status="✅已入待办",
        )
        info_item = MorningItem(
            source="GitLab",
            repo_or_project="group/project",
            title="Release note v1.0",
            translated_title="版本发布说明 v1.0",
            url="https://gitlab.com/group/project/-/tags/v1.0",
            updated_at=datetime.datetime(2026, 9, 3, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            reason="comment",
            reason_cn="评论",
            is_actionable=False,
        )
        pending = ["完成晨间模块开发", "审核系统单元"]

        report = format_morning_report(
            target_date="2026-09-03",
            actionable_items=[act_item],
            info_items=[info_item],
            pending_tasks=pending,
            gh_ok=True,
            mail_ok=True,
            vikunja_ok=True,
        )

        # Assert title
        self.assertIn("📬 **消息与待办 · 2026-09-03**", report)
        # Assert bold numbered lines
        self.assertIn("**1)** [GitHub] vlify/vikunja-ai-butler — 晨间播报中的错误 https://github.com/vlify/vikunja-ai-butler/issues/10", report)
        self.assertIn("✅已入待办", report)
        self.assertIn("**1)** [GitLab] group/project — 版本发布说明 v1.0 https://gitlab.com/group/project/-/tags/v1.0", report)
        self.assertIn("**1)** 完成晨间模块开发", report)
        self.assertIn("**2)** 审核系统单元", report)

        # STRICT PROHIBITION: NO Markdown list syntax (- item, * item, 1. item)
        lines = [line.strip() for line in report.splitlines() if line.strip()]
        for line in lines:
            self.assertFalse(line.startswith("- "), f"Forbidden list marker '- ' in: {line}")
            self.assertFalse(line.startswith("* "), f"Forbidden list marker '* ' in: {line}")
            self.assertFalse(re.match(r"^\d+\.\s+", line), f"Forbidden numbered list marker in: {line}")

        # Blank line between paragraphs
        self.assertIn("\n\n**1)**", report)

    # ----------------------------------------------------------------------
    # 8. test_empty_and_unavailable_sections
    # ----------------------------------------------------------------------
    def test_empty_and_unavailable_sections(self):
        """Contract 8: Empty sections show '无', failed sections show '不可用'."""
        # 1) Everything succeeded but empty -> '无'
        report_empty = format_morning_report(
            target_date="2026-09-03",
            actionable_items=[],
            info_items=[],
            pending_tasks=[],
            gh_ok=True,
            mail_ok=True,
            vikunja_ok=True,
        )
        lines_empty = [l.strip() for l in report_empty.splitlines() if l.strip()]
        self.assertIn("📥 **需要行动**", lines_empty)
        self.assertIn("📰 **纯信息**", lines_empty)
        self.assertIn("📋 **当前未完成待办**", lines_empty)
        self.assertEqual(report_empty.count("无"), 3)

        # 2) Everything failed -> '不可用'
        report_failed = format_morning_report(
            target_date="2026-09-03",
            actionable_items=[],
            info_items=[],
            pending_tasks=[],
            gh_ok=False,
            mail_ok=False,
            vikunja_ok=False,
        )
        self.assertIn("不可用", report_failed)
        self.assertEqual(report_failed.count("不可用"), 3)

    # ----------------------------------------------------------------------
    # 9. test_matrix_delivery_and_email_fallback
    # ----------------------------------------------------------------------
    def test_matrix_delivery_and_email_fallback(self):
        """Contract 9: Dual channel delivery. Matrix failure falls back to himalaya email."""
        cfg = {
            "morning": {
                "matrix": {
                    "homeserver_url": "https://matrix.org",
                    "room_id": "!HOvvSGQLISurrWtLMx:matrix.org",
                    "token": "matrix_secret_token",
                }
            },
            "notifier": {
                "type": "himalaya",
                "account": "gmail",
                "to": "user@example.com",
                "from": "bot@example.com",
            }
        }
        report = "📬 **消息与待办 · 2026-09-03**\n\n无"

        # Case A: Matrix delivery succeeds
        with patch("butler.matrix.send_matrix_message") as mock_matrix:
            mock_matrix.return_value = {"event_id": "$mock_event_123"}
            with patch("butler.digest.send_himalaya_email") as mock_email:
                deliver_morning_report(report, cfg)
                mock_matrix.assert_called_once()
                mock_email.assert_not_called()

        # Case B: Matrix fails -> fallbacks to himalaya email
        with patch("butler.matrix.send_matrix_message") as mock_matrix:
            mock_matrix.side_effect = MatrixDeliveryError("Matrix HTTP 500 error")
            with patch("butler.digest.send_himalaya_email") as mock_email:
                deliver_morning_report(report, cfg)
                mock_matrix.assert_called_once()
                mock_email.assert_called_once()

        # Case C: Both fail -> raises error without secret leakage
        with patch("butler.matrix.send_matrix_message") as mock_matrix:
            mock_matrix.side_effect = MatrixDeliveryError("Matrix timeout")
            with patch("butler.digest.send_himalaya_email") as mock_email:
                mock_email.side_effect = Exception("SMTP connection failure")
                with self.assertRaises(Exception) as cm:
                    deliver_morning_report(report, cfg)
                self.assertNotIn("matrix_secret_token", str(cm.exception))


class TestEdgeFixtures(unittest.TestCase):
    """§6 Edge Fixtures tests."""

    # ----------------------------------------------------------------------
    # Edge 1: TSV malformed lines
    # ----------------------------------------------------------------------
    def test_edge_tsv_malformed_lines(self):
        """Malformed TSV lines: \\t, quotes, backslashes, Chinese/English, unknown reason, missing cols, invalid timestamp."""
        # 1) Line with tab inside title
        line_tab = "2026-09-03T01:00:00Z\tmention\trepo\tTitle\twith\ttab\thttps://github.com/repo/pull/1"
        item_tab = parse_github_tsv_line(line_tab)
        self.assertIsNotNone(item_tab)
        self.assertEqual(item_tab.source, "GitHub")
        self.assertEqual(item_tab.url, "https://github.com/repo/pull/1")

        # 2) Line with quotes and backslashes
        line_quotes = '2026-09-03T01:00:00Z\tassign\trepo\tTitle "with quotes" and \\backslash\\\thttps://github.com/repo/pull/2'
        item_quotes = parse_github_tsv_line(line_quotes)
        self.assertIsNotNone(item_quotes)
        self.assertIn('"with quotes"', item_quotes.title)
        self.assertIn('\\backslash\\', item_quotes.title)

        # 3) Chinese / English mixed title
        line_mix = "2026-09-03T01:00:00Z\tcomment\trepo\t修复 issue #123 导致的 Segfault 错误\thttps://github.com/repo/issues/123"
        item_mix = parse_github_tsv_line(line_mix)
        self.assertIsNotNone(item_mix)
        self.assertEqual(item_mix.title, "修复 issue #123 导致的 Segfault 错误")

        # 4) Unknown reason (e.g. 'merged') -> falls back to info-only
        line_unknown = "2026-09-03T01:00:00Z\tmerged\trepo\tPR merged\thttps://github.com/repo/pull/3"
        item_unknown = parse_github_tsv_line(line_unknown)
        self.assertIsNotNone(item_unknown)
        self.assertFalse(item_unknown.is_actionable)

        # 5) Missing columns (< 5 cols) -> graceful handling (None or default fallback, doesn't crash)
        line_missing = "2026-09-03T01:00:00Z\tmention\trepo"
        item_missing = parse_github_tsv_line(line_missing)
        self.assertIsNone(item_missing)

        # 6) Invalid timestamp
        line_bad_time = "INVALID_TIMESTAMP\tmention\trepo\tSome Title\thttps://github.com/repo/pull/4"
        item_bad_time = parse_github_tsv_line(line_bad_time)
        self.assertIsNotNone(item_bad_time)
        self.assertIsNotNone(item_bad_time.updated_at)

    # ----------------------------------------------------------------------
    # Edge 2: 24h boundary (24h ± 1min)
    # ----------------------------------------------------------------------
    def test_edge_24h_boundary(self):
        """Mail boundary: precisely 24h - 1min (retained) vs 24h + 1min (discarded)."""
        cst = ZoneInfo("Asia/Shanghai")
        now_dt = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=cst)

        # 24h - 1min ago = 2026-09-02 12:01:00 CST
        in_boundary_date_str = "02/09/2026 12:01"
        item_in = parse_gmail_envelope(
            envelope_id="101",
            subject="Discussion on GitLab issue",
            sender="gitlab@example.com",
            date_str=in_boundary_date_str,
            now_dt=now_dt,
        )
        self.assertIsNotNone(item_in)

        # 24h + 1min ago = 2026-09-02 11:59:00 CST
        out_boundary_date_str = "02/09/2026 11:59"
        item_out = parse_gmail_envelope(
            envelope_id="102",
            subject="Discussion on GitLab issue",
            sender="gitlab@example.com",
            date_str=out_boundary_date_str,
            now_dt=now_dt,
        )
        self.assertIsNone(item_out)

    # ----------------------------------------------------------------------
    # Edge 3: Deduplication boundaries
    # ----------------------------------------------------------------------
    def test_edge_dedup_boundaries(self):
        """Dedup boundaries: same link different title / same title different link / case sensitivity / different platforms."""
        existing_tasks = [
            {
                "id": 1,
                "title": "[GitHub] Fix Database Deadlock",
                "description": "https://github.com/vlify/repo/issues/10",
                "done": False,
            }
        ]
        client = MockVikunjaClient(tasks=existing_tasks)

        # Case A: Same link different title -> Deduped! (Already in task)
        item_same_link = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo",
            title="Different Title for Same Issue",
            translated_title="不同标题但相同链接",
            url="https://github.com/vlify/repo/issues/10",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        reconcile_with_vikunja([item_same_link], client=client, dry_run=False)
        self.assertEqual(item_same_link.status, "⟳已在待办")
        self.assertEqual(len(client.created_tasks), 0)

        # Case B: Same title (case-insensitive) different link with same platform prefix -> Deduped!
        item_case_title = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo",
            title="fix database deadlock",
            translated_title="fix database deadlock",
            url="https://github.com/vlify/repo/issues/999",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="assign",
            reason_cn="指派",
            is_actionable=True,
        )
        reconcile_with_vikunja([item_case_title], client=client, dry_run=False)
        self.assertEqual(item_case_title.status, "⟳已在待办")
        self.assertEqual(len(client.created_tasks), 0)

        # Case C: Same title on DIFFERENT platform -> NOT deduped!
        item_diff_platform = MorningItem(
            source="GitLab",
            repo_or_project="other/repo",
            title="Fix Database Deadlock",
            translated_title="修复数据库死锁",
            url="https://gitlab.com/other/repo/-/issues/10",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        reconcile_with_vikunja([item_diff_platform], client=client, dry_run=False)
        self.assertEqual(item_diff_platform.status, "✅已入待办")
        self.assertEqual(len(client.created_tasks), 1)

    # ----------------------------------------------------------------------
    # Edge 4: Translation boundaries
    # ----------------------------------------------------------------------
    def test_edge_translation_boundaries(self):
        """Translation boundaries: empty titles, empty LLM string, non-UTF8/malformed output, timeout."""
        # 1) Empty titles list -> Never calls LLM
        call_tracker = []
        llm = MockLLMRunner(output_text="", call_tracker=call_tracker)
        translate_items_batch([], llm)
        self.assertEqual(len(call_tracker), 0)

        # 2) LLM returns empty string -> Falls back to original title
        item1 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo",
            title="Original Title 1",
            url="https://github.com/vlify/repo/1",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        llm_empty = MockLLMRunner(output_text="")
        translate_items_batch([item1], llm_empty)
        self.assertEqual(item1.translated_title, "Original Title 1")

        # 3) LLM returns malformed non-JSON / gibberish -> Falls back to original
        item2 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo",
            title="Original Title 2",
            url="https://github.com/vlify/repo/2",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        llm_garbage = MockLLMRunner(output_text="SOME GIBBERISH THAT IS NOT JSON")
        translate_items_batch([item2], llm_garbage)
        self.assertEqual(item2.translated_title, "Original Title 2")

        # 4) LLM timeout -> Falls back to original
        item3 = MorningItem(
            source="GitHub",
            repo_or_project="vlify/repo",
            title="Original Title 3",
            url="https://github.com/vlify/repo/3",
            updated_at=datetime.datetime(2026, 9, 3, 8, 0),
            reason="mention",
            reason_cn="提及",
            is_actionable=True,
        )
        llm_timeout = MockLLMRunner(fail_with=LLMTimeoutError("Timeout"))
        translate_items_batch([item3], llm_timeout)
        self.assertEqual(item3.translated_title, "Original Title 3")

    # ----------------------------------------------------------------------
    # Edge 5: Delivery boundaries
    # ----------------------------------------------------------------------
    def test_edge_delivery_boundaries(self):
        """Delivery boundaries: Matrix 401, timeout, both fail."""
        cfg = {
            "morning": {
                "matrix": {
                    "homeserver_url": "https://matrix.org",
                    "room_id": "!room:matrix.org",
                    "token": "secret_token_val",
                }
            },
            "notifier": {
                "type": "himalaya",
                "account": "gmail",
                "to": "user@example.com",
            }
        }
        report = "Test report"

        # Matrix 401 triggers email fallback
        with patch("butler.matrix.send_matrix_message") as mock_matrix:
            mock_matrix.side_effect = MatrixDeliveryError("Matrix HTTP 401 Unauthorized")
            with patch("butler.digest.send_himalaya_email") as mock_email:
                deliver_morning_report(report, cfg)
                mock_email.assert_called_once()

        # Matrix timeout triggers email fallback
        with patch("butler.matrix.send_matrix_message") as mock_matrix:
            mock_matrix.side_effect = MatrixDeliveryError("Matrix Request timed out")
            with patch("butler.digest.send_himalaya_email") as mock_email:
                deliver_morning_report(report, cfg)
                mock_email.assert_called_once()

        # Both fail -> exits with exception, NO secret in exception
        with patch("butler.matrix.send_matrix_message") as mock_matrix:
            mock_matrix.side_effect = MatrixDeliveryError("Matrix error")
            with patch("butler.digest.send_himalaya_email") as mock_email:
                mock_email.side_effect = Exception("Himalaya error")
                with self.assertRaises(Exception) as cm:
                    deliver_morning_report(report, cfg)
                self.assertNotIn("secret_token_val", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
