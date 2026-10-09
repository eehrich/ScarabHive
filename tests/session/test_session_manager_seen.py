"""What counts as seen of a session's file: what this process wrote, and a load it read into a tracker.

SessionManager.changed_on_disk tells the claim of a session (app._bring_the_copy_up_to_date) whether another
process wrote the file since this one last had it -- then the copy in memory is read again. A load only to show
or ask the session (the web UI opening it, a check of its record) used to count as seen as well: what another
process wrote before it was hidden, and the stale copy was saved over that turn.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from agent_system.services.session_cache import SessionCache
from agent_system.services.session_manager import SessionManager


async def _saved(manager: SessionManager, messages: list[str]) -> dict:
    session = await manager.create_session(user_id="alice", session_id="s1", agent_name="coder", llm_profile="normal")
    session["messages"] = [{"role": "user", "content": text} for text in messages]
    await manager.save_session(session)
    return session


async def _written_by_another_process_since(manager: SessionManager, tmp_path) -> None:
    """The file written after what this manager saw, and before anything that comes next -- whatever the
    file system's clock resolution."""
    written = manager._cache.seen["s1"] + 0.01
    os.utime(tmp_path / "alice" / "s1.json", (written, written))
    while time.time() <= written + 0.01:
        await asyncio.sleep(0.005)


async def test_a_load_that_only_shows_the_session_does_not_count_as_seen(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])
    assert manager.changed_on_disk("alice", "s1") is False, "fixture: its own save is not seen"
    await _written_by_another_process_since(manager, tmp_path)

    await manager.load_session("alice", "s1")

    assert manager.changed_on_disk("alice", "s1") is True, "the load hid the other process's write"


async def test_a_load_read_into_a_tracker_counts_as_seen(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])
    await _written_by_another_process_since(manager, tmp_path)

    await manager.load_session("alice", "s1")
    manager.mark_seen("s1")

    assert manager.changed_on_disk("alice", "s1") is False


async def test_what_was_seen_outlives_the_cache_and_is_bounded_on_its_own(tmp_path, monkeypatch):
    """Tied to the cache, sessions only browsed (the cache is an LRU of every load) pushed out the stamps of
    sessions that sit in a tracker -- whose next claim then fell back to guessing by length."""
    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])
    manager._cache.max_size = 1
    await manager.create_session(user_id="alice", session_id="s2", agent_name="coder", llm_profile="normal")
    assert "s1" not in manager._cache.entries, "fixture: s1 was not evicted"
    assert manager.changed_on_disk("alice", "s1") is False, "the eviction took what was seen with it"

    monkeypatch.setattr(SessionCache, "SEEN_KEPT", 2)
    await manager.save_session(await manager.load_session("alice", "s1"))  # s1 seen again: the newest now
    await manager.create_session(user_id="alice", session_id="s3", agent_name="coder", llm_profile="normal")
    assert list(manager._cache.seen) == ["s1", "s3"], "not bounded, or not the one seen longest ago that went"

    manager.clear_cache()
    assert manager._cache.seen == {}


async def test_a_session_read_into_a_tracker_is_seen_as_it_was_read(tmp_path):
    """SessionService.load_and_restore_session marks what it put into the tracker as seen: the next claim does not
    read the file again -- over what a run of this process added and has not saved yet."""
    from types import SimpleNamespace

    from agent_system.servers.agent.components.session_tracking import SessionTracker
    from agent_system.services.session_service import SessionService

    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])
    await _written_by_another_process_since(manager, tmp_path)
    agent = SimpleNamespace(agent_config=None, _session_tracker=SessionTracker({}))

    exists, _count = await SessionService(manager).load_and_restore_session(agent, "alice", "s1")

    assert exists
    assert manager.changed_on_disk("alice", "s1") is False, "what the tracker holds counts as unseen"


async def test_a_deleted_session_leaves_nothing_seen(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])

    await manager.delete_session("alice", "s1", create_backup=False)

    assert "s1" not in manager._cache.seen


async def _renamed(manager: SessionManager) -> None:
    await manager.rename_session("alice", "s1", "a new title")


async def _metadata_updated(manager: SessionManager) -> None:
    await manager.update_session_metadata("alice", "s1", {"tags": ["pinned"]})


async def _vars_replaced(manager: SessionManager) -> None:
    await manager.replace_session_context_vars("alice", "s1", {"aufgabe": "World"})


async def _reinstated(manager: SessionManager) -> None:
    record = await manager.peek_session("alice", "s1")
    await manager.delete_session("alice", "s1", create_backup=False)
    manager._deleted.discard("s1")  # as the archive's restore finds it: gone from the store
    await manager.reinstate_session(record)


@pytest.mark.parametrize("write", [_renamed, _metadata_updated, _vars_replaced, _reinstated],
                         ids=["rename", "metadata", "vars", "reinstate"])
async def test_what_this_process_writes_is_seen(tmp_path, write):
    """A write of this manager is no change another process made: read back for it, the copy in memory lost what
    a run of this process has not saved yet (a rename in the web UI during a run's session-end hooks)."""
    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])
    await _written_by_another_process_since(manager, tmp_path)  # so that only the write can make it seen

    await write(manager)

    assert manager.changed_on_disk("alice", "s1") is False


async def test_a_session_this_process_created_is_seen(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))

    await manager.create_session(user_id="alice", session_id="s1", agent_name="coder", llm_profile="normal")

    assert manager.changed_on_disk("alice", "s1") is False


async def test_an_empty_record_is_read_into_the_tracker_like_any_other(tmp_path):
    """A record with no messages was left out: the copy in memory stayed as it was -- one another process emptied
    since (/undo down to nothing) was saved back by the next save -- and nothing counted as seen."""
    from types import SimpleNamespace

    from agent_system.llm.models import ChatMessage
    from agent_system.servers.agent.components.session_tracking import SessionTracker
    from agent_system.services.session_service import SessionService

    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, [])
    tracker = SessionTracker({})
    tracker.set_session_messages("s1", [ChatMessage(role="user", content="a copy another process emptied since")])
    agent = SimpleNamespace(agent_config=None, _session_tracker=tracker)

    exists, count = await SessionService(manager).load_and_restore_session(agent, "alice", "s1")

    assert (exists, count) == (True, 0)
    assert tracker.get_session_messages("s1") == []
    assert tracker.emptied("s1"), "the next save would not write it empty"
    assert manager.changed_on_disk("alice", "s1") is False


async def test_a_stamp_that_cannot_be_set_does_not_undo_the_restore(tmp_path):
    """The restore has put the session into the tracker when it marks it seen: a failure there reported the whole
    restore as failed, and open_for_run took the session for a new one and dropped what it had just read."""
    from types import SimpleNamespace

    from agent_system.servers.agent.components.session_tracking import SessionTracker
    from agent_system.services.session_service import SessionService

    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, ["first"])

    def broken(session_id):
        raise RuntimeError("no stamp")

    manager.mark_seen = broken
    agent = SimpleNamespace(agent_config=None, _session_tracker=SessionTracker({}))

    assert await SessionService(manager).load_and_restore_session(agent, "alice", "s1") == (True, 1)
