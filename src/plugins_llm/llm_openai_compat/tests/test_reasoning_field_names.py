"""One payload, several names — the thinking text must survive all of them.

Providers disagree on where a model's thinking sits in the answer:

    DeepSeek (direct)   message.reasoning_content
    OpenRouter (chat)   message.reasoning, and reasoning_details[].text
    Responses route     output[].type == "reasoning" -> summary[] or content[]

Reading only the first name lost the text everywhere else. Measured before the
fix: of 4.000 stored chat answers ``reasoning_content`` was filled in NONE
while the text was present in 52; of 2.345 stored reasoning items on the
Responses route ``summary`` was empty in ALL of them while ``content[].text``
carried up to 294.763 characters. The Writer runs on that second route.

These tests pin every name, so a future client that learns a new one cannot
quietly drop an old one.

They also pin the second half of the rule: the text is stored EXACTLY ONCE.
Where the provider's artifacts have to be kept verbatim anyway, the thinking
stays inside them and the message gets no copy — reading goes through
``reasoning_artifacts.thinking_text``, which knows both homes.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.models import ChatMessage
from agent_system.utils.reasoning_artifacts import thinking_text
from plugins_llm.llm_openai_compat.httpx_client import HTTPXOpenAIClient
from plugins_llm.llm_openai_compat.openai_responses_client import (
    OpenAIResponsesClient,
)


def _httpx_client() -> HTTPXOpenAIClient:
    return HTTPXOpenAIClient(model="gpt-3.5-turbo", api_key="test-key",
                             max_retries=1, retry_backoff=0.01)


def _responses_client() -> OpenAIResponsesClient:
    return OpenAIResponsesClient(model="openai/gpt-5.6-terra", api_key="sk-or-test",
                                 base_url="https://openrouter.ai/api/v1")


def _chat_body(message: dict) -> dict:
    return {"choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


# --- the helper that reads thinking out of reasoning_details ---------------

def test_reasoning_text_from_details_joins_text_blocks():
    """Only blocks that actually carry text contribute, in index order.

    Blocks are finished sections, so they are joined like paragraphs. No
    fixture value ends in a space: a trailing space would hide a missing
    separator, which is how the glued variant survived unnoticed.
    """
    text = HTTPXOpenAIClient._reasoning_text_from_details([
        {"type": "reasoning.text", "text": "First I check the file."},
        {"type": "reasoning.encrypted", "data": "OPAQUE"},   # no text
        {"type": "reasoning.text", "text": "Then I weigh it."},
        {"type": "reasoning.text", "text": "   "},           # blank
    ])
    assert text == "First I check the file.\n\nThen I weigh it."


def test_reasoning_text_from_details_accepts_the_stream_accumulator():
    """The streaming path holds blocks as {index: block} — order by index."""
    text = HTTPXOpenAIClient._reasoning_text_from_details({
        1: {"type": "reasoning.text", "text": "Second."},
        0: {"type": "reasoning.text", "text": "First."},
    })
    assert text == "First.\n\nSecond."


def test_reasoning_text_from_details_is_empty_without_text():
    assert HTTPXOpenAIClient._reasoning_text_from_details(None) == ""
    assert HTTPXOpenAIClient._reasoning_text_from_details([]) == ""
    assert HTTPXOpenAIClient._reasoning_text_from_details(
        [{"type": "reasoning.encrypted", "data": "X"}]) == ""


# --- streamed fragments of one block ---------------------------------------

def test_streamed_text_fragments_are_concatenated_not_overwritten():
    """``text`` arrives in fragments like ``data`` does.

    Overwriting kept only the last fragment, which still looks like a valid
    (just very short) thought — the failure was invisible.
    """
    accumulated: dict[int, dict] = {}
    for fragment in ("The plan ", "is to ", "read the file first."):
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            accumulated, {"index": 0, "type": "reasoning.text", "text": fragment})
    assert accumulated[0]["text"] == "The plan is to read the file first."


def test_streamed_encrypted_fragments_still_concatenate():
    """Regression guard: the pre-existing ``data`` behaviour is unchanged."""
    accumulated: dict[int, dict] = {}
    for fragment in ("AAA", "BBB"):
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            accumulated, {"index": 0, "type": "reasoning.encrypted", "data": fragment})
    assert accumulated[0]["data"] == "AAABBB"


def test_streamed_summary_fragments_are_concatenated_too():
    """A ``reasoning.summary`` block fragments like the other two.

    Which field carries the payload depends on the block type, so the
    concatenation cannot be tied to one name.
    """
    accumulated: dict[int, dict] = {}
    for fragment in ("In short: ", "read, then write."):
        HTTPXOpenAIClient._accumulate_reasoning_detail(
            accumulated,
            {"index": 0, "type": "reasoning.summary", "summary": fragment})
    assert accumulated[0]["summary"] == "In short: read, then write."


# --- the block types disagree on WHERE the text sits ------------------------

def test_reasoning_text_from_details_reads_the_summary_field():
    """``reasoning.summary`` carries its text in ``summary``, not in ``text``.

    Documented per block type: reasoning.text -> text, reasoning.summary ->
    summary, reasoning.encrypted -> data. Reading only ``text`` dropped every
    summary block — the shape Anthropic and Gemini produce over OpenRouter.
    """
    text = HTTPXOpenAIClient._reasoning_text_from_details([
        {"type": "reasoning.summary", "summary": "weighed two options"},
    ])
    assert text == "weighed two options"


def test_chat_summary_block_is_readable_where_it_lies():
    """The same, end to end through the chat route."""
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done",
         "reasoning_details": [{"type": "reasoning.summary",
                                "summary": "condensed thinking"}]}))
    assert "reasoning_content" not in result["assistant"]
    assert thinking_text(result["assistant"]) == "condensed thinking"


# --- chat completions route -------------------------------------------------

def test_chat_reads_openrouter_reasoning_field():
    """OpenRouter names it ``reasoning`` — the field that was never read."""
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done",
         "reasoning": "I should check the documentation first."}))
    assert result["assistant"]["reasoning_content"] == \
        "I should check the documentation first."


def test_chat_thinking_inside_details_stays_there():
    """One home, not two: the artifact keeps the text, the message does not.

    The blocks have to survive verbatim for the replay anyway, so a copy on
    the message would store the same thought a second time.
    """
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done",
         "reasoning_details": [{"type": "reasoning.text", "text": "thinking hard"}]}))
    assert "reasoning_content" not in result["assistant"]
    assert thinking_text(result["assistant"]) == "thinking hard"


def test_chat_stores_the_thinking_once_when_both_names_arrive():
    """OpenRouter sends the SAME text twice — only one copy may be stored."""
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done",
         "reasoning": "weighed the options",
         "reasoning_details": [{"type": "reasoning.text",
                                "text": "weighed the options"}]}))
    assert "reasoning_content" not in result["assistant"]
    assert thinking_text(result["assistant"]) == "weighed the options"


def test_deepseek_roundtrip_sends_the_text_from_wherever_it_lives():
    """DeepSeek needs the thinking BACK on every assistant message with tools.

    An empty string satisfies the schema and loses the chain of thought — the
    documented failure mode (reasoning grew from 103 to 65.536 tokens over a
    run because the model re-derived it every turn). Storing the text once
    means it can sit in the artifacts, so the payload has to look there.
    """
    client = HTTPXOpenAIClient(model="deepseek-v4-pro", api_key="test-key")
    msgs = [{"role": "assistant", "content": "done",
             "reasoning_details": [{"type": "reasoning.text",
                                    "text": "why I did it"}]}]
    client._postprocess_messages_for_provider(msgs)
    assert msgs[0]["reasoning_content"] == "why I did it"


def test_deepseek_roundtrip_keeps_the_field_present_without_thinking():
    """No thinking anywhere: the key must still be there, or DeepSeek 400s."""
    client = HTTPXOpenAIClient(model="deepseek-v4-pro", api_key="test-key")
    msgs = [{"role": "assistant", "content": "done"}]
    client._postprocess_messages_for_provider(msgs)
    assert msgs[0]["reasoning_content"] == ""


def test_chat_keeps_the_text_when_the_artifact_is_encrypted_only():
    """An encrypted block is no home for readable text — the message keeps it."""
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done",
         "reasoning": "weighed the options",
         "reasoning_details": [{"type": "reasoning.encrypted", "data": "OPAQUE"}]}))
    assert result["assistant"]["reasoning_content"] == "weighed the options"


def test_chat_prefers_deepseeks_own_field():
    """DeepSeek direct keeps working: its name wins over the alternatives."""
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done",
         "reasoning_content": "deepseek text",
         "reasoning": "openrouter text"}))
    assert result["assistant"]["reasoning_content"] == "deepseek text"


def test_chat_without_any_thinking_sets_no_field():
    result = _httpx_client()._format_response(_chat_body(
        {"role": "assistant", "content": "done"}))
    assert "reasoning_content" not in result["assistant"]


# --- responses route (the one the Writer runs on) ---------------------------

def test_responses_reads_raw_thinking_from_content():
    """``summary`` was empty in all 2.345 measured items; the text sat here."""
    result = _responses_client()._format_response({"output": [
        {"type": "reasoning", "id": "rs_1", "status": "completed",
         "summary": [],
         "content": [{"type": "reasoning_text", "text": "weighing the options"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
    ]})
    assert "reasoning_content" not in result["assistant"]
    assert thinking_text(result["assistant"]) == "weighing the options"
    assert result["assistant"]["content"] == "answer"


def test_responses_still_reads_the_openai_summary():
    """Regression guard: the OpenAI family only ever sends a summary."""
    result = _responses_client()._format_response({"output": [
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text",
                                                         "text": "short form"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
    ]})
    assert "reasoning_content" not in result["assistant"]
    assert thinking_text(result["assistant"]) == "short form"


def test_responses_encrypted_only_item_yields_no_thinking_text():
    """An encrypted item carries no readable thinking — and must not invent one."""
    result = _responses_client()._format_response({"output": [
        {"type": "reasoning", "id": "rs_1", "summary": [],
         "encrypted_content": "OPAQUE-BLOB"},
        {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
    ]})
    assert "reasoning_content" not in result["assistant"]
    # The absence alone would also hold if the reader returned "" for
    # everything, so pin what it actually says — and that the opaque blob
    # never leaks into the thinking.
    assert thinking_text(result["assistant"]) == ""
    assert result["assistant"]["content"] == "answer"


# --- the streaming path -----------------------------------------------------
#
# Five of the changed sites live in the streaming loop, and nothing in the repo
# exercised it with reasoning: each of the three stream-end sites could have
# been reverted on its own while every other test stayed green. The three are
# reached by how the stream ENDS — with ``[DONE]``, without a terminator, and
# with a last line that never got its newline.

def _sse(*lines: str, terminated: bool = True) -> bytes:
    body = "\n".join(lines)
    return (body + "\n").encode("utf-8") if terminated else body.encode("utf-8")


async def _stream_chunks(payload: bytes) -> list:
    response = MagicMock()

    async def aiter_bytes():
        yield payload

    response.aiter_bytes = aiter_bytes
    response.status_code = 200
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)

    client = _httpx_client()
    with patch("httpx.AsyncClient.stream", return_value=response):
        return [chunk async for chunk in client.chat_tools_streaming(
            [ChatMessage(role="user", content="hi")], [])]


@pytest.mark.asyncio
async def test_streaming_reads_openrouter_reasoning_deltas():
    """``delta.reasoning`` — the name that made every OpenRouter model look silent."""
    chunks = await _stream_chunks(_sse(
        'data: {"choices":[{"delta":{"reasoning":"first "}}]}',
        'data: {"choices":[{"delta":{"reasoning":"second"}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}]}',
        'data: [DONE]'))
    assert [c["delta"] for c in chunks if c["type"] == "thinking_delta"] == \
        ["first ", "second"]
    final = chunks[-1]
    assert final["type"] == "final"
    assert final["assistant"]["reasoning_content"] == "first second"


@pytest.mark.asyncio
async def test_streaming_stores_the_thinking_once():
    """Deltas AND the artifact carry it — the message must keep no copy."""
    chunks = await _stream_chunks(_sse(
        'data: {"choices":[{"delta":{"reasoning":"why I did it",'
        '"reasoning_details":[{"index":0,"type":"reasoning.text",'
        '"text":"why I did it"}]}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}]}',
        'data: [DONE]'))
    final = chunks[-1]
    assert "reasoning_content" not in final["assistant"]
    assert thinking_text(final["assistant"]) == "why I did it"


@pytest.mark.asyncio
async def test_streaming_without_terminator_keeps_the_thinking():
    """A stream that just stops: its own end site, same rule."""
    chunks = await _stream_chunks(_sse(
        'data: {"choices":[{"delta":{"reasoning_content":"deepseek thinking"}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}]}'))
    final = chunks[-1]
    assert final["type"] == "final"
    assert final["assistant"]["reasoning_content"] == "deepseek thinking"


@pytest.mark.asyncio
async def test_streaming_rest_buffer_keeps_the_thinking():
    """The last line never got its newline — the salvage path parses it."""
    chunks = await _stream_chunks(_sse(
        'data: {"choices":[{"delta":{"reasoning":"tail thinking"}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}]}',
        'data: [DONE]', terminated=False))
    final = chunks[-1]
    assert final["type"] == "final"
    assert final["assistant"]["reasoning_content"] == "tail thinking"
