"""SessionManager.subtree_ids: a session and every session below it, the tree the session list shows.

The usage tracker counts a session's sub-agents by it: a state machine's run session makes no LLM call of its
own, and only the sessions below it -- its agents' -- say which calls are the run's.
"""
from __future__ import annotations

import json

import pytest

from agent_system.services.session_manager import SessionManager


@pytest.fixture
def sm(tmp_path):
    return SessionManager(storage_path=str(tmp_path / "sessions"))


async def _session(sm: SessionManager, session_id: str, parent: str | None = None) -> None:
    created = await sm.create_session(user_id="u", session_id=session_id, agent_name="a",
                                      llm_profile="p", parent_session_id=parent)
    await sm.save_session(created)


async def test_every_session_below_comes_after_its_parent(sm):
    await _session(sm, "run")
    await _session(sm, "agent-a", "run")
    await _session(sm, "agent-b", "run")
    await _session(sm, "deep", "agent-b")
    await _session(sm, "elsewhere")

    tree = await sm.subtree_ids("u", "run")

    assert tree[0] == "run"
    assert sorted(tree) == ["agent-a", "agent-b", "deep", "run"]
    assert tree.index("agent-b") < tree.index("deep")
    assert await sm.subtree_ids("u", "deep") == ["deep"]


async def test_a_stale_sub_index_naming_a_session_above_does_not_loop(sm):
    await _session(sm, "run")
    await _session(sm, "agent", "run")
    # a sub-index left behind that names the root as the agent's child
    (sm.storage_path / "u" / ".subs.agent.index.json").write_text(json.dumps(
        {"run": {"session_id": "run", "parent_session": {"session_id": "agent"}}}), encoding="utf-8")

    assert sorted(await sm.subtree_ids("u", "run")) == ["agent", "run"]
