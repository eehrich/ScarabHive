"""
Hook Registry with topological ordering and execution engine.

Manages registration, ordering, and execution of plugin hooks
with support for named dependencies and parallel execution.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from .exceptions import CircularDependencyError
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
                **extra_metadata  # Include any additional metadata
            }
            
            # Register the hook with metadata
            self._hooks[hook_type].append((hook_name, hook, order_spec, metadata))
            
            logger.info(
                f"Registered hook '{hook_name}' for {hook_type.value} "
                f"(before={order_spec.get('before', [])}, after={order_spec.get('after', [])}), enabled={enabled}"
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
                    logger.info(f"Unregistered hook '{hook_name}' from {hook_type.value}")
                    return True
            return False
    
    async def execute_hooks(
        self,
        hook_type: HookType,
        context: HookContext,
        timeout: Optional[float] = None
    ) -> HookContext:
        """
        Execute all registered hooks for a specific type in dependency order.
        
        Args:
            hook_type: Type of hooks to execute
            context: Hook context to pass to hooks
            timeout: Optional timeout override (seconds)
            
        Returns:
            Modified context after all hooks executed
            
        Note:
            Hooks are executed sequentially in dependency order.
            Errors in individual hooks are logged but do not stop execution.
            Context modifications are accumulated across hooks.
        """
        timeout = timeout or self.default_timeout
        
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
            if not metadata.get("enabled", True):
                logger.debug(f"Skipping disabled hook '{hook_name}'")
                continue
            
            # Use hook-specific timeout if available, otherwise use registry default
            hook_timeout = metadata.get("timeout", timeout)
            
            try:
                # Deep copy context for isolation
                hook_context = self._deep_copy_context(current_context)
                
                # Execute the appropriate hook method with timeout
                start_time = asyncio.get_event_loop().time()
                
                result = await asyncio.wait_for(
                    self._execute_hook_method(hook_type, hook_instance, hook_context),
                    timeout=hook_timeout
                )
                
                exec_time = asyncio.get_event_loop().time() - start_time
                
                # Update statistics
                self._update_stats(hook_name, success=result.success, exec_time=exec_time)
                
                if result.success:
                    if result.modified and result.context:
                        # Use modified context for next hook
                        current_context = result.context
                        logger.debug(f"Hook '{hook_name}' modified context")
                    else:
                        logger.debug(f"Hook '{hook_name}' executed successfully (no modifications)")
                else:
                    logger.warning(
                        f"Hook '{hook_name}' failed: {result.error}",
                        extra={"hook_name": hook_name, "hook_type": hook_type.value}
                    )
                
            except asyncio.TimeoutError:
                logger.error(
                    f"Hook '{hook_name}' timed out after {hook_timeout}s",
                    extra={"hook_name": hook_name, "hook_type": hook_type.value, "timeout": hook_timeout}
                )
                self._update_stats(hook_name, success=False, exec_time=hook_timeout)
                
            except Exception as e:
                logger.error(
                    f"Hook '{hook_name}' raised exception: {e}",
                    exc_info=True,
                    extra={"hook_name": hook_name, "hook_type": hook_type.value}
                )
                self._update_stats(hook_name, success=False, exec_time=0.0)
        
        return current_context
    
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
        elif hook_type == HookType.FORMAT_OUTPUT:
            return await hook.on_format_output(context)
        elif hook_type == HookType.SESSION_START:
            return await hook.on_session_start(context)
        elif hook_type == HookType.SESSION_END:
            return await hook.on_session_end(context)
        else:
            raise ValueError(f"Unknown hook type: {hook_type}")
    
    def _topological_sort(
        self,
        hooks_list: List[tuple[str, PluginHook, Dict[str, List[str]], Dict[str, Any]]]
    ) -> List[tuple[str, PluginHook, Dict[str, Any]]]:
        """
        Sort hooks by dependency order using topological sort.
        
        Args:
            hooks_list: List of (name, hook, order_spec, metadata) tuples
            
        Returns:
            List of (name, hook, metadata) tuples in execution order
            
        Raises:
            CircularDependencyError: If circular dependencies detected
        """
        # Build dependency graph
        # For each hook, track what it must come before and after
        hooks_map = {name: hook for name, hook, _, _ in hooks_list}
        order_specs = {name: spec for name, _, spec, _ in hooks_list}
        metadata_map = {name: meta for name, _, _, meta in hooks_list}
        
        # Build adjacency list (hook -> hooks that must come after it)
        # and track in-degree (number of dependencies)
        graph: Dict[str, Set[str]] = defaultdict(set)
        in_degree: Dict[str, int] = defaultdict(int)
        
        # Initialize all hooks with 0 in-degree
        for hook_name in hooks_map:
            in_degree[hook_name] = 0
        
        # Add virtual "begin" and "end" nodes for absolute positioning
        in_degree["begin"] = 0
        in_degree["end"] = 0
        
        # Process ordering specifications to build the dependency graph
        # Edge A->B means "A must execute before B" (B depends on A)
        # Use a set to track which edges we've already added to avoid duplicates
        edges_added: Set[tuple[str, str]] = set()
        
        for hook_name, order_spec in order_specs.items():
            # "after" relationships: hook comes after these predecessors
            # If hook says "after: [A]", then A -> hook (A must execute before hook)
            for predecessor in order_spec.get("after", []):
                pred_key = str(predecessor)
                # Only process if the predecessor exists or is a virtual node
                if pred_key in hooks_map or pred_key in ("begin", "end"):
                    edge = (pred_key, hook_name)
                    if edge not in edges_added:
                        # Add edge: predecessor -> hook_name
                        graph[pred_key].add(hook_name)
                        in_degree[hook_name] += 1
                        edges_added.add(edge)
            
            # "before" relationships: hook comes before these successors
            # If hook says "before: [B]", then hook -> B (hook must execute before B)
            for successor in order_spec.get("before", []):
                succ_key = str(successor)
                # Only process if the successor exists or is a virtual node
                if succ_key in hooks_map or succ_key in ("begin", "end"):
                    edge = (hook_name, succ_key)
                    if edge not in edges_added:
                        # Add edge: hook_name -> successor
                        graph[hook_name].add(succ_key)
                        in_degree[succ_key] += 1
                        edges_added.add(edge)
        
        # Kahn's algorithm for topological sort
        queue = [node for node in in_degree if in_degree[node] == 0]
        result = []
        processed_count = 0
        
        while queue:
            # Sort queue for deterministic ordering when multiple nodes have no dependencies
            queue.sort()
            
            # Process node with no incoming edges
            current = queue.pop(0)
            processed_count += 1
            
            # Skip virtual nodes in output, but include real hooks
            if current in hooks_map:
                result.append(current)
            
            # Reduce in-degree for all successors of current node
            for successor in graph[current]:
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)
        
        # Check for cycles - if we didn't process all nodes (including virtual), there's a cycle
        total_nodes = len(hooks_map) + 2  # All hooks + "begin" + "end"
        if processed_count != total_nodes:
            # Some nodes still have in-degree > 0, indicating a cycle
            real_hooks = set(hooks_map.keys())
            processed_hooks = set(result)
            remaining = real_hooks - processed_hooks
            raise CircularDependencyError(
                f"Circular dependency detected in hooks: {sorted(remaining)}"
            )
        
        # Return sorted hooks with metadata
        return [(name, hooks_map[name], metadata_map[name]) for name in result]
    
    def _deep_copy_context(self, context: HookContext) -> HookContext:
        """
        Create a deep copy of context for hook isolation.
        
        Note: Some fields (agent, llm) are copied by reference since they're
        stateful objects that hooks should not modify directly.
        """
        return HookContext(
            hook_type=context.hook_type,
            request_id=context.request_id,
            session_id=context.session_id,
            agent=context.agent,  # Reference copy
            agent_name=context.agent_name,
            messages=copy.deepcopy(context.messages) if context.messages else None,
            llm_response=copy.deepcopy(context.llm_response) if context.llm_response else None,
            tool_call=copy.deepcopy(context.tool_call) if context.tool_call else None,
            tool_result=copy.deepcopy(context.tool_result) if context.tool_result else None,
            output=context.output,  # String is immutable
            metadata=copy.deepcopy(context.metadata),
            step=context.step,
            llm=context.llm,  # Reference copy
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
    
    def get_stats(self, hook_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get execution statistics.
        
        Args:
            hook_name: Optional hook name to get stats for (None = all hooks)
            
        Returns:
            Statistics dictionary
        """
        if hook_name:
            return dict(self._stats.get(hook_name, {}))
        return {name: dict(stats) for name, stats in self._stats.items()}
    
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
