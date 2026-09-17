"""finish_reason has to survive the trip from client to truncation guard.

The guard in the agent loop reads it off `llm_out` -- but nothing in between
carried it, so `if finish_reason == "length"` was dead code and a truncated
answer was accepted as an empty one (which then triggers a 'Continue' nudge and
replays the same runaway).

Scope: httpx_client, the gemini clients, openai_responses, anthropic (stream
and batch), ollama and the batch wrapper report finish_reason, so the guard is
live for those providers. The transport is provider-agnostic regardless.
"""
import copy

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
        profiles={"normal": LLMProfile(model_ref="gpt-4"),
                  "backup": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )


def _agent(max_steps=1, llm_profile="normal"):
    agent_config = AgentConfig(max_steps=max_steps, llm_profile=llm_profile)
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
        into an upstream error would move the run onto the fallback profile
        and, for an agent without a fallback chain, end the run --
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


_FILTERED_USAGE = {"prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290}


class _FilteredLLM:
    """Answers every call with finish_reason=content_filter -- streamed or
    blocking, the same answer either way, so the two paths are comparable."""

    model = "stub/filtered"

    def __init__(self, streaming: bool, assistant: dict):
        self.streaming = streaming
        self.assistant = assistant
        self.calls = 0

    def supports_streaming(self) -> bool:
        return self.streaming

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        if self.streaming:
            raise AssertionError("chat_tools() must not be reached on the streaming path")
        self.calls += 1
        return {"assistant": copy.deepcopy(self.assistant), "finish_reason": "content_filter",
                "usage": dict(_FILTERED_USAGE)}

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None,
                                   status_scope=None):
        if not self.streaming:
            raise AssertionError("chat_tools_streaming() must not be reached on the blocking path")
        self.calls += 1
        yield {"type": "final", "assistant": copy.deepcopy(self.assistant),
               "finish_reason": "content_filter", "usage": dict(_FILTERED_USAGE)}


class _BackupLLM:
    model = "stub/backup"

    def __init__(self):
        self.calls = 0

    def supports_streaming(self) -> bool:
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.calls += 1
        return {"assistant": {"role": "assistant", "content": "backup answer"},
                "finish_reason": "stop"}


_EMPTY = {"role": "assistant", "content": ""}
_PARTIAL = {"role": "assistant", "content": "half of a sentence the filter"}
_WITH_TOOL_CALLS = {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "t1", "type": "function",
                                    "function": {"name": "f", "arguments": "{}"}}]}
_PATHS = pytest.mark.parametrize("streaming", [True, False], ids=["streaming", "blocking"])
_FILTERED = pytest.mark.parametrize("assistant", [_EMPTY, _PARTIAL], ids=["empty", "partial"])


class TestContentFilterIsOneRuleForBothPaths:
    """The blocking path only copied finish_reason=content_filter along. An
    empty answer was then nudged with "Continue" until the empty-response guard
    gave up -- the fallback chain never saw it -- and a partial one was
    delivered as complete. Reachable through openai_responses with
    ``streaming: false``, whose _format_response reports the reason without an
    error key."""

    @_FILTERED
    @pytest.mark.asyncio
    async def test_without_a_chain_both_paths_end_on_the_same_error(self, assistant):
        outcome = {}
        for streaming in (True, False):
            llm = _FilteredLLM(streaming, assistant)
            agent = _agent(max_steps=3)
            agent.llm = llm
            events = await _collect(agent)
            assert llm.calls == 1, (
                f"streaming={streaming}: the filtered model was asked {llm.calls} times")
            outcome[streaming] = [e for e in events if e.get("type") in ("error", "final")]
        assert [e.get("error_type") for e in outcome[True]] == ["content_filter"], outcome[True]
        assert outcome[False] == outcome[True]

    @_PATHS
    @_FILTERED
    @pytest.mark.asyncio
    async def test_the_fallback_answers_instead(self, streaming, assistant):
        llm = _FilteredLLM(streaming, assistant)
        agent = _agent(max_steps=3, llm_profile=["normal", "backup"])
        agent.llm = llm
        backup = _BackupLLM()
        built = []
        agent._create_fallback_llm = lambda profile: built.append(profile) or backup

        events = await _collect(agent)

        assert llm.calls == 1
        assert built == ["backup"]
        assert backup.calls == 1
        assert not [e for e in events if e.get("type") == "error"]
        assert [e["summary"] for e in events if e.get("type") == "final"] == ["backup answer"]

    @_PATHS
    @_FILTERED
    @pytest.mark.asyncio
    async def test_the_blocked_call_is_still_counted(self, streaming, assistant):
        """The provider bills the blocked call: its usage reaches the caller's
        sum of the turn under its own model, its content never does -- with a
        chain and without one."""
        for chain in (["normal", "backup"], "normal"):
            agent = _agent(max_steps=3, llm_profile=chain)
            agent.llm = _FilteredLLM(streaming, assistant)
            agent._create_fallback_llm = lambda profile: _BackupLLM()

            events = await _collect(agent)

            counted = [e for e in events if e.get("type") == "thinking_complete"
                       and e.get("usage") == _FILTERED_USAGE]
            assert len(counted) == 1, (chain, events)
            assert counted[0]["model"] == "stub/filtered"
            assert counted[0]["assistant"] == {}

    @_PATHS
    @pytest.mark.asyncio
    async def test_tool_calls_keep_the_turn(self, streaming):
        """Gemini reports a non-standard finish_reason while still returning
        usable tool calls; httpx _format_response keeps them, so neither path
        may throw the turn away."""
        llm = _FilteredLLM(streaming, _WITH_TOOL_CALLS)
        agent = _agent(max_steps=1, llm_profile=["normal", "backup"])
        agent.llm = llm
        built = []
        agent._create_fallback_llm = lambda profile: built.append(profile) or _BackupLLM()

        events = await _collect(agent)

        completes = [e for e in events if e.get("type") == "thinking_complete"]
        assert completes, "the turn never completed"
        assert completes[0]["assistant"]["tool_calls"] == _WITH_TOOL_CALLS["tool_calls"]
        assert "error" not in completes[0]["assistant"]
        assert built == [], f"a turn with tool calls switched profiles: {built}"
