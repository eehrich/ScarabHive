"""Hook dispatch for the clients the agent loop does not wire.

A chat client gets its hooks from the agent that owns it (``wire_llm_hooks``).
A TTS or decisions client is called directly — from a plugin, a job, a script
— so there is no agent around it and it must tell the global registry itself.
Without that, the message debugger and every other hook consumer go blind to a
whole class of calls, which is not hypothetical: after the TTS client moved
into a plugin its relative hook imports resolved to the wrong package, every
dispatch raised ImportError, and nothing above DEBUG said so.

Two functions, one per phase. Both take what the CALLER has — a TTS call
brings audio seconds, a decision brings tokens and a cost — and nothing here
branches on which kind of client is calling. That is the whole reason this is
one module and not two: the clients differ in what they fill in, not in how
the dispatch works. A caller's own vocabulary travels in ``metadata``, so
nothing here has to know what a served model is.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: ``provider:phase`` markers whose dispatch already failed. A broken dispatch
#: is a permanent condition, not a per-call event: warn once, then stay quiet.
_reported_failures: set = set()


def current_request_id() -> str:
    """The run this call belongs to, empty outside one.

    ``or ""`` is the guard: the contextvar is declared with ``default=None``,
    so a call made outside a run reads None rather than raising — and None in
    a field typed ``str`` reaches a consumer's table as NULL instead of
    "belongs to no run".
    """
    try:
        from agent_system.tools.status import current_request_id as _var
        return _var.get() or ""
    except Exception:
        return ""


def _report_failure(provider: str, phase: str, error: Exception) -> None:
    """A dead hook dispatch must not be findable only at DEBUG level."""
    marker = f"{provider}:{phase}"
    if marker in _reported_failures:
        logger.debug("%s hook error (%s): %s", phase, provider, error)
        return
    _reported_failures.add(marker)
    logger.warning(
        "%s hooks are NOT being dispatched for provider=%s (%s: %s) — "
        "message_debugger and every other hook consumer are blind to these "
        "calls. Reported once per provider and phase.",
        phase, provider, type(error).__name__, error)


async def notify_request(
    *,
    provider: str,
    model: str,
    url: str,
    payload: Optional[dict] = None,
    session_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire PRE_LLM_REQUEST for a call no agent is wiring."""
    await _dispatch(
        "PRE_LLM_REQUEST", provider=provider, model=model, url=url,
        session_id=session_id, metadata=metadata,
        llm_request_payload=payload,
    )


async def notify_response(
    *,
    provider: str,
    model: str,
    url: str,
    duration_ms: float = 0.0,
    response_data: Optional[dict] = None,
    usage: Optional[dict] = None,
    session_id: str = "",
    error: Optional[str] = None,
    finish_reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire POST_LLM_RESPONSE with a result or an error.

    Called on EVERY way a call can end, including the ways that failed: a
    request the debugger never sees an answer to is how a whole class of calls
    went missing once before.
    """
    await _dispatch(
        "POST_LLM_RESPONSE", provider=provider, model=model, url=url,
        session_id=session_id, metadata=metadata,
        llm_response_data=response_data,
        llm_usage=usage,
        llm_duration_ms=duration_ms,
        llm_error=error,
        llm_finish_reason=finish_reason or ("stop" if not error else None),
    )


async def _dispatch(
    hook_name: str, *, provider: str, model: str, url: str, session_id: str,
    metadata: Optional[Dict[str, Any]], **fields: Any,
) -> None:
    """Tell the global registry; never fail the call because of it.

    A blind debugger is bad; a TTS synthesis or a decision that fails because
    a hook consumer raised is worse. The failure is reported instead — once.
    """
    try:
        from agent_system.hooks import HookContext, HookType, get_hook_registry

        hook_type = getattr(HookType, hook_name)
        context = HookContext(
            hook_type=hook_type,
            request_id=current_request_id(),
            session_id=session_id,
            # No agent: that is exactly what marks these calls for the
            # consumers that must not count them twice (a hook on
            # POST_LLM_RESPONSE sees chat calls as well).
            agent=None,
            agent_name=provider,
            llm_provider=provider,
            llm_model=model,
            llm_request_url=url,
            llm_is_streaming=False,
            metadata={"timestamp_ms": time.time() * 1000, **(metadata or {})},
            **fields,
        )
        await get_hook_registry().execute_hooks(hook_type, context)
    except Exception as e:
        _report_failure(provider, hook_name.lower(), e)
