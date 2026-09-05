#!/usr/bin/env python3
"""
inbox-classify -- Automated GTD Inbox Classifier (Thin Entrypoint).

Thin entrypoint for systemd service and CLI invocation.
Orchestration and validation logic resides in butler.classify_app.
"""

import sys
import argparse
from pathlib import Path

# Ensure project root is in sys.path for direct CLI and systemd invocation
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.classify_app.prompts import build_prompt
from butler.classify_app.validator import parse_and_validate_llm_output
from butler.classify_app.actions import execute_plan
from butler.classify_app.engine import run_classify


def main():
    parser = argparse.ArgumentParser(description="Automated GTD Inbox Classifier for Vikunja")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry-run simulation mode without modifying tasks")
    parser.add_argument("-c", "--config", type=str, help="Path to configuration file")
    parser.add_argument("-e", "--env-file", type=str, help="Path to .env credentials file")
    args = parser.parse_args()

    exit_code = run_classify(
        dry_run=args.dry_run,
        config_path=args.config,
        env_path=args.env_file,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
