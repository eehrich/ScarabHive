"""
Config Agent Factory Module

Creates plugin-compatible factory functions for configuration-based agents.
Part of Epic 0043: Configuration-Based Agents.

This module enables agents defined purely through configuration (YAML) to be
instantiated and registered alongside plugin-based agents without requiring
custom Python code.
"""

from typing import Callable, Any, Optional
import logging
from pathlib import Path

from ..config.models import ConfigBasedAgentDefinition, MCPConfig
from ..servers.agent import build_agent_server


logger = logging.getLogger(__name__)


def create_config_based_agent_factory(
    name: str,
    definition: ConfigBasedAgentDefinition,
    global_mcp_config: Optional[MCPConfig] = None
) -> Callable[..., Any]:
    """
    Create a factory function for a configuration-based agent.
    
    The returned factory is compatible with the plugin system's signature
    and can be registered in the plugin_registry just like plugin-based agents.
    
    Args:
        name: Unique name for the agent (e.g., "financial_analyst")
        definition: ConfigBasedAgentDefinition from config
        global_mcp_config: Optional global MCPConfig for defaults/overrides
    
    Returns:
        Factory function with signature: (mcp_config: MCPConfig) -> server_instance
    
    Example:
        >>> definition = config.config_agents["financial_analyst"]
        >>> factory = create_config_based_agent_factory("financial_analyst", definition)
        >>> metadata = PluginMetadata(
        ...     name="financial_analyst",
        ...     description=definition.description,
        ...     type=PluginType.SERVER,
        ...     source="config"
        ... )
        >>> registry.register("financial_analyst", factory, metadata)
    """
    
    def config_agent_factory(mcp_config: MCPConfig) -> Any:
        """
        Factory function that creates an agent server instance.
        
        This function merges the config-based agent definition with the
        runtime MCPConfig to create a fully configured agent.
        
        Args:
            mcp_config: Runtime MCPConfig (may contain overrides)
        
        Returns:
            Initialized agent server instance
        """
        logger.info(f"Creating config-based agent: {name}")
        
        # Merge configurations (runtime config takes precedence)
        merged_config = _merge_agent_configs(
            base_config=definition.agent_config,
            mcp_config=mcp_config,
            global_config=global_mcp_config
        )
        
        # Load system prompt (from template file or inline)
        system_prompt = _load_system_prompt(
            definition=definition,
            agent_name=name
        )
        
        # Build the agent server using the standard build_agent_server function
        # This ensures config-based agents use the same infrastructure as plugin agents
        agent_server = build_agent_server(
            mcp_config=merged_config,
            system_prompt_override=system_prompt
        )
        
        logger.info(
            f"Config agent '{name}' created: "
            f"LLM={merged_config.agent_config.llm_profile}, "
            f"steps={merged_config.agent_config.max_steps}, "
            f"tools={len(merged_config.agent_config.tools.allowed)} allowed"
        )
        
        return agent_server
    
    # Attach metadata to factory for discovery
    config_agent_factory.__name__ = f"config_agent_{name}"
    config_agent_factory.__doc__ = definition.description or f"Config-based agent: {name}"
    config_agent_factory._config_based = True  # type: ignore
    config_agent_factory._config_definition = definition  # type: ignore
    config_agent_factory._config_name = name  # type: ignore
    
    return config_agent_factory


def _merge_agent_configs(
    base_config: Any,
    mcp_config: MCPConfig,
    global_config: Optional[MCPConfig] = None
) -> MCPConfig:
    """
    Merge agent configuration from definition with runtime and global configs.
    
    Priority order (highest to lowest):
    1. Runtime mcp_config (API overrides)
    2. Config-based agent definition
    3. Global default config
    
    Args:
        base_config: AgentConfig from ConfigBasedAgentDefinition
        mcp_config: Runtime MCPConfig
        global_config: Global default MCPConfig
    
    Returns:
        Merged MCPConfig
    """
    # Start with global defaults if available
    if global_config:
        merged = global_config.model_copy(deep=True)
    else:
        merged = mcp_config.model_copy(deep=True)
    
    # Apply config-based agent settings
    if base_config:
        # Override agent_config fields from definition
        merged.agent_config = base_config.model_copy(deep=True)
    
    # Apply runtime overrides (API parameters, user selections)
    if mcp_config.agent_config:
        # Override only explicitly set fields
        if mcp_config.agent_config.llm_profile:
            merged.agent_config.llm_profile = mcp_config.agent_config.llm_profile
        if mcp_config.agent_config.max_steps:
            merged.agent_config.max_steps = mcp_config.agent_config.max_steps
        # Tools config: runtime takes full precedence if specified
        if mcp_config.agent_config.tools:
            merged.agent_config.tools = mcp_config.agent_config.tools.model_copy(deep=True)
    
    return merged


def _load_system_prompt(
    definition: ConfigBasedAgentDefinition,
    agent_name: str
) -> Optional[str]:
    """
    Load system prompt from template file or inline definition.
    
    Args:
        definition: ConfigBasedAgentDefinition
        agent_name: Name of the agent (for logging)
    
    Returns:
        System prompt string or None if not specified
    """
    agent_config = definition.agent_config
    
    # Check for inline system_prompt
    if agent_config.system_prompt:
        logger.debug(f"Using inline system_prompt for agent '{agent_name}'")
        return agent_config.system_prompt
    
    # Check for system_template file
    if agent_config.system_template:
        template_path = Path(agent_config.system_template)
        
        if not template_path.is_absolute():
            # Resolve relative to project root
            template_path = Path.cwd() / template_path
        
        if not template_path.exists():
            logger.warning(
                f"System template not found for agent '{agent_name}': "
                f"{template_path}"
            )
            return None
        
        try:
            # Load template content (YAML file with system_prompt key)
            import yaml
            with open(template_path, 'r', encoding='utf-8') as f:
                template_data = yaml.safe_load(f)
            
            if isinstance(template_data, dict) and 'system_prompt' in template_data:
                logger.debug(
                    f"Loaded system_prompt from template for agent '{agent_name}': "
                    f"{template_path}"
                )
                return template_data['system_prompt']
            else:
                logger.warning(
                    f"Template file missing 'system_prompt' key: {template_path}"
                )
                return None
        
        except Exception as e:
            logger.error(
                f"Failed to load system template for agent '{agent_name}': {e}",
                exc_info=True
            )
            return None
    
    # No prompt specified
    return None
