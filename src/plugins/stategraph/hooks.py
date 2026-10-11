"""What the LLM calls inside a ``call`` activity cost.

An agent activity counts its calls from its run's events (backend._Spend). A call activity runs Python -- the
writer's pipeline, a tool that asks a model -- whose LLM calls reach no run's events: they are counted here, on the
one path every call takes, post_llm_response, into the meter of the call activity they run under
(backend.CALL_SPEND). Every other call -- a chat, a job -- finds no meter: one context var read, nothing else.
"""
from __future__ import annotations

import json
import logging

from agent_system.hooks import HookContext, HookResult, PluginHook

from .engine.backend import CALL_SPEND

logger = logging.getLogger(__name__)


class StateGraphHooks(PluginHook):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, config={})

    async def on_post_llm_response(self, context: HookContext) -> HookResult:
        meter = CALL_SPEND.get()
        # No usage, no spend: a retry or a failed call reports through the same hook without one, and the call
        # that later succeeds reports again (the tracker's rule, llm/hook_notify.notify_response).
        if meter is None or not isinstance(context.llm_usage, dict) or not context.llm_usage:
            return HookResult(success=True, modified=False, context=context)
        # Every instance's hook sees the call, each with its own deep copy of the context: what identifies the call
        # is what the copies share -- its time stamp, model, usage and the provider's response id where it gives one.
        response = context.llm_response_data if isinstance(context.llm_response_data, dict) else {}
        call = (context.metadata.get("timestamp_ms"), context.llm_provider, context.llm_model, response.get("id"),
                json.dumps(context.llm_usage, sort_keys=True, default=str))
        if call not in meter.counted:
            meter.counted.append(call)
            try:
                meter.add_call(context.llm_usage, context.llm_model or None,
                               is_batch=(context.llm_provider or "").startswith("batch_"))
            except Exception:  # noqa: BLE001 - a meter must never break the call it counts
                logger.warning("stategraph: a call's LLM spend was not counted", exc_info=True)
        return HookResult(success=True, modified=False, context=context)
