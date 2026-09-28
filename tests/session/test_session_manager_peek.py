"""SessionManager.peek_session reads a record without counting it as seen.

A caller that only asks the record something before it claims the session (app._session_agent_name: which agent
it ran with) loaded it -- and a load stamps the manager's cache, so changed_on_disk then took what another process
had written for this manager's own, and the claim kept the stale copy in memory.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from agent_system.services.session_manager import (
    SessionManager,
    SessionNotFoundError,
    SessionPermissionError,
)


async def _saved(manager: SessionManager, user: str, messages: list[str]) -> None:
    session = await manager.create_session(user_id=user, session_id="s1", agent_name="coder", llm_profile="normal")
    session["messages"] = [{"role": "user", "content": text} for text in messages]
    await manager.save_session(session)


async def test_a_peek_leaves_a_change_another_process_made_a_change(tmp_path):
    ours, theirs = SessionManager(storage_path=str(tmp_path)), SessionManager(storage_path=str(tmp_path))
    await _saved(ours, "alice", ["first"])
    record = await theirs.load_session("alice", "s1")
    record["messages"].append({"role": "user", "content": "theirs"})
    await theirs.save_session(record)
    # Written after our own save and before the peek, whatever the file system's clock resolution
    written = ours._cache["s1"][1] + 0.01
    os.utime(tmp_path / "alice" / "s1.json", (written, written))
    while time.time() <= written + 0.01:
        await asyncio.sleep(0.005)

    peeked = await ours.peek_session("alice", "s1")

    assert [m["content"] for m in peeked["messages"]] == ["first", "theirs"]
    assert peeked["agent_name"] == "coder"
    assert ours.changed_on_disk("alice", "s1") is True, "the peek counted the other process's change as seen"


async def test_a_peek_answers_for_its_user_only(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    await _saved(manager, "alice", ["first"])
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "s1.json").write_bytes((tmp_path / "alice" / "s1.json").read_bytes())

    with pytest.raises(SessionPermissionError):
        await manager.peek_session("bob", "s1")
    with pytest.raises(SessionNotFoundError):
        await manager.peek_session("alice", "missing")
