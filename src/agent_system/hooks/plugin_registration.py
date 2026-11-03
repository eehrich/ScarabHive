"""Registering the hooks a plugin declares in its schema with the HookRegistry.

Two entry points walk all plugins: ToolServerIntegration (the HTTP agent
server) and :func:`register_bootstrapped_plugin_hooks` (processes that only
bootstrap the servers). Both go through :func:`register_all_plugin_hooks`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


async def register_plugin_hooks(
    plugin_name: str,
    plugin_instance: Any,
    metadata: Dict[str, Any] | None = None,
    registry: Any | None = None,
    hooks_config: Any | None = None,
    instance_hook_config: Dict[str, Any] | None = None,
) -> List[str]:
    """Register hooks declared in plugin metadata.
    
    Args:
        plugin_name: Name of the plugin
        plugin_instance: Instantiated plugin server (should implement PluginHook for hook-enabled plugins)
        metadata: Plugin metadata from plugin.toml (optional, will use _plugin_metadata attribute if not provided)
        registry: Hook registry to use (optional, will use global registry if not provided)
        hooks_config: Global hooks configuration, ``load_hooks_config(settings)``
            (optional; defaults -- no overrides -- if not provided)
        instance_hook_config: The server INSTANCE's ``hook_config`` mapping from
            its (merged) ToolServerConfig. ``enabled: false`` disables every hook of
            this instance at registration (below the operator's global
            ``hooks.overrides``); any other value changes nothing. Lower-only,
            so an instance can ship dark next to an enabled sibling, while
            raising a schema default stays an operator decision.
    
    Returns:
        List of registered hook names
    
    Raises:
        ValueError: If hook configuration is invalid
        TypeError: If plugin doesn't implement PluginHook interface
    """
    from . import get_hook_registry, PluginHook, HookType, load_hooks_config
    
    registered_hooks: list[str] = []
    
    # Get metadata from parameter or plugin instance attribute
    if metadata is None:
        metadata = getattr(plugin_instance, '_plugin_metadata', None)
    
    if not metadata:
        logger.debug(f"Plugin '{plugin_name}' has no metadata, skipping hook registration")
        return registered_hooks
    
    # Defaults when the caller passes no global hook settings
    if hooks_config is None:
        hooks_config = load_hooks_config()
    
    hooks_list = metadata.get('hooks', [])
    if not hooks_list:
        logger.debug(f"Plugin '{plugin_name}' declares no hooks in metadata")
        return registered_hooks
    
    # Verify plugin implements PluginHook interface OR has the required hook methods
    # Duck-typing: Plugin doesn't need to inherit from PluginHook if it has the hook methods
    if not isinstance(plugin_instance, PluginHook):
        # Check if plugin has hook methods (duck typing for hybrid plugins)
        hook_method_name = f"on_{hooks_list[0].get('type', '').lower()}"
        if not hasattr(plugin_instance, hook_method_name):
            raise TypeError(
                f"Plugin '{plugin_name}' declares hooks but does not implement PluginHook interface "
                f"and is missing hook method '{hook_method_name}'. "
                "The plugin class must either inherit from PluginHook or implement async hook methods."
            )
        logger.debug(f"Plugin '{plugin_name}' uses duck-typed hooks (has {hook_method_name} method)")

    
    # Use provided registry or get global one
    if registry is None:
        registry = get_hook_registry()
    
    for hook_metadata in hooks_list:
        try:
            # Validate required fields
            hook_name = hook_metadata.get('name')
            hook_type_str = hook_metadata.get('type')
            
            if not hook_name:
                logger.warning(f"Plugin '{plugin_name}' has hook with missing 'name', skipping")
                continue
            
            if not hook_type_str:
                logger.warning(f"Plugin '{plugin_name}' hook '{hook_name}' missing 'type', skipping")
                continue
            
            # Parse hook type
            try:
                hook_type = HookType[hook_type_str.upper()]
            except KeyError:
                logger.warning(
                    f"Plugin '{plugin_name}' hook '{hook_name}' has invalid type '{hook_type_str}'. "
                    f"Valid types: {[t.name.lower() for t in HookType]}"
                )
                continue

            # on_error: block -- a pre_tool_call hook that fails blocks the
            # call instead of letting it run. Anything else would read as the
            # default and silently let calls through a policy hook, so say so.
            on_error = hook_metadata.get('on_error')
            if on_error is not None and (on_error != "block" or hook_type != HookType.PRE_TOOL_CALL):
                logger.warning(
                    f"Plugin '{plugin_name}' hook '{hook_name}' has on_error {on_error!r}: only "
                    f"'block' on a pre_tool_call hook has an effect -- a failing hook lets the call run"
                )

            # Get configuration from plugin metadata
            enabled = hook_metadata.get('enabled', True)
            timeout = hook_metadata.get('timeout', hooks_config.default_timeout)
            description = hook_metadata.get('description', '')
            category = hook_metadata.get('category', None)  # Optional category/tag
            order_spec = hook_metadata.get('order', {})
            
            # Instance-level default: the server config's hook_config.enabled
            # speaks for THIS instance (schema speaks for the plugin type).
            # LOWER-ONLY by design: False disables the instance's hooks,
            # True does NOT lift a schema-level off-switch -- the shipped
            # sub_agent_manager config says enabled:true against a schema
            # that deliberately starts the hook disabled, and honouring the
            # True would have flipped that hook on for every agent. Raising
            # a default is the operator's move (global hooks.overrides).
            # Applied before those overrides, so the operator still wins.
            if isinstance(instance_hook_config, dict)                     and instance_hook_config.get('enabled') is False:
                enabled = False

            # Apply global hooks configuration overrides
            # Build full hook name for lookup (plugin.hook_name)
            full_hook_name = f"{plugin_name}.{hook_name}"
            
            # Global config overrides plugin defaults. The exact
            # "plugin.hook" key wins; a plugin-wide "plugin" key applies to
            # every hook of that plugin (plugins.yaml uses both forms).
            override = hooks_config.overrides.get(full_hook_name)
            if override is None:
                override = hooks_config.overrides.get(plugin_name)
            if override is not None:
                if 'enabled' in override:
                    enabled = override['enabled']
                if 'timeout' in override:
                    timeout = override['timeout']
                if 'order' in override:
                    order_spec = override['order']
            
            # Apply global enabled flag if hooks are globally disabled
            if not hooks_config.enabled:
                enabled = False
            
            # Validate order specification
            before_list = order_spec.get('before', [])
            after_list = order_spec.get('after', [])
            
            if not isinstance(before_list, list):
                logger.warning(f"Hook '{hook_name}' has invalid 'before' specification (must be list)")
                before_list = []
            
            if not isinstance(after_list, list):
                logger.warning(f"Hook '{hook_name}' has invalid 'after' specification (must be list)")
                after_list = []
            
            # Check for self-reference
            if hook_name in before_list or hook_name in after_list:
                logger.warning(f"Hook '{hook_name}' references itself in ordering, removing self-reference")
                before_list = [h for h in before_list if h != hook_name]
                after_list = [h for h in after_list if h != hook_name]
            
            order = {
                'before': before_list,
                'after': after_list
            }
            
            # Skip if hook already registered (e.g., HTTP server registered
            # it via ToolServerIntegration AND CLI subprocess calls
            # ``register_bootstrapped_plugin_hooks`` in the same process — or
            # any other re-entry path). Without this guard the second pass
            # raises ValueError per hook and floods the log with tracebacks.
            existing_hooks = registry.list_hooks(hook_type).get(hook_type.value, [])
            if full_hook_name in existing_hooks:
                logger.debug(
                    f"Hook '{full_hook_name}' already registered for "
                    f"{hook_type.value} — skipping duplicate registration"
                )
                registered_hooks.append(full_hook_name)
                continue

            # Register with HookRegistry (it expects a PluginHook instance)
            await registry.register_hook(
                hook_type=hook_type,
                hook_name=full_hook_name,  # Use full name with plugin prefix
                hook=plugin_instance,  # Pass the PluginHook instance
                order_spec=order,
                enabled=enabled,
                timeout=timeout,
                description=description,
                category=category,  # Pass category for grouping
                # Additional metadata for tracking
                plugin=plugin_name,
                source="plugin_discovery",
                on_error=on_error,
            )

            registered_hooks.append(full_hook_name)  # Track full name
            logger.debug(
                f"Registered hook '{full_hook_name}' from plugin '{plugin_name}' "
                f"(type={hook_type.value}, enabled={enabled}, description='{description}')"
            )

        except Exception as e:
            logger.error(
                f"Failed to register hook '{hook_metadata.get('name', 'unknown')}' "
                f"from plugin '{plugin_name}': {e}",
                exc_info=True
            )
            continue

    return registered_hooks


# Override warnings already logged in this process: both registration paths
# (ToolServerIntegration and register_bootstrapped_plugin_hooks) may run in one process.
_REPORTED_OVERRIDE_WARNINGS: Set[str] = set()


def warn_unknown_hook_overrides(settings: Any, registry: Any = None) -> List[str]:
    """Log every ``hooks.overrides`` key that matches no registered hook.

    Overrides are looked up by exact name, so a typo, a short name or the name
    of a renamed instance silently does nothing -- the hook keeps its default.
    Global keys may name a hook (``<instance>.<hook>``) or an instance with
    hooks (``<instance>``, applies to all of them); agent keys only the full
    hook name. Call after all hooks are registered.

    Each message is logged once per process. Returns all messages (for tests).
    """
    from . import get_hook_registry
    from ..config.settings import get_tool_server_config

    if registry is None:
        registry = get_hook_registry()
    hook_names = {name for names in registry.list_hooks().values() for name in names}
    instances_with_hooks = {name.rsplit(".", 1)[0] for name in hook_names}
    warnings: List[str] = []

    global_overrides = getattr(getattr(settings, "hooks", None), "overrides", None) or {}
    for key in global_overrides:
        if key not in hook_names and key not in instances_with_hooks:
            warnings.append(
                f"hooks.overrides key '{key}' matches no registered hook or hook-owning "
                "instance -- it has no effect (misspelled, renamed instance, or its server "
                "is not enabled?)"
            )

    unknown_agent_keys: Dict[str, List[str]] = {}
    servers = getattr(getattr(settings, "plugins", None), "servers", None) or {}
    for server_name, server_cfg in servers.items():
        if not getattr(server_cfg, "enabled", False):
            continue
        try:
            agent_config = getattr(get_tool_server_config(server_name, settings), "agent_config", None)
        except Exception:
            logger.debug("No merged config for '%s'", server_name, exc_info=True)
            continue
        agent_hooks = getattr(agent_config, "hooks", None)
        for key in (getattr(agent_hooks, "overrides", None) or {}):
            # a server that is configured and off: its hook is not there, and nothing is misspelled
            owner = servers.get(key.rsplit(".", 1)[0])
            if key not in hook_names and (owner is None or getattr(owner, "enabled", False)):
                unknown_agent_keys.setdefault(key, []).append(server_name)
    for key, agents in sorted(unknown_agent_keys.items()):
        warnings.append(
            f"agent_config.hooks.overrides key '{key}' (agents: {', '.join(sorted(agents))}) "
            "matches no registered hook -- it has no effect. Use the full "
            "'<instance>.<hook>' name of an enabled server."
        )

    for message in warnings:
        if message not in _REPORTED_OVERRIDE_WARNINGS:
            _REPORTED_OVERRIDE_WARNINGS.add(message)
            logger.warning(message)
    return warnings


_BOOTSTRAPPED_HOOKS_REGISTERED = False


async def register_all_plugin_hooks(plugin_registry: Any, settings: Any | None = None) -> List[str]:
    """Register the hooks of every plugin in *plugin_registry* (a PluginToolRegistry).

    ``settings`` is the caller's already-loaded AgentSystemConfig; it supplies
    the global ``hooks:`` overrides and each instance's ``hook_config``
    registration default (from its MERGED server config -- raw and merged
    differ, and agents get the merged form). For an instance it does not
    know, or without settings, the hook_config the instance was built with
    applies (PluginToolRegistry keeps it). With settings, the
    ``hooks.overrides`` keys are checked once all hooks are in.

    A plugin whose hooks fail to register is logged and skipped; the others
    still register.

    Returns:
        List of all registered hook names (full ``plugin.hook`` form).
    """
    from . import load_hooks_config
    from ..config.settings import get_tool_server_config

    hooks_config = load_hooks_config(settings)
    all_registered: List[str] = []

    for server_name in plugin_registry.list_servers():
        server = plugin_registry.get_server(server_name)
        if not server or not getattr(server, 'plugin_schema', None):
            continue
        plugin_schema = server.plugin_schema
        if 'hooks' not in plugin_schema:
            continue

        # The actual plugin instance (unwrap PluginToolAdapter); for hybrid
        # plugins the hooks live on its hooks_plugin.
        plugin_instance = server.plugin_server if hasattr(server, 'plugin_server') else server
        if hasattr(plugin_instance, 'hooks_plugin'):
            plugin_instance = plugin_instance.hooks_plugin

        # Guarded: one server whose merge does not validate must not take
        # down startup -- it registers on the default it was built with.
        try:
            server_cfg = get_tool_server_config(server_name, settings) if settings else None
        except Exception:
            logger.debug("No merged config for '%s'", server_name, exc_info=True)
            server_cfg = None
        instance_hook_config = (getattr(server_cfg, 'hook_config', None) if server_cfg
                                else getattr(server, 'instance_hook_config', None))

        try:
            registered = await register_plugin_hooks(
                plugin_name=server_name,
                plugin_instance=plugin_instance,
                metadata=plugin_schema,
                hooks_config=hooks_config,
                instance_hook_config=instance_hook_config,
            )
        except Exception as e:
            logger.error(f"Failed to register hooks for plugin '{server_name}': {e}", exc_info=True)
            continue
        if registered:
            logger.debug(f"Registered {len(registered)} hook(s) for plugin '{server_name}': {registered}")
        all_registered.extend(registered)

    if settings is not None:
        warn_unknown_hook_overrides(settings)
    return all_registered


async def register_bootstrapped_plugin_hooks(settings: Any | None = None) -> List[str]:
    """Register hooks for all plugins already loaded into the global plugin registry.

    The HTTP agent server registers plugin hooks via
    ``ToolServerIntegration._register_plugin_hooks``. CLI subprocesses (e.g.
    ``writer_audio produce``) only call ``bootstrap_servers`` and never get
    hooks wired up, so listeners like the message debugger silently miss
    every LLM call made from the CLI.

    Call this once after ``bootstrap_servers`` to mirror the HTTP-server
    behaviour. Subsequent calls are no-ops.

    ``settings`` is the caller's already-loaded AgentSystemConfig; it supplies
    the global ``hooks:`` overrides and each instance's ``hook_config``
    registration default. Without it (or for an instance it does not know)
    the hook_config the instance was built with applies -- no config file is
    re-read here.

    Returns:
        List of all registered hook names (full ``plugin.hook`` form).
    """
    global _BOOTSTRAPPED_HOOKS_REGISTERED
    if _BOOTSTRAPPED_HOOKS_REGISTERED:
        return []

    from ..plugins.tool_adapter import plugin_tool_registry

    all_registered = await register_all_plugin_hooks(plugin_tool_registry, settings)
    _BOOTSTRAPPED_HOOKS_REGISTERED = True
    if all_registered:
        logger.info(
            "register_bootstrapped_plugin_hooks: wired %d plugin hooks", len(all_registered)
        )
    return all_registered
