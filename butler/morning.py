#!/usr/bin/env python3
"""
morning -- Automated Morning News and Task Reconciliation Module (Thin Entrypoint).

Thin entrypoint for systemd service and CLI invocation.
Orchestration, collection, translation, and reconciliation logic resides in butler.morning_app.
"""

import sys
import argparse
from pathlib import Path

# Ensure project root is in sys.path for direct CLI and systemd invocation
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.morning_app.models import (
    MorningItem,
    GITHUB_REASON_MAP,
    ACTIONABLE_REASONS,
    normalize_title_for_dedup,
    parse_date_flexible,
)
from butler.morning_app.collect import (
    parse_github_tsv_line,
    fetch_github_notifications,
    parse_gmail_envelope,
    fetch_gmail_messages,
)
from butler.morning_app.translate import translate_items_batch
from butler.morning_app.reconcile import reconcile_with_vikunja
from butler.morning_app.compose import format_morning_report
from butler.morning_app.engine import run_morning


def main():
    parser = argparse.ArgumentParser(description="Morning News and Task Reconciliation for Vikunja")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry-run simulation mode without creating tasks or delivering notifications")
    parser.add_argument("-d", "--date", type=str, help="Target date in YYYY-MM-DD format (defaults to today)")
    parser.add_argument("-c", "--config", type=str, help="Path to configuration file")
    parser.add_argument("-e", "--env-file", type=str, help="Path to .env credentials file")
    args = parser.parse_args()

    exit_code = run_morning(
        dry_run=args.dry_run,
        config_path=args.config,
        env_path=args.env_file,
        target_date=args.date,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
