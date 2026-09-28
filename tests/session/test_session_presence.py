"""Session presence and waking (core/session_presence.py).

The rules against a temp sessions directory. A holder in another process is a
real process, and its crash a real kill; the agent loop's holds go through a
real Agent with a fake LLM. Only the detached start of a woken run is
replaced -- a test must not start agent runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
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
from agent_system.core.cancellation import get_cancellation_manager
from agent_system.tools.base import ToolServerRegistry
from agent_system.tools.status import current_request_id
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

        async def record(session_id, request_id, messages=None, persisted=False, **_outcome):
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

        async def record(session_id, request_id, messages=None, persisted=False, **_outcome):
            events.append(persisted)

        monkeypatch.setattr(agent._hook_manager, "execute_session_end_hooks", record)

        [event async for event in agent.run_events("do it", session_id="s1")]

        assert events == [False], "a run that saved nothing counts its input as delivered"


class TestTakingTheLock:
    """What a hold does while other processes take, drop and ask about the same file."""

    @pytest.mark.skipif(os.name != "nt", reason="only Windows refuses opens of a file on its way out")
    def test_a_lock_file_being_deleted_does_not_refuse_the_hold(self, tmp_path, monkeypatch):
        """Windows answers a delete in progress with a refusal, not with "gone".

        A probe that finds the file held by nobody drops it (_drop), and a hold
        opening the name at that moment is refused -- which does not stop the
        run, it lets it write the session with nothing keeping a second run out.
        Measured with six holders and two askers on one session: 52 holds in six
        seconds were refused this way.
        """
        opened = []
        real_open = os.open

        def refuse_once(path, flags, *args, **kwargs):
            if str(path).endswith(".lock") and not opened:
                opened.append(path)
                raise PermissionError(13, "being deleted")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(sp.os, "open", refuse_once)
        presence = sp.SessionPresence(tmp_path)

        assert presence.hold("s1", "u", "agent") is True, "the hold was refused, and the run would go on unheld"
        assert opened, "fixture: the refusal never happened"
        presence.release("s1", "u")

    def test_a_refusal_that_does_not_pass_is_reported(self, tmp_path, monkeypatch):
        """A lock file that cannot be opened at all: the run is told it could
        not hold the session. POSIX deletes a name at once, so a refusal there
        is the directory's and is reported at once -- waiting would only put
        the report off; Windows first waits out a delete in progress
        (_open_locked), and gives up after its attempts."""
        attempts = []
        real_open = os.open

        def refuse(path, flags, *args, **kwargs):
            if str(path).endswith(".lock"):
                attempts.append(path)
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(sp.os, "open", refuse)
        presence = sp.SessionPresence(tmp_path)

        assert presence.hold("s1", "u", "agent") is False
        assert len(attempts) == (20 if os.name == "nt" else 1), f"{len(attempts)} attempts"

    def test_a_holder_that_let_go_while_being_asked_about_is_not_reported_as_busy(
            self, tmp_path, monkeypatch):
        """Between the failed attempt and the answer the holder can let go.

        Reported all the same, the caller is told the session is worked on
        elsewhere while it is free -- measured, 415 of some 3000 answers of
        "running" under six holders had gone stale by then.
        """
        real_open_locked = sp._open_locked
        attempts = []

        def busy_first(path, *args, **kwargs):
            attempts.append(path)
            return None if len(attempts) == 1 else real_open_locked(path, *args, **kwargs)

        monkeypatch.setattr(sp, "_open_locked", busy_first)
        monkeypatch.setattr(sp, "_probe", lambda path: {"status": "running", "agent": "someone"})
        presence = sp.SessionPresence(tmp_path)

        assert presence.hold("s1", "u", "agent") is True, "a session nobody holds was reported as busy"
        assert len(attempts) == 2, "fixture: the second attempt never happened"
        presence.release("s1", "u")


class TestTheWakeLog:
    """logs/agent-wake.log is where a wake that went wrong can be read afterwards."""

    def test_two_wakes_writing_at_once_keep_every_line_of_both(self, tmp_path):
        """Each wake hands its own handle to its child, and both append.

        A handle that keeps an offset of its own lets the second wake write over
        the first: measured on Windows, the first kept 0 of its 200 lines -- and
        the wake somebody would look for is the one that said something.
        """
        log = tmp_path / "agent-wake.log"
        go = tmp_path / "go"
        # Both wait for the same sign before they write: started one after the
        # other they might not overlap at all, and the test would pass on a
        # handle that cannot take two writers.
        child = ("import os, sys, time\n"
                 "while not os.path.exists(sys.argv[1]): time.sleep(0.005)\n"
                 "for i in range(200): sys.stderr.write('%s line %d\\n' % (sys.argv[2], i))\n"
                 "sys.stderr.flush()\n")
        running = []
        for tag in ("first", "second"):
            errors = sp._append_handle(log)
            running.append(subprocess.Popen(
                [sys.executable, "-c", child, str(go), tag],
                stdout=subprocess.DEVNULL, stderr=errors))
            errors.close()
        go.touch()
        for process in running:
            assert process.wait(timeout=60) == 0, "a writer did not finish"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        for tag in ("first", "second"):
            assert sum(1 for line in lines if line.startswith(tag)) == 200, \
                f"{tag} wake lost lines: {len(lines)} in the log altogether"


class TestAStoppedRun:
    """A run its user stopped: nothing starts its session again by itself. Measured in the
    user's test of 22.09.2026: a stop with a message appended mid-run and async sub-agents
    at work woke the session as agent-cli in a process of its own, and the chat showed it as
    worked on elsewhere until that run was done."""

    @pytest.fixture(autouse=True)
    def no_stops_from_other_tests(self, monkeypatch):
        monkeypatch.setattr(sp, "_stops", set())

    def test_input_left_waiting_does_not_wake_a_session_let_go_stopped(self, store, tmp_path, spawned):
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        assert store.notify("s1", USER)[0] == "delivered_next_step", "fixture: the input found the run"
        sp.note_stop("r1")
        store.release("s1", USER)
        assert spawned == []
        # work of the stopped run that ends later rings for it: queued, not woken
        assert store.notify("s1", USER) == ("queued", sp.STOPPED)
        assert spawned == [] and not store.pending("s1", USER)

    def test_the_stop_counts_when_an_outer_hold_lets_go_last(self, store, tmp_path, spawned):
        # The endpoint holds the session around the run and lets go after it, knowing
        # nothing of the stop.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "endpoint") and store.hold("s1", USER, "run", run="r1")
        store.notify("s1", USER)
        sp.note_stop("r1")
        store.release("s1", USER)
        store.release("s1", USER)
        assert spawned == []
        assert store.notify("s1", USER)[0] == "queued"

    def test_the_next_run_lifts_the_mark(self, store, tmp_path, spawned):
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "agent_a", run="r2")
        store.notify("s1", USER)
        store.release("s1", USER)
        assert spawned == [("s1", USER, 1)]

    def test_a_hold_that_starts_no_run_keeps_the_mark(self, store, tmp_path, spawned):
        # /undo and an append hold the session to write it, and a woken run holds it
        # before it looks for input: none of them is the run that takes it up again.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "endpoint")
        store.release("s1", USER)
        assert store.notify("s1", USER)[0] == "queued"
        assert spawned == []

    def test_while_a_chat_stays_open_after_a_stop_nothing_starts_a_turn(self, store, tmp_path, spawned):
        # agent-cli chat holds its session between turns, so the stopped turn's hold is
        # not the last: the chat answers from memory, for rings of its own process and
        # for its prompt watcher, which asks pending() for a ring from elsewhere.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "chat") and store.hold("s1", USER, "turn", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.notify("s1", USER) == ("queued", sp.STOPPED)
        elsewhere = sp.SessionPresence(tmp_path)
        assert elsewhere.notify("s1", USER)[0] == "delivered_next_step", "fixture: the ring found no holder"
        assert not store.pending("s1", USER)
        store.release("s1", USER)
        assert spawned == []

    def test_a_stop_noted_after_the_run_let_go_still_counts(self, store, tmp_path, spawned):
        # A Ctrl-C in the run's own frames: the run lets go before the chat hears of it.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "chat") and store.hold("s1", USER, "turn", run="r1")
        assert store.notify("s1", USER)[0] == "delivered_next_step"
        store.release("s1", USER)
        sp.note_stop("r1")
        assert not store.pending("s1", USER)
        store.release("s1", USER)
        assert spawned == []
        assert (tmp_path / USER / "s1.stopped").exists()

    def test_a_turn_after_a_stopped_one_is_not_stopped(self, store, tmp_path, spawned):
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "chat") and store.hold("s1", USER, "turn", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "turn", run="r2")
        # while it runs, what rings for it is read by it
        assert store.notify("s1", USER)[0] == "delivered_next_step"
        store.release("s1", USER)
        store.release("s1", USER)   # the chat ends
        assert spawned == [("s1", USER, 1)]
        assert not (tmp_path / USER / "s1.stopped").exists()

    def test_the_run_an_endpoint_holds_around_lifts_the_mark(self, store, tmp_path, spawned):
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "endpoint") and store.hold("s1", USER, "run", run="r2")
        assert store.notify("s1", USER)[0] == "delivered_next_step"
        store.release("s1", USER)
        store.release("s1", USER)
        assert spawned == [("s1", USER, 1)]

    def test_an_agent_as_tool_stopped_alone_does_not_stop_its_caller(self, store, tmp_path, spawned):
        # An agent-as-tool runs on its caller's session, under the caller's tool call id.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "caller", run="r1") and store.hold("s1", USER, "tool", run="r1_003")
        sp.note_stop("r1_003")
        store.release("s1", USER)
        assert store.notify("s1", USER)[0] == "delivered_next_step"
        store.release("s1", USER)
        assert spawned == [("s1", USER, 1)]

    @pytest.mark.parametrize("stopped, woken", [("older", True), ("newer", False)])
    def test_the_run_that_took_it_last_decides_whatever_lets_go_last(self, store, tmp_path, spawned, stopped, woken):
        # The older run still finalizes (a slow save, session-end hooks) while the
        # user's newer one runs through, so the older one lets go last.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "endpoint") and store.hold("s1", USER, "run", run="older")
        assert store.hold("s1", USER, "endpoint") and store.hold("s1", USER, "run", run="newer")
        sp.note_stop(stopped)
        store.notify("s1", USER)
        store.release("s1", USER)
        store.release("s1", USER)
        store.release("s1", USER)
        store.release("s1", USER)
        assert spawned == ([("s1", USER, 1)] if woken else [])

    def test_input_rung_before_the_stop_does_not_wake_after_the_next_run(self, store, tmp_path, spawned):
        # The stop took its doorbell; the input waits in its store, read at a step.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        assert store.notify("s1", USER)[0] == "delivered_next_step"
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "agent_a", run="r2")
        store.release("s1", USER)
        assert spawned == []

    def test_a_stop_that_lets_go_while_a_ring_takes_the_lock_is_seen(self, store, tmp_path, spawned, monkeypatch):
        # The ring passed the check before the lock; the stopped run let go in between.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        open_locked = sp._open_locked

        def run_lets_go_first(path):
            sp.note_stop("r1")
            store.release("s1", USER)
            return open_locked(path)

        monkeypatch.setattr(sp, "_open_locked", run_lets_go_first)
        assert store.notify("s1", USER) == ("queued", sp.STOPPED)
        assert spawned == []

    def test_an_outer_hold_that_saw_ctrl_c_notes_it_for_its_run(self, store, tmp_path, spawned):
        # agent-run: Ctrl-C cancels the task, not the run, and the run lets go first,
        # knowing nothing of it. agent-run's own hold goes last and says it.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "cli") and store.hold("s1", USER, "run", run="r1")
        store.notify("s1", USER)
        store.release("s1", USER)
        store.release("s1", USER, stopped=True)
        assert spawned == []
        assert sp.stopped_by_user("r1")
        assert store.notify("s1", USER)[0] == "queued"

    @pytest.mark.parametrize("stopped, woken", [("r2", False), ("r1", True)])
    def test_an_agent_as_tool_of_an_older_run_is_not_the_last_run(self, store, tmp_path, spawned, stopped, woken):
        # Two runs on one session at once (two tabs, two agents); the older one then
        # calls an agent-as-tool, under its own tool-call id.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "a", run="r1") and store.hold("s1", USER, "b", run="r2")
        assert store.hold("s1", USER, "tool", run="r1_005")
        sp.note_stop(stopped)
        store.notify("s1", USER)
        store.release("s1", USER)
        store.release("s1", USER)
        store.release("s1", USER)
        assert spawned == ([("s1", USER, 1)] if woken else [])

    def test_an_agent_as_tool_does_not_lift_its_stopped_callers_mark(self, tmp_path, monkeypatch, spawned):
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        store = sp.presence_for(SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True)))
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "a", run="r1")
        sp.note_stop("r1")
        assert (tmp_path / USER / "s1.stopped").exists(), "fixture: the stop marked nothing"
        assert store.hold("s1", USER, "tool", run="r1_004")   # a tool call still in flight
        assert (tmp_path / USER / "s1.stopped").exists()
        store.release("s1", USER)
        store.release("s1", USER)

    def test_a_stop_is_on_disk_as_soon_as_it_is_noted(self, tmp_path, monkeypatch, spawned):
        # agent-cli chat keeps the session for hours after a Ctrl-C; a process that
        # ends before it lets go (closed, killed) must not take the stop with it.
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        store = sp.presence_for(SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True)))
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "chat") and store.hold("s1", USER, "turn", run="r1")
        store.release("s1", USER)
        sp.note_stop("r1")
        assert (tmp_path / USER / "s1.stopped").exists()
        elsewhere = sp.SessionPresence(tmp_path)   # what another process reads
        assert elsewhere.notify("s1", USER) == ("queued", sp.STOPPED)
        store.release("s1", USER)

    def test_a_stop_marks_only_the_session_of_the_run_it_stopped(self, tmp_path, monkeypatch, spawned):
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        store = sp.presence_for(SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True)))
        _stored(tmp_path, "s1")
        _stored(tmp_path, "s2")
        assert store.hold("s1", USER, "a", run="r1") and store.hold("s2", USER, "b", run="r2")
        sp.note_stop("r1")
        assert (tmp_path / USER / "s1.stopped").exists(), "fixture: the stop marked nothing"
        assert not (tmp_path / USER / "s2.stopped").exists()
        store.release("s1", USER)
        store.release("s2", USER)

    def test_a_holder_that_saw_a_stop_before_any_run_took_it_marks_it(self, store, tmp_path, spawned):
        # A Ctrl-C while agent-cli or agent-run still loads the session.
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "cli")
        assert sp.SessionPresence(tmp_path).notify("s1", USER)[0] == "delivered_next_step"
        store.release("s1", USER, stopped=True)
        assert spawned == []
        assert store.notify("s1", USER)[0] == "queued"

    async def test_a_stop_noted_while_the_work_already_rings_is_seen_at_the_next_ring(
            self, tmp_path, monkeypatch, spawned):
        # A background process of r1 ends while r1 still holds: the ring rings on. Then the
        # user stops r1 and asks again (r2). A check before the first ring alone lets the
        # stopped run's work through the STOPPED answers until r2 lifts the mark.
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0)
        config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
        store = sp.presence_for(config)
        _stored(tmp_path, "s1")
        real_notify, answers = store.notify, []

        def notify(session_id, user_id):
            answer = real_notify(session_id, user_id)
            answers.append(answer)
            if len(answers) == 1:      # r1 still holds; now its user stops it
                sp.note_stop("r1")
                store.release("s1", USER)
            elif len(answers) == 2:    # reached only if the stop is not checked again
                store.hold("s1", USER, "agent_a", run="r2")
            elif len(answers) == 3:
                store.release("s1", USER)
            return answer

        monkeypatch.setattr(store, "notify", notify)
        assert store.hold("s1", USER, "agent_a", run="r1")
        token = current_request_id.set("r1_002_async_x")
        try:
            state = await sp.wake_session(config, "s1", USER, what="r1's background process")
        finally:
            current_request_id.reset(token)

        assert answers[0][0] == "delivered_next_step", "fixture: the first ring did not find r1 holding"
        assert state == "queued"
        assert spawned == []

    async def test_a_caller_that_names_the_run_is_believed_over_the_task(self, tmp_path, monkeypatch, spawned):
        # An admin cancels the sub-agent itself: its own id is noted, and the job's task
        # still carries it from the stream it did not drain. Its caller, r1, was not stopped.
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0)
        config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
        sp.presence_for(config)
        _stored(tmp_path, "s1")
        sp.note_stop("r1_001_async_x")
        token = current_request_id.set("r1_001_async_x")
        try:
            state = await sp.wake_session(config, "s1", USER, what="sub-agent x", started_by="r1_001")
        finally:
            current_request_id.reset(token)
        assert state == "woke_session"
        assert spawned == [("s1", USER, 1)]

    async def test_a_run_adopted_under_an_id_that_was_stopped_before_is_not_stopped(self):
        # writer_jobs sends a run again under its request id, through /run.
        from agent_system.app import _validate_client_request_id

        sp.note_stop("r-again-01")
        assert await _validate_client_request_id("r-again-01") == "r-again-01"
        assert not sp.stopped_by_user("r-again-01")

    async def test_a_stop_noted_before_the_run_registers_stays(self, tmp_path, monkeypatch):
        # A minted id (chat, agent-cli) is new: a stop noted before its run registers is its own.
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
        _stored(tmp_path, "s1")
        sp.note_stop("r-early-stop")
        agent = _agent(tmp_path, monkeypatch, lambda: sp.presence_for(agent.system_config).notify("s1", USER))

        [event async for event in agent.run_events("do it", session_id="s1", request_id="r-early-stop")]

        assert sp.stopped_by_user("r-early-stop")
        assert woken == []

    async def test_a_ctrl_c_inside_an_agent_as_tool_is_the_outer_runs(self, tmp_path, monkeypatch):
        # collect_final_result swallows a Ctrl-C of the run it collects -- not of one it
        # collects for another run, whose handler has to see it.
        from agent_system.servers.agent.result_utils import collect_final_result

        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch, lambda: None)

        def ctrl_c(self, session_id, request_id):
            raise KeyboardInterrupt

        monkeypatch.setattr(Agent, "_presence_step", ctrl_c)
        token = current_request_id.set("r1")
        try:
            with pytest.raises(KeyboardInterrupt):
                await collect_final_result(agent, "do it", request_id="r1_003", session_id="s1")
        finally:
            current_request_id.reset(token)
        assert not sp.stopped_by_user("r1_003")

    async def test_work_of_a_stopped_run_does_not_wake_after_the_next_run(
            self, tmp_path, monkeypatch, spawned, caplog):
        # Stop, then ask again at once: the new run lifts the mark, and a sub-agent the
        # stopped run started ends meanwhile. Its task carries the stopped run's id.
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0)   # a wrong answer rings on
        config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
        store = sp.presence_for(config)
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "agent_a", run="r2")   # the user asks again

        token = current_request_id.set("r1_003_async_x")
        try:
            with caplog.at_level(logging.INFO, logger=sp.__name__):
                state = await sp.wake_session(config, "s1", USER, what="sub-agent x")
        finally:
            current_request_id.reset(token)
        store.release("s1", USER)

        assert state == "queued"
        assert spawned == []
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    async def test_work_of_the_run_that_holds_it_still_rings(self, tmp_path, monkeypatch, spawned):
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0)
        config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
        store = sp.presence_for(config)
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)
        assert store.hold("s1", USER, "agent_a", run="r10")

        token = current_request_id.set("r10_001_async_y")   # r10's, not r1's
        try:
            state = await sp.wake_session(config, "s1", USER, still_needed=lambda: False)
        finally:
            current_request_id.reset(token)
        store.release("s1", USER)

        assert state == "delivered_next_step"
        assert spawned == [("s1", USER, 1)]

    async def test_work_of_a_run_nobody_stopped_reaches_the_session_after_the_next_run(
            self, tmp_path, monkeypatch, spawned):
        # r0 ended its turn over background work; the user then asks (r1) and stops
        # that. r0's work ends while the session is marked: it is still news once the
        # user's next run (r2) lifts the mark.
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0.05)
        config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
        store = sp.presence_for(config)
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)

        token = current_request_id.set("r0_002_async_z")
        try:
            ringing = asyncio.create_task(sp.wake_session(config, "s1", USER, what="r0's sub-agent"))
        finally:
            current_request_id.reset(token)
        await asyncio.sleep(0.2)
        assert not ringing.done(), "the ringing gave up at the stopped session"
        assert store.hold("s1", USER, "agent_a", run="r2")
        await asyncio.sleep(0.2)
        store.release("s1", USER)
        await asyncio.wait_for(ringing, 10)

        assert spawned == [("s1", USER, 1)]

    async def test_a_stopped_session_is_no_warning(self, tmp_path, monkeypatch, spawned, caplog):
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0)
        config = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
        store = sp.presence_for(config)
        _stored(tmp_path, "s1")
        assert store.hold("s1", USER, "agent_a", run="r1")
        sp.note_stop("r1")
        store.release("s1", USER)

        with caplog.at_level(logging.INFO, logger=sp.__name__):
            assert await sp.wake_session(config, "s1", USER, what="a background command") == "queued"

        assert [record.levelno for record in caplog.records if "Did NOT wake" in record.getMessage()] == [logging.INFO]
        assert spawned == []

    async def test_the_stop_button_notes_the_stop(self):
        from agent_system.services.background_job_manager import BackgroundJobManager

        await BackgroundJobManager().cancel_job("r-stop-button")

        assert sp.stopped_by_user("r-stop-button")
        assert sp.stopped_by_user("r-stop-button_004_async_x")
        assert not sp.stopped_by_user("r-stop-button2")

    async def test_a_run_after_a_stopped_one_is_woken_as_usual(self, tmp_path, monkeypatch):
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch, lambda: sp.presence_for(agent.system_config).notify("s1", USER))
        presence = sp.presence_for(agent.system_config)
        assert presence.hold("s1", USER, "test_agent", run="r1")
        sp.note_stop("r1")
        presence.release("s1", USER)

        events = [event async for event in agent.run_events("do it", session_id="s1", request_id="r-next")]

        assert any(event.get("type") == "final" for event in events), f"fixture: the run did not answer: {events}"
        assert woken == ["s1"]

    async def test_work_a_stopped_run_started_does_not_ring_after_the_next_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "WAKE_RETRY_SECONDS", 0)   # a wrong answer rings on
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
        _stored(tmp_path, "s1")

        def stop():
            sp.note_stop("r-stopped")
            get_cancellation_manager().cancel_request("r-stopped")

        agent = _agent(tmp_path, monkeypatch, stop, tool_call_first=True)
        events = [event async for event in agent.run_events("do it", session_id="s1", request_id="r-stopped")]
        assert any(event.get("type") == "cancelled" for event in events), f"fixture: the run did not stop: {events}"
        presence = sp.presence_for(agent.system_config)
        assert presence.hold("s1", USER, "test_agent", run="r-next")   # the user asks again

        token = current_request_id.set("r-stopped_002_async_x")   # a sub-agent it started
        try:
            state = await sp.wake_session(agent.system_config, "s1", USER, what="sub-agent x")
        finally:
            current_request_id.reset(token)
        presence.release("s1", USER)

        assert state == "queued"
        assert woken == []

    async def test_a_run_its_user_stops_is_not_woken_by_input_that_came_meanwhile(self, tmp_path, monkeypatch):
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
        _stored(tmp_path, "s1")

        def during_call():
            sp.presence_for(agent.system_config).notify("s1", USER)   # a message comes in
            sp.note_stop("r-stopped")                                   # and its user stops the run
            get_cancellation_manager().cancel_request("r-stopped")

        agent = _agent(tmp_path, monkeypatch, during_call, tool_call_first=True)

        events = [event async for event in agent.run_events("do it", session_id="s1", request_id="r-stopped")]

        assert any(event.get("type") == "cancelled" for event in events), f"fixture: the run did not stop: {events}"
        assert woken == []
        assert sp.presence_for(agent.system_config).notify("s1", USER)[0] == "queued"
        assert woken == []

    async def test_a_stop_during_the_runs_finalize_counts(self, tmp_path, monkeypatch):
        # The run has answered and saves (session-end hooks can take seconds): its token
        # is gone, and the Stop that comes now still stops it.
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch, lambda: None)
        finalize = Agent._finalize_request

        async def stopped_while_saving(self, *args, **kwargs):
            await finalize(self, *args, **kwargs)
            sp.presence_for(agent.system_config).notify("s1", USER)
            sp.note_stop("r-saving")

        monkeypatch.setattr(Agent, "_finalize_request", stopped_while_saving)

        events = [event async for event in agent.run_events("do it", session_id="s1", request_id="r-saving")]

        assert any(event.get("type") == "final" for event in events), f"fixture: the run did not answer: {events}"
        assert woken == []
        assert sp.presence_for(agent.system_config).notify("s1", USER)[0] == "queued"

    async def test_a_run_that_fails_is_no_stop(self, tmp_path, monkeypatch):
        # Its own failure cancels its token (and its sub-requests'): input that came
        # meanwhile still wakes the session.
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))
        _stored(tmp_path, "s1")
        agent = _agent(tmp_path, monkeypatch, lambda: None)

        async def failing_loop(self, *args, **kwargs):
            sp.presence_for(agent.system_config).notify("s1", USER)
            raise RuntimeError("the loop broke")
            yield  # an async generator, as the loop is

        monkeypatch.setattr(Agent, "_execute_llm_loop", failing_loop)

        events = [event async for event in agent.run_events("do it", session_id="s1", request_id="r-failing")]

        assert any(event.get("type") == "error" for event in events), f"fixture: the run did not fail: {events}"
        assert woken == ["s1"]


class TestAWakeTheAgentsRoleGateRefuses:
    """A wake runs the session's stored agent as the session's user (agent-cli). One its role gate
    (metadata.min_role) refuses is not started: refused, its letting go rang again -- a refused agent-cli per
    ring, up to max_wake_depth, for input nobody could read. The marker stays for a later ring."""

    @pytest.fixture
    def gated_world(self, tmp_path, monkeypatch, spawned):
        from agent_system.auth import agent_access, database
        from agent_system.auth.models import UserCreate, UserRole
        from agent_system.config.models import AgentMetadata, AuthConfig, PluginsConfig

        users = database.UserDatabase(tmp_path / "users.db")
        monkeypatch.setattr(database, "_db", users)
        monkeypatch.setattr(agent_access, "_local_operator_trusted", False)  # judged as agent-cli, not as this
        for name, role in (("root", UserRole.ADMIN), ("bob", UserRole.USER)):
            users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse",
                                         role=role))
        root = tmp_path / "sessions"
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(root))

        def agent(min_role):
            return ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(llm_profile="normal"),
                                    metadata=AgentMetadata(min_role=min_role))

        config = AgentSystemConfig(
            session_presence=SessionPresenceConfig(enabled=True),
            auth=AuthConfig(enabled=True, database_path=str(tmp_path / "absent.db")),
            plugins=PluginsConfig(servers={"gated_agent": agent("admin"), "open_agent": agent(None)}))
        presence = sp.presence_for(config)

        def stored(session_id, user_id, agent_name):
            (root / user_id).mkdir(parents=True, exist_ok=True)
            (root / user_id / f"{session_id}.json").write_text(json.dumps({"agent_name": agent_name}),
                                                               encoding="utf-8")

        return SimpleNamespace(presence=presence, stored=stored, spawned=spawned, config=config)

    def test_a_users_session_on_a_gated_agent_is_not_woken_and_keeps_its_input(self, gated_world):
        gated_world.stored("sb", "bob", "gated_agent")

        state, note = gated_world.presence.notify("sb", "bob")

        assert (state, gated_world.spawned) == ("queued", []), note
        assert "gated_agent" in note, note
        assert gated_world.presence.pending("sb", "bob"), "the input's marker went: a later ring finds nothing"
        # and letting the session go (what a refused run did) rings nobody either
        gated_world.presence.hold("sb", "bob", "gated_agent")
        gated_world.presence.release("sb", "bob")
        assert gated_world.spawned == []

    @pytest.mark.parametrize("session_id, user_id, agent_name", [
        ("sa", "root", "gated_agent"),      # the admin's session
        ("so", "bob", "open_agent"),        # an agent without a gate
        ("sc", "cli_user", "gated_agent"),  # the local operator: the woken agent-cli trusts it
    ])
    def test_a_wake_the_agent_would_accept_is_started(self, gated_world, session_id, user_id, agent_name):
        gated_world.stored(session_id, user_id, agent_name)

        assert gated_world.presence.notify(session_id, user_id)[0] == "woke_session"
        assert gated_world.spawned == [(session_id, user_id, 1)]

    @pytest.mark.parametrize("default_agent, woken", [("gated_agent", False), ("open_agent", True)])
    def test_a_session_whose_agent_is_gone_is_judged_by_the_agent_the_wake_runs(
            self, gated_world, monkeypatch, default_agent, woken):
        """agent-cli runs the config's default agent where the stored one is no longer defined."""
        monkeypatch.setattr(gated_world.config, "default_agent", default_agent)
        gated_world.stored("sg", "bob", "renamed_long_ago")

        state, note = gated_world.presence.notify("sg", "bob")

        assert (state == "woke_session") is woken, (state, note)

    @pytest.mark.parametrize("default_agent, woken", [("gated_agent", False), ("open_agent", True)])
    def test_a_session_file_that_names_no_agent_is_judged_by_the_default_agent(
            self, gated_world, monkeypatch, tmp_path, default_agent, woken):
        """Unreadable (or gone with its lock left behind): agent-cli then runs the config's default agent."""
        monkeypatch.setattr(gated_world.config, "default_agent", default_agent)
        user_dir = tmp_path / "sessions" / "bob"
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "su.json").write_text("{ half written", encoding="utf-8")

        state, note = gated_world.presence.notify("su", "bob")

        assert (state == "woke_session") is woken, (state, note)
