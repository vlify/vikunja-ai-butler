"""
Vikunja API client and transport abstraction for Vikunja AI Butler.

Security & Integrity Principles:
1. Least-privilege field mutation: Only 'project_id', 'title', and 'description'
   may be modified during GTD classification.
2. Status immutability: The 'done' status is STRICTLY read-only.
3. Safe transport: Pure Python standard library urllib by default (zero external
   package dependency), with configurable timeouts and secure header injection.
"""

import os
import sys
import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


class VikunjaAPIError(Exception):
    """Raised when Vikunja API returns an error or cannot be reached."""
    pass


class VikunjaClient:
    """
    HTTP client for interacting with Vikunja REST API (v1).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 30,
    ):
        self.base_url = (base_url or os.environ.get("VIKUNJA_URL", "")).rstrip("/")
        self.token = token or os.environ.get("VIKUNJA_TOKEN", "")
        self.timeout = timeout

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Vikunja-AI-Butler/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if not self.base_url:
            raise VikunjaAPIError("VIKUNJA_URL is not configured.")
        if not self.token:
            raise VikunjaAPIError("VIKUNJA_TOKEN is not configured.")

        url = f"{self.base_url}{endpoint}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers=self._get_headers(),
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status_code = resp.getcode()
                raw_body = resp.read().decode("utf-8")
                if not raw_body.strip():
                    return {}
                try:
                    return json.loads(raw_body)
                except json.JSONDecodeError as e:
                    raise VikunjaAPIError(f"Invalid JSON returned from {url}: {e}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise VikunjaAPIError(f"HTTP {e.code} for {method} {url}: {err_body}")
        except urllib.error.URLError as e:
            raise VikunjaAPIError(f"Network error connecting to {url}: {e.reason}")
        except Exception as e:
            raise VikunjaAPIError(f"Unexpected error calling {url}: {e}")

    def get_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetch active/all tasks. If project_id is given, query tasks in that project.
        """
        if project_id is not None:
            endpoint = f"/api/v1/projects/{project_id}/tasks"
        else:
            endpoint = "/api/v1/tasks"

        data = self._request(endpoint, method="GET")
        if isinstance(data, list):
            return data
        return []

    def get_done_tasks(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetch completed tasks.
        Vikunja REST API supports filter or query param. Fallback filters in-memory.
        """
        # Attempt to query with filter
        if project_id is not None:
            endpoint = f"/api/v1/projects/{project_id}/tasks?filter=done%20%3D%20true"
        else:
            endpoint = "/api/v1/tasks?filter=done%20%3D%20true"

        try:
            data = self._request(endpoint, method="GET")
            if isinstance(data, list) and data:
                return [t for t in data if isinstance(t, dict) and t.get("done") is True]
        except Exception:
            pass

        # Fallback: get tasks and filter locally
        all_tasks = self.get_tasks(project_id)
        return [t for t in all_tasks if isinstance(t, dict) and t.get("done") is True]

    def update_task(
        self,
        task_id: int,
        project_id: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update a task's destination project or expanded text.
        ENFORCES LEAST-PRIVILEGE MUTATION:
        Only project_id, title, description are allowed. 'done' is rejected.
        """
        payload: Dict[str, Any] = {}
        if project_id is not None:
            payload["project_id"] = int(project_id)
        if title is not None:
            payload["title"] = str(title)
        if description is not None:
            payload["description"] = str(description)

        if not payload:
            return {}

        # POST /api/v1/tasks/{id} is standard in Vikunja for updates
        endpoint = f"/api/v1/tasks/{task_id}"
        return self._request(endpoint, method="POST", payload=payload)
