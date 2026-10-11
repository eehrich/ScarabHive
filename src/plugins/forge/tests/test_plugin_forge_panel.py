"""The Forge panel in a real browser, against the real plugin: its router, its static files, its webhook store.

Seeded: repository ``app`` (GitLab ``team/app`` on host ``gl``, merge allowed) and ``docs``, left out (its host is
not configured); allowed users ``admin`` and ``alice``; the webhook runs new work as ``admin``. Bound: ``app#12`` to
``admin``'s session ``s-own`` with one message waiting, ``app!14`` to ``alice``'s ``s-alice``. Outcomes: the
assignment of #12 by @carol started ``s-own``; a payload of @erin could not be read; a comment of @dave on #30
went nowhere. The viewer is ``admin``. POST /__stub/store-broken makes the store unreadable, /__stub/hook-off takes
the webhook away.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent_system.config import AgentSystemConfig, ToolServerConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from plugins.forge.events import Event, Store
from plugins.forge.server import ForgeServer
from plugins.forge.web_endpoints import ForgeWebEndpoints
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent


def seeded_server(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> ForgeServer:
    monkeypatch.setenv("FORGE_PANEL_TOKEN", "tok")
    monkeypatch.setenv("FORGE_PANEL_SECRET", "secret")
    cfg = ToolServerConfig()
    cfg.hosts = {"gl": {"provider": "gitlab", "api_url": "https://gl.test/api/v4", "token_env": "FORGE_PANEL_TOKEN",
                        "webhook_secret_env": "FORGE_PANEL_SECRET"}}
    cfg.repos = {"app": {"host": "gl", "project": "team/app", "path": str(tmp / "clone"), "allow_merge": True},
                 "docs": {"host": "nowhere", "project": "team/docs"}}
    cfg.allowed_users = ["admin", "alice"]
    cfg.webhook = {"user": "admin"}
    server = ForgeServer("forge", AgentSystemConfig(), cfg)
    store = server.events = server.hooks_plugin.store = Store(tmp / "events.db")     # never the real data/
    store.bind("app", "issue", "12", "admin", "s-own")
    store.bind("app", "mr", "14", "alice", "s-alice")
    store.put("s-own", "admin", "news")
    store.record(Event("app", "assigned", "issue", "12", "carol", "#12", "", True), "new session, woke_session",
                 "admin", "s-own")
    store.record(Event("app", "note", "", "", "erin", "", "", False), "error: not read: RuntimeError: 401")
    store.record(Event("app", "comment", "issue", "30", "dave", "#30", "note 1", False),
                 "dropped: no session works on it")
    return server


def panel_app(server: ForgeServer) -> FastAPI:
    app = FastAPI()

    @app.post("/__stub/store-broken")
    async def store_broken():
        def broken(*_args, **_kwargs):
            raise OSError("disk gone")

        server.events.overview = broken
        return {}

    @app.post("/__stub/hook-off")
    async def hook_off():
        server.webhook = None
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("forge", server)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/forge", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        async def viewer(self, request):
            return "admin"

        monkeypatch.setattr(ForgeWebEndpoints, "_viewer", viewer)
        server = seeded_server(tmp_path_factory.mktemp("forge_panel"), monkeypatch)
        return run_app_test_page(BROWSER, panel_app(server), "tests/forge/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the repositories are listed, and one left out says why',
    'the policy names who may use forge and what the webhook may do',
    'the sessions at work show what waits for them, and only the viewer\'s own opens in the chat',
    'a webhook event that went nowhere is marked, one that started a session names it',
    'an event that failed is marked as an error',
    'an unreadable store is said, never shown as nothing happened',
    'with the webhook off the panel says so and shows nothing of before',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_forge_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
