"""Direct messages: kept in the pair's channel, handed over by the hook, sent
and listed through the core's session presence (core/session_presence/ has
the waking rules and their tests). The hook runs through the plugin's real
schema.yaml dispatch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent_system.config.models import AgentSystemConfig, SessionPresenceConfig
from agent_system.core import session_presence as sp
from agent_system.hooks import HookContext
from agent_system.llm.models import ChatMessage
from plugins.debate_forum.database import DebateForumDB
from plugins.debate_forum.hooks import DebateForumHooks
from plugins.debate_forum.server import DebateForumServer

PLUGIN_DIR = Path(__file__).resolve().parent.parent
USER = "cli_user"
CONFIG = AgentSystemConfig(session_presence=SessionPresenceConfig(enabled=True))


@pytest.fixture
def db(tmp_path):
    return DebateForumDB(tmp_path / "forum.db", wal_mode=False)


@pytest.fixture
def hooks(db):
    return DebateForumHooks(PLUGIN_DIR, db)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(root))
    return root


@pytest.fixture
def presence(sessions):
    return sp.presence_for(CONFIG)


def _server(db, config):
    srv = DebateForumServer.__new__(DebateForumServer)  # like the forum tests: no schema loading
    srv.name = "debate_forum"
    srv.db = db
    srv.min_message_length = 50
    srv.system_config = config
    return srv


@pytest.fixture
def server(db):
    return _server(db, CONFIG)


async def _step(hooks, session_id, request_id="req-1", tools_schema=None):
    """One LLM call of a session; returns what its request carries."""
    messages = [ChatMessage(role="user", content="the task")]
    await hooks.on_pre_llm_call(HookContext(
        hook_type="pre_llm_call", request_id=request_id, session_id=session_id,
        agent=MagicMock(), agent_name="agent_b", messages=messages, tools_schema=tools_schema))
    return messages


async def _request_end(hooks, session_id, request_id="req-1", persisted=True):
    """The end of a request. ``persisted`` is what the agent loop tells the
    hook: whether this request's conversation reached the session file."""
    await hooks.on_session_end(HookContext(
        hook_type="session_end", request_id=request_id, session_id=session_id,
        agent=MagicMock(), agent_name="agent_b", messages=[],
        metadata={"persisted": persisted}))


async def _send(server, to, text="how far is chapter 3?", user=USER):
    return await server.send_message({
        "_session_id": "sa", "_user_id": user, "_agent_name": "agent_a",
        "to": to, "message": text})


class TestDirectMessages:
    async def test_a_running_session_reads_the_message_on_its_next_step(self, hooks, server, presence):
        presence.hold("sb", USER, "agent_b")

        result = await _send(server, "sb")

        assert result["status"] == "delivered_next_step"
        request = await _step(hooks, "sb")
        assert "how far is chapter 3?" in request[-1].content
        assert 'from_session="sa"' in request[-1].content
        assert len(await _step(hooks, "sb")) == 1, "the message was handed over twice"
        await _request_end(hooks, "sb")
        assert len(await _step(hooks, "sb", request_id="req-2")) == 1, (
            "handed over again although the session has it")

    async def test_a_run_that_dies_hands_the_message_to_the_next_one(
            self, hooks, server, presence):
        """Delivered means "the session has it and saved it", which only the end
        of the request says. Marking it as it is read loses it in between."""
        presence.hold("sb", USER, "agent_b")
        await _send(server, "sb")

        await _step(hooks, "sb")  # this run dies here: no end, nothing saved

        assert "how far is chapter 3?" in (await _step(hooks, "sb", request_id="req-2"))[-1].content

    async def test_a_request_whose_save_never_happened_hands_the_message_on(
            self, hooks, server, presence):
        """The end of a request is not the end of the story: what the session
        was told lives in the answer it writes, and a save that failed or was
        cancelled takes that with it. Then the message is still undelivered."""
        presence.hold("sb", USER, "agent_b")
        await _send(server, "sb")
        await _step(hooks, "sb")

        await _request_end(hooks, "sb", persisted=False)

        assert "how far is chapter 3?" in (
            await _step(hooks, "sb", request_id="req-2"))[-1].content

    async def test_the_message_names_the_tool_that_answers_it(self, db, server, presence):
        # The tools carry the plugin instance's name, and a hint at a tool the
        # agent does not have is worse than none.
        hooks = DebateForumHooks(PLUGIN_DIR, db, tool_prefix="dm")
        presence.hold("sb", USER, "agent_b")
        await _send(server, "sb")

        assert "dm_send_message" in (await _step(hooks, "sb"))[-1].content

    async def test_an_agent_without_the_tool_gets_the_message_without_the_hint(self, db, server, presence):
        hooks = DebateForumHooks(PLUGIN_DIR, db, tool_prefix="dm")
        presence.hold("sb", USER, "agent_b")
        await _send(server, "sb")
        other = [{"type": "function", "function": {"name": "todo"}}]

        delivered = (await _step(hooks, "sb", tools_schema=other))[-1].content
        assert "how far is chapter 3?" in delivered and "send_message" not in delivered

        own = other + [{"type": "function", "function": {"name": "dm_send_message"}}]
        assert "dm_send_message" in (await _step(hooks, "sb", request_id="req-2", tools_schema=own))[-1].content

    async def test_a_call_from_no_session_says_so(self, server):
        result = await server.send_message({"_user_id": USER, "to": "sb", "message": "hi"})

        assert "session" in result["error"]

    async def test_a_sub_agents_session_is_not_started_for_a_message(
            self, server, sessions, monkeypatch):
        spawned = []
        monkeypatch.setattr(sp.presence, "spawn_wake", lambda session_id, user_id, depth:
                            spawned.append(session_id) or (os.getpid(), 0.0))
        (sessions / USER).mkdir(parents=True)
        (sessions / USER / "sb.json").write_text(
            json.dumps({"parent_session": {"session_id": "s0"}}), encoding="utf-8")

        result = await _send(server, "sb")

        assert (result["status"], spawned) == ("queued", [])

    async def test_a_session_of_another_user_cannot_be_reached(self, hooks, server, presence):
        presence.hold("sb", "someone_else", "agent_b")

        result = await _send(server, "sb")

        assert "error" in result
        assert len(await _step(hooks, "sb")) == 1, "stored anyway"

    async def test_list_sessions_shows_the_users_other_running_sessions(self, server, presence):
        presence.hold("sa", USER, "agent_a")
        presence.hold("sb", USER, "agent_b")
        presence.hold("sc", "someone_else", "agent_c")

        listed = await server.list_sessions({"_session_id": "sa", "_user_id": USER})

        assert listed["sessions"] == [{"session_id": "sb", "status": "running", "agent": "agent_b"}]

    async def test_an_idle_session_is_woken_for_the_message(self, server, sessions, monkeypatch):
        spawned = []
        monkeypatch.setattr(sp.presence, "spawn_wake", lambda session_id, user_id, depth:
                            spawned.append(session_id) or (os.getpid(), 0.0))
        (sessions / USER).mkdir(parents=True)
        (sessions / USER / "sb.json").write_text("{}", encoding="utf-8")

        result = await _send(server, "sb")

        assert result["status"] == "woke_session"
        assert spawned == ["sb"]

    async def test_the_conversation_shows_in_the_forum(self, db, server, presence):
        presence.hold("sb", USER, "agent_b")

        await _send(server, "sb")

        (channel,) = db.list_channels()
        assert db.get_group(channel["group_id"])["name"] == "Direct messages"
        assert [m["content"] for m in db.get_messages(channel["id"])] == ["how far is chapter 3?"]

    async def test_without_session_presence_the_tools_say_so(self, db):
        result = await _send(_server(db, AgentSystemConfig()), "sb")

        assert "session_presence" in result["error"]


def test_every_tool_in_the_schema_has_its_handler():
    schema = yaml.safe_load((PLUGIN_DIR / "schema.yaml").read_text(encoding="utf-8"))
    names = [tool["function"]["name"].replace("{{ name }}_", "") for tool in schema["tools"]]

    assert {"list_sessions", "send_message"} <= set(names)
    for name in names:
        assert callable(getattr(DebateForumServer, name, None)), name
