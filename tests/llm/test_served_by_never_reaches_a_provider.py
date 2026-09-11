"""served_by is bookkeeping for the provider pin -- it must never reach a provider.

Strict providers reject unknown message keys (see
HTTPXOpenAIClient._sanitize_message_for_api), and a fallback chain can carry an
OpenRouter turn into any client. The paths differ: httpx and the OpenAI batch
writer filter by whitelist, the Responses client and Ollama rebuild messages
from named fields, the OpenAI SDK client pops private fields one by one. Each
path is checked here.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_system.llm.models import ChatMessage
from plugins_llm.llm_common import openai_utils
from plugins_llm.llm_ollama.ollama_client import OllamaNativeAsyncClient
from plugins_llm.llm_openai.openai_client import OpenAIAsyncClient
from plugins_llm.llm_openai_compat.httpx_client import HTTPXOpenAIClient
from plugins_llm.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

TURN = [ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="ok", served_by="Google AI Studio"),
        ChatMessage(role="user", content="next")]


def _assert_absent(messages):
    assert messages, "nothing was sent -- vacuous test"
    assert all("served_by" not in m for m in messages)


def test_httpx_whitelist_drops_it():
    clean = HTTPXOpenAIClient._sanitize_message_for_api(
        {"role": "assistant", "content": "ok", "served_by": "Google AI Studio"})
    assert "served_by" not in clean


def test_responses_items_do_not_carry_it():
    client = OpenAIResponsesClient(model="m", api_key="k",
                                   base_url="https://openrouter.ai/api/v1")
    items = client._messages_to_input(list(TURN))
    assert items and "served_by" not in str(items)


def test_ollama_drops_it():
    with patch("httpx.AsyncClient"):
        client = OllamaNativeAsyncClient(model="llama2")
        _assert_absent(client._map_messages(list(TURN)))


def test_openai_batch_body_drops_it():
    # batch_client dumps the whole ChatMessage (mode="json", None included).
    _assert_absent(openai_utils.normalize_messages([m.model_dump(mode="json") for m in TURN]))


@pytest.fixture
def openai_sdk():
    mock_class, mock_instance = MagicMock(), MagicMock()
    mock_class.return_value = mock_instance
    with patch("openai.AsyncOpenAI", mock_class):
        client = OpenAIAsyncClient(model="gpt-4", api_key="k",
                                   base_url="https://api.openai.com/v1")
        create = AsyncMock(side_effect=RuntimeError("stop after capture"))
        mock_instance.chat.completions.create = create
        yield client, create


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "chat_tools", "chat_tools_streaming"])
async def test_openai_sdk_drops_it(openai_sdk, path):
    client, create = openai_sdk
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    call = {
        "chat": lambda: client.chat(list(TURN)),
        "chat_tools": lambda: client.chat_tools(list(TURN), tools),
        "chat_tools_streaming": lambda: client.chat_tools_streaming(list(TURN), tools),
    }[path]
    try:
        result = call()
        if hasattr(result, "__anext__"):
            async for _ in result:
                pass
        else:
            await result
    except Exception:
        pass  # the mock stops the call right after the request was built
    assert create.call_args is not None, "create was never called -- vacuous test"
    _assert_absent(create.call_args.kwargs["messages"])
