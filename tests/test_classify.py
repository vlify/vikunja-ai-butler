"""Unit tests for GTD inbox classification and fail-closed security rules."""

import os
import unittest
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
        self.fail_add_relation = False

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._tasks

    def update_task(
        self,
        task_id: int,
        project_id: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        call = {
            "task_id": task_id,
            "project_id": project_id,
            "title": title,
            "description": description,
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
        self.assertEqual(len(client.relations), 1)
        rel = client.relations[0]
        self.assertEqual(rel["task_id"], 201)
        self.assertEqual(rel["other_task_id"], 102)
        self.assertEqual(rel["relation_kind"], "subtask")

        self.assertEqual(len(client.updates), 1)
        up = client.updates[0]
        self.assertEqual(up["task_id"], 102)
        self.assertEqual(up["project_id"], 6)

    def test_run_classify_attach_relation_api_failure_is_fail_closed(self):
        raw = '[{"id": 102, "action": "attach", "parent_task_id": 201, "expanded_title": null, "expanded_description": null}]'
        all_tasks = self.sample_inbox + [
            {"id": 201, "title": "Learn Spark", "project_id": 6, "done": False},
        ]
        client = MockVikunjaClient(all_tasks)
        client.fail_add_relation = True
        llm = MockLLMRunner(output_text=raw)

        ret = run_classify(dry_run=False, client=client, llm=llm)
        self.assertNotEqual(ret, 0)
        self.assertEqual(len(client.updates), 0)

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


if __name__ == "__main__":
    unittest.main()
