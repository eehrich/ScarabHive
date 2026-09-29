"""ask_user through the real path.

The server is built by its factory; the call comes from a real
``Agent.run_events`` (only the LLM is scripted), the question reaches the
run's stream as a status line, and the answer goes through the plugin's HTTP
route. What is pinned:

* the question reaches the stream with its options, and the answer -- options,
  free text, several options where the question allows it -- comes back as
  the tool result the model reads;
* only the run's user or an admin may answer, signed in, not by API key; an
  answer that does not fit the question is refused and the question waits on;
* a run nobody watches gets an error at once, and so does a call that is not
  the model's own (a script's);
* no answer within ``ask_timeout``, a cancelled run, a reader who left, a wait
  cut off from outside: each ends the call with its own result;
* the question's row always ends, so the chat takes its box down, and an
  answer after the end is refused, not taken.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.auth.security import create_access_token
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    AuthConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.core.cancellation import get_cancellation_manager
from agent_system.core.request_context import (
    register_request_user,
    release_request_user_tree,
    release_run_attended,
    set_run_attended,
)
from agent_system.core.run_questions import AnswerRejected
from agent_system.servers.agent.components.session_tracking import SessionTracker
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import get_background_job_manager
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry
from agent_system.tools.status import StatusHandler, StatusPhase, get_status_bus
from plugins.ask_user.plugin import PLUGIN_FACTORY
from plugins.sub_agent_manager.server import SubAgentManagerServer

URL = "/plugins/ask_user/answer"


def _server(**config) -> Any:
    # A question nobody in the test answers ends in seconds, not after five minutes.
    return PLUGIN_FACTORY("ask_user", AgentSystemConfig(),
                          ToolServerConfig(type="ask_user", enabled=True, config={"ask_timeout": 5, **config}))


def _ask(call_id: str, question: str, options: Optional[List[str]] = None, **extra) -> Dict[str, Any]:
    arguments: Dict[str, Any] = {"question": question, **extra}
    if options is not None:
        arguments["options"] = options
    return {"id": call_id, "type": "function", "function": {"name": "ask_user", "arguments": json.dumps(arguments)}}


class _Model:
    """One round of tool calls per entry of ``rounds``, then an answer; records every request."""

    model = "test/model"

    def __init__(self, *rounds: List[Dict[str, Any]]):
        self._rounds = rounds
        self.requests: List[List[Dict[str, Any]]] = []

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.requests.append([{"role": m.role, "content": m.content, "tool_call_id": m.tool_call_id}
                              for m in messages])
        if len(self.requests) <= len(self._rounds):
            yield {"type": "final", "assistant": {"role": "assistant", "content": None,
                                                  "tool_calls": self._rounds[len(self.requests) - 1]}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}

    def results(self) -> List[Dict[str, Any]]:
        """Every tool result the model read, in order."""
        seen: Dict[str, Dict[str, Any]] = {}
        for request in self.requests:
            for m in request:
                if m["role"] == "tool":
                    seen[m["tool_call_id"]] = json.loads(m["content"])
        return list(seen.values())


def _agent(server, name: str = "asking_agent", registry: Optional[ToolServerRegistry] = None) -> Agent:
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
    if registry is None:
        registry = ToolServerRegistry()
        registry.register("ask_user", server)
    agent_config = AgentConfig(max_steps=6, llm_profile="normal", tools=ToolConfig(allowed=["ask_user/*"]))
    return Agent(name, AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), registry)


@pytest.fixture
def watched():
    """watch(request_id, user, attended) -- a run the web chat started for ``user``."""
    ids: List[str] = []

    def watch(request_id: str, user: str = "alice", attended: bool = True) -> str:
        set_run_attended(request_id, attended)
        register_request_user(request_id, user)
        ids.append(request_id)
        return request_id

    yield watch
    for request_id in ids:
        release_run_attended(request_id)
        release_request_user_tree(request_id)


@pytest.fixture
def users(tmp_path, monkeypatch):
    """A user store of this test's own: alice and bob, root the admin."""
    store = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", store)
    for name, role in [("alice", UserRole.USER), ("bob", UserRole.USER), ("root", UserRole.ADMIN)]:
        store.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    return store


def _signed_in(name: str) -> Dict[str, str]:
    account = database.get_db().get_user_by_username(name)
    token = create_access_token({"sub": name, "user_id": account.id, "role": account.role.value},
                                expires_delta=timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def _client(server, auth: bool = False) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.config = AgentSystemConfig(auth=AuthConfig(enabled=auth))
    app.include_router(server.get_web_router())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _question_of(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("type") != "status":
        return None
    return (event.get("meta") or {}).get("ask_user")


def _questions(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [q for q in map(_question_of, events) if q is not None]


def _row(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every line of the ask_user call's row, in order."""
    return [e for e in events if e.get("type") == "status" and str(e.get("server", "")).startswith("ask_user.")]


def _last(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The row's last line: it must end the row (the chat takes the box down with it)."""
    row = _row(events)
    assert row, "the call left no row"
    assert row[-1]["phase"] in ("end", "error"), f"the row did not end: {row[-1]}"
    return row[-1]


async def _run(agent: Agent, request_id: str, answer=None, session_id: str = "sess-1") -> List[Dict[str, Any]]:
    """Drive the run; ``answer(question)`` is awaited once for every question it streams."""
    events = []
    asked = set()
    async for event in agent.run_events("go", request_id=request_id, session_id=session_id):
        events.append(event)
        question = _question_of(event)
        if question is not None and answer is not None and question["id"] not in asked:
            asked.add(question["id"])
            await answer(question)
    return events


@contextlib.contextmanager
def _answering_on_the_last_line(server):
    """Answers a question the moment its row's last line is published -- after the
    call stopped waiting. Yields what became of each answer: refused, not taken."""
    late: List[str] = []
    ids: Dict[str, str] = {}

    class _Answers(StatusHandler):
        async def process(self, event):
            if not str(event.server).startswith("ask_user."):
                return
            asked = (event.meta or {}).get("ask_user")
            if asked:
                ids[event.request_id] = asked["id"]
            elif event.phase in (StatusPhase.END, StatusPhase.ERROR) and event.request_id in ids:
                try:
                    server.broker.answer(ids[event.request_id], [], "too late")
                    late.append("taken")
                except AnswerRejected as exc:
                    late.append(f"refused {exc.status}")

    handler = _Answers()
    get_status_bus().add_handler(handler)
    try:
        yield late
    finally:
        get_status_bus().remove_handler(handler)


class TestAnswers:

    async def test_the_question_reaches_the_stream_and_the_answer_is_the_result(self, watched):
        server = _server()
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model

        async with _client(server) as client:
            async def pick(question):
                response = await client.post(URL, json={"question_id": question["id"],
                                                        "choices": ["SQLite"], "text": "for now"})
                assert response.status_code == 200, response.text

            events = await _run(agent, watched("ask1"), answer=pick)

        [question] = {q["id"]: q for q in _questions(events)}.values()
        assert question["question"] == "Which database?"
        assert question["options"] == ["Postgres", "SQLite"] and question["multi_select"] is False
        assert question["answer_url"] == URL and question["session_id"] == "sess-1"
        assert question["request_id"].startswith("ask1_"), "the question is not the call's"
        assert model.results() == [{"status": "success", "choices": ["SQLite"], "text": "for now"}]
        last = _last(events)
        assert last["phase"] == "end" and last["message"] == 'answered by anonymous: SQLite -- "for now"'
        assert server.broker.pending() == []

    async def test_an_open_question_takes_free_text(self, watched):
        server = _server()
        long_question = "What should the file be called? " + "Some context. " * 100
        model = _Model([_ask("c1", long_question)])
        agent = _agent(server)
        agent.llm = model

        async with _client(server) as client:
            async def write(question):
                assert question["options"] == []
                assert question["question"] == long_question.strip(), "the page lost part of the question"
                await client.post(URL, json={"question_id": question["id"], "text": "  report.md  "})

            events = await _run(agent, watched("free1"), answer=write)

        assert model.results() == [{"status": "success", "choices": [], "text": "report.md"}]
        assert _last(events)["phase"] == "end"
        assert all(len(line["message"]) <= 140 for line in _row(events)), "a row line is wider than a row"

    async def test_multi_select_takes_several_options_in_the_order_the_question_lists_them(self, watched):
        server = _server()
        model = _Model([_ask("c1", "Which targets?", ["linux", "mac", "windows"], multi_select=True)])
        agent = _agent(server)
        agent.llm = model

        async with _client(server) as client:
            async def tick(question):
                assert question["multi_select"] is True
                response = await client.post(URL, json={"question_id": question["id"],
                                                        "choices": ["windows", "linux", "windows"]})
                assert response.status_code == 200, response.text

            await _run(agent, watched("multi1"), answer=tick)

        assert model.results() == [{"status": "success", "choices": ["linux", "windows"], "text": ""}]

    async def test_an_answer_that_does_not_fit_is_refused_and_the_question_waits_on(self, watched):
        server = _server()
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model
        refused: List[int] = []

        async with _client(server) as client:
            async def try_badly_then_well(question):
                for body in ({"choices": ["Postgres", "SQLite"]},      # one option only
                             {"choices": ["MySQL"]},                   # not an option
                             {"choices": [], "text": "   "},           # nothing at all
                             {"choices": "Postgres"}):                 # not a list
                    response = await client.post(URL, json={"question_id": question["id"], **body})
                    refused.append(response.status_code)
                assert server.broker.get(question["id"]) is not None, "a refused answer used the question up"
                ok = await client.post(URL, json={"question_id": question["id"], "choices": ["Postgres"]})
                assert ok.status_code == 200, ok.text
                again = await client.post(URL, json={"question_id": question["id"], "choices": ["SQLite"]})
                refused.append(again.status_code)

            await _run(agent, watched("badanswer1"), answer=try_badly_then_well)

        assert refused == [422, 422, 422, 422, 404]
        assert model.results() == [{"status": "success", "choices": ["Postgres"], "text": ""}]

    @pytest.mark.parametrize("arguments, says", [
        ({"question": ""}, "question is required"),
        ({"question": "Which?", "options": ["only one"]}, "give 2 to 4"),
        ({"question": "Which?", "options": ["a", "b", "c", "d", "e"]}, "give 2 to 4"),
        ({"question": "Which?", "options": ["Yes", "yes"]}, "say the same"),
        ({"question": "Which?", "multi_select": True}, "multi_select needs options"),
        ({"question": "x" * 2001}, "keep it under 2000"),
    ])
    async def test_arguments_that_make_no_question_are_refused_before_asking(self, watched, arguments, says):
        server = _server()
        model = _Model([{"id": "c1", "type": "function",
                         "function": {"name": "ask_user", "arguments": json.dumps(arguments)}}])
        agent = _agent(server)
        agent.llm = model

        events = await _run(agent, watched("badargs1"))

        [result] = model.results()
        assert result["status"] == "error" and says in result["error"], result
        assert _questions(events) == []
        last = _last(events)
        assert last["phase"] == "error" and last["message"].startswith("question not asked:"), last

    async def test_a_waiting_question_is_sent_again(self, watched):
        """A page that was reloaded skips the lines it read: the question comes back."""
        server = _server(reask_seconds=0.1)
        model = _Model([_ask("c1", "Go on?", ["yes", "no"])])
        agent = _agent(server)
        agent.llm = model
        lines: List[Dict[str, Any]] = []

        async with _client(server) as client:
            async for event in agent.run_events("go", request_id=watched("reask1"), session_id="s"):
                question = _question_of(event)
                if question is not None:
                    lines.append(question)
                    if len(lines) == 3:
                        await client.post(URL, json={"question_id": question["id"], "choices": ["yes"]})

        assert len(lines) >= 3 and len({q["id"] for q in lines}) == 1
        assert model.results()[0]["choices"] == ["yes"]


    async def test_a_message_in_the_chat_ends_the_wait_and_reaches_the_model_as_the_users(self, watched):
        """The person types into the chat's main field instead of the question's box:
        a mid-run message of the run. The question does not wait out its timeout
        for an answer that already came -- the model reads the message next."""
        server = _server(ask_timeout=30)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model

        async def write_in_the_chat(question):
            assert await agent.append_user_message("wrote1", "whatever is installed already")

        started = time.monotonic()
        events = await asyncio.wait_for(_run(agent, watched("wrote1"), answer=write_in_the_chat), 15)

        assert time.monotonic() - started < 10, "the question waited out a message in the chat"
        [result] = model.results()
        assert result["replied_in_chat"] is True and result["status"] == "success"
        assert "follows as their next message" in result["note"]
        after = model.requests[-1]
        assert [m["role"] for m in after[-2:]] == ["tool", "user"], after[-2:]
        assert after[-1]["content"] == "whatever is installed already"
        last = _last(events)
        assert last["phase"] == "end" and "wrote in the chat" in last["message"]
        assert server.broker.pending() == []


    async def test_a_message_sent_while_the_model_thought_ends_the_question_at_once(self, watched):
        """The person wrote while the model was still producing the call: the model
        has not read it, and it may answer the question -- so the question ends at
        once and the model reads the message next."""
        server = _server(ask_timeout=30)
        agent = _agent(server)

        class _TypedWhileThinking(_Model):
            async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
                if not self.requests:
                    assert await agent.append_user_message("early1", "Postgres, please")
                async for chunk in super().chat_tools_streaming(messages, tools, cancellation_token, status_scope):
                    yield chunk

        model = _TypedWhileThinking([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent.llm = model

        started = time.monotonic()
        events = await asyncio.wait_for(_run(agent, watched("early1")), 15)

        assert time.monotonic() - started < 5, "the question waited although a message had come"
        [result] = model.results()
        assert result["replied_in_chat"] is True
        assert model.requests[-1][-1]["content"] == "Postgres, please"
        assert _last(events)["phase"] == "end"


class TestNobodyToAsk:

    @pytest.mark.parametrize("marked", [False, None], ids=["client-said-unattended", "never-marked"])
    async def test_a_run_nobody_watches_gets_an_error_at_once(self, watched, marked):
        """openai_api, agent-run, JSON /run, a job: the run is not marked, or marked
        unattended. The call must not wait -- ask_timeout is a minute here."""
        server = _server(ask_timeout=60)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model
        request_id = watched("headless1", attended=False) if marked is False else "headless2"

        started = time.monotonic()
        events = await asyncio.wait_for(_run(agent, request_id), 10)

        assert time.monotonic() - started < 5, "the call waited for nobody"
        [result] = model.results()
        assert result["status"] == "error" and result["reason"] == "unattended"
        assert "Decide yourself" in result["error"] and "what you assumed" in result["error"]
        assert _questions(events) == [] and server.broker.pending() == []
        assert "nobody watches this run" in _last(events)["message"]

    async def test_a_script_call_is_refused_where_the_model_would_be_asked(self, watched):
        """tool_script runs the model's script through dispatch_tool_call, with the
        run's token for its hooks. Its per-call timeout would cut a question off,
        so ask_user refuses it -- in a run a person watches, where the model's own
        call would be asked."""
        server = _server()
        agent = _agent(server)
        root = watched("script1")
        stream = StatusEventForwarder()
        await stream.start_forwarding(root)
        token = get_cancellation_manager().create_token(root)
        try:
            result = await asyncio.wait_for(agent.dispatch_tool_call(
                "ask_user", {"question": "Which database?", "options": ["Postgres", "SQLite"]},
                session_id="s", request_id=f"{root}_001_ts01", hook_source="tool_script",
                cancellation_token=token), 10)
            lines = [e for e in stream.get_pending_events() if str(e.get("server", "")).startswith("ask_user.")]
        finally:
            await stream.stop_forwarding()
            get_cancellation_manager().unregister_request(root)

        assert result["status"] == "error" and result["reason"] == "not_own_call", result
        assert server.broker.pending() == []
        assert not any((e.get("meta") or {}).get("ask_user") for e in lines)

    async def test_a_call_without_its_status_row_is_refused(self, watched):
        """The model's own call comes through call_with_status, with a row of its own
        to carry the question. A plain call() with a token -- another in-process
        caller -- has none: refused, not asked on nothing."""
        server = _server()
        root = watched("norow1")
        stream = StatusEventForwarder()
        await stream.start_forwarding(root)
        token = get_cancellation_manager().create_token(f"{root}_001_000")
        try:
            result = await asyncio.wait_for(server.call("ask_user", {
                "question": "Which database?", "_request_id": f"{root}_001", "_user_id": "alice",
                "_cancellation_token": token}), 10)
        finally:
            await stream.stop_forwarding()
            get_cancellation_manager().unregister_request(f"{root}_001_000")

        assert result["status"] == "error" and result["reason"] == "not_own_call", result
        assert server.broker.pending() == []

    async def test_a_reader_who_left_stops_the_wait(self, watched):
        """The tab was closed while the question waited: the call does not wait out
        ask_timeout for nobody."""
        server = _server(gone_after_seconds=0.3, ask_timeout=30)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model
        root = watched("gone1")
        manager = get_background_job_manager()
        done = asyncio.Event()

        async def runner():
            yield {"type": "status", "message": "working"}
            await done.wait()

        await manager.create_job(request_id=root, user_id="alice", agent_name="asking_agent",
                                 session_id=None, agent_runner=runner)
        await manager.increment_sse_client(root)

        async def tab_closed(question):
            await manager.decrement_sse_client(root)

        try:
            events = await asyncio.wait_for(_run(agent, root, answer=tab_closed), 15)
        finally:
            done.set()

        [result] = model.results()
        assert result["status"] == "error" and result["reason"] == "gone", result
        assert "nobody reads the run any more" in _last(events)["message"]
        assert server.broker.pending() == []


class TestWaiting:

    async def test_no_answer_within_the_timeout(self, watched):
        server = _server(ask_timeout=0.3)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model

        events = await _run(agent, watched("timeout1"))

        [result] = model.results()
        assert result["status"] == "error" and result["reason"] == "timeout"
        assert "No answer came within 0.3 seconds" in result["error"] and "what you assumed" in result["error"]
        assert len({q["id"] for q in _questions(events)}) == 1
        last = _last(events)
        assert last["phase"] == "error" and "no answer within 0.3 s" in last["message"]
        assert server.broker.pending() == []

    async def test_an_answer_after_the_timeout_is_refused_not_taken(self, watched):
        server = _server(ask_timeout=0.3)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model

        with _answering_on_the_last_line(server) as late:
            await _run(agent, watched("late1"))

        assert late == ["refused 404"]
        assert model.results()[0]["reason"] == "timeout"

    async def test_a_cancel_stops_the_wait_and_ends_the_row(self, watched):
        """The chat's Stop cancels the run's request -- every token under it."""
        server = _server(ask_timeout=30)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model

        async def stop(question):
            assert get_cancellation_manager().cancel_request("cancel1"), "fixture: nothing to cancel"

        started = time.monotonic()
        events = await asyncio.wait_for(_run(agent, watched("cancel1"), answer=stop), 15)

        assert time.monotonic() - started < 10, "the question waited out a cancelled run"
        last = _last(events)
        assert last["phase"] == "error" and "cancelled" in last["message"], last
        assert server.broker.pending() == []
        assert all(r.get("status") != "success" for r in model.results())

    async def test_a_wait_cut_off_from_outside_ends_the_row(self, watched):
        """The call's task is torn down while it asks (the run's teardown): the row
        does not keep its box, and an answer that comes now is refused."""
        server = _server(ask_timeout=30)
        root = watched("cutoff1")
        stream = StatusEventForwarder()
        await stream.start_forwarding(root)
        token = get_cancellation_manager().create_token(f"{root}_001_000")
        params = {"question": "Which database?", "options": ["Postgres", "SQLite"],
                  "request_id": f"{root}_001", "_request_id": f"{root}_001", "_user_id": "alice",
                  "_session_id": "s", "_cancellation_token": token}
        try:
            with _answering_on_the_last_line(server) as late:
                call = asyncio.create_task(server.call_with_status("ask_user", params))
                for _ in range(200):
                    if server.broker.pending():
                        break
                    await asyncio.sleep(0.01)
                assert server.broker.pending(), "fixture: the question was never asked"
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
            lines = [e for e in stream.get_pending_events() if str(e.get("server", "")).startswith("ask_user.")]
        finally:
            await stream.stop_forwarding()
            get_cancellation_manager().unregister_request(f"{root}_001_000")

        assert lines[-1]["phase"] == "error" and "cut off" in lines[-1]["message"], lines[-1]
        assert server.broker.pending() == []
        assert late == ["refused 404"], "an answer was taken after the wait was cut off"

    async def test_a_sub_run_asks_on_the_stream_of_the_run_above_it(self, watched):
        """A sub-agent's status lines reach its caller's stream: the question is put
        there, and answered from there."""
        server = _server()
        root = watched("subroot1")
        parent_stream = StatusEventForwarder()
        await parent_stream.start_forwarding(root)
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model
        sub_id = f"{root}_002_sub_k9x2"
        register_request_user(sub_id, "alice")
        try:
            async with _client(server) as client:
                async def answer_from_the_parent():
                    for _ in range(300):
                        for event in parent_stream.get_pending_events():
                            question = (event.get("meta") or {}).get("ask_user")
                            if question:
                                await client.post(URL, json={"question_id": question["id"], "choices": ["Postgres"]})
                                return question
                        await asyncio.sleep(0.02)
                    return None

                answering = asyncio.create_task(answer_from_the_parent())
                await _run(agent, sub_id)
                question = await answering
        finally:
            await parent_stream.stop_forwarding()

        assert question is not None, "the sub-run's question never reached the parent's stream"
        assert question["request_id"].startswith(f"{sub_id}_")
        assert model.results() == [{"status": "success", "choices": ["Postgres"], "text": ""}]


class TestSubRuns:

    async def test_a_message_to_the_watched_conversation_ends_a_sub_agents_question(self, watched, tmp_path):
        """A sub-agent (started through the sub-agent manager, on an agent of its
        own) asks in the stream of the run the person watches. The person writes
        into that conversation instead: the parent sits blocked on the sub-agent
        and nobody would read the message until the question timed out. The
        question ends, the sub-agent is told to finish with what it has, and the
        parent reads the message once the sub-agent returned. A message waiting
        for a run outside the chain ends nothing."""
        server = _server(ask_timeout=30)
        parent = _agent(server, name="parent_agent")
        registry = parent.registry
        registry.register("sam", SubAgentManagerServer(
            "sam", AgentSystemConfig(), ToolServerConfig(type="sub_agent_manager", enabled=True, allowed_agents=["*"])))
        parent.agent_config.tools.allowed = ["sam/*"]
        child = _agent(server, name="asking_child", registry=registry)
        registry.register("asking_child", child)
        service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
        parent._session_service = child._session_service = service
        child.llm = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        spawn = {"id": "p1", "type": "function", "function": {"name": "sam_manage_sub_agent", "arguments": json.dumps(
            {"operation": "create", "agent_type": "asking_child", "task": "set up the database"})}}
        parent.llm = _Model([spawn])
        root = watched("subwrote1")
        elsewhere = SessionTracker()   # another agent, running another conversation with a message waiting
        elsewhere.register_request("otherrun1", "s-other", {"appended": ["not for this run"]})
        message = "Stop -- use whatever is installed."
        asked: List[Dict[str, Any]] = []

        async def write_to_the_conversation(question):
            asked.append(question)
            await asyncio.sleep(1.5)   # more than one look of the waiting question
            assert server.broker.get(question["id"]) is not None, "a message of another run ended the question"
            assert await parent.append_user_message(root, message)

        started = time.monotonic()
        events = await asyncio.wait_for(_run(parent, root, answer=write_to_the_conversation), 20)

        assert time.monotonic() - started < 12, "the question waited out a message to the conversation"
        [question] = asked
        assert question["request_id"].startswith(f"{root}_") and "_sub_" in question["request_id"]
        [result] = child.llm.results()
        assert result["status"] == "error" and result["reason"] == "replied_above", result
        assert "main conversation" in result["error"] and "what you assumed" in result["error"]
        assert not any(m["content"] == message for request in child.llm.requests for m in request), \
            "the sub-agent read the parent's message"
        after = parent.llm.requests[-1]
        assert [m["role"] for m in after[-2:]] == ["tool", "user"] and after[-1]["content"] == message
        last = _last(events)
        assert last["phase"] == "end" and "main conversation" in last["message"], last
        assert server.broker.pending() == []


class TestWhoMayAnswer:

    async def test_the_owner_is_the_runs_user_when_the_call_names_none(self, watched):
        """No session user reached the call: the run's registered user owns the
        question (the call's own id is registered only together with a user)."""
        server = _server(ask_timeout=20)
        root = watched("owner1", "alice")
        stream = StatusEventForwarder()
        await stream.start_forwarding(root)
        token = get_cancellation_manager().create_token(f"{root}_001_000")
        params = {"question": "Which database?", "request_id": f"{root}_001", "_request_id": f"{root}_001",
                  "_session_id": "s", "_cancellation_token": token}
        call = asyncio.create_task(server.call_with_status("ask_user", params))
        try:
            for _ in range(200):
                if server.broker.pending():
                    break
                await asyncio.sleep(0.01)
            [question] = server.broker.pending()
            assert question.owner == "alice"
            server.broker.answer(question.id, [], "Postgres")
            result = await asyncio.wait_for(call, 10)
        finally:
            if not call.done():
                call.cancel()
            await stream.stop_forwarding()
            get_cancellation_manager().unregister_request(f"{root}_001_000")

        assert result == {"status": "success", "choices": [], "text": "Postgres"}

    async def _waiting(self, server, request_id: str):
        """A run of alice's waiting on its question; returns (task, question)."""
        model = _Model([_ask("c1", "Which database?", ["Postgres", "SQLite"])])
        agent = _agent(server)
        agent.llm = model
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        async def drive():
            events = []
            async for event in agent.run_events("go", request_id=request_id, session_id="auth-s"):
                events.append(event)
                question = _question_of(event)
                if question is not None and not found.done():
                    found.set_result(question)
            return model, events

        task = asyncio.create_task(drive())
        return task, await asyncio.wait_for(found, 10)

    async def test_another_user_cannot_answer_and_the_owner_can(self, watched, users):
        server = _server(ask_timeout=20)
        task, question = await self._waiting(server, watched("auth1", "alice"))
        body = {"question_id": question["id"], "choices": ["SQLite"]}
        async with _client(server, auth=True) as client:
            foreign = await client.post(URL, headers=_signed_in("bob"), json=body)
            listed = await client.get("/plugins/ask_user/pending", headers=_signed_in("bob"))
            assert foreign.status_code == 403, foreign.text
            assert listed.json()["count"] == 0, "another user's question was listed"
            assert server.broker.get(question["id"]) is not None, "a refused answer used the question up"
            mine = await client.get("/plugins/ask_user/pending?request_id=auth1", headers=_signed_in("alice"))
            assert [q["id"] for q in mine.json()["questions"]] == [question["id"]]
            assert mine.json()["questions"][0]["question"] == "Which database?"
            own = await client.post(URL, headers=_signed_in("alice"), json=body)
            assert own.status_code == 200, own.text
        model, events = await asyncio.wait_for(task, 10)
        assert model.results() == [{"status": "success", "choices": ["SQLite"], "text": ""}]
        assert _last(events)["message"] == "answered by alice: SQLite"

    async def test_an_admin_may_answer(self, watched, users):
        server = _server(ask_timeout=20)
        task, question = await self._waiting(server, watched("auth2", "alice"))
        async with _client(server, auth=True) as client:
            response = await client.post(URL, headers=_signed_in("root"),
                                         json={"question_id": question["id"], "text": "use Postgres"})
        assert response.status_code == 200, response.text
        model, _ = await asyncio.wait_for(task, 10)
        assert model.results()[0]["text"] == "use Postgres"

    async def test_neither_an_api_key_nor_nobody_may_answer(self, watched, users):
        server = _server(ask_timeout=20)
        task, question = await self._waiting(server, watched("auth3", "alice"))
        key = users.generate_user_api_key(users.get_user_by_username("alice").id)
        assert key, "fixture: no API key"
        try:
            async with _client(server, auth=True) as client:
                body = {"question_id": question["id"], "choices": ["SQLite"]}
                by_key = await client.post(URL, headers={"X-API-Key": key}, json=body)
                by_bearer_key = await client.post(URL, headers={"Authorization": f"Bearer {key}"}, json=body)
                anonymous = await client.post(URL, json=body)
            assert (by_key.status_code, by_bearer_key.status_code, anonymous.status_code) == (401, 401, 401)
            assert server.broker.get(question["id"]) is not None
        finally:
            if server.broker.get(question["id"]) is not None:
                server.broker.answer(question["id"], ["Postgres"], "")
            await asyncio.wait_for(task, 10)
