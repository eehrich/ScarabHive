"""Which turn a change belongs to, and where that turn stands in the conversation now.

A checkpoint is named after a TURN: the message /undo cuts at (a question a
person sent, or the wake of a woken run -- ``chat_actions.starts_a_turn``) and
everything that answered it. Every change is filed under the turn that was the
conversation's last one when the change was recorded.

The conversation moves under the record, though. /undo drops the last turn
and leaves its files as they are (their changes are still on disk, and a later
rewind further back has to undo them too), /retry asks again, compaction drops
old turns from the front. So a turn is not identified by its number but by a
KEY -- the head message's role, the id of the run it opened, its timestamp --
and the record keeps, for every turn it has changes of, the keys of the turns
before it (its ancestors). That is enough to place every recorded turn in the
conversation as it is NOW:

* a turn still in the conversation stands at its index;
* a turn that was dropped stands right behind its nearest ancestor that is
  still there (``index + 0.5``): it happened after that turn began and before
  any turn that followed it -- a turn after it would have it as an ancestor;
* a turn none of whose ancestors are left stands before everything (-0.5).

Two dropped turns behind the same ancestor are ordered by the tree they grew
in: a turn after every turn it grew from, sibling branches by their oldest
record (hooks._order). "Everything since checkpoint C" is then every turn that
does not stand before C.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from agent_system.chat_actions import message_role, message_text, one_line, starts_a_turn

#: Length of a turn key in hex digits (64 bits).
KEY_LENGTH = 16
#: Characters of the question kept for listings.
QUESTION_CHARS = 120
#: Position of a turn none of whose ancestors is in the conversation any more.
BEFORE_EVERYTHING = -0.5


def _field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _timestamp_text(value: Any) -> str:
    """A message timestamp in one spelling, whether it is the datetime a
    ChatMessage holds or the ISO text a session file stores."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return str(value)


def head_key(message: Any) -> str:
    """The key of a turn, from its head message.

    The run id and the timestamp tell apart two heads with the same words (a
    question asked twice, /retry). The text joins only when both are missing:
    a head is not re-worded, but a key that depended on the words would break
    on the one that is.
    """
    request_id = _field(message, "request_id") or ""
    stamp = _timestamp_text(_field(message, "timestamp"))
    text = "" if (request_id or stamp) else message_text(message)
    raw = "\x00".join((message_role(message), str(request_id), stamp, text))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:KEY_LENGTH]


@dataclass(frozen=True)
class TurnRef:
    """The conversation's current turn: its key, the keys before it, its question."""

    key: str
    ancestors: Tuple[str, ...]
    question: str
    #: 0-based index of the turn in the conversation when it was read.
    index: int


def heads(messages: Iterable[Any]) -> List[Any]:
    """The head messages of the conversation's turns, in order."""
    return [message for message in messages if starts_a_turn(message)]


def current_turn(messages: Sequence[Any]) -> Optional[TurnRef]:
    """The turn a change made now belongs to: the conversation's last one."""
    found = heads(messages)
    if not found:
        return None
    keys = [head_key(message) for message in found]
    return TurnRef(key=keys[-1], ancestors=tuple(keys[:-1]),
                   question=one_line(message_text(found[-1]), QUESTION_CHARS),
                   index=len(keys) - 1)


def head_index(messages: Sequence[Any]) -> dict:
    """Turn key -> 0-based index, for the conversation as it is now."""
    return {head_key(message): index for index, message in enumerate(heads(messages))}


def position(turn_key: str, ancestors: Sequence[str], index: dict) -> float:
    """Where a recorded turn stands in the conversation ``index`` describes."""
    own = index.get(turn_key)
    if own is not None:
        return float(own)
    for ancestor in reversed(tuple(ancestors)):
        found = index.get(ancestor)
        if found is not None:
            return found + 0.5
    return BEFORE_EVERYTHING
