"""Tests for the session archive: whole trees move out, and come back whole.

The archive's whole point is that it deletes the only live copy of a
conversation, so every test here is about the order of that: what must be on
disk before anything is removed, and what must NOT be removed when a session
is young, running, or still on someone's screen.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_system.core.session_presence import SessionPresence
from agent_system.services.session_archive import (
    ArchiveError,
    ArchiveReport,
    ArchiveNotFound,
    SessionArchive,
)
from agent_system.services.session_manager import SessionManager

USER = "u1"


@pytest.fixture
def sm(tmp_path):
    return SessionManager(storage_path=str(tmp_path / "sessions"))


@pytest.fixture
def archive(sm, tmp_path):
    return SessionArchive(
        sm,
        archive_path=str(tmp_path / "session_archive"),
        retention_days=30,
        first_sweep_delay_seconds=0,
    )


def _user_dir(sm: SessionManager, user: str = USER) -> Path:
    return sm.storage_path / user


async def _make_tree(
        sm: SessionManager, root: str, children: list[str], user: str = USER) -> None:
    """A root session with sub-agent sessions below it, each with one message."""
    await sm.create_session(user_id=user, title=f"Tree {root}", session_id=root)
    for child in children:
        await sm.create_session(
            user_id=user, title=f"Sub {child}", session_id=child, parent_session_id=root,
        )
    for session_id in [root, *children]:
        data = await sm.load_session(user, session_id)
        data["messages"] = [{"role": "user", "content": f"hello from {session_id}"}]
        await sm.save_session(data)


def _age(sm: SessionManager, session_ids: list[str], days: float,
         user: str = USER) -> str:
    """Backdate sessions on disk and in their index rows. Returns the ISO stamp.

    Both halves matter: the index row beats the file date when the archive
    works out how old a tree is, so a test that only touches the file leaves
    the tree looking fresh.
    """
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    user_dir = _user_dir(sm, user)
    for session_id in session_ids:
        path = user_dir / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated_at"] = stamp
        path.write_text(json.dumps(data), encoding="utf-8")
        old = time.time() - days * 86400
        os.utime(path, (old, old))
    for index_path in [user_dir / "index.json", *user_dir.glob(".subs.*.index.json")]:
        if not index_path.exists():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for session_id in session_ids:
            if session_id in index:
                index[session_id]["updated_at"] = stamp
        index_path.write_text(json.dumps(index), encoding="utf-8")
    sm.clear_cache()
    return stamp


# ---------------------------------------------------------------------------
# What moves
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_old_tree_moves_into_one_archive(sm, archive, tmp_path):
    await _make_tree(sm, "root_a", ["kid_a1", "kid_a2"])
    _age(sm, ["root_a", "kid_a1", "kid_a2"], days=60)

    report = await archive.archive_user(USER)

    assert report.trees == 1
    assert report.sessions == 3
    assert report.roots == ["root_a"]

    user_dir = _user_dir(sm)
    assert not (user_dir / "root_a.json").exists()
    assert not (user_dir / "kid_a1.json").exists()
    assert list(user_dir.glob(".subs.*.index.json")) == []
    assert json.loads((user_dir / "index.json").read_text(encoding="utf-8")) == {}

    entries = await archive.list_archived(USER)
    assert [e["session_id"] for e in entries] == ["root_a"]
    assert entries[0]["session_count"] == 3

    zip_path = tmp_path / "session_archive" / USER / entries[0]["archive"]
    with zipfile.ZipFile(zip_path) as zf:
        assert set(zf.namelist()) == {
            "root_a.json", "kid_a1.json", "kid_a2.json", "_manifest.json",
        }


@pytest.mark.asyncio
async def test_the_archive_describes_itself(sm, archive, tmp_path):
    """The zip carries the manifest entry, so a lost index can be rebuilt."""
    await _make_tree(sm, "root_b", ["kid_b1"])
    _age(sm, ["root_b", "kid_b1"], days=60)
    await archive.archive_user(USER)

    entry = (await archive.list_archived(USER))[0]
    zip_path = tmp_path / "session_archive" / USER / entry["archive"]
    with zipfile.ZipFile(zip_path) as zf:
        inner = json.loads(zf.read("_manifest.json").decode("utf-8"))
    assert inner["session_id"] == "root_b"
    assert inner["sessions"] == ["root_b", "kid_b1"]


@pytest.mark.asyncio
async def test_a_session_no_index_knows_is_still_swept(sm, archive):
    """A file that drifted out of every index must not become immortal."""
    await _make_tree(sm, "root_c", [])
    _age(sm, ["root_c"], days=60)
    (_user_dir(sm) / "index.json").write_text("{}", encoding="utf-8")
    sm.clear_cache()

    report = await archive.archive_user(USER)

    assert report.roots == ["root_c"]
    assert not (_user_dir(sm) / "root_c.json").exists()


@pytest.mark.asyncio
async def test_a_child_without_its_parent_is_its_own_tree(sm, archive):
    """An orphaned sub-agent session is archived, not stranded."""
    await _make_tree(sm, "root_d", ["kid_d1"])
    (_user_dir(sm) / "root_d.json").unlink()
    _age(sm, ["kid_d1"], days=60)

    report = await archive.archive_user(USER)

    assert report.roots == ["kid_d1"]
    assert not (_user_dir(sm) / "kid_d1.json").exists()


# ---------------------------------------------------------------------------
# What must not move
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_recent_session_keeps_the_whole_tree(sm, archive):
    """Half a conversation is worth nothing, so the young child holds the tree."""
    await _make_tree(sm, "root_e", ["kid_e1", "kid_e2"])
    _age(sm, ["root_e", "kid_e1"], days=60)  # kid_e2 stays fresh

    report = await archive.archive_user(USER)

    assert report.trees == 0
    assert report.skipped_young == 1
    assert (_user_dir(sm) / "root_e.json").exists()
    assert (_user_dir(sm) / "kid_e1.json").exists()


@pytest.mark.asyncio
async def test_a_running_session_holds_its_tree(sm, tmp_path):
    """A 60-day-old session can still be running right now."""
    await _make_tree(sm, "root_f", ["kid_f1"])
    _age(sm, ["root_f", "kid_f1"], days=60)

    async def busy() -> set[str]:
        return {"kid_f1"}

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), busy_sessions=busy,
    )
    report = await archive.archive_user(USER)

    assert report.trees == 0
    assert report.skipped_busy == 1
    assert (_user_dir(sm) / "root_f.json").exists()


@pytest.mark.asyncio
async def test_a_session_OPENED_during_the_sweep_is_spared(sm, tmp_path):
    """The lock files are the only guard a CLI sweep has, so they must be current.

    Read once before the pass, they were a snapshot: with no cap a pass runs
    for minutes, and a conversation opened in minute three was archived in
    minute twelve. The running-jobs check does not cover it -- a CLI process
    has no job manager, and in the API a lock means "somebody has it open",
    not "a job is running".
    """
    await _make_tree(sm, "root_open1", [])
    await _make_tree(sm, "root_open2", ["kid_open2"])
    _age(sm, ["root_open1", "root_open2", "kid_open2"], days=60)

    presence = SessionPresence(root=sm.storage_path)
    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), presence=presence)

    real = archive._archive_tree
    opened = False

    async def open_the_other(user_id, tree, *, dry_run=False):
        nonlocal opened
        result = await real(user_id, tree, dry_run=dry_run)
        if not opened:  # while the pass runs, somebody opens the second one
            opened = presence.hold("kid_open2", USER, "chat") is True
        return result

    archive._archive_tree = open_the_other
    try:
        report = await archive.archive_user(USER)
    finally:
        presence.release("kid_open2", USER)

    assert opened, "the test never opened the session it is about"
    assert report.skipped_busy == 1, "the tree was taken while somebody had it open"
    assert (_user_dir(sm) / "kid_open2.json").exists()
    assert (_user_dir(sm) / "root_open2.json").exists()


@pytest.mark.asyncio
async def test_a_retry_writes_the_SAME_archive_not_a_second_one(sm, archive, tmp_path):
    """The path may not move when the tree loses its newest session.

    It was worked out from `tree.newest`, which on a retry is whatever is left
    -- an older month, so a second zip under a second name, and the first one
    holding sessions nothing points at any more.
    """
    await _make_tree(sm, "root_r", ["kid_r1"])
    # The child is the newest and lands in a different month than the root.
    _age(sm, ["root_r"], days=200)
    _age(sm, ["kid_r1"], days=60)

    real_delete = sm.delete_session
    blocked = {"root_r"}

    async def stubborn(user_id, session_id, create_backup=True):
        if session_id in blocked:
            raise OSError("file is open in another process")
        return await real_delete(user_id, session_id, create_backup=create_backup)

    sm.delete_session = stubborn
    try:
        await archive.archive_user(USER)       # kid_r1 goes, root_r stays
        blocked.clear()
        _age(sm, ["root_r"], days=200)
        await archive.archive_user(USER)       # the retry, now without the newest
    finally:
        sm.delete_session = real_delete

    zips = sorted(q.name for q in (tmp_path / "session_archive" / USER).rglob("*.zip"))
    assert zips == ["root_r.zip"], f"a second archive was written: {zips}"
    entries = await archive.list_archived(USER)
    assert len(entries) == 1
    with zipfile.ZipFile(
            tmp_path / "session_archive" / USER / entries[0]["archive"]) as zf:
        assert sorted(zf.namelist()) == ["_manifest.json", "kid_r1.json", "root_r.json"]


@pytest.mark.asyncio
async def test_a_session_left_behind_is_still_listed_as_a_child(sm, archive):
    """The partitions go first for the churn -- a survivor must get its row back.

    Without it the child is in no index at all: the sidebar shows it as a
    conversation of its own, and `list_child_sessions` of its parent does not
    have it.
    """
    await _make_tree(sm, "root_s", ["kid_s1", "kid_s2"])
    _age(sm, ["root_s", "kid_s1", "kid_s2"], days=60)

    real_delete = sm.delete_session

    async def stubborn(user_id, session_id, create_backup=True):
        if session_id == "kid_s1":
            raise OSError("file is open in another process")
        return await real_delete(user_id, session_id, create_backup=create_backup)

    sm.delete_session = stubborn
    try:
        report = await archive.archive_user(USER)
    finally:
        sm.delete_session = real_delete

    assert report.errors, "the delete was supposed to fail"
    assert (_user_dir(sm) / "kid_s1.json").exists()
    children = await sm.list_child_sessions(USER, "root_s")
    assert [c["session_id"] for c in children] == ["kid_s1"], (
        "the survivor lost its row when the partition went")


@pytest.mark.asyncio
async def test_a_broken_busy_check_archives_nothing(sm, tmp_path):
    """Not knowing which sessions run must not read as 'none of them'."""
    await _make_tree(sm, "root_g", [])
    _age(sm, ["root_g"], days=60)

    async def busy() -> set[str]:
        raise RuntimeError("job manager is gone")

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), busy_sessions=busy,
    )
    with pytest.raises(ArchiveError):
        await archive.archive_user(USER)
    assert (_user_dir(sm) / "root_g.json").exists()


@pytest.mark.asyncio
async def test_a_dry_run_only_reports(sm, archive, tmp_path):
    await _make_tree(sm, "root_h", ["kid_h1"])
    _age(sm, ["root_h", "kid_h1"], days=60)

    report = await archive.archive_user(USER, dry_run=True)

    assert report.trees == 1 and report.sessions == 2
    assert (_user_dir(sm) / "root_h.json").exists()
    assert not (tmp_path / "session_archive" / USER / "index.json").exists()


@pytest.mark.asyncio
async def test_a_retention_under_one_day_is_refused(sm, archive):
    await _make_tree(sm, "root_i", [])
    report = await archive.archive_user(USER, retention_days=0)
    assert report.trees == 0
    assert report.errors and "retention_days" in report.errors[0]
    assert (_user_dir(sm) / "root_i.json").exists()


@pytest.mark.asyncio
async def test_a_failed_archive_leaves_the_sessions_alone(sm, archive, monkeypatch):
    """Nothing is deleted before a readable archive is on disk."""
    await _make_tree(sm, "root_j", ["kid_j1"])
    _age(sm, ["root_j", "kid_j1"], days=60)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(SessionArchive, "_write_zip", staticmethod(boom))

    report = await archive.archive_user(USER)

    assert report.trees == 0
    assert report.errors and "disk full" in report.errors[0]
    assert (_user_dir(sm) / "root_j.json").exists()
    assert (_user_dir(sm) / "kid_j1.json").exists()


@pytest.mark.asyncio
async def test_an_unverifiable_archive_is_thrown_away(sm, archive, tmp_path):
    """A zip that comes back short is not a backup -- and leaves no .tmp behind."""
    await _make_tree(sm, "root_k", ["kid_k1"])
    _age(sm, ["root_k", "kid_k1"], days=60)

    real_namelist = zipfile.ZipFile.namelist

    def short_namelist(self):
        return [name for name in real_namelist(self) if name != "kid_k1.json"]

    zipfile.ZipFile.namelist = short_namelist
    try:
        report = await archive.archive_user(USER)
    finally:
        zipfile.ZipFile.namelist = real_namelist

    assert report.trees == 0
    assert report.errors and "missing" in report.errors[0]
    assert (_user_dir(sm) / "root_k.json").exists()
    assert list((tmp_path / "session_archive").rglob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Coming back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_brings_the_whole_tree_back_unchanged(sm, archive, tmp_path):
    await _make_tree(sm, "root_l", ["kid_l1", "kid_l2"])
    stamp = _age(sm, ["root_l", "kid_l1", "kid_l2"], days=60)
    await archive.archive_user(USER)

    result = await archive.restore(USER, "root_l")

    assert result["restored"] == 3
    restored = await sm.load_session(USER, "kid_l1")
    assert restored["messages"] == [{"role": "user", "content": "hello from kid_l1"}]
    # The conversation comes back as the conversation it was -- not as one
    # written today, which would postpone the next sweep by a month.
    assert restored["updated_at"] == stamp

    index = json.loads((_user_dir(sm) / "index.json").read_text(encoding="utf-8"))
    assert "root_l" in index
    sub_index = json.loads(
        (_user_dir(sm) / ".subs.root_l.index.json").read_text(encoding="utf-8"))
    assert sorted(sub_index) == ["kid_l1", "kid_l2"]

    assert await archive.list_archived(USER) == []
    assert list((tmp_path / "session_archive" / USER).rglob("*.zip")) == []


@pytest.mark.asyncio
async def test_a_restored_tree_is_listed_again(sm, archive):
    """The sidebar reads the index, so a restore has to reach it."""
    await _make_tree(sm, "root_m", ["kid_m1"])
    _age(sm, ["root_m", "kid_m1"], days=60)
    await archive.archive_user(USER)
    assert await sm.list_root_sessions(USER) == []

    await archive.restore(USER, "root_m")

    roots = await sm.list_root_sessions(USER)
    assert [s["session_id"] for s in roots] == ["root_m"]
    children = await sm.list_child_sessions(USER, "root_m")
    assert [s["session_id"] for s in children] == ["kid_m1"]


@pytest.mark.asyncio
async def test_restore_refuses_to_overwrite_a_live_session(sm, archive):
    await _make_tree(sm, "root_n", ["kid_n1"])
    _age(sm, ["root_n", "kid_n1"], days=60)
    await archive.archive_user(USER)

    # Another process -- an agent-cli run, a woken session -- wrote the id back
    # while the tree was away. Not through this manager: its tombstone refuses
    # that outright (see SessionManager.is_deleted), so the conflict this test
    # is about can only come from outside.
    (_user_dir(sm) / "kid_n1.json").write_text(
        json.dumps({
            "session_id": "kid_n1", "user_id": USER, "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:00:00+00:00", "title": "new", "agent_name": "a",
            "llm_profile": "p", "messages": [], "metadata": {},
        }), encoding="utf-8")

    with pytest.raises(ArchiveError, match="live again"):
        await archive.restore(USER, "root_n")

    assert not (_user_dir(sm) / "root_n.json").exists()  # nothing half-written
    assert len(await archive.list_archived(USER)) == 1


@pytest.mark.asyncio
async def test_restoring_something_that_was_never_archived(sm, archive):
    with pytest.raises(ArchiveNotFound, match="No archived session"):
        await archive.restore(USER, "nope")


@pytest.mark.asyncio
async def test_forget_deletes_the_archive_for_good(sm, archive, tmp_path):
    await _make_tree(sm, "root_o", ["kid_o1"])
    _age(sm, ["root_o", "kid_o1"], days=60)
    await archive.archive_user(USER)

    result = await archive.forget(USER, "root_o")

    assert result["sessions"] == 2
    assert await archive.list_archived(USER) == []
    assert list((tmp_path / "session_archive" / USER).rglob("*.zip")) == []
    with pytest.raises(ArchiveError):
        await archive.restore(USER, "root_o")


@pytest.mark.asyncio
async def test_archiving_twice_does_not_archive_it_twice(sm, archive):
    await _make_tree(sm, "root_p", ["kid_p1"])
    _age(sm, ["root_p", "kid_p1"], days=60)

    first = await archive.archive_user(USER)
    second = await archive.archive_user(USER)

    assert first.trees == 1
    assert second.trees == 0
    assert len(await archive.list_archived(USER)) == 1


@pytest.mark.asyncio
async def test_a_sweep_covers_every_user(sm, archive):
    await _make_tree(sm, "root_q", [])
    await sm.create_session(user_id="u2", title="other", session_id="root_r")
    _age(sm, ["root_q"], days=60)
    other = sm.storage_path / "u2" / "root_r.json"
    old = time.time() - 60 * 86400
    os.utime(other, (old, old))
    index_path = sm.storage_path / "u2" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["root_r"]["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    index_path.write_text(json.dumps(index), encoding="utf-8")
    sm.clear_cache()

    reports = await archive.sweep()

    assert reports["u1"].roots == ["root_q"]
    assert reports["u2"].roots == ["root_r"]


# ---------------------------------------------------------------------------
# When a delete does not go through -- the archive is then the only copy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_file_that_cannot_be_deleted_still_leaves_the_tree_archived(
        sm, archive, tmp_path, monkeypatch):
    """The zip is written and registered before anything is removed."""
    await _make_tree(sm, "root_s", ["kid_s1", "kid_s2"])
    _age(sm, ["root_s", "kid_s1", "kid_s2"], days=60)

    real_delete = sm.delete_session

    async def stubborn(user_id, session_id, create_backup=True):
        if session_id == "kid_s1":
            raise OSError("file is open in another process")
        return await real_delete(user_id, session_id, create_backup=create_backup)

    monkeypatch.setattr(sm, "delete_session", stubborn)

    report = await archive.archive_user(USER)

    assert report.trees == 1
    assert any("kid_s1" in error for error in report.errors)
    entries = await archive.list_archived(USER)
    assert entries[0]["session_count"] == 3  # all three are IN the archive
    with zipfile.ZipFile(tmp_path / "session_archive" / USER / entries[0]["archive"]) as zf:
        assert "kid_s1.json" in zf.namelist()
    assert (_user_dir(sm) / "kid_s1.json").exists()  # only the file survived


@pytest.mark.asyncio
async def test_the_next_pass_adds_to_the_archive_instead_of_replacing_it(
        sm, archive, tmp_path, monkeypatch):
    """A survivor must not overwrite the archive that holds its siblings."""
    await _make_tree(sm, "root_t", ["kid_t1", "kid_t2"])
    _age(sm, ["root_t", "kid_t1", "kid_t2"], days=60)

    real_delete = sm.delete_session
    blocked = {"kid_t1"}

    async def stubborn(user_id, session_id, create_backup=True):
        if session_id in blocked:
            raise OSError("file is open in another process")
        return await real_delete(user_id, session_id, create_backup=create_backup)

    monkeypatch.setattr(sm, "delete_session", stubborn)
    await archive.archive_user(USER)

    # Second pass: the file is free again, and it is on its own now -- root and
    # sibling are already gone, so it comes back as its own root.
    blocked.clear()
    _age(sm, ["kid_t1"], days=60)
    report = await archive.archive_user(USER)

    assert report.trees == 1
    assert not (_user_dir(sm) / "kid_t1.json").exists()

    entries = await archive.list_archived(USER)
    members = set()
    for entry in entries:
        with zipfile.ZipFile(tmp_path / "session_archive" / USER / entry["archive"]) as zf:
            members |= set(zf.namelist())
    # Nothing was overwritten out of existence.
    assert {"root_t.json", "kid_t1.json", "kid_t2.json"} <= members


@pytest.mark.asyncio
async def test_a_retried_tree_keeps_what_the_archive_already_held(
        sm, archive, tmp_path, monkeypatch):
    """The root survives, so the retry writes the SAME archive again."""
    await _make_tree(sm, "root_u", ["kid_u1"])
    _age(sm, ["root_u", "kid_u1"], days=60)

    real_delete = sm.delete_session
    blocked = {"root_u"}

    async def stubborn(user_id, session_id, create_backup=True):
        if session_id in blocked:
            raise OSError("file is open in another process")
        return await real_delete(user_id, session_id, create_backup=create_backup)

    monkeypatch.setattr(sm, "delete_session", stubborn)
    await archive.archive_user(USER)
    assert (_user_dir(sm) / "root_u.json").exists()
    assert not (_user_dir(sm) / "kid_u1.json").exists()

    blocked.clear()
    _age(sm, ["root_u"], days=60)
    await archive.archive_user(USER)

    entry = (await archive.list_archived(USER))[0]
    with zipfile.ZipFile(tmp_path / "session_archive" / USER / entry["archive"]) as zf:
        names = zf.namelist()
    # The child exists only in this archive -- a plain rewrite would drop it.
    assert sorted(names) == ["_manifest.json", "kid_u1.json", "root_u.json"]
    assert len(names) == len(set(names))  # and not twice
    assert entry["session_count"] == 2

    result = await archive.restore(USER, "root_u")
    assert result["restored"] == 2


@pytest.mark.asyncio
async def test_a_dry_run_is_not_capped(sm, tmp_path):
    """A report that stops counting at the cap would read as 'that is all'."""
    for index in range(4):
        await _make_tree(sm, f"root_v{index}", [])
    _age(sm, [f"root_v{index}" for index in range(4)], days=60)

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), max_trees_per_sweep=2)

    dry = await archive.archive_user(USER, dry_run=True)
    assert dry.trees == 4
    assert dry.capped is False

    wet = await archive.archive_user(USER)
    assert wet.trees == 2
    assert wet.capped is True
    assert wet.remaining == 2, "a capped pass has to say how much it left"
    assert len(await archive.list_archived(USER)) == 2


@pytest.mark.asyncio
async def test_the_young_are_counted_whole_however_early_the_cap_stops(sm, tmp_path):
    """The count must not depend on how far into the forest the pass got.

    Raised inside the loop it said how many young trees THIS pass walked past
    before the cap ended it -- which climbs every pass as the archivable trees
    ahead of them go, while the log reads it out as "this many are too young".
    Measured on the live store 20.09.2026 over an unchanged set: 328, 667, 970.
    """
    old_trees = [f"root_w{index}" for index in range(4)]
    for root in old_trees:
        await _make_tree(sm, root, [])
    _age(sm, old_trees, days=60)
    # Young ones BEHIND the cap: with one tree per pass, a counter raised in
    # the loop never reaches them.
    for index in range(3):
        await _make_tree(sm, f"root_young{index}", [])

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), max_trees_per_sweep=1)

    first = await archive.archive_user(USER)
    assert first.trees == 1 and first.capped is True
    assert first.skipped_young == 3, "the young trees the pass never reached"
    assert first.remaining == 3, "three old ones are still waiting"

    second = await archive.archive_user(USER)
    assert second.skipped_young == 3, "the same set, so the same number"
    assert second.remaining == 2

    # And once nothing old is left, the pass is not capped and leaves nothing.
    for _ in range(2):
        await archive.archive_user(USER)
    done = await archive.archive_user(USER)
    assert done.trees == 0 and done.capped is False
    assert done.skipped_young == 3 and done.remaining == 0


@pytest.mark.asyncio
async def test_a_second_pass_for_the_same_user_is_refused_not_queued(sm, archive):
    """Two passes share every target file, so only one runs at a time.

    They write the same "<root>.zip.tmp" and replace it over each other; the
    second then re-registers what the first already deleted and reports a
    failure per session. Waiting instead of refusing would be worse: the
    caller asked for a pass NOW and would get an answer about somebody else's
    work, minutes later.
    """
    await _make_tree(sm, "root_p1", [])
    await _make_tree(sm, "root_p2", [])
    _age(sm, ["root_p1", "root_p2"], days=60)

    started, may_finish = asyncio.Event(), asyncio.Event()
    real = archive._archive_tree

    async def slow(user_id, tree, *, dry_run=False):
        started.set()
        await may_finish.wait()
        return await real(user_id, tree, dry_run=dry_run)

    archive._archive_tree = slow
    first = asyncio.create_task(archive.archive_user(USER))
    try:
        # Both waits are bounded, so a guard that stops refusing FAILS this
        # test instead of hanging it: the second pass would queue behind a
        # first one this test deliberately never lets finish, and a hang is
        # not a red test -- pytest-timeout kills the session, and a harness
        # that counts FAILED lines then reads the silence as green.
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(ArchiveError, match="already running"):
            await asyncio.wait_for(archive.archive_user(USER), timeout=5)
    finally:
        may_finish.set()
        with contextlib.suppress(Exception):
            await first

    # And the lock is free again afterwards, so the next pass is not refused.
    assert (await archive.archive_user(USER)).errors == []


@pytest.mark.asyncio
async def test_the_refusal_is_per_user_not_for_everybody(sm, archive):
    """Two users share no file, so one waiting does not hold up the other.

    Without this the lock could be a single one and every test above would
    still pass -- `sweep()` walks the users one after another, so it never
    blocks itself. What it would break is the panel: "Archive now" for one
    user while the daily pass works through another would be refused for a
    conflict that does not exist.
    """
    await _make_tree(sm, "root_o", [], user="other")
    _age(sm, ["root_o"], days=60, user="other")

    await _make_tree(sm, "root_m", [])
    _age(sm, ["root_m"], days=60)

    started, may_finish = asyncio.Event(), asyncio.Event()
    real = archive._archive_tree

    async def slow(user_id, tree, *, dry_run=False):
        started.set()
        await may_finish.wait()
        return await real(user_id, tree, dry_run=dry_run)

    archive._archive_tree = slow
    mine = asyncio.create_task(archive.archive_user(USER))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        archive._archive_tree = real  # the other user's pass runs at full speed
        theirs = await asyncio.wait_for(archive.archive_user("other"), timeout=5)
    finally:
        may_finish.set()
        with contextlib.suppress(Exception):
            await mine

    assert theirs.trees == 1 and theirs.roots == ["root_o"]


@pytest.mark.asyncio
async def test_a_leftover_of_a_killed_pass_is_cleared(sm, archive, tmp_path):
    """A pass that is killed mid-write leaves a .tmp nobody comes back to.

    `_write_zip` clears the one it is about to use, so a tree that is archived
    again heals itself. One whose conversation was resumed is never archived
    again -- and its half-written copy would sit there for good.
    """
    await _make_tree(sm, "root_q", [])
    _age(sm, ["root_q"], days=60)
    month = tmp_path / "session_archive" / USER / "2026-01"
    month.mkdir(parents=True, exist_ok=True)
    stale = month / "root_gone.zip.tmp"
    stale.write_bytes(b"half a zip")
    cold = time.time() - 2 * 3600
    os.utime(stale, (cold, cold))
    # A second one that is still warm: the sweep lock holds THIS process, and
    # a CLI sweep in another may be writing exactly such a file right now.
    warm = month / "root_elsewhere.zip.tmp"
    warm.write_bytes(b"somebody is writing this")

    await archive.archive_user(USER)

    assert not stale.exists()
    assert warm.exists(), "a .tmp another process may be writing was taken away"
    assert (await archive.list_archived(USER))[0]["session_id"] == "root_q"


@pytest.mark.asyncio
async def test_without_a_cap_one_pass_takes_everything_old_enough(sm, archive):
    """The brief was "archive what is older than X", and that is one pass.

    The `archive` fixture sets no cap, so this also holds the default: a pass
    that stops after N leaves the rest sitting until the next day, which turns
    a cleanup into a drip and is not what was asked for.
    """
    assert archive.max_trees_per_sweep == 0, "no cap unless somebody asks for one"
    roots = [f"root_z{index}" for index in range(5)]
    for root in roots:
        await _make_tree(sm, root, [f"kid_{root}"])
    _age(sm, [*roots, *(f"kid_{root}" for root in roots)], days=60)

    report = await archive.archive_user(USER)

    assert report.trees == 5 and report.sessions == 10
    assert report.capped is False and report.remaining == 0
    assert sorted(e["session_id"] for e in await archive.list_archived(USER)) == sorted(roots)


@pytest.mark.asyncio
async def test_a_full_pass_is_capped_even_when_the_rest_is_in_use(sm, tmp_path):
    """A busy tree must not swallow the cap.

    With the busy check first, a pass whose remaining trees are all in use
    skipped every one of them and ran off the end without setting `capped` --
    so a pass that wrote its full quota with work left over said it was a
    finished one, in the log, the CLI and the panel alike.
    """
    for index in range(3):
        await _make_tree(sm, f"root_y{index}", [])
    _age(sm, [f"root_y{index}" for index in range(3)], days=60)

    async def busy() -> set[str]:
        return {"root_y2"}  # the only one left once the cap is reached

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"),
        max_trees_per_sweep=2, busy_sessions=busy)
    report = await archive.archive_user(USER)

    assert report.trees == 2
    assert report.capped is True, "the pass wrote its quota and stopped"
    assert report.skipped_busy == 0, "the cap ended it before that tree was asked about"
    assert report.remaining == 1


@pytest.mark.asyncio
async def test_a_tree_in_use_is_what_the_next_pass_finds(sm, tmp_path):
    """Not capped, yet something is left: "remaining" is not the cap's word."""
    await _make_tree(sm, "root_x1", [])
    await _make_tree(sm, "root_x2", [])
    _age(sm, ["root_x1", "root_x2"], days=60)

    async def busy() -> set[str]:
        return {"root_x1"}

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), busy_sessions=busy)
    report = await archive.archive_user(USER)

    assert report.trees == 1 and report.capped is False
    assert report.skipped_busy == 1
    assert report.remaining == 1, "the running one comes back to the next pass"


@pytest.mark.asyncio
async def test_one_failing_user_does_not_hide_the_others(sm, archive, monkeypatch):
    """A sweep is per user; one that blows up must not take the report with it."""
    await _make_tree(sm, "root_w", [])
    await sm.create_session(user_id="u2", title="other", session_id="root_x")
    _age(sm, ["root_w"], days=60)

    real = archive.archive_user

    async def fails_for_u2(user_id, **kwargs):
        if user_id == "u2":
            raise RuntimeError("that directory is gone")
        return await real(user_id, **kwargs)

    monkeypatch.setattr(archive, "archive_user", fails_for_u2)

    reports = await archive.sweep()

    assert reports["u1"].roots == ["root_w"]
    assert reports["u2"].trees == 0
    assert reports["u2"].errors == ["that directory is gone"]


@pytest.mark.asyncio
async def test_a_session_that_starts_running_during_the_sweep_is_spared(sm, tmp_path):
    """The running-jobs guard is re-read per tree, not once per sweep."""
    for name in ("root_y1", "root_y2"):
        await _make_tree(sm, name, [])
    _age(sm, ["root_y1", "root_y2"], days=60)

    seen: list[int] = []

    async def busy() -> set[str]:
        seen.append(len(seen))
        # Idle when the sweep starts; running by the time the second tree is up.
        return set() if len(seen) < 2 else {"root_y2"}

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), busy_sessions=busy)
    report = await archive.archive_user(USER)

    assert report.roots == ["root_y1"]
    assert report.skipped_busy == 1
    assert (_user_dir(sm) / "root_y2.json").exists()


@pytest.mark.asyncio
async def test_a_held_SUB_AGENT_session_holds_its_tree(sm, tmp_path):
    """The running-jobs check only sees THIS process; the lock file crosses them.

    Against the real SessionPresence, and deliberately on a SUB-AGENT session:
    ``list_for_user`` leaves those out by design ("they belong to the run that
    spawned them"), and they are 59.970 of the 60.196 sessions in this store --
    a guard built on that call would have missed nearly everything it is for.
    """
    await _make_tree(sm, "root_z", ["kid_z1"])
    _age(sm, ["root_z", "kid_z1"], days=60)

    presence = SessionPresence(root=sm.storage_path)
    assert presence.hold("kid_z1", USER, "writer") is True
    try:
        # The call the first version of this guard used says "nobody is busy".
        assert [e["session_id"] for e in presence.list_for_user(USER)] == []

        archive = SessionArchive(
            sm, archive_path=str(tmp_path / "session_archive"), presence=presence)
        report = await archive.archive_user(USER)
    finally:
        presence.release("kid_z1", USER)

    assert report.trees == 0
    assert report.skipped_busy == 1
    assert (_user_dir(sm) / "root_z.json").exists()
    assert (_user_dir(sm) / "kid_z1.json").exists()


@pytest.mark.asyncio
async def test_a_lock_nobody_holds_any_more_stops_nothing(sm, tmp_path):
    """A leftover .lock file is not a running session."""
    await _make_tree(sm, "root_z2", [])
    _age(sm, ["root_z2"], days=60)
    (_user_dir(sm) / "root_z2.lock").write_text("", encoding="utf-8")

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"),
        presence=SessionPresence(root=sm.storage_path))
    report = await archive.archive_user(USER)

    assert report.roots == ["root_z2"]


@pytest.mark.asyncio
async def test_an_archived_session_leaves_no_lock_file_behind(sm, archive):
    """Without presence, nothing else would clear it.

    SessionPresence._probe removes a lock nobody holds ("which goes"), so a
    test that has presence in it proves nothing about this code.
    """
    await _make_tree(sm, "root_lk", ["kid_lk1"])
    _age(sm, ["root_lk", "kid_lk1"], days=60)
    for session_id in ("root_lk", "kid_lk1"):
        (_user_dir(sm) / f"{session_id}.lock").write_text("", encoding="utf-8")

    assert archive._presence is None
    await archive.archive_user(USER)

    assert list(_user_dir(sm).glob("*.lock")) == []


@pytest.mark.asyncio
async def test_a_broken_presence_check_does_not_stop_the_sweep(sm, tmp_path, caplog):
    """Presence is optional, so its failure degrades -- see _held_sessions."""
    await _make_tree(sm, "root_aa", [])
    _age(sm, ["root_aa"], days=60)
    (_user_dir(sm) / "root_aa.lock").write_text("", encoding="utf-8")

    class Presence:
        def get(self, session_id, user_id):
            raise RuntimeError("lock directory vanished")

    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"), presence=Presence())
    report = await archive.archive_user(USER)

    assert report.roots == ["root_aa"]
    assert any("presence check failed" in record.getMessage()
               for record in caplog.records)


# ---------------------------------------------------------------------------
# Trees deeper than one level -- which is what this store actually holds
# ---------------------------------------------------------------------------

async def _make_deep_tree(sm: SessionManager) -> list[str]:
    """root -> kid -> grandkid -> great: four levels, as a book run leaves them."""
    ids = ["deep_root", "deep_kid", "deep_grand", "deep_great"]
    await sm.create_session(user_id=USER, title="Deep", session_id=ids[0])
    for child, parent in zip(ids[1:], ids[:-1]):
        await sm.create_session(
            user_id=USER, title=child, session_id=child, parent_session_id=parent)
    for session_id in ids:
        data = await sm.load_session(USER, session_id)
        data["messages"] = [{"role": "user", "content": f"hello from {session_id}"}]
        await sm.save_session(data)
    return ids


@pytest.mark.asyncio
async def test_a_deep_tree_moves_and_comes_back_whole(sm, archive, tmp_path):
    ids = await _make_deep_tree(sm)
    _age(sm, ids, days=60)

    report = await archive.archive_user(USER)

    assert report.trees == 1
    assert report.roots == ["deep_root"]
    assert report.sessions == 4  # the grandchild and the great-grandchild too
    user_dir = _user_dir(sm)
    assert [p.name for p in user_dir.glob("*.json") if p.name != "index.json"] == []
    assert list(user_dir.glob(".subs.*.index.json")) == []

    entry = (await archive.list_archived(USER))[0]
    with zipfile.ZipFile(tmp_path / "session_archive" / USER / entry["archive"]) as zf:
        assert sorted(zf.namelist()) == sorted(
            [f"{i}.json" for i in ids] + ["_manifest.json"])

    await archive.restore(USER, "deep_root")

    for child, parent in zip(ids[1:], ids[:-1]):
        partition = json.loads(
            (user_dir / f".subs.{parent}.index.json").read_text(encoding="utf-8"))
        assert list(partition) == [child]
    assert [s["session_id"] for s in await sm.list_root_sessions(USER)] == ["deep_root"]


@pytest.mark.asyncio
async def test_a_fresh_grandchild_holds_the_whole_deep_tree(sm, archive):
    """The young-session guard has to reach all the way down, not one level."""
    ids = await _make_deep_tree(sm)
    _age(sm, ids[:-1], days=60)  # everything but the great-grandchild

    report = await archive.archive_user(USER)

    assert report.trees == 0
    assert report.skipped_young == 1
    assert (_user_dir(sm) / "deep_root.json").exists()


@pytest.mark.asyncio
async def test_a_held_great_grandchild_holds_the_whole_deep_tree(sm, tmp_path):
    ids = await _make_deep_tree(sm)
    _age(sm, ids, days=60)

    presence = SessionPresence(root=sm.storage_path)
    assert presence.hold("deep_great", USER, "writer") is True
    try:
        archive = SessionArchive(
            sm, archive_path=str(tmp_path / "session_archive"), presence=presence)
        report = await archive.archive_user(USER)
    finally:
        presence.release("deep_great", USER)

    assert report.trees == 0
    assert report.skipped_busy == 1


# ---------------------------------------------------------------------------
# What counts as the age of a session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_index_timestamp_beats_the_file_date(sm, archive):
    """A restored backup has today's mtimes; the conversation is still old."""
    await _make_tree(sm, "root_ts", [])
    _age(sm, ["root_ts"], days=60)
    os.utime(_user_dir(sm) / "root_ts.json", None)  # touched, as a copy would be

    report = await archive.archive_user(USER)

    assert report.roots == ["root_ts"]


@pytest.mark.asyncio
async def test_the_file_date_decides_when_no_index_knows_it(sm, archive):
    await _make_tree(sm, "root_tm", [])
    (_user_dir(sm) / "index.json").write_text("{}", encoding="utf-8")
    sm.clear_cache()
    old = time.time() - 60 * 86400
    os.utime(_user_dir(sm) / "root_tm.json", (old, old))

    assert (await archive.archive_user(USER)).roots == ["root_tm"]


@pytest.mark.asyncio
async def test_an_age_nobody_can_read_counts_as_young(sm, archive, monkeypatch):
    """The unsafe default here would be 'older than everything'."""
    await _make_tree(sm, "root_nostat", [])
    (_user_dir(sm) / "index.json").write_text("{}", encoding="utf-8")
    sm.clear_cache()

    real_scandir = os.scandir

    class NoStat:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_file(self):
            return self._entry.is_file()

        def stat(self):
            raise OSError("cannot stat")

    def blind(path):
        return [NoStat(entry) for entry in real_scandir(path)]

    monkeypatch.setattr(
        "agent_system.services.session_archive.os.scandir",
        lambda path: blind(path) if str(path).endswith(USER) else real_scandir(path))

    report = await archive.archive_user(USER)

    assert report.trees == 0
    assert report.skipped_young == 1
    assert (_user_dir(sm) / "root_nostat.json").exists()


@pytest.mark.asyncio
async def test_a_deletion_backup_is_not_a_session(sm, archive):
    """.backup_* files live in the same directory and are not conversations."""
    await _make_tree(sm, "root_bk", [])
    _age(sm, ["root_bk"], days=60)
    backup = _user_dir(sm) / ".backup_root_bk_1700000000.json"
    backup.write_text('{"session_id": "root_bk"}', encoding="utf-8")
    old_enough = time.time() - 60 * 86400
    os.utime(backup, (old_enough, old_enough))  # or it is skipped for being young

    report = await archive.archive_user(USER)

    assert report.trees == 1     # one tree, not two
    assert report.sessions == 1  # the backup is not a second session
    assert backup.exists()       # and it is left alone


@pytest.mark.asyncio
async def test_a_session_that_is_its_own_parent_is_still_swept(sm, archive):
    """A partition naming its own session would put it in no tree at all."""
    await _make_tree(sm, "root_self", [])
    _age(sm, ["root_self"], days=60)
    index = json.loads(
        (_user_dir(sm) / "index.json").read_text(encoding="utf-8"))
    (_user_dir(sm) / ".subs.root_self.index.json").write_text(
        json.dumps({"root_self": index["root_self"]}), encoding="utf-8")

    assert (await archive.archive_user(USER)).roots == ["root_self"]


@pytest.mark.asyncio
async def test_a_user_without_a_directory_is_no_error(sm, archive):
    report = await archive.archive_user("nobody")
    assert report.trees == 0 and report.errors == []


# ---------------------------------------------------------------------------
# The archive is the only copy -- what must never be able to drop it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lost_manifest_does_not_cost_the_archives(sm, archive, tmp_path, monkeypatch):
    """Whether to add to an archive or replace it is decided by the FILE.

    Reading that from the manifest would mean a manifest that was lost or could
    not be parsed turns into overwritten archives -- the one failure here that
    cannot be undone.
    """
    await _make_tree(sm, "root_lm", ["kid_lm1"])
    _age(sm, ["root_lm", "kid_lm1"], days=60)

    real_delete = sm.delete_session
    blocked = {"root_lm"}

    async def stubborn(user_id, session_id, create_backup=True):
        if session_id in blocked:
            raise OSError("file is open in another process")
        return await real_delete(user_id, session_id, create_backup=create_backup)

    monkeypatch.setattr(sm, "delete_session", stubborn)
    await archive.archive_user(USER)          # kid archived and deleted, root stuck

    # The manifest is gone -- a corrupt one would read the same way.
    (tmp_path / "session_archive" / USER / "index.json").write_text("{}", encoding="utf-8")
    blocked.clear()
    _age(sm, ["root_lm"], days=60)
    await archive.archive_user(USER)

    entry = (await archive.list_archived(USER))[0]
    with zipfile.ZipFile(tmp_path / "session_archive" / USER / entry["archive"]) as zf:
        names = sorted(zf.namelist())
    assert names == ["_manifest.json", "kid_lm1.json", "root_lm.json"]


@pytest.mark.asyncio
async def test_the_zip_describes_itself_without_claiming_its_own_size(sm, archive, tmp_path):
    """The in-zip manifest is what an index rebuild would read."""
    await _make_tree(sm, "root_sd", ["kid_sd1"])
    _age(sm, ["root_sd", "kid_sd1"], days=60)
    await archive.archive_user(USER)

    entry = (await archive.list_archived(USER))[0]
    with zipfile.ZipFile(tmp_path / "session_archive" / USER / entry["archive"]) as zf:
        inner = json.loads(zf.read("_manifest.json").decode("utf-8"))
    assert inner["sessions"] == ["root_sd", "kid_sd1"]
    assert inner["title"] == "Tree root_sd"
    # A file cannot state its own size, and a later add would make the count stale.
    assert "bytes" not in inner and "session_count" not in inner
    assert entry["bytes"] > 0 and entry["session_count"] == 2


@pytest.mark.asyncio
async def test_a_manifest_pointing_out_of_the_archive_is_refused(sm, archive, tmp_path):
    """index.json is a file on disk, not a trusted input -- and forget() unlinks it."""
    await _make_tree(sm, "root_tr", [])
    _age(sm, ["root_tr"], days=60)
    await archive.archive_user(USER)

    outsider = tmp_path / "not_an_archive.zip"
    outsider.write_bytes(b"PK")
    manifest_path = tmp_path / "session_archive" / USER / "index.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root_tr"]["archive"] = "../../not_an_archive.zip"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArchiveNotFound, match="not in this archive"):
        await archive.restore(USER, "root_tr")
    await archive.forget(USER, "root_tr")
    assert outsider.exists()


# ---------------------------------------------------------------------------
# Restore, when it does not go through in one piece
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_restore_that_stopped_halfway_can_be_finished(sm, archive, monkeypatch):
    """Otherwise the half-restored tree is refused forever by its own leftovers."""
    await _make_tree(sm, "root_hr", ["kid_hr1", "kid_hr2"])
    _age(sm, ["root_hr", "kid_hr1", "kid_hr2"], days=60)
    await archive.archive_user(USER)

    real_reinstate = sm.reinstate_session
    failing = {"kid_hr2"}

    async def stops_halfway(session_data):
        if session_data["session_id"] in failing:
            raise OSError("disk full")
        return await real_reinstate(session_data)

    monkeypatch.setattr(sm, "reinstate_session", stops_halfway)
    with pytest.raises(OSError):
        await archive.restore(USER, "root_hr")
    assert (_user_dir(sm) / "root_hr.json").exists()   # two of three are back
    assert not (_user_dir(sm) / "kid_hr2.json").exists()

    failing.clear()
    result = await archive.restore(USER, "root_hr")

    assert result["restored"] == 3
    assert (_user_dir(sm) / "kid_hr2.json").exists()
    assert await archive.list_archived(USER) == []
    children = await sm.list_child_sessions(USER, "root_hr")
    assert sorted(s["session_id"] for s in children) == ["kid_hr1", "kid_hr2"]


@pytest.mark.asyncio
async def test_a_live_session_with_the_same_id_is_still_refused(sm, archive):
    """Resumable must not mean 'overwrites whatever is there'."""
    await _make_tree(sm, "root_dr", ["kid_dr1"])
    _age(sm, ["root_dr", "kid_dr1"], days=60)
    await archive.archive_user(USER)

    (_user_dir(sm) / "kid_dr1.json").write_text(
        json.dumps({
            "session_id": "kid_dr1", "user_id": USER, "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-19T00:00:00+00:00", "title": "something else",
            "agent_name": "a", "llm_profile": "p", "messages": [], "metadata": {},
        }), encoding="utf-8")

    with pytest.raises(ArchiveError, match="live again"):
        await archive.restore(USER, "root_dr")


@pytest.mark.asyncio
async def test_an_archive_holding_someone_elses_session_is_refused(sm, archive, tmp_path):
    """reinstate_session writes to the user_id INSIDE the document."""
    await _make_tree(sm, "root_fo", [])
    _age(sm, ["root_fo"], days=60)
    await archive.archive_user(USER)

    entry = (await archive.list_archived(USER))[0]
    zip_path = tmp_path / "session_archive" / USER / entry["archive"]
    with zipfile.ZipFile(zip_path) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    stolen = json.loads(members["root_fo.json"].decode("utf-8"))
    stolen["user_id"] = "someone_else"
    members["root_fo.json"] = json.dumps(stolen).encode("utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)

    with pytest.raises(ArchiveError, match="belong to someone else"):
        await archive.restore(USER, "root_fo")
    assert not (sm.storage_path / "someone_else").exists()


# ---------------------------------------------------------------------------
# The loop that actually runs in production
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_sweep_loop_keeps_going_after_a_failure(sm, tmp_path):
    """One bad sweep must not end the daily one for the life of the process."""
    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"),
        first_sweep_delay_seconds=0, sweep_interval_hours=1 / 3600,  # one second
    )
    calls: list[int] = []
    second = asyncio.Event()

    async def flaky(*, dry_run=False):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("the first one blows up")
        second.set()
        return {}

    archive.sweep = flaky
    task = asyncio.create_task(archive.sweep_loop())
    try:
        await asyncio.wait_for(second.wait(), timeout=10)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(calls) >= 2
    assert task.done()


@pytest.mark.asyncio
async def test_the_sweep_loop_stops_when_it_is_cancelled(sm, tmp_path):
    """Shutdown must not have to wait out first_sweep_delay_seconds."""
    archive = SessionArchive(
        sm, archive_path=str(tmp_path / "session_archive"),
        first_sweep_delay_seconds=3600,
    )
    task = asyncio.create_task(archive.sweep_loop())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert task.done()


# ---------------------------------------------------------------------------
# What the panel and the CLI read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_listing_puts_the_newest_archive_first(sm, archive):
    for name in ("root_o1", "root_o2", "root_o3"):
        await _make_tree(sm, name, [])
        _age(sm, [name], days=60)
        await archive.archive_user(USER)   # one at a time, so archived_at differs

    entries = await archive.list_archived(USER)

    assert [e["session_id"] for e in entries] == ["root_o3", "root_o2", "root_o1"]


def test_the_report_carries_every_field_the_panel_reads():
    """as_dict is the panel's contract; a dropped key is a silent blank."""
    report = ArchiveReport(user_id="u1")
    assert set(report.as_dict()) == {
        "user_id", "dry_run", "trees", "sessions", "bytes_live", "bytes_archived",
        "skipped_young", "skipped_busy", "capped", "remaining", "roots", "errors",
    }


@pytest.mark.asyncio
async def test_an_archived_tree_leaves_no_index_partitions_behind(sm, archive):
    """A leftover .subs file is what the sidebar reads as 'has children'."""
    ids = await _make_deep_tree(sm)
    _age(sm, ids, days=60)
    assert len(list(_user_dir(sm).glob(".subs.*.index.json"))) == 3

    await archive.archive_user(USER)

    assert list(_user_dir(sm).glob(".subs.*.index.json")) == []
    assert json.loads((_user_dir(sm) / "index.json").read_text(encoding="utf-8")) == {}


@pytest.mark.asyncio
async def test_a_refusal_from_the_manager_reaches_the_caller_readably(sm, archive, monkeypatch):
    """reinstate_session raises ValueError; the panel must not see a 500."""
    await _make_tree(sm, "root_ve", [])
    _age(sm, ["root_ve"], days=60)
    await archive.archive_user(USER)

    async def refuses(session_data):
        raise ValueError("Session root_ve is live -- refusing to overwrite it")

    monkeypatch.setattr(sm, "reinstate_session", refuses)

    with pytest.raises(ArchiveError, match="Could not restore root_ve"):
        await archive.restore(USER, "root_ve")
    assert len(await archive.list_archived(USER)) == 1  # still archived


@pytest.mark.asyncio
async def test_a_restored_session_can_be_written_to_again(sm, archive):
    """The tombstone from archiving must not outlive the restore."""
    await _make_tree(sm, "root_tb", [])
    _age(sm, ["root_tb"], days=60)
    await archive.archive_user(USER)
    assert sm.is_deleted("root_tb") is True

    await archive.restore(USER, "root_tb")

    assert sm.is_deleted("root_tb") is False
    data = await sm.load_session(USER, "root_tb")
    data["messages"].append({"role": "user", "content": "carrying on"})
    await sm.save_session(data)   # SessionDeletedError if the tombstone stayed

    assert len((await sm.load_session(USER, "root_tb"))["messages"]) == 2
