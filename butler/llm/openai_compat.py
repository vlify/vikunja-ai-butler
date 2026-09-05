"""
OpenAI-compatible HTTP provider for Vikunja AI Butler.

Supports any standard OpenAI-compatible API (LiteLLM, vLLM, Ollama, OpenAI, etc.)
using pure Python standard library (urllib.request). Zero external dependencies.
"""

import os
import json
import socket
import urllib.request
import urllib.error
from typing import Optional, Dict, Any

from butler.llm.base import LLMError, LLMTimeoutError, LLMExecutionError, LLMProvider


class OpenAICompatProvider:
    """
    Queries an OpenAI-compatible /chat/completions endpoint.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4000/v1",
        model: str = "glm",
        api_key: Optional[str] = None,
        api_key_env: Optional[str] = "LLM_GATEWAY_KEY",
        timeout_seconds: int = 240,
        temperature: Optional[float] = None,
    ):
        self.base_url = (base_url or "http://127.0.0.1:4000/v1").rstrip("/")
        self.model = model or "glm"
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

        # Resolve API key: explicit > specified env > default envs
        self.api_key = api_key or ""
        if not self.api_key and api_key_env:
            self.api_key = os.environ.get(api_key_env, "")
        if not self.api_key:
            self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def _get_endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Vikunja-AI-Butler/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key.strip()}"
        return headers

    def _mask_secret(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "[REDACTED]")
        return text

    def run(self, prompt: str) -> str:
        endpoint = self._get_endpoint()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers=self._get_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw_body = resp.read().decode("utf-8")
        except (TimeoutError, socket.timeout) as e:
            raise LLMTimeoutError(
                f"LLM request timed out after {self.timeout_seconds}s"
            ) from e
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            safe_body = self._mask_secret(err_body)
            raise LLMExecutionError(
                f"HTTP {e.code} from LLM endpoint {endpoint}: {safe_body}",
                exit_code=e.code,
                stderr=safe_body,
            ) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise LLMTimeoutError(
                    f"LLM request timed out after {self.timeout_seconds}s"
                ) from e
            safe_reason = self._mask_secret(str(e.reason))
            raise LLMExecutionError(
                f"Network error connecting to LLM endpoint {endpoint}: {safe_reason}",
                exit_code=1,
                stderr=safe_reason,
            ) from None
        except LLMError:
            raise
        except Exception as e:
            safe_err = self._mask_secret(str(e))
            raise LLMExecutionError(
                f"Unexpected error querying LLM endpoint: {safe_err}",
                exit_code=1,
                stderr=safe_err,
            ) from None

        try:
            resp_data = json.loads(raw_body)
        except json.JSONDecodeError as e:
            raise LLMExecutionError(
                f"Invalid JSON returned from LLM endpoint: {e}",
                exit_code=1,
                stderr=raw_body,
            ) from e

        choices = resp_data.get("choices")
        if not choices or not isinstance(choices, list):
            raise LLMExecutionError(
                f"LLM response missing 'choices' list: {raw_body}",
                exit_code=1,
                stderr=raw_body,
            )

        message = choices[0].get("message", {})
        content = message.get("content")
        if content is None:
            raise LLMExecutionError(
                f"LLM response missing choice content: {raw_body}",
                exit_code=1,
                stderr=raw_body,
            )

        return content
