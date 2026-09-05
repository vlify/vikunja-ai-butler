"""
CLI subprocess LLM provider for Vikunja AI Butler.
Strictly executes via argv list with shell=False.
Supports safe shlex argv interpolation for legacy '{prompt}' templates and stdin delivery for modern commands.
"""

import sys
import shlex
import subprocess
from typing import List, Optional, Union

from butler.llm.base import LLMError, LLMTimeoutError, LLMExecutionError, LLMProvider

_legacy_warning_emitted = False


def _warn_legacy_interpolation() -> None:
    global _legacy_warning_emitted
    if not _legacy_warning_emitted:
        sys.stderr.write(
            "[WARN] Legacy '{prompt}' template detected in LLM command. "
            "Migrated to safe argv interpolation (command injection protection active).\n"
        )
        sys.stderr.flush()
        _legacy_warning_emitted = True


class CLIProvider:
    """Executes a local command line utility to query an LLM."""

    def __init__(
        self,
        command: Union[str, List[str]],
        timeout_seconds: int = 240,
    ):
        self.timeout_seconds = timeout_seconds
        self.argv, self.has_prompt_template = self._parse_command(command)

    def _parse_command(self, cmd: Union[str, List[str]]) -> tuple[List[str], bool]:
        if not cmd:
            return [], False

        if isinstance(cmd, str):
            has_prompt = "{prompt}" in cmd
            tokens = shlex.split(cmd)
        elif isinstance(cmd, (list, tuple)):
            tokens = [str(t) for t in cmd]
            has_prompt = any("{prompt}" in t for t in tokens)
        else:
            tokens = [str(cmd)]
            has_prompt = "{prompt}" in tokens[0]

        if has_prompt:
            _warn_legacy_interpolation()

        return tokens, has_prompt

    def _build_argv(self, prompt: str) -> List[str]:
        if not self.has_prompt_template:
            return list(self.argv)

        interpolated: List[str] = []
        for token in self.argv:
            if "{prompt}" in token:
                if token == "{prompt}":
                    interpolated.append(prompt)
                else:
                    interpolated.append(token.replace("{prompt}", prompt))
            else:
                interpolated.append(token)
        return interpolated

    def run(self, prompt: str) -> str:
        if not self.argv:
            raise LLMExecutionError("LLM command is empty.", exit_code=1)

        final_argv = self._build_argv(prompt)
        use_stdin = not self.has_prompt_template

        try:
            res = subprocess.run(
                final_argv,
                input=prompt if use_stdin else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMTimeoutError(
                f"LLM command timed out after {self.timeout_seconds}s"
            ) from e
        except FileNotFoundError as e:
            raise LLMExecutionError(
                f"LLM executable not found: {final_argv[0]}",
                exit_code=127,
                stderr=str(e),
            ) from e
        except OSError as e:
            raise LLMExecutionError(
                f"LLM command execution failed: {e}",
                exit_code=1,
                stderr=str(e),
            ) from e

        if res.returncode != 0:
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
