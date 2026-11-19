"""``copy.deepcopy`` for the data a run carries -- dicts, lists, pydantic models of them, strings -- at about twice
the speed, with the same result.

The hook registry hands every hook a deep copy of the run's context (hooks/registry.py), the whole conversation
among it, for every hook of every step. Measured under load (50 agents at once, py-spy on the thread holding the
GIL), ``copy.deepcopy`` took about a fifth of the API process's Python time: a function call for every string of
the history, the generic dispatch for every container, pydantic's own ``__deepcopy__`` for every message.

The same copy, done for these types inline:

- atomic values (str, int, float, bool, None, bytes, complex) are kept, as ``copy.deepcopy`` keeps them;
- a plain ``dict`` or ``list`` is copied element by element;
- a pydantic model whose ``__deepcopy__`` is pydantic's own is copied as that method does it (its ``__dict__``,
  ``__pydantic_extra__``, ``__pydantic_fields_set__``, ``__pydantic_private__``);
- everything else -- a subclass of dict or list, a tuple, a set, a model with a ``__deepcopy__`` of its own, any
  other object -- goes to ``copy.deepcopy``.

One memo throughout, keyed as ``copy.deepcopy`` keys it (the id of the original) and handed on to it: an object the
structure holds twice is copied once and held twice in the copy, and a cycle stays a cycle.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

_ATOMIC = frozenset({str, int, float, bool, type(None), bytes, complex})
_PYDANTIC_DEEPCOPY = BaseModel.__deepcopy__


def deep_copy(value: Any, memo: Optional[dict[int, Any]] = None) -> Any:
    """A deep copy of ``value``, as ``copy.deepcopy(value)`` makes it."""
    kind = type(value)
    if kind in _ATOMIC:
        return value
    if memo is None:
        memo = {}
    key = id(value)
    if key in memo:
        return memo[key]
    if kind is dict:
        out_dict: dict = {}
        memo[key] = out_dict
        for k, v in value.items():
            out_dict[k if type(k) in _ATOMIC else deep_copy(k, memo)] = v if type(v) in _ATOMIC else deep_copy(v, memo)
        return out_dict
    if kind is list:
        out_list: list = []
        memo[key] = out_list
        out_list.extend(v if type(v) in _ATOMIC else deep_copy(v, memo) for v in value)
        return out_list
    if isinstance(value, BaseModel) and kind.__deepcopy__ is _PYDANTIC_DEEPCOPY:
        model = kind.__new__(kind)
        memo[key] = model
        object.__setattr__(model, "__dict__", deep_copy(value.__dict__, memo))
        object.__setattr__(model, "__pydantic_extra__", deep_copy(value.__pydantic_extra__, memo))
        object.__setattr__(model, "__pydantic_fields_set__", copy.copy(value.__pydantic_fields_set__))
        private = getattr(value, "__pydantic_private__", None)
        object.__setattr__(model, "__pydantic_private__", None if private is None else copy.deepcopy(
            {k: v for k, v in private.items() if v is not PydanticUndefined}, memo))
        return model
    return copy.deepcopy(value, memo)
