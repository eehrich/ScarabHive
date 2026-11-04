"""A write that moves a session file's ``updated_at`` moves its index row's too.

The archive ages a session by its index row first (``SessionArchive._collect_trees``),
and the session list sorts by it. ``update_session_metadata`` for fields the row does
not carry, ``drop_session_metadata_entry`` and ``replace_session_context_vars`` used to
touch only the file -- the session looked as old as its last save, and a sweep could
take a conversation that was worked on today.
"""
from __future__ import annotations

import json

import pytest

from agent_system.services import session_manager as session_manager_mod
from agent_system.services.session_archive import SessionArchive
from agent_system.services.session_manager import SessionManager
from tests.session.test_session_archive import USER, _age, _make_tree, _user_dir


def _row_time(sm: SessionManager, session_id: str) -> str:
    index = json.loads((_user_dir(sm) / "index.json").read_text(encoding="utf-8"))
    return index[session_id]["updated_at"]


def _archive(sm: SessionManager, tmp_path) -> SessionArchive:
    return SessionArchive(sm, archive_path=str(tmp_path / "session_archive"),
                          retention_days=30, first_sweep_delay_seconds=0)


@pytest.mark.asyncio
async def test_after_replacing_context_vars_the_archive_sees_a_young_session(tmp_path):
    storage = str(tmp_path / "sessions")
    await _make_tree(SessionManager(storage_path=storage), "root_v", [])
    # Months old -- then the person removes a variable, in a process that starts later.
    sm = SessionManager(storage_path=storage)
    stamp = _age(sm, ["root_v"], days=60)

    assert await sm.replace_session_context_vars(USER, "root_v", {"kept": "1"}) is True

    assert _row_time(sm, "root_v") > stamp
    report = await _archive(sm, tmp_path).archive_user(USER)
    assert report.trees == 0 and report.skipped_young == 1
    assert (_user_dir(sm) / "root_v.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["metadata", "drop"])
async def test_the_other_touching_writers_move_the_row(tmp_path, write):
    storage = str(tmp_path / "sessions")
    first = SessionManager(storage_path=storage)
    await _make_tree(first, "root_w", [])
    await first.update_session_metadata(USER, "root_w", {"sub_agents": {"k1": {"status": "running"}}})
    sm = SessionManager(storage_path=storage)
    stamp = _age(sm, ["root_w"], days=60)

    if write == "metadata":
        await sm.update_session_metadata(USER, "root_w", {"sub_agents": {"k1": {"status": "done"}}})
    else:
        assert await sm.drop_session_metadata_entry(USER, "root_w", "sub_agents", "k1") is True

    assert _row_time(sm, "root_w") > stamp


@pytest.mark.asyncio
async def test_an_untouched_write_leaves_the_row_alone(tmp_path):
    sm = SessionManager(storage_path=str(tmp_path / "sessions"))
    await _make_tree(sm, "root_x", [])
    sm = SessionManager(storage_path=str(tmp_path / "sessions"))
    stamp = _age(sm, ["root_x"], days=60)

    await sm.update_session_metadata(USER, "root_x", {"sub_agents": {}}, touch=False)

    assert _row_time(sm, "root_x") == stamp


@pytest.mark.asyncio
async def test_touches_in_a_row_rewrite_the_index_once_per_window(tmp_path, monkeypatch):
    """A parent whose sub-agents report one after another must not rewrite the main index each time."""
    sm = SessionManager(storage_path=str(tmp_path / "sessions"))
    await sm.create_session(user_id=USER, title="Busy", session_id="root_y")
    sm = SessionManager(storage_path=str(tmp_path / "sessions"))
    writes = []
    real = sm._index.update_entry

    async def counted(*args, **kwargs):
        writes.append(args[1])
        return await real(*args, **kwargs)

    monkeypatch.setattr(sm._index, "update_entry", counted)
    for n in range(5):
        await sm.update_session_metadata(USER, "root_y", {"sub_agents": {f"k{n}": {"status": "running"}}})
    assert writes == ["root_y"]

    monkeypatch.setattr(session_manager_mod, "INDEX_TOUCH_SECONDS", 0.0)
    await sm.update_session_metadata(USER, "root_y", {"sub_agents": {"k9": {"status": "running"}}})
    assert writes == ["root_y", "root_y"]
