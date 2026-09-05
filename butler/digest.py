#!/usr/bin/env python3
"""
digest -- Automated Daily Evening GTD & ActivityWatch Summary Report (Thin Entrypoint).

Thin entrypoint for systemd service and CLI invocation.
Orchestration and collection logic resides in butler.digest_app.
"""

import sys
import argparse
from pathlib import Path

# Ensure project root is in sys.path for direct CLI and systemd invocation
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.digest_app.collect import (
    get_weekday_str,
    format_completed_tasks,
    format_pending_tasks,
)
from butler.digest_app.aw_collector import (
    parse_duration_to_seconds,
    format_seconds_to_duration,
    build_hourly_timeline_events,
    query_activitywatch_breakdown,
)
from butler.digest_app.compose import build_digest_ai_insight, assemble_digest_report
from butler.digest_app.deliver import send_himalaya_email, save_backup_report
from butler.digest_app.engine import run_digest


def main():
    parser = argparse.ArgumentParser(description="Daily Evening GTD Summary Digest for Vikunja")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry-run simulation mode without sending notifications")
    parser.add_argument("-d", "--date", type=str, help="Target date in YYYY-MM-DD format (defaults to today)")
    parser.add_argument("-c", "--config", type=str, help="Path to configuration file")
    parser.add_argument("-e", "--env-file", type=str, help="Path to .env credentials file")
    args = parser.parse_args()

    exit_code = run_digest(
        dry_run=args.dry_run,
        target_date=args.date,
        config_path=args.config,
        env_path=args.env_file,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
