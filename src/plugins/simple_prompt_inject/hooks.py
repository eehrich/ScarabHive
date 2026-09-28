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
from agent_system.llm.message_roles import SYSTEM, is_input, role_of
from agent_system.llm.models import ChatMessage
from agent_system.utils.prompt_renderer import strip_prompt_comments

logger = logging.getLogger(__name__)

INJECTED_BY = "simple_prompt_inject"

# Project config directory (config/)
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"


class SimplePromptInjectPlugin(SchemaBasedPluginHook):
    """Hook plugin that injects a configurable prompt into conversations.

    On each pre_llm_call, injects the configured ``prompt_text`` (or content
    loaded from ``prompt_file``) as a message (default role: developer) at the
    configured position, marked with ``injected_by``. What repeated calls do
    depends on the position: ``before_last_user`` moves the text with the turn
    and therefore replaces the previous copy, ``end`` appends it once and
    writes again only when the rendered text changed, ``after_system`` keeps
    it right behind the system prompt and rewrites it there only when the
    rendered text changed.

    Both ``prompt_text`` and ``prompt_file`` content support Jinja2 template
    syntax rendered with the agent's ``template_vars``.

    Configuration (via plugins.yaml):
        prompt_text: Text to inject (empty = no-op)
        prompt_file: Path to .md file (relative to config/ or absolute)
        injection_position: 'before_last_user', 'end' or 'after_system'
        role: 'system', 'developer' or 'user'. A 'developer' message is what
            the RUN tells the model, and it keeps the position configured
            here. A 'system' message is part of the instructions and always
            stands behind the system prompt ('after_system').
    """

    def __init__(self, plugin_dir: Path | str, server_config: Any = None) -> None:
        super().__init__(plugin_dir)

        # Merge schema defaults with runtime config from plugins.yaml
        config = self.get_config()
        if server_config and hasattr(server_config, "config") and server_config.config:
            config.update(server_config.config)

        self.injection_position: str = str(config.get("injection_position", "before_last_user"))
        self.role: str = str(config.get("role", "developer"))
        if self.role == SYSTEM and self.injection_position != "after_system":
            # A system message belongs to the instructions at the head. Inside
            # the history Anthropic and Gemini hoist it there anyway, where it
            # reads as if it had held from the first turn -- so it is placed
            # there, and the contradicting position is a config mistake.
            logger.error(
                "simple_prompt_inject: role 'system' stands behind the system "
                "prompt; injection_position %r is ignored -- set 'after_system'",
                self.injection_position)
            self.injection_position = "after_system"

        # Resolve prompt source: prompt_file takes precedence over prompt_text
        prompt_file = str(config.get("prompt_file", "")).strip()
        if prompt_file:
            self.prompt_template = self._load_prompt_file(prompt_file)
        else:
            self.prompt_template = str(config.get("prompt_text", ""))

        # keep_trailing_newline: a text without template syntax renders to
        # itself. Jinja drops one trailing newline by default, and a text
        # rendered only once the session holds a variable -- the fast path
        # below skips Jinja while there is none -- changed at the call where
        # the first variable appeared: `prompt_text: |` ends in a newline,
        # and the head behind the system prompt moved for nothing.
        self._jinja_env = Environment(loader=BaseLoader(), autoescape=False,
                                      keep_trailing_newline=True)

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

        content = strip_prompt_comments(path.read_text(encoding="utf-8"), str(path))
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
        previous = next((m for m in reversed(messages)
                         if m.injected_by == INJECTED_BY), None)

        if self.injection_position == "after_system":
            # Part of the instructions: behind the system prompt, and it stays
            # there. Rewritten only when the rendered text changed -- a head
            # that changes invalidates the cached prefix of the whole history.
            rest = [m for m in messages if m.injected_by != INJECTED_BY]
            idx = self._after_system_prompt_index(rest)
            if (len(rest) == len(messages) - 1 and messages[idx].injected_by == INJECTED_BY
                    and messages[idx].role == self.role and messages[idx].content == rendered):
                return HookResult(success=True, modified=False, context=context)
            messages = rest
            messages.insert(idx, new_msg)
        elif self.injection_position == "end":
            # Appended and then left alone: an unchanged text written again
            # would move the end of the prompt for nothing, and everything
            # before it has to be paid for a second time.
            if previous is not None and previous.content == rendered:
                return HookResult(success=True, modified=False, context=context)
            messages.append(new_msg)
        else:
            # "before_last_user": here the POINT is the distance to the end --
            # a reminder the model should read just before it answers. That one
            # has to move with the turn, so the previous copy goes. It costs
            # the last message's cache, not the history's: everything in front
            # of the insertion point stays byte-identical.
            messages = [m for m in messages if m.injected_by != INJECTED_BY]
            idx = self._find_last_user_index(messages)
            if idx is not None:
                messages.insert(idx, new_msg)
            else:
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
    def _after_system_prompt_index(messages: list[ChatMessage]) -> int:
        """Index right behind the leading system message(s)."""
        idx = 0
        while idx < len(messages) and role_of(messages[idx]) == SYSTEM:
            idx += 1
        return idx

    @staticmethod
    def _find_last_user_index(messages: list[ChatMessage]) -> int | None:
        """Index of the last thing the model is being asked to answer, or None.

        `is_input`, not `role == "user"` and not `opens_a_turn`: this anchor
        exists for its DISTANCE TO THE END, so it has to find whatever stands
        last -- a woken run's `developer` wake as well as the marked user
        messages the run appends inside a turn (a delivered direct message, a
        continuation nudge, a debate post). Asking for the role alone missed
        the wake; asking for the head of the turn missed all the marked ones
        and buried the reminder in front of the whole answered exchange.
        """
        for i in range(len(messages) - 1, -1, -1):
            if is_input(messages[i]):
                return i
        return None
