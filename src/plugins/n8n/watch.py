"""Waiting for one production execution to end (design §6.2).

n8n is the store of the outcome: the woken run reads it with
n8n_get_execution. This only finds out WHEN there is something to read, over
the public API -- the MCP's 100 requests per five minutes are no budget for
polling (M-MCP-24, F-AUTH5).
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from .client import N8nError, N8nNotFound, N8nUnavailable

# waiting, new and running are no end (F-EXE2, F-BR7); unknown is n8n's own "no idea".
END_STATES = frozenset({"success", "error", "crashed", "canceled", "unknown"})
FIRST_DELAY_S = 2.0
MAX_DELAY_S = 30.0


async def wait_for_end(read: Callable[[], Awaitable[dict]], *, max_s: float,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                       clock: Callable[[], float] = time.monotonic) -> dict:
    """Poll until the execution has an end state: ``{"status": ..., "note"?: ...}``.

    n8n being away is waited out with backoff; after ``max_s`` the answer is
    "unknown". A 404 ends it: either the execution was never stored (a setting
    of the workflow, M-MCP-41) or it is gone (pruning, F-EXE4). Any other error
    -- a refused key, a missing scope -- ends it at once: waiting cannot fix it."""
    deadline = clock() + max_s
    delay, read_once, reachable = FIRST_DELAY_S, False, False
    while True:
        try:
            execution = await read()
        except N8nNotFound:
            return {"status": "not_found",
                    "note": "gone -- pruned, or a setting of the workflow does not keep such runs"
                    if read_once else "not stored -- a setting of the workflow does not keep such runs"}
        except N8nUnavailable:
            reachable = False
        except N8nError as exc:
            return {"status": "unknown", "note": f"the watch stopped: {exc}"}
        else:
            read_once = reachable = True
            state = str(execution.get("status") or "") if isinstance(execution, dict) else ""
            if state in END_STATES:
                return {"status": state}
        if clock() + delay > deadline:
            return {"status": "unknown",
                    "note": f"still not finished after {max_s / 3600:g} h" if reachable
                    else "n8n not reachable -- look in n8n"}
        await sleep(delay)
        delay = min(delay * 1.5, MAX_DELAY_S)
