"""`/undo`, `/retry` and `/export` for the surfaces that are not a terminal.

The browser's conversation IS the record: it reloads a session from disk on
every message. So the cut has to reach the file -- and the agent's copy in
memory with it, or the next save writes the dropped turn straight back.

Drives the real build_app over ASGI against a temp session store, with the
real SessionManager and SessionService. Nothing about the cut is faked: it is
chat_actions.split_off_last_exchange, the same one agent-cli uses.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from agent_system.config.models import SessionPresenceConfig
from agent_system.core.session_presence import presence_for
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio

USER = "anonymous"  # auth is off in here


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
def api(tmp_path, monkeypatch):
    """The app on a temp store, never on the real data/sessions."""
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    app.state.config.session_presence = SessionPresenceConfig(enabled=True)
    manager = SessionManager(storage_path=str(tmp_path))
    service = SessionService(manager)
    monkeypatch.setattr(app_mod, "_session_service", service)
    return SimpleNamespace(app=app, manager=manager,
                           presence=presence_for(app.state.config))


def _turn(question, answer):
    return [{"role": "user", "content": question},
            {"role": "assistant", "content": answer}]


async def _stored(api, session_id="s1", messages=(), llm_profile="default"):
    session = await api.manager.create_session(
        user_id=USER, session_id=session_id, agent_name="chat_agent",
        llm_profile=llm_profile)
    session["messages"] = list(messages)
    await api.manager.save_session(session)


async def _messages_on_disk(api, session_id="s1"):
    record = await api.manager.load_session(USER, session_id)
    return (record or {}).get("messages") or []


def _busy(monkeypatch):
    """Every session is in another process's hands from here on.

    The same helper the presence tests next door use: a hold that nests
    inside this process cannot show what a FOREIGN one does.
    """
    from agent_system.core.session_presence import SessionBusy, SessionPresence

    def hold(self, session_id, user_id, agent_name):
        raise SessionBusy(session_id, "other_agent")

    monkeypatch.setattr(SessionPresence, "hold", hold)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


class TestUndo:
    async def test_the_last_exchange_leaves_the_record(self, api):
        await _stored(api, messages=_turn("erste frage", "erste antwort")
                      + _turn("schreib die routine", "hier ist sie"))

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 200, response.text
        assert response.json()["dropped"]["text"] == "schreib die routine"
        left = await _messages_on_disk(api)
        assert [m["content"] for m in left] == ["erste frage", "erste antwort"]

    async def test_the_copy_in_memory_is_cut_with_it(self, api):
        """Cut only on disk, the agent still holds the old conversation --
        and the very next save puts the dropped turn back."""
        await _stored(api, messages=_turn("erste frage", "erste antwort")
                      + _turn("schreib die routine", "hier ist sie"))

        async with _client(api.app) as client:
            await client.post("/chat/undo", json={"session_id": "s1"}, timeout=30.0)
            # Any later write of that session goes through the agent's copy.
            appended = await client.post("/sessions/s1/append",
                                         json={"content": "was anderes"}, timeout=30.0)

        assert appended.status_code == 200, appended.text
        left = await _messages_on_disk(api)
        assert [m["content"] for m in left] == [
            "erste frage", "erste antwort", "was anderes"], (
                "the dropped turn came back with the next save")

    async def test_a_session_with_nothing_to_take_back(self, api):
        await _stored(api, messages=[{"role": "assistant", "content": "hallo"}])

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 200, response.text
        assert response.json()["dropped"] is None
        assert len(await _messages_on_disk(api)) == 1, "it cut something anyway"

    async def test_a_session_another_process_runs_is_refused(self, api, monkeypatch):
        """Not edited underneath a running turn -- the same refusal an append
        to a busy session gets, and for the same reason: both would write the
        conversation and the last save would win."""
        await _stored(api, messages=_turn("frage", "antwort"))
        _busy(monkeypatch)

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 409, response.text
        assert len(await _messages_on_disk(api)) == 2, "it cut a running session"

    async def test_a_session_THIS_process_runs_is_refused_too(self, api):
        """Holds NEST inside a process, so the claim lets a run of this very
        process through -- and the run then writes its whole message list back
        when it finishes, putting the dropped exchange straight back while the
        browser shows it gone."""
        await _stored(api, messages=_turn("frage", "antwort"))
        api.presence.hold("s1", USER, "chat_agent")   # as /run holds it

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 409, response.text
        assert "running" in response.text
        assert len(await _messages_on_disk(api)) == 2

    async def test_a_session_a_run_of_this_agent_has_is_refused_by_its_lock(self, api):
        """Refused by session presence only, a run of this process that did not
        show there -- presence off, or a run that took the session after the
        check -- had the exchange cut beside it, and its save put it back."""
        await _stored(api, messages=_turn("frage", "antwort"))
        tracker = api.app.state.agent._session_tracker
        assert await tracker.acquire_session_lock("s1", "run_1")   # as a run holds it
        try:
            async with _client(api.app) as client:
                response = await client.post("/chat/undo", json={"session_id": "s1"},
                                             timeout=30.0)
        finally:
            await tracker.release_session_lock("s1", "run_1")

        assert response.status_code == 409, response.text
        assert "running" in response.text
        assert len(await _messages_on_disk(api)) == 2

    async def test_a_cut_that_is_not_saved_is_taken_back(self, api, monkeypatch):
        """Answered with the exchange gone while the record still had it -- and
        the cut left in memory for whatever saved the session next."""
        from agent_system import app as app_mod

        await _stored(api, messages=_turn("frage", "antwort"))

        async def fails(*args, **kwargs):
            return False

        monkeypatch.setattr(app_mod._session_service, "save_session", fails)
        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 500, response.text
        messages = api.app.state.agent._session_tracker.get_session_messages("s1")
        assert [m.content for m in messages] == ["frage", "antwort"], "the cut stayed in memory"

    async def test_what_another_process_wrote_meanwhile_is_read_before_the_cut(self, api):
        """The agent's name was read off the record by a LOAD, which counts the
        file as seen: the claim then took what another process had written for
        this process's own and cut the stale copy in memory -- its last
        exchange, the wrong one -- and saved that over the other's turn."""
        from agent_system.llm.models import ChatMessage

        await _stored(api, messages=_turn("frage", "antwort"))
        api.app.state.agent._session_tracker.set_session_messages(
            "s1", [ChatMessage(role="user", content="frage"),
                   ChatMessage(role="assistant", content="antwort")])
        import asyncio
        await asyncio.sleep(0.05)  # file times on Windows advance in ~16 ms steps
        woken = SessionManager(storage_path=str(api.manager.storage_path))
        record = await woken.load_session(USER, "s1")
        record["messages"] += _turn("noch eine frage", "die antwort des geweckten laufs")
        await woken.save_session(record)

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 200, response.text
        assert response.json()["dropped"]["text"] == "noch eine frage"
        assert [m["content"] for m in await _messages_on_disk(api)] == ["frage", "antwort"]

    async def test_the_agent_of_a_turn_settling_the_session_does_the_cutting(self, api, monkeypatch):
        """The record names the agent of the last SAVED run. An API turn on another
        agent whose run saved nothing puts its copy back over the session: cut in
        the record's agent's tracker, the exchange came back with it."""
        from test_app_session_presence import _another_agent

        from agent_system import app as app_mod

        coder = _another_agent("coder")
        registry = api.app.state.tool_registry
        get, names = registry.get, registry.list
        monkeypatch.setattr(registry, "get", lambda name: coder if name == "coder" else get(name))
        monkeypatch.setattr(registry, "list", lambda: [*names(), "coder"])
        await _stored(api, messages=_turn("frage", "antwort") + _turn("noch eine", "die zweite"))
        await app_mod._session_service.load_and_restore_session(coder, USER, "s1")
        seen = coder._session_tracker.watch_appends("s1")  # a turn of coder settles it
        try:
            async with _client(api.app) as client:
                response = await client.post("/chat/undo", json={"session_id": "s1"},
                                             timeout=30.0)
        finally:
            coder._session_tracker.unwatch_appends("s1", seen)

        assert response.status_code == 200, response.text
        assert [m.content for m in coder._session_tracker.get_session_messages("s1")] == [
            "frage", "antwort"], "not cut where the settling turn looks"

    async def test_the_agent_that_RAN_the_session_does_the_cutting(self, api):
        """Every agent carries its own SessionTracker. Cutting the one a
        selector happens to show leaves the exchange standing in the one that
        ran it -- and its next save writes it back."""
        await _stored(api, messages=_turn("frage", "antwort"))

        async with _client(api.app) as client:
            response = await client.post(
                "/chat/undo",
                json={"session_id": "s1", "agent_name": "irgendein_anderer"},
                timeout=30.0)

        assert response.status_code == 200, response.text
        assert response.json()["dropped"]["text"] == "frage"
        assert await _messages_on_disk(api) == [], (
            "the record was not shortened -- a foreign agent's tracker was cut")

    async def test_force_cuts_a_session_whose_lock_is_a_leftover(self, api, monkeypatch):
        """A crashed process leaves the lock behind, and then /undo would be
        refused forever. Same escape hatch /run has."""
        await _stored(api, messages=_turn("frage", "antwort"))
        _busy(monkeypatch)

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", params={"force": "true"},
                                         json={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 200, response.text
        assert await _messages_on_disk(api) == []

    async def test_without_a_session_id(self, api):
        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={}, timeout=30.0)

        assert response.status_code == 400

    async def test_an_attached_question_says_so(self, api):
        """The file lives on the viewer's disk; only they can attach it again,
        so /retry has to say the picture is not coming with it."""
        await _stored(api, messages=[
            {"role": "user", "content": [
                {"type": "text", "text": "was ist das?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]},
            {"role": "assistant", "content": "ein blitter"}])

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        dropped = response.json()["dropped"]
        assert dropped["had_attachments"] is True
        assert dropped["text"] == "was ist das? [image_url]"


class TestContext:
    """What fills the window -- the measurement and the estimate, apart."""

    async def test_the_split_names_what_is_filling_it(self, api):
        await _stored(api, messages=[
            {"role": "user", "content": "schreib die routine"},
            {"role": "assistant", "content": "gleich"},
            {"role": "tool", "content": "x" * 8000},
        ])

        async with _client(api.app) as client:
            response = await client.get("/chat/context",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 200, response.text
        parts = response.json()["estimated"]["parts"]
        assert parts["tool_results"]["count"] == 1
        assert parts["questions"]["count"] == 1
        # The long tool result is the biggest thing in there, and saying so is
        # the whole point -- a total alone cannot.
        biggest = max(parts, key=lambda name: parts[name]["tokens"])
        assert biggest == "tool_results", parts

    async def test_the_prompt_and_the_tools_are_in_it(self, api):
        """They sit in the window on every call, before a word is typed."""
        await _stored(api, messages=[{"role": "user", "content": "frage"}])

        async with _client(api.app) as client:
            response = await client.get("/chat/context",
                                        params={"session_id": "s1"}, timeout=30.0)

        parts = response.json()["estimated"]["parts"]
        assert "system_prompt" in parts and "tools" in parts
        assert parts["system_prompt"]["tokens"] > 0, (
            "the agent's own prompt was not counted")

    async def test_what_was_never_measured_is_not_invented(self, api):
        """A session no LLM call ever ran in has no provider count -- an
        estimate is worth showing, a made-up measurement is not.

        Its own id: the usage tracker is a real registered plugin with a real
        store, and "s1" has been used by enough runs to have a snapshot in it.
        """
        await _stored(api, session_id="ctx-never-called", messages=[
            {"role": "user", "content": "frage"}])

        async with _client(api.app) as client:
            response = await client.get(
                "/chat/context", params={"session_id": "ctx-never-called"},
                timeout=30.0)

        assert response.json()["last_call"] == {}
        assert response.json()["estimated"]["total"] > 0

    async def test_the_window_is_the_one_this_session_runs_on(self, api):
        """The model picked in the panel lives on the SESSION. Read off the
        agent instead, the line stated the window of a model this session has
        not used since -- next to a measurement counted against another one."""
        from agent_system.config.models import LLMProfile

        config = api.app.state.config
        wide = config.llm_system.models[next(iter(config.llm_system.models))].model_copy(deep=True)
        wide.context_window = 123456
        config.llm_system.models["zz_wide"] = wide
        config.llm_system.profiles["zz_wide"] = LLMProfile(model_ref="zz_wide")
        await _stored(api, messages=[{"role": "user", "content": "frage"}],
                      llm_profile="zz_wide")

        async with _client(api.app) as client:
            response = await client.get("/chat/context",
                                        params={"session_id": "s1"}, timeout=30.0)

        agent_window = getattr(getattr(api.app.state.agent, "llm", None), "context_window", 0)
        assert agent_window != 123456, "fixture: the agent's own window is the one we look for"
        assert response.json()["window"] == 123456, response.text

    async def test_the_agents_own_cap_still_applies_to_it(self, api):
        """llm_params are what the agent says about every model it runs on,
        including one it did not choose itself -- so they decide what the
        next call is really counted against."""
        from agent_system.config.models import LLMProfile

        config = api.app.state.config
        wide = config.llm_system.models[next(iter(config.llm_system.models))].model_copy(deep=True)
        wide.context_window = 123456
        config.llm_system.models["zz_wide2"] = wide
        config.llm_system.profiles["zz_wide2"] = LLMProfile(model_ref="zz_wide2")
        api.app.state.agent.agent_config.llm_params = {"*": {"context_window": 4242}}
        await _stored(api, messages=[{"role": "user", "content": "frage"}],
                      llm_profile="zz_wide2")

        async with _client(api.app) as client:
            response = await client.get("/chat/context",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.json()["window"] == 4242, response.text

    async def test_a_session_that_is_not_there(self, api):
        async with _client(api.app) as client:
            response = await client.get("/chat/context",
                                        params={"session_id": "gibtsnicht"},
                                        timeout=30.0)

        # No record, no messages -- but the agent's prompt and tools are real,
        # so this answers rather than 404s. What it must NOT do is invent a
        # conversation.
        assert response.status_code == 200, response.text
        assert response.json()["estimated"]["parts"]["questions"]["count"] == 0

    async def test_the_prompt_is_rendered_for_THIS_session(self, api, monkeypatch):
        """Without the session id the template vars are skipped, and the one
        line the command exists to show is short by the whole var payload."""
        from agent_system.servers.agent.server import Agent

        asked = []

        async def describe(self, session_id=None):
            asked.append(session_id)
            return "ein prompt", []

        monkeypatch.setattr(Agent, "describe_context_inputs", describe)
        await _stored(api, messages=[{"role": "user", "content": "frage"}])

        async with _client(api.app) as client:
            await client.get("/chat/context", params={"session_id": "s1"},
                             timeout=30.0)

        assert asked == ["s1"]

    async def test_a_session_with_no_record_gets_no_measurement(self, api):
        """The tracker is keyed by session id ALONE, and a session that is not
        on disk has no owner there -- so the ownership check passes for
        anybody. A guessed id must not answer with someone else's counts."""
        async with _client(api.app) as client:
            response = await client.get(
                "/chat/context", params={"session_id": "s1"}, timeout=30.0)

        # "s1" has snapshots in the real usage store from earlier runs; with
        # no record for it here, none of them may come back.
        assert response.status_code == 200, response.text
        assert response.json()["last_call"] == {}


class TestTranscript:
    async def test_it_writes_the_conversation(self, api):
        await _stored(api, messages=_turn("was macht der blitter?",
                                          "er kopiert speicher"))

        async with _client(api.app) as client:
            response = await client.get("/chat/transcript",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 200, response.text
        assert "was macht der blitter?" in response.text
        assert "er kopiert speicher" in response.text
        assert "chat_agent" in response.text and "s1" in response.text

    async def test_the_browser_saves_it_instead_of_painting_it(self, api):
        await _stored(api, messages=_turn("frage", "antwort"))

        async with _client(api.app) as client:
            response = await client.get("/chat/transcript",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers["content-disposition"]
        assert "chat-s1.md" in response.headers["content-disposition"]

    async def test_a_conversation_with_nothing_in_it_is_refused(self, api):
        """A file holding nothing but a heading, reported as written. Reachable
        right after /undo takes the only exchange out."""
        await _stored(api, messages=[])

        async with _client(api.app) as client:
            response = await client.get("/chat/transcript",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 409, response.text
        assert "no messages" in response.text

    async def test_a_session_that_is_not_there(self, api):
        async with _client(api.app) as client:
            response = await client.get("/chat/transcript",
                                        params={"session_id": "gibtsnicht"},
                                        timeout=30.0)

        assert response.status_code == 404


class TestLastAnswer:
    """`/copy` for the browser: the text of the last answer, read from the
    record the way the terminal's /copy reads its session (chat_actions
    .last_answer)."""

    async def test_a_turn_ending_on_tool_calls_gives_the_answer_before_them(self, api):
        await _stored(api, messages=_turn("frage", "die  antwort\nin zwei Zeilen") + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ergebnis"}])

        async with _client(api.app) as client:
            response = await client.get("/chat/last_answer",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 200, response.text
        assert response.json() == {"text": "die  antwort\nin zwei Zeilen"}

    async def test_a_session_a_run_works_on_is_refused(self, api):
        """Its record holds what a checkpoint wrote mid-run -- copied, that
        would be reported as the answer."""
        await _stored(api, messages=_turn("frage", "halbe antwort"))
        api.presence.hold("s1", USER, "chat_agent")   # as /run holds it

        async with _client(api.app) as client:
            response = await client.get("/chat/last_answer",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 409, response.text
        assert "running" in response.text

    async def test_no_answer_yet_is_empty(self, api):
        await _stored(api, messages=[{"role": "user", "content": "frage"}])

        async with _client(api.app) as client:
            response = await client.get("/chat/last_answer",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 200, response.text
        assert response.json() == {"text": ""}

    async def test_a_session_that_is_not_there(self, api):
        async with _client(api.app) as client:
            response = await client.get("/chat/last_answer",
                                        params={"session_id": "gibtsnicht"},
                                        timeout=30.0)

        assert response.status_code == 404


class TestWhoHoldsASession:
    """With session presence off, the process still knows its own runs: a
    session one of them works on is not cut, nor copied from."""

    @staticmethod
    def _presence_off_and_running(api, monkeypatch):
        from agent_system.services.background_job_manager import BackgroundJobManager

        api.app.state.config.session_presence = SessionPresenceConfig(enabled=False)

        async def active_sessions(self):
            return {"s1": {"user_id": USER}}

        monkeypatch.setattr(BackgroundJobManager, "active_sessions", active_sessions)

    async def test_last_answer_refuses_a_session_this_process_runs(self, api, monkeypatch):
        await _stored(api, messages=_turn("frage", "halbe antwort"))
        self._presence_off_and_running(api, monkeypatch)

        async with _client(api.app) as client:
            response = await client.get("/chat/last_answer",
                                        params={"session_id": "s1"}, timeout=30.0)

        assert response.status_code == 409, response.text
        # without presence no agent-cli chat can be seen, and the message does not claim one
        assert "a run of this server" in response.text

    async def test_undo_refuses_a_session_this_process_runs(self, api, monkeypatch):
        await _stored(api, messages=_turn("frage", "antwort"))
        self._presence_off_and_running(api, monkeypatch)

        async with _client(api.app) as client:
            response = await client.post("/chat/undo", json={"session_id": "s1"},
                                         timeout=30.0)

        assert response.status_code == 409, response.text
        assert len(await _messages_on_disk(api)) == 2, "it cut a running session"

    async def test_another_users_session_is_not_read(self, api):
        session = await api.manager.create_session(
            user_id="bob", session_id="s-bob", agent_name="chat_agent", llm_profile="default")
        session["messages"] = _turn("frage", "bobs antwort")
        await api.manager.save_session(session)

        async with _client(api.app) as client:
            response = await client.get("/chat/last_answer",
                                        params={"session_id": "s-bob"}, timeout=30.0)

        assert response.status_code in (403, 404), response.text
        assert "bobs antwort" not in response.text

    async def test_a_name_no_session_can_have_is_the_callers_mistake(self, api):
        async with _client(api.app) as client:
            for path in ("/chat/last_answer", "/chat/transcript"):
                response = await client.get(path, params={"session_id": "../x"}, timeout=30.0)
                assert response.status_code == 400, (path, response.text)
