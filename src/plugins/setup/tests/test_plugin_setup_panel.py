"""The Setup panel in a real browser: the real page, script and router; the data from a stub shaped by the real
installation_state -- a field renamed on the server fails here, not only on the skeletons of a live panel."""
from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent_system.api.debug_endpoints import require_admin_viewer
from agent_system.config.models import ToolServerConfig
from agent_system.config.settings import load_settings
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
TESTS = Path(__file__).parent
PAGE_TIMEOUT = 120

# the page gets PAGE_TIMEOUT, and a page out of time fails these tests; pytest.ini's 120 s would end the whole run
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]


def panel_app(monkeypatch, users_folder: Path) -> FastAPI:
    from plugins.setup import server as module

    loaded = load_settings()
    # a user database of nobody's: the shape is what counts, not the accounts of this machine
    absent = str(users_folder / "absent" / "users.db")
    config = loaded.model_copy(update={"auth": loaded.auth.model_copy(update={"database_path": absent})})
    real = module.installation_state(config)  # the shape the panel gets in production
    stub: dict[str, Any] = {"state": copy.deepcopy(real), "probe": {"ok": True}, "probe_delay": 0.0, "probe_fails": False,
                            "me": "admin", "me_fails": False, "me_delays": [], "patched": [], "state_fails": False,
                            "state_delays": [], "calls": {"state": 0, "me": 0, "late": 0}}

    def state(config, signing_key=None):
        stub["calls"]["state"] += 1
        if stub["state_fails"]:
            raise RuntimeError("config.yaml does not parse")
        answer = copy.deepcopy(stub["state"])
        if stub["state_delays"]:
            time.sleep(stub["state_delays"].pop(0))  # answers with the state of the moment it was asked
            stub["calls"]["late"] += 1
        return answer

    async def probe(config, llm_config=None):
        await asyncio.sleep(stub["probe_delay"])
        if stub["probe_fails"]:
            raise RuntimeError("the probe itself broke")
        return copy.deepcopy(stub["probe"])

    def write_key(cfg_path, name, value):
        # the real one writes beside the config the server was built with: this machine's config/local.env
        stub["written"].append({"name": name, "value": value})
        if stub["key_refuse"]:
            raise HTTPException(status_code=400, detail=f"{name} not written: the value holds a line break")
        for key in stub["state"]["keys"]:  # the row's state changes: the table is drawn anew
            if key["name"] == name:
                key["state"] = "set"

    def reload_app_config(app):
        if stub["reload_error"]:
            raise module.ConfigReloadFailed(stub["reload_error"])
        return {}

    def ensure_signing_key(cfg_path, known=()):
        stub["signing_keys_made"] += 1
        stub["state"]["auth"]["configured_signing_key_known"] = False
        return "a new signing key is in local.env"

    stub.update(written=[], key_refuse=False, signing_keys_made=0, reload_error=None)
    monkeypatch.setattr(module, "installation_state", state)
    monkeypatch.setattr(module, "probe_chat", probe)
    monkeypatch.setattr(module, "write_key", write_key)
    monkeypatch.setattr(module, "ensure_signing_key", ensure_signing_key)
    monkeypatch.setattr(module, "reload_app_config", reload_app_config)

    app = FastAPI()
    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("setup", module.SetupServer("setup", config,
                                                             ToolServerConfig(type="setup", enabled=True)))
    registry.apply_to_app(app)
    app.dependency_overrides[require_admin_viewer] = lambda: None
    app.state.config = config

    @app.get("/auth/me")
    async def me():
        stub["calls"]["me"] += 1
        if stub["me_delays"]:
            await asyncio.sleep(stub["me_delays"].pop(0))
            stub["calls"]["late"] += 1
        if stub["me_fails"]:
            raise HTTPException(status_code=503, detail="for a moment")
        return {"username": stub["me"]}

    @app.get("/__stub/calls")
    async def calls():
        return stub["calls"]

    @app.patch("/auth/me")
    async def change(request: Request):
        body = await request.json()
        stub["patched"].append(body)
        if body.get("current_password") != "admin123":
            raise HTTPException(status_code=403, detail="Current password is incorrect")
        stub["state"]["auth"]["default_admin_password"] = False
        return {"username": stub["me"]}

    @app.post("/__stub/set")
    async def set_stub(request: Request):
        data = await request.json()
        for item in data.get("keys", []):
            assert set(item) == set(real["keys"][0]), f"stub key fields {set(item)} are not the server's"
        if "keys" in data:
            stub["state"]["keys"] = data["keys"]
        for name, value in data.get("auth", {}).items():
            assert name in real["auth"], f"the server's auth has no field {name!r}"
            stub["state"]["auth"][name] = value
        for name in ("me", "me_fails", "me_delays", "probe", "probe_delay", "probe_fails", "state_fails", "state_delays",
                     "key_refuse", "reload_error"):
            if name in data:
                stub[name] = data[name]
        return {}

    @app.get("/__stub/patched")
    async def patched():
        return stub["patched"]

    @app.get("/__stub/written")
    async def written():
        return {"keys": stub["written"], "signing_keys_made": stub["signing_keys_made"]}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/setup", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(monkeypatch, tmp_path_factory.mktemp("setup_panel"))
        yield run_app_test_page(BROWSER, app, "tests/setup/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the key table shows each key’s state, three sections and the count of the rest, and a key name is text',
    'a configuration that names no key says so',
    'a refresh, or a failed /auth/me, keeps a half-typed password; a wrong current one keeps the form; the right one changes it',
    'another admin sees the warning without the form, and what cannot be told reads Unknown',
    'the signing key line says a known key needs fixing, a pending restart, and a known key a restart would bring',
    'a chat test locks its button and says it runs, then shows the answer or the provider’s error as text',
    'a refresh during a chat test keeps it running, and a failed test replaces the answer before it',
    'a state that cannot be read says so, a chat test still shows its outcome, and the next read clears it',
    'a load overtaken by a newer one is never drawn, nor clears the newer one’s notice',
    'a key is sent and its field cleared, a refused one keeps what was typed, one the environment sets has no field',
    'an own signing key is offered only while a known one is configured, asked for first, then made',
    'what is typed in one row survives a save in another, and a failed reload says to restart',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_setup_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
