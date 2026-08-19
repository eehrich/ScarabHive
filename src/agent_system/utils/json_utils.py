import json
import logging
import re
from typing import Any, Optional

import json_repair as _json_repair_lib

__all__ = ["safe_serialize", "repair_json", "strip_markdown_fences"]

logger = logging.getLogger(__name__)


# Matches a single fenced block: ```json\n...\n``` or ``` \n...\n```
# Non-greedy body so multiple fences in one text don't merge.
_MARKDOWN_FENCE_RE = re.compile(
    r"```(?:[a-zA-Z0-9_+-]*)\s*\n?(.*?)\n?```",
    re.DOTALL,
)


def strip_markdown_fences(text: str) -> str:
    """Strip Markdown code fences from LLM output and return inner content.

    Handles the three patterns that appear across the codebase:
    - Single fenced block with language tag: ``` ```json\\n{...}\\n``` ```
    - Single fenced block without language: ``` ```\\n{...}\\n``` ```
    - Text where the entire output is wrapped in fences (head + tail)

    Idempotent: text without fences is returned unchanged (stripped of
    surrounding whitespace). Multiple fence blocks: returns the FIRST
    inner block (matches existing call-site semantics — LLM-output rarely
    has multiple JSON fences, and earlier code took the first).

    Args:
        text: Raw LLM output, possibly with markdown fences around JSON.

    Returns:
        The text inside the first fence block, or the input text stripped
        of leading/trailing whitespace if no fence is detected.
    """
    if not text:
        return text or ""
    stripped = text.strip()
    if not stripped:
        return ""

    # Fast path: leading + trailing fences (most common case for LLM JSON
    # output that's fully wrapped). Mirrors the existing line-based strippers
    # in pipeline_agent._parse_json_result and polish_pipeline.extract_json_from_text.
    if stripped.startswith("```"):
        # Skip the first line (e.g. "```json" or "```")
        nl = stripped.find("\n")
        if nl >= 0:
            body = stripped[nl + 1:]
        else:
            body = stripped[3:]
        body = body.rstrip()
        if body.endswith("```"):
            # Fully fenced: return the inner content
            inner = body[:-3].strip()
            if inner:
                return inner
            # empty body -> regex below
        elif "```" not in body:
            # No closing fence at all: best effort, return everything after
            # the opening fence (mirrors the old line-based strippers).
            body = body.strip()
            if body:
                return body
        # Fall through: there IS a closing fence but prose follows it
        # (fenced JSON with a trailing "Note: ..." line), or the body was
        # empty. Returning body here handed the caller the closing fence plus
        # the trailing prose; the regex below extracts the fence's content.

    # General path: find a fence anywhere in the text (e.g. ``` ```json…``` ```
    # embedded inside surrounding prose). Returns first match's inner content.
    match = _MARKDOWN_FENCE_RE.search(stripped)
    if match:
        inner = match.group(1).strip()
        if inner:
            return inner

    return stripped


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
