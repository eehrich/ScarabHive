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
from ..mcp.base import MCPRegistry


logger = logging.getLogger(__name__)


def _get_agent_class_for_base_type(base_type: str) -> type:
    """
    Dynamically resolve the agent class based on base_type.
    
    Args:
        base_type: The base_type from config (e.g., "agent", "basic_agent", "web_research_agent")
    
    Returns:
        Agent class to instantiate
    
    Raises:
        ImportError: If the plugin type cannot be imported
    """
    # Map common base types to their module paths
    type_map = {
        "agent": "agent_system.servers.agent.server",
        "basic_agent": "plugins.basic_agent.server",
        "web_research_agent": "plugins.web_research_agent.server",
    }
    
    # Get module path (either from map or assume it's a plugin)
    if base_type in type_map:
        module_path = type_map[base_type]
    else:
        # Assume it's a plugin with standard structure
        module_path = f"plugins.{base_type}.server"
    
    # Extract class name (CamelCase version of base_type)
    # e.g., "basic_agent" -> "BasicAgent"
    class_name = "".join(word.capitalize() for word in base_type.split("_"))
    
    try:
        # Dynamic import
        module = __import__(module_path, fromlist=[class_name])
        agent_class = getattr(module, class_name)
        logger.debug(f"Resolved base_type '{base_type}' to class {agent_class.__name__}")
        return agent_class
    except (ImportError, AttributeError) as e:
        logger.error(f"Failed to import agent class for base_type '{base_type}': {e}")
        raise ImportError(
            f"Cannot import agent class for base_type '{base_type}'. "
            f"Expected module: {module_path}, class: {class_name}"
        ) from e


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
        Factory function with signature: (agent_name, system_config, mcp_config) -> Agent
        Matches plugin factory pattern used in bootstrap
    
    Example:
        >>> definition = config.config_agents["financial_analyst"]
        >>> factory = create_config_based_agent_factory("financial_analyst", definition)
        >>> metadata = {
        ...     "name": "financial_analyst",
        ...     "description": definition.description,
        ...     "source": "config"
        ... }
        >>> factory._plugin_metadata = metadata
        >>> # Later in bootstrap:
        >>> agent = factory(agent_name, system_config, mcp_config)
    """
    
    def config_agent_factory(agent_name: str, system_config: Any, mcp_config: MCPConfig) -> Any:
        """
        Factory function that creates an Agent instance.
        
        This signature matches the plugin pattern: (name, system_config, mcp_config)
        
        Args:
            agent_name: Name for this agent instance
            system_config: AgentSystemConfig with system-wide settings
            mcp_config: MCPConfig for this agent (may contain overrides)
        
        Returns:
            Initialized Agent instance (or specific plugin type based on base_type)
        """
        logger.info(f"Creating config-based agent: {name} (instance name: {agent_name}, type: {definition.base_type})")
        
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
        
        # Create a temporary registry for this agent
        # (bootstrap will override with shared registry)
        temp_registry = MCPRegistry()
        
        # Select the appropriate agent class based on base_type
        agent_class = _get_agent_class_for_base_type(definition.base_type)
        
        # Create the Agent using selected class
        # Constructor: (name, system_config, mcp_config, registry)
        agent = agent_class(
            name=agent_name,
            system_config=system_config,
            mcp_config=merged_config,
            registry=temp_registry
        )
        
        # Override system prompt if defined in config
        if system_prompt:
            agent._system_prompt_override = system_prompt
        
        logger.info(
            f"Config agent '{name}' created as '{agent_name}' (type: {definition.base_type}): "
            f"LLM={merged_config.agent_config.llm_profile}, "
            f"steps={merged_config.agent_config.max_steps}, "
            f"tools={len(merged_config.agent_config.tools.allowed) if merged_config.agent_config.tools else 0} allowed"
        )
        
        return agent
    
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
