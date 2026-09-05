"""
Validation and fail-closed security checks for LLM classification output.
"""

import re
import json
from typing import Any, Dict, List, Optional, Set


def _clean_and_validate_subtasks(subtasks_raw: Any, task_id: int) -> List[str]:
    if not isinstance(subtasks_raw, list) or not subtasks_raw:
        raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} requires non-empty list of subtasks: {subtasks_raw}")
    if len(subtasks_raw) > 3:
        raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} max 3 subtasks allowed per task, got {len(subtasks_raw)}.")

    cleaned = []
    for s_idx, s in enumerate(subtasks_raw):
        if not isinstance(s, str) or not s.strip():
            raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} subtask #{s_idx+1} is empty or not a string.")
        cleaned.append(s.strip())
    return cleaned


def parse_and_validate_llm_output(
    raw_text: str,
    inbox_tasks: List[Dict[str, Any]],
    allowed_projects: Dict[int, str],
    forbidden_projects: Set[int],
    parent_candidates: Optional[List[Dict[str, Any]]] = None,
    active_tasks: Optional[List[Dict[str, Any]]] = None,
    max_active_mutations: int = 5,
) -> List[Dict[str, Any]]:
    """
    Enforce strict fail-closed validation on LLM JSON output.
    Raises ValueError with '[FAIL-CLOSED]' prefix if any constraint is violated.
    """
    text = raw_text.strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    json_str = match.group(0) if match else text

    try:
        data = json.loads(json_str)
    except Exception as e:
        raise ValueError(f"[FAIL-CLOSED] LLM output is not valid JSON: {e}\nRaw output:\n{raw_text}")

    if not isinstance(data, list):
        raise ValueError(f"[FAIL-CLOSED] Root JSON structure is not an array (list): {type(data)}")

    inbox_map = {t["id"]: t["title"] for t in inbox_tasks}
    active_map = {t["id"]: t for t in (active_tasks or [])}
    all_valid_map = {**{tid: {"id": tid, "title": title, "project_id": None} for tid, title in inbox_map.items()}, **active_map}
    valid_parent_map = {p["id"]: p for p in (parent_candidates or [])}
    inbox_ids = set(inbox_map.keys())

    plan: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()
    active_mutations_count = 0

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} is not a dict: {item}")

        # Rule 1: done status is strictly forbidden in mutation payload
        if "done" in item:
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} contains forbidden 'done' field: {item}")

        task_id = item.get("id")
        if task_id is None:
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} missing 'id': {item}")

        try:
            task_id = int(task_id)
        except (ValueError, TypeError):
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} 'id' is not a valid integer: {item}")

        if task_id not in all_valid_map:
            raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} does not belong to inbox or active tasks.")

        if task_id in seen_ids:
            raise ValueError(f"[FAIL-CLOSED] Duplicate task ID {task_id} in LLM output.")

        # Determine action (default 'move' for backward compatibility)
        action = item.get("action")
        if action is None:
            action = "attach" if item.get("parent_task_id") is not None else "move"

        if action not in ("move", "attach", "spawn", "refine"):
            raise ValueError(
                f"[FAIL-CLOSED] Item #{idx+1} invalid action '{action}'. Only 'move', 'attach', 'spawn', 'refine' allowed."
            )

        seen_ids.add(task_id)
        task_info = all_valid_map[task_id]
        orig_title = task_info.get("title", "")
        curr_pid = task_info.get("project_id")

        if task_id not in inbox_ids:
            active_mutations_count += 1
            if active_mutations_count > max_active_mutations:
                raise ValueError(
                    f"[FAIL-CLOSED] Batch quota exceeded: max {max_active_mutations} active task mutations allowed, got {active_mutations_count}."
                )

        target_pid = None
        pname = ""
        parent_id = None
        parent_title = None
        spawn_subtasks: Optional[List[str]] = None

        if action == "move":
            target_pid = item.get("project_id")
            if target_pid is None:
                raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} missing 'project_id' for 'move' action: {item}")
            try:
                target_pid = int(target_pid)
            except (ValueError, TypeError):
                raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} 'project_id' is not a valid integer: {item}")

            if target_pid in forbidden_projects:
                raise ValueError(f"[FAIL-CLOSED] Target project_id {target_pid} is explicitly forbidden.")

            if target_pid not in allowed_projects:
                raise ValueError(f"[FAIL-CLOSED] Target project_id {target_pid} is not in allowed whitelist: {list(allowed_projects.keys())}")

            pname = allowed_projects[target_pid]

            # Compound move: optionally allow subtasks (e.g. for projects)
            if "subtasks" in item and item["subtasks"] is not None:
                spawn_subtasks = _clean_and_validate_subtasks(item["subtasks"], task_id)

        elif action == "attach":
            if "subtasks" in item and item["subtasks"] is not None:
                raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} 'attach' action must NOT provide 'subtasks': {item}")

            parent_id = item.get("parent_task_id")
            if parent_id is None:
                raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} missing 'parent_task_id' for 'attach' action: {item}")
            try:
                parent_id = int(parent_id)
            except (ValueError, TypeError):
                raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} 'parent_task_id' is not a valid integer: {item}")

            if "project_id" in item and item["project_id"] is not None:
                raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} 'attach' action must NOT provide 'project_id': {item}")

            if parent_id in inbox_ids:
                raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} parent_task_id {parent_id} cannot be an inbox task itself.")

            if parent_id == task_id:
                raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} cannot be a subtask of itself.")

            if parent_id not in valid_parent_map:
                raise ValueError(f"[FAIL-CLOSED] Parent task ID {parent_id} is not in candidate parent tasks.")

            parent_info = valid_parent_map[parent_id]
            target_pid = parent_info.get("project_id")
            if target_pid is None:
                raise ValueError(f"[FAIL-CLOSED] Parent task ID {parent_id} has no valid project_id.")
            parent_title = parent_info.get("title", "")
            pname = parent_info.get("project_name", allowed_projects.get(target_pid, f"Project {target_pid}"))

        elif action == "spawn":
            if task_id in inbox_ids or curr_pid is None:
                raise ValueError(
                    f"[FAIL-CLOSED] Task ID {task_id} is an inbox task and cannot use 'spawn' directly. Use 'move' with 'subtasks' instead."
                )

            spawn_subtasks = _clean_and_validate_subtasks(item.get("subtasks"), task_id)
            target_pid = curr_pid
            pname = allowed_projects.get(target_pid, f"Project {target_pid}") if target_pid else ""

        elif action == "refine":
            target_pid = curr_pid
            pname = allowed_projects.get(target_pid, f"Project {target_pid}") if target_pid else ""

        # Validate text expansion / refinement rules
        exp_title = item.get("expanded_title")
        exp_desc = item.get("expanded_description")

        final_exp_title = None
        final_exp_desc = None

        if action == "refine":
            if not exp_title or not isinstance(exp_title, str) or not exp_title.strip():
                raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} 'refine' action requires non-empty 'expanded_title'.")
            if not exp_desc or not isinstance(exp_desc, str) or not exp_desc.startswith("原条目: "):
                raise ValueError(
                    f"[FAIL-CLOSED] Task ID {task_id} 'refine' action 'expanded_description' must start with '原条目: '."
                )
            final_exp_title = exp_title.strip()
            final_exp_desc = exp_desc.strip()
        else:
            if exp_title is not None:
                if not isinstance(exp_title, str) or not exp_title.strip():
                    raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} 'expanded_title' is empty or invalid string.")

                if exp_desc is None or not isinstance(exp_desc, str) or not exp_desc.startswith("原条目: "):
                    raise ValueError(
                        f"[FAIL-CLOSED] Task ID {task_id} 'expanded_description' must start with '原条目: ' for traceability."
                    )

                final_exp_title = exp_title.strip()
                final_exp_desc = exp_desc.strip()
            else:
                if exp_desc is not None:
                    raise ValueError(
                        f"[FAIL-CLOSED] Task ID {task_id} has null 'expanded_title' but non-null 'expanded_description'."
                    )

        plan.append({
            "id": task_id,
            "title": orig_title,
            "action": action,
            "project_id": target_pid,
            "project_name": pname,
            "parent_task_id": parent_id,
            "parent_title": parent_title,
            "subtasks": spawn_subtasks,
            "expanded_title": final_exp_title,
            "expanded_description": final_exp_desc,
        })

    return plan
