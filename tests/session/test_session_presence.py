"""Session presence and waking (core/session_presence.py).

The rules against a temp sessions directory. A holder in another process is a
real process, and its crash a real kill; the agent loop's holds go through a
real Agent with a fake LLM. Only the detached start of a woken run is
replaced -- a test must not start agent runs.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from unittest.mock import AsyncMock

import psutil
import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
    SessionPresenceConfig,
)
from agent_system.core import session_presence as sp
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager

USER = "cli_user"

HOLDER = """
import sys, time
from agent_system.core.session_presence import SessionPresence
print(SessionPresence(sys.argv[1]).hold(sys.argv[2], sys.argv[3], "agent_b"), flush=True)
time.sleep(120)
"""


@pytest.fixture
def store(tmp_path):
    return sp.SessionPresence(tmp_path)


@pytest.fixture
def spawned(monkeypatch):
    """Wakes that would have started a run. The fake names this live process,
    so the wake counts as under way afterwards."""
    calls = []

    def fake_spawn(session_id, user_id, depth):
        calls.append((session_id, user_id, depth))
        return os.getpid(), psutil.Process().create_time()

    monkeypatch.setattr(sp, "spawn_wake", fake_spawn)
    return calls


@pytest.fixture
def other_process(tmp_path):
    """hold(session_id) holds the session from a separate process; kill() is its crash."""
    processes = []

    def hold(session_id, user_id=USER):
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(tmp_path), session_id, user_id],
            stdout=subprocess.PIPE, text=True)
        processes.append(process)
        assert process.stdout.readline().strip() == "True", "the other process could not hold it"
        return process

    yield hold
    for process in processes:
        process.kill()
        process.wait()


def _stored(root, session_id, user_id=USER, parent=""):
    """A session file, as a run leaves it behind; with ``parent`` one a
    sub-agent was spawned into (SessionManager.create_session writes that)."""
    (root / user_id).mkdir(parents=True, exist_ok=True)
    content = {"parent_session": {"session_id": parent}} if parent else {}
    (root / user_id / f"{session_id}.json").write_text(json.dumps(content), encoding="utf-8")


def _eventually(check, timeout=10.0):
    # Windows lets go of a killed process's locks a moment after it is gone.
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "not reached in time"
        time.sleep(0.05)


class TestHolding:
    def test_a_session_another_process_holds_counts_as_running(self, store, other_process):
        other_process("sb")

        assert store.get("sb", USER) == {"status": "running", "agent": "agent_b", "sub_agent": False}
        assert store.list_for_user(USER) == [{"session_id": "sb", "status": "running", "agent": "agent_b"}]
        with pytest.raises(sp.SessionBusy) as refused:
            store.hold("sb", USER, "agent_x")
        assert "agent_b" in str(refused.value), "the refusal does not say who runs it"

    def test_status_answers_about_the_lock_without_reading_the_session(
            self, store, other_process, tmp_path, monkeypatch):
        """The sub-agent list asks this per sub-agent before every LLM call, and
        a sub-agent's session file is its whole transcript: get() parses it."""
        _stored(tmp_path, "sb")
        _stored(tmp_path, "nobody_holds_it")
        other_process("sb")
        monkeypatch.setattr(sp, "_stored_session",
                            lambda path: pytest.fail("status() read the session file"))

        assert store.status("sb", USER) == "running"
        assert store.status("nobody_holds_it", USER) is None
        assert store.status("no_such_session", USER) is None

    # Short on purpose: without the bound this hangs rather than fails, and a
    # 120-second wait for the default timeout hides which test it was.
    @pytest.mark.timeout(10)
    def test_a_lock_file_that_keeps_being_replaced_is_given_up_on(self, store, monkeypatch):
        # Every attempt means the file was deleted between the open and the
        # lock. A filesystem that keeps answering that way must not spin here.
        monkeypatch.setattr(sp, "_is_file_at", lambda fd, path: False)

        assert store.hold("sb", USER, "agent_b") is False

    def test_a_crashed_holder_leaves_nothing_behind_that_looks_running(
            self, store, other_process, tmp_path):
        other_process("sb").kill()

        _eventually(lambda: store.list_for_user(USER) == [])
        assert not (tmp_path / USER / "sb.lock").exists()

    def test_holds_nest_and_the_last_release_lets_go(self, store, tmp_path):
        store.hold("sb", USER, "agent_b")
        store.hold("sb", USER, "agent_b")

        store.release("sb", USER)
        assert store.get("sb", USER)["status"] == "running"

        store.release("sb", USER)
        assert store.get("sb", USER) is None
        assert not (tmp_path / USER / "sb.lock").exists()

    async def test_it_finds_the_session_files_where_session_manager_puts_them(self, store, tmp_path):
        await SessionManager(storage_path=str(tmp_path)).create_session(
            user_id="team/a..b", session_id="s1")

        assert store.get("s1", "team/a..b")["status"] == "idle"


class TestNotify:
    def test_a_running_session_reads_the_input_on_its_next_step(self, store, spawned, tmp_path):
        store.hold("sb", USER, "agent_b")

        assert store.notify("sb", USER) == ("delivered_next_step", "")
        assert spawned == []
        assert (tmp_path / USER / "sb.pending").exists()

    def test_an_unknown_session_is_reported(self, store, spawned, tmp_path):
        # And its marker goes: a session that ran but never reached disk can
        # leave one behind, and nobody would ever come for it.
        (tmp_path / USER).mkdir(parents=True)
        (tmp_path / USER / "nope.pending").touch()

        assert store.notify("nope", USER)[0] == "unknown"
        assert spawned == []
        assert not store.pending("nope", USER)

    def test_a_stored_session_nobody_holds_is_woken_once(self, store, spawned, tmp_path):
        _stored(tmp_path, "sb")

        # The second answer is not the one a HELD session gives: that one lets go
        # and can be rung again, this one is being read right now, and a caller
        # that rings on would start a second run for it.
        assert [store.notify("sb", USER)[0], store.notify("sb", USER)[0]] == [
            "woke_session", "being_woken"]
        assert spawned == [("sb", USER, 1)]
        # The woken run asks for this before it starts: gone means somebody
        # else took the input, and waking for nothing costs an LLM call.
        assert store.pending("sb", USER)

    def test_a_session_whose_holder_crashed_is_woken(self, store, spawned, other_process, tmp_path):
        _stored(tmp_path, "sb")
        other_process("sb").kill()

        _eventually(lambda: store.notify("sb", USER)[0] == "woke_session")
        assert spawned == [("sb", USER, 1)]

    def test_a_woken_run_wakes_the_next_one_level_deeper(self, store, spawned, monkeypatch, tmp_path):
        monkeypatch.setenv(sp.WAKE_DEPTH_ENV, "1")
        _stored(tmp_path, "sb")

        store.notify("sb", USER)

        assert spawned == [("sb", USER, 2)]

    def test_the_wake_chain_stops_at_its_limit(self, store, spawned, monkeypatch, tmp_path):
        monkeypatch.setenv(sp.WAKE_DEPTH_ENV, str(store.max_wake_depth))
        _stored(tmp_path, "sb")

        assert store.notify("sb", USER)[0] == "queued"
        assert spawned == []
        assert not store.pending("sb", USER), "a marker nobody is coming for"


    def test_a_wake_that_cannot_be_started_leaves_the_input_waiting(
            self, store, monkeypatch, tmp_path):
        # The run that lets the session go is the one that notifies, from its
        # own finally: a wake that fails must not take that run down with it.
        def no_process(session_id, user_id, depth):
            raise RuntimeError("no process could be started")

        monkeypatch.setattr(sp, "spawn_wake", no_process)
        _stored(tmp_path, "sb")

        state, note = store.notify("sb", USER)

        assert state == "queued"
        assert "could not wake it" in note


class TestRelease:
    def test_input_after_the_last_step_wakes_the_session_once_it_is_let_go(
            self, store, spawned, tmp_path):
        _stored(tmp_path, "sb")
        store.hold("sb", USER, "agent_b")
        store.notify("sb", USER)

        store.release("sb", USER)

        assert spawned == [("sb", USER, 1)]

    def test_input_a_later_step_took_wakes_nobody(self, store, spawned, tmp_path):
        _stored(tmp_path, "sb")
        store.hold("sb", USER, "agent_b")
        store.notify("sb", USER)
        store.take_pending("sb", USER)  # the next LLM call; its hooks hand the input over

        store.release("sb", USER)

        assert spawned == []

    def test_an_inner_release_wakes_nobody(self, store, spawned, tmp_path):
        # agent-cli run holds around the loop's own hold: the loop lets go
        # before the run's save after it.
        _stored(tmp_path, "sb")
        store.hold("sb", USER, "agent_b")
        store.hold("sb", USER, "agent_b")
        store.notify("sb", USER)

        store.release("sb", USER)

        assert spawned == []


class TestListing:
    def test_the_users_other_sessions_that_run(self, store):
        store.hold("sa", USER, "agent_a")
        store.hold("sb", USER, "agent_b")
        store.hold("sc", "someone_else", "agent_c")

        assert store.list_for_user(USER, exclude="sa") == [
            {"session_id": "sb", "status": "running", "agent": "agent_b"}]

    def test_a_session_being_woken_is_listed(self, store, spawned, tmp_path):
        _stored(tmp_path, "sb")
        store.notify("sb", USER)

        assert store.list_for_user(USER) == [{"session_id": "sb", "status": "waking", "agent": ""}]

    def test_an_idle_session_is_listed_under_the_agent_it_was_saved_on(self, store, tmp_path):
        (tmp_path / USER).mkdir(parents=True)
        (tmp_path / USER / "sb.json").write_text(
            json.dumps({"agent_name": "stored_agent"}), encoding="utf-8")
        store.hold("sb", USER, "")  # a run that has not named itself yet

        assert store.list_for_user(USER) == [
            {"session_id": "sb", "status": "running", "agent": "stored_agent"}]


class TestSubAgentSessions:
    """A sub-agent's session belongs to the run that spawned it. Started from
    outside it would answer into nothing, so it is neither offered nor woken."""

    def test_it_is_not_listed(self, store, tmp_path):
        _stored(tmp_path, "sb", parent="s_main")
        store.hold("sb", USER, "agent_b")

        assert store.list_for_user(USER) == []
        assert store.get("sb", USER)["sub_agent"] is True

    def test_it_is_not_woken(self, store, spawned, tmp_path):
        _stored(tmp_path, "sb", parent="s_main")

        state, note = store.notify("sb", USER)

        assert (state, spawned) == ("queued", [])
        assert "sub-agent" in note
        assert not store.pending("sb", USER)

    def test_letting_go_of_one_wakes_nobody_either(self, store, spawned, tmp_path):
        _stored(tmp_path, "sb", parent="s_main")
        store.hold("sb", USER, "agent_b")
        store.notify("sb", USER)  # input during its run

        store.release("sb", USER)

        assert spawned == []


def test_the_suite_never_starts_a_real_wake(store, tmp_path):
    # Waking means starting agent-cli: a real run with real LLM calls. The
    # guard for that sits in the root conftest, over every test in the suite;
    # the ones that mean to wake replace spawn_wake themselves. Driven through
    # notify(), which is the only way production gets there.
    _stored(tmp_path, "s1")

    with pytest.raises(BaseException, match="agent-cli") as refused:
        store.notify("s1", USER)

    assert not isinstance(refused.value, Exception), (
        "notify() turns an Exception into a queued message, so a guard that is "
        "one is swallowed: every test that reaches a wake would go green")


def test_a_message_does_not_make_an_idle_session_look_busy(store, tmp_path, monkeypatch):
    # notify() keeps the lock file while it starts the woken run. A run
    # beginning in that window must wait the start out instead of being turned
    # away as a session that runs -- nobody runs it, and the refusal tells the
    # user to wait for a process that does not exist.
    _stored(tmp_path, "sb")
    starting, go_on = threading.Event(), threading.Event()

    def slow_spawn(session_id, user_id, depth):
        starting.set()
        assert go_on.wait(5), "the test never let the wake finish"
        return 0, 0.0  # a run that is gone again: the marker must not outlive it

    monkeypatch.setattr(sp, "spawn_wake", slow_spawn)
    waking = threading.Thread(target=store.notify, args=("sb", USER))
    waking.start()
    try:
        assert starting.wait(5), "fixture: the wake never got as far as starting a run"
        threading.Timer(0.2, go_on.set).start()
        other = sp.SessionPresence(tmp_path)  # another process's view of the file
        began = time.monotonic()

        assert other.hold("sb", USER, "agent_c") is True
        # Long enough to be binding: retrying without waiting runs out in
        # milliseconds, so the hold has to have sat the start out, not outrun it.
        assert time.monotonic() - began >= 0.15
    finally:
        go_on.set()
        waking.join(5)


def test_letting_go_survives_a_wake_the_disk_refuses(store, tmp_path, monkeypatch):
    # release() runs in the finally of its callers -- the endpoints, the CLI,
    # the agent loop. A wake that cannot even write its marker must not replace
    # the answer they are on their way out with.
    _stored(tmp_path, "sb")
    store.hold("sb", USER, "agent_b")
    (tmp_path / USER / "sb.pending").touch()  # input arrived during the run

    def refuse(path, attempts=20):
        raise OSError("the disk keeps replacing it")

    monkeypatch.setattr(sp, "_open_locked", refuse)

    store.release("sb", USER)  # must not raise


def test_session_presence_is_off_by_default():
    # Every agent run holds its session when it is on; tests with a default
    # config must not write into the real data directory.
    assert sp.presence_for(AgentSystemConfig()) is None


class _SessionService:
    """Records the saves; the loop only saves and runs its checkpoint loop."""

    def __init__(self, events):
        self.events = events

    async def save_session(self, **kwargs):
        self.events.append("saved")
        return True

    def start_checkpoint_loop(self, *args, **kwargs):
        pass

    async def stop_checkpoint_loop(self, *args, **kwargs):
        pass


def _agent(tmp_path, monkeypatch, during_call, session_service=None, tool_call_first=False):
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )
    system_config = AgentSystemConfig(
        llm_system=llm_system, session_presence=SessionPresenceConfig(enabled=True))
    agent = Agent("test_agent", system_config,
                  ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(max_steps=3)),
                  ToolServerRegistry(), session_service=session_service)
    agent._session_tracker.set_session_metadata(
        "s1", {"user_id": USER, "agent_name": "test_agent", "llm_profile": "normal"})

    replies = []

    async def chat_tools(messages, tools, **kwargs):
        during_call()
        replies.append(None)
        if tool_call_first and len(replies) == 1:  # a second LLM call follows the tool result
            return {"assistant": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "no_such_tool", "arguments": "{}"}}]}}
        return {"assistant": {"role": "assistant", "content": "done"}}

    llm = AsyncMock()
    llm.supports_streaming = lambda: False
    llm.chat_tools = chat_tools
    agent.llm = llm
    return agent


class TestTheAgentLoop:
    async def test_a_request_holds_its_session_while_it_runs(self, tmp_path, monkeypatch):
        seen = []
        agent = _agent(tmp_path, monkeypatch, lambda: seen.append(
            sp.presence_for(agent.system_config).get("s1", USER)))

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert seen == [{"status": "running", "agent": "test_agent", "sub_agent": False}]
        assert sp.presence_for(agent.system_config).get("s1", USER) is None

    async def test_input_during_the_last_llm_call_wakes_the_session_after_its_save(
            self, tmp_path, monkeypatch):
        events = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth:
                            events.append(("woken", session_id, depth)) or (0, 0.0))
        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch,
                       lambda: sp.presence_for(agent.system_config).notify("s1", USER),
                       session_service=_SessionService(events))

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert events[-1] == ("woken", "s1", 1), f"woken before the last save: {events}"
        assert "saved" in events

    async def test_the_session_is_held_from_the_start_of_the_run(self, tmp_path, monkeypatch):
        # Not only from the first LLM call: whoever lets go of the endpoint's
        # hold meanwhile -- a client that disconnects -- would leave the session
        # looking idle while the run has it, and a direct message arriving then
        # would start a second run of the same session.
        agent = _agent(tmp_path, monkeypatch, lambda: None)
        at_setup = []
        initialize = Agent._initialize_request_and_conversation

        async def recording_initialize(self, *args, **kwargs):
            at_setup.append(
                (sp.presence_for(self.system_config).get("s1", USER) or {}).get("status"))
            return await initialize(self, *args, **kwargs)

        monkeypatch.setattr(Agent, "_initialize_request_and_conversation", recording_initialize)

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert at_setup == ["running"], "the run sets itself up on a session that looks idle"

    async def test_a_cancelled_save_still_lets_go_of_the_session(self, tmp_path, monkeypatch):
        # The client disconnects while the run is saving. Whatever that takes
        # down, the session must not stay behind looking like it runs: every
        # later run of it would be refused over a process that is gone.
        class _Cancelled(_SessionService):
            async def save_session(self, **kwargs):
                raise asyncio.CancelledError()

        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch, lambda: None, session_service=_Cancelled([]))

        with pytest.raises(asyncio.CancelledError):
            [event async for event in agent.run_events("do it", session_id="s1")]

        assert sp.presence_for(agent.system_config).get("s1", USER)["status"] == "idle"

    async def test_a_run_that_does_not_hold_the_session_leaves_its_input_alone(
            self, tmp_path, monkeypatch, other_process, spawned):
        # Forced past the refusal (--force / force=true): the input waiting
        # belongs to the process that holds the session, and taking the marker
        # would rob it of its wake-up after the run.
        _stored(tmp_path, "s1")
        other_process("s1")
        store = sp.SessionPresence(tmp_path)
        assert store.notify("s1", USER)[0] == "delivered_next_step", (
            "fixture: the other process does not have the session")
        agent = _agent(tmp_path, monkeypatch, lambda: None)

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert store.pending("s1", USER), "it took the input the holder is waiting for"

    async def test_input_a_later_llm_call_hands_over_wakes_nobody(self, tmp_path, monkeypatch, spawned):
        calls = []

        def during_call():
            calls.append(None)
            if len(calls) == 1:
                sp.presence_for(agent.system_config).notify("s1", USER)

        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch, during_call, tool_call_first=True)

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert len(calls) == 2, "fixture: no second LLM call to hand the input over"
        assert spawned == []

    async def test_the_session_end_hooks_run_after_the_save(self, tmp_path, monkeypatch):
        # What a request carried counts as delivered when its conversation is on
        # disk -- debate_forum marks direct messages there. Run before the save,
        # the hooks count on a save that may never happen.
        events = []
        agent = _agent(tmp_path, monkeypatch, lambda: None,
                       session_service=_SessionService(events))

        async def record(session_id, request_id, messages=None, persisted=False):
            events.append(("session_end", persisted))

        monkeypatch.setattr(agent._hook_manager, "execute_session_end_hooks", record)

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert events == ["saved", ("session_end", True)], events

    async def test_a_save_that_did_not_happen_is_told_to_the_hooks(self, tmp_path, monkeypatch):
        # Persisting never raises -- a failure must not kill a running request --
        # so the hooks have to be told, or "delivered" is a promise nobody kept.
        class _Broken(_SessionService):
            async def save_session(self, **kwargs):
                raise RuntimeError("the disk is full")

        events = []
        agent = _agent(tmp_path, monkeypatch, lambda: None, session_service=_Broken(events))

        async def record(session_id, request_id, messages=None, persisted=False):
            events.append(persisted)

        monkeypatch.setattr(agent._hook_manager, "execute_session_end_hooks", record)

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert events == [False], "a run that saved nothing counts its input as delivered"
