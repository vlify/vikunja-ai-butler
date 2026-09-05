"""
Daily Evening GTD and ActivityWatch Summary Digest Application Package.
"""

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

__all__ = [
    "get_weekday_str",
    "format_completed_tasks",
    "format_pending_tasks",
    "parse_duration_to_seconds",
    "format_seconds_to_duration",
    "build_hourly_timeline_events",
    "query_activitywatch_breakdown",
    "build_digest_ai_insight",
    "assemble_digest_report",
    "send_himalaya_email",
    "save_backup_report",
    "run_digest",
]
