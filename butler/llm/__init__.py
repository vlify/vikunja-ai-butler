"""
LLM Provider package for Vikunja AI Butler.
"""

from typing import Any, Dict, Optional

from butler.llm.base import (
    LLMError,
    LLMTimeoutError,
    LLMExecutionError,
    LLMProvider,
)
from butler.llm.cli import CLIProvider
from butler.llm.openai_compat import OpenAICompatProvider
from butler.llm.runner import LLMRunner


def create_llm_provider(cfg: Optional[Dict[str, Any]] = None) -> LLMProvider:
    """
    Factory creating an LLMProvider instance based on configuration.
    Supports 'openai-compat' (default) and 'cli'.
    Detects legacy command configurations with '{prompt}' and safely migrates them to CLIProvider.
    """
    if cfg is None:
        cfg = {}

    provider_type = cfg.get("provider")
    command = cfg.get("command")
    timeout = cfg.get("timeout_seconds", 240)

    # Legacy config detection: '{prompt}' placeholder in command
    has_legacy_prompt = isinstance(command, str) and "{prompt}" in command

    if has_legacy_prompt:
        return CLIProvider(command=command, timeout_seconds=timeout)

    if provider_type == "cli" or (command and provider_type != "openai-compat"):
        return CLIProvider(command=command, timeout_seconds=timeout)

    # Default to openai-compat
    base_url = cfg.get("base_url", "http://127.0.0.1:4000/v1")
    model = cfg.get("model", "glm")
    api_key = cfg.get("api_key")
    api_key_env = cfg.get("api_key_env", "LLM_GATEWAY_KEY")
    temperature = cfg.get("temperature")

    return OpenAICompatProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        timeout_seconds=timeout,
        temperature=temperature,
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
