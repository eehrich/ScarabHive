"""What a later call sends of an earlier turn's thinking -- the round trip the model config asks for.

Measured on the production book_launcher (message_debugger, 8 runs, 43 calls): with reasoning_details stripped
from older assistant messages, every step after the first rewrote ~26k tokens of prompt cache instead of reading
them. Anthropic reads its cache by matching the prefix byte for byte, and the message ahead of the write anchor
lost its blocks on the next call. Which round trip a model needs is declared per model, not guessed here.
"""
from __future__ import annotations

import pytest

from plugins_llm.llm_openai_compat.httpx_client import HTTPXOpenAIClient

OPENROUTER = "https://openrouter.ai/api/v1"


def client(model: str, base_url: str = OPENROUTER, **extra) -> HTTPXOpenAIClient:
    return HTTPXOpenAIClient(model=model, api_key="test-key", base_url=base_url, **extra)


def turns() -> list[dict]:
    """Two assistant turns of a tool chain, each with the thinking blocks the provider sent back."""
    return [
        {"role": "user", "content": "Prepare the next book."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "okf_read_concept", "arguments": "{}"}}],
         "reasoning_details": [{"type": "reasoning.text", "text": "Check the histogram first."}]},
        {"role": "tool", "tool_call_id": "c1", "content": "concept"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function",
                                                             "function": {"name": "writer_content_book", "arguments": "{}"}}],
         "reasoning_details": [{"type": "reasoning.text", "text": "Now the catalogue."}]},
        {"role": "tool", "tool_call_id": "c2", "content": "catalogue"},
    ]


def thinking_kept(messages: list[dict]) -> list[bool]:
    return [bool(message.get("reasoning_details")) for message in messages if message.get("role") == "assistant"]


@pytest.mark.parametrize("mode, expected", [
    ("keep_all", [True, True]),    # a byte-stable prefix: what an Anthropic cache reads back
    ("keep_last", [False, True]),  # only this turn's signature, as Gemini validates it
    ("strip", [False, False]),     # providers that refuse the field back
])
def test_the_model_config_decides_which_thinking_travels_back(mode, expected):
    messages = turns()

    client("some/model", reasoning_details_mode=mode)._postprocess_messages_for_provider(messages)

    assert thinking_kept(messages) == expected


def test_a_turn_whose_chain_was_broken_by_compaction_goes_without_its_thinking():
    """Sending half a chain is the 400 "encrypted content could not be verified" -- the orphan travels bare.

    A closed turn: the answer is written, nothing upstream waits for its thinking.
    """
    messages = turns()
    messages[1].pop("tool_calls")
    messages[1]["content"] = "The concept is ready."
    messages[1]["rd_orphaned"] = True

    client("some/model", reasoning_details_mode="keep_all")._postprocess_messages_for_provider(messages)

    assert thinking_kept(messages) == [False, True]
    assert not any("rd_orphaned" in message for message in messages), "the flag must never reach the provider"


def test_an_orphaned_turn_that_is_still_waiting_for_its_tools_keeps_its_thinking():
    """The one case where a broken chain must travel anyway.

    invalidate_reasoning_artifacts flags the MOST RECENT assistant message, and at a pre_llm_call
    hook that message is the OPEN tool-use turn. Anthropic wants that turn's thinking echoed back
    complete -- stripping it is the very 400 the orphan rule exists to avoid.
    """
    messages = turns()
    messages[3]["rd_orphaned"] = True  # the last assistant turn, tool_calls still open

    client("some/model", reasoning_details_mode="keep_all")._postprocess_messages_for_provider(messages)

    assert thinking_kept(messages) == [True, True]
    assert not any("rd_orphaned" in message for message in messages), "the flag must never reach the provider"
