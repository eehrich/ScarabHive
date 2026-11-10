"""What the LLM calls inside a ``call`` activity cost -- and a dynamic workflow's result for its caller.

An agent activity counts its calls from its run's events (backend._Spend). A call activity runs Python -- the
writer's pipeline, a tool that asks a model -- whose LLM calls reach no run's events: they are counted here, on the
one path every call takes, post_llm_response, into the meter of the call activity they run under
(backend.CALL_SPEND). Every other call -- a chat, a job -- finds no meter: one context var read, nothing else.

A dynamic workflow that ended waits for its caller (workflows.WaitingResults, in runs.db): the wake only rings,
it carries no news. pre_llm_call hands it over, as one turn at the end of the caller's history. The hook is off
unless an agent switches it on (hooks.overrides, as the sub-agent manager's context hook): every hook the registry
runs costs a copy of the history, on every call. On, it asks the database on a turn's first call (a woken turn may
run in another process); later calls only see what ended in this process meanwhile.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from agent_system.hooks import HookContext, HookResult, PluginHook

from .engine.backend import CALL_SPEND

logger = logging.getLogger(__name__)

#: The mark of the turns the hook adds (injected_by): a person wrote none of them.
MARKER = "stategraph"
#: How much of a workflow's output the turn carries -- as get_run's answer cuts it.
RESULT_CHARS = 20000


def result_head(row: dict[str, Any]) -> str:
    """A result turn's first words: which run, and which of its ends -- how the hook finds its turn in a history."""
    return (f"Your workflow run {row.get('id')} ({row.get('machine_id')}) ended {row.get('status')}"
            + (f" in {row['final_state']}" if row.get("final_state") else "")
            + (f" at {row['finished_at']}" if row.get("finished_at") else ""))


def holds(messages: Any, head: str) -> bool:
    """Whether ``messages`` carry the hook's turn that starts with ``head``."""
    return any(getattr(m, "injected_by", None) == MARKER and isinstance(m.content, str) and m.content.startswith(head)
               for m in messages or ())


def result_turn(row: dict[str, Any]) -> str:
    """What the caller reads of a workflow run that ended: how, its cost, and its output or error."""
    cost = row.get("cost")
    head = (result_head(row) + (f", cost {float(cost):.4f} USD" if isinstance(cost, (int, float)) else "") + ".")
    body = row.get("output") if row.get("output") is not None else row.get("error")
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1, default=str)
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + f"\n... [{len(text) - RESULT_CHARS} characters more: get_run with full_output]"
    return f"{head}\n\n{'Output' if row.get('output') is not None else 'Error'}:\n{text}"


class StateGraphHooks(PluginHook):
    def __init__(self, name: str, server: Optional[Any] = None) -> None:
        super().__init__(name=name, config={})
        self.server = server

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """A workflow result waiting for this session, as a turn at the end of its history (append-only). As a
        user turn, marked: it is data the workflow's agents produced -- from files or pages they read -- not a note
        of the run with an instruction's weight."""
        server = self.server
        results = getattr(server, "workflow_results", None)
        if results is None or not context.session_id or context.messages is None:
            return HookResult(success=True, modified=False, context=context)
        from agent_system.llm.models import ChatMessage

        added, session = 0, context.session_id
        handed = results.handed(session)
        try:
            run_ids = results.waiting_for(session, look_up=not context.step)
        except Exception:  # noqa: BLE001 - an unreadable store costs the caller a poll, never its call
            logger.warning("stategraph: waiting workflow results of %s not read", session, exc_info=True)
            run_ids = []
        # the handed ones too: a turn appended to an earlier call that never reached its history goes again
        for run_id in dict.fromkeys([*run_ids, *handed]):
            try:
                row = server.run_store.get_run(run_id) or {}
                if not row:
                    continue
                head = result_head(row)  # this end's: a turn of an earlier end in the history is not this one
                if holds(context.messages, head):  # the history this call is made with holds it: delivered
                    results.deliver(run_id, session)
                    continue
                # handed, and no longer waiting: its caller read it with get_run, or it runs again (resumed) and its
                # next end parks it anew
                if run_id not in run_ids and not results.waiting(run_id):
                    results.deliver(run_id, session)
                    continue
                turn = result_turn({**row, "cost": server.run_manager.budget(run_id)[1]})
            except Exception:  # noqa: BLE001 - it stays waiting: a poll for the caller, never a failed call
                logger.warning("stategraph: workflow run %s not handed to its caller", run_id, exc_info=True)
                continue
            context.messages.append(ChatMessage(role="user", content=turn, injected_by=MARKER))
            results.hand(session, run_id, head)  # delivered once a history holds it (on_post_llm_call, next call)
            added += 1
        return HookResult(success=True, modified=bool(added), context=context)

    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        """A handed result whose turn stands in the history this call was made with is delivered -- also on a
        turn's last call, after which no pre_llm_call comes to see it (and its wake would ring again)."""
        results = getattr(self.server, "workflow_results", None)
        if results is not None and context.session_id:
            for run_id, head in results.handed(context.session_id).items():
                if holds(context.messages, head):
                    try:
                        results.deliver(run_id, context.session_id)
                    except Exception:  # noqa: BLE001 - it stays handed: the next call confirms it
                        logger.warning("stategraph: workflow run %s not marked delivered", run_id, exc_info=True)
        return HookResult(success=True, modified=False, context=context)

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
