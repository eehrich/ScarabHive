"""has_session_data: the chat's "Open in" menu offers the memory panel for a session once a memory of it is stored --
from the cache this process holds, and from the session's file in a process that never loaded it. A session whose
memories were all deleted keeps its file and holds nothing."""
import asyncio
from types import SimpleNamespace

import pytest

from plugins.memory.tests.test_plugin_memory_access import Vectors


def _plugin(storage):
    from plugins.memory.plugin import PLUGIN_FACTORY

    plugin = PLUGIN_FACTORY("memory", {}, SimpleNamespace(storage_path=str(storage), auto_extract_keywords=False))
    plugin.server.vector_store = Vectors()
    return plugin


@pytest.fixture
def storage(tmp_path):
    plugin = _plugin(tmp_path / "memories")

    async def seed():
        for session_id in ("s-kept", "s-emptied"):
            await plugin.server._operation_store(session_id=session_id, title="A memory", content="kept", importance=5)
        await plugin.server._operation_delete(session_id="s-emptied", memory_id="mem_001")

    asyncio.run(seed())
    assert (tmp_path / "memories" / "s-emptied.json").exists(), "the emptied session left no file -- this would be vacuous"
    return tmp_path / "memories", plugin


def test_the_process_that_stored_answers_from_its_cache(storage):
    _, plugin = storage
    assert plugin.has_session_data("s-kept", "ada") is True
    assert plugin.has_session_data("s-emptied", "ada") is False
    assert plugin.has_session_data("s-none", "ada") is False


def test_another_process_answers_from_the_file(storage):
    path, _ = storage
    fresh = _plugin(path)
    assert fresh.server._collections_cache == {}, "the fresh plugin has a cache -- the file would not be read"

    assert fresh.has_session_data("s-kept", "ada") is True
    assert fresh.has_session_data("s-emptied", "ada") is False
    assert fresh.has_session_data("s-none", "ada") is False
