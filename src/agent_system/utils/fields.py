"""Reading a field of something that arrives as a dict or as an object."""
from __future__ import annotations

from typing import Any


def field_of(obj: Any, name: str, default: Any = None) -> Any:
    """*name* of *obj* -- a key of a dict, an attribute of anything else -- or *default*."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
