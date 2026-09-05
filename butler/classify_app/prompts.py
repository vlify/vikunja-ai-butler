"""
GTD classification and prompt building logic.
"""

from typing import Any, Dict, List, Optional


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
   - Compound Move: When moving to a Project list (e.g., project_id: 6), you can ALSO optionally provide "subtasks": ["Step 1", "Step 2"] (1 to 3 items) to automatically generate next-action subtasks under it!
   - Can optionally include "expanded_title" and "expanded_description" (must start with '原条目: <original_title>').
2. "attach": Attach task as a subtask of an existing candidate parent task. Requires "id", "action": "attach", "parent_task_id".
   - Do NOT provide "project_id" or "subtasks" when attaching.
3. "spawn": For an EXISTING active parent task, spawn 1 to 3 concrete next-action subtasks. Requires "id" (parent task id), "action": "spawn", "subtasks": ["subtask 1", "subtask 2"].
   - For tasks in Inbox, do NOT use "spawn" directly; use "move" with "subtasks" instead.
4. "refine": For vague titles lacking clear action verbs, refine title and description. Requires "id", "action": "refine", "expanded_title", "expanded_description".
   - expanded_description MUST start on the first line with: 原条目: <original_title>

GTD Heuristics & Directives:
- If an inbox task is a multi-step project (e.g., requires >1 action), assign it to the Project list (6) AND provide 1 to 3 concrete next-action subtasks in the "subtasks" field!
- If a task in Single Action (5) actually contains multiple steps, move it to Project (6) and spawn subtasks.
- If a task is blocked waiting on an external response, move it to Waiting For (7).

Safety Constraints:
- NEVER delete tasks, NEVER modify done status.
- Moving to unlisted lists or Inbox itself is strictly forbidden.
- Total optimization actions on active tasks MUST NOT exceed {max_active_mutations} (be selective, focus on the most impactful refinements).
- Output strict JSON array without Markdown fences or extra commentary.

Format:
[
  {{"id": 101, "action": "move", "project_id": 6, "expanded_title": "搭建个人技术博客", "expanded_description": "原条目: 搭建博客\\n建立公网知识库。", "subtasks": ["选型评估", "配置域名"]}},
  {{"id": 102, "action": "attach", "parent_task_id": 16, "expanded_title": null, "expanded_description": null}},
  {{"id": 103, "action": "spawn", "subtasks": ["Step 1: Setup environment", "Step 2: Run benchmark"]}},
  {{"id": 104, "action": "refine", "expanded_title": "Read Rust Guide Ch 1-3", "expanded_description": "原条目: Learn Rust\\nRead chapters 1 to 3."}}
]
"""
    return prompt
