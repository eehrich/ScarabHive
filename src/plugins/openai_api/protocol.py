"""The OpenAI wire formats: what a Responses or Chat Completions request asks, and the answers' shapes.

A request becomes one agent turn: the last user message is the turn, the
messages before it are earlier turns of the conversation, and system/developer
messages (and the Responses ``instructions``) reach the agent as a marked block
in front of the turn -- the agent keeps its own system prompt. Only text is
taken; anything else is refused, not dropped (``ApiError``).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

INSTRUCTIONS_HEADER = "Instructions from the client application:"


class ApiError(Exception):
    """An OpenAI-shaped error: ``{"error": {message, type, param, code}}`` with an HTTP status."""

    def __init__(self, status: int, message: str, *, type_: str = "invalid_request_error",
                 param: Optional[str] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status, self.message, self.type, self.param, self.code = status, message, type_, param, code

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.type, "param": self.param, "code": self.code}}


@dataclass
class Turn:
    message: str
    history: list[Any] = field(default_factory=list)  # ChatMessage, earlier turns in order
    title: str = ""  # the conversation's first user text -- without the instructions in front of the turn


def _text(content: Any, where: str) -> str:
    """The text of a message content: a string, or text parts; any other part is refused."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ApiError(400, f"{where}: content must be a string or a list of text parts", param=where)
    texts = []
    for index, part in enumerate(content):
        kind = part.get("type") if isinstance(part, dict) else None
        if kind in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
            texts.append(part["text"])
        else:
            raise ApiError(400, f"{where}[{index}]: only text parts are supported, not {kind!r}",
                           param=f"{where}[{index}]")
    return "".join(texts)


def turn_from_messages(messages: Any, where: str, instructions: Optional[str] = None) -> Turn:
    """Messages (Chat Completions ``messages``, or Responses ``input`` items) as one turn."""
    from agent_system.llm.models import ChatMessage

    if not isinstance(messages, list) or not messages:
        raise ApiError(400, f"{where} must be a non-empty list of messages", param=where)
    notes = [instructions] if instructions else []
    history: list[Any] = []
    last_user: Optional[str] = None
    first_user: Optional[str] = None
    for index, item in enumerate(messages):
        if not isinstance(item, dict) or item.get("type", "message") != "message":
            kind = item.get("type") if isinstance(item, dict) else type(item).__name__
            raise ApiError(400, f"{where}[{index}]: only messages are supported, not {kind!r} "
                                "(the agent calls its own tools)", param=f"{where}[{index}]")
        role = item.get("role")
        text = _text(item.get("content"), f"{where}[{index}].content")
        if role in ("system", "developer"):
            notes.append(text)
        elif role in ("user", "assistant"):
            if last_user is not None:
                history.append(ChatMessage(role="user", content=last_user))
                last_user = None
            if role == "user":
                last_user = text
                first_user = text if first_user is None else first_user
            else:
                history.append(ChatMessage(role="assistant", content=text))
        else:
            raise ApiError(400, f"{where}[{index}].role: {role!r} is not supported "
                                "(user, assistant, system, developer)", param=f"{where}[{index}].role")
    if last_user is None:
        raise ApiError(400, f"{where}: the last message must come from the user", param=where)
    notes = [note for note in notes if note.strip()]
    message = f"{INSTRUCTIONS_HEADER}\n" + "\n\n".join(notes) + "\n\n" + last_user if notes else last_user
    return Turn(message=message, history=history, title=first_user or "")


def refuse_client_tools(body: dict[str, Any]) -> None:
    if body.get("tools"):
        raise ApiError(400, "tools: not supported -- the agent brings its own tools", param="tools")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now() -> int:
    return int(time.time())


def sse(data: dict[str, Any], event: Optional[str] = None) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------------ models

def model_list(names: list[str]) -> dict[str, Any]:
    return {"object": "list",
            "data": [{"id": name, "object": "model", "created": 0, "owned_by": "scarabhive"} for name in names]}


# ------------------------------------------------------------------ chat completions

def chat_usage(usage: dict[str, int]) -> dict[str, int]:
    return {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"]}


def chat_completion(completion_id: str, model: str, text: str, usage: dict[str, int]) -> dict[str, Any]:
    return {"id": completion_id, "object": "chat.completion", "created": now(), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop", "logprobs": None}],
            "usage": chat_usage(usage)}


def chat_chunk(completion_id: str, model: str, delta: dict[str, Any], finish: Optional[str] = None) -> dict[str, Any]:
    return {"id": completion_id, "object": "chat.completion.chunk", "created": now(), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}]}


# ------------------------------------------------------------------ responses

def response_usage(usage: dict[str, int]) -> dict[str, Any]:
    return {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"],
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


def output_message(message_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    return {"type": "message", "id": message_id, "status": status, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}] if status == "completed" else []}


def response_object(response_id: str, model: str, *, status: str, created: int, store: bool,
                    previous: Optional[str], output: list[dict[str, Any]], usage: Optional[dict[str, int]] = None,
                    error: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"id": response_id, "object": "response", "created_at": created, "status": status, "model": model,
            "output": output, "previous_response_id": previous, "store": store, "error": error,
            "incomplete_details": None, "instructions": None, "metadata": {}, "parallel_tool_calls": False,
            "tool_choice": "auto", "tools": [], "temperature": None, "top_p": None,
            "usage": response_usage(usage) if usage is not None else None}
