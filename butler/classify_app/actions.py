"""
Action executor for GTD classification plan mutations and dry-run simulations.
"""

import sys
from typing import Any, Dict, List, Set
from butler.vikunja_client import VikunjaClient, VikunjaAPIError


def execute_plan(
    plan: List[Dict[str, Any]],
    inbox_tasks: List[Dict[str, Any]],
    client: VikunjaClient,
    dry_run: bool = False,
) -> int:
    """
    Execute the validated classification plan or output dry-run actions.
    Returns 0 on complete success, 1 on failure.
    """
    print("=" * 72)
    if dry_run:
        print(" [DRY-RUN] Classification & optimization plan generated (No API mutations executed)")
    else:
        print(" [EXECUTE] Executing task moves, attachments, refinements, and subtask spawns...")
    print("=" * 72)

    inbox_handled_ids: Set[int] = set()
    active_mutations_count = 0
    inbox_ids = {t["id"] for t in inbox_tasks}

    for item in plan:
        t_id = item["id"]
        t_title = item["title"]
        action = item.get("action", "move")
        pid = item["project_id"]
        pname = item["project_name"]
        parent_id = item.get("parent_task_id")
        parent_title = item.get("parent_title")
        exp_title = item.get("expanded_title")
        exp_desc = item.get("expanded_description")
        subtasks = item.get("subtasks") or []

        if t_id in inbox_ids:
            inbox_handled_ids.add(t_id)
        else:
            active_mutations_count += 1

        if action == "attach":
            if dry_run:
                if exp_title:
                    print(f"-> [PLAN ATTACH+EXPAND] Task #{t_id} [{t_title}] => subtask of #{parent_id} [{parent_title}] in [{pname} (ID: {pid})]")
                    print(f"   Expanded title: [{t_title}] -> [{exp_title}]")
                    print(f"   Expanded desc:  {exp_desc}")
                else:
                    print(f"-> [PLAN ATTACH] Task #{t_id} [{t_title}] => subtask of #{parent_id} [{parent_title}] in [{pname} (ID: {pid})]")
            else:
                try:
                    client.update_task(
                        task_id=t_id,
                        project_id=pid,
                        parent_task_id=parent_id,
                        title=exp_title,
                        description=exp_desc,
                    )
                except VikunjaAPIError as e:
                    err_msg = str(e)
                    # 409 Idempotency: HTTP 409 / code 4008 ("The task relation already exists")
                    # means relation is already established; goal achieved, skip and continue.
                    if ("4008" in err_msg or "409" in err_msg) and "relation already exists" in err_msg.lower():
                        print(f"[SKIP] Task #{t_id} already attached to parent #{parent_id} (relation already exists)")
                    else:
                        print(f"[FAIL] Failed to attach task #{t_id} to parent #{parent_id}: {e}", file=sys.stderr)
                        return 1
                else:
                    if exp_title:
                        print(f"[OK] [ATTACHED+EXPANDED] Task #{t_id} [{t_title}] -> [{exp_title}] => subtask of #{parent_id} [{parent_title}] in [{pname} (ID: {pid})]")
                    else:
                        print(f"[OK] [ATTACHED] Task #{t_id} [{t_title}] => subtask of #{parent_id} [{parent_title}] in [{pname} (ID: {pid})]")

        elif action == "spawn":
            if dry_run:
                print(f"-> [PLAN SPAWN] Parent #{t_id} [{t_title}] in [{pname} (ID: {pid})] => Spawn {len(subtasks)} subtasks:")
                for st in subtasks:
                    print(f"     + Subtask: [{st}]")
            else:
                try:
                    # Note on repeated writes (e.g. spawn subtasks with duplicate names):
                    # Vikunja API allows tasks with duplicate titles without returning 409.
                    # Per SPEC minimal-diff principle,现状保持 fail-closed,不在此顺手扩大改动面。
                    for st in subtasks:
                        client.create_task(
                            project_id=pid,
                            title=st,
                            parent_task_id=t_id,
                        )
                    print(f"[OK] [SPAWNED] Parent #{t_id} [{t_title}] => Created {len(subtasks)} subtasks in [{pname} (ID: {pid})]")
                except VikunjaAPIError as e:
                    print(f"[FAIL] Failed to spawn subtasks for task #{t_id}: {e}", file=sys.stderr)
                    return 1

        elif action == "refine":
            if dry_run:
                print(f"-> [PLAN REFINE] Task #{t_id} [{t_title}] -> [{exp_title}] in [{pname} (ID: {pid})]")
                print(f"   Refined desc:  {exp_desc}")
            else:
                try:
                    client.update_task(
                        task_id=t_id,
                        title=exp_title,
                        description=exp_desc,
                    )
                    print(f"[OK] [REFINED] Task #{t_id} [{t_title}] -> [{exp_title}] in [{pname} (ID: {pid})]")
                except VikunjaAPIError as e:
                    print(f"[FAIL] Failed to refine task #{t_id}: {e}", file=sys.stderr)
                    return 1

        else:
            if dry_run:
                if exp_title and subtasks:
                    print(f"-> [PLAN MOVE+EXPAND+SPAWN] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})]")
                    print(f"   Expanded title: [{t_title}] -> [{exp_title}]")
                    print(f"   Expanded desc:  {exp_desc}")
                    for st in subtasks:
                        print(f"     + Subtask: [{st}]")
                elif exp_title:
                    print(f"-> [PLAN MOVE+EXPAND] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})]")
                    print(f"   Expanded title: [{t_title}] -> [{exp_title}]")
                    print(f"   Expanded desc:  {exp_desc}")
                elif subtasks:
                    print(f"-> [PLAN MOVE+SPAWN] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})]")
                    for st in subtasks:
                        print(f"     + Subtask: [{st}]")
                else:
                    print(f"-> [PLAN MOVE] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})]")
            else:
                try:
                    client.update_task(
                        task_id=t_id,
                        project_id=pid,
                        title=exp_title,
                        description=exp_desc,
                    )
                except VikunjaAPIError as e:
                    print(f"[FAIL] Failed to update task #{t_id}: {e}", file=sys.stderr)
                    return 1

                if subtasks:
                    for s_idx, st in enumerate(subtasks):
                        try:
                            client.create_task(
                                project_id=pid,
                                title=st,
                                parent_task_id=t_id,
                            )
                        except VikunjaAPIError as e:
                            print(f"[FAIL] Failed to create subtask {s_idx+1}/{len(subtasks)} for task #{t_id}: {e}", file=sys.stderr)
                            return 1

                if exp_title and subtasks:
                    print(f"[OK] [MOVED+EXPANDED+SPAWNED] Task #{t_id} [{t_title}] -> [{exp_title}] => [{pname} (ID: {pid})] with {len(subtasks)} subtasks")
                elif exp_title:
                    print(f"[OK] [MOVED+EXPANDED] Task #{t_id} [{t_title}] -> [{exp_title}] => [{pname} (ID: {pid})]")
                elif subtasks:
                    print(f"[OK] [MOVED+SPAWNED] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})] with {len(subtasks)} subtasks")
                else:
                    print(f"[OK] [MOVED] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})]")

    remaining_inbox_count = len(inbox_tasks) - len(inbox_handled_ids)
    for t in inbox_tasks:
        if t["id"] not in inbox_handled_ids:
            print(f"[KEPT] [RETAINED INBOX] Task #{t['id']} [{t['title']}]")

    print("=" * 72)
    print("Classification & Optimization Summary:")
    print(f"  Inbox Tasks Handled: {len(inbox_handled_ids)}/{len(inbox_tasks)} (Retained: {remaining_inbox_count})")
    print(f"  Active Tasks Optimized: {active_mutations_count}")
    print("=" * 72)

    return 0
