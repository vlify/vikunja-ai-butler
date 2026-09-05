"""
Data models, constants, and parsing utilities for morning news and tasks.
"""

import re
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional, Set
from email.utils import parsedate_to_datetime

# Reason mapping according to SPEC §2
GITHUB_REASON_MAP: Dict[str, str] = {
    "mention": "提及",
    "review_requested": "请求审查",
    "subscribe": "订阅",
    "assign": "指派",
    "team_mention": "团队提及",
    "author": "作者动态",
    "comment": "评论",
    "ci_activity": "CI",
}

# Actionable reasons: review_requested, mention, assign, team_mention
ACTIONABLE_REASONS: Set[str] = {"review_requested", "mention", "assign", "team_mention"}


class MorningItem:
    """Represents a single morning news or notification item."""
    def __init__(
        self,
        source: str,
        repo_or_project: str,
        title: str,
        url: str,
        updated_at: Optional[datetime.datetime] = None,
        reason: str = "",
        reason_cn: str = "",
        is_actionable: bool = False,
        translated_title: Optional[str] = None,
        status: str = "",
    ):
        self.source = source  # "GitHub" | "GitLab" | "Greasyfork" | "Codeberg" | "Email"
        self.repo_or_project = repo_or_project
        self.title = title
        self.url = url
        self.updated_at = updated_at
        self.reason = reason
        self.reason_cn = reason_cn
        self.is_actionable = is_actionable
        self.translated_title = translated_title or title
        self.status = status  # "✅已入待办" | "⟳已在待办" | ""

    def __repr__(self) -> str:
        return f"<MorningItem [{self.source}] {self.repo_or_project} - {self.title} ({self.status})>"


def normalize_title_for_dedup(title: str) -> str:
    """Normalize title for fuzzy/case-insensitive matching."""
    t = title.strip().lower()
    # Remove leading platform prefix like [github], [gitlab]
    t = re.sub(r"^\[[a-z0-9_\-]+\]\s*", "", t)
    # Remove excessive whitespaces
    t = re.sub(r"\s+", " ", t)
    return t


def parse_date_flexible(date_str: str, tz: ZoneInfo) -> Optional[datetime.datetime]:
    """Parse various mail date formats into target timezone."""
    date_str = date_str.strip()
    if not date_str:
        return None

    # Try DD/MM/YYYY HH:MM
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            pass

    # Try RFC 2822
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(tz)
    except Exception:
        pass

    # Try ISO
    try:
        dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except Exception:
        pass

    return None
