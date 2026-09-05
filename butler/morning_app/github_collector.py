"""
GitHub notifications collector via gh CLI API.
"""

import os
import sys
import shutil
import datetime
import subprocess
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple

from butler.morning_app.models import (
    MorningItem,
    GITHUB_REASON_MAP,
    ACTIONABLE_REASONS,
)


def parse_github_tsv_line(line: str, tz_name: str = "Asia/Shanghai") -> Optional[MorningItem]:
    """
    Tolerantly parse a single TSV line from GitHub notifications output:
    [updated_at, reason, repo_full_name, title, url]
    Handles extra tabs in title, quotes, backslashes, mixed Chinese/English,
    unknown reasons, missing columns, and invalid timestamps.
    """
    if not line or not line.strip():
        return None

    parts = line.strip().split("\t")
    if len(parts) < 5:
        return None

    updated_at_raw = parts[0].strip()
    reason_raw = parts[1].strip()
    repo_raw = parts[2].strip()
    url_raw = parts[-1].strip()
    title_raw = "\t".join(parts[3:-1]).strip() if len(parts) > 5 else parts[3].strip()

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if updated_at_raw:
        try:
            iso_str = updated_at_raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            dt_obj = dt.astimezone(tz)
        except Exception:
            dt_obj = datetime.datetime.now(tz)
    else:
        dt_obj = datetime.datetime.now(tz)

    reason_cn = GITHUB_REASON_MAP.get(reason_raw, reason_raw or "未知")
    is_actionable = reason_raw in ACTIONABLE_REASONS

    return MorningItem(
        source="GitHub",
        repo_or_project=repo_raw,
        title=title_raw,
        url=url_raw,
        updated_at=dt_obj,
        reason=reason_raw,
        reason_cn=reason_cn,
        is_actionable=is_actionable,
    )


def fetch_github_notifications(
    gh_bin: str = "/usr/bin/gh",
    tz_name: str = "Asia/Shanghai",
) -> Tuple[List[MorningItem], bool]:
    """
    Fetch GitHub notifications using the read-only gh CLI api command.
    Enforces 'env -u GIT_DIR -u GIT_WORK_TREE' isolation.
    Returns (items, success).
    """
    bin_path = gh_bin if os.path.isabs(gh_bin) and os.path.exists(gh_bin) else shutil.which("gh") or "/usr/bin/gh"

    cmd = (
        f"env -u GIT_DIR -u GIT_WORK_TREE {bin_path} api '/notifications?per_page=30' "
        f"--jq '.[] | [.updated_at, .reason, .repository.full_name, .subject.title, "
        f"(.subject.url | sub(\"api.github.com/repos/\"; \"github.com/\") | sub(\"/pulls/\"; \"/pull/\"))] | @tsv'"
    )

    try:
        res = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except Exception as e:
        print(f"[WARN] Failed to execute GitHub notification command: {e}", file=sys.stderr)
        return [], False

    if res.returncode != 0:
        print(f"[WARN] GitHub notifications returned non-zero code {res.returncode}: {res.stderr.strip()}", file=sys.stderr)
        return [], False

    items: List[MorningItem] = []
    for line in res.stdout.splitlines():
        item = parse_github_tsv_line(line, tz_name=tz_name)
        if item:
            items.append(item)

    return items, True
