"""The app's lifespan -- the one build_app registers -- starts what needs a running loop.

A module-level ``lifespan()`` in app.py once started the job cleanup; it was never
the app's, and in production no finished job ever left the process, event buffer
and all. This drives the real lifespan, with only the tool integration left out.
"""
from __future__ import annotations

import asyncio
import signal

import pytest

from agent_system.services import background_job_manager as bjm

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def app_without_tools(tmp_path, monkeypatch):
    """The real app, its tool integration and batch manager left out."""
    from agent_system import app as app_mod

    async def no_tools(*args, **kwargs):
        raise RuntimeError("no tool integration in this test")

    async def no_batch_manager(*args, **kwargs):
        return None

    monkeypatch.setattr(app_mod, "initialize_tools", no_tools)
    monkeypatch.setattr(app_mod, "start_batch_queue_manager", no_batch_manager)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    bjm.reset_background_job_manager()
    try:
        yield app_mod.build_app()
    finally:
        bjm.reset_background_job_manager()


async def test_the_apps_lifespan_runs_the_job_cleanup_and_stops_it(app_without_tools):
    app = app_without_tools
    manager = bjm.get_background_job_manager()
    assert manager._cleanup_task is None, "fixture: a cleanup was running before the app started"
    async with app.router.lifespan_context(app):
        task = manager._cleanup_task
        assert task is not None and not task.done(), "the app runs without its job cleanup"
    assert manager._cleanup_task is None and task.done(), "the job cleanup outlived the app"


@pytest.fixture
def sigint_recorded():
    """SIGINT goes to a recorder, as it goes to uvicorn's handler in a server; restored afterwards."""
    received = []

    def recorder(signum, frame):
        received.append(signum)

    before = signal.signal(signal.SIGINT, recorder)
    try:
        yield recorder, received
    finally:
        signal.signal(signal.SIGINT, before)


async def test_a_stop_signal_sets_the_shutdown_event_while_the_app_runs(app_without_tools, sigint_recorded):
    # uvicorn runs the app's shutdown only after it waited for the open connections: the streams that end on this
    # event have to hear of the stop as the signal comes (services/lifecycle.announce_stop)
    from agent_system import app_state

    app, (recorder, received) = app_without_tools, sigint_recorded
    async with app.router.lifespan_context(app):
        assert not app_state.shutdown_event.is_set(), "fixture: the event is set before any stop"
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(app_state.shutdown_event.wait(), timeout=2)
        assert received == [signal.SIGINT], "the server's own handler did not get the signal"
    assert signal.getsignal(signal.SIGINT) is recorder, "the app left its handler behind"


async def test_an_app_ending_by_an_error_leaves_no_handler_behind(app_without_tools, sigint_recorded):
    app, (recorder, _received) = app_without_tools, sigint_recorded
    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("boom")
    assert signal.getsignal(signal.SIGINT) is recorder
