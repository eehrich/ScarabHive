"""An agent run with a response_format, through the real loop (Agent.run_events).

The format belongs to the run's FINAL answer, the step without tool calls. It is sent as the
provider's field on every call of the run -- the tool steps too, so the request prefix stays the
same from call to call -- where the step's LLM takes it; described in the conversation where it
does not and the caller allowed the prompt fallback; refused before any call otherwise. The final
answer is validated against the schema and sent back once for correction.

The LLM is scripted at the client seam (``chat_tools_streaming``); what the loop does around it
-- the decision per call, the notes, the check, the final event -- is the production code. One
test drives the real httpx Chat Completions client against a mock transport to measure what the
wire sees from step to step.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, ModelCapabilitiesConfig, ToolServerConfig
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm import structured_output
from agent_system.llm.structured_output import JSON_OBJECT, JSON_SCHEMA, ResponseFormat, close_schema_workers
from agent_system.llm.structured_output import STRUCTURED_OUTPUT_UNAVAILABLE
from agent_system.servers.agent.server import (
    STRUCTURED_OUTPUT_INVALID, STRUCTURED_OUTPUT_UNSUPPORTED, Agent,
)
from agent_system.tools.base import ToolServerRegistry
from test_agent_finish_reason_transport import _llm_system
import signal

# re holds the GIL, so only a signal ends a runaway match; Windows has no SIGALRM, and there
# the thread method still ends a hang (by ending the process) instead of the run never starting.
TIMEOUT_METHOD = "signal" if hasattr(signal, "SIGALRM") else "thread"

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["city", "days"], "additionalProperties": False}
GOOD = '{"city": "Oslo", "days": 3}'
TOOL = "lookup_weather"  # a tool the agent does not have: the step still runs, its result is an error


@pytest.fixture(autouse=True)
async def _schema_workers():
    """The answers are checked in schema worker processes of this test's loop: ended with it."""
    yield
    await close_schema_workers()


def _agent(max_steps: int = 5) -> Agent:
    agent_config = AgentConfig(max_steps=max_steps, llm_profile="normal")
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", AgentSystemConfig(llm_system=_llm_system()), server_config, ToolServerRegistry())


class ScriptedLLM:
    """One answer per call; "tool" is a step that calls a tool. Records what each call was handed."""

    response_format_kinds = (JSON_SCHEMA, JSON_OBJECT)

    def __init__(self, answers: list[str], *, native: bool = True):
        self.answers = answers
        self.native = native
        self.model = "scripted-1"
        self.calls: list[dict[str, Any]] = []

    def supports_streaming(self) -> bool:
        return True

    def supports_response_format(self, response_format: ResponseFormat) -> bool:
        return self.native

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        text, with_tool = (answer, False) if not isinstance(answer, tuple) else (answer[0], True)
        with_tool = with_tool or answer == "tool"
        assistant: dict[str, Any] = {"role": "assistant", "content": "" if answer == "tool" else text}
        if with_tool:
            assistant["tool_calls"] = [{"id": f"c{len(self.calls)}", "type": "function",
                                        "function": {"name": TOOL, "arguments": "{}"}}]
        yield {"type": "final", "assistant": assistant, "usage": {"completion_tokens": 5},
               "finish_reason": "tool_calls" if with_tool else "stop"}

    async def chat_tools(self, *args, **kwargs):
        raise AssertionError("chat_tools() must not be reached in a streaming test")


async def _run(agent: Agent, response_format: Optional[ResponseFormat] = None) -> list[dict]:
    extra = {"response_format": response_format} if response_format is not None else {}
    return [e async for e in agent.run_events("plan my trip", session_id="so", **extra)]


def _finals(events):
    return [e for e in events if e.get("type") == "final"]


def _errors(events):
    return [e for e in events if e.get("type") == "error"]


def _notes(messages, marker):
    return [m for m in messages if getattr(m, "injected_by", None) == marker]


async def test_the_format_goes_on_every_call_and_the_final_answer_is_delivered_as_json():
    agent = _agent()
    fmt = ResponseFormat(schema=SCHEMA, name="trip")
    agent.llm = llm = ScriptedLLM(["tool", GOOD])

    events = await _run(agent, fmt)

    assert len(llm.calls) == 2
    # The tool step too: one field for the whole run keeps the request prefix the same.
    assert [call["kwargs"].get("response_format") for call in llm.calls] == [fmt, fmt]
    assert not _notes(llm.calls[0]["messages"], "agent.structured_output"), "a native field needs no note"
    finals = _finals(events)
    assert finals and finals[-1]["summary"] == GOOD and finals[-1]["content_format"] == "json"
    assert not _errors(events)


async def test_the_answer_is_not_rendered_by_the_format_output_hooks():
    agent = _agent()
    agent.llm = ScriptedLLM([GOOD])

    async def to_html(output, **_):
        return f"<p>{output}</p>", "html"

    agent._hook_manager.execute_format_output_hooks = to_html

    events = await _run(agent, ResponseFormat(schema=SCHEMA))
    assert _finals(events)[-1]["summary"] == GOOD
    # The web chat shows the answer from the thinking events, before the final one arrives.
    shown = [e for e in events if e.get("type") == "thinking" and (e.get("content") or e.get("assistant"))]
    assert shown, "no thinking event carried the answer: this check would be vacuous"
    for event in shown:
        body = event.get("assistant") or event
        assert (body["content"], body["content_format"]) == (GOOD, "json")

    # Measured against the same run without a format: there the hook does render.
    plain = _agent()
    plain.llm = ScriptedLLM([GOOD])
    plain._hook_manager.execute_format_output_hooks = to_html
    assert _finals(await _run(plain))[-1]["summary"] == f"<p>{GOOD}</p>"


async def test_an_answer_that_does_not_match_is_sent_back_once_with_what_is_wrong():
    agent = _agent()
    agent.llm = llm = ScriptedLLM(['{"city": "Oslo", "days": "three"}', GOOD])

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert len(llm.calls) == 2, "the invalid answer ended the run"
    second = llm.calls[1]["messages"]
    notes = _notes(second, "agent.structured_output_repair")
    assert len(notes) == 1 and notes[0].role == DEVELOPER and "days" in notes[0].content
    assert second[-1] is notes[0], "the correction request is not what the model reads last"
    assert any(m.role == "assistant" and "three" in str(m.content) for m in second), "its answer was dropped"
    assert _finals(events)[-1]["summary"] == GOOD


async def test_an_answer_still_wrong_after_the_correction_ends_the_run_with_an_error():
    agent = _agent()
    agent.llm = llm = ScriptedLLM(["not json at all"])

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert len(llm.calls) == 2, "not one correction"
    assert not _finals(events), "an answer that does not match was delivered"
    errors = _errors(events)
    assert errors and errors[0]["error_type"] == STRUCTURED_OUTPUT_INVALID
    assert "after one correction" in errors[0]["message"]


async def test_a_fenced_answer_is_delivered_and_kept_without_its_fence():
    agent = _agent()
    agent.llm = ScriptedLLM(["```json\n" + GOOD + "\n```"])

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert _finals(events)[-1]["summary"] == GOOD
    kept = [m for m in agent._session_tracker.get_session_messages("so") if m.role == "assistant"]
    assert kept[-1].content == GOOD, "the session keeps the fence the caller parses (openai_api reads it there)"


async def test_a_run_that_only_ever_answers_blank_is_an_error_not_an_empty_answer():
    agent = _agent(max_steps=6)
    agent.llm = llm = ScriptedLLM(["   "])

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert len(llm.calls) == 3, "the fixture did not reach the no-tool-calls limit"
    assert not _finals(events)
    assert _errors(events)[0]["error_type"] == STRUCTURED_OUTPUT_INVALID


async def test_a_model_that_cannot_take_the_format_is_refused_before_any_call():
    agent = _agent()
    agent.llm = llm = ScriptedLLM([GOOD], native=False)

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert llm.calls == [], "the request went out without the field"
    errors = _errors(events)
    assert errors and errors[0]["error_type"] == STRUCTURED_OUTPUT_UNSUPPORTED
    assert "scripted-1" in errors[0]["message"]
    assert not _finals(events)


async def test_with_the_prompt_fallback_the_format_is_described_and_the_answer_still_checked():
    agent = _agent()
    agent.llm = llm = ScriptedLLM(["tool", '{"city": "Oslo"}', GOOD], native=False)

    events = await _run(agent, ResponseFormat(schema=SCHEMA, prompt_fallback=True))

    assert all("response_format" not in call["kwargs"] for call in llm.calls), "sent to a model that cannot take it"
    first_notes = _notes(llm.calls[0]["messages"], "agent.structured_output")
    assert len(first_notes) == 1 and first_notes[0].role == DEVELOPER
    note = first_notes[0].content
    assert json.loads(note[note.index("{"):]) == SCHEMA
    # Written once, and it stays where it was: the later calls carry the same note, not another.
    for call in llm.calls[1:]:
        assert _notes(call["messages"], "agent.structured_output") == first_notes
    assert len(llm.calls) == 3, "the answer missing 'days' was not sent back"
    assert _finals(events)[-1]["summary"] == GOOD


async def test_json_mode_sends_the_field_and_says_what_it_is_for():
    agent = _agent()
    agent.llm = llm = ScriptedLLM(['{"anything": true}'])
    fmt = ResponseFormat(type=JSON_OBJECT)

    events = await _run(agent, fmt)

    assert llm.calls[0]["kwargs"].get("response_format") == fmt
    # JSON mode carries no schema: the model hears of the format from the note (OpenAI refuses
    # json_object without the word JSON in the conversation).
    assert len(_notes(llm.calls[0]["messages"], "agent.structured_output")) == 1
    assert _finals(events)[-1]["summary"] == '{"anything": true}'


async def test_a_run_without_a_format_hands_the_client_nothing_new():
    """Byte-identical for every caller that does not ask (the writer's AgentCaller, every chat):
    no keyword, no note, the format_output hooks as before."""
    agent = _agent()
    agent.llm = llm = ScriptedLLM(["tool", "plain prose"])

    events = await _run(agent)

    assert [call["kwargs"] for call in llm.calls] == [{}, {}]
    for call in llm.calls:
        assert not [m for m in call["messages"] if str(getattr(m, "injected_by", "")).startswith(
            "agent.structured_output")]
    assert _finals(events)[-1]["summary"] and not _errors(events)


# ------------------------------------------------------------------ the wire, measured

def _chat_body(content: Optional[str] = None, tool: bool = False) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": "c1", "type": "function",
                                  "function": {"name": TOOL, "arguments": "{}"}}]
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "gpt-x",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _wire_client(bodies: list[dict], sent: list[dict]):
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=bodies[len(sent) - 1])

    client = HTTPXOpenAIClient(model="gpt-x", api_key="k", base_url="https://gateway.test/v1",
                               capabilities=ModelCapabilitiesConfig(streaming=False, structured_output=True),
                               max_retries=0)
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    return client, patched


async def test_on_the_wire_every_call_of_the_run_shares_the_field_and_the_prefix(monkeypatch):
    """The cache decision, measured on the real Chat Completions client: step 2 repeats step 1's
    tools and response_format byte for byte and extends its messages -- the prefix a provider
    caches on (tools, then the schema, then the conversation) is the same. A field on the last call
    only would change what stands in front of the conversation exactly there."""
    sent: list[dict] = []
    client, patched = _wire_client([_chat_body(tool=True), _chat_body(content=GOOD)], sent)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    agent = _agent()
    agent.llm = client
    fmt = ResponseFormat(schema=SCHEMA, name="trip", strict=True)

    events = await _run(agent, fmt)

    assert len(sent) == 2, [e for e in events if e.get("type") in ("error", "final")]
    first, second = sent
    expected = {"type": "json_schema", "json_schema": {"name": "trip", "schema": SCHEMA, "strict": True}}
    assert first["response_format"] == second["response_format"] == expected
    assert json.dumps(first["response_format"]) == json.dumps(second["response_format"])
    assert first.get("tools") == second.get("tools")
    assert second["messages"][:len(first["messages"])] == first["messages"], "the history was rewritten"
    assert _finals(events)[-1]["summary"] == GOOD


async def test_a_format_that_is_not_a_response_format_is_refused_at_once():
    agent = _agent()
    agent.llm = llm = ScriptedLLM([GOOD])

    with pytest.raises(TypeError, match="ResponseFormat"):
        [e async for e in agent.run_events("plan", session_id="so", response_format={"type": "json_object"})]
    assert llm.calls == []


# ------------------------------------------------------------------ when the run changes model

def _chain_agent(max_steps: int = 5, **config) -> Agent:
    from agent_system.config.models import LLMModelConfig, LLMProfile, LLMSystemConfig

    names = ("normal", "backup", "third", "advanced")
    llm_system = LLMSystemConfig(models={n: LLMModelConfig(provider="openai", model=n, api_key="k") for n in names},
                                 profiles={n: LLMProfile(model_ref=n) for n in names}, default_profile="normal")
    agent_config = AgentConfig(max_steps=max_steps, llm_profile=["normal", "backup", "third"], **config)
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system), server_config, ToolServerRegistry())


def _rate_limited(model: str) -> ScriptedLLM:
    from agent_system.llm.models import LLMRateLimitError

    llm = ScriptedLLM([LLMRateLimitError("429", model=model)])
    llm.model = model
    return llm


async def test_the_format_is_decided_per_call_when_a_fallback_answers_the_step():
    """The base takes the field and is rate-limited; the step moves to a fallback that does not: that
    call goes out without the field and with the note -- decided for the call, not for the step."""
    agent = _chain_agent()
    agent.llm = base = _rate_limited("normal")
    backup = ScriptedLLM([GOOD], native=False)
    agent._create_fallback_llm = {"backup": backup}.get

    events = await _run(agent, ResponseFormat(schema=SCHEMA, prompt_fallback=True))

    assert base.calls and base.calls[0]["kwargs"].get("response_format") is not None
    assert backup.calls and "response_format" not in backup.calls[0]["kwargs"]
    assert len(_notes(backup.calls[0]["messages"], "agent.structured_output")) == 1
    assert _finals(events)[-1]["summary"] == GOOD


async def test_without_the_fallback_a_failover_passes_over_a_model_that_cannot_take_the_format():
    agent = _chain_agent()
    agent.llm = _rate_limited("normal")
    backup = ScriptedLLM([GOOD], native=False)
    third = ScriptedLLM([GOOD])
    agent._create_fallback_llm = {"backup": backup, "third": third}.get

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert backup.calls == [], "the failover chose a model that cannot take the format"
    assert third.calls and third.calls[0]["kwargs"].get("response_format") is not None
    assert _finals(events)[-1]["summary"] == GOOD and not _errors(events)


async def test_without_the_fallback_a_stuck_run_does_not_escalate_to_a_model_that_cannot_take_it():
    """Two all-error tool steps open an escalation window. The advanced model cannot take the format:
    the run stays on its own model instead of ending on the escalation."""
    agent = _chain_agent(max_steps=6, llm_profile_advanced=["advanced"], auto_escalate_on_stuck=True)
    agent.llm = base = ScriptedLLM(["tool", "tool", "tool", GOOD])
    advanced = ScriptedLLM([GOOD], native=False)
    agent._get_escalation_llm = lambda: advanced

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert advanced.calls == [] and len(base.calls) == 4
    assert _finals(events)[-1]["summary"] == GOOD and not _errors(events)

    # Measured against the same run without a format: there the window opens and the advanced model answers.
    plain = _chain_agent(max_steps=6, llm_profile_advanced=["advanced"], auto_escalate_on_stuck=True)
    plain.llm = ScriptedLLM(["tool", "tool", "tool", GOOD])
    plain_advanced = ScriptedLLM([GOOD], native=False)
    plain._get_escalation_llm = lambda: plain_advanced
    await _run(plain)
    assert plain_advanced.calls, "fixture: the tool errors never opened an escalation window"


async def test_a_note_taken_out_of_the_history_comes_back_before_the_budget_note():
    """A hook that compacts drops the format note in the step that carries the step-budget note. The note
    comes back -- once -- and the budget note stays the last thing the model reads."""
    agent = _chain_agent(max_steps=5)
    agent.llm = llm = ScriptedLLM(["tool", "tool", "tool", GOOD], native=False)
    original = agent._hook_manager.execute_pre_llm_hooks

    async def compacting(**kwargs):
        messages = await original(**kwargs)
        if kwargs["step"] == 3:
            return [m for m in messages if getattr(m, "injected_by", None) != "agent.structured_output"]
        return messages

    agent._hook_manager.execute_pre_llm_hooks = compacting

    events = await _run(agent, ResponseFormat(schema=SCHEMA, prompt_fallback=True))

    last = llm.calls[3]["messages"]
    assert last[-1].injected_by == "agent.step_budget", "fixture: step 4 of 5 carries no budget note"
    assert last[-2].injected_by == "agent.structured_output", [m.injected_by for m in last[-3:]]
    assert len(_notes(last, "agent.structured_output")) == 1
    assert _finals(events)[-1]["summary"] == GOOD


def _blocking(agent: Agent) -> list[int]:
    """Loop detection pinned so that the same call is blocked on the last step and the final call (as in
    test_agent_max_steps_final_call); returns the steps whose tools ran."""
    agent._loop_detection_config = {"history_size": 20, "exact_match_threshold": 3, "sequence_threshold": 2,
                                    "block_after_threshold": 5, "auto_unblock_after_steps": 3}
    executed: list[int] = []
    execute = agent._tool_execution_manager.execute_tools_streaming

    async def counting(**kwargs):
        executed.append(kwargs["step"])
        async for item in execute(**kwargs):
            yield item

    agent._tool_execution_manager.execute_tools_streaming = counting
    return executed


@pytest.mark.parametrize("text", [GOOD, "not json"])
async def test_the_answer_beside_blocked_tool_calls_on_the_final_call_is_checked_too(text):
    agent = _agent(max_steps=5)
    agent.llm = llm = ScriptedLLM(["tool"] * 5 + [(text, "tool")])
    executed = _blocking(agent)

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert len(llm.calls) == 6 and 5 not in executed, f"fixture: tool rounds at {executed}"
    if text == GOOD:
        assert _finals(events)[-1]["summary"] == GOOD and _finals(events)[-1]["content_format"] == "json"
    else:
        assert not _finals(events), "an answer that does not match was delivered"
        assert _errors(events)[0]["error_type"] == STRUCTURED_OUTPUT_INVALID


async def test_without_the_fallback_a_walk_around_a_blocked_model_passes_over_one_that_cannot_take_it():
    from agent_system.llm.model_health import model_health

    agent = _chain_agent()
    agent.llm = base = ScriptedLLM([GOOD])
    base.model = "normal"
    model_health.block(base, max_pause=3600, rate_limit=False, reason="test")
    backup = ScriptedLLM([GOOD], native=False)
    third = ScriptedLLM([GOOD])
    agent._create_fallback_llm = {"backup": backup, "third": third}.get

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert base.calls == [], "fixture: the block did not make the step walk around the base"
    assert backup.calls == [] and third.calls, "the walk chose a model that cannot take the format"
    assert _finals(events)[-1]["summary"] == GOOD and not _errors(events)



async def test_a_format_outside_the_subset_ends_the_run_before_any_call():
    agent = _agent()
    agent.llm = llm = ScriptedLLM([GOOD])

    events = await _run(agent, ResponseFormat(schema={"type": "object", "allOf": [{"type": "object"}]}))

    assert llm.calls == [], "the run went on with a format nobody checked"
    error = _errors(events)[0]
    assert error["error_type"] == STRUCTURED_OUTPUT_INVALID and "'allOf'" in error["message"]


def _chain(n: int) -> dict:
    defs: dict = {"a0": {"type": "string"}}
    for i in range(1, n + 1):
        defs[f"a{i}"] = {"anyOf": [{"$ref": f"#/$defs/a{i - 1}"}, {"$ref": f"#/$defs/a{i - 1}"}]}
    return defs


@pytest.mark.timeout(15, method=TIMEOUT_METHOD)
async def test_an_answer_that_cannot_be_checked_in_time_ends_the_run_without_a_pointless_correction(monkeypatch):
    """Inside the subset and still minutes of work: the worker is killed at its deadline, the check fails
    closed, and the model is not asked again -- no answer would be checked any faster."""
    monkeypatch.setattr(structured_output, "CHECK_DEADLINE", 1.0)
    agent = _agent()
    agent.llm = llm = ScriptedLLM(["[" + ",".join(["1"] * 200_000) + "]"])
    costly = ResponseFormat(schema={"type": "array", "items": {"$ref": "#/$defs/a7"}, "$defs": _chain(7)})

    events = await _run(agent, costly)

    assert len(llm.calls) == 1, "the model was asked again although the check itself failed"
    error = _errors(events)[0]
    assert error["error_type"] == STRUCTURED_OUTPUT_INVALID and "did not finish within 1 s" in error["message"]


async def test_without_the_fallback_the_runs_own_model_decides_before_any_step():
    """The run's model cannot take the format and is blocked: a walk around it would reach a model that can,
    and the run would end mid-way once the block lifts. It ends before anything ran instead."""
    from agent_system.llm.model_health import model_health

    agent = _chain_agent()
    agent.llm = base = ScriptedLLM([GOOD], native=False)
    base.model = "normal"
    model_health.block(base, max_pause=3600, rate_limit=False, reason="test")
    third = ScriptedLLM([GOOD])
    agent._create_fallback_llm = {"backup": third, "third": third}.get

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert base.calls == [] and third.calls == [], "a step ran"
    assert _errors(events)[0]["error_type"] == STRUCTURED_OUTPUT_UNSUPPORTED


async def test_a_note_hollowed_out_under_its_marker_is_written_again():
    """An archiving compaction keeps the marker and replaces the text: that is no description of the format."""
    agent = _agent()
    agent.llm = llm = ScriptedLLM(["tool", GOOD], native=False)
    original = agent._hook_manager.execute_pre_llm_hooks

    async def archiving(**kwargs):
        messages = await original(**kwargs)
        if kwargs["step"] == 1:
            return [m.model_copy(update={"content": "[archived]"}) if m.injected_by == "agent.structured_output"
                    else m for m in messages]
        return messages

    agent._hook_manager.execute_pre_llm_hooks = archiving

    events = await _run(agent, ResponseFormat(schema=SCHEMA, prompt_fallback=True))

    notes = _notes(llm.calls[1]["messages"], "agent.structured_output")
    assert [n.content == "[archived]" for n in notes] == [True, False], [n.content[:20] for n in notes]
    assert _finals(events)[-1]["summary"] == GOOD


class _ForgetsTheFormat(ScriptedLLM):
    """Says it takes the format when the run starts, and not when it is called -- a stand-in for a switch
    of model that did not ask (the guard behind _takes_format)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked = 0

    def supports_response_format(self, response_format):
        self.asked += 1
        return self.asked == 1


async def test_a_call_to_a_model_that_cannot_take_the_format_is_never_sent_without_it():
    agent = _agent()
    agent.llm = llm = _ForgetsTheFormat([GOOD])

    events = await _run(agent, ResponseFormat(schema=SCHEMA))

    assert llm.asked >= 2, "fixture: the per-call decision was never asked"
    assert llm.calls == [] and _errors(events)[0]["error_type"] == STRUCTURED_OUTPUT_UNSUPPORTED



@pytest.mark.timeout(10, method=TIMEOUT_METHOD)
async def test_a_catastrophic_pattern_leaves_the_event_loop_serving_other_work():
    """The check runs in the schema worker's process: while the run's answer (and its correction) run out of
    time against the pattern there, a ticker on this loop keeps ticking."""
    import asyncio
    import time

    agent = _agent()
    # Ten values: every check runs through its whole budget (PATTERN_BUDGET), not one match's worth.
    agent.llm = ScriptedLLM([json.dumps({"s": ["a" * 40 + "b"] * 10})])
    fmt = ResponseFormat(schema={"type": "object", "properties": {"s": {"type": "array", "items": {
        "type": "string", "pattern": "^(a|a)*$"}}}})
    ticks: list[float] = []
    running = True

    async def ticker():
        while running:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.ensure_future(ticker())
    started = time.monotonic()
    try:
        events = await _run(agent, fmt)
    finally:
        running = False
        await task
    assert time.monotonic() - started < 3.0
    errors = _errors(events)
    assert errors and errors[0]["error_type"] == STRUCTURED_OUTPUT_INVALID and "in the time allowed" in errors[0]["message"]
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert len(ticks) > 20 and max(gaps) < 0.25, f"the loop stood still for {max(gaps):.2f} s"



async def test_a_model_with_only_json_mode_in_its_entry_gets_the_description_not_the_field(monkeypatch):
    """json_mode values in the catalogue were never verified: JSON mode goes native only with structured_output.
    Measured on the real Chat Completions client: no response_format on the wire, the note in the history."""
    sent: list[dict] = []
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_body(content='{"any": 1}'))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: original(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    agent = _agent()
    agent.llm = HTTPXOpenAIClient(model="gpt-x", api_key="k", base_url="https://gateway.test/v1", max_retries=0,
                                  capabilities=ModelCapabilitiesConfig(streaming=False, json_mode=True))

    events = await _run(agent, ResponseFormat(type=JSON_OBJECT, prompt_fallback=True))

    assert "response_format" not in sent[0]
    assert any("one JSON object" in (m.get("content") or "") for m in sent[0]["messages"])
    assert _finals(events)[-1]["summary"] == '{"any": 1}'



@pytest.mark.parametrize("prechecked", [True, False], ids=["the answer's check", "the format's check"])
async def test_a_checker_that_breaks_is_no_verdict_the_run_ends_as_unavailable(monkeypatch, tmp_path, prechecked):
    """Busy or broken is not the answer's fault: its own error_type (openai_api: a retryable 503), and the
    model is not asked for a correction nobody could check."""
    from pathlib import Path

    script = tmp_path / "exiting_worker.py"
    script.write_text("import sys\nsys.exit(3)\n")
    monkeypatch.setattr(structured_output, "_WORKER_PATH", Path(script))
    agent = _agent()
    agent.llm = llm = ScriptedLLM([GOOD])

    events = await _run(agent, ResponseFormat(schema=SCHEMA, checked=prechecked))

    error = _errors(events)[0]
    assert error["error_type"] == STRUCTURED_OUTPUT_UNAVAILABLE and "not available" in error["message"]
    assert len(llm.calls) == (1 if prechecked else 0)
    assert not _finals(events)


async def test_the_run_checks_in_its_user_s_lane(monkeypatch):
    from agent_system.core.request_context import register_request_user, release_request_user

    owners: list = []
    real = structured_output.SchemaWorkerPool.request

    async def recording(self, payload, deadline, owner=None):
        owners.append(owner)
        return await real(self, payload, deadline, owner)

    monkeypatch.setattr(structured_output.SchemaWorkerPool, "request", recording)
    register_request_user("lane-run", "bob")
    try:
        agent = _agent()
        agent.llm = ScriptedLLM([GOOD])
        [e async for e in agent.run_events("plan", request_id="lane-run", session_id="so",
                                           response_format=ResponseFormat(schema=SCHEMA))]
    finally:
        release_request_user("lane-run")
    assert owners and set(owners) == {"bob"}, owners
