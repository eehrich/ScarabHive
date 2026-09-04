"""POST /run with ``llm_profile`` must resolve the profile against the LIVE
config, not the one ``build_app`` closed over.

``POST /admin/reload-config`` replaces the ConfigService's config and
``app.state.config``; the override path in ``_get_agent_with_overrides``
kept reading the ``build_app`` closure, so a profile added by a reload was
"not found" and silently fell back to the default profile.
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


async def test_run_override_resolves_the_profile_from_the_live_config(monkeypatch, caplog):
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod
    from agent_system.config.models import LLMProfile
    from agent_system.llm import factory as llm_factory

    app = app_mod.build_app()

    # A reload's effect, without the reload: the service now hands out a
    # config that knows one more profile than the build_app closure does.
    stale = app_mod._config_service.get_config()
    live = stale.model_copy(deep=True)
    some_model = next(iter(live.llm_system.models))
    live.llm_system.profiles["zz_live_only"] = LLMProfile(model_ref=some_model)
    assert "zz_live_only" not in stale.llm_system.profiles, "fixture: deep copy did not decouple"

    class LiveConfigService:
        def get_config(self):
            return live

    monkeypatch.setattr(app_mod, "_config_service", LiveConfigService())

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
    """What /run accepts, the listings the UI picks from must know.

    Same staleness, other endpoints: GET /llm/profiles and the "default" of
    GET /agents answered from the build_app closure, so a profile or default
    agent introduced by a reload stayed invisible while /run already used it.
    """
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod
    from agent_system.config.models import LLMProfile

    app = app_mod.build_app()

    stale = app_mod._config_service.get_config()
    live = stale.model_copy(deep=True)
    live.llm_system.profiles["zz_live_only"] = LLMProfile(model_ref=next(iter(live.llm_system.models)))
    live.default_agent = "zz_live_agent"
    assert "zz_live_only" not in stale.llm_system.profiles, "fixture: deep copy did not decouple"
    assert stale.default_agent != "zz_live_agent", "fixture: the default agent was already that"

    class LiveConfigService:
        def get_config(self):
            return live

    monkeypatch.setattr(app_mod, "_config_service", LiveConfigService())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        profiles = await client.get("/llm/profiles", timeout=60.0)
        agents = await client.get("/agents", timeout=60.0)

    assert profiles.status_code == 200, profiles.text
    assert "zz_live_only" in [p["name"] for p in profiles.json()["profiles"]], \
        "the profile list came from the build_app closure"
    assert agents.status_code == 200, agents.text
    assert agents.json()["default"] == "zz_live_agent"
