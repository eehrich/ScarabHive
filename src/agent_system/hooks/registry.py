"""
Hook Registry with topological ordering and execution engine.

Manages registration, ordering, and execution of plugin hooks
with support for named dependencies and parallel execution.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import inspect
import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from .exceptions import CircularDependencyError
from .ordering import topological_sort
from .plugin_hook import HookContext, HookResult, HookType, PluginHook

logger = logging.getLogger(__name__)

# Global hook registry instance
_global_hook_registry: Optional[HookRegistry] = None


def get_hook_registry() -> HookRegistry:
    """Get or create the global hook registry instance."""
    global _global_hook_registry
    if _global_hook_registry is None:
        _global_hook_registry = HookRegistry()
    return _global_hook_registry


class HookRegistry:
    """
    Central registry for managing plugin hooks.

    Responsibilities:
    - Register hooks with ordering specifications
    - Resolve dependency ordering using topological sort
    - Execute hooks in correct order with error isolation
    - Track hook execution timing and errors

    Thread-safe for registration, execution managed by asyncio.
    """

    def __init__(self, default_timeout: float = 30.0):
        """
        Initialize hook registry.

        Args:
            default_timeout: Default timeout for hook execution in seconds
        """
        self.default_timeout = default_timeout

        # Hook storage: hook_type -> list of (hook_name, hook_instance, order_spec, metadata)
        # metadata contains: enabled, timeout, description
        self._hooks: Dict[HookType, List[tuple[str, PluginHook, Dict[str, List[str]], Dict[str, Any]]]] = defaultdict(list)

        # Lock for thread-safe registration
        self._lock = asyncio.Lock()

        # Execution statistics
        self._stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "executions": 0,
            "successes": 0,
            "failures": 0,
            "total_time": 0.0,
            "avg_time": 0.0,
        })

    async def register_hook(
        self,
        hook_type: HookType,
        hook_name: str,
        hook: PluginHook,
        order_spec: Optional[Dict[str, List[str]]] = None,
        enabled: bool = True,
        timeout: Optional[float] = None,
        description: str = "",
        category: Optional[str] = None,
        **extra_metadata
    ) -> None:
        """
        Register a hook for a specific lifecycle point.

        Args:
            hook_type: Type of hook (PRE_LLM_CALL, POST_LLM_CALL, etc.)
            hook_name: Unique name for this hook
            hook: PluginHook instance
            order_spec: Optional ordering specification {"before": [...], "after": [...]}
            enabled: Whether the hook is enabled (default: True)
            timeout: Hook-specific timeout in seconds (default: use registry default)
            description: Human-readable description of the hook
            category: Optional category/tag for grouping hooks (e.g., "inject", "optimize")
            **extra_metadata: Additional metadata to store with the hook

        Raises:
            ValueError: If hook with same name already registered for this type
        """
        async with self._lock:
            # Check for duplicates
            existing_names = [name for name, _, _, _ in self._hooks[hook_type]]
            if hook_name in existing_names:
                raise ValueError(f"Hook '{hook_name}' already registered for {hook_type.value}")

            # Use hook's own order spec if not provided
            if order_spec is None:
                order_spec = hook.get_order_spec()

            # Ensure order spec has required keys
            if "before" not in order_spec:
                order_spec["before"] = []
            if "after" not in order_spec:
                order_spec["after"] = []

            # Build metadata
            metadata = {
                "enabled": enabled,
                "timeout": timeout if timeout is not None else self.default_timeout,
                "description": description,
                "category": category,  # Store category for dependency resolution
                **extra_metadata  # Include any additional metadata
            }

            # Register the hook with metadata
            self._hooks[hook_type].append((hook_name, hook, order_spec, metadata))

            logger.debug(
                f"Registered hook '{hook_name}' for {hook_type.value} "
                f"(category={category}, before={order_spec.get('before', [])}, after={order_spec.get('after', [])}), enabled={enabled}"
            )

    async def unregister_hook(self, hook_type: HookType, hook_name: str) -> bool:
        """
        Unregister a hook.

        Args:
            hook_type: Type of hook
            hook_name: Name of hook to unregister

        Returns:
            True if hook was found and removed, False otherwise
        """
        async with self._lock:
            hooks_list = self._hooks[hook_type]
            for i, (name, _, _, _) in enumerate(hooks_list):
                if name == hook_name:
                    hooks_list.pop(i)
                    logger.debug(f"Unregistered hook '{hook_name}' from {hook_type.value}")
                    return True
            return False

    async def execute_hooks(
        self,
        hook_type: HookType,
        context: HookContext,
        timeout: Optional[float] = None,
        hook_filter: Optional[Callable[[str, bool], bool]] = None,
        stop_when: Optional[Callable[[HookContext], bool]] = None,
        on_failure: Optional[Callable[[str, Dict[str, Any], HookContext, str], None]] = None,
    ) -> HookContext:
        """
        Execute all registered hooks for a specific type in dependency order.

        Args:
            hook_type: Type of hooks to execute
            context: Hook context to pass to hooks
            timeout: Optional timeout (seconds) for every hook of this run;
                overrides each hook's own timeout
            hook_filter: Optional filter function(hook_name, default_enabled)
                -> bool deciding whether a hook runs; if it raises, the
                hook's default enabled state applies
            stop_when: Optional predicate over the context a successful hook
                left; true ends the chain there and no later hook runs (a
                blocked tool call is final -- no later hook may lift it)
            on_failure: Optional callback(hook_name, hook metadata, context,
                reason) for a hook that failed (timeout, exception, invalid
                result, success=False); it may change the context, and
                stop_when is asked again afterwards

        Returns:
            Modified context after all hooks executed

        Note:
            Hooks are executed sequentially in dependency order.
            Errors in individual hooks are logged but do not stop execution
            -- unless on_failure changes the context so that stop_when holds.
            Context modifications are accumulated across hooks.
            hook_filter allows agent-specific hook filtering (e.g., disabled_hooks)
        """
        # Get hooks for this type
        async with self._lock:
            hooks_list = list(self._hooks[hook_type])

        if not hooks_list:
            logger.debug(f"No hooks registered for {hook_type.value}")
            return context

        # Resolve execution order
        try:
            ordered_hooks = self._topological_sort(hooks_list)
        except CircularDependencyError as e:
            logger.error(f"Circular dependency in hooks for {hook_type.value}: {e}")
            # Continue with original order if we can't resolve dependencies
            ordered_hooks = [(name, hook, meta) for name, hook, _, meta in hooks_list]

        logger.debug(
            f"Executing {len(ordered_hooks)} hooks for {hook_type.value}: "
            f"{[name for name, _, _ in ordered_hooks]}"
        )

        # Execute hooks in order
        current_context = context
        for hook_name, hook_instance, metadata in ordered_hooks:
            # Get default enabled state from hook metadata
            hook_enabled_by_default = metadata.get("enabled", True)

            # Apply agent-specific hook filter if provided
            # Pass default state so filter can make informed decision
            if hook_filter:
                try:
                    if len(inspect.signature(hook_filter).parameters) >= 2:
                        should_execute = hook_filter(hook_name, hook_enabled_by_default)
                    else:  # the older signature, hook_filter(hook_name)
                        should_execute = bool(hook_filter(hook_name))
                except Exception as e:
                    logger.warning(
                        f"hook_filter failed for '{hook_name}': {e}; "
                        f"using its default (enabled={hook_enabled_by_default})"
                    )
                    should_execute = hook_enabled_by_default

                if not should_execute:
                    continue
            else:
                # No filter - use default enabled state
                if not hook_enabled_by_default:
                    logger.debug(f"Skipping disabled hook '{hook_name}'")
                    continue

            # An explicit timeout argument wins over the hook's own one
            hook_timeout = (timeout if timeout is not None
                            else metadata.get("timeout", self.default_timeout))

            try:
                # Deep copy context for isolation
                hook_context = self._deep_copy_context(current_context)

                # Inject per-agent hook config from agent's hooks.overrides.
                # This strips standard hook-system keys (enabled, timeout,
                # order) and exposes only custom config to the handler via
                # context.hook_config.  Allows agents to pass arbitrary
                # config to any hook without framework changes.
                hook_context.hook_config = self._extract_agent_hook_config(
                    hook_context, hook_name
                )

                # Tell SchemaBasedPluginHook which specific hook to
                # dispatch (strip plugin prefix to get the short name).
                hook_context.target_hook_name = (
                    hook_name.rsplit(".", 1)[-1] if "." in hook_name else hook_name
                )

                # Execute the appropriate hook method with timeout
                start_time = asyncio.get_event_loop().time()

                result = await asyncio.wait_for(
                    self._execute_hook_method(hook_type, hook_instance, hook_context),
                    timeout=hook_timeout
                )

                exec_time = asyncio.get_event_loop().time() - start_time

                # Validate hook result
                validation_error = self._validate_hook_result(result, hook_name)
                if validation_error:
                    logger.error(
                        f"Hook '{hook_name}' returned invalid result: {validation_error}",
                        extra={"hook_name": hook_name, "hook_type": hook_type.value}
                    )
                    self._update_stats(hook_name, success=False, exec_time=exec_time)
                    if self._report_failure(on_failure, stop_when, hook_name, metadata,
                                            current_context, "returned an invalid result"):
                        break
                    continue

                # Update statistics
                self._update_stats(hook_name, success=result.success, exec_time=exec_time)

                if result.success:
                    if result.modified and result.context:
                        # Audit log the modification
                        self._audit_log_modification(
                            hook_name,
                            hook_type,
                            current_context,
                            result.context,
                            result.metadata
                        )
                        # Use modified context for next hook
                        current_context = result.context
                        # Whose call it is is no hook's to change, and a hook
                        # that rebuilds the context field by field drops the
                        # fields it does not name (context_engineer and
                        # context_summarizer do). The step budget likewise.
                        current_context.user_id = context.user_id
                        current_context.max_steps = context.max_steps
                        current_context.final_call = context.final_call

                        # Merge hook result metadata into context metadata
                        if result.metadata:
                            current_context.metadata.update(result.metadata)

                        logger.debug(f"Hook '{hook_name}' modified context")
                    else:
                        # Even if not modified, update metadata from hook result
                        if result.metadata:
                            current_context.metadata.update(result.metadata)
                    if stop_when is not None and stop_when(current_context):
                        logger.debug(f"Hook '{hook_name}' ended the {hook_type.value} chain")
                        break
                else:
                    logger.warning(
                        f"Hook '{hook_name}' failed: {result.error}",
                        extra={"hook_name": hook_name, "hook_type": hook_type.value}
                    )
                    if self._report_failure(on_failure, stop_when, hook_name, metadata,
                                            current_context, f"reported a failure: {result.error}"):
                        break

            except asyncio.TimeoutError:
                logger.error(
                    f"Hook '{hook_name}' timed out after {hook_timeout}s",
                    extra={"hook_name": hook_name, "hook_type": hook_type.value, "timeout": hook_timeout}
                )
                self._update_stats(hook_name, success=False, exec_time=hook_timeout)
                if self._report_failure(on_failure, stop_when, hook_name, metadata,
                                        current_context, f"timed out after {hook_timeout}s"):
                    break

            except Exception as e:
                logger.error(
                    f"Hook '{hook_name}' raised exception: {e}",
                    exc_info=True,
                    extra={"hook_name": hook_name, "hook_type": hook_type.value}
                )
                self._update_stats(hook_name, success=False, exec_time=0.0)
                if self._report_failure(on_failure, stop_when, hook_name, metadata,
                                        current_context, f"raised {type(e).__name__}"):
                    break

        return current_context

    @staticmethod
    def _report_failure(on_failure, stop_when, hook_name: str, metadata: Dict[str, Any],
                        context: HookContext, reason: str) -> bool:
        """Hand a failed hook to ``on_failure``; whether the chain ends there."""
        if on_failure is None:
            return False
        on_failure(hook_name, metadata, context, reason)
        return stop_when is not None and bool(stop_when(context))

    async def _execute_hook_method(
        self,
        hook_type: HookType,
        hook: PluginHook,
        context: HookContext
    ) -> HookResult:
        """Execute the appropriate hook method based on type."""
        if hook_type == HookType.PRE_LLM_CALL:
            return await hook.on_pre_llm_call(context)
        elif hook_type == HookType.POST_LLM_CALL:
            return await hook.on_post_llm_call(context)
        elif hook_type == HookType.PRE_TOOL_CALL:
            return await hook.on_pre_tool_call(context)
        elif hook_type == HookType.POST_TOOL_CALL:
            return await hook.on_post_tool_call(context)
        elif hook_type == HookType.SESSION_START:
            return await hook.on_session_start(context)
        elif hook_type == HookType.SESSION_END:
            return await hook.on_session_end(context)
        elif hook_type == HookType.PRE_LLM_REQUEST:
            return await hook.on_pre_llm_request(context)
        elif hook_type == HookType.POST_LLM_RESPONSE:
            return await hook.on_post_llm_response(context)
        elif hook_type == HookType.LLM_PROGRESS:
            return await hook.on_llm_progress(context)
        else:
            raise ValueError(f"Unknown hook type: {hook_type}")

    def _topological_sort(
        self,
        hooks_list: List[tuple[str, PluginHook, Dict[str, List[str]], Dict[str, Any]]]
    ) -> List[tuple[str, PluginHook, Dict[str, Any]]]:
        """Hooks in execution order; see :func:`.ordering.topological_sort`."""
        return topological_sort(hooks_list)

    def _deep_copy_context(self, context: HookContext) -> HookContext:
        """
        Create a deep copy of context for hook isolation.

        Note: Some fields (agent, llm, cancellation_token) are copied by reference since they're
        stateful objects that hooks should not modify directly.
        """
        # replace(): every field not named here is carried over by reference
        # (agent, llm, cancellation_token, tools_schema -- read-only for hooks;
        # the rest immutable). A field list spelled out in full dropped each
        # new HookContext field until someone added it here too.
        return dataclasses.replace(
            context,
            messages=copy.deepcopy(context.messages) if context.messages else None,
            llm_response=copy.deepcopy(context.llm_response) if context.llm_response else None,
            tool_call=copy.deepcopy(context.tool_call) if context.tool_call else None,
            tool_result=copy.deepcopy(context.tool_result) if context.tool_result else None,
            metadata=copy.deepcopy(context.metadata),
            hook_config=copy.deepcopy(context.hook_config) if context.hook_config else {},
            llm_request_payload=copy.deepcopy(context.llm_request_payload) if context.llm_request_payload else None,
            llm_response_data=copy.deepcopy(context.llm_response_data) if context.llm_response_data else None,
            llm_usage=copy.deepcopy(context.llm_usage) if context.llm_usage else None,
        )

    # Keys managed by the hook system itself — stripped from hook_config
    _HOOK_SYSTEM_KEYS = frozenset({"enabled", "timeout", "order"})

    def _extract_agent_hook_config(
        self, context: HookContext, hook_name: str
    ) -> Dict[str, Any]:
        """Extract custom per-agent config for a specific hook.

        Reads the agent's ``hooks.overrides[hook_name]`` dict, strips
        hook-system keys (``enabled``, ``timeout``, ``order``), and
        returns the remaining entries.  This lets agents pass arbitrary
        config to any hook via their agent YAML::

            hooks:
              overrides:
                my_plugin.my_hook:
                  enabled: true        # ← system key (stripped)
                  custom_key: "value"  # ← passed in hook_config

        Returns:
            Dict with custom config (may be empty).
        """
        agent = context.agent
        if agent is None:
            return {}
        try:
            hooks_cfg = getattr(
                getattr(agent, "agent_config", None), "hooks", None
            )
            if hooks_cfg is None or not hasattr(hooks_cfg, "overrides"):
                return {}
            override = hooks_cfg.overrides.get(hook_name)
            if not override or not isinstance(override, dict):
                return {}
            # Return only non-system keys
            return {
                k: v for k, v in override.items()
                if k not in self._HOOK_SYSTEM_KEYS
            }
        except Exception:
            return {}

    def _validate_hook_result(self, result: Any, hook_name: str) -> Optional[str]:
        """
        Validate hook result conforms to expected schema.

        Args:
            result: Result returned by hook
            hook_name: Name of hook (for error reporting)

        Returns:
            Error message if validation fails, None if valid
        """
        # Check result is HookResult instance
        if not isinstance(result, HookResult):
            return f"Result must be HookResult instance, got {type(result).__name__}"

        # Check required fields
        if not hasattr(result, "success"):
            return "Result missing required 'success' field"

        if not isinstance(result.success, bool):
            return f"Result 'success' must be bool, got {type(result.success).__name__}"

        # If modified=True, context must be provided
        if getattr(result, "modified", False) and not getattr(result, "context", None):
            return "Result has modified=True but no context provided"

        # If context is provided, validate it's a HookContext
        if hasattr(result, "context") and result.context is not None:
            if not isinstance(result.context, HookContext):
                return f"Result context must be HookContext, got {type(result.context).__name__}"

        # Validate metadata is dict if provided
        if hasattr(result, "metadata") and result.metadata is not None:
            if not isinstance(result.metadata, dict):
                return f"Result metadata must be dict, got {type(result.metadata).__name__}"

        return None

    def _audit_log_modification(
        self,
        hook_name: str,
        hook_type: HookType,
        original_context: HookContext,
        modified_context: HookContext,
        metadata: Dict[str, Any]
    ) -> None:
        """
        Log hook modifications for audit trail.

        Args:
            hook_name: Name of hook that made modification
            hook_type: Type of hook
            original_context: Original context before modification
            modified_context: Context after modification
            metadata: Hook result metadata
        """
        # Build audit entry
        audit_entry = {
            "hook_name": hook_name,
            "hook_type": hook_type.value,
            "request_id": original_context.request_id,
            "session_id": original_context.session_id,
            "timestamp": asyncio.get_event_loop().time(),
            "modifications": {},
            "metadata": metadata
        }

        # Detect and log specific modifications
        if original_context.messages != modified_context.messages:
            audit_entry["modifications"]["messages"] = {
                "original_count": len(original_context.messages) if original_context.messages else 0,
                "modified_count": len(modified_context.messages) if modified_context.messages else 0,
            }

        if original_context.llm_response != modified_context.llm_response:
            audit_entry["modifications"]["llm_response"] = True

        if original_context.tool_call != modified_context.tool_call:
            audit_entry["modifications"]["tool_call"] = True

        if original_context.tool_result != modified_context.tool_result:
            audit_entry["modifications"]["tool_result"] = True

        if original_context.metadata != modified_context.metadata:
            audit_entry["modifications"]["metadata"] = True

        # Log at DEBUG level for audit trail (only visible when debugging)
        logger.debug(
            f"Hook modification audit: {hook_name}",
            extra={
                "audit_type": "hook_modification",
                "hook_name": hook_name,
                "hook_type": hook_type.value,
                "modifications": audit_entry["modifications"],
                "request_id": original_context.request_id,
                "session_id": original_context.session_id
            }
        )

    def _update_stats(self, hook_name: str, success: bool, exec_time: float) -> None:
        """Update execution statistics for a hook."""
        stats = self._stats[hook_name]
        stats["executions"] += 1
        if success:
            stats["successes"] += 1
        else:
            stats["failures"] += 1
        stats["total_time"] += exec_time
        stats["avg_time"] = stats["total_time"] / stats["executions"]

    def get_stats(self, hook_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Get execution statistics.

        Args:
            hook_name: Optional hook name to get stats for (None = all hooks)

        Returns:
            Statistics dictionary; for a named hook ``{}`` if it is registered
            but never ran, ``None`` if no such hook exists (callers in app.py
            and cli_utils check ``is None`` -- an unconditional ``{}`` made
            their not-found branches dead code).
        """
        if hook_name:
            if hook_name in self._stats:
                return dict(self._stats[hook_name])
            if any(hook_name == name
                   for hooks in self._hooks.values()
                   for name, _, _, _ in hooks):
                return {}
            return None
        return {name: dict(stats) for name, stats in self._stats.items()}

    def clear_stats(self, hook_name: Optional[str] = None) -> None:
        """Drop execution statistics (all hooks, or a single named one)."""
        if hook_name is None:
            self._stats.clear()
        else:
            self._stats.pop(hook_name, None)

    def list_hooks(self, hook_type: Optional[HookType] = None) -> Dict[str, List[str]]:
        """
        List registered hooks.

        Args:
            hook_type: Optional hook type to filter by

        Returns:
            Dictionary mapping hook types to lists of hook names
        """
        result = {}
        if hook_type:
            result[hook_type.value] = [name for name, _, _, _ in self._hooks[hook_type]]
        else:
            for htype, hooks_list in self._hooks.items():
                result[htype.value] = [name for name, _, _, _ in hooks_list]
        return result

    def get_hook_info(self, hook_name: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a hook.

        Args:
            hook_name: Name of the hook

        Returns:
            Dictionary with hook information or None if not found
        """
        for hook_type, hooks_list in self._hooks.items():
            for name, hook, order_spec, metadata in hooks_list:
                if name == hook_name:
                    return {
                        "name": name,
                        "type": hook_type.value,
                        "enabled": metadata.get("enabled", True),
                        "timeout": metadata.get("timeout", self.default_timeout),
                        "description": metadata.get("description", ""),
                        "order": order_spec,
                        "order_spec": order_spec,  # Backward compatibility
                        "metadata": metadata,
                        "class": hook.__class__.__name__,
                        "module": hook.__class__.__module__,
                        "stats": self.get_stats(hook_name),
                    }
        return None
