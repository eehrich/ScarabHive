"""
Prompt Rendering Strategies

Implements strategy pattern for flexible prompt rendering with multiple sources.
Extracted from servers/agent/server.py to reduce complexity (Issue #10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, TYPE_CHECKING
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
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
    # Plugin types with an enabled instance, and the enabled external MCP
    # servers -- the installation, not this agent's allowlist (that is tools).
    plugins: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)


def build_context_values(context: PromptContext) -> Dict[str, Any]:
    """Jinja context shared by prompt strategies and skill bodies.

    Module-level (not a method) because skills are rendered by PromptRenderer,
    which is not a PromptStrategy — and this never needed ``self``.

    A prompt can branch on what is there:
      * ``tools`` -- what THIS agent may call: server names, tool names and
        ``server.tool`` for external MCP tools. ``has_tool(pattern)`` asks it
        with fnmatch patterns, because tool names carry the instance name
        (``has_tool('*_manage_sub_agent')``, ``has_tool('github.*')``).
      * ``plugins`` -- plugin types installed and switched on, whether this
        agent may use them or not (``'writer_pipeline_v4' in plugins``).
      * ``mcp_servers`` -- external MCP servers switched on.
    None of them may change between two steps: the system prompt is the
    cached prefix. So they come from configuration, never from a live state
    such as a connection.
    """
    tools = context.available_tools

    def has_tool(pattern: str) -> bool:
        return any(fnmatchcase(name, pattern) for name in tools)

    context_vals = {
        "tools": tools,
        "has_tool": has_tool,
        "plugins": context.plugins,
        "mcp_servers": context.mcp_servers,
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
        return build_context_values(context)


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
    """Render system prompt from a markdown template file (whole file = prompt,
    Jinja2-rendered)."""

    def can_handle(self, context: PromptContext) -> bool:
        """Check if agent has system_template path configured."""
        system_template_path = getattr(context.agent_config, 'system_template', None)
        return bool(system_template_path)

    def render(self, context: PromptContext) -> tuple[str, Optional[str]]:
        """Render the markdown template file with context."""
        system_template_path = context.agent_config.system_template
        context_vals = self._get_context_values(context)

        logger.debug(
            "Agent %s rendering system_template from path: %s",
            context.agent_name, system_template_path
        )

        # The datetime values are already in context_vals, beneath the
        # template_vars; merging them again here would override a pinned date.
        rendered = render_prompts(system_template_path, context_vals, auto_datetime=False)
        system_prompt = rendered.get("system_prompt", "")
        if not system_prompt.strip():
            # A configured template that renders empty (empty file, or a body
            # fully gated behind a false {% if %}) would otherwise fall through
            # to the generic default prompt silently — surface it.
            logger.warning(
                "Agent %s: system_template '%s' rendered to an empty prompt; "
                "the agent will fall back to the default prompt.",
                context.agent_name, system_template_path)
        return system_prompt, None


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


#: Missing-skill names already reported, so the error is logged once per agent
#: instead of on every single render (prompts render on every LLM call).
_reported_missing_skills: set[tuple[str, str]] = set()


class PromptRenderer:
    """
    Orchestrates prompt rendering using strategy pattern.

    Order of precedence:
      1. Subclass hook (get_custom_system_prompt)
      2. In-memory raw prompt (agent_config.system_prompt)
      3. Template file (agent_config.system_template)
      4. Default fallback

    Skills configured on the agent are appended to whichever prompt won — the
    single place where all four strategies meet.
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
        Render prompts using first applicable strategy, then append skills.

        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        system_prompt, tools_prompt = "You are an assistant agent.", None
        for strategy in self.strategies:
            if strategy.can_handle(context):
                result = strategy.render(context)
                # If strategy returns empty string, try next one
                if result[0]:
                    system_prompt, tools_prompt = result
                    break

        system_prompt = self._append_skills(system_prompt, context)
        return system_prompt, tools_prompt

    def _append_skills(self, system_prompt: str, context: PromptContext) -> str:
        """Append ``always`` skill bodies plus an index of ``on_demand`` ones.

        Appended at the END of the system prompt, in configured order: the
        system prompt is the stable cache prefix, so a deterministic order keeps
        it byte-identical between calls.

        Skill bodies are taken verbatim (Agent Skills standard — see
        ``skills/registry.py``). ``on_demand`` skills contribute only their
        one-line description: without that index the agent would never know
        they exist and would never fetch them.
        """
        skills_cfg = getattr(context.agent_config, "skills", None)
        wanted = list(getattr(skills_cfg, "always", []) or []) if skills_cfg else []
        on_demand = list(getattr(skills_cfg, "on_demand", []) or []) if skills_cfg else []
        if not wanted and not on_demand:
            return system_prompt

        from agent_system.skills.registry import configured_skill_registry

        # Roots come from config (skills.skill_dirs), falling back to the
        # default; this runs on every LLM call, and only a change of the roots
        # touches the filesystem.
        registry = configured_skill_registry(context.system_config)
        parts = [system_prompt.rstrip()] if system_prompt.strip() else []

        for name in wanted:
            skill = registry.get(name)
            if skill is None:
                key = (context.agent_name, name)
                if key not in _reported_missing_skills:
                    _reported_missing_skills.add(key)
                    logger.error(
                        "Agent %s: skill '%s' not found — it will be MISSING from the "
                        "prompt. Known skills: %s (scanned: %s)",
                        context.agent_name, name, registry.names() or "none",
                        registry.scanned_dirs or "no dirs",
                    )
                continue
            try:
                # Verbatim, never templated: the body is instructions, not a
                # template. Rendering it would silently blank any literal
                # {{ ... }} the author wrote, because unknown variables render
                # empty. Shared prompt fragments belong in the prompt TEMPLATES
                # via {% include %}, not in skills.
                rendered = skill.body().strip()
            except Exception as e:  # noqa: BLE001 - a broken skill must not kill the run
                logger.error(
                    "Agent %s: skill '%s' failed to render (%s) — skipped",
                    context.agent_name, name, e,
                )
                continue
            if rendered:
                parts.append(rendered)
                logger.debug(
                    "Agent %s: appended skill '%s' v%s (%d chars)",
                    context.agent_name, skill.name, skill.version, len(rendered),
                )

        index = self._on_demand_index(on_demand, registry, context)
        if index:
            parts.append(index)

        return "\n\n".join(parts)

    def _on_demand_index(self, names, registry, context: PromptContext) -> str:
        """One-line-per-skill index telling the agent what it can fetch.

        Only the descriptions go into the prompt; bodies and bundled files are
        pulled with the ``skills`` plugin's tools when a task needs them.
        """
        lines = []
        for name in names:
            skill = registry.get(name)
            if skill is None:
                key = (context.agent_name, name)
                if key not in _reported_missing_skills:
                    _reported_missing_skills.add(key)
                    logger.error(
                        "Agent %s: on_demand skill '%s' not found — it will be MISSING "
                        "from the index. Known skills: %s (scanned: %s)",
                        context.agent_name, name, registry.names() or "none",
                        registry.scanned_dirs or "no dirs",
                    )
                continue
            desc = " ".join((skill.description or "").split()) or "(no description)"
            lines.append(f"- `{skill.name}`: {desc}")

        if not lines:
            return ""
        # The tools carry their instance's name: name the ones this agent has.
        # The type is compared after inheritance (`kb2: {type: kb}`, `kb: {type: skills}`).
        from agent_system.config.settings import get_tool_server_config

        tools = set(context.available_tools)
        servers = getattr(context.system_config.plugins, "servers", None) or {}
        instance = next((name for name in servers if f"{name}_read" in tools
                         and getattr(get_tool_server_config(name, context.system_config), "type", None) == "skills"),
                        None)
        if instance is None:
            key = (context.agent_name, "")
            if key not in _reported_missing_skills:
                _reported_missing_skills.add(key)
                logger.error("Agent %s has on_demand skills but no skills tool to read them "
                             "(allow e.g. skills/*) — the index is left out.", context.agent_name)
            return ""
        return (
            "## Available skills\n\n"
            "Reference material you can load when a task needs it. Read a skill with "
            f"`{instance}_read`; list its bundled files with `{instance}_list`.\n\n"
            + "\n".join(lines)
        )
