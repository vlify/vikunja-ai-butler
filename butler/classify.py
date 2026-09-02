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
from typing import Any, Dict, List, Optional, Set, Tuple

from butler.config import load_config
from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm_runner import LLMRunner, LLMError, LLMTimeoutError


def build_prompt(tasks: List[Dict[str, Any]], allowed_projects: Dict[int, str]) -> str:
    """
    Assemble the GTD classification prompt with strict JSON output schema.
    """
    lines = []
    for idx, t in enumerate(tasks, 1):
        desc = t.get("description", "").strip() or "(no description)"
        lines.append(f"{idx}. ID: {t['id']} | Title: {t['title']} | Description: {desc}")
    tasks_str = "\n".join(lines)

    target_defs = "\n".join([f"- {pid}: {name}" for pid, name in allowed_projects.items()])

    prompt = f"""You are a professional GTD task classifier. Classify the following tasks from the Inbox into the most appropriate target list.

Allowed target lists (ONLY classify into one of these):
{target_defs}

Rules:
1. Moving to unlisted lists or Inbox itself is strictly forbidden.
2. If a task has insufficient info or should stay in Inbox, DO NOT include it in the output.
3. NEVER create tasks, NEVER delete tasks, NEVER modify done status. Only return project_id and optional expansion.
4. Expansion rules:
   - For vague items lacking clear actionability, output expanded_title and expanded_description (2-3 sentences).
   - expanded_description MUST start on the first line with: 原条目: <original_title>
   - For clear items, expanded_title and expanded_description MUST be null.
5. Output strict JSON array without Markdown fences or extra commentary.

Format:
[
  {{"id": 101, "project_id": 5, "expanded_title": null, "expanded_description": null}},
  {{"id": 102, "project_id": 6, "expanded_title": "Learn Spark Basics", "expanded_description": "原条目: spark\\nStudy Spark architecture and DataFrame API."}}
]

Tasks to classify:
{tasks_str}
"""
    return prompt


def parse_and_validate_llm_output(
    raw_text: str,
    inbox_tasks: List[Dict[str, Any]],
    allowed_projects: Dict[int, str],
    forbidden_projects: Set[int],
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

    valid_task_map = {t["id"]: t["title"] for t in inbox_tasks}
    plan: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} is not a dict: {item}")

        # Rule 1: done status is strictly forbidden in mutation payload
        if "done" in item:
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} contains forbidden 'done' field: {item}")

        task_id = item.get("id")
        target_pid = item.get("project_id")

        if task_id is None or target_pid is None:
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} missing 'id' or 'project_id': {item}")

        try:
            task_id = int(task_id)
            target_pid = int(target_pid)
        except (ValueError, TypeError):
            raise ValueError(f"[FAIL-CLOSED] Item #{idx+1} 'id' or 'project_id' is not a valid integer: {item}")

        if task_id not in valid_task_map:
            raise ValueError(f"[FAIL-CLOSED] Task ID {task_id} does not belong to current inbox tasks.")

        if task_id in seen_ids:
            raise ValueError(f"[FAIL-CLOSED] Duplicate task ID {task_id} in LLM output.")

        if target_pid in forbidden_projects:
            raise ValueError(f"[FAIL-CLOSED] Target project_id {target_pid} is explicitly forbidden.")

        if target_pid not in allowed_projects:
            raise ValueError(f"[FAIL-CLOSED] Target project_id {target_pid} is not in allowed whitelist: {list(allowed_projects.keys())}")

        seen_ids.add(task_id)
        orig_title = valid_task_map[task_id]

        # Rule 2: validate text expansion rules
        exp_title = item.get("expanded_title")
        exp_desc = item.get("expanded_description")

        final_exp_title = None
        final_exp_desc = None

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
            "project_id": target_pid,
            "project_name": allowed_projects[target_pid],
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

    # Step 1: Fetch tasks and filter inbox
    try:
        all_tasks = client.get_tasks()
    except VikunjaAPIError as e:
        print(f"Error fetching tasks from Vikunja: {e}", file=sys.stderr)
        return 1

    inbox_tasks = []
    for t in all_tasks:
        if isinstance(t, dict) and not t.get("done", False) and t.get("project_id") == inbox_pid:
            inbox_tasks.append({
                "id": t.get("id"),
                "title": t.get("title", "").strip(),
                "description": t.get("description", "").strip(),
            })

    if not inbox_tasks:
        print("[INBOX] Inbox is empty. No tasks to classify. Exiting cleanly.")
        return 0

    print(f"[INBOX] Found {len(inbox_tasks)} pending inbox tasks. Invoking LLM classifier...")

    # Step 2: Build prompt and execute LLM
    prompt = build_prompt(inbox_tasks, allowed_projects)

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
        )
    except ValueError as e:
        print(f"{e}", file=sys.stderr)
        return 1

    # Step 4: Execute plan or dry-run
    print("=" * 72)
    if dry_run:
        print(" [DRY-RUN] Classification plan generated (No API mutations executed)")
    else:
        print(" [EXECUTE] Executing inbox task moves and expansions...")
    print("=" * 72)

    moved_ids: Set[int] = set()
    counts = {pid: 0 for pid in allowed_projects}

    for item in plan:
        t_id = item["id"]
        t_title = item["title"]
        pid = item["project_id"]
        pname = item["project_name"]
        exp_title = item.get("expanded_title")
        exp_desc = item.get("expanded_description")

        moved_ids.add(t_id)
        counts[pid] = counts.get(pid, 0) + 1

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

    remaining_count = len(inbox_tasks) - len(moved_ids)
    for t in inbox_tasks:
        if t["id"] not in moved_ids:
            print(f"[KEPT] [RETAINED INBOX] Task #{t['id']} [{t['title']}]")

    print("=" * 72)
    print("Classification Summary:")
    print(f"  Total Inbox Tasks: {len(inbox_tasks)}")
    for pid, pname in allowed_projects.items():
        print(f"  {pname} ({pid}): {counts.get(pid, 0)}")
    print(f"  Retained in Inbox: {remaining_count}")
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
