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
        
        # Override merged_config.agent_config.system_prompt with the fully merged prompt
        # This ensures the Agent class uses the complete multi-section prompt
        if system_prompt:
            merged_config.agent_config.system_prompt = system_prompt
            # Clear system_template to prevent double-rendering
            merged_config.agent_config.system_template = None
        
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
        
        # Set visibility flags based on metadata.visibility
        # This controls where the agent appears (UI, tool discovery, both, or neither)
        # Default: "private" - agents must explicitly opt-in to visibility (secure by default)
        visibility = "private"  # Default: not visible anywhere
        if definition.metadata and hasattr(definition.metadata, 'visibility'):
            visibility = definition.metadata.visibility
        
        # Map visibility to flags
        agent._mcp_public = visibility in ["ui", "both"]
        agent._mcp_tool_visible = visibility in ["tool", "both"]
        agent._visibility_set_explicitly = True  # Mark that visibility was explicitly configured
        
        logger.info(
            f"Config agent '{name}' created as '{agent_name}' (type: {definition.base_type}): "
            f"LLM={merged_config.agent_config.llm_profile}, "
            f"steps={merged_config.agent_config.max_steps}, "
            f"tools={len(merged_config.agent_config.tools.allowed) if merged_config.agent_config.tools else 0} allowed, "
            f"visibility={visibility} (ui={agent._mcp_public}, tool={agent._mcp_tool_visible})"
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
    Load and merge system prompt sections from base template and agent template.
    
    Multi-level prompt merging strategy:
    1. Load base template sections (from default_config.system_template if exists)
    2. Load agent template sections (from agent_config.system_template)
    3. Override with inline system_prompt (agent_config.system_prompt) - replaces only system_prompt section
    
    All YAML keys in template files are treated as sections and merged.
    
    Args:
        definition: ConfigBasedAgentDefinition
        agent_name: Name of the agent (for logging)
    
    Returns:
        Merged system prompt string or None if not specified
    """
    agent_config = definition.agent_config
    
    # Step 1: Load base template sections (from plugins.yaml default_config)
    base_sections = _load_prompt_sections("config/prompts/system_prompt.yaml", "base template")
    
    # Step 2: Load agent-specific template sections (merge with base)
    merged_sections = base_sections.copy()
    
    if agent_config.system_template:
        agent_sections = _load_prompt_sections(agent_config.system_template, f"agent '{agent_name}'")
        # Merge: agent sections override base sections
        merged_sections.update(agent_sections)
        logger.debug(
            f"Merged {len(agent_sections)} sections from agent template for '{agent_name}': "
            f"{list(agent_sections.keys())}"
        )
    
    # Step 3: Inline system_prompt overrides only the 'system_prompt' section
    if agent_config.system_prompt:
        merged_sections['system_prompt'] = agent_config.system_prompt
        logger.debug(f"Inline system_prompt overrides 'system_prompt' section for agent '{agent_name}'")
    
    # Render final prompt: concatenate all sections in defined order
    if not merged_sections:
        logger.warning(f"No prompt sections loaded for agent '{agent_name}'")
        return None
    
    # Define section order (sections not in this list come after, alphabetically)
    section_order = [
        'system_prompt',
        'tools_prompt',
        'general_instructions_prompt',
    ]
    
    # Sort sections: known sections first (in defined order), then unknown ones (alphabetically)
    def sort_key(item):
        section_name, _ = item
        try:
            return (0, section_order.index(section_name))
        except ValueError:
            return (1, section_name)  # Unknown sections come last, sorted alphabetically
    
    sorted_sections = sorted(merged_sections.items(), key=sort_key)
    
    # Concatenate sections with separators
    final_prompt = "\n\n".join(
        f"# {section_name}\n{content}" if section_name != "system_prompt" else content
        for section_name, content in sorted_sections
    )
    
    logger.debug(
        f"Final prompt for '{agent_name}' assembled from {len(merged_sections)} sections: "
        f"{[name for name, _ in sorted_sections]}"
    )
    
    return final_prompt


def _load_prompt_sections(template_path: str | Path, context: str) -> dict[str, str]:
    """
    Load all sections from a prompt template YAML file.
    
    Args:
        template_path: Path to YAML template file
        context: Description for logging (e.g., "agent 'financial_analyst'")
    
    Returns:
        Dictionary mapping section_name -> section_content
        Returns empty dict if file doesn't exist or has errors
    """
    template_path = Path(template_path)
    
    if not template_path.is_absolute():
        template_path = Path.cwd() / template_path
    
    if not template_path.exists():
        logger.debug(f"Template not found for {context}: {template_path}")
        return {}
    
    try:
        import yaml
        with open(template_path, 'r', encoding='utf-8') as f:
            template_data = yaml.safe_load(f)
        
        if not isinstance(template_data, dict):
            logger.warning(f"Template for {context} is not a dict: {template_path}")
            return {}
        
        # Filter out non-string values and comments
        sections = {
            key: value
            for key, value in template_data.items()
            if isinstance(value, str) and not key.startswith('_')
        }
        
        logger.debug(
            f"Loaded {len(sections)} sections from {context} template: "
            f"{list(sections.keys())}"
        )
        
        return sections
    
    except Exception as e:
        logger.error(
            f"Failed to load template for {context}: {e}",
            exc_info=True
        )
        return {}
