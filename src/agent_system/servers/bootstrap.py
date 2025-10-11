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
    
    Modern approach:
    - All plugins use (name, system_config, mcp_config) constructor
    - No legacy workarounds or parent_llm injection
    - MCP config passed directly to plugins via mcp_config parameter
    """
    # Ensure mcp_system exists
    if not config.mcp_system:
        logger.warning("No mcp_system configuration found, skipping bootstrap")
        return
    
    # Discover plugins from configured directories
    configured = config.mcp_system.plugin_dirs or []
    dirs = [Path(p) for p in configured if p]
    
    # Fallback: check current working directory for plugins/
    if not dirs:
        cwd_plugins = Path.cwd() / "plugins"
        if cwd_plugins.exists() and cwd_plugins.is_dir():
            dirs = [cwd_plugins]
        else:
            # Development fallback: src/plugins
            src_plugins = Path.cwd() / "src" / "plugins"
            if src_plugins.exists() and src_plugins.is_dir():
                dirs = [src_plugins]
    
    # Always include src/plugins as fallback for development
    src_plugins = Path.cwd() / "src" / "plugins"
    if src_plugins.exists() and src_plugins.is_dir() and src_plugins not in dirs:
        dirs.append(src_plugins)

    plugins = discover_all_plugins(dirs=dirs if dirs else None)

    # Discover configuration-based agents (Epic 0043)
    config_agents = discover_config_agents(config.mcp_system)
    
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
        logger.debug("No external MCP plugins discovered in %s", dirs)

    # Instantiate enabled servers (each gets its MCPConfig from mcp_system.servers)
    enabled_servers = [k for k, v in config.mcp_system.servers.items() if v.enabled]
    
    # Add config-based agents to enabled_servers (they have their own enabled flag)
    if config_agents:
        for agent_name in config_agents.keys():
            if agent_name not in enabled_servers:
                enabled_servers.append(agent_name)
                logger.debug(f"Added config agent '{agent_name}' to enabled_servers")
    
    for key in enabled_servers:
        # Use get_mcp_config_by_name to merge default_config with server-specific config
        # For config agents, create MCPConfig from their definition if not in servers
        if key in config_agents and key not in config.mcp_system.servers:
            # Config agent not in servers section - create MCPConfig from definition
            from ..config.models import MCPConfig
            agent_def = config.mcp_system.config_agents[key]
            server_mcp_cfg = MCPConfig(
                type=agent_def.base_type or "agent",
                enabled=True,
                agent_config=agent_def.agent_config
            )
            logger.debug(f"Created MCPConfig for config agent '{key}' from definition")
        else:
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
                    
            except Exception as e:
                logger.exception("Failed to instantiate plugin '%s' for server '%s': %s", typ, key, e)
                if "test" in str(Path.cwd()):
                    raise
            continue

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
