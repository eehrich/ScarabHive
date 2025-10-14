"""
Plugin Hook System for Agent Lifecycle.

Provides extensible hooks allowing plugins to intercept agent lifecycle points:
- pre_llm_call: Before LLM invocation (modify messages, inject context)
- post_llm_call: After LLM response (modify response, extract metadata)
- pre_tool_call: Before tool execution (modify parameters, apply policies)
- post_tool_call: After tool execution (modify results, apply transformations)
- format_output: Format final output (convert to markdown, HTML, etc.)
- session_start: Initialize session (inject system prompts, setup state)
- session_end: Cleanup session (persist state, generate summaries)

Key features:
- Named ordering system with before/after dependencies
- Async execution with error isolation
- YAML configuration with enable/disable per hook
- Request-scoped status message support
"""

from .registry import HookRegistry, get_hook_registry
from .plugin_hook import (
    PluginHook,
    HookContext,
    HookType,
    HookResult,
)
from .exceptions import (
    HookError,
    HookOrderingError,
    HookExecutionError,
    HookTimeoutError,
    CircularDependencyError,
)

__all__ = [
    # Registry
    "HookRegistry",
    "get_hook_registry",
    # Base classes and types
    "PluginHook",
    "HookContext",
    "HookType",
    "HookResult",
    # Exceptions
    "HookError",
    "HookOrderingError",
    "HookExecutionError",
    "HookTimeoutError",
    "CircularDependencyError",
]
