"""The shutdown the framework can actually reach.

`capabilities.stop_plugin` looks up ONE attribute and has no fallback:
`getattr(plugin, "stop_plugin", None)`, otherwise it returns. A plugin that
calls its teardown `shutdown`, `close` or `cleanup` is never asked to stop --
and what does not stop here is the background indexer over the semantic index
and the vector store it holds open.

The tests go through the real `capabilities.stop_plugin` rather than calling
the method: what is being pinned is that the framework REACHES it, and a test
that calls `server.stop_plugin()` itself would stay green after a rename.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent_system.plugins import capabilities
from plugins.file_ops.server import FileOpsServer, PLUGIN_FACTORY


@pytest.fixture
def server(tmp_path):
    return FileOpsServer("file_ops", {}, {"allowed_paths": [str(tmp_path)]})


def test_the_factory_is_what_carries_the_hook():
    """The adapter reaches `getattr(adapter, "plugin_server", adapter)`.

    A hook on an inner helper would never be found, so it has to sit on the
    class PLUGIN_FACTORY hands back.
    """
    assert PLUGIN_FACTORY is FileOpsServer
    assert callable(getattr(FileOpsServer, "stop_plugin", None)), (
        "the only name capabilities.stop_plugin looks for is missing")


@pytest.mark.asyncio
async def test_stopping_the_plugin_stops_the_search_engine(server, monkeypatch, caplog):
    """Reached AND finished.

    `capabilities.stop_plugin` swallows whatever the hook raises into a
    warning, so "the first line ran" is not the same as "the teardown
    worked" -- the dead `shutdown` this replaces ended on a
    `super().shutdown()` that does not exist, and only the log said so.
    """
    stopped = []

    async def record():
        stopped.append("search")

    monkeypatch.setattr(server.search_engine, "stop", record)

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.capabilities"):
        await capabilities.stop_plugin(server)

    assert stopped == ["search"], (
        "the framework's stop never reached the index -- the indexer keeps "
        "running and the vector store stays open")
    failed = [r.getMessage() for r in caplog.records
              if r.name == "agent_system.plugins.capabilities"]
    assert not failed, f"the hook did not finish: {failed}"


@pytest.mark.asyncio
async def test_a_plugin_without_the_hook_is_silently_skipped():
    """Why the name matters: there is no fallback to shutdown/close/cleanup.

    This is the behaviour that made the teardown unreachable, pinned so the
    next plugin author can see it rather than rediscover it.
    """
    called = []
    plugin = SimpleNamespace(
        name="old_style",
        shutdown=lambda: called.append("shutdown"),
        close=lambda: called.append("close"),
        cleanup=lambda: called.append("cleanup"),
    )

    await capabilities.stop_plugin(plugin)

    assert called == [], "capabilities.stop_plugin grew a fallback -- good, but untold"
