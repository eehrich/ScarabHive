"""An admin stops or restarts the server (POST /admin/server/stop, /restart): what is asked of the process.

Nothing here ends a process: the signal, the exit and the exec are replaced by recorders.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system import own_console
from agent_system.api import admin_endpoints
from agent_system.api.admin_endpoints import router as admin_router
from agent_system.auth.dependencies import require_admin
from agent_system.services import lifecycle


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test starts with a server nobody has asked anything of, and leaves no signal behind."""
    monkeypatch.setattr(lifecycle, "_pending", None)
    monkeypatch.setattr(lifecycle, "_managed", False)
    monkeypatch.setattr(lifecycle, "GRACE_SECONDS", 0.0)
    sent = []
    monkeypatch.setattr(signal, "raise_signal", sent.append)
    return sent


class Loop:
    """Records what would be scheduled; run() does it now."""

    def __init__(self):
        self.calls = []

    def call_later(self, delay, callback):
        self.calls.append((delay, callback))

    def run(self):
        for _, callback in self.calls:
            callback()


# ── the request ───────────────────────────────────────────────────────────

def test_a_stop_is_one_sigint_after_the_answer_is_out(fresh, monkeypatch):
    started = []
    monkeypatch.setattr(lifecycle.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: started.append(kw["target"])))
    loop = Loop()
    lifecycle.request("stop", loop)
    assert lifecycle.pending() == "stop" and fresh == []  # nothing before the answer has gone
    assert [delay for delay, _ in loop.calls] == [lifecycle.ANSWER_SECONDS]
    loop.run()
    assert fresh == [signal.SIGINT] and started == [lifecycle._insist]


def test_a_second_request_is_refused_while_one_is_pending():
    lifecycle.request("stop", Loop())
    with pytest.raises(lifecycle.AlreadyPending, match="already about to stop"):
        lifecycle.request("stop", Loop())
    assert lifecycle.pending() == "stop"


def test_an_unknown_action_is_refused():
    with pytest.raises(ValueError, match="stop, restart"):
        lifecycle.request("reboot", Loop())
    assert lifecycle.pending() is None


def test_a_restart_needs_something_that_starts_the_server_again(monkeypatch):
    with pytest.raises(lifecycle.RestartUnavailable, match="not started by agent-api"):
        lifecycle.request("restart", Loop())  # not started through app.run()
    assert lifecycle.pending() is None
    lifecycle.mark_managed()
    monkeypatch.setattr(lifecycle.os, "name", "nt")
    monkeypatch.setattr(own_console, "_serving", False)  # Windows, but no launcher above it
    assert lifecycle.can_restart() is False
    monkeypatch.setattr(own_console, "_serving", True)
    assert lifecycle.can_restart() is True
    monkeypatch.setattr(lifecycle.os, "name", "posix")
    monkeypatch.setattr(own_console, "_serving", False)
    assert lifecycle.can_restart() is True  # it replaces itself
    lifecycle.request("restart", Loop())
    assert lifecycle.pending() == "restart"


def test_a_server_that_does_not_stop_is_hurried_and_then_ended(fresh, monkeypatch):
    ended = []
    monkeypatch.setattr(lifecycle, "_leave", lambda outright: ended.append((outright, list(fresh))))
    lifecycle._insist()
    # the second SIGINT first (uvicorn stops waiting), the end only after it
    assert ended == [(True, [signal.SIGINT])]


# ── leaving ───────────────────────────────────────────────────────────────

def test_after_a_stop_the_process_just_ends(monkeypatch):
    monkeypatch.setattr(lifecycle, "_pending", "stop")
    lifecycle.finish()  # returns: run() ends, exit code 0
    monkeypatch.setattr(lifecycle, "_pending", None)
    lifecycle.finish()  # a Ctrl+C in the terminal: nothing was asked for


def test_after_a_restart_on_windows_the_exit_code_asks_the_launcher(monkeypatch):
    monkeypatch.setattr(lifecycle, "_pending", "restart")
    monkeypatch.setattr(lifecycle.os, "name", "nt")
    with pytest.raises(SystemExit) as left:
        lifecycle.finish()
    assert left.value.code == own_console.RESTART_EXIT_CODE == 75


def test_after_a_restart_elsewhere_the_process_replaces_itself(monkeypatch):
    replaced = []
    monkeypatch.setattr(lifecycle, "_pending", "restart")
    monkeypatch.setattr(lifecycle.os, "name", "posix")
    monkeypatch.setattr(lifecycle.os, "execv", lambda path, argv: replaced.append((path, argv)))
    monkeypatch.setattr(sys, "orig_argv", ["python", "-m", "agent_system.app"])
    with pytest.raises(SystemExit):  # only because the recorder returns: execv does not
        lifecycle.finish()
    assert replaced == [(sys.executable, [sys.executable, "-m", "agent_system.app"])]


def test_ended_outright_the_exit_code_still_says_restart(monkeypatch):
    codes = []
    monkeypatch.setattr(lifecycle.os, "name", "nt")
    monkeypatch.setattr(lifecycle.os, "_exit", codes.append)
    for pending in ("restart", "stop"):
        monkeypatch.setattr(lifecycle, "_pending", pending)
        with pytest.raises(SystemExit):  # only because the recorder returns: os._exit does not
            lifecycle._leave(outright=True)
    assert codes == [75, 0]


def test_the_launcher_starts_the_server_again_for_as_long_as_it_asks(monkeypatch, capsys):
    codes = iter([75, 75, 3])
    started = []
    monkeypatch.setattr(own_console, "relaunch", lambda target, args: started.append((target, args)) or next(codes))
    assert own_console.relaunch_restarting("agent_system.app:run", []) == 3  # any other code is the server's own
    assert started == [("agent_system.app:run", [])] * 3
    assert capsys.readouterr().out.count("starting it again") == 2


def test_started_through_python_m_own_console_the_server_comes_back_too(monkeypatch, capsys):
    # the VS Code tasks start the API this way, not through agent-api
    codes = iter([75, 0])
    started = []
    monkeypatch.setattr(own_console, "needed", lambda: True)
    monkeypatch.setattr(own_console, "relaunch", lambda target, args: started.append((target, args)) or next(codes))
    monkeypatch.setattr(sys, "argv", ["own_console", "agent_system.app:run"])
    with pytest.raises(SystemExit) as exited:
        own_console.main()
    assert exited.value.code == 0
    assert started == [("agent_system.app:run", [])] * 2


# ── the routes ────────────────────────────────────────────────────────────

@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(username="root")

    async def two_running(admin_user=None):
        return SimpleNamespace(total=2)

    monkeypatch.setattr(admin_endpoints, "list_active_sessions", two_running)
    asked = []
    monkeypatch.setattr(lifecycle, "request", lambda action, loop: asked.append((action, isinstance(loop, asyncio.AbstractEventLoop))))
    app.state.asked = asked
    return app


def test_stop_and_restart_ask_the_lifecycle_and_say_what_is_running(app):
    client = TestClient(app)
    assert client.post("/admin/server/stop").json() == {"status": "stopping", "running_requests": 2}
    assert client.post("/admin/server/restart").json() == {"status": "restarting", "running_requests": 2}
    assert app.state.asked == [("stop", True), ("restart", True)]


@pytest.mark.parametrize("error", [lifecycle.AlreadyPending("The server is already about to stop."),
                                   lifecycle.RestartUnavailable("This server was not started by agent-api")])
def test_a_refusal_is_a_conflict_with_the_reason(app, monkeypatch, error):
    def refuse(action, loop):
        raise error

    monkeypatch.setattr(lifecycle, "request", refuse)
    answer = TestClient(app).post("/admin/server/restart")
    assert answer.status_code == 409 and answer.json()["detail"] == str(error)


def test_a_count_that_fails_does_not_keep_the_server_up(app, monkeypatch):
    async def broken(admin_user=None):
        raise RuntimeError("registry gone")

    monkeypatch.setattr(admin_endpoints, "list_active_sessions", broken)
    assert TestClient(app).post("/admin/server/stop").json() == {"status": "stopping", "running_requests": 0}
    assert app.state.asked == [("stop", True)]


def test_only_an_admin_reaches_the_routes(app):
    del app.dependency_overrides[require_admin]
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/admin/server/stop").status_code in (401, 403)
    assert client.post("/admin/server/restart").status_code in (401, 403)
    assert app.state.asked == []
