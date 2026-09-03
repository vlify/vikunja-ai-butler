#!/usr/bin/env python3
"""
inbox-classify -- Automated GTD Inbox Classifier with Strict Fail-Closed Safety.

FAIL-CLOSED SAFETY STRATEGY:
1. Target Whitelist: Tasks can ONLY be moved to explicitly whitelisted projects
   (e.g., Single Action, Project, Waiting For). Moving to Inbox (1) or Someday (8)
   is strictly forbidden.
2. Read-Only Task State: The task completion status ('done') is immutable. Any
   LLM output containing the 'done' key will trigger an immediate abort.
3. Trace Preservation (Expansion Rules):
   - For vague items, 'expanded_title' must be non-empty and 'expanded_description'
     MUST strictly begin with '原条目: <original_title>' for rollback traceability.
   - For clear items, both expanded fields must be null (strict no-touch rule).
4. Strict Task Ownership: Every task ID returned by the LLM must exist in the
   fetched inbox list. Duplicates or unmapped IDs cause immediate abort.
5. All-or-Nothing Mutation: If JSON validation fails on ANY single item, the
   entire batch is aborted before making any mutating API calls.
"""

import os
import sys
import re
import json
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure project root is in sys.path for direct CLI and systemd invocation
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.config import load_config
from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm_runner import LLMRunner, LLMError, LLMTimeoutError


def build_prompt(
    tasks: List[Dict[str, Any]],
    allowed_projects: Dict[int, str],
    parent_candidates: Optional[List[Dict[str, Any]]] = None,
    active_tasks: Optional[List[Dict[str, Any]]] = None,
    max_active_mutations: int = 5,
) -> str:
    """
    Assemble the GTD classification and task allocation optimization prompt
    with strict JSON output schema.
    """
    sections = []

    # 1. Inbox tasks section
    if tasks:
        inbox_lines = []
        for idx, t in enumerate(tasks, 1):
            desc = t.get("description", "").strip() or "(no description)"
            inbox_lines.append(f"{idx}. ID: {t['id']} | Title: {t['title']} | Description: {desc}")
        sections.append(f"Tasks from Inbox to classify and clear (Inbox Zero):\n" + "\n".join(inbox_lines))

    # 2. Active tasks section (for morning todo triage and refinement)
    if active_tasks:
        active_lines = []
        for idx, t in enumerate(active_tasks, 1):
            desc = t.get("description", "").strip() or "(no description)"
            pname = allowed_projects.get(t.get("project_id", 0), "Unknown")
            active_lines.append(f"{idx}. ID: {t['id']} | Current List: [{pname}] | Title: {t['title']} | Description: {desc}")
        sections.append(
            f"Active tasks from existing lists to review and optimize (Max {max_active_mutations} optimizations):\n"
            + "\n".join(active_lines)
        )

    # 3. Candidate parent tasks section
    if parent_candidates:
        parent_lines = [
            f"- ID: {p['id']} | Project: {p.get('project_name', allowed_projects.get(p.get('project_id', 0), 'Unknown'))} | Title: {p['title']}"
            for p in parent_candidates
        ]
        sections.append("Candidate parent tasks (to attach items as subtasks or spawn subtasks under):\n" + "\n".join(parent_lines))

    target_defs = "\n".join([f"- {pid}: {name}" for pid, name in allowed_projects.items()])

    prompt = f"""You are a professional GTD task classifier and allocation optimizer.
Review and optimize tasks to prepare an actionable, clear "Today's Todo" list for tomorrow morning.

Allowed target lists (for 'move' action):
{target_defs}

{chr(10).join(sections)}

Rules & Supported Actions:
1. "move": Move task to one of the allowed target lists. Requires "id", "action": "move", "project_id".
   - Use to correct misallocated lists (e.g. multi-step project in Single Action -> Project; waiting on external -> Waiting For).
2. "attach": Attach task as a subtask of an existing candidate parent task. Requires "id", "action": "attach", "parent_task_id".
   - Do NOT provide "project_id" when attaching (inherits parent's project).
3. "spawn": For a broad/complex parent task, spawn 1 to 3 concrete next-action subtasks. Requires "id" (parent task id), "action": "spawn", "subtasks": ["subtask 1", "subtask 2"].
4. "refine": For vague titles lacking clear action verbs, refine title and description. Requires "id", "action": "refine", "expanded_title", "expanded_description".
   - expanded_description MUST start on the first line with: 原条目: <original_title>

Safety Constraints:
- NEVER delete tasks, NEVER modify done status.
- Moving to unlisted lists or Inbox itself is strictly forbidden.
- Total optimization actions on active tasks MUST NOT exceed {max_active_mutations} (be selective, focus on the most impactful refinements).
- Output strict JSON array without Markdown fences or extra commentary.

Format:
[
  {{"id": 101, "action": "move", "project_id": 5, "expanded_title": null, "expanded_description": null}},
  {{"id": 102, "action": "attach", "parent_task_id": 16, "expanded_title": null, "expanded_description": null}},
  {{"id": 103, "action": "spawn", "subtasks": ["Step 1: Setup environment", "Step 2: Run benchmark"]}},
  {{"id": 104, "action": "refine", "expanded_title": "Read Rust Guide Ch 1-3", "expanded_description": "原条目: Learn Rust\\nRead chapters 1 to 3."}}
]
"""
    return prompt


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

        elif action == "attach":
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
            subtasks_raw = item.get("subtasks")
            if not isinstance(subtasks_raw, list) or not subtasks_raw:
                raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} 'spawn' action requires non-empty list of subtasks: {item}")
            if len(subtasks_raw) > 3:
                raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} 'spawn' action max 3 subtasks allowed per task, got {len(subtasks_raw)}.")

            cleaned_subtasks = []
            for s_idx, s in enumerate(subtasks_raw):
                if not isinstance(s, str) or not s.strip():
                    raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} subtask #{s_idx+1} is empty or not a string.")
                cleaned_subtasks.append(s.strip())

            spawn_subtasks = cleaned_subtasks
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


def run_classify(
    dry_run: bool = False,
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    client: Optional[VikunjaClient] = None,
    llm: Optional[LLMRunner] = None,
) -> int:
    """
    Execute inbox classification cycle. Returns 0 on success, non-zero on failure.
    """
    cfg = load_config(config_path=config_path, env_path=env_path)

    inbox_pid = cfg.get("gtd", {}).get("inbox_project_id", 1)
    allowed_projects = cfg.get("gtd", {}).get("allowed_target_projects", {
        5: "Single Action",
        6: "Project",
        7: "Waiting For",
    })
    forbidden_projects = set(cfg.get("gtd", {}).get("forbidden_target_projects", [1, 8]))

    if client is None:
        v_cfg = cfg.get("vikunja", {})
        client = VikunjaClient(
            base_url=v_cfg.get("url"),
            token=v_cfg.get("token"),
            timeout=v_cfg.get("timeout_seconds", 30),
        )

    # Step 1: Fetch tasks and filter inbox, active tasks, and parent candidates
    try:
        all_tasks = client.get_tasks()
    except VikunjaAPIError as e:
        print(f"Error fetching tasks from Vikunja: {e}", file=sys.stderr)
        return 1

    inbox_tasks = []
    parent_candidates = []
    active_tasks = []
    for t in all_tasks:
        if not isinstance(t, dict) or t.get("done", False):
            continue
        pid = t.get("project_id")
        if pid == inbox_pid:
            inbox_tasks.append({
                "id": t.get("id"),
                "title": t.get("title", "").strip(),
                "description": t.get("description", "").strip(),
            })
        elif pid in allowed_projects:
            active_tasks.append({
                "id": t.get("id"),
                "title": t.get("title", "").strip(),
                "description": t.get("description", "").strip(),
                "project_id": pid,
            })
            parent_candidates.append({
                "id": t.get("id"),
                "title": t.get("title", "").strip(),
                "project_id": pid,
                "project_name": allowed_projects[pid],
            })

    if not inbox_tasks and not active_tasks:
        print("[INBOX] Both inbox and active task lists are empty. No tasks to classify or optimize. Exiting cleanly.")
        return 0

    print(
        f"[INBOX] Found {len(inbox_tasks)} inbox tasks, {len(active_tasks)} active tasks, "
        f"and {len(parent_candidates)} candidate parent tasks. Invoking LLM classifier & optimizer..."
    )

    # Step 2: Build prompt and execute LLM
    prompt = build_prompt(
        inbox_tasks,
        allowed_projects,
        parent_candidates=parent_candidates,
        active_tasks=active_tasks[:15],
        max_active_mutations=5,
    )

    if llm is None:
        llm_cfg = cfg.get("llm", {})
        cmd = llm_cfg.get("command", "")
        timeout = llm_cfg.get("timeout_seconds", 240)
        llm = LLMRunner(command_template=cmd, timeout_seconds=timeout)

    try:
        raw_output = llm.run(prompt)
    except LLMTimeoutError as e:
        print(f"Error: LLM classifier timed out: {e}", file=sys.stderr)
        return 124
    except LLMError as e:
        print(f"Error: LLM classifier execution failed: {e}", file=sys.stderr)
        return 1

    # Step 3: Strict fail-closed validation
    try:
        plan = parse_and_validate_llm_output(
            raw_output,
            inbox_tasks=inbox_tasks,
            allowed_projects=allowed_projects,
            forbidden_projects=forbidden_projects,
            parent_candidates=parent_candidates,
            active_tasks=active_tasks,
            max_active_mutations=5,
        )
    except ValueError as e:
        print(f"{e}", file=sys.stderr)
        return 1

    # Step 4: Execute plan or dry-run
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
                    print(f"[FAIL] Failed to attach task #{t_id} to parent #{parent_id}: {e}", file=sys.stderr)
                    return 1

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
                if exp_title:
                    print(f"-> [PLAN MOVE+EXPAND] Task #{t_id} [{t_title}] => [{pname} (ID: {pid})]")
                    print(f"   Expanded title: [{t_title}] -> [{exp_title}]")
                    print(f"   Expanded desc:  {exp_desc}")
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

                if exp_title:
                    print(f"[OK] [MOVED+EXPANDED] Task #{t_id} [{t_title}] -> [{exp_title}] => [{pname} (ID: {pid})]")
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
