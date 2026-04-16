"""Simple Prompt Inject Plugin - Schema-based hooks plugin.

Injects configurable prompt text into conversations before LLM calls.
Useful for adding persistent instructions, reminders, or context
without modifying agent system prompts.

Supports Jinja2 template rendering with agent template_vars.
Prompt source can be inline ``prompt_text`` or an external ``prompt_file``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, BaseLoader, TemplateSyntaxError, UndefinedError

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)

INJECTED_BY = "simple_prompt_inject"

# Project config directory (config/)
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"


class SimplePromptInjectPlugin(SchemaBasedPluginHook):
    """Hook plugin that injects a configurable prompt into conversations.

    On each pre_llm_call, injects the configured ``prompt_text`` (or content
    loaded from ``prompt_file``) as a message (default role: system) at the
    configured position. Uses the ``injected_by`` field on ChatMessage so that
    repeated calls replace the previous injection rather than accumulating
    duplicates.

    Both ``prompt_text`` and ``prompt_file`` content support Jinja2 template
    syntax rendered with the agent's ``template_vars``.

    Configuration (via plugins.yaml):
        prompt_text: Text to inject (empty = no-op)
        prompt_file: Path to .md file (relative to config/ or absolute)
        injection_position: 'before_last_user' or 'end'
        role: 'system' or 'user'
    """

    def __init__(self, plugin_dir: Path | str, mcp_config: Any = None) -> None:
        super().__init__(plugin_dir)

        # Merge schema defaults with runtime config from plugins.yaml
        config = self.get_config()
        if mcp_config and hasattr(mcp_config, "config") and mcp_config.config:
            config.update(mcp_config.config)

        self.injection_position: str = str(config.get("injection_position", "before_last_user"))
        self.role: str = str(config.get("role", "system"))

        # Resolve prompt source: prompt_file takes precedence over prompt_text
        prompt_file = str(config.get("prompt_file", "")).strip()
        if prompt_file:
            self.prompt_template = self._load_prompt_file(prompt_file)
        else:
            self.prompt_template = str(config.get("prompt_text", ""))

        self._jinja_env = Environment(loader=BaseLoader(), autoescape=False)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_prompt_file(prompt_file: str) -> str:
        """Load prompt content from a file path.

        Resolves relative paths against the project config/ directory.
        """
        path = Path(prompt_file)
        if not path.is_absolute():
            path = _CONFIG_DIR / path
        path = path.resolve()

        if not path.exists():
            raise FileNotFoundError(
                f"simple_prompt_inject: prompt_file not found: {path}"
            )
        if not path.is_file():
            raise ValueError(
                f"simple_prompt_inject: prompt_file is not a file: {path}"
            )

        content = path.read_text(encoding="utf-8")
        logger.info("Loaded prompt_file: %s (%d chars)", path, len(content))
        return content

    # ------------------------------------------------------------------
    # Hook handler – name must match schema.yaml hook name exactly
    # ------------------------------------------------------------------

    async def inject_prompt(self, context: HookContext) -> HookResult:
        """Inject the configured prompt text into the message list.

        The prompt template is rendered with Jinja2 using the agent's
        template_vars (session-scoped, then agent_config fallback).
        """
        if not self.prompt_template:
            return HookResult(success=True, modified=False, context=context)

        if context.messages is None:
            return HookResult(success=True, modified=False, context=context)

        # Resolve template variables from agent context
        template_vars = self._get_template_vars(context)

        # Render prompt through Jinja2
        rendered = self._render_template(self.prompt_template, template_vars)
        if not rendered:
            return HookResult(success=True, modified=False, context=context)

        # Build replacement message
        new_msg = ChatMessage(
            role=self.role,
            content=rendered,
            injected_by=INJECTED_BY,
        )

        # Work on a shallow copy so we don't mutate the original list
        messages = list(context.messages)

        # Remove any previously injected message from this plugin
        messages = [m for m in messages if m.injected_by != INJECTED_BY]

        # Insert at correct position
        if self.role == "system":
            # System messages must always go at the beginning, after existing
            # system messages.  Placing them mid-conversation causes LLM errors.
            idx = self._find_after_system_index(messages)
            messages.insert(idx, new_msg)
        elif self.injection_position == "before_last_user":
            idx = self._find_last_user_index(messages)
            if idx is not None:
                messages.insert(idx, new_msg)
            else:
                messages.append(new_msg)
        else:
            # "end" – append
            messages.append(new_msg)

        context.messages = messages
        return HookResult(success=True, modified=True, context=context)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_template_vars(context: HookContext) -> dict[str, Any]:
        """Extract template_vars from agent context.

        Tries session-scoped vars first (they may be updated at runtime),
        then falls back to static agent_config.template_vars.
        """
        agent = context.agent
        if agent is None:
            return {}

        # Session-scoped template vars (preferred – may be updated at runtime)
        if (
            context.session_id
            and hasattr(agent, "_session_tracker")
            and agent._session_tracker
        ):
            session_vars = agent._session_tracker.get_session_template_vars(
                context.session_id
            )
            if session_vars:
                return dict(session_vars)

        # Fallback: static agent_config template_vars
        if hasattr(agent, "agent_config") and agent.agent_config:
            cfg_vars = agent.agent_config.template_vars
            if cfg_vars:
                return dict(cfg_vars)

        return {}

    def _render_template(self, template_str: str, template_vars: dict[str, Any]) -> str:
        """Render a Jinja2 template string with the given variables.

        Returns the rendered string. On error, logs a warning and returns
        the unmodified template string (graceful fallback).
        """
        if not template_vars:
            # No variables → skip Jinja2 entirely (fast path, avoids
            # accidental interpretation of literal {{ }} in prompt text)
            return template_str

        try:
            template = self._jinja_env.from_string(template_str)
            return template.render(**template_vars)
        except (TemplateSyntaxError, UndefinedError) as exc:
            logger.warning(
                "simple_prompt_inject: Jinja2 render error: %s – using raw template",
                exc,
            )
            return template_str

    @staticmethod
    def _find_last_user_index(messages: list[ChatMessage]) -> int | None:
        """Return index of the last user message, or None."""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user":
                return i
        return None

    @staticmethod
    def _find_after_system_index(messages: list[ChatMessage]) -> int:
        """Return the index right after the last leading system message."""
        for i, msg in enumerate(messages):
            if msg.role != "system":
                return i
        return len(messages)
