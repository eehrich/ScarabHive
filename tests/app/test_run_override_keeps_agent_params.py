"""A profile chosen for a RUN still runs with the agent's own llm_params.

An override picks another MODEL, not another agent. ``_get_agent_with_overrides``
built the client without ``agent_config.llm_params``, so everything the agent
says about every model it runs on was silently gone for that run -- measured on
the coder, whose ``context_window: 200000`` and ``prompt_cache_mode`` never
reached the profile picked in the panel; its calls were then counted against the
model's 272000, which is what made ``/context`` show two different windows.

Fallbacks never had this hole (``_create_fallback_llm`` passes them through), and
now neither do the four override paths that share ``agent_params_for_profile``.
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.anyio


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


def _app_with_a_capped_agent(monkeypatch):
    """The real app, whose default agent caps every model it runs on."""
    from agent_system import app as app_mod
    from agent_system.config.models import LLMProfile

    _disable_auth(monkeypatch)
    app = app_mod.build_app()
    config = app.state.config
    config.llm_system.profiles["zz_other"] = LLMProfile(
        model_ref=next(iter(config.llm_system.models)))
    app.state.agent.agent_config.llm_params = {
        "*": {"context_window": 4242}, "zz_other": {"max_tokens": 111}}
    return app


async def test_a_profile_picked_for_the_run_keeps_the_agents_params(monkeypatch):
    from agent_system.llm import factory as llm_factory

    app = _app_with_a_capped_agent(monkeypatch)
    seen = []
    original = llm_factory.create_llm_from_profile

    def recording(*args, **kwargs):
        seen.append(kwargs.get("llm_params"))
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_factory, "create_llm_from_profile", recording)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.post("/run", params={"llm_profile": "zz_other"},
                                     json={"task": "noop"}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert seen, "fixture: the override never built a client"
    # "*" for every model it runs on, the profile's own entry on top of it.
    assert seen[0] == {"context_window": 4242, "max_tokens": 111}, seen[0]
