"""Near-miss suggestions for paths an agent got slightly wrong.

An LLM that asks for ``reference/seitenformat.md`` when the bundle holds
``references/seitenformat.md`` has made a typo, and a "did you mean" turns a
wasted turn into a corrected one.

The reason this needs its own module rather than a two-line ``get_close_matches``
call at each site is the case where a suggestion is WORSE than the error:
``kapitel_15.md`` does not exist while ``kapitel_16.md`` does. Those are two
different chapters, not two spellings of one, and pointing an agent at the
neighbour invites it to write the right content into the wrong document —
silently, and past every conformance check. Numeric siblings are therefore
suppressed by construction, not by tuning a similarity cutoff.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Sequence

#: Below this ratio a "suggestion" is a guess, and a wrong guess costs more than
#: the plain not-found it replaces.
DEFAULT_CUTOFF = 0.72

_DIGITS_RE = re.compile(r"\d+")


def _digits_stripped(text: str) -> str:
    return _DIGITS_RE.sub("", text)


def differ_only_in_digits(a: str, b: str) -> bool:
    """Whether two names are the same apart from their digits.

    ``kapitel_15.md`` / ``kapitel_16.md`` -> True (different chapters)
    ``reference/x.md`` / ``references/x.md`` -> False (one is a typo)
    """
    return a != b and _digits_stripped(a) == _digits_stripped(b)


def suggest_path(wanted: str, candidates: Iterable[str],
                 cutoff: float = DEFAULT_CUTOFF) -> Optional[str]:
    """The one candidate ``wanted`` was probably meant to be, or None.

    Only ONE suggestion is ever returned: a list of maybes is noise the agent
    has to re-decide, and it already gets the real listing alongside.

    Cost is linear in the candidate count — measured at ~95 ms for 5000, which
    is why this stays on the error path and is never used to answer a hit.
    """
    wanted = (wanted or "").strip()
    pool = [c for c in candidates if c]
    if not wanted or not pool:
        return None

    lowered = wanted.lower()
    for c in pool:  # a pure case slip is not a guess — take it
        if c.lower() == lowered:
            return c

    best, best_score = None, 0.0
    for c in pool:
        if differ_only_in_digits(lowered, c.lower()):
            continue
        score = SequenceMatcher(None, lowered, c.lower()).ratio()
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= cutoff else None


def siblings_of(wanted: str, candidates: Sequence[str], limit: int = 20) -> List[str]:
    """What actually exists next to ``wanted``, for the not-a-typo case.

    When the wanted document simply is not there yet, the useful answer is the
    directory listing, not a guess: the agent can see the gap and decide whether
    to create it.
    """
    wanted = (wanted or "").strip()
    prefix = wanted.rsplit("/", 1)[0] + "/" if "/" in wanted else ""
    same_dir = [c for c in candidates if c.rsplit("/", 1)[0] + "/" == prefix] \
        if prefix else list(candidates)
    return sorted(same_dir)[:limit] or sorted(candidates)[:limit]
