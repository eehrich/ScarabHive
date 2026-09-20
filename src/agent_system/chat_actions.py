"""What a chat command DOES to a conversation — the same on every surface.

``chat_commands`` owns what exists and how a typed line is parsed, and each
surface owns how it talks to its user. In between sit two things that are the
same everywhere and were about to exist twice: where the last exchange of a
conversation ends, and what a conversation looks like written out.

Both read the two shapes a message comes in — the ``ChatMessage`` an agent
holds in memory and the plain dict a session file stores — because that is
exactly where the surfaces differ. The terminal cuts the agent's own message
list and lets the next save follow; the browser reloads the record from disk
on every message, so there the record IS the conversation.

Nothing here prints, writes or awaits: the caller decides what to do with the
answer, which is what keeps one copy serving a REPL and an HTTP endpoint.
(`measured_context` reads a plugin's store, which is still a read -- it asks
nobody to wait and changes nothing.)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

from .llm.message_roles import opens_a_turn

logger = logging.getLogger(__name__)

__all__ = [
    "message_role", "message_text", "tool_calls_of", "tool_call_summary",
    "one_line", "starts_a_turn", "split_off_last_exchange",
    "transcript_markdown", "context_breakdown", "measured_context",
]

#: What a conversation is made of, and the role each kind arrives under.
CONTEXT_KINDS: tuple[tuple[str, str], ...] = (
    ("questions", "user"),
    ("answers", "assistant"),
    ("tool_results", "tool"),
    # What the run told the model, as opposed to what a person asked.
    ("notes", "developer"),
)

#: Groups that are left out of a breakdown when they are empty.
_OPTIONAL_KINDS = ("other", "notes")


def _field(message: Any, name: str) -> Any:
    """*name* of a message, whichever shape it arrived in."""
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def message_role(message: Any) -> str:
    """"user", "assistant", "tool" -- or "?" for something unreadable."""
    role = _field(message, "role")
    return role if isinstance(role, str) else "?"


def message_text(message: Any) -> str:
    """Readable text of a message whose content may be multimodal.

    A part with no text of its own is named by its type ("[image_url]"), so
    that a question asked with a picture reads as a question and not as a
    blank line. What goes BACK to a model is the message itself, never this.
    """
    content = _field(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _field(item, "text")
            parts.append(text or f"[{_field(item, 'type') or 'part'}]")
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def tool_calls_of(message: Any) -> list:
    """The tool calls a message carries, or an empty list."""
    calls = _field(message, "tool_calls")
    return list(calls) if calls else []


def tool_call_summary(call: Any) -> tuple[str, Any]:
    """(name, arguments) of a tool call in either dict shape."""
    if not isinstance(call, dict):
        return "?", None
    fn = call.get("function") or {}
    # `or` rather than a get-default: an explicit null would slip through.
    name = fn.get("name") or call.get("name") or "?"
    return str(name), fn.get("arguments") or call.get("arguments")


def one_line(value: Any, limit: int = 60) -> str:
    """Compact single-line form of a tool argument or result value."""
    # default=str: a datetime or a Path in a tool result would otherwise
    # raise here, in a formatter whose whole job is to never be the reason
    # something failed.
    text = (value if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def starts_a_turn(message: Any) -> bool:
    """A message that OPENED a turn -- something a person sent, or a wake.

    Every stored user message went to the agent: one that opens with a command
    word was sent escaped ("//help me ..."), so hiding it left the answer in
    the history without its question.

    A woken run's task is a `developer` message rather than a `user` one
    (cli_utils/agent_runner.wake_message): the RUN is speaking, but it opened
    its turn exactly as a typed line does, and /undo, /retry and the browser's
    drop-last-exchange all cut HERE. On the role alone they would walk past the
    woken exchange to the person's previous question and drop both.

    The loop's own notes are `developer` too. They carry `injected_by` and
    stand INSIDE a turn, not at its head -- and a session drops them on the way
    to disk anyway (servers/agent/components/session_tracking).
    """
    return opens_a_turn(message) and bool(message_text(message).strip())


def split_off_last_exchange(
    messages: Sequence[Any],
) -> tuple[list, Optional[Any]]:
    """(what stays, the question that was asked) -- or (everything, None).

    The last question and EVERYTHING that answered it: the assistant's text,
    its tool calls and their results all belong to the turn the question
    started, and leaving half of it behind would ask the next turn to
    continue an answer nobody can see any more.

    The question is handed back whole, not as text: asked with an image, it
    is a multimodal message, and a retry has to send those parts again.
    """
    for index in range(len(messages) - 1, -1, -1):
        if starts_a_turn(messages[index]):
            return list(messages[:index]), messages[index]
    return list(messages), None


def measured_context(agent: Any, session_id: str) -> dict:
    """What the PROVIDER counted on the last call of this session.

    From context_usage_tracker, which hooks every LLM call in the process --
    so it also sees the calls a sub-agent made, which never appear in the
    coordinator's own event stream.

    Empty when the plugin is not registered: an estimate is still worth
    showing, a made-up measurement is not. `is_stale` is the tracker's own
    word for "the context was rewritten since" -- the number then describes a
    conversation that no longer exists.
    """
    registry = getattr(agent, "registry", None)
    if registry is None:
        return {}
    try:
        tracker = getattr(registry.get("context_usage_tracker"), "tracker", None)
        latest = tracker.get_latest(session_id=session_id) if tracker else None
    except Exception:
        logger.debug("No usage snapshot for %s", session_id, exc_info=True)
        return {}
    if not latest:
        return {}
    return {
        "window": int(latest.get("context_window") or 0),
        "prompt_tokens": int(latest.get("prompt_tokens") or 0),
        "cached": int(latest.get("cached_tokens") or 0),
        "is_stale": bool(latest.get("is_stale")),
    }


def live_context_window(agent: Any, llm_override: Any = None) -> int:
    """The window the NEXT call runs against, or 0 when nobody knows it.

    The client this chat is on, not the one the LAST call was counted against:
    a /model switch or a fallback changes the model and with it the size, and
    a share worked out against the old one states a fill that is not true.
    """
    client = llm_override or getattr(agent, "llm", None)
    window = getattr(client, "context_window", None)
    # > 0 is a guard, not a case anyone produces: a configured 0 reads the
    # same either way, and a negative one would render a negative share
    # rather than no share at all.
    return window if isinstance(window, int) and window > 0 else 0


def context_breakdown(
    messages: Sequence[Any],
    *,
    system_prompt: str = "",
    tools: Optional[Sequence[Any]] = None,
) -> dict:
    """Where the context window goes, in ESTIMATED tokens, by kind.

    What a provider really counted is known only after a call, and only as one
    number. The split is what answers "why is my window full" -- and in a long
    session the answer is almost always the tool RESULTS, which nothing else
    says out loud.

    Estimate and measurement stay apart: a category worked out as "measured
    minus estimated" looks exact and carries the error of both. Whoever
    renders this prints the provider's number next to it, not inside it.

    Tools are the SCHEMAS the model is handed; the system prompt is the
    rendered one, which already contains the tool prompt. Both sit in the
    window on every single call, which is why they are their own lines.
    """
    from .llm.token_utils import estimate_token_count

    groups: dict[str, list] = {name: [] for name, _ in CONTEXT_KINDS}
    groups["other"] = []
    for message in messages:
        role = message_role(message)
        name = next((n for n, r in CONTEXT_KINDS if r == role), "other")
        groups[name].append(message)

    parts = {name: {"tokens": estimate_token_count(group), "count": len(group)}
             for name, group in groups.items()}
    parts["system_prompt"] = {
        "tokens": (estimate_token_count([{"role": "system", "content": system_prompt}])
                   if system_prompt else 0),
        "count": 1 if system_prompt else 0,
    }
    tool_list = list(tools or [])
    parts["tools"] = {
        "tokens": estimate_token_count([], tools=tool_list) if tool_list else 0,
        "count": len(tool_list),
    }
    # These only when there is something: a line reading 0 invites the
    # question what it is, and the answer is usually "nothing".
    for optional in _OPTIONAL_KINDS:
        if not parts[optional]["count"]:
            del parts[optional]
    return {"parts": parts, "total": sum(p["tokens"] for p in parts.values())}


def transcript_markdown(
    messages: Sequence[Any],
    *,
    agent_name: str,
    session_id: str,
    llm: str = "",
) -> str:
    """The conversation as markdown: what was asked, what came back, what ran.

    Tool traffic is condensed to one line each -- a transcript is for reading,
    and a single file_ops_read result can be longer than the conversation
    around it.
    """
    lines = [f"# Chat with {agent_name}", "",
             f"Session `{session_id}`" + (f" -- LLM: {llm}" if llm else ""), ""]
    for message in messages:
        role = message_role(message)
        text = message_text(message).strip()
        if role == "user":
            lines += ["## You", "", text, ""]
        elif role == "assistant":
            if text:
                lines += ["## Agent", "", text, ""]
            for call in tool_calls_of(message):
                name, arguments = tool_call_summary(call)
                lines.append(f"- tool `{name}` {one_line(arguments, 120)}")
        elif role == "developer":
            # Not a turn anybody took: the run putting something in front of
            # the model. A transcript that hides it reads as if the agent knew
            # things nobody told it.
            lines += ["## Note from the run", "", text, ""]
        elif role == "tool":
            lines.append(f"  -> {one_line(text, 120)}")
    return "\n".join(lines) + "\n"
