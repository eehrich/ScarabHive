"""A stop ends the streams a browser holds open at once, instead of waiting out uvicorn's graceful timeout.

uvicorn runs the app's shutdown -- where app_state.shutdown_event used to be set -- only after it has waited for
the open connections, and every tab holds one: GET /api/agent-runs/stream, which never ends by itself. Each stop
waited the timeout out and cancelled it ("Cancel 1 running task(s), timeout graceful shutdown exceeded").
lifecycle.announce_stop sets the event as the stop signal arrives.

The server here is a real uvicorn in a process of its own, on a free port, with the real stream route and the
lifespan's two lines from agent_system.app; a thread in it raises SIGINT, as lifecycle and own_console do.
"""
from __future__ import annotations

import asyncio
import http.client
import signal
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from agent_system.services import lifecycle

#: uvicorn's graceful timeout in the server below; a stop that waits it out takes at least this long.
GRACEFUL_S = 6

SERVER = textwrap.dedent('''
    import asyncio, contextlib, signal, sys, threading, time
    from pathlib import Path
    import uvicorn
    from fastapi import FastAPI
    from agent_system import app_state
    from agent_system.api import run_event_routes
    from agent_system.run_events import RunEventBus, RunEventStore, use_run_events
    from agent_system.services.lifecycle import announce_stop

    port, db, announce = int(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
    use_run_events(RunEventStore(Path(db)), RunEventBus())

    @contextlib.asynccontextmanager
    async def lifespan(app):
        app_state.shutdown_event = asyncio.Event()  # as agent_system.app's lifespan
        undo = announce_stop(app_state.shutdown_event) if announce else (lambda: None)
        yield
        undo()
        app_state.shutdown_event.set()

    app = FastAPI(lifespan=lifespan)
    app.include_router(run_event_routes.router)

    @app.post("/__stop")
    def stop():
        threading.Timer(0.2, signal.raise_signal, (signal.SIGINT,)).start()  # as lifecycle._begin
        return {}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info",
                                           timeout_graceful_shutdown=%d))
    try:
        server.run()
    except KeyboardInterrupt:  # the captured SIGINT, raised again once the server is down -- uvicorn.run swallows it
        pass
    print("STOPPED", flush=True)
''' % GRACEFUL_S)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _stop_with_a_stream_open(tmp_path, announce: bool) -> tuple[float, str]:
    """Seconds from the stop to the server's exit, and what it printed."""
    port = _free_port()
    output = tmp_path / "server_output.log"  # a file, not a pipe: a pipe nobody reads blocks the server when full
    printed = lambda: output.read_text(encoding="utf-8", errors="replace")  # noqa: E731
    with open(output, "wb") as sink:
        server = subprocess.Popen([sys.executable, "-c", SERVER, str(port), str(tmp_path / "run_events.db"),
                                   "1" if announce else "0"], stdout=sink, stderr=subprocess.STDOUT)
    stream = None
    try:
        deadline = time.monotonic() + 30
        while True:
            assert server.poll() is None, printed()
            try:
                stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                stream.request("GET", "/api/agent-runs/stream")
                break
            except OSError:
                assert time.monotonic() < deadline, "the server did not come up"
                stream.close()
                time.sleep(0.2)
        response = stream.getresponse()
        assert response.status == 200
        assert response.read(6) == b"retry:", "fixture: the stream is not open"

        stop = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        stop.request("POST", "/__stop")
        assert stop.getresponse().status == 200
        stop.close()
        began = time.monotonic()
        server.wait(timeout=GRACEFUL_S + 20)
        return time.monotonic() - began, printed()
    finally:
        if stream is not None:
            stream.close()
        if server.poll() is None:
            server.kill()
            server.wait(timeout=10)


def test_a_stop_ends_the_run_events_stream_without_waiting_out_the_timeout(tmp_path):
    took, output = _stop_with_a_stream_open(tmp_path, announce=True)

    assert "STOPPED" in output, output
    assert "timeout graceful shutdown exceeded" not in output, output
    assert took < GRACEFUL_S / 2, f"the stop took {took:.1f} s:\n{output}"


def test_without_the_announcement_the_stop_waits_the_timeout_out(tmp_path):
    # the fixture's own check: the open stream does hold the stop up, so the test above measures something
    took, output = _stop_with_a_stream_open(tmp_path, announce=False)

    assert "timeout graceful shutdown exceeded" in output, output
    assert took >= GRACEFUL_S - 0.5


# -- announce_stop itself --------------------------------------------------------------------------------------

@pytest.fixture
def sigint_handler():
    before = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, before)


async def test_the_signal_sets_the_event_and_still_reaches_the_handler_before(sigint_handler):
    called = []
    previous = lambda signum, frame: called.append(signum)  # noqa: E731 -- the handler a server installed
    signal.signal(signal.SIGINT, previous)
    event = asyncio.Event()

    undo = lifecycle.announce_stop(event)
    signal.raise_signal(signal.SIGINT)
    await asyncio.wait_for(event.wait(), timeout=2)

    assert called == [signal.SIGINT]
    undo()
    assert signal.getsignal(signal.SIGINT) is previous


async def test_undo_leaves_a_handler_set_since(sigint_handler):
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    undo = lifecycle.announce_stop(asyncio.Event())
    since = lambda signum, frame: None  # noqa: E731 -- uvicorn restoring its own on the way out
    signal.signal(signal.SIGINT, since)

    undo()

    assert signal.getsignal(signal.SIGINT) is since


def test_off_the_main_thread_nothing_is_installed(sigint_handler):
    import threading

    before = signal.getsignal(signal.SIGINT)
    undone = []

    def in_a_thread():
        undone.append(asyncio.run(_announce_and_undo()))

    async def _announce_and_undo():
        lifecycle.announce_stop(asyncio.Event())()
        return True

    worker = threading.Thread(target=in_a_thread)
    worker.start()
    worker.join(10)
    assert undone == [True] and signal.getsignal(signal.SIGINT) is before
