"""Which HTTP statuses a provider call should try again.

Every client here reaches its provider through Cloudflare, and Cloudflare
answers with its own 52x range when the origin is the problem: 520 is the
catch-all for an unexpected origin response, 521-524 name the origin being
down, refusing, unreachable or too slow. All of them are transient by
definition, and none of them is a 5xx the origin itself sent.

They belong next to the classic 429/50x because leaving them out is not a
missing retry but a hard failure: the client raises, and a caller that fans
out loses the whole series to one flaky call. Measured on the decisions
endpoint (2026-09-23): a 520 under six concurrent calls, dead on the spot.

529 is "overloaded": TypeSafe's API reference names it beside 429 as the one
to retry with backoff, and Anthropic uses the code the same way.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

__all__ = ["RETRYABLE_STATUS", "retry_after_seconds"]

#: Worth another attempt: rate limit, origin 5xx, Cloudflare's 52x range, overload.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})


_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def retry_after_seconds(value: Any, *, cap: Optional[float] = None,
                        now: Optional[datetime] = None) -> Optional[float]:
    """A Retry-After (or rate-limit reset) header as seconds to wait, else None.

    Reads plain seconds (``20``, ``1.5``), duration forms (``1.5s``, ``200ms``,
    ``6m0s``) and the HTTP-date form RFC 9110 allows (``Wed, 21 Oct 2026
    07:28:00 GMT``). The answer is finite and >= 0 -- inf, nan, a negative
    number or unreadable text is no hint at all -- and at most ``cap`` when
    one is given: a server's ``Retry-After: 3600`` must not park a turn.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    seconds: Optional[float]
    try:
        seconds = float(text)
    except ValueError:
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", text)
        if parts and "".join(number + unit for number, unit in parts) == text:
            seconds = sum(float(number) * _UNIT_SECONDS[unit] for number, unit in parts)
        else:
            try:
                when = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, cap) if cap is not None else seconds
