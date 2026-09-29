"""The OpenAI family's wire shapes for structured output.

Three routes speak them: the SDK client (llm_openai), the httpx Chat Completions
client and the Responses client (llm_openai_compat) -- and every gateway behind
those (OpenRouter, a llama.cpp or vLLM host, Ollama's /v1). One place, so the
three cannot drift apart. What a ``ResponseFormat`` is, and when a client may
send one at all, is ``agent_system.llm.structured_output``.
"""
from __future__ import annotations

from typing import Any

from agent_system.llm.structured_output import JSON_OBJECT, ResponseFormat


def _named_schema(response_format: ResponseFormat) -> dict[str, Any]:
    """name, schema and -- where the caller set them -- strict and description."""
    shape: dict[str, Any] = {"name": response_format.name, "schema": response_format.schema}
    if response_format.strict is not None:
        shape["strict"] = response_format.strict
    if response_format.description is not None:
        shape["description"] = response_format.description
    return shape


def chat_completions_response_format(response_format: ResponseFormat) -> dict[str, Any]:
    """``response_format`` of a Chat Completions request."""
    if response_format.type == JSON_OBJECT:
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": _named_schema(response_format)}


def responses_text_format(response_format: ResponseFormat) -> dict[str, Any]:
    """``text.format`` of a Responses request: the same fields, one level flatter."""
    if response_format.type == JSON_OBJECT:
        return {"type": "json_object"}
    return {"type": "json_schema", **_named_schema(response_format)}
