"""Handlers that answer ABOUT the configuration must read the LIVE one.

``POST /admin/reload-config`` replaces ``app.state.config`` (admin_endpoints)
while every handler closed over the config ``build_app`` started with. The
override path in ``_get_agent_with_overrides`` kept reading that closure, so a
profile added by a reload was "not found" and silently fell back to the
default profile -- measured live on 2026-09-04, and the same staleness sat in
the listings the UI picks from.

The tests below stand in for the reload by writing ``app.state.config``, which
is exactly what the reload endpoint does; the full chain (edit llm.yaml ->
reload -> /run on the new profile) was measured live on the same day.
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.anyio


def _disable_auth(monkeypatch):
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


def _app_with_a_reloaded_config(monkeypatch):
    """An app whose live config knows one profile more than it started with."""
    from agent_system import app as app_mod
    from agent_system.config.models import LLMProfile

    _disable_auth(monkeypatch)
    app = app_mod.build_app()

    stale = app.state.config
    assert stale is not None, "fixture: build_app did not put its config into app.state"
    live = stale.model_copy(deep=True)
    live.llm_system.profiles["zz_live_only"] = LLMProfile(model_ref=next(iter(live.llm_system.models)))
    live.default_agent = "zz_live_agent"
    assert "zz_live_only" not in stale.llm_system.profiles, "fixture: deep copy did not decouple"

    app.state.config = live  # what admin_endpoints does after a successful reload
    return app, live, stale


async def test_run_override_resolves_the_profile_from_the_live_config(monkeypatch, caplog):
    from agent_system.llm import factory as llm_factory

    app, live, _ = _app_with_a_reloaded_config(monkeypatch)

    seen = []
    original = llm_factory.create_llm_from_profile

    def recording(*args, **kwargs):
        seen.append((kwargs.get("config"), kwargs.get("llm_profile")))
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_factory, "create_llm_from_profile", recording)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", params={"llm_profile": "zz_live_only"},
                                 json={"task": "noop"}, timeout=60.0)

    assert resp.status_code == 200, resp.text
    assert seen, "fixture: the override never built a client"
    assert seen[0][1] == "zz_live_only", f"fell back to {seen[0][1]!r} instead of the live profile"
    assert seen[0][0] is live
    assert "falling back to default profile" not in caplog.text


async def test_the_listings_answer_from_the_live_config_too(monkeypatch):
    """What /run accepts, the listings the UI picks from must know."""
    app, _, _ = _app_with_a_reloaded_config(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        profiles = await client.get("/llm/profiles", timeout=60.0)
        config_dump = await client.get("/admin/config", timeout=60.0)

    assert profiles.status_code == 200, profiles.text
    assert "zz_live_only" in [p["name"] for p in profiles.json()["profiles"]], \
        "the profile list came from the build_app closure"
    assert config_dump.status_code == 200, config_dump.text
    assert "zz_live_only" in config_dump.json()["llm_system"]["profiles"], \
        "/admin/config reported the state before the reload"


async def test_the_agent_list_names_the_agent_run_actually_uses(monkeypatch):
    """``default`` must be the entry agent /run runs without agent_name -- not
    config.default_agent, which a reload can move without moving the entry
    agent with it."""
    app, live, stale = _app_with_a_reloaded_config(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        agents = await client.get("/agents", timeout=60.0)

    assert agents.status_code == 200, agents.text
    default = agents.json()["default"]
    assert default == app.state.agent.name
    assert default != live.default_agent, (
        "fixture: the live config still names the same agent, so this test cannot tell "
        "'what /run uses' from 'what the config says'")
    assert default == stale.default_agent, "the entry agent was built from the config at startup"
