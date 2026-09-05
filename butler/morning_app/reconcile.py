"""
Vikunja task reconciliation and deduplication for morning actionable items.
"""

import re
import sys
from typing import List, Optional, Tuple

from butler.morning_app.models import MorningItem, normalize_title_for_dedup
from butler.vikunja_client import VikunjaClient, VikunjaAPIError


def reconcile_with_vikunja(
    actionable_items: List[MorningItem],
    client: Optional[VikunjaClient],
    target_project_id: int = 1,
    dry_run: bool = False,
) -> Tuple[List[str], bool]:
    """
    Reconcile actionable items against existing Vikunja tasks.
    Deduplication rules:
    - Same platform prefix + same title (case-insensitive) -> ⟳已在待办
    - Same description/source link -> ⟳已在待办
    - Otherwise -> creates task (intercepted if dry_run) and marks ✅已入待办
    Returns (pending_task_titles, success).
    """
    if client is None:
        for item in actionable_items:
            item.status = "✅已入待办" if not dry_run else "✅已入待办(DRY-RUN,未实际创建)"
        return [], False

    try:
        tasks = client.get_tasks()
    except VikunjaAPIError as e:
        print(f"[WARN] Failed to fetch tasks from Vikunja: {e}", file=sys.stderr)
        return [], False

    # Extract pending (not done) tasks
    pending_tasks: List[str] = []
    existing_items: List[Tuple[str, str, str]] = []  # (platform_lower, normalized_title, description)

    for t in tasks:
        if not isinstance(t, dict):
            continue
        if not t.get("done", False):
            title = str(t.get("title", "")).strip()
            desc = str(t.get("description", "")).strip()
            if title:
                pending_tasks.append(title)

            m = re.match(r"^\[([a-zA-Z0-9_\-]+)\]", title)
            platform = m.group(1).lower() if m else ""
            norm_title = normalize_title_for_dedup(title)
            existing_items.append((platform, norm_title, desc))

    # Reconcile each actionable item
    for item in actionable_items:
        item_platform = item.source.lower()
        item_norm_title = normalize_title_for_dedup(item.title)
        item_norm_translated = normalize_title_for_dedup(item.translated_title or item.title)
        item_url = item.url.strip()

        is_dup = False
        for ex_platform, ex_norm_title, ex_desc in existing_items:
            # 1) Link match
            if item_url and item_url in ex_desc:
                is_dup = True
                break
            # 2) Same platform prefix AND same title (case-insensitive)
            if item_platform == ex_platform and (item_norm_title == ex_norm_title or item_norm_translated == ex_norm_title):
                is_dup = True
                break

        if is_dup:
            item.status = "⟳已在待办"
        else:
            task_title = f"[{item.source}] {item.translated_title or item.title}".strip()
            task_desc = item.url.strip()

            if dry_run:
                print(f"[DRY-RUN] Would create Vikunja task in project {target_project_id}: '{task_title}' ({task_desc})")
                item.status = "✅已入待办(DRY-RUN,未实际创建)"
            else:
                try:
                    client.create_task(
                        project_id=target_project_id,
                        title=task_title,
                        description=task_desc,
                    )
                    item.status = "✅已入待办"
                except Exception as e:
                    print(f"[WARN] Failed to create Vikunja task '{task_title}': {e}", file=sys.stderr)
                    item.status = "创建失败"

    return pending_tasks, True
