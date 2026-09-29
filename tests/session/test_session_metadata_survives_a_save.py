"""A save does not undo what update_session_metadata merged while the saver held its copy.

A checkpoint save (SessionService.save_session) loads the session, puts the
run's messages in and writes it back; the sub-agent manager links each
sub-agent it spawns into the same session's metadata meanwhile, through
update_session_metadata. The save's copy was loaded before that link, and
written back whole it took the link out again -- a sub-agent the session had
started was gone from its metadata, and with it from every view that reads it.

Driven through the real SessionService and SessionManager on a temporary
folder; the link is put exactly between the save's load and its write.
"""
from types import SimpleNamespace

import pytest

from agent_system.llm.models import ChatMessage
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService


def _agent(messages):
    tracker = SimpleNamespace(get_session_messages=lambda session_id: messages,
                              emptied=lambda session_id: False,
                              get_session_template_vars=lambda session_id: None)
    return SimpleNamespace(_session_tracker=tracker, agent_config=None)


@pytest.mark.asyncio
async def test_a_sub_agent_linked_during_a_checkpoint_save_stays_linked(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    session = await manager.create_session("anonymous", title="coordinator", agent_name="coordinator")
    session_id = session["session_id"]
    # linked before the save loads its copy: the copy holds it, and is stale by one
    await manager.update_session_metadata("anonymous", session_id, {"sub_agents": {"sub_helper_0": {"status": "done"}}})
    load = manager.load_session

    async def load_then_link(user_id, sid, *args, **kwargs):
        loaded = await load(user_id, sid, *args, **kwargs)
        # the sub-agent manager, between the save's load and its write
        await manager.update_session_metadata(user_id, sid, {"sub_agents": {"sub_helper_1": {"status": "running"}}})
        return loaded
    manager.load_session = load_then_link

    messages = [ChatMessage(role="user", content="go"), ChatMessage(role="assistant", content="started a helper")]
    saved = await SessionService(manager).save_session(_agent(messages), "anonymous", session_id, "coordinator",
                                                       "default", was_new_session=False)

    assert saved, "fixture: the save did not go through"
    stored = await manager.load_session("anonymous", session_id, bypass_cache=True)
    assert stored["metadata"].get("sub_agents") == {"sub_helper_0": {"status": "done"},
                                                    "sub_helper_1": {"status": "running"}}
    # ... and the save itself landed: its messages, counted
    assert [m["content"] for m in stored["messages"]] == ["go", "started a helper"]
    assert stored["metadata"]["message_count"] == 2
    assert stored["metadata"]["last_agent_response"] == "started a helper"


@pytest.mark.asyncio
async def test_a_key_the_file_does_not_have_comes_from_the_caller(tmp_path):
    # The sub-agent manager's first link starts the mapping on its loaded copy.
    manager = SessionManager(storage_path=str(tmp_path))
    session = await manager.create_session("anonymous", title="coordinator", agent_name="coordinator")
    loaded = await manager.load_session("anonymous", session["session_id"])
    assert "sub_agents" not in loaded["metadata"], "fixture: the file must not have the key yet"
    loaded["metadata"]["sub_agents"] = {}

    await manager.save_session(loaded)

    stored = await manager.load_session("anonymous", session["session_id"], bypass_cache=True)
    assert stored["metadata"]["sub_agents"] == {}
