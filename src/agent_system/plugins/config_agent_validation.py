"""
Config Agent Validation Module

Comprehensive validation and error reporting for configuration-based agents.
Part of Epic 0043: Configuration-Based Agents.

Provides detailed, actionable error messages for common configuration mistakes.
"""

from typing import List, Dict, Optional
import logging
from pathlib import Path

from ..config.models import ConfigBasedAgentDefinition


logger = logging.getLogger(__name__)


class ConfigAgentValidationError(Exception):
    """Exception raised when config agent validation fails"""
    
    def __init__(self, agent_name: str, errors: List[str]):
        self.agent_name = agent_name
        self.errors = errors
        message = f"Validation failed for config agent '{agent_name}':\n"
        for i, error in enumerate(errors, 1):
            message += f"  {i}. {error}\n"
        super().__init__(message)


def validate_config_agent(
    name: str,
    definition: ConfigBasedAgentDefinition,
    llm_profiles: Optional[List[str]] = None
) -> List[str]:
    """
    Validate a single config agent definition with detailed error messages.
    
    Args:
        name: Agent name (for error messages)
        definition: ConfigBasedAgentDefinition to validate
        llm_profiles: Optional list of valid LLM profile names
    
    Returns:
        List of error messages (empty if valid)
    """
    errors = []
    
    # Basic structure validation
    if not name or not name.strip():
        errors.append("Agent name is empty or whitespace-only")
        return errors  # Can't continue without valid name
    
    if not name.replace('_', '').replace('-', '').isalnum():
        errors.append(
            f"Agent name '{name}' contains invalid characters. "
            f"Use only alphanumeric, underscore, and hyphen."
        )
    
    # Base type validation
    # Note: base_type can be any valid plugin name (e.g., "agent", "basic_agent", 
    # "web_research_agent", etc.). The actual validation happens at runtime when
    # the plugin is loaded. Here we just check for basic sanity.
    if not definition.base_type or not definition.base_type.strip():
        errors.append("base_type is required and cannot be empty")
    elif not definition.base_type.replace('_', '').isalnum():
        errors.append(
            f"Invalid base_type '{definition.base_type}'. "
            f"Use only alphanumeric characters and underscores."
        )
    
    # Agent config validation
    if not definition.agent_config:
        errors.append("Missing agent_config section")
        return errors  # Can't continue without agent_config
    
    agent_cfg = definition.agent_config
    
    # LLM profile validation
    if not agent_cfg.llm_profile:
        errors.append(
            "Missing llm_profile. "
            "Specify an LLM profile from llm.yaml (e.g., 'normal', 'turbo', 'deepseek')"
        )
    elif llm_profiles and agent_cfg.llm_profile not in llm_profiles:
        errors.append(
            f"Unknown llm_profile '{agent_cfg.llm_profile}'. "
            f"Available profiles: {', '.join(llm_profiles)}"
        )
    
    # Max steps validation
    if agent_cfg.max_steps is not None:
        if agent_cfg.max_steps <= 0:
            errors.append(
                f"Invalid max_steps ({agent_cfg.max_steps}). Must be positive integer."
            )
        elif agent_cfg.max_steps > 100:
            errors.append(
                f"max_steps ({agent_cfg.max_steps}) is very high. "
                f"Consider lower value (typical: 5-30) to avoid infinite loops."
            )
    
    # System prompt validation
    has_inline_prompt = bool(agent_cfg.system_prompt)
    has_template = bool(agent_cfg.system_template)
    
    if has_inline_prompt and has_template:
        errors.append(
            "Both system_prompt (inline) and system_template (file) specified. "
            "Use only one. Inline prompt will take precedence."
        )
    
    if not has_inline_prompt and not has_template:
        errors.append(
            "No system prompt specified. "
            "Provide either system_prompt (inline) or system_template (file path)."
        )
    
    # Template file validation
    if has_template:
        template_path = Path(agent_cfg.system_template)
        if not template_path.is_absolute():
            template_path = Path.cwd() / template_path
        
        if not template_path.exists():
            errors.append(
                f"System template file not found: {agent_cfg.system_template}\n"
                f"  Resolved path: {template_path}\n"
                f"  Make sure the file exists relative to project root."
            )
        elif not template_path.is_file():
            errors.append(
                f"System template path is not a file: {agent_cfg.system_template}"
            )
    
    # Tools configuration validation
    if agent_cfg.tools:
        tools = agent_cfg.tools
        
        if not isinstance(tools.allowed, list):
            errors.append("tools.allowed must be a list of tool patterns")
        else:
            # Check for common mistakes
            if "*" in tools.allowed and len(tools.allowed) > 1:
                errors.append(
                    "tools.allowed contains '*' (all tools) and specific patterns. "
                    "The '*' makes other patterns redundant."
                )
            
            # Validate pattern syntax
            for pattern in tools.allowed:
                if not isinstance(pattern, str):
                    errors.append(f"Tool pattern must be string, got {type(pattern)}: {pattern}")
                elif pattern.count('*') > 1:
                    errors.append(
                        f"Tool pattern '{pattern}' has multiple wildcards. "
                        f"Use format 'plugin/*' or 'plugin/tool_name'."
                    )
        
        if not isinstance(tools.blocked, list):
            errors.append("tools.blocked must be a list of tool patterns")
    
    # Context management validation
    if agent_cfg.context_management:
        ctx = agent_cfg.context_management
        
        if ctx.enabled:
            valid_strategies = ["TRUNCATE_OLDEST", "SUMMARIZE_OLDEST", "SLIDING_WINDOW", "SMART_COMPRESSION"]
            if ctx.strategy and ctx.strategy not in valid_strategies:
                errors.append(
                    f"Unknown context_management.strategy '{ctx.strategy}'. "
                    f"Valid options: {', '.join(valid_strategies)}"
                )
            
            if ctx.preserve_recent_messages is not None:
                if ctx.preserve_recent_messages < 0:
                    errors.append("context_management.preserve_recent_messages must be non-negative")
                elif ctx.preserve_recent_messages > 50:
                    errors.append(
                        f"context_management.preserve_recent_messages ({ctx.preserve_recent_messages}) is very high. "
                        f"Consider lower value (typical: 5-20)."
                    )
    
    # Metadata validation
    if definition.metadata:
        if "version" in definition.metadata:
            version = definition.metadata["version"]
            # Basic semantic version check
            if not isinstance(version, str) or not version.replace('.', '').replace('-', '').isalnum():
                errors.append(
                    f"Invalid version format '{version}'. "
                    f"Use semantic versioning (e.g., '1.0.0')."
                )
        
        if "tags" in definition.metadata:
            if not isinstance(definition.metadata["tags"], list):
                errors.append("metadata.tags must be a list of strings")
    
    return errors


def validate_all_config_agents(
    agents_config: Optional[Dict[str, ConfigBasedAgentDefinition]],
    llm_profiles: Optional[List[str]] = None
) -> Dict[str, List[str]]:
    """
    Validate all config agents in the system configuration.
    
    Args:
        agents_config: Dict mapping agent name to ConfigBasedAgentDefinition
        llm_profiles: Optional list of valid LLM profile names
    
    Returns:
        Dict mapping agent_name -> list of error messages
        Empty lists indicate valid agents
    """
    validation_results = {}
    
    if not agents_config:
        logger.info("No config agents to validate")
        return validation_results
    
    for name, definition in agents_config.items():
        errors = validate_config_agent(name, definition, llm_profiles)
        validation_results[name] = errors
        
        if errors:
            logger.error(
                f"Config agent '{name}' has {len(errors)} validation error(s):\n" +
                "\n".join(f"  - {err}" for err in errors)
            )
        else:
            logger.debug(f"Config agent '{name}' passed validation")
    
    return validation_results


def get_validation_summary(validation_results: Dict[str, List[str]]) -> str:
    """
    Get a human-readable summary of validation results.
    
    Args:
        validation_results: Results from validate_all_config_agents()
    
    Returns:
        Formatted summary string
    """
    total = len(validation_results)
    failed = sum(1 for errors in validation_results.values() if errors)
    passed = total - failed
    
    summary = "Config Agent Validation Summary:\n"
    summary += f"  Total agents: {total}\n"
    summary += f"  Passed: {passed}\n"
    summary += f"  Failed: {failed}\n"
    
    if failed > 0:
        summary += "\nFailed agents:\n"
        for name, errors in validation_results.items():
            if errors:
                summary += f"  - {name}: {len(errors)} error(s)\n"
                for error in errors:
                    summary += f"      {error}\n"
    
    return summary
