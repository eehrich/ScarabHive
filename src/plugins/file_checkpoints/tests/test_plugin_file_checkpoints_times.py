"""When each change was made, in the listing the Files panel groups and stamps by.

A file's moment is the call that left it as it is: written twice in a turn, the second write. A checkpoint's is its
newest file's. A record from before the column was kept reads the moment the change was recorded, and the first
recording into it adds the column.
"""
from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from file_rewind_rig import SESSION, USER, Rig, create, replace
from plugins.file_checkpoints import store as store_module
from plugins.file_checkpoints.store import Journal


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    # Every moment the record takes is a second after the one before: which call set it is then in the order.
    ticks = itertools.count(1_800_000_000)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: float(next(ticks))))
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


def _moment(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


async def test_a_file_is_stamped_with_the_call_that_left_it_and_a_checkpoint_with_its_newest(rig):
    work = rig.work
    (work / "f.txt").write_text("v0")
    await rig.turn("edit", [replace(work / "f.txt", "v0", "v1"), create(work / "g.txt", "G")],
                   [replace(work / "f.txt", "v1", "v2")])

    [entry] = (await rig.checkpoints())["checkpoints"]
    files = {f["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: f for f in entry["files"]}

    assert all(f["changed_at"].endswith("+00:00") for f in entry["files"]), entry
    # f was recorded before g, and written again after it: its moment is the second write's.
    assert _moment(files["f.txt"]["changed_at"]) > _moment(files["g.txt"]["changed_at"]), files
    assert entry["changed_at"] == files["f.txt"]["changed_at"]


async def test_a_record_from_before_the_column_reads_when_it_was_recorded_and_takes_it_on(rig):
    work = rig.work
    await rig.turn("one", [create(work / "a.txt", "A")])
    db = Journal(rig.store, USER, SESSION).db_path
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE changes DROP COLUMN changed_at")       # as a record made before it
        recorded = conn.execute("SELECT recorded_at FROM changes").fetchone()[0]

    [old] = (await rig.checkpoints())["checkpoints"]
    assert _moment(old["files"][0]["changed_at"]) == recorded

    await rig.turn("two", [create(work / "b.txt", "B")])                 # the first recording into it

    listing = (await rig.checkpoints())["checkpoints"]
    assert [len(c["files"]) for c in listing] == [1, 1]
    assert _moment(listing[1]["changed_at"]) > recorded
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT changed_at FROM changes WHERE path LIKE '%b.txt'").fetchone()[0]
