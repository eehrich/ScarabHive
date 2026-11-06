"""Where a turn's thinking blocks stood among its other blocks, and how they go back.

A turn is stored as separate fields -- content, tool_calls, thinking_blocks --
and rebuilt on replay. Rebuilding it as [all thinking] + [text] + [all tool_use]
moved a block the model wrote after a tool call (Opus 5.5, Sonnet 5.5 and Fable
5.x write a progress-update block right before the tool_use it introduces; a
max_tokens stop soon after a call can end on one) in front of it, and a turn
must go back as it came. Each block therefore keeps how many blocks of the
replayed turn -- its text, joined, then its tool_use blocks -- precede it
(``_after``), and the replay puts it back there.

``_after`` is ours: it is stripped from every block that goes out.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

#: Both thinking block types; ``redacted_thinking`` is as mandatory on replay.
THINKING_TYPES = ("thinking", "redacted_thinking")
_AFTER = "_after"


def _as_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    dump = getattr(block, "model_dump", None)
    return dump(mode="json", exclude_none=True) if callable(dump) else {}


def capture(content: Any) -> List[Dict[str, Any]]:
    """Verbatim copy of every thinking block of an answer, with its position.

    Two filters that look reasonable and are NOT allowed here: dropping blocks
    with empty ``thinking`` text (with ``display="omitted"``, the default on the
    5.x models, the text is always empty while the signature carries the
    reasoning), and keeping only ``type == "thinking"`` (that drops
    ``redacted_thinking``). Either makes a complete echo a partial one.
    """
    def kind_of(block: Any) -> Any:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind == "text":
            return "text" if _as_dict(block).get("text") else "empty_text"  # the replay builds no block for empty text
        return kind

    kinds = [kind_of(block) for block in content or []]
    has_text = "text" in kinds
    blocks: List[Dict[str, Any]] = []
    calls = 0
    text_before = False
    for block, kind in zip(content or [], kinds):
        if kind not in THINKING_TYPES:
            if kind == "text":
                text_before = True
            elif kind != "empty_text":
                calls += 1
            continue
        copy = _as_dict(block)
        if copy:
            # Counted in the replay's layout: the turn's text is stored as one
            # string and goes back first, its tool_use blocks after it.
            copy[_AFTER] = calls + (1 if has_text and (text_before or calls) else 0)
            blocks.append(copy)
    return blocks


def sendable(blocks: Any) -> List[Dict[str, Any]]:
    """The blocks that may go out, positions kept for :func:`interleave`.

    A ``thinking`` block without a signature (none arrived for it) is always a
    400 and is left out.
    """
    return [dict(b) for b in blocks or [] if isinstance(b, dict)
            and not (b.get("type") == "thinking" and not b.get("signature"))]


def interleave(thinking: List[Dict[str, Any]], others: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """*others* (text, tool_use) with each thinking block back where it stood.

    A block stored before positions were kept stands first, where the replay
    always put it.
    """
    out: List[Dict[str, Any]] = []
    pending = sorted(enumerate(thinking), key=lambda pair: (pair[1].get(_AFTER, 0), pair[0]))
    i = 0
    for position in range(len(others) + 1):
        while i < len(pending) and min(pending[i][1].get(_AFTER, 0), len(others)) <= position:
            out.append({k: v for k, v in pending[i][1].items() if k != _AFTER})
            i += 1
        if position < len(others):
            out.append(others[position])
    return out


_NAMED = re.compile(r"messages\.(\d+)\.content\.(\d+)")


def _proof(block: Any) -> Any:
    return block.get("signature") or block.get("data") if isinstance(block, dict) else None


def strip_from_refused(messages: List[Any], sent: List[Dict[str, Any]], error: str) -> int:
    """Anthropic's recovery from a refused thinking binding, on the session itself.

    The error names the block (``messages.N.content.M`` of the request sent);
    that block and every thinking block after it leave the history -- text and
    tool calls stay, the thinking text moves to ``reasoning_content``. Without
    a block to find, all of them go. Returns number of messages touched.
    """
    start = 0
    named = _NAMED.search(error or "")
    if named:
        try:
            proof = _proof(sent[int(named.group(1))]["content"][int(named.group(2))])
        except (IndexError, KeyError, TypeError):
            proof = None
        hits = [i for i, msg in enumerate(messages) if proof and any(
            _proof(b) == proof for b in getattr(msg, "thinking_blocks", None) or [])]
        start = hits[0] if hits else 0
    touched = 0
    for msg in messages[start:]:
        blocks = getattr(msg, "thinking_blocks", None)
        if not blocks:
            continue
        text = "".join(b.get("thinking") or "" for b in blocks if isinstance(b, dict))
        if text and not getattr(msg, "reasoning_content", None):
            msg.reasoning_content = text
        msg.thinking_blocks = None
        touched += 1
    return touched
