"""
Formatting and presentation logic for GTD morning brief report.
"""

from typing import List
from butler.morning_app.models import MorningItem


def format_morning_report(
    target_date: str,
    actionable_items: List[MorningItem],
    info_items: List[MorningItem],
    pending_tasks: List[str],
    gh_ok: bool,
    mail_ok: bool,
    vikunja_ok: bool,
) -> str:
    """
    Format morning brief report strictly conforming to SPEC §4.
    STRICT PROHIBITION OF MARKDOWN LISTS (- item, * item, 1. item).
    Items are separated by blank lines and indexed with bold numbers (**1)**).
    """
    sections: List[str] = [f"📬 **消息与待办 · {target_date}**"]

    # 1. Actionable items
    sections.append("📥 **需要行动**")
    if not gh_ok and not mail_ok:
        sections.append("不可用")
    elif not actionable_items:
        sections.append("无")
    else:
        act_lines = []
        for idx, item in enumerate(actionable_items, 1):
            time_str = item.updated_at.strftime("%H:%M") if item.updated_at else ""
            time_part = f"({time_str})" if time_str else ""
            line = f"**{idx})** [{item.source}] {item.repo_or_project} — {item.translated_title or item.title} {item.url}{time_part}{item.status}".strip()
            act_lines.append(line)
        sections.append("\n\n".join(act_lines))

    # 2. Pure info items
    sections.append("📰 **纯信息**")
    if not gh_ok and not mail_ok:
        sections.append("不可用")
    elif not info_items:
        sections.append("无")
    else:
        info_lines = []
        for idx, item in enumerate(info_items, 1):
            time_str = item.updated_at.strftime("%H:%M") if item.updated_at else ""
            time_part = f"({time_str})" if time_str else ""
            line = f"**{idx})** [{item.source}] {item.repo_or_project} — {item.translated_title or item.title} {item.url}{time_part}".strip()
            info_lines.append(line)
        sections.append("\n\n".join(info_lines))

    # 3. Current pending tasks
    sections.append("📋 **当前未完成待办**")
    if not vikunja_ok:
        sections.append("不可用")
    elif not pending_tasks:
        sections.append("无")
    else:
        pending_lines = []
        for idx, task_title in enumerate(pending_tasks, 1):
            pending_lines.append(f"**{idx})** {task_title}")
        sections.append("\n\n".join(pending_lines))

    return "\n\n".join(sections).strip() + "\n"
