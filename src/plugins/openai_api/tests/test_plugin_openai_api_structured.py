"""Structured output through openai_api: Chat Completions ``response_format``, Responses ``text.format``.

Through the real routes and the real ``openai`` SDK -- its ``parse()`` helpers validate the answer
against the model class they sent the schema for, so a test passes only when the content IS that
JSON. Behind the routes: a ``ScriptedAgent`` that records the format it was handed, and for the
whole chain (routes -> Agent loop -> LLM) a real ``Agent`` with a scripted LLM.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import openai
import pytest
from pydantic import BaseModel

from agent_system.llm import structured_output
from agent_system.llm.structured_output import JSON_OBJECT, JSON_SCHEMA, close_schema_workers
from test_plugin_openai_api import ScriptedAgent, build, client, raw
import signal

# re holds the GIL, so only a signal ends a runaway match; Windows has no SIGALRM, and there
# the thread method still ends a hang (by ending the process) instead of the run never starting.
TIMEOUT_METHOD = "signal" if hasattr(signal, "SIGALRM") else "thread"

pytestmark = pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning")


@pytest.fixture(autouse=True)
async def _schema_workers():
    """The schemas and answers are checked in schema worker processes of this test's loop: ended with it."""
    yield
    await close_schema_workers()

ANSWER = '{"city": "Oslo", "days": 3}'


class Trip(BaseModel):
    city: str
    days: int


class StructuredAgent(ScriptedAgent):
    """A ScriptedAgent that keeps the response_format each run was handed (None: none was)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.formats: list[Any] = []

    def run_events(self, task: str, request_id: Optional[str] = None, session_id: Optional[str] = None, **kwargs):
        self.formats.append(kwargs.get("response_format"))
        return super().run_events(task, request_id=request_id, session_id=session_id, **kwargs)


def _events(answer: httpx.Response) -> list[dict]:
    return [json.loads(line[6:]) for line in answer.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


# ------------------------------------------------------------------ the format reaches the agent

async def test_chat_completions_parse_gets_the_model_it_asked_for(tmp_path):
    agent = StructuredAgent("chat_agent", ANSWER)
    app, _ = build(tmp_path, agent)

    completion = await client(app).chat.completions.parse(
        model="chat_agent", messages=[{"role": "user", "content": "plan"}], response_format=Trip)

    assert completion.choices[0].message.parsed == Trip(city="Oslo", days=3)
    fmt = agent.formats[0]
    assert fmt.type == JSON_SCHEMA and fmt.name == "Trip" and fmt.strict is True
    assert fmt.schema["required"] == ["city", "days"], "not the schema the SDK sent"
    assert fmt.prompt_fallback, "an agent whose model cannot take the field would refuse the turn"


async def test_responses_parse_gets_the_model_it_asked_for(tmp_path):
    agent = StructuredAgent("chat_agent", ANSWER)
    app, _ = build(tmp_path, agent)

    response = await client(app).responses.parse(model="chat_agent", input="plan", text_format=Trip)

    assert response.output_parsed == Trip(city="Oslo", days=3)
    assert agent.formats[0].name == "Trip" and agent.formats[0].schema["properties"]["days"]["type"] == "integer"


async def test_json_mode_and_plain_text(tmp_path):
    agent = StructuredAgent("chat_agent", '{"any": 1}')
    app, _ = build(tmp_path, agent)
    api = client(app)
    messages = [{"role": "user", "content": "hi"}]

    await api.chat.completions.create(model="chat_agent", messages=messages, response_format={"type": "json_object"})
    await api.responses.create(model="chat_agent", input="hi", text={"format": {"type": "json_object"}})
    await api.chat.completions.create(model="chat_agent", messages=messages, response_format={"type": "text"})
    await api.responses.create(model="chat_agent", input="hi", text={"verbosity": "low"})
    await api.chat.completions.create(model="chat_agent", messages=messages)

    assert [f.type if f else None for f in agent.formats] == [JSON_OBJECT, JSON_OBJECT, None, None, None]


# ------------------------------------------------------------------ what is refused

@pytest.mark.parametrize("path,body,param,code", [
    ("/chat/completions", {"response_format": {"type": "xml"}}, "response_format.type", "invalid_value"),
    ("/chat/completions", {"response_format": {"type": "json_schema"}}, "response_format.json_schema",
     "missing_required_parameter"),
    ("/chat/completions", {"response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}},
     "response_format.json_schema.name", "missing_required_parameter"),
    ("/chat/completions", {"response_format": {"type": "json_schema", "json_schema": {"name": "t"}}},
     "response_format.json_schema.schema", "missing_required_parameter"),
    ("/chat/completions", {"response_format": {"type": "json_schema", "json_schema": {
        "name": "t", "schema": {"type": "object", "properties": {"a": {"type": "nope"}}}}}},
     "response_format.json_schema.schema", "invalid_value"),
    ("/chat/completions", {"response_format": {"type": "json_schema", "json_schema": {
        "name": "has space", "schema": {"type": "object"}}}}, "response_format.json_schema.name", "invalid_value"),
    ("/chat/completions", {"response_format": "json"}, "response_format", "invalid_type"),
    ("/chat/completions", {"response_format": {"type": "json_schema", "json_schema": "t"}},
     "response_format.json_schema", "invalid_type"),
    ("/chat/completions", {"response_format": {"type": "json_schema", "json_schema": {
        "name": "t", "schema": "{}"}}}, "response_format.json_schema.schema", "invalid_type"),
    ("/responses", {"text": {"format": {"type": "json_schema", "schema": {"type": "object"}}}},
     "text.format.name", "missing_required_parameter"),
    ("/responses", {"text": {"format": {"type": "json_schema", "name": "t", "schema": {"type": "object"},
                                        "strict": "yes"}}}, "text.format.strict", "invalid_type"),
    ("/responses", {"text": {"format": {"type": "yaml"}}}, "text.format.type", "invalid_value"),
    ("/responses", {"text": "json"}, "text", "invalid_type"),
])
async def test_a_format_that_cannot_be_asked_for_is_an_openai_error_before_the_run(tmp_path, path, body, param, code):
    agent = StructuredAgent("chat_agent", ANSWER)
    app, _ = build(tmp_path, agent)
    request = ({"model": "chat_agent", "messages": [{"role": "user", "content": "hi"}]} if path == "/chat/completions"
               else {"model": "chat_agent", "input": "hi"})

    async with raw(app) as web:
        answer = await web.post(path, json={**request, **body})

    assert answer.status_code == 400
    error = answer.json()["error"]
    assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", param, code)
    assert agent.calls == [], "the agent ran"


async def test_the_sdk_reads_the_refusal_as_its_bad_request(tmp_path):
    app, _ = build(tmp_path, StructuredAgent("chat_agent", ANSWER))

    with pytest.raises(openai.BadRequestError) as refused:
        await client(app).chat.completions.create(
            model="chat_agent", messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_schema", "json_schema": {"schema": {"type": "object"}}})
    assert refused.value.param == "response_format.json_schema.name"


# ------------------------------------------------------------------ streams

async def test_a_structured_stream_carries_the_answer_whole_and_nothing_of_the_steps(tmp_path):
    """A multi-step run: an unstructured stream sends every step's text; a structured one sends only the
    checked answer, so the joined deltas ARE the JSON. Both formats' events pass the SDK's own models."""
    from openai.types.chat import ChatCompletionChunk
    from openai.types.responses import ResponseStreamEvent
    from pydantic import TypeAdapter

    app, _ = build(tmp_path, StructuredAgent("chat_agent", ANSWER, steps=2))
    schema_format = {"type": "json_schema", "json_schema": {"name": "Trip", "schema": Trip.model_json_schema()}}
    chat = {"model": "chat_agent", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    async with raw(app) as web:
        chunks = _events(await web.post("/chat/completions", json={**chat, "response_format": schema_format}))
        plain = _events(await web.post("/chat/completions", json=chat))
        events = _events(await web.post("/responses", json={
            "model": "chat_agent", "input": "hi", "stream": True, "store": False,
            "text": {"format": {"type": "json_schema", "name": "Trip", "schema": Trip.model_json_schema()}}}))

    def joined(stream: list[dict]) -> str:
        return "".join(c["choices"][0]["delta"].get("content") or "" for c in stream if c["choices"])

    assert "step 1 note" in joined(plain), "the fixture ran one step: this test would be vacuous"
    assert joined(chunks) == ANSWER
    for chunk in chunks:
        ChatCompletionChunk.model_validate(chunk)
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    done = [e["text"] for e in events if e["type"] == "response.output_text.done"]
    assert deltas == ANSWER and done == [ANSWER]
    for event in events:
        TypeAdapter(ResponseStreamEvent).validate_python(event)


# ------------------------------------------------------------------ the whole chain: route -> Agent loop -> LLM

class _LLM:
    """What the agent's model answers, call by call; takes the schema as a field (native)."""

    response_format_kinds = (JSON_SCHEMA, JSON_OBJECT)

    def __init__(self, answers: list[str], native: bool = True):
        self.answers, self.native, self.model, self.formats = answers, native, "scripted-1", []

    def supports_streaming(self) -> bool:
        return True

    def supports_response_format(self, response_format) -> bool:
        return self.native

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None, **kwargs):
        self.formats.append(kwargs.get("response_format"))
        answer = self.answers[min(len(self.formats), len(self.answers)) - 1]
        yield {"type": "final", "assistant": {"role": "assistant", "content": answer},
               "usage": {"prompt_tokens": 5, "completion_tokens": 5}, "finish_reason": "stop"}


def _real_agent(llm: _LLM):
    from agent_system.config.models import (
        AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig, ToolServerConfig,
    )
    from agent_system.servers.agent.server import Agent
    from agent_system.tools.base import ToolServerRegistry

    llm_system = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="k")},
                                 profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    agent_config = AgentConfig(max_steps=4, llm_profile="normal")
    agent = Agent("chat_agent", AgentSystemConfig(llm_system=llm_system),
                  ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), ToolServerRegistry())
    agent.llm = llm
    return agent


@pytest.mark.parametrize("native", [True, False])
async def test_the_agent_loop_corrects_the_answer_and_the_sdk_parses_it(tmp_path, native):
    llm = _LLM(['{"city": "Oslo", "days": "three"}', ANSWER], native=native)
    app, _ = build(tmp_path, _real_agent(llm))

    completion = await client(app).chat.completions.parse(
        model="chat_agent", messages=[{"role": "user", "content": "plan"}], response_format=Trip)

    assert completion.choices[0].message.parsed == Trip(city="Oslo", days=3)
    assert len(llm.formats) == 2, "the first answer was not sent back"
    if native:
        assert all(f is not None and f.name == "Trip" for f in llm.formats)
    else:
        assert llm.formats == [None, None], "the field went to a model that cannot take it"


async def test_an_answer_the_agent_cannot_get_right_is_an_error_not_a_parse_failure(tmp_path):
    app, _ = build(tmp_path, _real_agent(_LLM(["Oslo, three days"])))

    async with raw(app) as web:
        answer = await web.post("/responses", json={"model": "chat_agent", "input": "plan", "store": False,
                                                    "text": {"format": {"type": "json_schema", "name": "Trip",
                                                                        "schema": Trip.model_json_schema()}}})

    assert answer.status_code == 500
    error = answer.json()["error"]
    assert "does not match the requested format after one correction" in error["message"]
    assert error["code"] == "structured_output_invalid" and answer.headers["x-should-retry"] == "false"


async def test_the_sdk_does_not_buy_two_more_runs_for_the_same_verdict(tmp_path):
    """With the SDK's default retries: one run -- the answer and its correction -- not three."""
    from openai import AsyncOpenAI

    llm = _LLM(["Oslo, three days"])
    app, _ = build(tmp_path, _real_agent(llm))
    api = AsyncOpenAI(api_key="k", base_url="http://test/plugins/openai_api/v1",
                      http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))

    with pytest.raises(openai.InternalServerError):
        await api.chat.completions.parse(model="chat_agent", messages=[{"role": "user", "content": "plan"}],
                                         response_format=Trip)
    assert len(llm.formats) == 2, f"{len(llm.formats)} LLM calls: the SDK ran the turn again"


class _Machine(StructuredAgent):
    """An agent whose run_events takes no response_format (the stategraph facade's MachineAgent)."""

    def run_events(self, task: str, request_id: Optional[str] = None, session_id: Optional[str] = None,
                   llm_override: Any = None, llm_profile_info_override: Optional[str] = None,
                   use_advanced_model: bool = False):
        return super().run_events(task, request_id=request_id, session_id=session_id)


@pytest.mark.parametrize("path,body,param", [
    ("/chat/completions", {"messages": [{"role": "user", "content": "hi"}],
                           "response_format": {"type": "json_object"}}, "response_format"),
    ("/responses", {"input": "hi", "text": {"format": {"type": "json_object"}}}, "text.format"),
])
async def test_an_agent_that_cannot_take_a_format_is_refused_before_it_runs(tmp_path, path, body, param):
    machine = _Machine("chat_agent", ANSWER)
    app, _ = build(tmp_path, machine)

    async with raw(app) as web:
        refused = await web.post(path, json={"model": "chat_agent", **body})
        plain = await web.post(path, json={"model": "chat_agent", **{k: v for k, v in body.items()
                                                                      if k not in ("response_format", "text")}})

    assert refused.status_code == 400
    error = refused.json()["error"]
    assert (error["param"], error["code"]) == (param, "unsupported_parameter")
    assert plain.status_code == 200 and len(machine.calls) == 1, "only the plain request ran"



async def test_a_chat_stream_names_the_format_verdict_in_its_error_chunk(tmp_path):
    app, _ = build(tmp_path, _real_agent(_LLM(["Oslo, three days"])))
    fmt = {"type": "json_schema", "json_schema": {"name": "Trip", "schema": Trip.model_json_schema()}}

    async with raw(app) as web:
        answer = await web.post("/chat/completions", json={"model": "chat_agent", "stream": True,
                                                           "messages": [{"role": "user", "content": "plan"}],
                                                           "response_format": fmt})

    errors = [e["error"] for e in _events(answer) if "error" in e]
    assert errors and errors[0]["code"] == "structured_output_invalid"



# ------------------------------------------------------------------ what the review measured, through the endpoint

def _defs_chain(n: int, keyword: str) -> dict:
    defs: dict = {"a0": {"type": "string"}}  # the review's chain ended on `false`: refused as no schema object
    for i in range(1, n + 1):
        defs[f"a{i}"] = {keyword: [{"$ref": f"#/$defs/a{i - 1}"}, {"$ref": f"#/$defs/a{i - 1}"}]}
    return {"$defs": defs, "$ref": f"#/$defs/a{n}"}


WIDE = {"type": "object", "properties": {f"field_{i:04d}": {"type": "string", "pattern": "^[a-z]+$"}
                                         for i in range(1_600)}}


@pytest.mark.timeout(20, method=TIMEOUT_METHOD)  # a hang ends the test, not the run
@pytest.mark.parametrize("schema, named", [
    ({"default": {"$schema": "https://json-schema.org/draft/2020-12/schema", "pattern": "^(a|a)*$"},
      "$ref": "#/default"}, "'#/default'"),
    ({"$schema": "http://json-schema.org/draft-03/schema#",
      "extends": {"$schema": "https://json-schema.org/draft/2020-12/schema", "pattern": "^(a|a)*$"}}, "draft-03"),
    ({"$defs": {"d": {"patternProperties": {"^(a|a)*$": {}}, "unevaluatedProperties": False}}, "$ref": "#/$defs/d"},
     "'patternProperties'"),
    (_defs_chain(30, "allOf"), "'allOf'"),
    (_defs_chain(30, "anyOf"), "subschemas through anyOf"),
    ({"type": "string", "pattern": "^(?:(?:(?:a{130}){130}){130})$"}, "repeats too much"),
    ({"type": "object", "description": "x" * 200_000}, "characters as JSON"),
])
async def test_a_schema_the_review_used_against_the_process_is_a_400_that_leaves_it_alone(tmp_path, schema, named,
                                                                                          monkeypatch):
    """Each within the deadline, with the keyword named; the agent never runs, the event loop keeps ticking
    and this process's memory stays where it was (the checks run in the schema worker)."""
    import asyncio
    import time

    import psutil

    monkeypatch.setattr(structured_output, "PREPARE_DEADLINE", 2.0)
    agent = StructuredAgent("chat_agent", ANSWER)
    app, _ = build(tmp_path, agent)
    ticks: list[float] = []
    running = True

    async def ticker():
        while running:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    rss_before = psutil.Process().memory_info().rss
    task = asyncio.ensure_future(ticker())
    started = time.monotonic()
    try:
        async with raw(app) as web:
            answer = await web.post("/chat/completions", json={
                "model": "chat_agent", "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}})
    finally:
        running = False
        await task
    assert time.monotonic() - started < 3.0
    assert answer.status_code == 400, answer.text
    error = answer.json()["error"]
    assert (error["param"], error["code"]) == ("response_format.json_schema.schema", "invalid_value")
    assert named in error["message"], error["message"]
    assert agent.calls == []
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps, default=0.0) < 0.25, f"the loop stood still for {max(gaps):.2f} s"
    assert psutil.Process().memory_info().rss - rss_before < 64 * 1024 * 1024


@pytest.mark.timeout(20, method=TIMEOUT_METHOD)
async def test_a_wide_schema_is_built_off_the_event_loop(tmp_path, monkeypatch):
    """Inside the subset and some 90 000 characters: checked in the worker, while the loop goes on. The
    worker's functions are booby-trapped in this process -- a check moved back here (into a thread, say)
    fails on them; the gap in the ticks is the looser second look."""
    import asyncio
    import time

    from agent_system.llm import schema_worker

    def trap(*args, **kwargs):
        raise AssertionError("a schema check ran in the API process")

    for name in ("normalize_schema", "check_value", "handle"):
        monkeypatch.setattr(schema_worker, name, trap)

    assert 60_000 < len(json.dumps(WIDE)) < 100_000, "fixture: the schema is not as wide as intended"
    agent = StructuredAgent("chat_agent", json.dumps({f"field_{i:04d}": "abc" for i in range(1_600)}))
    app, _ = build(tmp_path, agent)
    ticks: list[float] = []
    running = True

    async def ticker():
        while running:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.ensure_future(ticker())
    try:
        async with raw(app) as web:
            answer = await web.post("/responses", json={
                "model": "chat_agent", "input": "hi", "store": False,
                "text": {"format": {"type": "json_schema", "name": "wide", "schema": WIDE}}})
    finally:
        running = False
        await task
    assert answer.status_code == 200, answer.text
    assert agent.formats[0].checked and agent.formats[0].schema == WIDE
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    # Built here, this schema holds the loop some 0.2 s (measured); in the worker the ticks go on.
    assert max(gaps, default=0.0) < 0.25, f"the loop stood still for {max(gaps):.2f} s"


async def test_a_structured_stream_says_it_is_alive_while_the_run_goes_on(tmp_path, monkeypatch):
    """Nothing of the run goes out before its checked answer -- but a comment does, so a proxy keeps the
    connection. The SDK reads past it."""
    import asyncio

    from plugins.openai_api import plugin as api_plugin

    monkeypatch.setattr(api_plugin, "KEEPALIVE_SECONDS", 0.05)

    async def slow(call: dict) -> str:
        await asyncio.sleep(0.4)
        return ANSWER

    app, _ = build(tmp_path, StructuredAgent("chat_agent", slow))
    fmt = {"type": "json_schema", "json_schema": {"name": "Trip", "schema": Trip.model_json_schema()}}
    async with raw(app) as web:
        answer = await web.post("/chat/completions", json={"model": "chat_agent", "stream": True,
                                                           "messages": [{"role": "user", "content": "hi"}],
                                                           "response_format": fmt})
    assert answer.text.count(": keep-alive") >= 3, answer.text[:300]
    stream = await client(app).chat.completions.create(model="chat_agent", stream=True, response_format=fmt,
                                                       messages=[{"role": "user", "content": "hi"}])
    assert "".join([c.choices[0].delta.content or "" async for c in stream if c.choices]) == ANSWER



async def test_stopping_the_plugin_ends_its_schema_workers(tmp_path):
    import psutil

    app, plugin = build(tmp_path, StructuredAgent("chat_agent", ANSWER))
    await client(app).chat.completions.parse(model="chat_agent", messages=[{"role": "user", "content": "x"}],
                                             response_format=Trip)

    def workers() -> list:
        found = []
        for child in psutil.Process().children():
            try:
                if any("schema_worker.py" in part for part in child.cmdline()):
                    found.append(child)
            except (psutil.ZombieProcess, psutil.NoSuchProcess):
                pass  # another test's child, ended and not collected yet: its command line is gone
        return found

    running = workers()
    assert running, "fixture: no schema worker ran"
    await plugin.stop_plugin()
    assert not workers()
    assert not [worker.pid for worker in running if worker.is_running()], "a worker was left behind, or not collected"



async def test_a_decimal_multiple_passes_the_route(tmp_path):
    """pydantic's Field(multiple_of=0.1) and an answer of 2.3: a multiple, and parsed as one."""
    from pydantic import Field

    class Price(BaseModel):
        amount: float = Field(multiple_of=0.1)

    llm = _LLM(['{"amount": 2.3}'])
    app, _ = build(tmp_path, _real_agent(llm))

    completion = await client(app).chat.completions.parse(
        model="chat_agent", messages=[{"role": "user", "content": "price"}], response_format=Price)

    assert completion.choices[0].message.parsed == Price(amount=2.3)
    assert len(llm.formats) == 1, "a valid answer was sent back for correction"


def _checker_script(tmp_path, *, check: str) -> str:
    """A stand-in worker: answers prepare with the schema it got; on check it `check`s (exits, or hangs)."""
    script = tmp_path / "stand_in_worker.py"
    script.write_text("import json, sys, time\n"
                      "for line in sys.stdin:\n"
                      "    request = json.loads(line)\n"
                      "    if request['op'] == 'prepare':\n"
                      "        sys.stdout.write(json.dumps({'ok': True, 'schema': request['schema']}) + '\\n')\n"
                      "        sys.stdout.flush()\n"
                      "        continue\n"
                      f"    {check}\n")
    return str(script)


@pytest.mark.parametrize("path", ["/chat/completions", "/responses"])
async def test_a_checker_that_breaks_is_a_retryable_503_not_a_verdict(tmp_path, monkeypatch, path):
    """No verdict on the answer: 503 with its own code, and no x-should-retry: false -- the SDK may try again."""
    from pathlib import Path

    monkeypatch.setattr(structured_output, "_WORKER_PATH", Path(_checker_script(tmp_path, check="sys.exit(3)")))
    llm = _LLM([ANSWER])
    app, _ = build(tmp_path, _real_agent(llm))
    fmt = ({"response_format": {"type": "json_schema", "json_schema": {"name": "Trip", "schema": Trip.model_json_schema()}},
            "messages": [{"role": "user", "content": "plan"}]} if path == "/chat/completions" else
           {"text": {"format": {"type": "json_schema", "name": "Trip", "schema": Trip.model_json_schema()}},
            "input": "plan", "store": False})

    async with raw(app) as web:
        answer = await web.post(path, json={"model": "chat_agent", **fmt})

    assert answer.status_code == 503, answer.text
    assert answer.json()["error"]["code"] == "structured_output_unavailable"
    assert "x-should-retry" not in answer.headers
    assert len(llm.formats) == 1, "the model was asked again although the answer was never judged"


async def test_a_checker_that_cannot_start_is_a_503_before_the_run(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(structured_output, "_WORKER_PATH", Path(tmp_path / "missing.py"))
    agent = StructuredAgent("chat_agent", ANSWER)
    app, _ = build(tmp_path, agent)

    async with raw(app) as web:
        answer = await web.post("/chat/completions", json={
            "model": "chat_agent", "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}})

    assert answer.status_code == 503 and "not available" in answer.json()["error"]["message"]
    assert answer.json()["error"]["code"] == "structured_output_unavailable" and "x-should-retry" not in answer.headers
    assert agent.calls == []


async def test_the_route_checks_in_the_callers_lane(tmp_path, monkeypatch):
    """The per-user cap needs the user: the route hands it to the worker pool (auth off: one user)."""
    owners: list = []
    real = structured_output.SchemaWorkerPool.request

    async def recording(self, payload, deadline, owner=None):
        owners.append(owner)
        return await real(self, payload, deadline, owner)

    monkeypatch.setattr(structured_output.SchemaWorkerPool, "request", recording)
    app, _ = build(tmp_path, _real_agent(_LLM([ANSWER])))

    await client(app).chat.completions.parse(model="chat_agent", messages=[{"role": "user", "content": "x"}],
                                             response_format=Trip)

    assert owners and set(owners) == {"anonymous"}, owners
