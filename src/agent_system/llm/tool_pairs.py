"""Which tool results belong to which assistant turn -- by position, not by id alone.

Every layer that moves, drops or summarises messages must keep an assistant
with tool_calls together with ALL of its results: half a pair is a 400 from
every provider, and the broken list is then persisted and re-sent on every
following step.

Ids are not unique across a history: the Gemini batch client mints
``call_{name}_{index}``, which repeats across assistant turns. A map
``tool_call_id -> assistant`` is last-writer-wins and hands an old result to a
later assistant. A result belongs to the most recent assistant BEFORE it that
issued its id. (Moved here from context_engineer's compaction, which had it
right; context_summarizer paired by id.)
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

__all__ = ["tool_call_owners", "tool_call_units"]


def _get(msg: Any, name: str) -> Any:
    return msg.get(name) if isinstance(msg, Mapping) else getattr(msg, name, None)


def _call_ids(msg: Any) -> list[str]:
    ids = []
    for tc in _get(msg, "tool_calls") or []:
        tc_id = tc.get("id") if isinstance(tc, Mapping) else getattr(tc, "id", None)
        if tc_id:
            ids.append(tc_id)
    return ids


def tool_call_owners(messages: Sequence[Any]) -> Dict[int, int]:
    """``{tool message index: index of the assistant it answers}``.

    A tool result is owned by the most recent assistant before it that issued
    its id; an assistant whose tool_calls carry no usable id owns the run of
    tool messages right after it. A result no assistant owns is absent.
    Messages may be dicts or ChatMessage objects.
    """
    owners: Dict[int, int] = {}
    issued_by: Dict[str, int] = {}
    idless: int | None = None
    for i, msg in enumerate(messages):
        role = _get(msg, "role")
        if role == "tool":
            owner = issued_by.get(_get(msg, "tool_call_id") or "")
            if owner is None:
                owner = idless
            if owner is not None:
                owners[i] = owner
            continue
        idless = None
        if role != "assistant" or not _get(msg, "tool_calls"):
            continue
        ids = _call_ids(msg)
        for tc_id in ids:
            issued_by[tc_id] = i
        if not ids:
            # Nothing links the results to this message: absorb the
            # contiguous run of tool messages that follows -- the shape every
            # provider emits -- instead of splitting a pair.
            idless = i
    return owners


def tool_call_units(messages: Sequence[Any]) -> Dict[int, frozenset[int]]:
    """Map each assistant/tool index to the indivisible unit it belongs to.

    An assistant with tool_calls plus all of its results is one unit. Indices
    with no tool involvement are absent -- callers treat that as a unit of one.
    Units hold only assistant/tool indices, so a unit never contains a system
    or user message.
    """
    units: Dict[int, set[int]] = {}
    for i, msg in enumerate(messages):
        if _get(msg, "role") == "assistant" and _get(msg, "tool_calls"):
            units[i] = {i}
    for tool_index, owner in tool_call_owners(messages).items():
        units[owner].add(tool_index)
    groups: Dict[int, frozenset[int]] = {}
    for members in units.values():
        unit = frozenset(members)
        for member in members:
            groups[member] = unit
    return groups
