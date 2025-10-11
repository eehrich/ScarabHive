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
from ..plugins.config_agent_discovery import discover_config_agents

logger = logging.getLogger(__name__)


def bootstrap_servers(config: AgentSystemConfig, registry: MCPRegistry) -> None:
    """Discover and register all configured MCP servers.
    
    New structure (Epic 0044):
    - Uses config.plugins for local plugin servers
    - Uses config.agents for configuration-based agents
    - All plugins use (name, system_config, mcp_config) constructor
    - No legacy mcp_system support
    """
    # Check for new structure
    if not config.plugins and not config.agents:
        logger.warning("No plugins or agents configuration found, skipping bootstrap")
        return
    
    # Discover plugins from configured directories
    configured = config.plugins.plugin_dirs if config.plugins else []
    dirs = [Path(p) for p in configured if p]

    plugins = discover_all_plugins(dirs=dirs if dirs else None)

    # Discover configuration-based agents (Epic 0043)
    # Pass agents dict directly (no longer needs MCPSystemConfig wrapper)
    config_agents = discover_config_agents(config.agents)
    
    # Merge config agents into plugins dict (config agents override if name conflicts)
    if config_agents:
        logger.info(
            f"Discovered {len(config_agents)} configuration-based agents: "
            f"{', '.join(sorted(config_agents.keys()))}"
        )
        # Config agents take precedence over plugin-based agents with same name
        for name, factory in config_agents.items():
            if name in plugins:
                logger.warning(
                    f"Config agent '{name}' overrides plugin-based agent with same name"
                )
            plugins[name] = factory

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
    
    # Add enabled config-based agents to enabled_servers
    # Config agents are already filtered by enabled flag in discover_config_agents
    if config_agents:
        for agent_name in config_agents.keys():
            if agent_name not in enabled_servers:
                enabled_servers.append(agent_name)
                logger.debug(f"Added config agent '{agent_name}' to enabled_servers")
    
    for key in enabled_servers:
        # Use get_mcp_config_by_name to merge default_config with server-specific config
        # For config agents, create MCPConfig from their definition if not in servers
        if key in config_agents:
            plugins_servers = config.plugins.servers if config.plugins else {}
            if key not in plugins_servers:
                # Config agent not in servers section - create MCPConfig from definition
                from ..config.models import MCPConfig
                agent_def_data = config.agents.get(key) if config.agents else None
                if not agent_def_data:
                    logger.warning(f"Config agent '{key}' not found in config.agents, skipping")
                    continue
                    
                server_mcp_cfg = MCPConfig(
                    type=agent_def_data.base_type if hasattr(agent_def_data, 'base_type') else "agent",
                    enabled=True,
                    agent_config=agent_def_data.agent_config if hasattr(agent_def_data, 'agent_config') else None
                )
                logger.debug(f"Created MCPConfig for config agent '{key}' from definition")
            else:
                server_mcp_cfg = get_mcp_config_by_name(key, config)
        else:
            server_mcp_cfg = get_mcp_config_by_name(key, config)
        
        if not server_mcp_cfg:
            logger.warning("Failed to resolve MCP config for server '%s', skipping", key)
            continue
            
        logger.debug(f"Bootstrap server '{key}': type={server_mcp_cfg.type}, enabled={server_mcp_cfg.enabled}")
        typ = server_mcp_cfg.type
        
        # For config-based agents, use the agent's factory (not the base_type's plugin factory)
        # This ensures multi-section prompts and config-specific settings are applied
        if key in config_agents:
            factory = config_agents[key]
            logger.debug(f"Using config-based agent factory for '{key}' (base_type={typ})")
        # Check if plugin provides this type
        elif typ in plugins:
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
                
                # Agent plugins need shared registry access to call other plugins
                from agent_system.servers.agent.server import Agent as _Agent
                if isinstance(inst, _Agent):
                    inst.registry = registry
                    logger.debug("Updated agent %s to use shared registry with %d servers", 
                               key, len(registry._servers))
                    
                    # Set visibility for plugin agents
                    # 1. Check if plugin.yaml has visibility field
                    # 2. Otherwise use default "private" (secure by default)
                    if not hasattr(inst, '_visibility_set_explicitly'):
                        visibility = "private"  # Default: not visible
                        
                        # Check plugin.yaml metadata for visibility
                        if hasattr(factory, '_plugin_metadata') and factory._plugin_metadata:
                            plugin_vis = factory._plugin_metadata.get('visibility')
                            if plugin_vis in ["ui", "tool", "both", "private"]:
                                visibility = plugin_vis
                                logger.debug("Plugin agent '%s' using visibility from plugin.yaml: %s", key, visibility)
                        
                        # Map visibility to flags
                        inst._mcp_public = visibility in ["ui", "both"]
                        inst._mcp_tool_visible = visibility in ["tool", "both"]
                        logger.debug("Plugin agent '%s' visibility set: %s (ui=%s, tool=%s)", 
                                   key, visibility, inst._mcp_public, inst._mcp_tool_visible)
                    
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
