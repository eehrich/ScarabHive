"""Retry utilities for LLM clients.

Common retry and error handling utilities shared across LLM client implementations.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any


def parse_retry_delay(error_msg: str) -> float | None:
    """Parse retry delay from rate limit error message.
    
    Looks for patterns like:
    - 'Please retry in 32.487019579s'
    - 'retryDelay': '32s'
    - 'retry-after' header values
    
    Args:
        error_msg: Error message or header value to parse
        
    Returns:
        Delay in seconds, or None if not found.
    """
    # Try to find "Please retry in Xs" pattern
    match = re.search(r'retry in ([\d.]+)s', error_msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    
    # Try to find retryDelay JSON pattern
    match = re.search(r'"retryDelay"\s*:\s*"(\d+)s?"', error_msg)
    if match:
        return float(match.group(1))
    
    return None


def is_rate_limit_error(error: Exception) -> bool:
    """Check if error is a rate limit (429) error.
    
    Args:
        error: Exception to check
        
    Returns:
        True if the error indicates rate limiting.
    """
    error_str = str(error).lower()
    return (
        '429' in error_str or
        'rate limit' in error_str or
        'resource_exhausted' in error_str or
        'quota' in error_str or
        'too many requests' in error_str
    )


async def execute_with_cancellation(
    llm_task: asyncio.Task,
    cancellation_token: Any,
    check_interval: float = 0.1,
) -> Any:
    """Execute LLM task with efficient event-based cancellation monitoring.

    Instead of polling with timeouts (which throws exceptions every interval),
    uses asyncio.wait() to efficiently wait for either completion or cancellation.

    Args:
        llm_task: The asyncio Task to execute
        cancellation_token: Token with `is_cancelled` property
        check_interval: How often to check cancellation (seconds)
        
    Returns:
        The result of llm_task when completed

    Raises:
        Exception: When cancelled by user
    """
    cancel_event = asyncio.Event()

    async def check_cancellation():
        """Background task that monitors cancellation without polling exceptions"""
        while not llm_task.done():
            if cancellation_token.is_cancelled:
                cancel_event.set()
                break
            await asyncio.sleep(check_interval)

    cancel_task = asyncio.create_task(check_cancellation())

    # Wait for either LLM completion or cancellation (efficient, no exceptions!)
    done, pending = await asyncio.wait(
        {llm_task, cancel_task},
        return_when=asyncio.FIRST_COMPLETED
    )

    if cancel_event.is_set():
        # Cancellation requested - clean up LLM task
        llm_task.cancel()
        try:
            await llm_task
        except asyncio.CancelledError:
            pass
        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
        raise Exception("Request cancelled by user during LLM call")

    # LLM completed - clean up cancel task
    cancel_task.cancel()
    try:
        await cancel_task
    except asyncio.CancelledError:
        pass

    return await llm_task
