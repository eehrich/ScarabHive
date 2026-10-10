"""A session another PROCESS is running is reported, because nothing else sees it.

``BackgroundJobManager.active_sessions`` says of itself that it answers for
"this process": its background jobs and its own agents' session trackers. A
woken run is neither -- presence starts it as ``agent-cli run --woken``, a
process of its own (``core.session_presence.spawn_wake``), whose events never
reach the API at all. So the session that is working hardest right after a
wake answered ``/api/sessions/active`` as idle, and the panel had no way to
know the turn it was missing existed. Measured on session jrbqugnco7: 13
messages on disk, six on screen, the final answer among the missing.

The lock files are the cross-process truth. What comes back is deliberately
not attachable and carries no request id: there is nothing to follow, only
something to wait for.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agent_system.api.session_endpoints import _add_sessions_running_elsewhere
from agent_system.config.models import SessionPresenceConfig
from agent_system.core import session_presence as sp

USER = "someone"

#: Holds a session and stays alive until killed -- a stand-in for the woken run.
HOLDER = """
import sys, time
from agent_system.core import session_presence as sp
sp.presence.sessions_dir = lambda: __import__("pathlib").Path(sys.argv[1])
store = sp.SessionPresence(__import__("pathlib").Path(sys.argv[1]))
print(store.hold(sys.argv[2], sys.argv[3], "agent_b"), flush=True)
time.sleep(60)
"""


class _Agent:
    def __init__(self, enabled=True):
        self.system_config = type("Cfg", (), {
            "session_presence": SessionPresenceConfig(enabled=enabled)})()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.presence, "sessions_dir", lambda: tmp_path)
    sp.presence._stores.clear()
    yield tmp_path
    sp.presence._stores.clear()


@pytest.fixture
def holder(root):
    processes = []

    def hold(session_id, user_id=USER):
        process = subprocess.Popen([sys.executable, "-c", HOLDER, str(root), session_id, user_id],
                                   stdout=subprocess.PIPE, text=True)
        processes.append(process)
        assert process.stdout.readline().strip() == "True", "the other process could not hold it"
        return process

    yield hold
    for process in processes:
        process.kill()
        process.wait()


def test_a_session_another_process_holds_is_reported(root, holder):
    holder("woken")
    answer: dict = {}

    _add_sessions_running_elsewhere(["woken"], USER, _Agent(), answer)

    assert "woken" in answer, (
        "a session held by another process answered as idle -- which is what made a "
        "woken run look like it never happened")
    assert answer["woken"]["elsewhere"] is True
    # Nothing to follow: that run's events never reach this process, and a client
    # handed a request id would ask GET /events for one nobody here knows.
    assert answer["woken"]["request_id"] is None
    assert answer["woken"]["attachable"] is False


def test_a_session_nobody_holds_is_not_reported(root):
    """Or every idle session in the sidebar would claim to be working.

    The session EXISTS here -- a file, no holder -- because that is the case
    the sidebar is full of, and the one a mutation that reported every asked-for
    id would get away with if the test asked about a name with no session at
    all.
    """
    (root / USER).mkdir(parents=True, exist_ok=True)
    (root / USER / "quiet.json").write_text(
        json.dumps({"agent_name": "agent_b"}), encoding="utf-8")
    assert sp.SessionPresence(root).get("quiet", USER)["status"] == "idle", "fixture holds it"

    answer: dict = {}
    _add_sessions_running_elsewhere(["quiet"], USER, _Agent(), answer)
    assert answer == {}


def test_the_endpoint_actually_asks(root, holder, monkeypatch):
    """Through the ROUTE, not the helper.

    Everything else here calls the function directly, and a mutation that took
    the call out of ``list_active_sessions`` altogether stayed green: the tests
    were measuring a function nobody had to be using. This one goes the way the
    panel goes.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_system.api import session_endpoints as endpoints

    holder("woken")

    async def no_jobs():
        return {}

    monkeypatch.setattr(endpoints, "get_background_job_manager",
                        lambda: type("JM", (), {"active_sessions": staticmethod(no_jobs)})())

    app = FastAPI()
    app.include_router(endpoints.session_router)
    app.dependency_overrides[endpoints.get_optional_user] = lambda: type("U", (), {"username": USER})()
    app.dependency_overrides[endpoints.get_session_manager] = lambda: None
    app.dependency_overrides[endpoints.get_agent_optional] = _Agent

    answer = TestClient(app).get("/api/sessions/active?ids=woken").json()["active"]

    assert answer.get("woken", {}).get("elsewhere") is True, (
        "the route answered idle for a session another process is running")
    assert answer["woken"]["request_id"] is None


def test_what_this_process_already_knows_is_left_alone(root, holder):
    """Jobs and trackers win: they can be followed, and this cannot. Overwritten
    here, a run the page could stream would be downgraded to one it can only
    wait for."""
    holder("woken")
    answer = {"woken": {"request_id": "r-1", "attachable": True,
                        "answered": False, "elsewhere": False}}

    _add_sessions_running_elsewhere(["woken"], USER, _Agent(), answer)

    assert answer["woken"]["request_id"] == "r-1"
    assert answer["woken"]["elsewhere"] is False



def test_a_session_this_process_holds_is_not_reported_as_elsewhere(root):
    """A run's tracker lets go before its presence hold does -- the save and the
    session-end hooks sit between the two -- and an append holds the lock without
    any tracker at all. The lock file answers "running" to this process's own
    probe as well, because the OS lock conflicts across handles of one process.
    Read as another process, it refused a delete with a message that was untrue.
    """
    store = sp.presence_for(_Agent().system_config)
    assert store.hold("mine", USER, "agent_b"), "fixture could not hold the session"
    try:
        assert store.get("mine", USER)["status"] == "running", (
            "fixture is vacuous: the probe does not see this process's own hold")
        answer: dict = {}
        _add_sessions_running_elsewhere(["mine"], USER, _Agent(), answer)
        assert answer == {}, "this process's own hold was reported as another process"
    finally:
        store.release("mine", USER)

def test_presence_switched_off_reports_nothing(root, holder):
    """Without presence there are no lock files to read, and a session held by
    another process is simply not knowable. Saying nothing is the honest
    answer; guessing from the session file's mtime would not be."""
    holder("woken")
    answer: dict = {}
    _add_sessions_running_elsewhere(["woken"], USER, _Agent(enabled=False), answer)
    assert answer == {}


def test_without_an_agent_it_does_not_reach_for_a_config(root, holder):
    """The endpoint's agent dependency is optional, and a poll must not fail
    just because no agent is registered yet."""
    holder("woken")
    answer: dict = {}
    _add_sessions_running_elsewhere(["woken"], USER, None, answer)
    assert answer == {}


def test_a_session_of_another_user_is_not_reported(root, holder):
    """No separate ownership check exists here, and none is needed: presence
    looks in the ASKING user's directory. If that ever stopped being true this
    route would hand out other people's sessions."""
    holder("woken", user_id="somebody_else")
    answer: dict = {}
    _add_sessions_running_elsewhere(["woken"], USER, _Agent(), answer)
    assert answer == {}
