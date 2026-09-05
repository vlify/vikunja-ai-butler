"""
Core orchestration engine for daily evening GTD and ActivityWatch summary digest.
"""

import os
import sys
import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from butler.config import load_config
from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm import create_llm_provider, LLMProvider
from butler.digest_app.collect import (
    get_weekday_str,
    format_completed_tasks,
    format_pending_tasks,
)
from butler.digest_app.aw_collector import query_activitywatch_breakdown
from butler.digest_app.compose import build_digest_ai_insight, assemble_digest_report
from butler.digest_app.deliver import send_himalaya_email, save_backup_report


def run_digest(
    dry_run: bool = False,
    target_date: Optional[str] = None,
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    client: Optional[VikunjaClient] = None,
    llm: Optional[LLMProvider] = None,
) -> int:
    """
    Execute evening digest generation cycle.
    """
    cfg = load_config(config_path=config_path, env_path=env_path)
    tz_name = cfg.get("timezone", "Asia/Shanghai")

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if not target_date:
        target_date = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    try:
        dt_obj = datetime.datetime.strptime(target_date, "%Y-%m-%d")
        weekday_str = get_weekday_str(dt_obj)
    except Exception:
        weekday_str = ""

    report_title = f"🌙 晚间总结 · {target_date}({weekday_str})" if weekday_str else f"🌙 晚间总结 · {target_date}"

    # Prepare Vikunja client
    if client is None:
        v_cfg = cfg.get("vikunja", {})
        client = VikunjaClient(
            base_url=v_cfg.get("url"),
            token=v_cfg.get("token"),
            timeout=v_cfg.get("timeout_seconds", 30),
        )

    # 1. Fetch Vikunja tasks
    all_tasks: List[Dict[str, Any]] = []
    done_tasks: List[Dict[str, Any]] = []
    try:
        all_tasks = client.get_tasks()
        done_tasks = client.get_done_tasks()
    except VikunjaAPIError as e:
        print(f"[WARN] Failed to fetch tasks from Vikunja: {e}", file=sys.stderr)

    # 2. Format Completed Tasks
    completed_section, completed_count = format_completed_tasks(
        done_tasks=done_tasks,
        target_date_str=target_date,
        tz_name=tz_name,
    )

    # 3. Format Pending Tasks
    gtd_cfg = cfg.get("gtd", {})
    allowed_projects = gtd_cfg.get("allowed_target_projects", {
        1: "收件箱 (Inbox)",
        5: "单次执行清单",
        6: "项目执行清单",
        7: "等待回执清单",
    })
    # Ensure Inbox is also included in allowed for display
    inbox_pid = gtd_cfg.get("inbox_project_id", 1)
    if inbox_pid not in allowed_projects:
        allowed_projects[inbox_pid] = "收件箱 (Inbox)"

    project_order = gtd_cfg.get("digest_project_order", [inbox_pid, 5, 6, 7])

    pending_section = format_pending_tasks(
        tasks=all_tasks,
        allowed_projects=allowed_projects,
        project_order=project_order,
    )

    # 4. ActivityWatch Screen Time
    aw_cfg = cfg.get("activitywatch", {})
    aw_section = query_activitywatch_breakdown(aw_cfg, target_date=target_date, tz_name=tz_name)

    # 5. LLM AI Insight
    if llm is None and cfg.get("llm"):
        llm = create_llm_provider(cfg.get("llm", {}))

    ai_section = build_digest_ai_insight(
        llm=llm,
        target_date=target_date,
        completed_section=completed_section,
        aw_section=aw_section,
    )

    # Assemble complete report — 晚间总结仅列已完成,不含待办
    full_report = assemble_digest_report(
        report_title=report_title,
        completed_section=completed_section,
        aw_section=aw_section,
        ai_section=ai_section,
    )

    # Backup report to file if configured
    backup_cfg = cfg.get("backup", {})
    backup_path = os.environ.get("BACKUP_FILE") or backup_cfg.get("file_path")
    save_backup_report(full_report, backup_path)

    # Output or Deliver
    if dry_run or cfg.get("notifier", {}).get("type") == "none":
        print(full_report)
        return 0

    notifier_cfg = cfg.get("notifier", {})
    n_type = notifier_cfg.get("type")
    if n_type == "himalaya":
        h_nested = notifier_cfg.get("himalaya", {}) if isinstance(notifier_cfg.get("himalaya"), dict) else {}
        mail_to = (
            notifier_cfg.get("to")
            or notifier_cfg.get("mail_to")
            or h_nested.get("to")
            or h_nested.get("mail_to")
            or ""
        )
        account = notifier_cfg.get("account") or h_nested.get("account") or "default"
        try:
            send_himalaya_email(notifier_cfg, subject=report_title, body=full_report)
            print(f"[OK] Daily digest successfully sent via Himalaya to {mail_to} (account: {account})")
            return 0
        except Exception as e:
            print(f"[FAIL] Notification delivery failed: {e}", file=sys.stderr)
            if backup_path:
                print(f"[INFO] Report is safely preserved at backup file: {backup_path}", file=sys.stderr)
            return 1
    else:
        print(full_report)

    return 0
