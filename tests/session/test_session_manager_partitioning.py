"""Tests for the per-parent index partitioning in SessionManager.

The session index is split so that top-level (main) sessions land in
``<user>/index.json`` while sub-agent sessions land in
``<user>/.subs.<parent>.index.json``. With N parallel ``agent-cli``
processes — each running its own main session — sub-agents from
different processes write to different files, eliminating cross-process
contention on the main index.

These tests exercise routing, migration, listing across partitions,
deletion targeting and multi-parent scenarios.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path

from agent_system.services.session_manager import SessionManager


@pytest.fixture
def temp_storage(tmp_path):
    storage = tmp_path / "test_sessions"
    storage.mkdir()
    return str(storage)


@pytest.fixture
def sm(temp_storage):
    return SessionManager(storage_path=temp_storage)


def _user_dir(sm: SessionManager, user_id: str) -> Path:
    return sm.storage_path / user_id


def _read_index(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Routing — where does an entry go on save?
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_session_goes_to_main_index(sm):
    """Top-level (no parent) session lands in <user>/index.json only."""
    await sm.create_session(user_id="u1", title="Main", session_id="main_one")

    ud = _user_dir(sm, "u1")
    main_idx = _read_index(ud / "index.json")
    assert "main_one" in main_idx

    # No sub-index files should exist for a user with only main sessions
    sub_indices = list(ud.glob(".subs.*.index.json"))
    assert sub_indices == []


@pytest.mark.asyncio
async def test_subagent_routes_to_sub_index_after_save(sm):
    """Sub-agent (with parent_session set) lands in per-parent sub-index."""
    main = await sm.create_session(user_id="u1", title="Parent", session_id="parent_a")

    # Sub-session is created top-level first (sub_agent_manager flow), then
    # parent_session is added on save.
    await sm.create_session(user_id="u1", title="Sub", session_id="sub_child_x")
    sub_data = await sm.load_session("u1", "sub_child_x")
    sub_data["parent_session"] = {
        "session_id": main["session_id"],
        "created_at": main["created_at"],
    }
    sub_data["depth"] = 1
    await sm.save_session(sub_data)

    ud = _user_dir(sm, "u1")
    sub_index_path = ud / f".subs.{main['session_id']}.index.json"
    assert sub_index_path.exists(), "sub-index file should exist after save"
    assert "sub_child_x" in _read_index(sub_index_path)


@pytest.mark.asyncio
async def test_save_with_parent_cleans_up_main_entry(sm):
    """Migration: when a sub-agent first saves with a parent, the stale
    main-index entry left behind by the initial create_session is removed."""
    main = await sm.create_session(user_id="u1", title="Parent", session_id="parent_b")
    await sm.create_session(user_id="u1", title="Sub", session_id="sub_migrate")
    # At this point sub_migrate is in main index
    main_idx = _read_index(_user_dir(sm, "u1") / "index.json")
    assert "sub_migrate" in main_idx

    # Now flip it to a sub-agent
    sub_data = await sm.load_session("u1", "sub_migrate")
    sub_data["parent_session"] = {"session_id": main["session_id"], "created_at": "x"}
    await sm.save_session(sub_data)

    main_idx = _read_index(_user_dir(sm, "u1") / "index.json")
    assert "sub_migrate" not in main_idx, (
        "stale main-index entry should have been cleaned up after the "
        "save_session that introduced parent_session"
    )
    sub_idx = _read_index(_user_dir(sm, "u1") / f".subs.{main['session_id']}.index.json")
    assert "sub_migrate" in sub_idx


@pytest.mark.asyncio
async def test_two_parents_get_separate_sub_indices(sm):
    """Two parallel main sessions produce two distinct sub-index files."""
    p1 = await sm.create_session(user_id="u1", title="P1", session_id="parent_p1")
    p2 = await sm.create_session(user_id="u1", title="P2", session_id="parent_p2")

    for parent_id, sub_id in [("parent_p1", "sub_a"), ("parent_p2", "sub_b")]:
        await sm.create_session(user_id="u1", title="S", session_id=sub_id)
        d = await sm.load_session("u1", sub_id)
        d["parent_session"] = {"session_id": parent_id, "created_at": "x"}
        await sm.save_session(d)

    ud = _user_dir(sm, "u1")
    idx_p1 = _read_index(ud / f".subs.parent_p1.index.json")
    idx_p2 = _read_index(ud / f".subs.parent_p2.index.json")

    assert "sub_a" in idx_p1 and "sub_b" not in idx_p1
    assert "sub_b" in idx_p2 and "sub_a" not in idx_p2


# ---------------------------------------------------------------------------
# Listing — must merge across partitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_sessions_merges_main_and_sub_indices(sm):
    """list_sessions returns all main + sub-agent entries, deduped."""
    main = await sm.create_session(user_id="u1", title="Main", session_id="main_l1")
    await sm.create_session(user_id="u1", title="A", session_id="sub_l1_a")
    await sm.create_session(user_id="u1", title="B", session_id="sub_l1_b")
    for sid in ["sub_l1_a", "sub_l1_b"]:
        d = await sm.load_session("u1", sid)
        d["parent_session"] = {"session_id": main["session_id"], "created_at": "x"}
        await sm.save_session(d)

    sessions = await sm.list_sessions("u1")
    ids = {s["session_id"] for s in sessions}
    assert ids == {"main_l1", "sub_l1_a", "sub_l1_b"}

    # Sub entries must carry their parent_session (sub-index version, not main)
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["sub_l1_a"]["parent_session"]["session_id"] == "main_l1"
    assert by_id["sub_l1_b"]["parent_session"]["session_id"] == "main_l1"


@pytest.mark.asyncio
async def test_list_sessions_user_isolation_across_partitions(sm):
    """Two users with sub-agents don't see each other's sessions."""
    m1 = await sm.create_session(user_id="u1", title="Main1", session_id="m_one")
    m2 = await sm.create_session(user_id="u2", title="Main2", session_id="m_two")

    for user, parent_id, sub_id in [("u1", "m_one", "sub_one"), ("u2", "m_two", "sub_two")]:
        await sm.create_session(user_id=user, title="S", session_id=sub_id)
        d = await sm.load_session(user, sub_id)
        d["parent_session"] = {"session_id": parent_id, "created_at": "x"}
        await sm.save_session(d)

    u1_ids = {s["session_id"] for s in await sm.list_sessions("u1")}
    u2_ids = {s["session_id"] for s in await sm.list_sessions("u2")}
    assert u1_ids == {"m_one", "sub_one"}
    assert u2_ids == {"m_two", "sub_two"}


# ---------------------------------------------------------------------------
# Deletion — must target the right partition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_subagent_removes_from_sub_index(sm):
    """Deleting a sub-agent removes it from the per-parent index, not main."""
    main = await sm.create_session(user_id="u1", title="P", session_id="parent_d1")
    await sm.create_session(user_id="u1", title="X", session_id="sub_d_x")
    d = await sm.load_session("u1", "sub_d_x")
    d["parent_session"] = {"session_id": main["session_id"], "created_at": "x"}
    await sm.save_session(d)

    await sm.delete_session("u1", "sub_d_x", create_backup=False)

    sub_idx_path = _user_dir(sm, "u1") / f".subs.{main['session_id']}.index.json"
    sub_idx = _read_index(sub_idx_path) if sub_idx_path.exists() else {}
    assert "sub_d_x" not in sub_idx

    # And it should not reappear via list_sessions
    ids = {s["session_id"] for s in await sm.list_sessions("u1")}
    assert "sub_d_x" not in ids


@pytest.mark.asyncio
async def test_delete_main_session_removes_from_main_only(sm):
    """Deleting a top-level session targets the main index, no sub-indices."""
    await sm.create_session(user_id="u1", title="Solo", session_id="solo_one")
    await sm.delete_session("u1", "solo_one", create_backup=False)

    ud = _user_dir(sm, "u1")
    main_idx = _read_index(ud / "index.json") if (ud / "index.json").exists() else {}
    assert "solo_one" not in main_idx


# ---------------------------------------------------------------------------
# Rebuild — partitioned reconstruction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rebuild_only_includes_matching_parent_partition(sm):
    """_rebuild_index with parent_session_id only includes sub-agents of that parent."""
    p1 = await sm.create_session(user_id="u1", title="P1", session_id="reb_p1")
    p2 = await sm.create_session(user_id="u1", title="P2", session_id="reb_p2")
    for parent_id, sub_id in [("reb_p1", "sub_r1"), ("reb_p2", "sub_r2")]:
        await sm.create_session(user_id="u1", title="S", session_id=sub_id)
        d = await sm.load_session("u1", sub_id)
        d["parent_session"] = {"session_id": parent_id, "created_at": "x"}
        await sm.save_session(d)

    # Wipe sub-indices, then rebuild for parent reb_p1
    ud = _user_dir(sm, "u1")
    for f in ud.glob(".subs.*.index.json"):
        f.unlink()

    rebuilt = await sm._rebuild_index("u1", parent_session_id="reb_p1")
    assert set(rebuilt.keys()) == {"sub_r1"}
    assert "sub_r2" not in rebuilt


@pytest.mark.asyncio
async def test_rebuild_main_only_includes_top_level(sm):
    """_rebuild_index without parent_session_id only includes top-level sessions."""
    await sm.create_session(user_id="u1", title="A", session_id="top_a")
    await sm.create_session(user_id="u1", title="B", session_id="top_b")
    await sm.create_session(user_id="u1", title="X", session_id="sub_skip")
    d = await sm.load_session("u1", "sub_skip")
    d["parent_session"] = {"session_id": "top_a", "created_at": "x"}
    await sm.save_session(d)

    # Force rebuild of main partition
    rebuilt = await sm._rebuild_index("u1", parent_session_id=None)
    assert set(rebuilt.keys()) == {"top_a", "top_b"}
    assert "sub_skip" not in rebuilt


# ---------------------------------------------------------------------------
# Helper smoke tests
# ---------------------------------------------------------------------------

def test_extract_parent_id_from_metadata():
    f = SessionManager._extract_parent_id
    assert f({"parent_session": {"session_id": "P1", "created_at": "x"}}) == "P1"
    assert f({"parent_session": None}) is None
    assert f({}) is None
    assert f({"parent_session": {"session_id": ""}}) is None  # empty string
    assert f({"parent_session": "not-a-dict"}) is None


def test_get_index_path_routing(sm):
    main = sm._get_index_path("u1")
    sub = sm._get_index_path("u1", parent_session_id="parent_xyz")
    assert main.name == "index.json"
    assert sub.name == ".subs.parent_xyz.index.json"
    assert main.parent == sub.parent  # same user dir


def test_get_index_path_validates_parent_id(sm):
    """Invalid parent_session_id (not alphanumeric/underscore/dash) is rejected
    so it can't escape the user dir via path traversal."""
    with pytest.raises(ValueError):
        sm._get_index_path("u1", parent_session_id="../escape")
