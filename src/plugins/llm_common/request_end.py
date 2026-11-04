"""How a provider request that ended by an exception is reported.

Every chat client fires post_llm_response for each request, also for one that
ended by error, cancellation or an abandoned stream; this is the error text
that report carries. It lived as an identical private copy in five clients.
"""
from __future__ import annotations

import asyncio

__all__ = ["ending_error"]


def ending_error(error: BaseException) -> str:
    """The post_llm_response error text for a request that ended by *error*."""
    if isinstance(error, asyncio.CancelledError):
        return str(error) or "cancelled"
    if isinstance(error, GeneratorExit):
        return "stream abandoned by the caller"
    return str(error) or type(error).__name__
