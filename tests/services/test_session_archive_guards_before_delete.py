"""The archive deletes the only live copy, so two things must hold just before it does:

* the archive index it registers the tree in is readable -- an unreadable one is
  refused, not replaced by an index holding this one tree;
* the tree is still the one that was checked -- a conversation resumed while its
  zip was written stays live, and the zip this pass made goes again.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_system.services.session_archive import SessionArchive
from agent_system.services.session_archive_store import ArchiveError, ArchiveStore
from agent_system.services.session_manager import SessionManager

USER = "u1"


@pytest.fixture
def sm(tmp_path):
    return SessionManager(storage_path=str(tmp_path / "sessions"))


async def _old_tree(sm: SessionManager, root: str, days: float = 60) -> None:
    await sm.create_session(user_id=USER, title=f"Tree {root}", session_id=root)
    data = await sm.load_session(USER, root)
    data["messages"] = [{"role": "user", "content": "hello"}]
    await sm.save_session(data)
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    user_dir = sm.storage_path / USER
    path = user_dir / f"{root}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = stamp
    path.write_text(json.dumps(record), encoding="utf-8")
    old = time.time() - days * 86400
    os.utime(path, (old, old))
    index_path = user_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index[root]["updated_at"] = stamp
    index_path.write_text(json.dumps(index), encoding="utf-8")
    sm.clear_cache()


# -- an unreadable index ------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unreadable_index_is_refused_and_left_as_it_is(tmp_path):
    store = ArchiveStore(tmp_path / "archive")
    manifest = store.manifest_path(USER)
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"other_tree": {"archive": "2026-01/other_tree.zip"', encoding="utf-8")  # cut off
    before = manifest.read_bytes()

    with pytest.raises(ArchiveError):
        await store.remember(USER, "new_tree", {"archive": "2026-09/new_tree.zip"})

    assert manifest.read_bytes() == before, "the index of every other tree was written over"


@pytest.mark.asyncio
async def test_an_index_that_is_no_object_is_refused(tmp_path):
    store = ArchiveStore(tmp_path / "archive")
    manifest = store.manifest_path(USER)
    manifest.parent.mkdir(parents=True)
    manifest.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ArchiveError):
        await store.remember(USER, "new_tree", {})
    assert manifest.read_text(encoding="utf-8") == "[1, 2]"


@pytest.mark.asyncio
async def test_no_index_yet_is_started(tmp_path):
    store = ArchiveStore(tmp_path / "archive")
    await store.remember(USER, "first", {"archive": "x.zip"})
    assert json.loads(store.manifest_path(USER).read_text(encoding="utf-8")) == {"first": {"archive": "x.zip"}}


@pytest.mark.asyncio
async def test_a_sweep_keeps_the_live_copy_while_the_index_is_unreadable(sm, tmp_path):
    await _old_tree(sm, "root_x")
    archive = SessionArchive(sm, archive_path=str(tmp_path / "archive"), retention_days=30)
    manifest = archive._store.manifest_path(USER)
    manifest.parent.mkdir(parents=True)
    manifest.write_text("not json", encoding="utf-8")

    report = await archive.archive_user(USER)

    assert report.trees == 0 and report.errors
    assert (sm.storage_path / USER / "root_x.json").exists()
    assert manifest.read_text(encoding="utf-8") == "not json"


# -- a tree taken up while its zip was written --------------------------------

def _zips(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "archive").rglob("*.zip"))


@pytest.mark.asyncio
async def test_a_session_resumed_while_its_zip_was_written_stays(sm, tmp_path):
    await _old_tree(sm, "root_r")
    calls = 0

    async def busy():
        nonlocal calls
        calls += 1
        return set() if calls == 1 else {"root_r"}  # idle when checked, running once the zip is there

    archive = SessionArchive(sm, archive_path=str(tmp_path / "archive"), retention_days=30, busy_sessions=busy)
    report = await archive.archive_user(USER)

    assert report.trees == 0 and report.skipped_busy == 1, report.as_dict()
    assert (sm.storage_path / USER / "root_r.json").exists()
    assert _zips(tmp_path) == [], "the zip of a tree that stayed live was kept"
    assert await archive.list_archived(USER) == []


@pytest.mark.asyncio
async def test_a_session_written_while_its_zip_was_written_stays(sm, tmp_path, monkeypatch):
    await _old_tree(sm, "root_w")
    archive = SessionArchive(sm, archive_path=str(tmp_path / "archive"), retention_days=30)
    write_zip = archive._store.write_zip
    session_file = sm.storage_path / USER / "root_w.json"

    def written_meanwhile(*args, **kwargs):
        result = write_zip(*args, **kwargs)
        os.utime(session_file, None)  # another process saved it in the meantime
        return result

    monkeypatch.setattr(archive._store, "write_zip", written_meanwhile)
    report = await archive.archive_user(USER)

    assert report.trees == 0 and report.skipped_busy == 1, report.as_dict()
    assert session_file.exists()
    assert _zips(tmp_path) == []


@pytest.mark.asyncio
async def test_an_untouched_tree_still_moves(sm, tmp_path):
    await _old_tree(sm, "root_u")

    async def busy():
        return set()

    archive = SessionArchive(sm, archive_path=str(tmp_path / "archive"), retention_days=30, busy_sessions=busy)
    report = await archive.archive_user(USER)

    assert report.roots == ["root_u"]
    assert not (sm.storage_path / USER / "root_u.json").exists()
    assert len(_zips(tmp_path)) == 1
