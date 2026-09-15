"""
Plugin Hook System for Agent Lifecycle.

Provides extensible hooks allowing plugins to intercept agent lifecycle points:
- pre_llm_call: Before LLM invocation (modify messages, inject context)
- post_llm_call: After LLM response (modify response, extract metadata)
- pre_llm_request / post_llm_response: LLM-client level (exact API payloads)
- llm_progress: During a streaming LLM call
- format_output: Format final output (convert to markdown, HTML, etc.)
- session_start: Initialize session (inject system prompts, setup state)
- session_end: Cleanup session (persist state, generate summaries)
- pre_tool_call / post_tool_call: declared but never fired (the tool loop
  does not call tool hooks; registration logs a warning)

Key features:
- Named ordering system with before/after dependencies
- Async execution with error isolation
- YAML configuration with enable/disable per hook
- Request-scoped status message support
- Global hook configuration via config/plugins.yaml
"""

from .registry import HookRegistry, get_hook_registry
from .plugin_hook import (
    PluginHook,
    HookContext,
    HookType,
    HookResult,
)
from .schema_based import SchemaBasedPluginHook
from .config import (
    HooksConfig,
    load_hooks_config,
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
    "SchemaBasedPluginHook",
    "HookContext",
    "HookType",
    "HookResult",
    # Configuration
    "HooksConfig",
    "load_hooks_config",
    # Exceptions
    "HookError",
    "HookOrderingError",
    "HookExecutionError",
    "HookTimeoutError",
    "CircularDependencyError",
]
