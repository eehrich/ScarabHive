"""
Central service for injecting dependencies into agents in the MCP registry.

This module provides a unified way to inject session_service and other dependencies
into all agents registered in the MCPRegistry. Used by CLI, API, and agent_run to
ensure consistent dependency injection across all entry points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..mcp.base import MCPRegistry

logger = logging.getLogger(__name__)


def inject_session_service_into_agents(
    registry: MCPRegistry,
    session_service: Any,
) -> int:
    """Inject session_service into all Agent instances in the registry.
    
    Supports both MCPRegistry and PluginMCPRegistry interfaces.
    
    This is critical for sub-agent management and any tools/hooks that need
    access to session storage. Must be called AFTER bootstrap_servers() creates
    all agents.
    
    Args:
        registry: MCPRegistry or PluginMCPRegistry containing registered servers and agents
        session_service: SessionService instance to inject
        
    Returns:
        Number of agents that received session_service injection
        
    Example:
        >>> from agent_system.mcp.base import MCPRegistry
        >>> from agent_system.services.session_service import SessionService
        >>> registry = MCPRegistry()
        >>> session_service = SessionService(session_manager)
        >>> count = inject_session_service_into_agents(registry, session_service)
        >>> logger.info(f"Injected session_service into {count} agents")
    """
    # Import here to avoid circular dependency
    from ..servers.agent.server import Agent
    
    # Detect registry type and use appropriate interface
    if hasattr(registry, 'list'):
        # MCPRegistry interface
        server_names = registry.list()
        get_server_func = registry.get
        logger.debug("[inject] Using MCPRegistry interface (list/get)")
    elif hasattr(registry, 'list_servers'):
        # PluginMCPRegistry interface
        server_names = registry.list_servers()
        get_server_func = registry.get_server
        logger.debug("[inject] Using PluginMCPRegistry interface (list_servers/get_server)")
    else:
        raise ValueError(
            f"Unknown registry type: {type(registry).__name__} - "
            f"must have list() or list_servers() method"
        )
    
    injected_count = 0
    
    for server_name in server_names:
        try:
            server = get_server_func(server_name)
            
            # PluginMCPRegistry wraps servers in PluginMCPAdapter - unwrap if needed
            if hasattr(server, 'plugin_server'):
                actual_server = server.plugin_server
            else:
                actual_server = server
            
            # Only inject into Agent instances (not other MCP servers)
            if isinstance(actual_server, Agent):
                # Inject session_service
                actual_server._session_service = session_service
                logger.debug(f"[inject] session_service -> agent '{server_name}'")
                injected_count += 1
                
        except Exception as e:
            logger.debug(
                f"[inject] Failed to inject session_service into '{server_name}': {e}"
            )
    
    if injected_count > 0:
        logger.info(
            f"[inject] Successfully injected session_service into {injected_count} agent(s)"
        )
    else:
        logger.warning(
            "[inject] No agents found in registry for session_service injection"
        )
    
    return injected_count


def inject_dependencies_into_agents(
    registry: MCPRegistry,
    session_service: Any = None,
    **additional_deps: Any,
) -> dict[str, int]:
    """Inject multiple dependencies into all Agent instances in the registry.
    
    Supports both MCPRegistry and PluginMCPRegistry interfaces.
    
    Generic injection function that can handle session_service and any future
    dependencies that need to be injected into agents.
    
    Args:
        registry: MCPRegistry or PluginMCPRegistry containing registered servers and agents
        session_service: SessionService instance to inject (optional)
        **additional_deps: Additional dependencies to inject as _<name> attributes
        
    Returns:
        Dict mapping dependency names to injection counts
        
    Example:
        >>> counts = inject_dependencies_into_agents(
        ...     registry,
        ...     session_service=session_service,
        ...     custom_service=my_service
        ... )
        >>> # Agents now have _session_service and _custom_service attributes
    """
    from ..servers.agent.server import Agent
    
    # Detect registry type and use appropriate interface
    if hasattr(registry, 'list'):
        # MCPRegistry interface
        server_names = registry.list()
        get_server_func = registry.get
    elif hasattr(registry, 'list_servers'):
        # PluginMCPRegistry interface
        server_names = registry.list_servers()
        get_server_func = registry.get_server
    else:
        raise ValueError(
            f"Unknown registry type: {type(registry).__name__} - "
            f"must have list() or list_servers() method"
        )
    
    results = {}
    
    # Build list of dependencies to inject
    deps_to_inject = {}
    if session_service is not None:
        deps_to_inject['_session_service'] = session_service
    
    for dep_name, dep_value in additional_deps.items():
        if not dep_name.startswith('_'):
            dep_name = f'_{dep_name}'
        deps_to_inject[dep_name] = dep_value
    
    # Inject each dependency
    for dep_name, dep_value in deps_to_inject.items():
        injected_count = 0
        
        for server_name in server_names:
            try:
                server = get_server_func(server_name)
                
                # PluginMCPRegistry wraps servers in PluginMCPAdapter - unwrap if needed
                if hasattr(server, 'plugin_server'):
                    actual_server = server.plugin_server
                else:
                    actual_server = server
                
                if isinstance(actual_server, Agent):
                    setattr(actual_server, dep_name, dep_value)
                    injected_count += 1
                    
            except Exception as e:
                logger.debug(
                    f"[inject] Failed to inject {dep_name} into '{server_name}': {e}"
                )
        
        results[dep_name] = injected_count
        
        if injected_count > 0:
            logger.info(
                f"[inject] Successfully injected {dep_name} into {injected_count} agent(s)"
            )
    
    return results
