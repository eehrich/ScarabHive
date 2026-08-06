"""Bounded reads over stored text — the `read` half of list/search/read.

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
#: Deliberately the SAME number the variable and tool-result handlers already
#: enforce internally: a higher ceiling here would simply be refused by them,
#: and the caller would see a validation error instead of a bounded answer.
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
    """A bounded window into ``content``, plus how to get the rest."""
    total = len(content)
    limit = DEFAULT_READ_CHARS if limit is None else max(1, min(int(limit), MAX_READ_CHARS))
    offset = max(0, int(offset))

    chunk = content[offset:offset + limit]
    end = offset + len(chunk)
    truncated = end < total or offset > 0
    return {
        "content": chunk,
        "offset": offset,
        "returned_chars": len(chunk),
        "total_chars": total,
        "truncated": truncated,
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
