"""
Shared agent runner functionality for both agent-run and agent-cli.

This module provides centralized logic for:
- Agent selection and creation
- LLM profile override
- Agent execution with status monitoring
- Error handling and reporting
"""
from __future__ import annotations

import logging
from typing import Optional, Any, Tuple

from ..config.settings import AgentSystemConfig, get_mcp_config_by_name
from ..config.models import AgentConfig
from ..mcp.base import MCPRegistry
from ..servers.agent.server import Agent


logger = logging.getLogger(__name__)


def get_agent_with_llm_override(
    config: AgentSystemConfig,
    registry: MCPRegistry,
    agent_name: str,
    llm_profile: Optional[str] = None
) -> Tuple[Agent, Optional[Any], Optional[str]]:
    """Get agent instance with optional LLM profile override.
    
    This function mirrors the functionality from interface_api's _get_agent_with_overrides.
    It handles both plugin-based agents and config-based agents.
    
    Args:
        config: System configuration
        registry: MCP registry containing all servers
        agent_name: Name of the agent to get
        llm_profile: Optional LLM profile to override agent's default
        
    Returns:
        Tuple of (agent_instance, llm_override, llm_profile_info)
        - agent_instance: The selected agent from registry
        - llm_override: LLM client to pass to run_events (None if using agent's default)
        - llm_profile_info: Profile info string for status display (None if no override)
        
    Raises:
        ValueError: If agent not found or LLM profile invalid
    """
    # Get agent from registry
    try:
        agent = registry.get(agent_name)
    except KeyError:
        # Build helpful error message with available agents
        available_agents = []
        
        # Get agents from plugins.servers
        if config.plugins and config.plugins.servers:
            available_agents.extend([
                name for name, server in config.plugins.servers.items()
                if server.enabled and server.agent_config is not None
            ])
        
        # Remove duplicates and sort
        available_agents = sorted(set(available_agents))
        
        error_msg = f"Agent '{agent_name}' not found in configuration."
        if available_agents:
            error_msg += "\n\nAvailable agents:\n  " + "\n  ".join(available_agents)
        else:
            error_msg += "\n\nNo agents are configured. Check your config files."
        
        raise ValueError(error_msg)
    
    # Verify it's actually an Agent instance
    if not isinstance(agent, Agent):
        raise ValueError(f"'{agent_name}' is not an agent (found: {type(agent).__name__})")
    
    # Create LLM override if profile specified
    llm_override = None
    llm_profile_info = None
    
    if llm_profile and config.llm_system and config.llm_system.profiles:
        if llm_profile not in config.llm_system.profiles:
            available_profiles = sorted(config.llm_system.profiles.keys())
            error_msg = f"LLM profile '{llm_profile}' not found in configuration."
            if available_profiles:
                error_msg += "\n\nAvailable profiles:\n  " + "\n  ".join(available_profiles)
            raise ValueError(error_msg)
        
        try:
            # Use factory function that properly handles batch mode
            from ..llm.factory import create_llm_from_profile, resolve_llm_config_for_agent
            
            llm_override = create_llm_from_profile(
                config=config,
                llm_profile=llm_profile,
            )
            
            # Get profile info for status display
            temp_agent_config = AgentConfig(llm_profile=llm_profile)
            llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
            model = llm_kwargs.get('model', 'unknown')
            provider = llm_kwargs.get('provider', 'unknown')
            llm_profile_info = f"{llm_profile}:{provider}/{model}"
            
            logger.info(f"Using LLM override: {llm_profile_info}")
        except Exception as e:
            logger.error(f"Failed to create LLM override: {e}", exc_info=True)
            raise ValueError(f"Failed to apply LLM profile '{llm_profile}': {str(e)}")
    
    return agent, llm_override, llm_profile_info


async def create_and_register_agent(
    config: AgentSystemConfig,
    registry: MCPRegistry,
    agent_name: str,
    session_service=None
) -> Agent:
    """Create and register an agent if it doesn't already exist in registry.
    
    This handles both plugin-based agents (from plugins.servers) and
    config-based agents (from agents.yaml).
    
    Args:
        config: System configuration
        registry: MCP registry to register agent in
        agent_name: Name of the agent to create
        session_service: Optional SessionService to inject into agent
        
    Returns:
        Agent instance (either newly created or existing from registry)
        
    Raises:
        ValueError: If agent configuration not found or invalid
    """
    # Check if agent already exists in registry
    try:
        existing_agent = registry.get(agent_name)
        if isinstance(existing_agent, Agent):
            logger.info(f"Using existing agent '{agent_name}' from registry")
            # Update session_service for existing agent
            if session_service and hasattr(existing_agent, '_session_service'):
                existing_agent._session_service = session_service
            return existing_agent
    except KeyError:
        pass  # Agent doesn't exist, need to create it
    
    # Try to get MCP config from plugins.servers
    mcp_config = get_mcp_config_by_name(agent_name, config)
    
    if not mcp_config:
        # Build helpful error message
        available_agents = []
        
        # Get agents from plugins.servers
        if config.plugins and config.plugins.servers:
            available_agents.extend([
                name for name, server in config.plugins.servers.items()
                if server.enabled and server.agent_config is not None
            ])
        
        # Remove duplicates and sort
        available_agents = sorted(set(available_agents))
        
        error_msg = f"Agent '{agent_name}' not found in configuration."
        if available_agents:
            error_msg += "\n\nAvailable agents:\n  " + "\n  ".join(available_agents)
        else:
            error_msg += "\n\nNo agents are configured. Check your config files."
        
        raise ValueError(error_msg)
    
    if not mcp_config.agent_config:
        raise ValueError(f"Agent '{agent_name}' has no agent_config section")
    
    # Create the agent using the signature: Agent(name, system_config, mcp_config, registry, session_service)
    agent = Agent(agent_name, config, mcp_config, registry, session_service=session_service)
    
    # Make agent public so it shows up in tool lists if needed
    agent._mcp_public = True
    
    # Register the agent in the registry
    registry.register(agent_name, agent)
    
    logger.info(f"Created agent '{agent_name}' with LLM profile '{mcp_config.agent_config.llm_profile}'")
    return agent
