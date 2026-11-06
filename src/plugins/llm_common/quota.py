"""Whether a 429 is an account cap -- no wait helps -- or a throttle, which a short wait does.

Every client decided it with ``"quota" in text or "exhausted" in text``. Google's normal per-minute throttle is
RESOURCE_EXHAUSTED ("Resource has been exhausted (e.g. check quota)") and matched both, so the model was blocked for
``max_block_seconds`` for every agent (model_health, rate_limit=False) where a short wait would have done. The
other way round costs a short wait that fails again; this way costs the model for everyone -- so a body that names
a per-minute limit or a rate limit is a throttle, whatever else it says.

Patterns after opencode's provider-error.ts (MIT): account-cap codes and wording, Cerebras' token_quota_exceeded
(a per-minute throttle) excluded. Google words a per-minute and a per-day limit the same ("You exceeded your
current quota"); only its quota id tells them apart (...PerMinute... / ...PerDay...).
"""
from __future__ import annotations

import re

__all__ = ["quota_exhausted"]

#: A link names no limit: Google links ".../rate-limits" from every quota message.
_URL = re.compile(r"https?://\S+")
#: A throttle, whatever else the text says.
_THROTTLE = re.compile(
    r"per[-_\s]?minute|perminute|rate[-_\s]?limit|too[-_\s]?many[-_\s]?requests|rate increased too quickly"
    r"|token_quota_exceeded", re.IGNORECASE)
#: An account cap: credits, billing, a plan's or a day's quota.
_CAP = re.compile(
    r"insufficient[-_\s]?(?:quota|balance|credits?|funds)|(?<!token_)quota[-_\s]?exceeded"
    r"|exceeded your current quota|credit balance is too low|out of credits|billing|payment required"
    r"|budget exceeded|spending limit|used all available credits|usage limit|limit exhausted|credit ?limit ?exceeded|perday|per[-_\s]day"
    r"|package has expired|plan does not yet include", re.IGNORECASE)


#: Google's 429 text names its wait ("Please retry in 23.4s."): a short one is a throttle, however it is worded.
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s\b", re.IGNORECASE)
#: Longest wait still read as a throttle, in seconds.
THROTTLE_WAIT_S = 60.0


def quota_exhausted(text: str) -> bool:
    """True when a rate-limit answer's text says an account cap is reached; False for a throttle or nothing known."""
    plain = _URL.sub(" ", str(text or ""))
    if _THROTTLE.search(plain):
        return False
    wait = _RETRY_IN.search(plain)
    if wait and float(wait.group(1)) <= THROTTLE_WAIT_S:
        return False
    return bool(_CAP.search(plain))
