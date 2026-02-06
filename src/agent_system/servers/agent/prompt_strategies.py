"""
Prompt Rendering Strategies

Implements strategy pattern for flexible prompt rendering with multiple sources.
Extracted from servers/agent/server.py to reduce complexity (Issue #10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, TYPE_CHECKING
from dataclasses import dataclass
from jinja2 import Template
import logging

from agent_system.utils.prompt_renderer import get_datetime_context, render_prompts

if TYPE_CHECKING:
    from agent_system.config.models import AgentConfig, AgentSystemConfig

logger = logging.getLogger(__name__)


@dataclass
class PromptContext:
    """Context for prompt rendering."""
    agent_name: str
    agent_config: AgentConfig
    system_config: AgentSystemConfig
    available_tools: list[str]
    max_steps: int
    current_step: int  # Current step number (1-indexed, updated per-step)
    agent_instance: Any  # The actual agent instance for hook calls
    session_template_vars: Optional[Dict[str, Any]] = None  # Session-scoped vars (override agent_config)


class PromptStrategy(ABC):
    """Abstract base for prompt rendering strategies."""
    
    @abstractmethod
    def can_handle(self, context: PromptContext) -> bool:
        """Check if this strategy can handle the given context."""
        pass
    
    @abstractmethod
    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """
        Render prompts.
        
        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        pass
    
    def _get_context_values(self, context: PromptContext) -> Dict[str, Any]:
        """Build common context values for template rendering."""
        context_vals = {
            "tools": context.available_tools,
            "max_steps": context.max_steps,
            "current_step": context.current_step
        }
        
        # Add datetime context if enabled
        if (hasattr(context.system_config, 'context') and 
            context.system_config.context.auto_datetime):
            dt_ctx = get_datetime_context(
                context.system_config.context.timezone,
                context.system_config.context.location
            )
            context_vals.update(dt_ctx)
        
        # Add custom template variables from agent config
        # These override built-in variables if there's a conflict
        if context.agent_config.template_vars:
            context_vals.update(context.agent_config.template_vars)
        
        # Add session-scoped template variables (highest priority)
        # These override BOTH built-in AND agent_config template_vars
        # CRITICAL: This ensures session isolation - each session has its own vars
        if context.session_template_vars:
            context_vals.update(context.session_template_vars)
        
        return context_vals


class SubclassHookStrategy(PromptStrategy):
    """Use subclass-provided custom system prompt via hook method."""
    
    def can_handle(self, context: PromptContext) -> bool:
        """Check if agent has custom prompt hook."""
        return hasattr(context.agent_instance, 'get_custom_system_prompt')
    
    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """Call the agent's custom prompt hook."""
        context_vals = self._get_context_values(context)
        
        try:
            custom_prompt = context.agent_instance.get_custom_system_prompt(context_vals)
            if custom_prompt:
                logger.debug(
                    "Agent %s using subclass custom system prompt (len=%d)",
                    context.agent_name, len(custom_prompt)
                )
                return custom_prompt, None
        except Exception as e:  # pragma: no cover
            logger.warning(
                "Custom system prompt hook failed for agent %s: %s",
                context.agent_name, e
            )
        
        # Hook failed, let another strategy handle it
        return "", None


class RawPromptStrategy(PromptStrategy):
    """Use in-memory raw system prompt string."""
    
    def can_handle(self, context: PromptContext) -> bool:
        """Check if agent has raw system_prompt configured."""
        system_prompt_raw = getattr(context.agent_config, 'system_prompt', None)
        return bool(system_prompt_raw)
    
    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """Render raw prompt as Jinja2 template."""
        system_prompt_raw = context.agent_config.system_prompt
        context_vals = self._get_context_values(context)
        
        logger.debug(
            "Agent %s using in-memory system_prompt (length=%s)",
            context.agent_name, len(system_prompt_raw or '')
        )
        
        try:
            rendered_system = Template(system_prompt_raw).render(**context_vals)
            return rendered_system, None
        except Exception as e:
            logger.warning(
                f"Failed to render system prompt template for {context.agent_name}: {e}",
                exc_info=True
            )
            return "You are an assistant agent.", None


class TemplateFileStrategy(PromptStrategy):
    """Render system prompt from template file."""
    
    # Section ordering for merged prompt
    SECTION_ORDER = [
        'system_prompt',
        'tools_prompt',
        'general_instructions_prompt',
    ]
    
    def can_handle(self, context: PromptContext) -> bool:
        """Check if agent has system_template path configured."""
        system_template_path = getattr(context.agent_config, 'system_template', None)
        return bool(system_template_path)
    
    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """Render template file with context."""
        system_template_path = context.agent_config.system_template
        context_vals = self._get_context_values(context)
        
        logger.debug(
            "Agent %s rendering system_template from path: %s",
            context.agent_name, system_template_path
        )
        
        # Render all sections from template
        rendered_sections = render_prompts(
            system_template_path,
            context_vals,
            auto_datetime=(
                context.system_config.context.auto_datetime 
                if hasattr(context.system_config, 'context') else False
            ),
            timezone=(
                context.system_config.context.timezone 
                if hasattr(context.system_config, 'context') else None
            ),
            location=(
                context.system_config.context.location 
                if hasattr(context.system_config, 'context') else None
            )
        )
        
        # Merge sections with priority-based ordering
        merged_prompt = self._merge_sections(rendered_sections)
        return merged_prompt, None
    
    def _merge_sections(self, sections: Dict[str, str]) -> str:
        """
        Merge prompt sections in priority order.
        
        Matches behavior of config_agent_factory._load_system_prompt()
        """
        def sort_key(item):
            section_name, _ = item
            try:
                return (0, self.SECTION_ORDER.index(section_name))
            except ValueError:
                # Unknown sections come last, sorted alphabetically
                return (1, section_name)
        
        sorted_sections = sorted(sections.items(), key=sort_key)
        
        # Concatenate with separators (except system_prompt which has no header)
        merged_prompt = "\n\n".join(
            f"# {section_name}\n{content}" if section_name != "system_prompt" else content
            for section_name, content in sorted_sections
        )
        
        return merged_prompt


class DefaultPromptStrategy(PromptStrategy):
    """Fallback to default system prompt."""
    
    def can_handle(self, context: PromptContext) -> bool:
        """Always can handle (fallback strategy)."""
        return True
    
    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """Return default prompt."""
        logger.debug(
            "Agent %s has no prompts config, using default system prompt",
            context.agent_name
        )
        return "You are an assistant agent.", None


class PromptRenderer:
    """
    Orchestrates prompt rendering using strategy pattern.
    
    Order of precedence:
      1. Subclass hook (get_custom_system_prompt)
      2. In-memory raw prompt (agent_config.system_prompt)
      3. Template file (agent_config.system_template)
      4. Default fallback
    """
    
    def __init__(self):
        """Initialize with ordered list of strategies."""
        self.strategies = [
            SubclassHookStrategy(),
            RawPromptStrategy(),
            TemplateFileStrategy(),
            DefaultPromptStrategy(),
        ]
    
    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """
        Render prompts using first applicable strategy.
        
        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        for strategy in self.strategies:
            if strategy.can_handle(context):
                result = strategy.render(context)
                # If strategy returns empty string, try next one
                if result[0]:
                    return result
        
        # Should never reach here (DefaultPromptStrategy always handles)
        return "You are an assistant agent.", None
