"""Report a client's requests that no agent wires through llm/hook_notify.py.

No agent wires a client a plugin builds for itself (llm_router's routed client, context_engineer's summaries and
prune checkpoints). Without this its requests reached no pre_llm_request/post_llm_response hook: the message
debugger, otel and the usage tracker never saw them, and their cost counted nowhere. The agent-less path is the right
one, not the calling agent's hooks: the tracker books an agent-less call as spend of its own, and would skip one
carrying the agent -- whose own ledger never sees this call.
"""
from __future__ import annotations

from typing import Any

from agent_system.core.request_context import current_run_session
from agent_system.llm import hook_notify

__all__ = ["book_as_agentless"]


def book_as_agentless(client: Any, session_id: str = "") -> None:
    """Wire ``client``'s request hooks to the agent-less notifications (idempotent: it sets, never stacks).

    Without ``session_id`` a call is booked to the run it is made in (current_run_session), read per call: a cached
    client serves many sessions."""
    if not hasattr(client, "set_llm_hooks"):
        return

    def session() -> str:
        return session_id or current_run_session.get() or ""

    async def on_request(info: dict) -> None:
        await hook_notify.notify_request(
            provider=info.get("provider") or "unknown", model=info.get("model") or "",
            url=info.get("url") or "", payload=info.get("payload"), session_id=session())

    async def on_response(info: dict) -> None:
        await hook_notify.notify_response(
            provider=info.get("provider") or "unknown", model=info.get("model") or "",
            url=info.get("url") or "", duration_ms=info.get("duration_ms") or 0.0,
            response_data=info.get("response_data"), usage=info.get("usage"),
            session_id=session(), error=info.get("error"),
            finish_reason=info.get("finish_reason"),
            metadata={"served_by": (info.get("routing") or {}).get("selected")})

    client.set_llm_hooks(on_pre_request=on_request, on_post_response=on_response)
