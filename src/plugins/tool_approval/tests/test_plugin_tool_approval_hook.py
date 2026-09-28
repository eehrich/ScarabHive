"""tool_approval through the real path.

The plugin is built by its factory and registered from its own schema.yaml by
``register_plugin_hooks``, as the app does; the calls come from a real
``Agent.run_events`` (only the LLM is scripted) or from ``dispatch_tool_call``;
answers go through the plugin's HTTP route. What is pinned:

* the modes and rules decide as documented (deny wins, auto runs the rest,
  ask runs allow rules and asks the rest);
* a question reaches the run's stream, the answer endpoint decides the call,
  and only the run's user or an admin may give it -- signed in, not by API key;
* unanswered questions time out, a cancel stops the wait, and neither runs the
  call; the question row always ends, so the page takes its buttons down;
* "allow for this session" holds for that session's later calls, and nothing
  more;
* a run nobody watches -- no client said so, or the stream that would carry the
  question has ended -- gets the ``unattended`` answer;
* an agent that did not switch the hook on pays nothing.
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
    HooksConfig as AgentHooksConfig,
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
from agent_system.hooks import HooksConfig, HookType
from agent_system.hooks.registry import get_hook_registry
from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder, attended_stream_of
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import get_background_job_manager
from agent_system.tools.base import ToolServer, ToolServerRegistry
from agent_system.tools.status import StatusHandler, StatusPhase, get_status_bus
from plugins.tool_approval.broker import AnswerRejected
from plugins.tool_approval.plugin import PLUGIN_FACTORY

HOOK = "tool_approval.check_tool_call"


class _Probe(ToolServer):
    """A real tool server with two tools; records every call that ran."""

    def __init__(self):
        super().__init__("probe", AgentSystemConfig(), ToolServerConfig(type="probe", enabled=True))
        self.received: List[Dict[str, Any]] = []

    def get_tools(self):
        return [{"type": "function", "function": {
            "name": name, "description": "Probe.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}
            for name in ("probe_echo", "probe_wipe")]

    async def probe_echo(self, params):
        self.received.append({"tool": "probe_echo", "text": params.get("text")})
        return {"status": "ok", "echo": params.get("text")}

    async def probe_wipe(self, params):
        self.received.append({"tool": "probe_wipe", "text": params.get("text")})
        return {"status": "ok", "wiped": params.get("text")}


def _call(call_id: str, text: str, tool: str = "probe_echo") -> Dict[str, Any]:
    return {"id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps({"text": text})}}


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


def _agent(probe: _Probe, override: Optional[Dict[str, Any]], name: str = "approval_agent",
           registry: Optional[ToolServerRegistry] = None) -> Agent:
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
    if registry is None:
        registry = ToolServerRegistry()
        registry.register("probe", probe)
    # A question nobody in the test answers ends in seconds, not after the default five
    # minutes: a mutation that asks where it should not fails fast instead of hanging.
    hooks = AgentHooksConfig(overrides={HOOK: {"enabled": True, "ask_timeout": 5, **override}}
                             if override is not None else {})
    agent_config = AgentConfig(max_steps=6, llm_profile="normal", tools=ToolConfig(allowed=["probe/*"]), hooks=hooks)
    return Agent(name, AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), registry)


@pytest.fixture
async def approval():
    """install(instance_config=None, **global_override) -> the plugin, registered as the app registers it."""
    registry = get_hook_registry()
    names: List[str] = []

    async def install(instance_config: Optional[Dict[str, Any]] = None, **override):
        plugin = PLUGIN_FACTORY("tool_approval", AgentSystemConfig(),
                                ToolServerConfig(type="tool_approval", enabled=True, config=instance_config or {}))
        # the operator's global hooks.overrides, as load_hooks_config hands them on
        hooks_config = HooksConfig(overrides={HOOK: override} if override else {})
        names.extend(await register_plugin_hooks("tool_approval", plugin, plugin.get_schema_data(),
                                                 registry, hooks_config))
        assert names == [HOOK], names
        return plugin

    yield install
    for name in names:
        await registry.unregister_hook(HookType.PRE_TOOL_CALL, name)


@pytest.fixture
def watched():
    """watch(request_id, user) -- a run the web chat started for ``user``: attended, and owned."""
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
    """A user store of this test's own: alice and bob, root the admin; alice has an API key."""
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


def _client(plugin, auth: bool = False) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.config = AgentSystemConfig(auth=AuthConfig(enabled=auth))
    app.include_router(plugin.get_web_router())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@contextlib.contextmanager
def _answering_on_the_last_line(plugin):
    """Answers a question the moment its row's last line is published -- after
    the hook stopped waiting, while it writes that line. Yields what became of
    each answer: it must be refused, not taken for a call already decided."""
    late: List[str] = []

    class _Answers(StatusHandler):
        async def process(self, event):
            if event.server == "tool_approval" and event.phase in (StatusPhase.END, StatusPhase.ERROR):
                try:
                    plugin.broker.answer(event.request_id.rsplit("_", 1)[-1], "allow_once")
                    late.append("taken")
                except AnswerRejected as exc:
                    late.append(f"refused {exc.status}")

    handler = _Answers()
    get_status_bus().add_handler(handler)
    try:
        yield late
    finally:
        get_status_bus().remove_handler(handler)


async def _job(request_id: str, readers: int, unread_for: float = 0.0) -> asyncio.Event:
    """A job under ``request_id`` with ``readers`` streams on it, as /events keeps
    one for a run -- without any, unread for the last ``unread_for`` seconds; it
    ends when the returned event is set."""
    done = asyncio.Event()

    async def runner():
        yield {"type": "status", "message": "working"}
        await done.wait()

    manager = get_background_job_manager()
    await manager.create_job(request_id=request_id, user_id="alice", agent_name="approval_agent",
                             session_id=None, agent_runner=runner)
    for _ in range(readers):
        await manager.increment_sse_client(request_id)
    job = await manager.get_job(request_id)
    if not readers:
        job.unread_since = time.monotonic() - unread_for
    assert job.sse_client_count == readers, "fixture: the job's readers"
    return done


def _question_of(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("type") != "status":
        return None
    return (event.get("meta") or {}).get("tool_approval")


def _approval_lines(events: List[Dict[str, Any]], phase: str) -> List[Dict[str, Any]]:
    return [e for e in events if e.get("type") == "status" and e.get("server") == "tool_approval"
            and e.get("phase") == phase]


async def _run(agent: Agent, request_id: str, session_id: str = "sess-1", answer=None) -> List[Dict[str, Any]]:
    """Drive the run; ``answer(question, event)`` is awaited for every question line it streams."""
    events = []
    asked = set()
    async for event in agent.run_events("go", request_id=request_id, session_id=session_id):
        events.append(event)
        question = _question_of(event)
        if question is not None and answer is not None and question["id"] not in asked:
            asked.add(question["id"])
            await answer(question, event)
    return events


def _questions(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [q for q in map(_question_of, events) if q is not None]


class TestRules:

    async def test_off_lets_every_call_run(self, approval):
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "off", "deny": ["probe/*"]})
        agent.llm = _Model([_call("c1", "one")])

        await _run(agent, "offrun1")

        assert [r["text"] for r in probe.received] == ["one"]

    async def test_a_bare_yaml_off_means_off(self, approval):
        """YAML 1.1 reads `mode: off` as false; blocking every call would be the
        opposite of what it says."""
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": False, "deny": ["probe/*"]})
        agent.llm = _Model([_call("c1", "one")])

        await _run(agent, "offyaml1")

        assert [r["text"] for r in probe.received] == ["one"]

    async def test_the_model_does_not_read_the_argument_pattern(self, approval):
        """A regex in the result is a recipe for the call that slips past it."""
        await approval()
        probe = _Probe()
        model = _Model([_call("c1", "rm -rf /", "probe_wipe")])
        agent = _agent(probe, {"mode": "auto",
                               "deny": [{"tool": "probe/*", "arguments": {"text": r"\brm\s+-[a-z]*r"}}]})
        agent.llm = model

        await _run(agent, "noregex1")

        error = model.results()[0]["error"]
        assert probe.received == [] and "probe/*" in error
        assert "rm" not in error.replace("probe_wipe", ""), error
        assert "do not try to get the same effect" in error

    async def test_auto_blocks_what_a_deny_rule_names_and_runs_the_rest(self, approval):
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "auto",
                               "deny": [{"tool": "probe/probe_wipe", "arguments": {"text": "^/etc"}}]})
        model = _Model([_call("c1", "/etc/hosts", "probe_wipe"), _call("c2", "/tmp/x", "probe_wipe"),
                        _call("c3", "/etc", "probe_echo")])
        agent.llm = model

        events = await _run(agent, "autorun1")

        assert probe.received == [{"tool": "probe_wipe", "text": "/tmp/x"}, {"tool": "probe_echo", "text": "/etc"}]
        blocked = model.results()[0]
        assert blocked["type"] == "ToolCallBlocked"
        assert "probe/probe_wipe" in blocked["error"] and "Do not send it again" in blocked["error"]
        assert "^/etc" not in blocked["error"], "the model read the argument pattern"
        assert [e["blocked"] for e in events if e.get("type") == "tool_error"] == [True]

    async def test_ask_runs_what_an_allow_rule_names_without_asking(self, approval, watched):
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"allow": ["probe/probe_echo"]})
        agent.llm = _Model([_call("c1", "one")])

        events = await _run(agent, watched("allowrun1"))

        assert [r["text"] for r in probe.received] == ["one"]
        assert _questions(events) == [], "a call an allow rule names was asked about"

    async def test_deny_wins_over_allow(self, approval):
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "ask", "allow": ["probe/*"], "deny": ["probe/probe_wipe"]})
        agent.llm = _Model([_call("c1", "x", "probe_wipe"), _call("c2", "y")])

        await _run(agent, "denyallow1")

        assert probe.received == [{"tool": "probe_echo", "text": "y"}]

    async def test_the_instance_rules_hold_and_an_agent_adds_to_them(self, approval):
        """The instance's deny rule cannot be lifted by an agent's allow rule;
        the agent's own deny rule adds to the instance's."""
        await approval({"mode": "auto", "deny": ["probe/probe_wipe"]})
        probe = _Probe()
        agent = _agent(probe, {"allow": ["probe/*"], "deny": [{"tool": "probe/*", "arguments": {"text": "secret"}}]})
        agent.llm = _Model([_call("c1", "x", "probe_wipe"), _call("c2", "secret"), _call("c3", "fine")])

        await _run(agent, "instance1")

        assert probe.received == [{"tool": "probe_echo", "text": "fine"}]

    async def test_a_rule_that_cannot_be_read_blocks_the_call(self, approval):
        await approval()
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {"mode": "auto", "deny": [{"tool": "probe/*", "arguments": {"text": "("}}]})
        agent.llm = model

        await _run(agent, "badrule1")

        assert probe.received == []
        assert "cannot be read" in model.results()[0]["error"]


class TestUnattended:

    async def test_a_run_nobody_watches_blocks_what_it_would_ask(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {})
        agent.llm = model

        # owned, but started by a client that shows no questions (openai_api, the writer)
        events = await _run(agent, watched("headless1", attended=False))

        assert probe.received == []
        error = model.results()[0]["error"]
        assert "nobody is watching" in error and "tell the user" in error
        assert _questions(events) == [] and plugin.broker.pending() == []

    async def test_unattended_allow_lets_it_run(self, approval):
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"unattended": "allow"})
        agent.llm = _Model([_call("c1", "one")])

        await _run(agent, "headless2")

        assert [r["text"] for r in probe.received] == ["one"]

    async def test_a_stream_that_ended_asks_nobody(self, watched):
        """An async sub-agent that outlives the run above it: the mark alone does not make it attended."""
        root = watched("rootgone1")
        forwarder = StatusEventForwarder()
        await forwarder.start_forwarding(root)
        assert attended_stream_of(f"{root}_003_async_ab12") == root
        await forwarder.stop_forwarding()

        assert attended_stream_of(f"{root}_003_async_ab12") is None
        assert attended_stream_of(root) is None

    async def test_a_run_whose_job_nobody_reads_asks_nobody(self, approval, watched):
        """The tab was closed: the job runs on, marked, with no reader. A call is
        not put to nobody for ask_timeout seconds -- it gets the unattended answer."""
        plugin = await approval()
        root = watched("noreader1")
        job_done = await _job(root, readers=0, unread_for=60)
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {})
        agent.llm = model
        try:
            events = await _run(agent, root)
        finally:
            job_done.set()

        assert probe.received == [] and "nobody is watching" in model.results()[0]["error"]
        assert _questions(events) == [] and plugin.broker.pending() == []

    async def test_a_question_whose_reader_left_stops_waiting(self, approval, watched):
        plugin = await approval()
        root = watched("reader1")
        job_done = await _job(root, readers=1)
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {"gone_after_seconds": 0.3, "ask_timeout": 20})
        agent.llm = model

        async def tab_closed(question, event):
            await get_background_job_manager().decrement_sse_client(root)

        try:
            events = await asyncio.wait_for(_run(agent, root, answer=tab_closed), 10)
        finally:
            job_done.set()

        assert probe.received == []
        assert "nobody is watching" in model.results()[0]["error"]
        assert "nobody reads the run any more" in _approval_lines(events, "error")[0]["message"]
        assert plugin.broker.pending() == []

    async def test_a_call_during_a_reload_is_asked(self, approval, watched):
        """The page reloads: for a moment no stream reads the job. A call then is
        asked -- not blocked as if the tab were closed -- and the reloaded page
        answers it."""
        plugin = await approval()
        root = watched("reloading1")
        job_done = await _job(root, readers=0, unread_for=0.5)
        probe = _Probe()
        agent = _agent(probe, {"gone_after_seconds": 5, "ask_timeout": 20})
        agent.llm = _Model([_call("c1", "one")])

        async def reloaded_and_answers(question, event):
            await get_background_job_manager().increment_sse_client(root)
            plugin.broker.answer(question["id"], "allow_once")

        try:
            events = await asyncio.wait_for(_run(agent, root, answer=reloaded_and_answers), 10)
        finally:
            job_done.set()

        assert len(_questions(events)) >= 1
        assert [r["text"] for r in probe.received] == ["one"]

    async def test_a_reload_within_the_grace_keeps_the_question(self, approval, watched):
        plugin = await approval()
        root = watched("reload1")
        job_done = await _job(root, readers=1)
        probe = _Probe()
        agent = _agent(probe, {"gone_after_seconds": 5, "ask_timeout": 20})
        agent.llm = _Model([_call("c1", "one")])
        manager = get_background_job_manager()

        async def reload_then_answer(question, event):
            await manager.decrement_sse_client(root)
            await asyncio.sleep(1.2)   # more than one reader poll, less than the grace
            await manager.increment_sse_client(root)
            plugin.broker.answer(question["id"], "allow_once")

        try:
            await asyncio.wait_for(_run(agent, root, answer=reload_then_answer), 10)
        finally:
            job_done.set()

        assert [r["text"] for r in probe.received] == ["one"]

    async def test_only_a_marked_stream_at_or_above_the_id_counts(self, watched):
        watched("rootmark1")
        other = StatusEventForwarder()
        await other.start_forwarding("rootmark1")
        unmarked = StatusEventForwarder()
        await unmarked.start_forwarding("rootplain1")
        try:
            assert attended_stream_of("rootmark1_001_ts01") == "rootmark1"
            assert attended_stream_of("rootmark10") is None, "a prefix without '_' was taken for the run"
            assert attended_stream_of("rootplain1_001") is None
            assert attended_stream_of("") is None
        finally:
            await other.stop_forwarding()
            await unmarked.stop_forwarding()


class TestAsking:

    async def test_allow_once_runs_the_call_and_the_next_one_is_asked_again(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {})
        agent.llm = _Model([_call("c1", "one")], [_call("c2", "two")])

        async with _client(plugin) as client:
            async def allow(question, event):
                assert probe.received == [] or question["arguments"] == 'text: "two"', "ran before its answer"
                response = await client.post("/plugins/tool_approval/answer",
                                             json={"question_id": question["id"], "decision": "allow_once"})
                assert response.status_code == 200, response.text

            events = await _run(agent, watched("once1"), answer=allow)

        assert [r["text"] for r in probe.received] == ["one", "two"]
        questions = _questions(events)
        assert [q["arguments"] for q in questions] == ['text: "one"', 'text: "two"']
        assert questions[0]["answer_url"] == "/plugins/tool_approval/answer"
        assert questions[0]["request_id"] == "once1" and questions[0]["session_id"] == "sess-1"
        ends = _approval_lines(events, "end")
        assert len(ends) == 2 and all("allowed once by anonymous" in e["message"] for e in ends)
        assert plugin.broker.pending() == []

    async def test_deny_blocks_and_the_model_reads_the_reason(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {})
        agent.llm = model

        async with _client(plugin) as client:
            async def deny(question, event):
                await client.post("/plugins/tool_approval/answer", json={
                    "question_id": question["id"], "decision": "deny", "reason": "use the staging copy"})

            events = await _run(agent, watched("deny1"), answer=deny)

        assert probe.received == []
        error = model.results()[0]["error"]
        assert "The user denied" in error and "use the staging copy" in error and "Do not send it again" in error
        assert [e.get("blocked") for e in events if e.get("type") == "tool_error"] == [True]
        assert "denied by anonymous" in _approval_lines(events, "error")[0]["message"]

    async def test_allow_for_the_session_holds_for_that_session_only(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {"deny": [{"tool": "probe/*", "arguments": {"text": "never"}}]})
        agent.llm = _Model([_call("c1", "one")], [_call("c2", "two"), _call("c3", "never")])

        async with _client(plugin) as client:
            async def for_the_session(question, event):
                await client.post("/plugins/tool_approval/answer",
                                  json={"question_id": question["id"], "decision": "allow_session"})

            first = await _run(agent, watched("sess1run"), session_id="sess-A", answer=for_the_session)
            assert [r["text"] for r in probe.received] == ["one", "two"]
            assert len(_questions(first)) == 1, "the second call of an allowed tool was asked again"

            # another session of the same agent is asked again
            agent.llm = _Model([_call("c4", "four")])
            asked: List[str] = []

            async def record_and_allow(question, event):
                asked.append(question["arguments"])
                await client.post("/plugins/tool_approval/answer",
                                  json={"question_id": question["id"], "decision": "allow_once"})

            await _run(agent, watched("sess2run"), session_id="sess-B", answer=record_and_allow)

        assert asked == ['text: "four"']
        assert "never" not in [r["text"] for r in probe.received], "the grant lifted a deny rule"

    async def test_a_message_in_the_chat_approves_nothing(self, approval, watched):
        """While an approval waits, the person types into the chat: a mid-run message
        for the model, not an answer. ask_user ends its wait on such a message (the
        shared put_to_person takes an ``interrupt``); an approval must not -- the call
        neither runs nor settles until a button answers."""
        plugin = await approval()
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {"ask_timeout": 20})
        agent.llm = model
        root = watched("chatmsg1")

        async def write_then_deny(question, event):
            assert await agent.append_user_message(root, "sure, go ahead")
            await asyncio.sleep(1.5)   # more than one look of the waiting question
            assert [q.id for q in plugin.broker.pending()] == [question["id"]], "a chat message settled the approval"
            assert probe.received == [], "a chat message let the call run"
            plugin.broker.answer(question["id"], "deny")

        await asyncio.wait_for(_run(agent, root, answer=write_then_deny), 15)

        assert probe.received == []
        assert "The user denied" in model.results()[0]["error"]

    async def test_a_waiting_question_is_sent_again(self, approval, watched):
        """A page that was reloaded skips the lines it read: the question comes back."""
        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {"reask_seconds": 0.1})
        agent.llm = _Model([_call("c1", "one")])
        lines: List[Dict[str, Any]] = []

        async with _client(plugin) as client:
            events = []
            async for event in agent.run_events("go", request_id=watched("reask1"), session_id="s"):
                events.append(event)
                question = _question_of(event)
                if question is not None:
                    lines.append(event)
                    if len(lines) == 3:
                        await client.post("/plugins/tool_approval/answer",
                                          json={"question_id": question["id"], "decision": "allow_once"})

        assert len(lines) >= 3 and len({line["meta"]["tool_approval"]["id"] for line in lines}) == 1
        assert [r["text"] for r in probe.received] == ["one"]

    async def test_an_unanswered_question_times_out_and_the_call_does_not_run(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {"ask_timeout": 0.3})
        agent.llm = model

        events = await _run(agent, watched("timeout1"))

        assert probe.received == []
        assert "nobody answered within 0.3 seconds" in model.results()[0]["error"]
        assert len(_questions(events)) == 1
        assert "no answer within 0.3 s" in _approval_lines(events, "error")[0]["message"]
        assert plugin.broker.pending() == []

    async def test_an_answer_after_the_timeout_is_refused_not_taken(self, approval, watched):
        """The row's last line is written after the wait is over: an answer that
        arrives while it is written must be refused, not reported as taken for a
        call that was already blocked."""
        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {"ask_timeout": 0.3})
        agent.llm = _Model([_call("c1", "one")])
        with _answering_on_the_last_line(plugin) as late:
            await _run(agent, watched("late1"))

        assert late == ["refused 404"]
        assert probe.received == []

    async def test_a_cancel_while_asking_reports_the_call_cancelled(self, approval, watched):
        plugin = await approval()
        probe = _Probe()
        agent = _agent(probe, {})
        agent.llm = _Model([_call("c1", "one")])

        async def cancel(question, event):
            token = get_cancellation_manager().get_token("cancel1")
            assert token is not None, "fixture: the run has no token"
            token.cancel()

        events = await _run(agent, watched("cancel1"), answer=cancel)

        assert probe.received == []
        assert any(e.get("type") == "tool_cancelled" for e in events)
        assert not [e for e in events if e.get("blocked")], "a cancelled call was reported blocked"
        assert "cancelled while asking" in _approval_lines(events, "error")[0]["message"]
        assert plugin.broker.pending() == []

    async def test_a_question_ends_before_the_hooks_own_timeout(self, approval, watched):
        """The operator set the hook's timeout under ask_timeout: the question ends
        short of it, with this hook's words -- not cut off by the registry, which
        would block with the framework's generic text."""
        plugin = await approval(timeout=0.5)
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {"ask_timeout": 30})
        agent.llm = model

        events = await _run(agent, watched("cutoff1"))

        assert probe.received == []
        assert "nobody answered within 0.45 seconds" in model.results()[0]["error"]
        assert "no answer within 0.45 s" in _approval_lines(events, "error")[0]["message"]
        assert plugin.broker.pending() == []

    async def test_a_wait_cut_off_from_outside_ends_the_row_and_blocks(self, approval, watched):
        """The run is torn down while its hook asks (a cancelled task, a client
        gone): the call does not run, the row does not keep its buttons, and
        an answer that comes now is refused."""
        plugin = await approval()
        root = watched("cutoff2")
        stream = StatusEventForwarder()
        await stream.start_forwarding(root)
        probe = _Probe()
        agent = _agent(probe, {})
        call = {"id": "c1", "name": "probe_echo", "server": "probe", "arguments": {"text": "x"}, "source": "model"}
        try:
            with _answering_on_the_last_line(plugin) as late:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(agent._hook_manager.execute_pre_tool_hooks(
                        call, step=1, request_id=root, session_id="s"), 0.3)
            lines = [e for e in stream.get_pending_events() if e.get("server") == "tool_approval"]
        finally:
            await stream.stop_forwarding()

        assert probe.received == []
        assert [e["phase"] for e in lines][-1] == "error" and "cut off" in lines[-1]["message"]
        assert plugin.broker.pending() == []
        assert late == ["refused 404"], "an answer was taken after the wait was cut off"

    async def test_a_crash_in_the_hook_blocks_the_call(self, approval, watched, monkeypatch):
        """on_error: block -- a policy hook that fails must not let the call run."""
        plugin = await approval()
        probe = _Probe()
        model = _Model([_call("c1", "one")])
        agent = _agent(probe, {})
        agent.llm = model

        def broken(**kwargs):
            raise RuntimeError("broker down")

        monkeypatch.setattr(plugin.broker, "open", broken)
        await _run(agent, watched("crash1"))

        assert probe.received == []
        assert model.results()[0]["type"] == "ToolCallBlocked"

    async def test_a_sub_run_asks_on_the_stream_of_the_run_above_it(self, approval, watched):
        """A sub-agent's status lines reach its caller's stream (the id prefix):
        the question is put there, and answered from there."""
        plugin = await approval()
        root = watched("subroot1")
        parent_stream = StatusEventForwarder()
        await parent_stream.start_forwarding(root)
        probe = _Probe()
        agent = _agent(probe, {})
        agent.llm = _Model([_call("c1", "one")])
        sub_id = f"{root}_002_sub_k9x2"
        register_request_user(sub_id, "alice")
        try:
            async with _client(plugin) as client:
                async def answer_from_the_parent():
                    for _ in range(200):
                        for event in parent_stream.get_pending_events():
                            question = (event.get("meta") or {}).get("tool_approval")
                            if question:
                                await client.post("/plugins/tool_approval/answer", json={
                                    "question_id": question["id"], "decision": "allow_once"})
                                return question
                        await asyncio.sleep(0.02)
                    return None

                answering = asyncio.create_task(answer_from_the_parent())
                await _run(agent, sub_id)
                question = await answering
        finally:
            await parent_stream.stop_forwarding()

        assert question is not None, "the sub-run's question never reached the parent's stream"
        assert question["request_id"] == sub_id
        assert [r["text"] for r in probe.received] == ["one"]


class TestWhoMayAnswer:

    async def _ask(self, plugin, request_id: str, owner: str):
        """A run of ``owner`` waiting on its question; returns (task, question)."""
        probe = _Probe()
        agent = _agent(probe, {"ask_timeout": 20})
        agent.llm = _Model([_call("c1", "one")])
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        async def drive():
            events = []
            async for event in agent.run_events("go", request_id=request_id, session_id="auth-s"):
                events.append(event)
                question = _question_of(event)
                if question is not None and not found.done():
                    found.set_result(question)
            return probe, events

        task = asyncio.create_task(drive())
        return task, await asyncio.wait_for(found, 10)

    async def test_another_user_cannot_answer_and_the_owner_can(self, approval, watched, users):
        plugin = await approval()
        task, question = await self._ask(plugin, watched("auth1", "alice"), "alice")
        async with _client(plugin, auth=True) as client:
            foreign = await client.post("/plugins/tool_approval/answer", headers=_signed_in("bob"),
                                        json={"question_id": question["id"], "decision": "allow_once"})
            listed = await client.get("/plugins/tool_approval/pending", headers=_signed_in("bob"))
            assert foreign.status_code == 403, foreign.text
            assert listed.json()["count"] == 0, "another user's question was listed"
            assert plugin.broker.get(question["id"]) is not None, "a refused answer used the question up"

            mine = await client.get("/plugins/tool_approval/pending", headers=_signed_in("alice"))
            assert [q["id"] for q in mine.json()["questions"]] == [question["id"]]
            for query, count in [("request_id=auth1", 1), ("request_id=auth", 0), ("session_id=auth-s", 1),
                                 ("session_id=other", 0)]:
                narrowed = await client.get(f"/plugins/tool_approval/pending?{query}", headers=_signed_in("alice"))
                assert narrowed.json()["count"] == count, query
            own = await client.post("/plugins/tool_approval/answer", headers=_signed_in("alice"),
                                    json={"question_id": question["id"], "decision": "allow_once"})
            assert own.status_code == 200, own.text
            again = await client.post("/plugins/tool_approval/answer", headers=_signed_in("alice"),
                                      json={"question_id": question["id"], "decision": "deny"})
            assert again.status_code == 404, "a question was answered twice"
        probe, events = await asyncio.wait_for(task, 10)
        assert [r["text"] for r in probe.received] == ["one"]
        assert "allowed once by alice" in _approval_lines(events, "end")[0]["message"]

    async def test_an_admin_may_answer(self, approval, watched, users):
        plugin = await approval()
        task, question = await self._ask(plugin, watched("auth2", "alice"), "alice")
        async with _client(plugin, auth=True) as client:
            response = await client.post("/plugins/tool_approval/answer", headers=_signed_in("root"),
                                         json={"question_id": question["id"], "decision": "deny"})
        assert response.status_code == 200, response.text
        probe, _ = await asyncio.wait_for(task, 10)
        assert probe.received == []

    async def test_neither_an_api_key_nor_nobody_may_answer(self, approval, watched, users):
        plugin = await approval()
        task, question = await self._ask(plugin, watched("auth3", "alice"), "alice")
        key = users.generate_user_api_key(users.get_user_by_username("alice").id)
        assert key, "fixture: no API key"
        try:
            async with _client(plugin, auth=True) as client:
                body = {"question_id": question["id"], "decision": "allow_once"}
                by_key = await client.post("/plugins/tool_approval/answer", headers={"X-API-Key": key}, json=body)
                by_bearer_key = await client.post("/plugins/tool_approval/answer",
                                                  headers={"Authorization": f"Bearer {key}"}, json=body)
                anonymous = await client.post("/plugins/tool_approval/answer", json=body)
            assert (by_key.status_code, by_bearer_key.status_code, anonymous.status_code) == (401, 401, 401)
            assert plugin.broker.get(question["id"]) is not None
        finally:
            if plugin.broker.get(question["id"]) is not None:
                plugin.broker.answer(question["id"], "deny")
            await asyncio.wait_for(task, 10)

    async def test_a_decision_that_does_not_exist_is_refused(self, approval, watched):
        plugin = await approval()
        task, question = await self._ask(plugin, watched("auth4", "alice"), "alice")
        async with _client(plugin) as client:
            response = await client.post("/plugins/tool_approval/answer",
                                         json={"question_id": question["id"], "decision": "sure"})
            assert response.status_code == 422, response.text
            assert plugin.broker.get(question["id"]) is not None
            await client.post("/plugins/tool_approval/answer",
                              json={"question_id": question["id"], "decision": "deny"})
        await asyncio.wait_for(task, 10)


class TestOtherCallers:

    async def test_a_script_call_is_decided_too(self, approval):
        """tool_script's calls pass the same hooks (dispatch_tool_call with a hook_source)."""
        await approval()
        probe = _Probe()
        agent = _agent(probe, {"mode": "auto", "deny": ["probe/probe_wipe"]})

        with pytest.raises(ToolDispatchError, match="not allowed for this agent"):
            await agent.dispatch_tool_call("probe_wipe", {"text": "x"}, session_id="s", request_id="script1_001_ts01",
                                           hook_source="tool_script")
        result = await agent.dispatch_tool_call("probe_echo", {"text": "y"}, session_id="s",
                                                request_id="script1_001_ts02", hook_source="tool_script")

        assert result["echo"] == "y" and probe.received == [{"tool": "probe_echo", "text": "y"}]

    async def test_an_agent_that_did_not_switch_it_on_pays_nothing(self, approval, monkeypatch):
        """The hook is registered as the app registers it, enabled nowhere: the
        call runs, and no hook context is even built."""
        plugin = await approval({"mode": "auto", "deny": ["probe/*"]})
        probe = _Probe()
        agent = _agent(probe, None)
        agent.llm = _Model([_call("c1", "one")])
        asked = []
        monkeypatch.setattr(plugin, "check_tool_call", lambda context: asked.append(context))

        await _run(agent, "defaultoff1")

        assert [r["text"] for r in probe.received] == ["one"]
        assert asked == []
        assert agent._hook_manager.wants_hooks(HookType.PRE_TOOL_CALL) is False
