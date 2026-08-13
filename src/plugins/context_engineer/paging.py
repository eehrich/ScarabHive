"""Bounded reads over stored text — the `read` half of list/read.

Retrieval used to be all-or-nothing: asking for a stored tool result returned
the whole thing, measured in production at 134k characters in one call. That
undoes the compaction that put it away in the first place, and usually the agent
wanted one section of it.

Everything here answers with the same envelope, whatever kind of thing was read,
so a model can build one expectation instead of four:

    content, offset, returned_chars, total_chars, truncated, next_offset

``next_offset`` is the whole point of the shape: a truncated answer that does not
say how to continue is a dead end, and the agent's only recovery is to call the
original tool again.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Per-read ceiling. Generous enough for a real section, small enough that a
#: mis-aimed read cannot refill the context it is protecting.
#:
#: One ceiling for every kind of read — archive entries, tool results and the
#: attached files that share their store all come back through here.
MAX_READ_CHARS = 5000

#: What a read returns when the caller names no size.
DEFAULT_READ_CHARS = 2000

#: Characters of context shown around each hit in a find.
FIND_CONTEXT_CHARS = 200

#: A find reports at most this many hits; more than that is a browsing problem,
#: not a reading one.
MAX_FIND_MATCHES = 12


def slice_text(content: str, *, offset: int = 0,
               limit: Optional[int] = None) -> Dict[str, Any]:
    """A bounded window into ``content``, plus how to get the rest.

    A NEGATIVE offset counts from the end — ``offset=-2000`` is the last 2000
    characters, the ``tail`` every model already knows from files. Without it
    the only way to reach the end of a stored result was to page forward
    through all of it, which is the opposite of what compaction is for:
    observed live, an agent walking a 100k-character result in 5000-character
    steps put the whole thing back into the context it had just been freed from.
    """
    total = len(content)
    # 0 means "no size given", not "zero characters". The handler this replaced
    # wrote `limit or DEFAULT_READ_CHARS`, so a 0 became 2000; going through
    # max(1, ...) turned it into a ONE-character answer and a next_offset walk
    # into one call per character.
    limit = (DEFAULT_READ_CHARS if not limit or int(limit) <= 0
             else min(int(limit), MAX_READ_CHARS))
    offset = int(offset)
    offset = max(0, total + offset) if offset < 0 else offset

    chunk = content[offset:offset + limit]
    end = offset + len(chunk)
    return {
        "content": chunk,
        "offset": offset,
        "returned_chars": len(chunk),
        "total_chars": total,
        "truncated": end < total or offset > 0,
        # Both directions, like the list tool. A tail read answers
        # truncated=True with next_offset=None, which alone reads as a dead
        # end — "you did not see everything" and no way to continue. What is
        # missing there sits BEFORE the window.
        "has_more_before": offset > 0,
        "has_more_after": end < total,
        "next_offset": end if end < total else None,
    }


def find_in_text(content: str, needle: str, *,
                 context_chars: int = FIND_CONTEXT_CHARS,
                 max_matches: int = MAX_FIND_MATCHES) -> Dict[str, Any]:
    """Hits for ``needle`` with surrounding context, instead of the whole text.

    This is what makes a large stored result usable without pulling it back:
    "the part about chapter 3" rather than all 134k characters of it.
    """
    total = len(content)
    if not needle:
        return {"matches": [], "match_count": 0, "total_chars": total,
                "error": "find requires a non-empty string"}

    haystack, pin = content.lower(), needle.lower()
    matches: List[Dict[str, Any]] = []
    pos = 0
    while len(matches) < max_matches:
        idx = haystack.find(pin, pos)
        if idx == -1:
            break
        start = max(0, idx - context_chars)
        end = min(total, idx + len(needle) + context_chars)
        matches.append({
            "at": idx,
            "snippet": ("…" if start else "") + content[start:end] + ("…" if end < total else ""),
        })
        # Past the whole needle, not one character: overlapping hits on a
        # repeated word would otherwise fill the budget with near-duplicates.
        pos = idx + len(needle)

    more = haystack.find(pin, pos) != -1 if matches else False
    return {
        "matches": matches,
        "match_count": len(matches),
        "more_matches": more,
        "total_chars": total,
    }
