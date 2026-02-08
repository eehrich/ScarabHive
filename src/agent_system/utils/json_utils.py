import json
import logging
from typing import Any, Optional

import json_repair as _json_repair_lib

__all__ = ["safe_serialize", "repair_json"]

logger = logging.getLogger(__name__)


def repair_json(
    raw: str,
    *,
    ensure_ascii: bool = False,
    return_objects: bool = True,
) -> Optional[Any]:
    """Repair malformed JSON strings, typically from LLM output.

    Uses the ``json-repair`` library which handles:
    - Missing or extra commas
    - Missing quotes around keys or values
    - Single quotes instead of double quotes
    - Missing closing brackets / braces (truncated JSON)
    - Unescaped characters inside strings
    - Trailing commas before ``}`` or ``]``
    - Comments (``//`` and ``/* */``)
    - JavaScript-style unquoted keys
    - Boolean/null case variations (``True`` → ``true``)
    - Incomplete key-value pairs
    - And many more edge cases

    Args:
        raw: The potentially malformed JSON string.
        ensure_ascii: If False (default), preserve non-Latin characters
            (German, Chinese, etc.) in the output.
        return_objects: If True (default), return parsed Python objects directly.
            If False, return the repaired JSON string.

    Returns:
        The parsed Python object (dict/list/str/etc.) when ``return_objects=True``,
        or the repaired JSON string when ``return_objects=False``.
        Returns ``None`` only if the input is empty/whitespace or repair yields
        an empty result.
    """
    if not raw or not raw.strip():
        return None

    try:
        result = _json_repair_lib.repair_json(
            raw,
            return_objects=return_objects,
            ensure_ascii=ensure_ascii,
        )
    except Exception:
        logger.debug("json-repair failed for input (length %d)", len(raw), exc_info=True)
        return None

    # json-repair returns "" for completely broken input
    if result == "" or result is None:
        return None

    # If return_objects=False, result is a string — validate it parses
    if not return_objects:
        try:
            json.loads(result)  # type: ignore[arg-type]
        except (json.JSONDecodeError, TypeError):
            return None

    return result

def safe_serialize(obj: Any) -> str:
    """Serialize arbitrary Python objects to JSON safely.

    Strategy:
    - First attempt direct json.dumps
    - On failure, recursively convert unsupported types (dict, list, primitives retained)
    - Fallback emits minimal error JSON if conversion still fails
    """
    def _convert(o):
        if isinstance(o, (str, int, float, bool)) or o is None:
            return o
        if isinstance(o, dict):
            return {str(k): _convert(v) for k, v in o.items()}
        if isinstance(o, (list, tuple, set)):
            return [_convert(v) for v in o]
        return f"<{type(o).__name__}>"

    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps(_convert(obj), ensure_ascii=False)
        except Exception:
            return json.dumps({"error": f"Unserializable object of type {type(obj).__name__}"}, ensure_ascii=False)
