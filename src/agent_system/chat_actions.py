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
"""
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

__all__ = [
    "message_role", "message_text", "tool_calls_of", "tool_call_summary",
    "one_line", "starts_a_turn", "split_off_last_exchange",
    "transcript_markdown",
]


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
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def starts_a_turn(message: Any) -> bool:
    """A user message with something in it.

    Every stored user message went to the agent: one that opens with a command
    word was sent escaped ("//help me ..."), so hiding it left the answer in
    the history without its question.
    """
    return message_role(message) == "user" and bool(message_text(message).strip())


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
        elif role == "tool":
            lines.append(f"  -> {one_line(text, 120)}")
    return "\n".join(lines) + "\n"
