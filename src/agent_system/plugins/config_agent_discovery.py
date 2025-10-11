"""
Config Agent Discovery Module

Discovers and registers configuration-based agents from agents.yaml.
Part of Epic 0043: Configuration-Based Agents.

This module scans the agents section of the configuration and creates
factory functions for each enabled agent, making them available through the
plugin registry.
"""

from typing import Dict, Callable, Any, Optional
import logging

from ..config.models import ConfigBasedAgentDefinition
from ..servers.config_agent_factory import create_config_based_agent_factory
from .config_agent_validation import validate_config_agent, ConfigAgentValidationError


logger = logging.getLogger(__name__)


def discover_config_agents(
    agents_config: Optional[Dict[str, ConfigBasedAgentDefinition]]
) -> Dict[str, Callable[..., Any]]:
    """
    Discover configuration-based agents from agents_config.
    
    Scans the agents dictionary and creates factory functions for each
    enabled agent. The factories are compatible with the plugin system and
    can be registered in the plugin_registry.
    
    Args:
        agents_config: Dict mapping agent name to ConfigBasedAgentDefinition
    
    Returns:
        Dict mapping agent_name -> factory_function (with _plugin_metadata attached)
        Ready for registration in plugin_registry
    
    Example:
        >>> config = load_settings()
        >>> discovered = discover_config_agents(config.agents)
        >>> for name, factory in discovered.items():
        ...     plugin_registry.register(name, factory)
    """
    discovered: Dict[str, Callable[..., Any]] = {}
    
    # Check if agents config exists
    if not agents_config:
        logger.info("No agents section found in configuration")
        return discovered
    
    logger.info(
        f"Discovering config-based agents: "
        f"{len(agents_config)} definitions found"
    )
    
    # Process each config agent definition
    for agent_name, definition in agents_config.items():
        try:
            # Skip disabled agents
            if not definition.enabled:
                logger.debug(f"Skipping disabled config agent: {agent_name}")
                continue
            
            # Validate agent definition with detailed error messages
            validation_errors = validate_config_agent(agent_name, definition)
            if validation_errors:
                logger.error(
                    f"❌ Config agent '{agent_name}' failed validation:\n" +
                    "\n".join(f"  - {err}" for err in validation_errors)
                )
                raise ConfigAgentValidationError(agent_name, validation_errors)
            
            # Create factory function
            factory = create_config_based_agent_factory(
                name=agent_name,
                definition=definition,
                global_mcp_config=None  # No global default in new structure
            )
            
            # Attach plugin metadata as dictionary (like filesystem plugins)
            metadata_dict = {
                "name": agent_name,
                "description": definition.description or f"Config-based agent: {agent_name}",
                "type": "agent",  # or definition.base_type
                "source": "config",  # Mark as config-based (vs "filesystem" for code-based)
                "version": definition.metadata.get("version", "1.0.0") if definition.metadata else "1.0.0",
                "author": definition.metadata.get("author", "Unknown") if definition.metadata else "Unknown",
                "tags": definition.metadata.get("tags", []) if definition.metadata else [],
                "category": definition.metadata.get("category") if definition.metadata else None
            }
            
            try:
                setattr(factory, "_plugin_metadata", metadata_dict)
            except Exception as e:
                logger.debug(f"Failed to attach metadata to factory: {e}")
            
            discovered[agent_name] = factory
            
            logger.info(
                f"✅ Discovered config agent '{agent_name}': "
                f"{definition.description[:60]}..." if len(definition.description or "") > 60 
                else f"✅ Discovered config agent '{agent_name}': {definition.description}"
            )
        
        except Exception as e:
            logger.error(
                f"❌ Failed to discover config agent '{agent_name}': {e}",
                exc_info=True
            )
            # Continue with other agents even if one fails
            continue
    
    logger.info(
        f"Config agent discovery complete: "
        f"{len(discovered)}/{len(agents_config.config_agents)} agents discovered"
    )
    
    return discovered


def _validate_agent_definition(name: str, definition: Any) -> None:
    """
    Validate a config agent definition.
    
    Args:
        name: Agent name (for error messages)
        definition: ConfigBasedAgentDefinition to validate
    
    Raises:
        ValueError: If definition is invalid
    """
    # Check base_type
    if definition.base_type not in ["agent", "server"]:
        raise ValueError(
            f"Invalid base_type '{definition.base_type}' for agent '{name}'. "
            f"Must be 'agent' or 'server'"
        )
    
    # Check agent_config exists
    if not definition.agent_config:
        raise ValueError(f"Missing agent_config for agent '{name}'")
    
    # Check LLM profile is specified
    if not definition.agent_config.llm_profile:
        raise ValueError(f"Missing llm_profile in agent_config for agent '{name}'")
    
    # Check max_steps is positive
    if definition.agent_config.max_steps and definition.agent_config.max_steps <= 0:
        raise ValueError(
            f"Invalid max_steps ({definition.agent_config.max_steps}) for agent '{name}'. "
            f"Must be positive"
        )
    
    # Check system prompt or template is provided
    has_prompt = (
        definition.agent_config.system_prompt or
        definition.agent_config.system_template
    )
    if not has_prompt:
        logger.warning(
            f"Agent '{name}' has no system_prompt or system_template. "
            f"Will use default prompt."
        )
    
    # Validate tools config if present
    if definition.agent_config.tools:
        tools = definition.agent_config.tools
        if tools.allowed and not isinstance(tools.allowed, list):
            raise ValueError(
                f"Invalid tools.allowed for agent '{name}'. Must be a list"
            )
        if tools.blocked and not isinstance(tools.blocked, list):
            raise ValueError(
                f"Invalid tools.blocked for agent '{name}'. Must be a list"
            )
    
    logger.debug(f"Validation passed for config agent: {name}")


def get_config_agent_info(agent_name: str, agents_config: Optional[Dict[str, ConfigBasedAgentDefinition]]) -> Dict[str, Any]:
    """
    Get detailed information about a specific config agent.
    
    Useful for API endpoints and CLI commands to inspect config agents.
    
    Args:
        agent_name: Name of the config agent
        agents_config: Dict mapping agent name to ConfigBasedAgentDefinition
    
    Returns:
        Dict with agent information
    
    Raises:
        KeyError: If agent not found
    """
    if not agents_config or agent_name not in agents_config:
        raise KeyError(f"Config agent '{agent_name}' not found")
    
    definition = agents_config[agent_name]
    
    return {
        "name": agent_name,
        "enabled": definition.enabled,
        "description": definition.description,
        "base_type": definition.base_type,
        "llm_profile": definition.agent_config.llm_profile,
        "max_steps": definition.agent_config.max_steps,
        "system_template": definition.agent_config.system_template,
        "has_inline_prompt": bool(definition.agent_config.system_prompt),
        "tools": {
            "allowed": definition.agent_config.tools.allowed if definition.agent_config.tools else [],
            "blocked": definition.agent_config.tools.blocked if definition.agent_config.tools else []
        } if definition.agent_config.tools else None,
        "context_management": {
            "enabled": definition.agent_config.context_management.enabled,
            "strategy": definition.agent_config.context_management.strategy,
            "preserve_recent_messages": definition.agent_config.context_management.preserve_recent_messages
        } if definition.agent_config.context_management else None,
        "metadata": definition.metadata or {}
    }


def list_config_agents(agents_config: Optional[Dict[str, ConfigBasedAgentDefinition]]) -> list[Dict[str, Any]]:
    """
    List all config agents with basic information.
    
    Args:
        agents_config: Dict mapping agent name to ConfigBasedAgentDefinition
    
    Returns:
        List of dicts with agent information
    """
    if not agents_config:
        return []
    
    agents = []
    for name, definition in agents_config.items():
        agents.append({
            "name": name,
            "enabled": definition.enabled,
            "description": definition.description,
            "llm_profile": definition.agent_config.llm_profile,
            "max_steps": definition.agent_config.max_steps,
            "metadata": definition.metadata or {}
        })
    
    return agents
