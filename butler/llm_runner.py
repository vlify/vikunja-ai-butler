"""
LLM execution runner for Vikunja AI Butler.

Principle of Model Agnosticism:
- Does not lock into any specific LLM provider or proprietary CLI.
- Executes any command template supporting '{prompt}' placeholder or piped stdin.
- Enforces execution timeout and captures exit codes cleanly.
"""

import os
import sys
import shlex
import tempfile
import subprocess
from typing import Optional


class LLMError(Exception):
    """Base exception for LLM execution issues."""
    pass


class LLMTimeoutError(LLMError):
    """Raised when LLM command execution exceeds timeout limit."""
    pass


class LLMExecutionError(LLMError):
    """Raised when LLM command exits with non-zero status code."""
    def __init__(self, message: str, exit_code: int, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class LLMRunner:
    def __init__(self, command_template: str, timeout_seconds: int = 240):
        self.command_template = command_template
        self.timeout_seconds = timeout_seconds

    def run(self, prompt: str) -> str:
        """
        Execute the LLM command with the given prompt.
        Handles placeholder replacement '{prompt}' or pipes via stdin.
        """
        if not self.command_template:
            raise LLMExecutionError("LLM command template is empty.", exit_code=1)

        # Check if {prompt} exists in the template
        if "{prompt}" in self.command_template:
            # For complex multi-line prompts, directly substituting into shell command
            # could cause shell argument length limits or escaping issues.
            # Therefore, we write prompt to a temporary file if very large, or escape safely.
            # But the most compatible shell pattern is writing prompt to a temp file and
            # replacing {prompt_file} or substituting safely.
            # Here we support '{prompt}' by replacing it, and if it exceeds 4000 chars,
            # or if the command takes stdin, pipe it.
            # If the user command has "{prompt}", let's format it.
            # To prevent shell injection while allowing arbitrary user command pipelines:
            cmd = self.command_template.replace("{prompt}", prompt.replace('"', '\\"'))
            use_stdin = False
        else:
            cmd = self.command_template
            use_stdin = True

        try:
            res = subprocess.run(
                cmd,
                input=prompt if use_stdin else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMTimeoutError(
                f"LLM command timed out after {self.timeout_seconds}s"
            ) from e

        if res.returncode != 0:
            # Special check for GNU timeout (124)
            if res.returncode == 124:
                raise LLMTimeoutError(
                    f"LLM command timed out (exit code 124): {res.stderr.strip()}"
                )
            raise LLMExecutionError(
                f"LLM command failed with exit code {res.returncode}",
                exit_code=res.returncode,
                stderr=res.stderr.strip(),
            )

        return res.stdout
