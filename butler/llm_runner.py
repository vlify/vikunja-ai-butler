"""
LLM execution runner for Vikunja AI Butler (Legacy Compatibility Re-export).

Maintained for backward compatibility with existing tests and scripts.
New code should import directly from `butler.llm`.
"""

from butler.llm import (
    LLMError,
    LLMTimeoutError,
    LLMExecutionError,
    LLMProvider,
    CLIProvider,
    OpenAICompatProvider,
    LLMRunner,
    create_llm_provider,
)

__all__ = [
    "LLMError",
    "LLMTimeoutError",
    "LLMExecutionError",
    "LLMProvider",
    "CLIProvider",
    "OpenAICompatProvider",
    "LLMRunner",
    "create_llm_provider",
]
