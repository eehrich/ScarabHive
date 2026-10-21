"""The chat's "Open in" menu for a session: the plugin panels with a session entry that hold data for it."""
from __future__ import annotations

import asyncio
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


def panel(instance: str, session: bool = True) -> dict:
    contexts = {"session": f"/plugins/{instance}/?session_id={{session_id}}"} if session else {}
    return {"panel": {"endpoint": f"/plugins/{instance}/", "title": instance.title(), "icon": "bug",
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

    def register(instance, plugin, session=True):
        monkeypatch.setitem(web_adapter.plugin_web_registry.web_plugins, instance, plugin)
        monkeypatch.setitem(tool_adapter.plugin_tool_registry.plugin_servers, instance,
                            SimpleNamespace(plugin_schema={"web_ui": panel(instance, session)}))
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
