"""`/undo files`, `/rewind` and `/rewind <n>` for the browser: /chat/undo with files,
/chat/checkpoints and /chat/rewind.

Drives the real build_app over ASGI on a temp session store. The app
bootstraps the shipped configuration, file_checkpoints included -- its record
lands beside the temp sessions -- and a real agent run writes files through a
real file_ops server on tmp_path (file_rewind_rig). Pinned:

* /undo files puts the exchange's files back and then drops it, in the record
  and in memory; refused (files changed outside the agent), it leaves both;
* /rewind <n> puts files back and leaves the conversation as it is;
* the endpoints refuse a running session, a number that names no checkpoint,
  and another user's session -- and without the plugin they say so (503);
* a plain /undo is unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from agent_system import app_state
from agent_system.config.models import SessionPresenceConfig
from agent_system.core.session_presence import presence_for
from agent_system.file_rewind import file_rewinder
from agent_system.hooks import load_hooks_config
from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from file_rewind_rig import BEFORE, Rig, create, replace, tree

pytestmark = pytest.mark.anyio

USER = "anonymous"  # auth is off in here
AGENT = "rewind_coder"
SID = "rw1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _disable_auth(monkeypatch):
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


@pytest.fixture
async def api(tmp_path, monkeypatch):
    from agent_system import app as app_mod
    from agent_system.hooks.registry import get_hook_registry

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    # The plugins the shipped configuration builds open their stores relative to
    # the working directory (data/...): from here they land in tmp_path, not in
    # the checkout.
    monkeypatch.chdir(tmp_path)
    app = app_mod.build_app()
    app.state.config.session_presence = SessionPresenceConfig(enabled=True)
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    service = SessionService(manager)
    monkeypatch.setattr(app_state, "session_service", service)
    # The app's own plugin, built from the shipped configuration -- not the rig's.
    plugin = file_rewinder()
    assert plugin is not None, "the app loaded no file_checkpoints"
    assert plugin.root == tmp_path / "file_checkpoints", plugin.root
    # Its hooks as the app's startup registers them (ToolServerIntegration._register_plugin_hooks);
    # no lifespan runs over ASGITransport.
    names = await register_plugin_hooks("file_checkpoints", plugin, plugin.get_schema_data(),
                                        get_hook_registry(), load_hooks_config(app.state.config))
    assert get_hook_registry().get_hook_info(BEFORE) and len(names) == 2
    rig = Rig(tmp_path)
    rig.plugin = plugin
    agent = rig.agent(AGENT)
    agent._session_service = service
    app.state.tool_registry.register(AGENT, agent)
    yield SimpleNamespace(app=app, manager=manager, rig=rig, agent=agent,
                          presence=presence_for(app.state.config))
    await rig.fs.search_engine.stop()


async def _two_turns(api):
    work = api.rig.work
    (work / "a.txt").write_bytes(b"original\r\n")
    await api.rig.turn("first", [replace(work / "a.txt", "original", "first")],
                       agent=api.agent, session_id=SID, user=USER)
    await api.rig.turn("second", [create(work / "b.txt", "second"),
                                  replace(work / "a.txt", "first", "second")],
                       agent=api.agent, session_id=SID, user=USER)


async def _questions_on_disk(api):
    record = await api.manager.load_session(USER, SID)
    return [m["content"] for m in record["messages"] if m["role"] == "user"]


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestUndoFiles:

    async def test_the_exchange_and_its_files_go_together(self, api):
        await _two_turns(api)

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": SID, "files": True}, timeout=30)

        assert response.status_code == 200, response.text
        assert response.json()["dropped"]["text"] == "second"
        assert response.json()["files"]["status"] == "rewound"
        assert tree(api.rig.work) == {"a.txt": b"first\r\n"}
        assert await _questions_on_disk(api) == ["first"]
        assert [m.content for m in api.agent._session_tracker.get_session_messages(SID)
                if m.role == "user"] == ["first"], "the copy in memory kept the exchange"

    async def test_refused_the_exchange_and_the_files_stay(self, api):
        await _two_turns(api)
        (api.rig.work / "b.txt").write_text("the person's")

        async with _client(api.app) as client:
            refused = await client.post("/chat/undo", json={"session_id": SID, "files": True}, timeout=30)
            forced = await client.post("/chat/undo", json={"session_id": SID, "files": True,
                                                           "overwrite": True}, timeout=30)

        assert refused.status_code == 409, refused.text
        assert "b.txt" in refused.json()["detail"]
        assert forced.status_code == 200, forced.text
        assert tree(api.rig.work) == {"a.txt": b"first\r\n"}
        assert await _questions_on_disk(api) == ["first"]

    async def test_a_refusal_keeps_the_record_as_it_was(self, api):
        await _two_turns(api)
        (api.rig.work / "a.txt").write_text("the person's")

        async with _client(api.app) as client:
            refused = await client.post("/chat/undo", json={"session_id": SID, "files": True}, timeout=30)

        assert refused.status_code == 409, refused.text
        assert await _questions_on_disk(api) == ["first", "second"]
        assert (api.rig.work / "b.txt").read_text() == "second"

    async def test_a_plain_undo_leaves_the_files(self, api):
        await _two_turns(api)

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": SID}, timeout=30)

        assert response.status_code == 200, response.text
        assert response.json()["files"] is None
        assert (api.rig.work / "b.txt").read_text() == "second"


class TestDeletingTheSession:

    async def test_the_persons_delete_takes_the_checkpoints_along(self, api):
        await _two_turns(api)
        from agent_system.api.session_endpoints import session_router
        from agent_system.auth.dependencies import get_optional_user

        record = api.rig.plugin.root / USER / SID
        assert (record / "journal.db").exists(), "fixture: nothing recorded"
        # The router the app mounts with authentication on (app.py), and nobody signed in -- anonymous's session.
        api.app.include_router(session_router)
        api.app.dependency_overrides[get_optional_user] = lambda: None
        api.app.state.session_manager = api.manager

        async with _client(api.app) as client:
            response = await client.delete(f"/api/sessions/{SID}", timeout=30)

        assert response.status_code == 200, response.text
        assert not record.exists(), "the deleted session's checkpoints stayed"
        assert (api.rig.work / "b.txt").read_text() == "second", "deleting the record touched the files"


class TestRewind:

    async def test_a_checkpoint_puts_files_back_and_keeps_the_conversation(self, api):
        await _two_turns(api)

        async with _client(api.app) as client:
            listing = await client.get("/chat/checkpoints", params={"session_id": SID}, timeout=30)
            rewound = await client.post("/chat/rewind", json={"session_id": SID, "checkpoint": 1}, timeout=30)

        assert listing.status_code == 200, listing.text
        # Read off the record on disk, placed by keys recorded off the agent's messages in memory.
        assert [(c["turn"], c["dropped"], c["question"]) for c in listing.json()["checkpoints"]] == [
            (1, False, "first"), (2, False, "second")]
        assert "/rewind <n>" in listing.json()["text"]
        assert rewound.status_code == 200, rewound.text
        assert rewound.json()["status"] == "rewound"
        assert tree(api.rig.work) == {"a.txt": b"original\r\n"}
        assert await _questions_on_disk(api) == ["first", "second"]

    async def test_what_the_request_must_name(self, api):
        await _two_turns(api)

        async with _client(api.app) as client:
            missing = await client.post("/chat/rewind", json={"session_id": SID}, timeout=30)
            unknown = await client.post("/chat/rewind", json={"session_id": SID, "checkpoint": 9}, timeout=30)

        assert missing.status_code == 400
        assert unknown.status_code == 404, unknown.text
        assert (api.rig.work / "b.txt").read_text() == "second"

    async def test_a_running_session_is_refused(self, api, monkeypatch):
        from agent_system.core.session_presence import SessionBusy, SessionPresence

        await _two_turns(api)

        def hold(self, session_id, user_id, agent_name):
            raise SessionBusy(session_id, "other_agent")

        # Another process runs it: the presence the app's agents have, here for the rig's agent too.
        api.agent.system_config.session_presence = SessionPresenceConfig(enabled=True)
        monkeypatch.setattr(SessionPresence, "hold", hold)
        async with _client(api.app) as client:
            response = await client.post("/chat/rewind", json={"session_id": SID, "checkpoint": 1}, timeout=30)

        assert response.status_code == 409, response.text
        assert (api.rig.work / "b.txt").read_text() == "second"

    async def test_another_users_session_is_refused(self, api, monkeypatch):
        """With a person signed in, the owner is checked against the tracker of
        the agent that holds the session -- the session here is anonymous's."""
        from agent_system.auth.enforcement import EndpointSecurityEnforcer

        await _two_turns(api)

        async def signed_in_as_bob(self, request, get_user):
            return SimpleNamespace(username="bob")

        monkeypatch.setattr(EndpointSecurityEnforcer, "enforce_endpoint_security", signed_in_as_bob)
        async with _client(api.app) as client:
            listing = await client.get("/chat/checkpoints", params={"session_id": SID}, timeout=30)
            rewind = await client.post("/chat/rewind", json={"session_id": SID, "checkpoint": 1}, timeout=30)
            undo = await client.post("/chat/undo", json={"session_id": SID, "files": True}, timeout=30)

        assert (listing.status_code, rewind.status_code, undo.status_code) == (403, 403, 403)
        assert (api.rig.work / "b.txt").read_text() == "second"
        assert await _questions_on_disk(api) == ["first", "second"]

    async def test_without_the_plugin_the_file_commands_say_so(self, api, monkeypatch):
        from agent_system import file_rewind

        await _two_turns(api)
        monkeypatch.setattr(file_rewind, "_rewinder", None)

        async with _client(api.app) as client:
            rewind = await client.post("/chat/rewind", json={"session_id": SID, "checkpoint": 1}, timeout=30)
            undo = await client.post("/chat/undo", json={"session_id": SID, "files": True}, timeout=30)
            plain = await client.post("/chat/undo", json={"session_id": SID}, timeout=30)

        assert (rewind.status_code, undo.status_code) == (503, 503)
        assert "file_checkpoints" in rewind.json()["detail"]
        assert plain.status_code == 200, plain.text
        assert await _questions_on_disk(api) == ["first"], "the 503 undo took the exchange anyway"
