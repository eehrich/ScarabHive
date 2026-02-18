"""Simple Prompt Inject Plugin - Schema-based hooks plugin.

Injects configurable prompt text into conversations before LLM calls.
Useful for adding persistent instructions, reminders, or context
without modifying agent system prompts.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)

INJECTED_BY = "simple_prompt_inject"


class SimplePromptInjectPlugin(SchemaBasedPluginHook):
    """Hook plugin that injects a configurable prompt into conversations.

    On each pre_llm_call, injects the configured ``prompt_text`` as a
    message (default role: system) at the configured position. Uses the
    ``injected_by`` field on ChatMessage so that repeated calls replace
    the previous injection rather than accumulating duplicates.

    Configuration (via plugins.yaml):
        prompt_text: Text to inject (empty = no-op)
        injection_position: 'before_last_user' or 'end'
        role: 'system' or 'user'
    """

    def __init__(self, plugin_dir: Path | str, mcp_config: Any = None) -> None:
        super().__init__(plugin_dir)

        # Merge schema defaults with runtime config from plugins.yaml
        config = self.get_config()
        if mcp_config and hasattr(mcp_config, "config") and mcp_config.config:
            config.update(mcp_config.config)

        self.prompt_text: str = str(config.get("prompt_text", ""))
        self.injection_position: str = str(config.get("injection_position", "before_last_user"))
        self.role: str = str(config.get("role", "system"))

    # ------------------------------------------------------------------
    # Hook handler – name must match schema.yaml hook name exactly
    # ------------------------------------------------------------------

    async def inject_prompt(self, context: HookContext) -> HookResult:
        """Inject the configured prompt text into the message list."""
        if not self.prompt_text:
            return HookResult(success=True, modified=False, context=context)

        if context.messages is None:
            return HookResult(success=True, modified=False, context=context)

        # Build replacement message
        new_msg = ChatMessage(
            role=self.role,
            content=self.prompt_text,
            injected_by=INJECTED_BY,
        )

        # Work on a shallow copy so we don't mutate the original list
        messages = list(context.messages)

        # Remove any previously injected message from this plugin
        messages = [m for m in messages if m.injected_by != INJECTED_BY]

        # Insert at configured position
        if self.injection_position == "before_last_user":
            idx = self._find_last_user_index(messages)
            if idx is not None:
                messages.insert(idx, new_msg)
            else:
                # No user message found – just append
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
    def _find_last_user_index(messages: list[ChatMessage]) -> int | None:
        """Return index of the last user message, or None."""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user":
                return i
        return None
