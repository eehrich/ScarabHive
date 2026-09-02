import json
import logging
import re
from typing import Any, Dict, List, Optional

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


def history_safe_tool_calls(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy tool calls so stored chat history always carries valid arguments JSON.

    tool_execution repairs malformed ``function.arguments`` for the execution
    itself, but the assistant message used to keep the raw string. Providers
    that validate history server-side then reject EVERY later request of the
    session ("Assistant tool call function.arguments must be valid JSON") --
    a poisoned session survives model fallback and burns to max steps
    (observed with v6 agents, 2026-09-01).

    Shape guard: only DICT-shaped repairs survive. repair_json turns common
    defects (concatenated objects, truncated list-wraps) into LISTS -- valid
    JSON, but Anthropic/Gemini put the parsed value into ``tool_use.input`` /
    ``functionCall.args``, which must be an object; a stored list would poison
    those providers instead. A single-dict list is unwrapped exactly like
    tool_execution does for the execution itself; everything else degrades to
    ``{}`` -- the paired JSONParseError tool message already tells the model.

    Returns copies only where a fix is needed; the original objects stay
    untouched on purpose, because the execution path must still error an
    irreparable call back to the model instead of running it with defaulted
    arguments.
    """
    safe: List[Dict[str, Any]] = []
    for tc in tool_calls:
        func = tc.get("function") if isinstance(tc, dict) else None
        raw = func.get("arguments") if isinstance(func, dict) else None
        fixed: Optional[str] = None
        if isinstance(func, dict) and raw is None:
            # Ollama-native calls carry no/dict arguments; string-expecting
            # providers 400 on a missing key after a profile fallback.
            fixed = "{}"
        elif isinstance(raw, dict):
            fixed = json.dumps(raw, ensure_ascii=False)
        elif isinstance(func, dict) and not isinstance(raw, str):
            # Exotic shapes (list, number, bool) — no producer emits them,
            # but the promise of this function is unconditional.
            fixed = "{}"
        elif isinstance(raw, str):
            if not raw.strip():
                fixed = "{}"
            else:
                try:
                    json.loads(raw)
                except json.JSONDecodeError:
                    repaired = repair_json(raw)
                    if (isinstance(repaired, list) and len(repaired) == 1
                            and isinstance(repaired[0], dict)):
                        repaired = repaired[0]
                    if isinstance(repaired, dict):
                        fixed = json.dumps(repaired, ensure_ascii=False)
                    else:
                        fixed = "{}"
                    logger.warning(
                        "Tool call arguments were not valid JSON (len=%d) -- "
                        "history stores the %s version",
                        len(raw),
                        "repaired" if fixed != "{}" else "emptied",
                    )
        if fixed is not None:
            safe.append({**tc, "function": {**func, "arguments": fixed}})
        else:
            safe.append(tc)
    return safe
