"""ChatMessage <-> OpenAI Realtime API (GA) conversion.

A request is ONE ``response.create`` with ``conversation: "none"``: the whole
history travels as ``input`` items, so no item has to be created and
confirmed one by one, and nothing of an earlier call lingers on the server.
The result is read from ``response.done`` -- its ``output`` holds every item
complete (text, tool name, arguments); the streamed deltas are only for the
live view.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Callable, Optional

from agent_system.llm.models import ChatMessage
from plugins_llm.llm_common import openai_utils

logger = logging.getLogger(__name__)

#: The Realtime models cap a response here (gpt-realtime, gpt-realtime-mini).
MAX_OUTPUT_TOKENS = 4096

#: Stands in for an audio part: this path sends no audio input.
AUDIO_NOTE = "[Audio attachment - audio input not supported by this model]"


def _content_parts(content: Any) -> list[dict[str, Any]]:
    """Chat Completions content parts ({type: text | image_url})."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    items = [c.model_dump(mode="json", exclude_none=True) if hasattr(c, "model_dump") else c
             for c in content]
    parts = openai_utils.normalize_content_list(items)
    if any(isinstance(i, dict) and i.get("type") in ("audio", "input_audio") for i in items):
        # normalize_content_list drops it; the model is told, instead of
        # answering as if nothing had been attached.
        logger.warning("Realtime: audio input is not sent, the model gets a note")
        parts.append({"type": "text", "text": AUDIO_NOTE})
    return parts


def _text(content: Any) -> str:
    return "\n".join(p.get("text", "") for p in _content_parts(content) if p.get("type") == "text")


def _message_item(role: str, content: Any) -> dict[str, Any] | None:
    parts = []
    for part in _content_parts(content):
        if part.get("type") == "text":
            parts.append({"type": "output_text" if role == "assistant" else "input_text",
                          "text": part.get("text", "")})
        elif part.get("type") == "image_url" and role == "user":
            url = part.get("image_url")
            parts.append({"type": "input_image", "image_url": url.get("url") if isinstance(url, dict) else url})
        else:
            logger.warning("Realtime: %s content part %r is not sent", role, part.get("type"))
    if not parts:
        return None
    return {"type": "message", "role": role, "content": parts}


def to_request_input(
    messages: list[ChatMessage],
    attachment: Optional[Callable[[ChatMessage], Optional[dict]]] = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """The instructions and the input items for ``response.create``.

    A leading system message becomes the instructions; any later one stays a
    system item at its place in the history. ``attachment`` turns a tool
    message's ``multimodal_content`` into a user message (the client's
    ``_create_multimodal_injection``), sent right after the tool output.
    """
    instructions: str | None = None
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system" and instructions is None and not items:
            instructions = _text(msg.content) or None
            continue
        if msg.role == "tool":
            output = msg.content if isinstance(msg.content, str) else _text(msg.content)
            items.append({"type": "function_call_output", "call_id": msg.tool_call_id, "output": output or ""})
            injection = attachment(msg) if attachment and msg.multimodal_content else None
            item = _message_item("user", injection.get("content")) if injection else None
            if item:
                items.append(item)
            continue
        item = _message_item(msg.role, msg.content)
        if item:
            items.append(item)
        if msg.role == "assistant":
            for call in msg.tool_calls or []:
                func = call.get("function", {})
                arguments = func.get("arguments", "{}")
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id"),
                    "name": func.get("name"),
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                })
    return instructions, items


def to_realtime_tools(tools: list[dict]) -> list[dict]:
    """Chat Completions tools -> the Realtime API's flat form.

    Chat Completions: {"type": "function", "function": {"name", "description", "parameters"}}
    Realtime API:     {"type": "function", "name", "description", "parameters"}
    """
    realtime_tools = []
    for tool in tools:
        if tool.get("type") != "function" or "function" not in tool:
            logger.warning("Realtime: skipping tool schema that is not a function: %s", tool)
            continue
        func = tool["function"]
        realtime_tool = {"type": "function", "name": func.get("name"),
                         "description": func.get("description", "")}
        if "parameters" in func:
            params = copy.deepcopy(func["parameters"])
            params.pop("additionalProperties", None)
            realtime_tool["parameters"] = params
        realtime_tools.append(realtime_tool)
    return realtime_tools


def to_assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    """The assistant message of a finished response (``response.done``)."""
    text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            text.extend(part.get("text") or part.get("transcript") or ""
                        for part in item.get("content") or []
                        if part.get("type") in ("output_text", "output_audio"))
        elif item.get("type") == "function_call":
            tool_calls.append({"id": item.get("call_id"), "type": "function",
                               "function": {"name": item.get("name"),
                                            "arguments": item.get("arguments") or "{}"}})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def to_openai_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Realtime usage in the Chat Completions shape the cost layer reads."""
    if not usage:
        return None
    details = usage.get("input_token_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens_details": {"cached_tokens": details.get("cached_tokens", 0)},
    }


def outcome(response: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """(finish_reason, error) of a finished response.

    A response cut at ``max_output_tokens`` or by the content filter keeps
    what it produced and says why, as Chat Completions does ("length",
    "content_filter") -- the agent loop decides what that means. Anything
    else that did not complete is an error in the shape the server reads.
    """
    status = response.get("status")
    if status in (None, "completed"):
        return None, None
    details = response.get("status_details") or {}
    reason = details.get("reason")
    if status == "incomplete" and reason == "max_output_tokens":
        return "length", None
    if status == "incomplete" and reason == "content_filter":
        return "content_filter", None
    error = details.get("error") or {}
    return None, {"error": True,
                  "type": error.get("type") or f"realtime_{status}",
                  "message": f"realtime response {status}: {error.get('code') or reason or details}"}
