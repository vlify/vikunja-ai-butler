"""Unit tests for GTD inbox classification and fail-closed security rules."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional

from butler.classify import (
    build_prompt,
    parse_and_validate_llm_output,
    run_classify,
)
from butler.vikunja_client import VikunjaClient
from butler.llm_runner import LLMRunner, LLMTimeoutError, LLMExecutionError


class MockVikunjaClient(VikunjaClient):
    def __init__(self, tasks: List[Dict[str, Any]]):
        super().__init__(base_url="http://127.0.0.1:3456", token="mock_token")
        self._tasks = tasks
        self.updates: List[Dict[str, Any]] = []
        self.relations: List[Dict[str, Any]] = []
        self.created: List[Dict[str, Any]] = []
        self.fail_add_relation = False

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._tasks

    def create_task(
        self,
        project_id: int,
        title: str,
        description: Optional[str] = None,
        parent_task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        task_id = 1000 + len(self.created) + 1
        call = {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "description": description,
            "parent_task_id": parent_task_id,
        }
        self.created.append(call)
        if parent_task_id is not None:
            self.add_relation(
                task_id=task_id,
                other_task_id=parent_task_id,
                relation_kind="parenttask",
            )
        return {"id": task_id, "title": title}

    def update_task(
        self,
        task_id: int,
        project_id: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        parent_task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        call = {
            "task_id": task_id,
            "project_id": project_id,
            "title": title,
            "description": description,
            "parent_task_id": parent_task_id,
        }
        self.updates.append(call)
        return {"id": task_id}

    def add_relation(
        self,
        task_id: int,
        other_task_id: int,
        relation_kind: str = "subtask",
    ) -> Dict[str, Any]:
        if self.fail_add_relation:
            from butler.vikunja_client import VikunjaAPIError
            raise VikunjaAPIError("Simulated add_relation error")
        call = {
            "task_id": task_id,
            "other_task_id": other_task_id,
            "relation_kind": relation_kind,
        }
        self.relations.append(call)
        return {"id": 1, "task_id": task_id, "other_task_id": other_task_id}


class MockLLMRunner(LLMRunner):
    def __init__(self, output_text: str = "", fail_with: Optional[Exception] = None):
        super().__init__(command_template="mock_cmd")
        self.output_text = output_text
        self.fail_with = fail_with
        self.call_count = 0
        self.last_prompt = ""

    def run(self, prompt: str) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        if self.fail_with:
            raise self.fail_with
        return self.output_text


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.allowed_projects = {
            5: "Single Action",
            6: "Project",
            7: "Waiting For",
        }
        self.forbidden_projects = {1, 8}
        self.sample_inbox = [
            {"id": 101, "title": "Buy coffee beans", "description": "", "project_id": 1, "done": False},
            {"id": 102, "title": "learn spark", "description": "", "project_id": 1, "done": False},
            {"id": 103, "title": "Pending invoice reply", "description": "", "project_id": 1, "done": False},
        ]

    def test_dry_run_success(self):
        llm_json = """
        [
          {"id": 101, "project_id": 5, "expanded_title": null, "expanded_description": null},
          {"id": 102, "project_id": 6, "expanded_title": "Learn Apache Spark Basics", "expanded_description": "原条目: learn spark\\nSetup environment."}
        ]
        """
        client = MockVikunjaClient(self.sample_inbox)
        llm = MockLLMRunner(output_text=llm_json)

        ret = run_classify(dry_run=True, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(llm.call_count, 1)
        # In dry run, no mutating calls should be made
        self.assertEqual(len(client.updates), 0)

    def test_execute_success(self):
        llm_json = """
        [
          {"id": 101, "project_id": 5, "expanded_title": null, "expanded_description": null},
          {"id": 102, "project_id": 6, "expanded_title": "Learn Apache Spark Basics", "expanded_description": "原条目: learn spark\\nSetup environment."}
        ]
        """
        client = MockVikunjaClient(self.sample_inbox)
        llm = MockLLMRunner(output_text=llm_json)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(len(client.updates), 2)

        # Verify task 101 update: only project_id, no title/desc expansion
        u101 = next(u for u in client.updates if u["task_id"] == 101)
        self.assertEqual(u101["project_id"], 5)
        self.assertIsNone(u101["title"])
        self.assertIsNone(u101["description"])

        # Verify task 102 update: project_id and expansion
        u102 = next(u for u in client.updates if u["task_id"] == 102)
        self.assertEqual(u102["project_id"], 6)
        self.assertEqual(u102["title"], "Learn Apache Spark Basics")
        self.assertTrue(u102["description"].startswith("原条目: "))

    def test_fail_closed_violations(self):
        # 1. Invalid JSON
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                "Not JSON text", self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("[FAIL-CLOSED]", str(ctx.exception))

        # 2. Contains forbidden done field
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 101, "project_id": 5, "done": true}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("done", str(ctx.exception))

        # 3. Unmapped/foreign task ID
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 999, "project_id": 5}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("does not belong", str(ctx.exception))

        # 4. Duplicate task ID
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 101, "project_id": 5}, {"id": 101, "project_id": 6}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("Duplicate", str(ctx.exception))

        # 5. Forbidden project target (e.g., 8: Someday/Maybe)
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 101, "project_id": 8}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("forbidden", str(ctx.exception))

        # 6. Target project not in whitelist
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 101, "project_id": 42}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("whitelist", str(ctx.exception))

        # 7. Expansion without original prefix
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 102, "project_id": 6, "expanded_title": "Learn Spark", "expanded_description": "Missing prefix"}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("原条目: ", str(ctx.exception))

        # 8. Null expanded_title but non-null expanded_description
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                '[{"id": 101, "project_id": 5, "expanded_title": null, "expanded_description": "Illegal desc"}]',
                self.sample_inbox, self.allowed_projects, self.forbidden_projects
            )
        self.assertIn("null 'expanded_title'", str(ctx.exception))

    def test_empty_inbox_short_circuit(self):
        client = MockVikunjaClient([])
        llm = MockLLMRunner(output_text="[]")

        ret = run_classify(client=client, llm=llm)
        self.assertEqual(ret, 0)
        # LLM should never be called when inbox is empty
        self.assertEqual(llm.call_count, 0)

    def test_llm_timeout_and_error(self):
        client = MockVikunjaClient(self.sample_inbox)

        # Timeout scenario (exit 124)
        llm_timeout = MockLLMRunner(fail_with=LLMTimeoutError("timed out"))
        ret_timeout = run_classify(client=client, llm=llm_timeout)
        self.assertEqual(ret_timeout, 124)
        self.assertEqual(len(client.updates), 0)

        # General error scenario
        llm_err = MockLLMRunner(fail_with=LLMExecutionError("failed", exit_code=1))
        ret_err = run_classify(client=client, llm=llm_err)
        self.assertEqual(ret_err, 1)
        self.assertEqual(len(client.updates), 0)


class TestClassifyAttach(unittest.TestCase):
    def setUp(self):
        self.allowed_projects = {
            5: "Single Action",
            6: "Project",
            7: "Waiting For",
        }
        self.forbidden_projects = {1, 8}
        self.sample_inbox = [
            {"id": 101, "title": "Buy coffee beans", "description": "", "project_id": 1, "done": False},
            {"id": 102, "title": "Subtask for spark", "description": "", "project_id": 1, "done": False},
        ]
        self.parent_candidates = [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "project_name": "Project"},
            {"id": 202, "title": "Office chore", "project_id": 5, "project_name": "Single Action"},
        ]

    def test_parse_valid_attach_action(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 201, "expanded_title": null, "expanded_description": null}]'
        plan = parse_and_validate_llm_output(
            raw,
            self.sample_inbox,
            self.allowed_projects,
            self.forbidden_projects,
            parent_candidates=self.parent_candidates,
        )
        self.assertEqual(len(plan), 1)
        item = plan[0]
        self.assertEqual(item["id"], 102)
        self.assertEqual(item["action"], "attach")
        self.assertEqual(item["parent_task_id"], 201)
        self.assertEqual(item["project_id"], 6)
        self.assertEqual(item["project_name"], "Project")

    def test_parse_attach_missing_parent_task_id(self):
        raw = '[{"id": 102, "action": "attach"}]'
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw,
                self.sample_inbox,
                self.allowed_projects,
                self.forbidden_projects,
                parent_candidates=self.parent_candidates,
            )
        self.assertIn("missing 'parent_task_id'", str(ctx.exception))

    def test_parse_attach_invalid_parent_task_id(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 999}]'
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw,
                self.sample_inbox,
                self.allowed_projects,
                self.forbidden_projects,
                parent_candidates=self.parent_candidates,
            )
        self.assertIn("not in candidate parent tasks", str(ctx.exception))

    def test_parse_attach_with_forbidden_project_id(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 201, "project_id": 6}]'
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw,
                self.sample_inbox,
                self.allowed_projects,
                self.forbidden_projects,
                parent_candidates=self.parent_candidates,
            )
        self.assertIn("must NOT provide 'project_id'", str(ctx.exception))

    def test_parse_attach_parent_is_inbox_task(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 101}]'
        candidates_with_inbox = self.parent_candidates + [{"id": 101, "title": "Buy coffee", "project_id": 1}]
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw,
                self.sample_inbox,
                self.allowed_projects,
                self.forbidden_projects,
                parent_candidates=candidates_with_inbox,
            )
        self.assertIn("cannot be an inbox task itself", str(ctx.exception))

    def test_run_classify_attach_calls_api_correctly(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 201, "expanded_title": null, "expanded_description": null}]'
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]
        client = MockVikunjaClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(len(client.updates), 1)
        up = client.updates[0]
        self.assertEqual(up["task_id"], 102)
        self.assertEqual(up["project_id"], 6)
        self.assertEqual(up["parent_task_id"], 201)

    def test_run_classify_spawn_calls_api_correctly(self):
        raw = '[{"id": 201, "action": "spawn", "subtasks": ["Step 1: Install", "Step 2: Config"]}]'
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]
        client = MockVikunjaClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(len(client.created), 2)
        c1, c2 = client.created
        self.assertEqual(c1["title"], "Step 1: Install")
        self.assertEqual(c1["parent_task_id"], 201)
        self.assertEqual(c1["project_id"], 6)
        self.assertEqual(c2["title"], "Step 2: Config")
        self.assertEqual(c2["parent_task_id"], 201)

        # Assert two-step relation creation
        self.assertEqual(len(client.relations), 2)
        r1, r2 = client.relations
        self.assertEqual(r1["task_id"], 1001)
        self.assertEqual(r1["other_task_id"], 201)
        self.assertEqual(r1["relation_kind"], "parenttask")
        self.assertEqual(r2["task_id"], 1002)
        self.assertEqual(r2["other_task_id"], 201)
        self.assertEqual(r2["relation_kind"], "parenttask")

    def test_run_classify_refine_calls_api_correctly(self):
        raw = '[{"id": 201, "action": "refine", "expanded_title": "Setup Spark Cluster", "expanded_description": "原条目: Learn Spark\\nInstall single node cluster."}]'
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]
        client = MockVikunjaClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(len(client.updates), 1)
        up = client.updates[0]
        self.assertEqual(up["task_id"], 201)
        self.assertEqual(up["title"], "Setup Spark Cluster")
        self.assertIn("原条目: Learn Spark", up["description"])

    def test_parse_and_validate_active_quota_exceeded(self):
        # 6 mutations on active tasks when max is 5
        raw = """
        [
          {"id": 201, "action": "spawn", "subtasks": ["S1"]},
          {"id": 202, "action": "spawn", "subtasks": ["S2"]},
          {"id": 203, "action": "spawn", "subtasks": ["S3"]},
          {"id": 204, "action": "spawn", "subtasks": ["S4"]},
          {"id": 205, "action": "spawn", "subtasks": ["S5"]},
          {"id": 206, "action": "spawn", "subtasks": ["S6"]}
        ]
        """
        active_tasks = [{"id": i, "title": f"T{i}", "project_id": 6} for i in range(201, 207)]
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw,
                inbox_tasks=[],
                allowed_projects=self.allowed_projects,
                forbidden_projects=self.forbidden_projects,
                active_tasks=active_tasks,
                max_active_mutations=5,
            )
        self.assertIn("[FAIL-CLOSED] Batch quota exceeded", str(ctx.exception))

    def test_dry_run_attach(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 201}]'
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]
        client = MockVikunjaClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=True, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(len(client.relations), 0)
        self.assertEqual(len(client.updates), 0)

    def test_run_classify_attach_409_already_exists_is_idempotent(self):
        """Test that attach encountering HTTP 409 / code 4008 ('relation already exists') is idempotent: does not return 1, continues to next item, returns 0."""
        from butler.vikunja_client import VikunjaAPIError

        raw = json.dumps([
            {"id": 102, "action": "attach", "parent_task_id": 201, "expanded_title": None, "expanded_description": None},
            {"id": 103, "action": "move", "project_id": 5, "expanded_title": None, "expanded_description": None},
        ])
        all_tasks = self.sample_inbox + [
            {"id": 103, "title": "Clean desk", "project_id": 1, "done": False},
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]

        class ConflictAttachClient(MockVikunjaClient):
            def update_task(self, task_id, project_id=None, title=None, description=None, parent_task_id=None):
                if task_id == 102:
                    raise VikunjaAPIError(
                        'Updated task #102 but failed to attach to parent #201: '
                        'HTTP 409 for PUT http://192.168.12.1:3456/api/v1/tasks/102/relations: '
                        '{"code":4008,"message":"The task relation already exists."}'
                    )
                return super().update_task(task_id, project_id, title, description, parent_task_id)

        client = ConflictAttachClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        with io.StringIO() as buf, redirect_stdout(buf):
            ret = run_classify(dry_run=False, client=client, llm=llm)
            out = buf.getvalue()

        self.assertEqual(ret, 0)
        # Verify subsequent item (task 103 move) was executed
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(client.updates[0]["task_id"], 103)
        self.assertEqual(client.updates[0]["project_id"], 5)
        # Verify idempotency log: contains [SKIP] or [OK], task id, parent id, and "relation already exists"
        self.assertTrue("[SKIP]" in out or "[OK]" in out)
        self.assertIn("102", out)
        self.assertIn("201", out)
        self.assertIn("relation already exists", out.lower())

    def test_run_classify_attach_server_error_fails_closed(self):
        """Test that attach encountering non-409 error (e.g. HTTP 500) fails closed: logs fail, returns 1, stops execution."""
        from butler.vikunja_client import VikunjaAPIError

        raw = json.dumps([
            {"id": 102, "action": "attach", "parent_task_id": 201, "expanded_title": None, "expanded_description": None},
            {"id": 103, "action": "move", "project_id": 5, "expanded_title": None, "expanded_description": None},
        ])
        all_tasks = self.sample_inbox + [
            {"id": 103, "title": "Clean desk", "project_id": 1, "done": False},
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]

        class ErrorAttachClient(MockVikunjaClient):
            def update_task(self, task_id, project_id=None, title=None, description=None, parent_task_id=None):
                if task_id == 102:
                    raise VikunjaAPIError(
                        'Updated task #102 but failed to attach to parent #201: '
                        'HTTP 500 for PUT http://192.168.12.1:3456/api/v1/tasks/102/relations: '
                        '{"code":5000,"message":"Internal server error"}'
                    )
                return super().update_task(task_id, project_id, title, description, parent_task_id)

        client = ErrorAttachClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        with io.StringIO() as buf, redirect_stderr(buf):
            ret = run_classify(dry_run=False, client=client, llm=llm)
            err = buf.getvalue()

        self.assertEqual(ret, 1)
        self.assertEqual(len(client.updates), 0)
        self.assertIn("[FAIL]", err)
        self.assertIn("Failed to attach task #102 to parent #201", err)

    def test_run_classify_attach_other_409_fails_closed(self):
        """Test that attach encountering a non-4008 409 error does NOT get swallowed and still fails closed."""
        from butler.vikunja_client import VikunjaAPIError

        raw = json.dumps([
            {"id": 102, "action": "attach", "parent_task_id": 201, "expanded_title": None, "expanded_description": None},
        ])
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]

        class OtherConflictClient(MockVikunjaClient):
            def update_task(self, task_id, project_id=None, title=None, description=None, parent_task_id=None):
                raise VikunjaAPIError(
                    'Updated task #102 but failed to attach to parent #201: '
                    'HTTP 409 for PUT http://192.168.12.1:3456/api/v1/tasks/102/relations: '
                    '{"code":4099,"message":"Another conflicting relation type exists."}'
                )

        client = OtherConflictClient(all_tasks)
        llm = MockLLMRunner(output_text=raw)

        with io.StringIO() as buf, redirect_stderr(buf):
            ret = run_classify(dry_run=False, client=client, llm=llm)
            err = buf.getvalue()

        self.assertEqual(ret, 1)
        self.assertIn("[FAIL]", err)

    def test_parse_valid_compound_move(self):
        """Test compound move carrying project_id=6, expanded title/desc, and 1-3 subtasks parses cleanly."""
        raw = """
        [
          {
            "id": 101,
            "action": "move",
            "project_id": 6,
            "expanded_title": "搭建个人技术博客",
            "expanded_description": "原条目: 搭建博客\\n建立公网技术知识库。",
            "subtasks": [
              "选型评估 (Astro / Hugo / Zola)",
              "配置 Cloudflare Pages 与自定义域名",
              "编写并发布第一篇初始化文章"
            ]
          }
        ]
        """
        plan = parse_and_validate_llm_output(
            raw,
            inbox_tasks=self.sample_inbox,
            allowed_projects=self.allowed_projects,
            forbidden_projects=self.forbidden_projects,
        )
        self.assertEqual(len(plan), 1)
        item = plan[0]
        self.assertEqual(item["id"], 101)
        self.assertEqual(item["action"], "move")
        self.assertEqual(item["project_id"], 6)
        self.assertEqual(item["expanded_title"], "搭建个人技术博客")
        self.assertIn("原条目: 搭建博客", item["expanded_description"])
        self.assertEqual(len(item["subtasks"]), 3)
        self.assertEqual(item["subtasks"][0], "选型评估 (Astro / Hugo / Zola)")

    def test_parse_compound_move_subtasks_overflow_fail_closed(self):
        """Test subtasks > 3 or containing empty string raises fail-closed error."""
        raw_overflow = """
        [
          {
            "id": 101,
            "action": "move",
            "project_id": 6,
            "subtasks": ["S1", "S2", "S3", "S4"]
          }
        ]
        """
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw_overflow,
                inbox_tasks=self.sample_inbox,
                allowed_projects=self.allowed_projects,
                forbidden_projects=self.forbidden_projects,
            )
        self.assertIn("[FAIL-CLOSED]", str(ctx.exception))
        self.assertIn("max 3 subtasks", str(ctx.exception))

        raw_empty = """
        [
          {
            "id": 101,
            "action": "move",
            "project_id": 6,
            "subtasks": ["S1", "  "]
          }
        ]
        """
        with self.assertRaises(ValueError) as ctx2:
            parse_and_validate_llm_output(
                raw_empty,
                inbox_tasks=self.sample_inbox,
                allowed_projects=self.allowed_projects,
                forbidden_projects=self.forbidden_projects,
            )
        self.assertIn("[FAIL-CLOSED]", str(ctx2.exception))
        self.assertIn("empty or not a string", str(ctx2.exception))

    def test_parse_inbox_task_spawn_fail_closed(self):
        """Test spawn on an inbox task directly is rejected fail-closed (must use compound move instead)."""
        raw = """
        [
          {
            "id": 101,
            "action": "spawn",
            "subtasks": ["Step 1", "Step 2"]
          }
        ]
        """
        with self.assertRaises(ValueError) as ctx:
            parse_and_validate_llm_output(
                raw,
                inbox_tasks=self.sample_inbox,
                allowed_projects=self.allowed_projects,
                forbidden_projects=self.forbidden_projects,
            )
        self.assertIn("[FAIL-CLOSED]", str(ctx.exception))
        self.assertIn("cannot use 'spawn' directly", str(ctx.exception))

    def test_run_classify_compound_move_calls_api_correctly(self):
        """Test compound move updates parent task and calls create_task for each subtask with parent_task_id."""
        raw = """
        [
          {
            "id": 101,
            "action": "move",
            "project_id": 6,
            "expanded_title": "搭建个人技术博客",
            "expanded_description": "原条目: 搭建博客\\n建立知识库。",
            "subtasks": ["选型", "配置域名"]
          }
        ]
        """
        client = MockVikunjaClient(self.sample_inbox)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 0)
        # Parent task updated
        self.assertEqual(len(client.updates), 1)
        up = client.updates[0]
        self.assertEqual(up["task_id"], 101)
        self.assertEqual(up["project_id"], 6)
        self.assertEqual(up["title"], "搭建个人技术博客")
        self.assertIn("原条目: 搭建博客", up["description"])
        # Subtasks created
        self.assertEqual(len(client.created), 2)
        c1, c2 = client.created
        self.assertEqual(c1["title"], "选型")
        self.assertEqual(c1["project_id"], 6)
        self.assertEqual(c1["parent_task_id"], 101)
        self.assertEqual(c2["title"], "配置域名")
        self.assertEqual(c2["project_id"], 6)
        self.assertEqual(c2["parent_task_id"], 101)
        # Relations attached via two-step creation
        self.assertEqual(len(client.relations), 2)
        r1, r2 = client.relations
        self.assertEqual(r1["task_id"], 1001)
        self.assertEqual(r1["other_task_id"], 101)
        self.assertEqual(r1["relation_kind"], "parenttask")
        self.assertEqual(r2["task_id"], 1002)
        self.assertEqual(r2["other_task_id"], 101)
        self.assertEqual(r2["relation_kind"], "parenttask")

    def test_run_classify_compound_move_partial_failure(self):
        """Test that if creating a subtask fails, execution halts cleanly and returns error code 1 without corrupting subsequent tasks."""
        class FailingSubtaskClient(MockVikunjaClient):
            def create_task(self, project_id, title, description=None, parent_task_id=None):
                if len(self.created) >= 1:
                    from butler.vikunja_client import VikunjaAPIError
                    raise VikunjaAPIError("Simulated subtask creation failure")
                return super().create_task(project_id, title, description, parent_task_id)

        raw = """
        [
          {
            "id": 101,
            "action": "move",
            "project_id": 6,
            "subtasks": ["Step 1", "Step 2", "Step 3"]
          }
        ]
        """
        client = FailingSubtaskClient(self.sample_inbox)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 1)
        # 1 subtask succeeded before failure
        self.assertEqual(len(client.created), 1)

    def test_dry_run_compound_move(self):
        """Test dry run outputs MOVE and SPAWN details without calling API."""
        raw = """
        [
          {
            "id": 101,
            "action": "move",
            "project_id": 6,
            "subtasks": ["Step 1", "Step 2"]
          }
        ]
        """
        client = MockVikunjaClient(self.sample_inbox)
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=True, client=client, llm=llm)
        self.assertEqual(ret, 0)
        self.assertEqual(len(client.updates), 0)
        self.assertEqual(len(client.created), 0)

    def test_run_classify_spawn_relation_failure_fail_closed(self):
        """Test that if add_relation fails during spawn, execution halts cleanly and returns 1."""
        raw = '[{"id": 201, "action": "spawn", "subtasks": ["Step 1"]}]'
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]
        client = MockVikunjaClient(all_tasks)
        client.fail_add_relation = True
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertEqual(ret, 1)


    def test_update_task_parent_link_two_step_fail_closed(self):
        """update_task must NOT put parent link in related_tasks payload; it must call add_relation."""
        from butler.vikunja_client import VikunjaClient, VikunjaAPIError
        calls = []

        class TestClient(VikunjaClient):
            def _request(self, endpoint, method="GET", payload=None):
                calls.append((endpoint, method, payload))
                return {"id": 7}

            def add_relation(self, task_id, other_task_id, relation_kind="subtask"):
                calls.append((f"/api/v1/tasks/{task_id}/relations", "PUT",
                              {"other_task_id": other_task_id, "relation_kind": relation_kind}))
                if getattr(self, "_fail_relation", False):
                    raise VikunjaAPIError("HTTP 401 relations")
                return {}

        client = TestClient(base_url="http://x", token="t")
        client.update_task(task_id=7, project_id=6, parent_task_id=12)
        post_payload = [p for ep, m, p in calls if m == "POST"][0]
        self.assertNotIn("related_tasks", post_payload)
        self.assertIn("project_id", post_payload)
        rel_calls = [c for c in calls if c[1] == "PUT" and "/relations" in c[0]]
        self.assertEqual(len(rel_calls), 1)
        self.assertEqual(rel_calls[0][0], "/api/v1/tasks/7/relations")
        self.assertEqual(rel_calls[0][2], {"other_task_id": 12, "relation_kind": "parenttask"})

        # relation failure must raise fail-closed (no silent drop)
        client._fail_relation = True
        with self.assertRaises(VikunjaAPIError):
            client.update_task(task_id=7, project_id=6, parent_task_id=12)

class TestVikunjaClientTwoStep(unittest.TestCase):
    """Direct unit tests for VikunjaClient two-step task creation and relations."""

    def test_create_task_with_parent_id_two_step_calls(self):
        from butler.vikunja_client import VikunjaClient
        calls = []

        class TestClient(VikunjaClient):
            def _request(self, endpoint, method="GET", payload=None):
                calls.append({"endpoint": endpoint, "method": method, "payload": payload})
                if method == "PUT" and "/tasks" in endpoint and "/relations" not in endpoint:
                    return {"id": 888, "title": payload.get("title")}
                if method == "PUT" and "/relations" in endpoint:
                    return {"id": 1, "task_id": 888, "other_task_id": 12, "relation_kind": "parenttask"}
                return {}

        client = TestClient(base_url="http://mock", token="mock")
        res = client.create_task(project_id=6, title="Subtask A", description="Desc", parent_task_id=12)

        self.assertEqual(res["id"], 888)
        self.assertEqual(len(calls), 2)
        # Step 1: PUT /api/v1/projects/6/tasks
        self.assertEqual(calls[0]["endpoint"], "/api/v1/projects/6/tasks")
        self.assertEqual(calls[0]["method"], "PUT")
        self.assertEqual(calls[0]["payload"], {"title": "Subtask A", "description": "Desc"})
        # Step 2: PUT /api/v1/tasks/888/relations
        self.assertEqual(calls[1]["endpoint"], "/api/v1/tasks/888/relations")
        self.assertEqual(calls[1]["method"], "PUT")
        self.assertEqual(calls[1]["payload"], {"other_task_id": 12, "relation_kind": "parenttask"})

    def test_create_task_without_parent_id_single_step(self):
        from butler.vikunja_client import VikunjaClient
        calls = []

        class TestClient(VikunjaClient):
            def _request(self, endpoint, method="GET", payload=None):
                calls.append({"endpoint": endpoint, "method": method, "payload": payload})
                return {"id": 888, "title": payload.get("title")}

        client = TestClient(base_url="http://mock", token="mock")
        res = client.create_task(project_id=6, title="Root Task")

        self.assertEqual(res["id"], 888)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["endpoint"], "/api/v1/projects/6/tasks")

    def test_create_task_relation_failure_raises_vikunja_api_error(self):
        from butler.vikunja_client import VikunjaClient, VikunjaAPIError

        class FailingRelationClient(VikunjaClient):
            def _request(self, endpoint, method="GET", payload=None):
                if "/relations" in endpoint:
                    raise VikunjaAPIError("401 invalid token")
                return {"id": 888, "title": "Subtask"}

        client = FailingRelationClient(base_url="http://mock", token="mock")
        with self.assertRaises(VikunjaAPIError) as ctx:
            client.create_task(project_id=6, title="Subtask", parent_task_id=12)
        self.assertIn("failed to attach to parent #12", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
