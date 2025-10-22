"""Server bootstrap for MCP plugin system.

Discovers and instantiates all configured MCP servers (plugins).
All plugins now use modern constructor: (name, system_config, mcp_config)
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config.models import AgentSystemConfig
from ..config.settings import get_mcp_config_by_name
from ..mcp.base import MCPRegistry
from ..plugins import discover_all_plugins

logger = logging.getLogger(__name__)


def bootstrap_servers(config: AgentSystemConfig, registry: MCPRegistry) -> None:
    """Discover and register all configured MCP servers.
    
    Uses config.plugins for local plugin servers.
    All plugins use (name, system_config, mcp_config) constructor.
    """
    # Check for plugins configuration
    if not config.plugins:
        logger.warning("No plugins configuration found, skipping bootstrap")
        return
    
    # Discover plugins from configured directories
    configured = config.plugins.plugin_dirs if config.plugins else []
    dirs = [Path(p) for p in configured if p]

    plugins = discover_all_plugins(dirs=dirs if dirs else None)

    # Log discovered plugins
    if plugins:
        logger.info("Discovered MCP plugins: %s", ", ".join(sorted(plugins.keys())))
    else:
        logger.debug("No external MCP plugins discovered")

    # Instantiate enabled servers from config.plugins.servers
    if not config.plugins:
        enabled_servers = []
    else:
        enabled_servers = [k for k, v in config.plugins.servers.items() if v.enabled]
    
    for key in enabled_servers:
        # Use get_mcp_config_by_name to merge default_config with server-specific config
        server_mcp_cfg = get_mcp_config_by_name(key, config)
        
        if not server_mcp_cfg:
            logger.warning("Failed to resolve MCP config for server '%s', skipping", key)
            continue
            
        logger.debug(f"Bootstrap server '{key}': type={server_mcp_cfg.type}, enabled={server_mcp_cfg.enabled}")
        typ = server_mcp_cfg.type
        
        # Check if plugin provides this type
        if typ in plugins:
            factory = plugins[typ]
            
            # Log plugin metadata if available
            meta = getattr(factory, '_plugin_metadata', None)
            if meta:
                desc = meta.get('description') or meta.get('summary') or ''
                ver = meta.get('version') or ''
                logger.info("Using plugin '%s' (version=%s) for server '%s': %s", typ, ver, key, desc)
        else:
            factory = None
            
        if factory:
            try:
                # MODERN: All plugins use (name, system_config, mcp_config) signature
                # Pass the MCPConfig object directly (not dict)
                inst = factory(key, config, server_mcp_cfg)
                registry.register(key, inst)
                
                # CRITICAL FIX: Also register in global plugin_mcp_registry to prevent duplicate instantiation
                # during MCP integration initialization (which uses plugin_mcp_registry)
                from ..plugins.mcp_adapter import plugin_mcp_registry
                plugin_mcp_registry.register_existing_plugin_instance(key, inst, config, server_mcp_cfg)
                logger.debug(f"Registered plugin '{key}' in both registries (MCPRegistry + PluginMCPRegistry)")
                
                # Agent plugins need shared registry access to call other plugins
                from agent_system.servers.agent.server import Agent as _Agent
                if isinstance(inst, _Agent):
                    inst.registry = registry
                    logger.debug("Updated agent %s to use shared registry with %d servers", 
                               key, len(registry._servers))
                    
                    # Apply instance-level metadata from MCPConfig (if provided)
                    if server_mcp_cfg.metadata:
                        inst._metadata = server_mcp_cfg.metadata.model_dump()
                        logger.debug("Applied instance metadata to agent '%s': %s", key, inst._metadata)
                    
                    # Apply instance-level description from MCPConfig (if provided)
                    if server_mcp_cfg.description:
                        inst._description = server_mcp_cfg.description
                        logger.debug("Applied instance description to agent '%s': %s", key, server_mcp_cfg.description)
                    
                    # Set visibility for plugin agents
                    # Priority: 1. MCPConfig.metadata.visibility, 2. plugin.yaml visibility, 3. default "private"
                    if not hasattr(inst, '_visibility_set_explicitly'):
                        visibility = "private"  # Default: not visible (secure by default)
                        
                        # Check MCPConfig metadata first (instance-level override)
                        if server_mcp_cfg.metadata and server_mcp_cfg.metadata.visibility:
                            visibility = server_mcp_cfg.metadata.visibility
                            logger.debug("Plugin agent '%s' using visibility from MCPConfig: %s", key, visibility)
                        # Check plugin.yaml metadata for visibility
                        elif hasattr(factory, '_plugin_metadata') and factory._plugin_metadata:
                            plugin_vis = factory._plugin_metadata.get('visibility')
                            if plugin_vis in ["ui", "tool", "both", "private"]:
                                visibility = plugin_vis
                                logger.debug("Plugin agent '%s' using visibility from plugin.yaml: %s", key, visibility)
                        
                        # Map visibility to flags
                        inst._mcp_public = visibility in ["ui", "both"]
                        inst._mcp_tool_visible = visibility in ["tool", "both"]
                        logger.debug("Plugin agent '%s' visibility set: %s (ui=%s, tool=%s)", 
                                   key, visibility, inst._mcp_public, inst._mcp_tool_visible)
                    
                    # Apply self_tool_descriptions from MCPConfig (if provided)
                    if server_mcp_cfg.self_tool_descriptions:
                        inst._self_tool_descriptions = server_mcp_cfg.self_tool_descriptions
                        logger.debug("Applied self_tool_descriptions to agent '%s': %d overrides", 
                                   key, len(inst._self_tool_descriptions))
                    
            except Exception as e:
                logger.exception("Failed to instantiate plugin '%s' for server '%s': %s", typ, key, e)
                if "test" in str(Path.cwd()):
                    raise
        # Direct agent type (non-plugin)
        elif typ == "agent":
            from .agent.server import Agent
            
            # Agent uses (name, system_config, mcp_config, registry) constructor
            agent_registry = MCPRegistry()
            registry.register(key, Agent(key, config, server_mcp_cfg, agent_registry))
        else:
            logger.warning("Unknown server type '%s' for server '%s'", typ, key)
            continue

    # Log registered servers
    try:
        logger.info("Registered MCP servers: %s", ", ".join(registry.list()))
    except Exception:
        logger.debug("Could not list registered servers after bootstrap")
