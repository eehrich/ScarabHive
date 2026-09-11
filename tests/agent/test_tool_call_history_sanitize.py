"""Stored tool calls must always carry valid, DICT-shaped arguments JSON.

tool_execution rejects a call whose ``function.arguments`` are not valid
JSON, but the assistant message kept the raw string. Providers that
validate history server-side (OpenRouter Responses API) then reject EVERY
later request of the session ("Assistant tool call function.arguments must
be valid JSON") — the poisoned session survives model fallback and burns to
max steps. Observed live with v6 agents on 2026-09-01 (21 cases, sessions
dead for a whole afternoon).

Shape matters as much as validity: a JSON list or string would poison
Anthropic/Gemini instead, because ``tool_use.input`` /
``functionCall.args`` must be an object.

And history stores what execution ran on: a call execution rejected is
stored as ``{}``, never as json-repair's guess -- the model could send the
guess again, and the garbage the rejection stopped would run after all.

Three layers under test: the sanitizer itself, the production wiring of a
live loop (second LLM request), and the session-load path (a session
persisted with poison must be clean on resume).
"""
from __future__ import annotations

import copy
import json

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
from agent_system.llm.models import ChatMessage
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent
from agent_system.utils.json_utils import history_safe_tool_calls, repair_json


def _tc(arguments, call_id="t1"):
    return {"id": call_id, "type": "function",
            "function": {"name": "some_tool", "arguments": arguments}}


class TestSanitizer:
    def test_valid_arguments_stay_byte_identical(self):
        tc = _tc('{"a": 1}')
        out = history_safe_tool_calls([tc])
        assert out[0] is tc  # untouched, not even copied

    def test_malformed_arguments_become_valid_json(self):
        raw = '{"a": 1,, "b": }'
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)  # fixture assurance: really malformed
        out = history_safe_tool_calls([_tc(raw)])
        json.loads(out[0]["function"]["arguments"])  # must not raise

    @pytest.mark.parametrize("raw", [
        '{"doc": "synopsis", "data": {"background": "und erkenn"idas Schuld", "age": 3}}',
        '{"doc": "synopsis", "data": {"background": "cut off',
        '[{"path": "x.md"}',
        '{"a":1}{"b":2}',
    ], ids=["spliced-string", "cut-off", "truncated-list-wrap", "concatenated"])
    def test_a_rejected_call_is_stored_empty_not_as_the_repaired_guess(self, raw):
        """Execution rejects these; history must not show a call that looks
        fine next to the error. The model could send the guess again as it
        stands, and the garbage the rejection stopped would run one turn
        later."""
        assert repair_json(raw), "json-repair makes nothing of this -- the test would measure nothing"
        out = history_safe_tool_calls([_tc(raw)])
        assert out[0]["function"]["arguments"] == "{}"

    def test_a_list_wrapped_object_is_stored_unwrapped(self):
        """'[{"path": "x.md"}]' runs as {"path": "x.md"}; stored as the list,
        it would poison Anthropic/Gemini, whose tool input must be an object."""
        out = history_safe_tool_calls([_tc('[{"path": "x.md"}]')])
        assert json.loads(out[0]["function"]["arguments"]) == {"path": "x.md"}

    @pytest.mark.parametrize("raw", ['"just a string"', "[1, 2]", "null"])
    def test_json_that_is_not_an_object_is_stored_empty(self, raw):
        out = history_safe_tool_calls([_tc(raw)])
        assert out[0]["function"]["arguments"] == "{}"

    def test_a_line_break_inside_a_string_is_stored_as_valid_json(self):
        """A literal line break inside a string is not strict JSON, but its
        value is unambiguous: execution runs the call, and history stores it
        re-serialized -- providers validate strictly."""
        raw = '{"text": "line1\nline2"}'
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)  # fixture assurance: really not strict JSON
        out = history_safe_tool_calls([_tc(raw)])
        assert json.loads(out[0]["function"]["arguments"]) == {"text": "line1\nline2"}

    def test_empty_arguments_become_empty_object(self):
        out = history_safe_tool_calls([_tc("")])
        assert out[0]["function"]["arguments"] == "{}"

    def test_dict_arguments_become_a_json_string(self):
        """Ollama-native calls carry dict arguments; string-expecting
        providers 400 on them after a profile fallback. Ollama itself
        converts strings back to dicts at request build, so the string
        form is safe everywhere."""
        out = history_safe_tool_calls([_tc({"a": 1})])
        assert out[0]["function"]["arguments"] == '{"a": 1}'

    def test_missing_arguments_become_empty_object(self):
        tc = {"id": "t1", "type": "function", "function": {"name": "x"}}
        out = history_safe_tool_calls([tc])
        assert out[0]["function"]["arguments"] == "{}"

    def test_original_objects_are_not_mutated(self):
        """The execution path reads the ORIGINAL list -- a malformed call must
        still be rejected there, not run with the {} history stores."""
        tc = _tc('{"a": 1,, "b": }')
        before = copy.deepcopy(tc)
        history_safe_tool_calls([tc])
        assert tc == before

    def test_entries_without_function_dict_survive(self):
        odd = {"id": "t2", "type": "function"}
        assert history_safe_tool_calls([odd]) == [odd]

    def test_exotic_argument_types_become_empty_object(self):
        """No producer emits list/number arguments — but the promise of the
        function is unconditional, so the hole is closed anyway."""
        for exotic in ([1, 2], 7, True):
            out = history_safe_tool_calls([_tc(exotic)])
            assert out[0]["function"]["arguments"] == "{}"


# ---------------------------------------------------------------------------
# Production wiring: the real agent loop, a fake LLM.
# ---------------------------------------------------------------------------

def _agent(max_steps=2):
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4",
                                        api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )
    agent_config = AgentConfig(max_steps=max_steps)
    system_config = AgentSystemConfig(llm_system=llm_system)
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", system_config, mcp_config, MCPRegistry())


def _capturing_llm(responses, seen):
    async def chat_tools(messages, tools, **kwargs):
        seen.append([copy.deepcopy(m) for m in messages])
        return responses[min(len(seen) - 1, len(responses) - 1)]

    llm = AsyncMock()
    llm.supports_streaming = lambda: False
    llm.chat_tools = chat_tools
    return llm


RAW_MALFORMED = '{"a": 1,, "b": }'


def _assert_calls_sanitized(request_messages):
    calls = [tc for m in request_messages
             for tc in (getattr(m, "tool_calls", None) or [])]
    assert calls, "request carries no tool call — vacuous test"
    for tc in calls:
        json.loads(tc["function"]["arguments"])  # must not raise


class TestWiring:
    @pytest.mark.asyncio
    async def test_next_request_never_carries_invalid_arguments(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads(RAW_MALFORMED)
        seen: list = []
        responses = [
            {"assistant": {"role": "assistant", "content": "",
                           "tool_calls": [_tc(RAW_MALFORMED)]}},
            {"assistant": {"role": "assistant", "content": "done"}},
        ]
        agent = _agent()
        agent.llm = _capturing_llm(responses, seen)
        [ev async for ev in agent.run_events("do it", session_id="s1")]

        assert len(seen) >= 2, "loop must have reached the second LLM call"
        _assert_calls_sanitized(seen[1])

    @pytest.mark.asyncio
    async def test_poisoned_persisted_session_is_clean_on_resume(self):
        """The original incident: sessions written BEFORE the sanitizer (or
        by an older build) lie on disk with raw invalid arguments. Loading
        them must not resurrect the poison — otherwise every resume dies on
        the FIRST request."""
        agent = _agent(max_steps=1)
        poisoned = ChatMessage(role="assistant", content="",
                               tool_calls=[_tc(RAW_MALFORMED)])
        agent._session_tracker.set_session_messages("s1", [poisoned])

        seen: list = []
        agent.llm = _capturing_llm(
            [{"assistant": {"role": "assistant", "content": "done"}}], seen)
        [ev async for ev in agent.run_events("weiter", session_id="s1")]

        assert seen, "loop must have made an LLM call"
        _assert_calls_sanitized(seen[0])

    @pytest.mark.asyncio
    async def test_poisoned_dict_message_is_clean_on_resume(self):
        """Same load path, other branch: persisted messages can arrive as raw
        DICTS (not ChatMessage objects) — that branch sanitizes too."""
        agent = _agent(max_steps=1)
        poisoned = {"role": "assistant", "content": "",
                    "tool_calls": [_tc(RAW_MALFORMED)]}
        agent._session_tracker.set_session_messages("s1", [poisoned])

        seen: list = []
        agent.llm = _capturing_llm(
            [{"assistant": {"role": "assistant", "content": "done"}}], seen)
        [ev async for ev in agent.run_events("weiter", session_id="s1")]

        assert seen, "loop must have made an LLM call"
        _assert_calls_sanitized(seen[0])


class TestSessionServiceRestore:
    @pytest.mark.asyncio
    async def test_restore_sanitizes_poisoned_persisted_messages(self):
        """The disk→memory boundary: readers like agent_service and the
        context plugins take messages straight from the tracker without
        passing the request-build path — restore itself must sanitize."""
        from agent_system.services.session_service import SessionService

        class FakeManager:
            async def load_session(self, user_id, session_id):
                return {"messages": [
                    {"role": "assistant", "content": "",
                     "tool_calls": [_tc(RAW_MALFORMED)]},
                ]}

        restored = {}

        class FakeTracker:
            def set_session_messages(self, session_id, messages):
                restored[session_id] = messages

            def set_session_metadata(self, session_id, metadata):
                pass

        class FakeAgent:
            _session_tracker = FakeTracker()

        service = SessionService(session_manager=FakeManager())
        ok, count = await service.load_and_restore_session(
            FakeAgent(), "u1", "s1")

        assert ok and count == 1, "restore did not run — vacuous test"
        calls = [tc for m in restored["s1"]
                 for tc in (getattr(m, "tool_calls", None) or [])]
        assert calls, "restored message lost its tool call — vacuous test"
        for tc in calls:
            json.loads(tc["function"]["arguments"])  # must not raise
