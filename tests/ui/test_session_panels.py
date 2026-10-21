"""The chat's "Open in" menu for a session: the plugin panels with a session entry that hold data for it."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth import session_access
from agent_system.config.models import AuthConfig, EndpointSecurityConfig, PluginSecurityConfig
from agent_system.plugins import tool_adapter, web_adapter
from agent_system.ui import routes
from agent_system.ui.routes import router


def panel(instance: str, session: bool = True, title: str = "") -> dict:
    contexts = {"session": f"/plugins/{instance}/?session_id={{session_id}}"} if session else {}
    return {"panel": {"endpoint": f"/plugins/{instance}/", "title": title or instance.title(), "icon": "bug",
                      "category": "debug", "contexts": contexts}, "endpoints": []}


class Holds:
    def __init__(self, sessions):
        self.sessions, self.asked = set(sessions), []

    def has_session_data(self, session_id, user_id):
        self.asked.append((session_id, user_id))
        return session_id in self.sessions


class HoldsAsync:
    async def has_session_data(self, session_id, user_id):
        return session_id == "s1"


class Breaks:
    def has_session_data(self, session_id, user_id):
        raise RuntimeError("store gone")


class Slow:
    def has_session_data(self, session_id, user_id):
        time.sleep(routes.SESSION_PANEL_TIMEOUT * 4)
        return False


class SlowAsync:
    async def has_session_data(self, session_id, user_id):
        await asyncio.sleep(routes.SESSION_PANEL_TIMEOUT * 4)
        return False


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(auth=AuthConfig(
        enabled=False, plugin_security=PluginSecurityConfig(), endpoint_security=EndpointSecurityConfig(rules=[])),
        plugins=None)

    def register(instance, plugin, session=True, title=""):
        monkeypatch.setitem(web_adapter.plugin_web_registry.web_plugins, instance, plugin)
        monkeypatch.setitem(tool_adapter.plugin_tool_registry.plugin_servers, instance,
                            SimpleNamespace(plugin_schema={"web_ui": panel(instance, session, title)}))
    app.register = register
    return app


def offered(app, session_id="s1"):
    response = TestClient(app).get("/api/ui/session-panels", params={"session_id": session_id})
    assert response.status_code == 200, response.text
    return set(response.json()["panels"])


def test_only_the_panels_holding_data_for_the_session_are_offered(app):
    used, unused = Holds({"s1"}), Holds({"s2"})
    app.register("used", used)
    app.register("unused", unused)
    app.register("used_async", HoldsAsync())
    app.register("no_session_entry", Holds({"s1"}), session=False)

    answer = TestClient(app).get("/api/ui/session-panels", params={"session_id": "s1"}).json()
    found, hidden = set(answer["panels"]), set(answer["hidden"])

    assert {"used", "used_async"} <= found and not {"unused", "no_session_entry"} & found, found
    assert hidden == {"unused"}, "the shell hides exactly these (a core panel or one without the entry is not here)"
    assert used.asked == [("s1", "anonymous")], "asked for the session, as the viewer"


def test_a_plugin_that_cannot_say_keeps_its_panel_offered(app):
    app.register("without_method", object())
    app.register("breaks", Breaks())
    app.register("slow", Slow())
    app.register("slow_async", SlowAsync())

    found = offered(app)

    assert {"without_method", "breaks", "slow", "slow_async"} <= found, found


def test_the_slow_ones_are_waited_for_side_by_side_and_only_so_long():
    async def ask_both():
        started = time.monotonic()
        answers = await asyncio.gather(routes._holds("slow", Slow(), "s1", "u"),
                                       routes._holds("slow_async", SlowAsync(), "s1", "u"))
        return answers, time.monotonic() - started  # measured before the worker thread is waited for at the end

    answers, took = asyncio.run(ask_both())

    assert answers == [True, True]
    assert took < routes.SESSION_PANEL_TIMEOUT * 1.8, f"took {took:.2f}s: waited past the limit or in turn"


def test_the_route_asks_the_plugins_side_by_side(app):
    for name in ("slow_a", "slow_b", "slow_c"):
        app.register(name, SlowAsync())

    started = time.monotonic()
    found = offered(app)

    assert {"slow_a", "slow_b", "slow_c"} <= found, found
    assert time.monotonic() - started < routes.SESSION_PANEL_TIMEOUT * 2.5, "asked one after the other"


def test_a_session_the_viewer_may_not_see_is_not_answered(app, monkeypatch):
    app.register("used", Holds({"s1"}))

    async def refuse(request, user, session_id):
        return False
    monkeypatch.setattr(session_access, "may_see_session", refuse)

    response = TestClient(app).get("/api/ui/session-panels", params={"session_id": "s1"})

    assert response.status_code == 404, response.text


def test_a_signed_in_viewer_is_asked_for_by_name_and_sees_only_the_panels_of_their_role(app, monkeypatch):
    mine, admins = Holds({"s1"}), Holds({"s1"})
    app.register("mine", mine)
    app.register("admins_only", admins)
    real = routes.plugin_panels
    monkeypatch.setattr(routes, "plugin_panels", lambda auth: [
        replace(p, roles=["admin"]) if p.id == "admins_only" else p for p in real(auth)])

    async def ada(request):
        return SimpleNamespace(username="ada", role=SimpleNamespace(value="user"))

    async def admit(request, user, session_id):
        return True
    monkeypatch.setattr(routes, "viewer_user", ada)
    monkeypatch.setattr(session_access, "may_see_session", admit)

    answer = TestClient(app).get("/api/ui/session-panels", params={"session_id": "s1"}).json()

    assert "mine" in answer["panels"], answer
    assert "admins_only" not in answer["panels"] + answer["hidden"], "a panel the viewer's catalogue lacks is not named"
    assert mine.asked == [("s1", "ada")] and admins.asked == [], "asked as the viewer, the hidden one not at all"


class Hangs:
    """A store that hangs until let go: the wait gives up, the worker thread does not."""
    def __init__(self):
        self.calls, self.go = 0, threading.Event()

    def has_session_data(self, session_id, user_id):
        self.calls += 1
        self.go.wait(5)
        return False


def test_a_hung_answer_holds_one_worker_and_frees_it_once_through():
    hangs = Hangs()

    async def ask_while_it_hangs_and_after():
        first = await routes._holds("hangs", hangs, "s1", "u")
        second = await routes._holds("hangs", hangs, "s2", "u")  # another question: the store is the one hung
        calls_while_hung = hangs.calls
        hangs.go.set()
        for _ in range(100):  # the thread finishes, then its callback runs on the loop
            if "hangs" not in routes._OVERDUE:
                break
            await asyncio.sleep(0.02)
        third = await routes._holds("hangs", hangs, "s1", "u")
        return first, second, calls_while_hung, third

    first, second, calls_while_hung, third = asyncio.run(ask_while_it_hangs_and_after())

    assert (first, second) == (True, True), "a panel that cannot say in time is offered"
    assert calls_while_hung == 1, "asked again while its thread still ran: a second worker taken"
    assert third is False and hangs.calls == 2, "the instance stayed locked after its thread was through"


def test_an_async_callable_that_is_no_coroutine_function_is_awaited():
    class AsyncCall:
        async def __call__(self, session_id, user_id):
            return False

    assert asyncio.run(routes._holds("async_call", SimpleNamespace(has_session_data=AsyncCall()), "s1", "u")) is False


def test_when_the_instance_that_answers_is_beyond_the_role_its_visible_siblings_stand_in(app, monkeypatch):
    """Instances of one plugin may show one list, answered for by one of them (the one that spawned): restricted to
    admins, that one is not the viewer's -- the sibling the viewer sees is offered, not hidden with it."""
    class Shares(Holds):
        session_data_shared = True

    app.register("manager_a", Shares({"s1", "s2"}), title="Managers")
    app.register("manager_b", Shares(set()), title="Managers")
    app.register("manager_c", Shares({"s2"}), title="Managers")
    app.register("another_plugin", Holds(set()), title="Managers")  # same title, its own data: never a stand-in
    app.register("todo_a", Holds({"s1"}), title="Todos")  # its own data: the sibling has none to stand in with
    app.register("todo_b", Holds(set()), title="Todos")
    real = routes.plugin_panels
    monkeypatch.setattr(routes, "plugin_panels", lambda auth: [
        replace(p, roles=["admin"]) if p.id in ("manager_a", "todo_a") else p for p in real(auth)])

    async def ada(request):
        return SimpleNamespace(username="ada", role=SimpleNamespace(value="user"))

    async def admit(request, user, session_id):
        return True
    monkeypatch.setattr(routes, "viewer_user", ada)
    monkeypatch.setattr(session_access, "may_see_session", admit)

    answer = TestClient(app).get("/api/ui/session-panels", params={"session_id": "s1"}).json()

    assert answer["panels"] == ["manager_b"], f"one stand-in, not one per sibling: {answer}"
    assert sorted(answer["hidden"]) == ["another_plugin", "manager_c", "todo_b"], answer
    answer = TestClient(app).get("/api/ui/session-panels", params={"session_id": "s2"}).json()
    assert answer["panels"] == ["manager_c"], f"a visible one answers itself: no stand-in beside it: {answer}"


def test_answers_asked_at_once_are_each_answered_and_the_same_question_shares_its_thread():
    """Two viewers' menus at the same moment: a quick store answers both -- neither is taken for a hung one."""
    class Quick:
        def __init__(self):
            self.asked = []

        def has_session_data(self, session_id, user_id):
            self.asked.append((session_id, user_id))
            time.sleep(0.05)
            return session_id == "s1"

    quick = Quick()

    async def ask_at_once():
        return await asyncio.gather(routes._holds("quick", quick, "s1", "ada"),
                                    routes._holds("quick", quick, "s2", "bob"),
                                    routes._holds("quick", quick, "s2", "bob"))

    assert asyncio.run(ask_at_once()) == [True, False, False]
    assert sorted(quick.asked) == [("s1", "ada"), ("s2", "bob")], "the same question asked twice, or one not asked"


def test_an_awaitable_answer_shared_by_the_same_question_asked_twice_is_awaited_once():
    class AsyncCall:
        async def __call__(self, session_id, user_id):
            await asyncio.sleep(0.05)
            return False

    plugin = SimpleNamespace(has_session_data=AsyncCall())

    async def ask_twice():
        return await asyncio.gather(routes._holds("twice", plugin, "s1", "u"), routes._holds("twice", plugin, "s1", "u"))

    assert asyncio.run(ask_twice()) == [False, False]


def test_another_question_through_does_not_free_an_instance_still_hung():
    """One question hangs in the store, another of the same instance ends later: the hung one still holds its
    worker, so a third question gets no thread of its own."""
    class Partly:
        def __init__(self):
            self.asked, self.go = [], threading.Event()

        def has_session_data(self, session_id, user_id):
            self.asked.append(session_id)
            if session_id == "s1":
                self.go.wait(5)
            else:
                time.sleep(routes.SESSION_PANEL_TIMEOUT * 1.4)  # through only after the wait gave up on it
            return False

    partly = Partly()

    async def ask():
        await asyncio.gather(routes._holds("partly", partly, "s1", "u"), routes._holds("partly", partly, "s2", "u"))
        await asyncio.sleep(routes.SESSION_PANEL_TIMEOUT)  # s2's thread is through meanwhile
        third = await routes._holds("partly", partly, "s3", "u")
        asked = sorted(partly.asked)
        partly.go.set()
        return third, asked

    third, asked = asyncio.run(ask())

    assert third is True and asked == ["s1", "s2"], f"a thread for s3 beside the hung one: {asked}"
