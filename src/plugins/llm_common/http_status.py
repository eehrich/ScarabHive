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

__all__ = ["RETRYABLE_STATUS"]

#: Worth another attempt: rate limit, origin 5xx, Cloudflare's 52x range, overload.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})
