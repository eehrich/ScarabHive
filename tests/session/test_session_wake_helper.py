"""``wake_session``: the one place that tells a session its work is over.

Its callers are background work that outlived the turn which started it: the
terminal and ssh_control plugins today. (The sub-agent manager still carries
its own copy in `_wake_parent`; adopting this one is its owner's call.) Both
run on a loop that is serving other requests, and both have already recorded
their result by the time they call. That is what the properties below are
about.

``notify`` itself is replaced everywhere here: on a session nobody holds it
STARTS AN agent-cli PROCESS, which a test must never do.
"""
import asyncio
import threading
import time

import pytest

from agent_system.config.models import SessionPresenceConfig
from agent_system.core import session_presence as presence_module
from agent_system.core.session_presence import (PRESENCE_OFF, SessionPresence,
                                                wake_blocked, wake_session)
from types import SimpleNamespace


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Presence on, over a sessions directory of this test's own."""
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    return SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))


@pytest.fixture
def notified(monkeypatch):
    """Records every notify, and in which thread it ran."""
    calls = []

    def fake_notify(self, session_id, user_id):
        calls.append({"session_id": session_id, "user_id": user_id,
                      "thread": threading.get_ident()})
        return "woke_session", ""

    monkeypatch.setattr(SessionPresence, "notify", fake_notify)
    return calls


@pytest.mark.asyncio
async def test_it_notifies_the_session_and_returns_what_came_back(config, notified):
    state = await wake_session(config, "sess-1", "someone", what="a background command")
    assert state == "woke_session"
    assert len(notified) == 1
    assert notified[0]["session_id"] == "sess-1"
    assert notified[0]["user_id"] == "someone"


@pytest.mark.asyncio
async def test_notify_does_not_run_on_the_callers_loop(config, notified):
    """notify takes file locks and may START A PROCESS. On the loop that would
    stall every other request this process is serving."""
    await wake_session(config, "sess-1", "someone")
    assert notified[0]["thread"] != threading.get_ident(), \
        "notify ran on the caller's thread, so it ran on its event loop"


@pytest.mark.asyncio
async def test_with_presence_off_the_guard_holds_instead_of_crashing(tmp_path, monkeypatch,
                                                                    notified, caplog):
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    off = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=False))
    assert await wake_session(off, "sess-1", "someone") == ""
    assert notified == []
    # "" on its own proves nothing here: without the guard the call reaches
    # notify on a None, and the AttributeError comes back out of the except as
    # the very same "". The quiet log is what tells the two apart.
    assert not [r for r in caplog.records if r.levelname == "WARNING"], caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id, user_id", [("", "someone"), ("sess-1", "")])
async def test_without_a_session_or_a_user_it_says_so_and_stops(config, notified, caplog,
                                                                session_id, user_id):
    """Both are injected per tool call. Missing means there is nobody to wake --
    and that has to reach the log, or an operator sees a session that never woke
    and no reason anywhere."""
    assert await wake_session(config, session_id, user_id, what="a job") == ""
    assert notified == []
    # As a WARNING, not a debug line: presence being off is a setting somebody
    # chose, but a wake asked for on a session that is not there is a fault, and
    # at debug level an operator never sees it.
    said = [r for r in caplog.records if "a job" in r.getMessage()]
    assert said and said[0].levelname == "WARNING", caplog.text


@pytest.mark.asyncio
async def test_a_notify_that_throws_costs_a_poll_not_the_operation(config, monkeypatch, caplog):
    """The operation is over and recorded by the time this runs. Letting the
    failure through would land in the caller's error handling, which would
    record a finished job a second time -- as failed."""
    def exploding(self, session_id, user_id):
        raise RuntimeError("the lock file is gone")

    monkeypatch.setattr(SessionPresence, "notify", exploding)

    assert await wake_session(config, "sess-1", "someone", what="a job") == ""
    assert "the lock file is gone" in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_cancelled_caller_is_not_held_up_over_a_message(config, monkeypatch):
    """Deliberately not swallowed: a caller being torn down should go down, and
    a wake that ate its cancellation would keep it alive over a notification."""
    def cancelled(self, session_id, user_id):
        raise asyncio.CancelledError()

    monkeypatch.setattr(SessionPresence, "notify", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await wake_session(config, "sess-1", "someone")


async def test_the_reason_for_a_quiet_skip_is_the_named_one(tmp_path, monkeypatch):
    """The log level is decided by comparing the reason against PRESENCE_OFF.
    If the returned text and the constant ever drift apart, presence being off
    -- a setting somebody chose -- starts warning on every background command.
    """
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    off = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=False))
    assert wake_blocked(off, "sess-1", "someone") == PRESENCE_OFF

    on = SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))
    assert wake_blocked(on, "sess-1", "someone") == ""
    assert wake_blocked(on, "", "someone") != PRESENCE_OFF


@pytest.mark.asyncio
async def test_waking_switched_off_is_named_before_anyone_relies_on_it(tmp_path, monkeypatch,
                                                                      notified):
    """max_wake_depth: 0 is documented as "never wake". Without this check it
    answers "armed" to every caller and wakes none of them."""
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    never = SimpleNamespace(
        session_presence=SessionPresenceConfig(enabled=True, max_wake_depth=0))
    blocked = wake_blocked(never, "sess-1", "someone")
    # Not just the setting's name -- the generic chain message names it too, and
    # would leave "0 = never wake" indistinguishable from a chain that ran out.
    assert "switched off" in blocked and "max_wake_depth" in blocked, blocked
    assert await wake_session(never, "sess-1", "someone") == ""
    assert notified == []


@pytest.mark.asyncio
async def test_a_run_at_the_end_of_the_wake_chain_says_so(tmp_path, monkeypatch, notified):
    """A run that was itself woken this deep wakes nobody. Telling its caller
    the wake is armed would strand exactly the work this feature is for."""
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("HIVE_WAKE_DEPTH", "3")
    config = SimpleNamespace(
        session_presence=SessionPresenceConfig(enabled=True, max_wake_depth=3))
    blocked = wake_blocked(config, "sess-1", "someone")
    assert "3" in blocked and "max_wake_depth" in blocked, blocked
    assert await wake_session(config, "sess-1", "someone") == ""
    assert notified == []


@pytest.mark.asyncio
async def test_an_exit_that_woke_nobody_does_not_read_like_success(config, monkeypatch, caplog):
    """notify has exits that wake nobody -- a sub-agent's session, a session
    that is not on disk. The log is the only place an operator sees it."""
    def queued(self, session_id, user_id):
        return "queued", "a sub-agent's session; the run that spawned it hands it over"

    monkeypatch.setattr(SessionPresence, "notify", queued)

    assert await wake_session(config, "sess-1", "someone", what="a job") == "queued"
    said = [r for r in caplog.records if "a job" in r.getMessage()]
    assert said, caplog.text
    assert said[0].levelname == "WARNING", (said[0].levelname, said[0].getMessage())
    assert "did not wake" in said[0].getMessage().lower(), said[0].getMessage()


@pytest.mark.asyncio
async def test_a_wake_that_worked_does_not_read_like_a_failure(config, notified, caplog):
    """The other half of the same line, and the half that was broken: it was
    decided with `report is logger.info`, which is never true -- a logger hands
    out a new bound method on every attribute access -- so a wake that worked
    reported itself as one that did not."""
    caplog.set_level("INFO")
    assert await wake_session(config, "sess-1", "someone", what="a job") == "woke_session"
    said = [r for r in caplog.records if "a job" in r.getMessage()]
    assert said, caplog.text
    assert said[0].levelname == "INFO", (said[0].levelname, said[0].getMessage())
    assert "did not wake" not in said[0].getMessage().lower(), said[0].getMessage()


@pytest.fixture
def instant_retry(monkeypatch):
    """The retry sleeps ten seconds in production; here it must not."""
    monkeypatch.setattr(presence_module, "WAKE_RETRY_SECONDS", 0)


@pytest.mark.asyncio
async def test_a_ring_that_a_held_session_throws_away_is_repeated(config, monkeypatch,
                                                                  instant_retry):
    """notify only rings a bell. A HELD session takes the marker at its next
    step expecting a hook to hand the input over -- nothing hands over "your
    background command finished", so that ring is lost. Ringing once meant the
    caller ended its turn over work it never heard about again."""
    answers = ["delivered_next_step", "delivered_next_step", "woke_session"]
    rung = []

    def notify(self, session_id, user_id):
        rung.append(session_id)
        return answers[len(rung) - 1], ""

    monkeypatch.setattr(SessionPresence, "notify", notify)

    assert await wake_session(config, "sess-1", "someone") == "woke_session"
    assert len(rung) == 3, rung


@pytest.mark.asyncio
async def test_the_ringing_stops_when_the_caller_read_it_itself(config, monkeypatch,
                                                                instant_retry):
    """A session that dealt with the result by itself must not be started again
    for it -- that costs a whole turn for nothing."""
    rung = []
    read = []

    def notify(self, session_id, user_id):
        rung.append(session_id)
        read.append(True)          # the caller reads it after the first ring
        return "delivered_next_step", ""

    monkeypatch.setattr(SessionPresence, "notify", notify)

    state = await wake_session(config, "sess-1", "someone",
                               still_needed=lambda: not read)
    assert state == "delivered_next_step"
    assert len(rung) == 1, rung


@pytest.mark.asyncio
async def test_the_ringing_is_bounded(config, monkeypatch, instant_retry):
    """A turn that outlasts the budget ends with the marker in place, and
    release() wakes the session on that -- but the ringing itself stops."""
    monkeypatch.setattr(presence_module, "WAKE_RETRIES", 3)
    rung = []

    def never_lets_go(self, session_id, user_id):
        rung.append(session_id)
        return "delivered_next_step", ""

    monkeypatch.setattr(SessionPresence, "notify", never_lets_go)

    assert await wake_session(config, "sess-1", "someone") == "delivered_next_step"
    assert len(rung) == 4, rung   # the first ring plus WAKE_RETRIES


@pytest.mark.asyncio
async def test_the_rings_are_spaced_out(config, monkeypatch):
    """Without the wait the whole budget is spent in microseconds and the
    session never gets the chance to let go -- the ringing would look like it
    happened and reach nobody."""
    monkeypatch.setattr(presence_module, "WAKE_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(presence_module, "WAKE_RETRIES", 2)
    monkeypatch.setattr(SessionPresence, "notify",
                        lambda self, s, u: ("delivered_next_step", ""))

    started = time.monotonic()
    await wake_session(config, "sess-1", "someone")
    # Three rings, so two waits. No upper bound is asserted: a loaded machine
    # may take longer and that is not a failure.
    assert time.monotonic() - started >= 0.08, time.monotonic() - started


@pytest.mark.asyncio
async def test_an_answer_the_loop_does_not_know_stops_it(config, monkeypatch, instant_retry,
                                                         caplog):
    """notify answered "delivered_next_step" both for a session somebody HOLDS
    and for one a wake run was already on its way to. Since 20.09.2026 the
    second is "being_woken", and it stops the ringing without this loop being
    touched -- anything outside RING_AGAIN ends it.

    It is also the best outcome there is, not a failure: a run is on its way and
    the marker is waiting for it. Reported as a warning it would read like the
    exits that wake nobody.
    """
    caplog.set_level("INFO")
    rung = []

    def being_woken(self, session_id, user_id):
        rung.append(session_id)
        return "being_woken", "a wake run is already on its way"

    monkeypatch.setattr(SessionPresence, "notify", being_woken)

    assert await wake_session(config, "sess-1", "someone", what="a job") == "being_woken"
    assert len(rung) == 1, rung
    said = [r for r in caplog.records if "a job" in r.getMessage()]
    assert said, caplog.text
    assert said[0].levelname == "INFO", (said[0].levelname, said[0].getMessage())
    assert "did not wake" not in said[0].getMessage().lower(), said[0].getMessage()


@pytest.mark.asyncio
async def test_the_guard_is_read_fresh_before_each_ring(config, monkeypatch):
    """The guard reads state that changes WHILE the loop waits: a wake run
    delivers and its session reads the result. Asked after the ring instead of
    before it, the ring has already gone out -- and a ring that lands once the
    news is delivered starts a SECOND wake run, a whole turn on the user's
    money. Ten seconds of staleness is exactly the window that matters.

    The count below DOES separate the two orders here, because the state flips
    during the wait: asked afterwards, the guard is ten seconds stale and the
    second ring has already gone out. What no count separates is the two orders
    against a guard that never changes mid-wait -- which is why this test moves
    the state from a task instead of from the fake notify. The margin is
    tenfold and the assertion one-sided, so a loaded machine cannot fail it; if
    it ever flickers, the fix is an injectable wait, not a longer one.
    """
    monkeypatch.setattr(presence_module, "WAKE_RETRY_SECONDS", 0.2)
    rung = []
    delivered = []

    def held(self, session_id, user_id):
        rung.append(session_id)
        return "delivered_next_step", ""

    monkeypatch.setattr(SessionPresence, "notify", held)

    async def deliver_during_the_wait():
        await asyncio.sleep(0.02)   # well inside the 0.2 s between rings
        delivered.append(True)

    asyncio.get_running_loop().create_task(deliver_during_the_wait())

    await wake_session(config, "sess-1", "someone", still_needed=lambda: not delivered)
    assert len(rung) == 1, f"rang again after the news had been delivered: {rung}"
