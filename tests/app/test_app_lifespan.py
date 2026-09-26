"""The app's lifespan -- the one build_app registers -- starts what needs a running loop.

A module-level ``lifespan()`` in app.py once started the job cleanup; it was never
the app's, and in production no finished job ever left the process, event buffer
and all. This drives the real lifespan, with only the tool integration left out.
"""
from __future__ import annotations

import pytest

from agent_system.services import background_job_manager as bjm

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_the_apps_lifespan_runs_the_job_cleanup_and_stops_it(tmp_path, monkeypatch):
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
        app = app_mod.build_app()
        manager = bjm.get_background_job_manager()
        assert manager._cleanup_task is None, "fixture: a cleanup was running before the app started"
        async with app.router.lifespan_context(app):
            task = manager._cleanup_task
            assert task is not None and not task.done(), "the app runs without its job cleanup"
        assert manager._cleanup_task is None and task.done(), "the job cleanup outlived the app"
    finally:
        bjm.reset_background_job_manager()
