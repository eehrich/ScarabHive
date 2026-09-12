"""The SDK client must keep the model's thinking, not discard it.

Gemini marks thinking parts with ``thought=True``. The SDK client separated
them from the answer correctly but then dropped them: they were collected for
the live view and never reached the assistant message, so Gemini runs
persisted no reasoning at all — no session, no debugger row, no later turn.

NOTE ON THE MOCK: parts are built as SimpleNamespace, never MagicMock. A
MagicMock answers ``hasattr`` for every name and is truthy, so ``part.thought``
would be true for EVERY part and the test would pass without measuring
anything.

And every part carries ``thought`` explicitly, ``None`` included: in the real
SDK it is a declared field (``thought: Optional[bool]``), so ``hasattr`` is
ALWAYS true there and only its truthiness decides. A part built without the
attribute would exercise a branch that does not exist in production — the
guard could then be shortened to a bare ``hasattr`` check and these tests
would stay green while every answer part turned into thinking.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins_llm.llm_gemini.gemini_sdk_client import GeminiSDKClient
from agent_system.llm.models import ChatMessage


@pytest.fixture
def sdk_client():
    client = GeminiSDKClient(
        model="gemini-2.5-flash",
        api_key="test-api-key",
        context_window=200000,
        request_timeout=180,
    )
    client._client = MagicMock()
    return client


def _response(parts):
    """A minimal SDK response object: candidates[0].content.parts."""
    return SimpleNamespace(
        candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=parts),
            finish_reason=None,
        )],
        usage_metadata=SimpleNamespace(
            prompt_token_count=5,
            candidates_token_count=3,
            total_token_count=8,
            cached_content_token_count=0,
        ),
    )


@pytest.mark.asyncio
async def test_thought_parts_are_kept_as_reasoning_content(sdk_client):
    """The thinking lands in reasoning_content, the answer stays clean."""
    sdk_client._client.aio.models.generate_content = AsyncMock(
        return_value=_response([
            SimpleNamespace(text="First I check the file.", thought=True),
            SimpleNamespace(text="Hello there!", thought=None),
        ]))

    result = await sdk_client.chat_tools([ChatMessage(role="user", content="Hi")], [])

    assert result["assistant"]["content"] == "Hello there!"
    assert result["assistant"]["reasoning_content"] == "First I check the file."


@pytest.mark.asyncio
async def test_answer_without_thinking_sets_no_reasoning_content(sdk_client):
    """No thought parts -> the field stays absent, not empty."""
    sdk_client._client.aio.models.generate_content = AsyncMock(
        return_value=_response([SimpleNamespace(text="Hello there!", thought=None)]))

    result = await sdk_client.chat_tools([ChatMessage(role="user", content="Hi")], [])

    assert result["assistant"]["content"] == "Hello there!"
    assert "reasoning_content" not in result["assistant"]
