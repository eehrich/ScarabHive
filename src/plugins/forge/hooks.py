"""The webhook's inbox handed to its session (docs/konzept.md §7, W4).

Before an LLM call, what waits for the session goes in as one message; once
the run's conversation is saved, it counts as delivered. A run that dies
before its save leaves it for the session's next run instead of losing it --
the pattern of debate_forum's direct messages.
"""
from __future__ import annotations

import logging

from agent_system.hooks import HookContext, HookResult, PluginHook
from agent_system.llm.models import ChatMessage

from .events import Store

logger = logging.getLogger(__name__)

INJECTED_BY = "forge"


class ForgeHooks(PluginHook):
    def __init__(self, name: str, store: Store) -> None:
        super().__init__(name=name, config={})
        self.store = store
        self._handed: dict[str, list[int]] = {}      # request id -> inbox ids handed in it

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        unchanged = HookResult(success=True, modified=False, context=context)
        if not context.session_id or context.messages is None:
            return unchanged
        try:
            handed = self._handed.get(context.request_id or "", [])
            waiting = [(i, text) for i, text in self.store.waiting(context.session_id) if i not in handed]
        except Exception:  # noqa: BLE001 - a broken store must not stop the agent
            logger.exception("forge: reading the webhook inbox failed")
            return unchanged
        if not waiting:
            return unchanged
        lines = "\n".join(f"- {text}" for _, text in waiting)
        context.messages.append(ChatMessage(role="user", injected_by=INJECTED_BY,
                                            content=f"[forge] News from the platform's webhook:\n{lines}"))
        # Still undelivered in the store until the run is saved; meanwhile the
        # next steps of this run must not read them again.
        self._handed[context.request_id or ""] = handed + [i for i, _ in waiting]
        logger.info("forge: handed %d webhook event(s) to session %s", len(waiting), context.session_id)
        return HookResult(success=True, modified=True, context=context)

    async def on_session_end(self, context: HookContext) -> HookResult:
        handed = self._handed.pop(context.request_id or "", None)
        if handed and (context.metadata or {}).get("persisted"):
            try:
                self.store.delivered(handed)
            except Exception:  # noqa: BLE001
                logger.exception("forge: marking webhook events delivered failed")
        elif handed:
            logger.info("forge: session %s was not saved; its %d webhook event(s) wait for its next run",
                        context.session_id, len(handed))
        return HookResult(success=True, modified=False, context=context)
