"""Tool parameters as models send them."""
from __future__ import annotations

from typing import Any, Dict


def bool_param(params: Dict[str, Any], key: str) -> bool:
    """A yes/no parameter, or a ValueError that says which and how.

    Models send booleans as text too, and `"false"` is a true value in Python:
    `recursive: "false"` deleted a whole directory tree, `overwrite: "false"`
    replaced the file it was meant to protect.
    """
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise ValueError(f"{key}: true or false, got {value!r}")
