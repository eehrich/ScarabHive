"""Tool parameters and plugin settings as models and YAML send them.

The framework does not check tool arguments against the schema, and YAML
settings arrive as whatever the operator typed. One reading for every plugin:

- ``as_bool``  -- true/false, 1/0, yes/no, on/off (any case); text is never
  truthy just for being non-empty (``"false"`` is a true value in Python).
- ``as_int``   -- an int, a whole float or its text (``"20"``, ``"20.0"``);
  never a bool, never inf/nan, no OverflowError; optionally clamped.
- ``as_float`` -- a finite number or its text; never a bool; optionally clamped.

Each takes a ``default`` for "not given" (None or blank text). For a value that
is given but unusable, ``strict=True`` raises a ValueError naming ``name``
(the message a model can act on); otherwise the default is returned.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, TypeVar

_TRUE = frozenset(("true", "1", "yes", "on"))
_FALSE = frozenset(("false", "0", "no", "off"))

_D = TypeVar("_D")


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _unusable(name: str, wanted: str, value: Any, strict: bool, default: Any) -> Any:
    if strict:
        raise ValueError(f"{name}: {wanted}, got {value!r}")
    return default


def as_bool(value: Any, default: bool = False, *, name: str = "value",
            strict: bool = False) -> bool:
    """A yes/no value; see the module docstring."""
    if _missing(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return _unusable(name, "true or false", value, strict, default)


def _clamp(number: Any, low: Any, high: Any) -> Any:
    if low is not None and number < low:
        return low
    if high is not None and number > high:
        return high
    return number


def as_int(value: Any, default: _D, *, low: Optional[int] = None,
           high: Optional[int] = None, name: str = "value",
           strict: bool = False) -> "int | _D":
    """A whole number, clamped to ``[low, high]`` when given; see the module docstring.

    The default is returned as is (not clamped), so ``None`` can mean "unset".
    """
    if _missing(value):
        return default
    number: Optional[int] = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            number = int(value)
    else:
        text = str(value).strip()
        try:
            number = int(text)
        except ValueError:
            try:
                as_float_ = float(text)
            except ValueError:
                as_float_ = math.nan
            if math.isfinite(as_float_) and as_float_.is_integer():
                number = int(as_float_)
    if number is None:
        return _unusable(name, "a whole number", value, strict, default)
    return _clamp(number, low, high)


def as_float(value: Any, default: _D, *, low: Optional[float] = None,
             high: Optional[float] = None, name: str = "value",
             strict: bool = False) -> "float | _D":
    """A finite number, clamped to ``[low, high]`` when given; see the module docstring."""
    if _missing(value):
        return default
    number: Optional[float] = None
    if not isinstance(value, bool):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = None
    if number is None or not math.isfinite(number):
        return _unusable(name, "a finite number", value, strict, default)
    return _clamp(number, low, high)


def bool_param(params: Dict[str, Any], key: str) -> bool:
    """A yes/no parameter, or a ValueError that says which and how.

    Models send booleans as text too, and `"false"` is a true value in Python:
    `recursive: "false"` deleted a whole directory tree, `overwrite: "false"`
    replaced the file it was meant to protect.
    """
    return as_bool(params.get(key), False, name=key, strict=True)


def flag_param(params: Dict[str, Any], key: str, default: bool = False) -> bool:
    """A yes/no parameter where an unusable value means ``default`` instead of an error."""
    return as_bool(params.get(key), default, name=key)
