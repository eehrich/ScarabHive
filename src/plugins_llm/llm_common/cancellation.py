"""Awaiting a provider call so that a cancel stays a cancel.

``agent_system.llm.retry_utils.execute_with_cancellation`` watches the token
while the call runs, but reports the cancel it finds as a plain ``Exception``.
The agent server reports a cancelled request as cancelled only when it arrives
as ``asyncio.CancelledError`` -- as anything else it becomes an upstream error,
which walks the whole fallback chain and ends the run as "Agent execution
failed". A client that watches its call while it runs does it through here
(llm_openai, llm_ollama, llm_anthropic; llm_gemini brings its own watch).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from agent_system.llm.retry_utils import execute_with_cancellation


async def await_call(llm_task: "asyncio.Task", cancellation_token: Any) -> Any:
    """The task's result; ``CancelledError`` once the user has cancelled."""
    if not cancellation_token:
        # The watcher reads the token unguarded, so nothing to watch with.
        return await llm_task
    try:
        return await execute_with_cancellation(llm_task, cancellation_token)
    except Exception as e:
        # Whatever ended the call, the user had already cancelled it. The
        # message is the one every other cancel carries; what really ended
        # the call stays in the cause.
        if cancellation_token.is_cancelled:
            raise asyncio.CancelledError("Request cancelled by user") from e
        raise
    finally:
        # A cancel from outside (the run itself was cancelled) leaves the
        # call running otherwise -- billed to the end, answer thrown away.
        # Waiting for it to land matters: the caller closes its HTTP client
        # on the way out, and a request that was only ASKED to stop would
        # still be holding it. Nobody awaits the task after this, so its
        # outcome is consumed here as well.
        if not llm_task.done():
            llm_task.cancel()
            with contextlib.suppress(BaseException):
                await llm_task
