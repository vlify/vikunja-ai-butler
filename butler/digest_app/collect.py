"""
GTD task formatting and date helpers for evening digest report.
"""

import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Tuple


def get_weekday_str(dt: datetime.datetime) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return weekdays[dt.weekday()]


def format_completed_tasks(
    done_tasks: List[Dict[str, Any]],
    target_date_str: str,
    tz_name: str = "Asia/Shanghai",
) -> Tuple[str, int]:
    """
    Filter and format tasks completed on target_date_str in specified timezone.
    Returns (markdown_text, count).
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    completed_today: List[Tuple[datetime.datetime, str, str]] = []

    for t in done_tasks:
        if not isinstance(t, dict):
            continue
        done_at_raw = t.get("done_at")
        if not done_at_raw or not isinstance(done_at_raw, str):
            continue

        try:
            iso_str = done_at_raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            dt_local = dt.astimezone(tz)

            if dt_local.strftime("%Y-%m-%d") == target_date_str:
                title = t.get("title", "未命名任务").strip()
                time_str = dt_local.strftime("%H:%M")
                completed_today.append((dt_local, title, time_str))
        except Exception:
            continue

    completed_today.sort(key=lambda x: x[0])
    count = len(completed_today)

    lines = [f"## ✅ 今日已完成 ({count} 条)"]
    if count == 0:
        lines.append("今日暂无完成记录——一份清单的意义在于划掉它")
    else:
        for _, title, time_str in completed_today:
            lines.append(f"- [x] {title} (完成于 {time_str})")

    return "\n".join(lines), count


def format_pending_tasks(
    tasks: List[Dict[str, Any]],
    allowed_projects: Dict[int, str],
    project_order: List[int],
) -> str:
    """
    Filter and format active GTD pending tasks grouped by project.
    Preserves hierarchical subtask relationships (indents subtasks under parent).
    """
    task_map: Dict[int, Dict[str, Any]] = {
        t["id"]: t for t in tasks if isinstance(t, dict) and "id" in t
    }
    parent_to_children: Dict[int, List[int]] = {}
    child_to_parent: Dict[int, int] = {}

    for t in tasks:
        if not isinstance(t, dict) or t.get("done", False):
            continue
        tid = t.get("id")
        if not tid:
            continue
        related = t.get("related_tasks") or {}
        parents = related.get("parenttask") or []
        if parents and isinstance(parents, list):
            p_id = parents[0].get("id")
            if p_id and p_id in task_map:
                child_to_parent[tid] = p_id
                parent_to_children.setdefault(p_id, []).append(tid)

    lines = ["## 📋 今日待办"]
    has_any_pending = False

    for pid in project_order:
        pname = allowed_projects.get(pid)
        if not pname:
            continue
        proj_tasks = [
            t for t in tasks
            if isinstance(t, dict) and not t.get("done", False) and t.get("project_id") == pid
        ]
        if not proj_tasks:
            continue

        has_any_pending = True
        lines.append(f"\n### {pname}")

        for t in proj_tasks:
            tid = t.get("id")
            if tid in child_to_parent:
                # Subtask will be rendered nested under its parent
                continue
            title = t.get("title", "未命名任务").strip()
            lines.append(f"- [ ] {title}")

            # Render child subtasks indented
            if tid in parent_to_children:
                for cid in parent_to_children[tid]:
                    child = task_map.get(cid)
                    if child and not child.get("done", False):
                        c_title = child.get("title", "未命名任务").strip()
                        lines.append(f"  - [ ] {c_title}")

    if not has_any_pending:
        return "## 📋 今日待办\n\n（今日无未完成待办事项）"

    return "\n".join(lines)
