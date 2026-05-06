"""Tests for the descendants_context_vars helper used by GET /api/sessions/{id}."""

import pytest
from agent_system.api.session_endpoints import _build_descendants_context_vars
from agent_system.services.session_manager import SessionManager


@pytest.fixture
def sm(tmp_path):
    storage = tmp_path / "sessions"
    storage.mkdir()
    return SessionManager(storage_path=str(storage))


async def _create(sm, sid, agent_name, parent_id=None, context_vars=None):
    await sm.create_session(
        user_id="user1",
        session_id=sid,
        title=f"{sid}",
        agent_name=agent_name,
        llm_profile="normal",
    )
    data = await sm.load_session("user1", sid)
    if parent_id:
        data["parent_session"] = {"session_id": parent_id, "created_at": "2026-01-01T00:00:00Z"}
    if context_vars is not None:
        data["context_vars"] = context_vars
    await sm.save_session(data)


@pytest.mark.asyncio
async def test_descendants_empty_when_no_children(sm):
    await _create(sm, "root", "linear_book")
    tree = await _build_descendants_context_vars(sm, mcp_registry=None, user_id="user1", root_session_id="root")
    assert tree == []


@pytest.mark.asyncio
async def test_descendants_two_levels(sm):
    """Hierarchical walk: root → child → grandchild, with vars at each level."""
    await _create(sm, "root", "linear_book", context_vars={"book_id": "1"})
    await _create(sm, "child_a", "v5b_story_designer", parent_id="root",
                  context_vars={"phase": "synopsis", "debate_channel_id": "ch1"})
    await _create(sm, "child_b", "v4_request_analyzer", parent_id="root", context_vars={})
    await _create(sm, "grand_a", "v5b_synopsis_writer", parent_id="child_a",
                  context_vars={"phase": "synopsis", "Stil_Autor": "Hesse"})

    tree = await _build_descendants_context_vars(sm, mcp_registry=None, user_id="user1", root_session_id="root")

    # Find child_a node
    by_sid = {n["session_id"]: n for n in tree}
    assert "child_a" in by_sid
    assert "child_b" in by_sid
    assert by_sid["child_a"]["context_vars"] == {"phase": "synopsis", "debate_channel_id": "ch1"}
    assert by_sid["child_b"]["context_vars"] == {}

    grand = by_sid["child_a"]["children"]
    assert len(grand) == 1
    assert grand[0]["session_id"] == "grand_a"
    assert grand[0]["context_vars"]["Stil_Autor"] == "Hesse"


@pytest.mark.asyncio
async def test_descendants_skip_unrelated_sessions(sm):
    """Sessions whose parent_session is None or points elsewhere must not leak in."""
    await _create(sm, "root", "linear_book")
    await _create(sm, "other_root", "chat_agent")  # top-level, no parent
    await _create(sm, "child_x", "v5b_story_designer", parent_id="other_root",
                  context_vars={"x": "y"})

    tree = await _build_descendants_context_vars(sm, mcp_registry=None, user_id="user1", root_session_id="root")
    assert tree == []
