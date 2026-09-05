"""
Core orchestration engine for GTD inbox classification.
"""

import sys
from typing import Optional
from butler.config import load_config
from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm import create_llm_provider, LLMProvider, LLMError, LLMTimeoutError
from butler.classify_app.prompts import build_prompt
from butler.classify_app.validator import parse_and_validate_llm_output
from butler.classify_app.actions import execute_plan


def run_classify(
    dry_run: bool = False,
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    client: Optional[VikunjaClient] = None,
    llm: Optional[LLMProvider] = None,
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
        llm = create_llm_provider(cfg.get("llm", {}))

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
    return execute_plan(plan, inbox_tasks, client, dry_run=dry_run)
