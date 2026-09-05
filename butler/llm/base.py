"""
Base interfaces and exception definitions for LLM providers in Vikunja AI Butler.
"""

from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """Base exception for LLM execution issues."""
    pass


class LLMTimeoutError(LLMError):
    """Raised when LLM command execution exceeds timeout limit."""
    pass


class LLMExecutionError(LLMError):
    """Raised when LLM command or API request fails."""
    def __init__(self, message: str, exit_code: int = 1, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


@runtime_checkable
class LLMProvider(Protocol):
    """
    Protocol defining the LLM provider interface.
    Any provider (OpenAI-compatible HTTP, CLI subprocess, etc.) must implement `run(prompt) -> str`.
    """
    def run(self, prompt: str) -> str:
        ...
