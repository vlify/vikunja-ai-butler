"""
Backward compatibility adapter for LLMRunner.
Wraps CLIProvider so existing imports and subclasses (e.g. MockLLMRunner) remain intact.
"""

from typing import List, Optional, Union

from butler.llm.base import LLMError, LLMTimeoutError, LLMExecutionError
from butler.llm.cli import CLIProvider


class LLMRunner(CLIProvider):
    """
    Legacy LLMRunner interface retained for backward compatibility.
    Inherits from CLIProvider; prompt is safely passed via stdin without shell interpolation.
    """

    def __init__(
        self,
        command_template: Union[str, List[str]] = "",
        timeout_seconds: int = 240,
    ):
        super().__init__(command=command_template, timeout_seconds=timeout_seconds)
        self.command_template = command_template
