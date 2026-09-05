"""
Core orchestration engine for morning news collection, reconciliation, and delivery.
"""

import sys
import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional

from butler.config import load_config
from butler.vikunja_client import VikunjaClient
from butler.llm import create_llm_provider, LLMProvider
from butler.matrix import deliver_morning_report
from butler.morning_app.models import MorningItem
from butler.morning_app.collect import fetch_github_notifications, fetch_gmail_messages
from butler.morning_app.translate import translate_items_batch
from butler.morning_app.reconcile import reconcile_with_vikunja
from butler.morning_app.compose import format_morning_report


def run_morning(
    dry_run: bool = False,
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    target_date: Optional[str] = None,
    client: Optional[VikunjaClient] = None,
    llm: Optional[LLMProvider] = None,
) -> int:
    """
    Main orchestration entrypoint for the morning news and reconciliation report.
    Returns exit code (0 on success or graceful degradation, non-zero on delivery failure).
    """
    cfg = load_config(config_path=config_path, env_path=env_path)
    tz_name = cfg.get("timezone", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if not target_date:
        target_date = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    morning_cfg = cfg.get("morning", {})
    gh_cfg = morning_cfg.get("github", {})
    mail_cfg = morning_cfg.get("gmail", {})
    target_project_id = morning_cfg.get("target_project_id", 1)

    # 1. Fetch GitHub notifications
    gh_items: List[MorningItem] = []
    gh_ok = True
    if gh_cfg.get("enabled", True):
        gh_bin = gh_cfg.get("gh_bin", "/usr/bin/gh")
        gh_items, gh_ok = fetch_github_notifications(gh_bin=gh_bin, tz_name=tz_name)

    # 2. Fetch Gmail messages
    mail_items: List[MorningItem] = []
    mail_ok = True
    if mail_cfg.get("enabled", True):
        himalaya_bin = mail_cfg.get("himalaya_bin", "himalaya")
        account = mail_cfg.get("account", "gmail")
        mail_items, mail_ok = fetch_gmail_messages(
            himalaya_bin=himalaya_bin,
            account=account,
            tz_name=tz_name,
        )

    all_items = gh_items + mail_items

    # 3. Translate titles in a single batch LLM call
    if llm is None and cfg.get("llm"):
        llm = create_llm_provider(cfg.get("llm", {}))

    translate_items_batch(all_items, llm)

    # Separate actionable vs info-only
    actionable_items = [item for item in all_items if item.is_actionable]
    info_items = [item for item in all_items if not item.is_actionable]

    # 4. Reconcile with Vikunja
    if client is None:
        v_cfg = cfg.get("vikunja", {})
        client = VikunjaClient(
            base_url=v_cfg.get("url"),
            token=v_cfg.get("token"),
            timeout=v_cfg.get("timeout_seconds", 30),
        )

    pending_tasks, vikunja_ok = reconcile_with_vikunja(
        actionable_items=actionable_items,
        client=client,
        target_project_id=target_project_id,
        dry_run=dry_run,
    )

    # 5. Format report
    report = format_morning_report(
        target_date=target_date,
        actionable_items=actionable_items,
        info_items=info_items,
        pending_tasks=pending_tasks,
        gh_ok=gh_ok,
        mail_ok=mail_ok,
        vikunja_ok=vikunja_ok,
    )

    # 6. Deliver report or print (dry-run)
    if dry_run:
        print(report)
        return 0

    try:
        deliver_morning_report(report, cfg)
        print("[OK] Morning report delivered successfully.")
        return 0
    except Exception as e:
        print(f"[FAIL] Notification delivery failed: {e}", file=sys.stderr)
        return 1
