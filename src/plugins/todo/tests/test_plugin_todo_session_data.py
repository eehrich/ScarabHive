"""has_session_data: the chat's "Open in" menu offers the Todos panel for a session once a task of it was created --
from the cache this process holds, and from the session's file in a process that never loaded it. A session whose
tasks were all deleted keeps its file and holds nothing."""
import asyncio
from types import SimpleNamespace

import pytest


def _plugin(storage):
    from plugins.todo.plugin import PLUGIN_FACTORY

    return PLUGIN_FACTORY("todo", {}, SimpleNamespace(storage_path=str(storage)))


@pytest.fixture
def storage(tmp_path):
    plugin = _plugin(tmp_path / "todos")

    async def seed():
        for session_id in ("s-kept", "s-emptied"):
            created = await plugin.server.create_todo(title="A task", context={"session_id": session_id})
        await plugin.server.delete_todo(task_id=created["task_id"], context={"session_id": "s-emptied"})

    asyncio.run(seed())
    assert (tmp_path / "todos" / "s-emptied.json").exists(), "the emptied session left no file -- this would be vacuous"
    return tmp_path / "todos", plugin


def test_the_process_that_created_answers_from_its_cache(storage):
    _, plugin = storage
    assert plugin.has_session_data("s-kept", "ada") is True
    assert plugin.has_session_data("s-emptied", "ada") is False
    assert plugin.has_session_data("s-none", "ada") is False


def test_another_process_answers_from_the_file(storage):
    path, _ = storage
    fresh = _plugin(path)
    assert fresh.server._sessions == {}, "the fresh plugin has a cache -- the file would not be read"

    assert fresh.has_session_data("s-kept", "ada") is True
    assert fresh.has_session_data("s-emptied", "ada") is False
    assert fresh.has_session_data("s-none", "ada") is False
