"""What the judge sees, and how its answer is read.

Pure functions, no I/O: the hook builds the excerpt synchronously at the step
boundary and hands it to a background task, so this module has to be cheap and
has to be testable without an agent.

The excerpt is bounded no matter how long the observed run is (concept §5):
the task, the tail of the system prompt, the last few KB of thinking and the
STATUS of the recent tool calls — never their arguments, because for a writing
tool the arguments are the whole content and the excerpt would outgrow the run
it is judging.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from agent_system.utils.reasoning_artifacts import thinking_text

VERDICTS = ("continue", "steer", "abort")


def _get(msg: Any, key: str) -> Any:
    if isinstance(msg, dict):
        return msg.get(key)
    return getattr(msg, key, None)


def _text(content: Any) -> str:
    """Text of a message content, whether string or multimodal parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            text = _get(part, "text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return ""


def _result_status(content: str) -> str:
    """ok / empty / error — decided by structure, not by words."""
    stripped = content.strip()
    if stripped in ("", "[]", "{}", "null"):
        return "empty"
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return "ok"
    if isinstance(parsed, dict) and (
            parsed.get("status") == "error" or parsed.get("error")):
        return "error"
    # A search that found nothing still answers "success": measured live,
    # {"status": "success", "files": [], "total_found": 0, ...} read as "ok",
    # and the judge never saw that every search came back empty. Every list
    # at the top level being empty is the structural form of "no result".
    if isinstance(parsed, dict):
        lists = [value for value in parsed.values() if isinstance(value, list)]
        if lists and not any(lists):
            return "empty"
    if isinstance(parsed, list) and not parsed:
        return "empty"
    return "ok"


def _fingerprint(arguments: Any) -> str:
    raw = arguments if isinstance(arguments, str) else json.dumps(
        arguments, sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{digest} ({len(raw)} chars)"


def build_excerpt(
    messages: List[Any],
    assistant: Optional[Dict[str, Any]],
    *,
    task_chars: int,
    spec_chars: int,
    reasoning_chars: int,
    max_tool_calls: int,
) -> Optional[Dict[str, Any]]:
    """The bounded view of a run, or None when the task cannot be seen.

    None means "no call": a judge that does not know the assignment can only
    guess, and the concept's rule for that case is ``continue``.
    """
    history = list(messages or [])
    last = history[-1] if history else None
    already_there = (last is not None and _get(last, "role") == "assistant"
                     and _get(last, "content") == _get(assistant or {}, "content")
                     and _get(last, "tool_calls") == _get(assistant or {}, "tool_calls"))
    if assistant and not already_there:
        history.append(assistant)

    system = next((m for m in history if _get(m, "role") == "system"), None)

    # The user messages, newest first into the budget. Taking only the FIRST
    # one was measured wrong live: in a follow-up request the judge was shown
    # the previous assignment and called the agent "drifted into unrelated
    # analyses" while it was answering the new question. The newest message
    # says what the agent works on now; older ones are context, and several in
    # a row (a follow-up, an injected note) all belong to the picture.
    #
    # A note the run added counts as one of them: it rides on `developer` since
    # the loop got that role, and "provide your final answer NOW, do NOT use
    # any tools" is exactly the instruction that explains the behaviour the
    # judge is about to score.
    user_messages: List[str] = []
    budget = task_chars
    for msg in reversed(history):
        if budget <= 0:
            break
        if _get(msg, "role") not in ("user", "developer"):
            continue
        text = _text(_get(msg, "content")).strip()
        if text:
            user_messages.append(text[:budget])
            budget -= len(user_messages[-1])
    user_messages.reverse()
    if not user_messages:
        return None

    # ponytail: the tail of the system prompt stands in for "the section that
    # describes the deliverable" — output contracts usually close a prompt.
    # A section finder belongs here once Etappe 1 shows the tail misses it.
    spec = _text(_get(system, "content"))[-spec_chars:] if system else ""

    thinking: List[str] = []
    budget = reasoning_chars
    for msg in reversed(history):
        if budget <= 0:
            break
        if _get(msg, "role") != "assistant":
            continue
        text = thinking_text(msg) or _text(_get(msg, "content"))
        if text.strip():
            thinking.append(text[-budget:])
            budget -= len(thinking[-1])
    thinking.reverse()

    results = {}
    for msg in history:
        if _get(msg, "role") == "tool" and _get(msg, "tool_call_id"):
            results[_get(msg, "tool_call_id")] = _text(_get(msg, "content"))

    calls: List[Dict[str, Any]] = []
    for msg in history:
        if _get(msg, "role") != "assistant":
            continue
        for call in _get(msg, "tool_calls") or []:
            function = _get(call, "function") or {}
            call_id = _get(call, "id")
            result = results.get(call_id)
            calls.append({
                "tool": _get(function, "name") or "?",
                "arguments": _fingerprint(_get(function, "arguments") or ""),
                "result": "pending" if result is None else _result_status(result),
                "result_chars": 0 if result is None else len(result),
            })

    return {
        "user_messages": user_messages,
        "expected_result": spec,
        "recent_thinking": "\n\n---\n\n".join(thinking),
        "recent_tool_calls": calls[-max_tool_calls:],
    }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_verdict(raw: str, excerpt: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read the judge's answer. Everything doubtful reads as ``continue``.

    Returns (verdict record, fail_open_reason). The reason is None when the
    answer was used as given; otherwise it names why it was not, so a judge
    that is systematically broken is visible in the log instead of looking
    like a judge that approves of everything.
    """
    fallback = {"verdict": "continue", "reason": "", "evidence": "", "message": ""}
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return fallback, "unparseable"
    if not isinstance(data, dict):
        return fallback, "not_an_object"

    record = {
        "verdict": str(data.get("verdict") or "").strip().lower(),
        "reason": str(data.get("reason") or "").strip(),
        "evidence": str(data.get("evidence") or "").strip(),
        "message": str(data.get("message") or "").strip(),
    }
    if record["verdict"] not in VERDICTS:
        return {**fallback, "reason": record["reason"]}, "unknown_verdict"
    if not record["evidence"]:
        return {**record, "verdict": "continue"}, "no_evidence"
    # The quote has to be real. A judge that paraphrases can put words in the
    # agent's mouth; the check is what the house already relies on elsewhere.
    haystack = _normalize(" ".join([
        *excerpt.get("user_messages", []), excerpt.get("expected_result", ""),
        excerpt.get("recent_thinking", ""),
        json.dumps(excerpt.get("recent_tool_calls", []), ensure_ascii=False),
    ]))
    # An ellipsis at either end marks a cut, not words of the excerpt — measured
    # live: a word-for-word quote ending in "..." was discarded as not verbatim.
    quote = record["evidence"].strip().strip(".…").strip()
    if not quote or _normalize(quote) not in haystack:
        return {**record, "verdict": "continue"}, "evidence_not_verbatim"
    if record["verdict"] != "continue" and not record["message"]:
        return {**record, "verdict": "continue"}, "no_message"
    return record, None
