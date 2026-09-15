"""finish_reason has to survive the trip from client to truncation guard.

The guard in the agent loop reads it off `llm_out` -- but nothing in between
carried it, so `if finish_reason == "length"` was dead code and a truncated
answer was accepted as an empty one (which then triggers a 'Continue' nudge and
replays the same runaway).

Scope: httpx_client, the gemini clients and -- since 2026-09-01 --
openai_responses report finish_reason (anthropic and ollama still do not),
so the guard is live for those providers. The transport is provider-agnostic
regardless.
"""
import pytest
from unittest.mock import AsyncMock

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    MCPConfig,
)
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent


def _llm_system():
    return LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4",
                                        api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )


def _agent(max_steps=1):
    agent_config = AgentConfig(max_steps=max_steps)
    system_config = AgentSystemConfig(llm_system=_llm_system())
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", system_config, mcp_config, MCPRegistry())


def _streaming_llm(final_chunk):
    async def _stream(messages, tools, cancellation_token=None, status_scope=None):
        yield {"type": "content_delta", "delta": "", "accumulated": ""}
        yield final_chunk

    llm = AsyncMock()
    llm.supports_streaming = lambda: True
    llm.chat_tools_streaming = _stream
    # Every call, the max-steps one included, streams. An auto-generated
    # AsyncMock here would answer a stray blocking call with a Mock object, and
    # an assertion like `assert errors` could pass for that reason instead of
    # the one under test. Fail loudly instead.
    async def _no_blocking_call(*args, **kwargs):
        raise AssertionError(
            "chat_tools() must not be reached in a streaming test")
    llm.chat_tools = _no_blocking_call
    return llm


def _blocking_llm(response):
    llm = AsyncMock()
    llm.supports_streaming = lambda: False
    llm.chat_tools = AsyncMock(return_value=response)
    return llm


async def _collect(agent, task="do it"):
    return [ev async for ev in agent.run_events(task, session_id="s1")]


class TestStreamingPath:
    @pytest.mark.asyncio
    async def test_finish_reason_reaches_thinking_complete(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": ""},
            "usage": {"completion_tokens": 8000},
            "finish_reason": "length",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert completes and completes[0].get("finish_reason") == "length"

    @pytest.mark.asyncio
    async def test_truncation_is_reported_instead_of_nudged(self):
        """An empty answer with finish_reason=length is a TRUNCATION. Without
        the transport it looked like 'the model had nothing to say'."""
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": ""},
            "usage": {"completion_tokens": 8000,
                      "completion_tokens_details": {"reasoning_tokens": 7900}},
            "finish_reason": "length",
        })
        events = await _collect(agent)
        errors = [e for e in events if e.get("type") == "error"]
        assert errors, "truncation must surface as an error"
        message = errors[0].get("message", "")
        assert "finish_reason=length" in message
        assert "reasoning=7900" in message

    @pytest.mark.asyncio
    async def test_absent_finish_reason_is_not_invented(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "fertige Antwort"},
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert completes and "finish_reason" not in completes[0]

    @pytest.mark.asyncio
    async def test_normal_stop_does_not_trigger_the_guard(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "fertige Antwort"},
            "finish_reason": "stop",
        })
        events = await _collect(agent)
        assert not [e for e in events if e.get("type") == "error"]


class TestNonStreamingPath:
    @pytest.mark.asyncio
    async def test_finish_reason_reaches_thinking_complete(self):
        agent = _agent()
        agent.llm = _blocking_llm({
            "assistant": {"role": "assistant", "content": ""},
            "usage": {"completion_tokens": 4096},
            "finish_reason": "length",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert completes and completes[0].get("finish_reason") == "length"

    @pytest.mark.asyncio
    async def test_truncation_is_reported(self):
        agent = _agent()
        agent.llm = _blocking_llm({
            "assistant": {"role": "assistant", "content": ""},
            "usage": {"completion_tokens": 4096},
            "finish_reason": "length",
        })
        events = await _collect(agent)
        errors = [e for e in events if e.get("type") == "error"]
        assert errors and "finish_reason=length" in errors[0].get("message", "")


class TestTruncatedButNotEmpty:
    """The existing guard only covers "the model produced nothing at all".

    An answer WITH content that was cut off at the cap fell through as if
    complete -- a scene ending mid-sentence, or a tool call whose arguments
    JSON is half-written. Measured 2026-09-01: a writing agent hit
    max_output_tokens, the cut-off text was accepted, nothing said so.
    """

    @pytest.mark.asyncio
    async def test_truncated_with_content_is_reported(self, caplog):
        agent = _agent()
        agent.llm = _blocking_llm({
            "assistant": {"role": "assistant", "content": "halber Satz, der mitten"},
            "usage": {"completion_tokens": 4096},
            "finish_reason": "length",
        })
        with caplog.at_level("WARNING"):
            await _collect(agent)
        assert any("truncated at the output cap" in r.message for r in caplog.records), (
            f"keine Warnung: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_truncated_with_content_is_not_an_error(self):
        """Deliberately no error: that would switch the fallback profile
        persistently and discard output that is usually still usable."""
        agent = _agent()
        agent.llm = _blocking_llm({
            "assistant": {"role": "assistant", "content": "halber Satz, der mitten"},
            "usage": {"completion_tokens": 4096},
            "finish_reason": "length",
        })
        events = await _collect(agent)
        assert not [e for e in events if e.get("type") == "error"]

    @pytest.mark.asyncio
    async def test_complete_answer_is_not_reported(self, caplog):
        agent = _agent()
        agent.llm = _blocking_llm({
            "assistant": {"role": "assistant", "content": "fertige Antwort"},
            "finish_reason": "stop",
        })
        with caplog.at_level("WARNING"):
            await _collect(agent)
        assert not any("truncated at the output cap" in r.message
                       for r in caplog.records)


class TestStreamingErrorSurfacing:
    """The streaming assembler builds its own assistant dict and never set the
    "error" key that _format_response produces -- so the fallback-profile
    switch was unreachable while streaming."""

    @pytest.mark.asyncio
    async def test_content_filter_becomes_an_upstream_error(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": ""},
            "finish_reason": "content_filter",
        })
        events = await _collect(agent)
        # The upstream-error branch consumes the pending thinking_complete and
        # emits an error instead -- that IS the fallback machinery engaging.
        errors = [e for e in events if e.get("type") == "error"]
        assert errors, "a content filter must not pass as an empty answer"
        assert "content filter" in errors[0].get("message", "").lower()

    @pytest.mark.asyncio
    async def test_incomplete_stream_does_not_switch_the_profile(self):
        """A missing [DONE] marker is usually a transient hiccup. Turning it
        into an upstream error would switch the fallback profile PERSISTENTLY
        (an hour) and, for an agent without a fallback chain, end the run --
        where the existing empty-response guard simply retries. It is logged,
        not escalated."""
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": ""},
            "finish_reason": "incomplete_stream",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert completes, "the turn must reach the normal empty-response path"
        assert "error" not in completes[0]["assistant"]

    @pytest.mark.asyncio
    async def test_content_filter_with_tool_calls_keeps_the_turn(self):
        """Gemini reports a non-standard finish_reason while still returning
        usable tool calls -- _format_response deliberately keeps those, and the
        streaming path must not diverge."""
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "",
                          "tool_calls": [{"id": "t1", "type": "function",
                                          "function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "content_filter",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert completes and "error" not in completes[0]["assistant"]

    @pytest.mark.asyncio
    async def test_missing_done_marker_with_content_is_NOT_an_error(self):
        """Some providers legitimately end without [DONE]. Failing those would
        turn every single answer into a fallback."""
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "eine vollstaendige Antwort"},
            "finish_reason": "incomplete_stream",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert "error" not in completes[0]["assistant"]
        finals = [e for e in events if e.get("type") == "final"]
        assert finals, "the answer must still be delivered"

    @pytest.mark.asyncio
    async def test_tool_calls_alone_also_count_as_content(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "",
                          "tool_calls": [{"id": "t1", "type": "function",
                                          "function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "incomplete_stream",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert "error" not in completes[0]["assistant"]

    @pytest.mark.asyncio
    async def test_an_existing_error_is_not_overwritten(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "",
                          "error": {"message": "original", "type": "upstream"}},
            "finish_reason": "content_filter",
        })
        events = await _collect(agent)
        errors = [e for e in events if e.get("type") == "error"]
        assert errors and errors[0].get("message") == "original"

    @pytest.mark.asyncio
    async def test_normal_stop_is_untouched(self):
        agent = _agent()
        agent.llm = _streaming_llm({
            "type": "final",
            "assistant": {"role": "assistant", "content": "alles gut"},
            "finish_reason": "stop",
        })
        events = await _collect(agent)
        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert "error" not in completes[0]["assistant"]
