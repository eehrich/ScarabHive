"""notify() handing a session to a waker of its own process (set_local_waker, api/woken_runs.py).

Real lock files under tmp_path; the waker is the test's. The conftest refuses spawn_wake,
so a ring that fell through to a process of its own would come back "could not wake it".
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import uuid
from types import SimpleNamespace

import pytest

from agent_system.core.session_presence import (SessionPresence, set_local_waker, wake_depth, woken_at,
                                                woken_process)
from agent_system.core.session_presence import presence as presence_mod

USER = "someone"


@pytest.fixture
def store(tmp_path):
    return SessionPresence(tmp_path)


def _session(store) -> str:
    session_id = f"s{uuid.uuid4().hex[:10]}"
    path = store._lock_path(session_id, USER).with_suffix(".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent_name": "assistant"}), encoding="utf-8")
    return session_id


def test_a_turn_woken_in_the_api_is_deep_in_the_chain_but_no_woken_process(monkeypatch):
    """Plugins refuse a wake to a woken PROCESS -- it ends with its turn. A turn the API woke
    carries the depth for the chain limit, and its process goes on."""
    monkeypatch.delenv("HIVE_WAKE_DEPTH", raising=False)

    def inside():
        woken_at(2)
        return wake_depth(), woken_process()

    assert contextvars.copy_context().run(inside) == (2, False)
    assert (wake_depth(), woken_process()) == (0, False), "the depth leaked out of the woken turn's context"
    monkeypatch.setenv("HIVE_WAKE_DEPTH", "1")
    assert (wake_depth(), woken_process()) == (1, True)


def test_a_turn_handed_over_is_held_by_this_process_and_carries_its_depth(store, monkeypatch):
    monkeypatch.delenv("HIVE_WAKE_DEPTH", raising=False)
    session_id = _session(store)
    taken = []
    set_local_waker(lambda presence, sid, user, depth: taken.append((presence, sid, user, depth)) or "")
    assert store.notify(session_id, USER) == ("woke_session", "")
    assert taken == [(store, session_id, USER, 1)]
    assert store.held_here(session_id, USER) and store.pending(session_id, USER)
    store.take_for_wake(session_id, USER)
    store.release(session_id, USER)   # the turn is over
    assert not store.held_here(session_id, USER) and store.status(session_id, USER) is None


def test_a_refused_turn_leaves_a_hold_that_nested_meanwhile_its_lock(store):
    session_id = _session(store)

    nested = []

    def refuses(presence, sid, user, depth):
        if not nested:   # once: letting go below rings again for the input still waiting
            nested.append(presence.hold(sid, user, "assistant"))   # a run of this process takes it meanwhile
        return "not now"

    set_local_waker(refuses)
    assert store.notify(session_id, USER) == ("queued", "not now")
    assert store.held_here(session_id, USER)
    other = SessionPresence(store.root)   # another process's view
    assert other.status(session_id, USER) == "running", "the run that nested holds the session without its lock"
    store.release(session_id, USER)
    assert other.status(session_id, USER) is None


def test_a_refused_turn_with_nobody_nested_lets_go_and_the_input_waits(store):
    session_id = _session(store)
    set_local_waker(lambda presence, sid, user, depth: "not now")
    assert store.notify(session_id, USER) == ("queued", "not now")
    assert not store.held_here(session_id, USER)
    assert store.status(session_id, USER) is None
    assert store.pending(session_id, USER), "a refusal dropped the marker the next run is told by"


def test_a_waker_that_fails_lets_go_and_drops_the_marker(store):
    session_id = _session(store)

    def fails(presence, sid, user, depth):
        raise RuntimeError("loop closed")

    set_local_waker(fails)
    state, note = store.notify(session_id, USER)
    assert state == "queued" and "loop closed" in note
    assert not store.held_here(session_id, USER) and store.status(session_id, USER) is None
    assert not store.pending(session_id, USER)


def test_after_the_api_has_ended_nobody_is_woken_in_a_process_of_its_own_either(store, monkeypatch):
    from agent_system.api import woken_runs

    spawned = []
    monkeypatch.setattr(presence_mod, "spawn_wake", lambda *args: spawned.append(args) or (1, 0.0))
    session_id = _session(store)
    woken_runs.close()
    assert store.notify(session_id, USER) == ("queued", woken_runs.SHUTTING_DOWN)
    assert not spawned
    assert store.pending(session_id, USER)


@pytest.mark.parametrize("cli_refuses, api_refuses, route", [
    (False, False, "here"),
    # The CLI's own session on a gated agent: agent-cli trusts the local operator, this process does not.
    (False, True, "agent-cli"),
    (True, True, "nobody"),
])
def test_a_wake_goes_where_the_run_may_be_taken(store, monkeypatch, cli_refuses, api_refuses, route):
    """Judged by agent-cli's rule alone, notify() handed the API a turn the API then refused, and the input lost
    its marker; judged by the API's alone, a session agent-cli may run was no longer woken at all."""
    from agent_system.auth import agent_access
    from agent_system.cli_utils import session_defaults
    from agent_system.config import settings

    monkeypatch.setattr(session_defaults, "usable_session_defaults", lambda agent, llm, config: (agent, llm))
    monkeypatch.setattr(settings, "get_tool_server_config",
                        lambda name, config: SimpleNamespace(metadata=SimpleNamespace(min_role="admin")))

    def denial(min_role, who, auth, local_operator=None):
        return "gated" if (cli_refuses if local_operator else api_refuses) else None

    monkeypatch.setattr(agent_access, "agent_run_denial", denial)
    spawned, taken = [], []
    monkeypatch.setattr(presence_mod, "spawn_wake", lambda *args: spawned.append(args) or (1, 0.0))
    set_local_waker(lambda presence, sid, user, depth: taken.append(sid) or "")
    store.system_config = SimpleNamespace(default_agent="assistant", auth=None)
    session_id = _session(store)
    state, _ = store.notify(session_id, USER)
    assert (state, bool(taken), bool(spawned)) == {
        "here": ("woke_session", True, False),
        "agent-cli": ("woke_session", False, True),
        "nobody": ("queued", False, False)}[route]
    if route == "nobody":
        assert store.pending(session_id, USER), "a refused wake dropped the marker the next run is told by"


def test_a_waker_interrupted_gives_the_hold_back(store):
    session_id = _session(store)

    def interrupted(presence, sid, user, depth):
        raise KeyboardInterrupt

    set_local_waker(interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.notify(session_id, USER)
    assert not store.held_here(session_id, USER), "an interrupted hand-over left a hold nobody lets go of"


def test_the_api_runs_a_turn_in_a_context_of_its_own(store):
    """The ringer's context -- its run, its tool call, its caller's profile -- is not the woken turn's."""
    from agent_system.api import woken_runs

    probe = contextvars.ContextVar("probe", default="fresh")
    seen = []

    async def scenario():
        loop = asyncio.get_running_loop()
        waker = woken_runs._Waker(SimpleNamespace(), loop)   # as the lifespan installs it
        waker._start = lambda *args: seen.append(probe.get())
        probe.set("the ringer's")
        waker(store, "s1", USER, 1)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert seen == ["fresh"]
