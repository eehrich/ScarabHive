"""SessionManager: a rename is a read-modify-write under the session's lock, and the
per-session locks do not pile up for every session the process ever touched."""
from __future__ import annotations

import asyncio
import gc

import pytest

from agent_system.services.session_manager import (
    SessionManager,
    SessionNotFoundError,
    SessionPermissionError,
)


@pytest.fixture
def sm(tmp_path):
    return SessionManager(storage_path=str(tmp_path / "sessions"))


@pytest.mark.asyncio
async def test_a_rename_keeps_a_write_that_landed_after_its_caller_could_have_loaded(sm):
    await sm.create_session(user_id="u", title="alt", session_id="s1")
    # Held: the rename waits for the session's lock, and the metadata update that holds it lands first.
    lock = await sm._get_session_lock("s1")
    async with lock:
        renaming = asyncio.create_task(sm.rename_session("u", "s1", "neu"))
        await asyncio.sleep(0.05)
        assert not renaming.done(), "the rename did not wait for the session's lock"
        path = sm._get_session_path("u", "s1")
        record = await sm._read_to_change(path, "u", "s1")
        record["metadata"]["tags"] = ["kept"]
        await sm._write_session_file(path, record)
    await renaming

    stored = await sm.load_session("u", "s1", bypass_cache=True)
    assert stored["title"] == "neu"
    assert stored["metadata"]["tags"] == ["kept"]
    assert {row["session_id"]: row["title"] for row in await sm.list_sessions("u")}["s1"] == "neu"


@pytest.mark.asyncio
async def test_a_write_queued_behind_the_renames_read_is_not_written_over(sm):
    """A load and a whole-record save let a write in between the two: its field (``depth``, no metadata, so
    save_session's merge does not keep it) was written over by the rename's older copy."""
    await sm.create_session(user_id="u", title="alt", session_id="s1")
    read = sm._read_session_file_async
    placing: list = []

    async def read_and_let_a_writer_queue(path):
        record = await read(path)
        if not placing:
            placing.append(asyncio.create_task(sm.set_session_place("u", "s1", depth=3)))
            await asyncio.sleep(0.05)
        return record

    sm.clear_cache()  # read from the file, as a session no load has cached
    sm._read_session_file_async = read_and_let_a_writer_queue
    await sm.rename_session("u", "s1", "neu")
    await placing[0]
    sm._read_session_file_async = read

    stored = await sm.load_session("u", "s1", bypass_cache=True)
    assert stored["title"] == "neu"
    assert stored.get("depth") == 3


@pytest.mark.asyncio
async def test_renaming_a_missing_session_says_so(sm):
    with pytest.raises(SessionNotFoundError):
        await sm.rename_session("u", "nope", "x")


@pytest.mark.asyncio
async def test_renaming_another_users_session_is_refused(sm):
    await sm.create_session(user_id="alice", title="hers", session_id="s2")
    with pytest.raises(SessionPermissionError):
        await sm.rename_session("bob", "s2", "mine now")
    assert (await sm.load_session("alice", "s2", bypass_cache=True))["title"] == "hers"


@pytest.mark.asyncio
async def test_a_session_lock_goes_once_nobody_holds_it(sm):
    for n in range(50):
        await sm.create_session(user_id="u", title="t", session_id=f"s{n}")
        await sm.update_session_metadata("u", f"s{n}", {"x": n})
    gc.collect()
    assert len(sm._session_locks) == 0


@pytest.mark.asyncio
async def test_a_held_lock_is_the_one_the_next_caller_gets(sm):
    lock = await sm._get_session_lock("s")
    async with lock:
        gc.collect()
        assert await sm._get_session_lock("s") is lock
