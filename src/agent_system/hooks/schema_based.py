"""
Schema-based hook plugin base class.

Provides a base class for hook plugins that define their hooks declaratively
in a schema.yaml file, analogous to SchemaBasedToolServer for tools.

Example schema.yaml:
```yaml
hooks:
  - name: optimize_context  # also the handler method name on the plugin class
    type: PRE_LLM_CALL
    description: Optimize context before LLM call
    enabled: true
    timeout: 60             # optional; order: {before: [...], after: [...]}

  - name: log_context_stats
    type: POST_LLM_CALL
    description: Log context statistics
    enabled: true

config:
  max_total_tokens: 100000
  preserve_system_messages: true
```

Usage:
```python
class MyPlugin(SchemaBasedPluginHook):
    async def optimize_context(self, context: HookContext) -> HookResult:
        # Handler implementation
        return HookResult(...)

    async def log_context_stats(self, context: HookContext) -> HookResult:
        # Handler implementation
        return HookResult(...)
```
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent_system.core.schema_base_mixin import config_defaults_from_schema
from agent_system.utils import yaml_io

from .plugin_hook import PluginHook, HookContext, HookResult

logger = logging.getLogger(__name__)


class SchemaBasedPluginHook(PluginHook):
    """Base class for schema-driven hook plugins.

    Automatically loads hook definitions and configuration from schema.yaml
    and dispatches to handler methods by name.

    Subclasses should:
    1. Create a schema.yaml file in the same directory
    2. Implement handler methods referenced in schema.yaml
    3. Handler methods should match signature: async def handler(context: HookContext) -> HookResult
    """

    def __init__(self, plugin_dir: Path | str):
        """Initialize schema-based plugin.

        Args:
            plugin_dir: Directory containing schema.yaml
        """
        self.plugin_dir = Path(plugin_dir)
        self._schema = self._load_schema()

        # Extract config values from schema
        # Schema config has structure: {key: {type: ..., default: ..., ...}}
        # We need to extract just the values: {key: default_value}
        schema_config = self._schema.get("config", {})
        self._config = self._extract_config_defaults(schema_config)

        self._hooks = self._schema.get("hooks", [])

        # Extract plugin name from directory
        plugin_name = self.plugin_dir.name

        # Initialize PluginHook with name and config
        super().__init__(name=plugin_name, config=self._config)

    def _extract_config_defaults(self, schema_config: dict[str, Any]) -> dict[str, Any]:
        """Extract default values from schema config structure.

        The same rule applies to a plugin that is a tool server as well as a
        hook, so the two share one implementation -- a second copy of it had
        already grown in the todo plugin and drifted into ignoring the
        plugins.yaml block entirely.

        Args:
            schema_config: Config section from schema.yaml with type/default/description

        Returns:
            Dict with just the config values (defaults)
        """
        return config_defaults_from_schema(schema_config)

    def _load_schema(self) -> dict[str, Any]:
        """Load schema.yaml from plugin directory.

        Returns:
            Schema dictionary with hooks and config sections

        Raises:
            FileNotFoundError: If schema.yaml doesn't exist
            yaml.YAMLError: If schema.yaml is invalid
        """
        schema_path = self.plugin_dir / "schema.yaml"
        if not schema_path.exists():
            raise FileNotFoundError(
                f"schema.yaml not found in {self.plugin_dir}. "
                f"SchemaBasedPluginHook requires a schema.yaml file."
            )

        with open(schema_path, encoding="utf-8") as f:
            schema = yaml_io.safe_load(f)

        if not isinstance(schema, dict):
            raise ValueError(f"schema.yaml must contain a dict, got {type(schema)}")

        return schema

    def get_hooks(self) -> list[dict[str, Any]]:
        """Get hook definitions from schema.

        Returns:
            List of hook definitions with name, type, handler, etc.
        """
        return self._hooks

    def get_config(self) -> dict[str, Any]:
        """Get configuration from schema.

        Returns:
            Configuration dictionary from schema.yaml config section
        """
        return self._config

    def get_schema_data(self) -> dict[str, Any]:
        """Get full schema data (hooks + config).

        Returns:
            Complete schema dictionary from schema.yaml
        """
        return self._schema

    async def _dispatch_hook(self, hook_name: str, context: HookContext) -> HookResult:
        """Dispatch hook execution to handler method.

        Convention: Method name must match hook name exactly.
        E.g., hook "validate_messages" calls method "validate_messages(context)"

        Args:
            hook_name: Name of the hook to execute (also the method name)
            context: Hook execution context

        Returns:
            Result from handler method

        Raises:
            AttributeError: If handler method doesn't exist
        """
        # Find hook definition in schema (for validation)
        hook_def = None
        for h in self._hooks:
            if h.get("name") == hook_name:
                hook_def = h
                break

        if not hook_def:
            logger.warning(f"Hook '{hook_name}' not found in schema for {self.__class__.__name__}")
            return HookResult(success=True, modified=False, context=context)

        # Convention: method name = hook name
        handler = getattr(self, hook_name, None)
        if not handler:
            raise AttributeError(
                f"Handler method '{hook_name}' not found on {self.__class__.__name__}. "
                f"Convention: hook name must match method name exactly."
            )

        if not callable(handler):
            raise TypeError(
                f"Handler '{hook_name}' on {self.__class__.__name__} is not callable"
            )

        return await handler(context)

    def _merge_results(self, results: list[HookResult], context: HookContext) -> HookResult:
        """Merge multiple hook results into a single result.

        Ensures modified=True if ANY hook modified the context.
        Returns the last result but preserves the modified flag.

        Args:
            results: List of HookResults from multiple hooks
            context: The final context after all modifications

        Returns:
            Merged HookResult
        """
        if not results:
            return HookResult(success=True, modified=False, context=context)

        any_modified = any(r.modified for r in results)
        final_result = results[-1]

        if any_modified and not final_result.modified:
            # Override modified flag if any previous hook modified
            return HookResult(
                success=final_result.success,
                modified=True,
                context=context,
                metadata=final_result.metadata,
                error=final_result.error
            )
        return final_result

    def _should_dispatch(self, hook: dict[str, Any], expected_type: str, context: HookContext) -> bool:
        """Check whether a schema hook entry should be dispatched.

        When the registry sets ``context.target_hook_name`` we dispatch
        only the targeted hook (the registry already handled
        enabled/disabled filtering).  Without a target we fall back to
        the schema-level ``enabled`` flag for backward compatibility.
        """
        hook_type = hook.get("type", "").upper()
        if hook_type != expected_type:
            return False

        target = getattr(context, "target_hook_name", None)
        if target:
            # Registry told us exactly which hook to run — match by name
            return hook.get("name") == target
        # Fallback: respect schema-level enabled flag
        return hook.get("enabled", True)

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "PRE_LLM_CALL", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "POST_LLM_CALL", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "PRE_TOOL_CALL", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_post_tool_call(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "POST_TOOL_CALL", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_format_output(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "FORMAT_OUTPUT", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_session_start(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "SESSION_START", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_session_end(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "SESSION_END", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_llm_progress(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "LLM_PROGRESS", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_pre_llm_request(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "PRE_LLM_REQUEST", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)

    async def on_post_llm_response(self, context: HookContext) -> HookResult:
        results = []
        for hook in self._hooks:
            if self._should_dispatch(hook, "POST_LLM_RESPONSE", context):
                result = await self._dispatch_hook(hook["name"], context)
                results.append(result)
                if result.modified and result.context:
                    context = result.context

        return self._merge_results(results, context)
