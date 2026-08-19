"""Plugins must build LLM clients through the full path.

Four plugins resolved their config correctly and then hand-listed the
`make_llm` arguments, which silently dropped everything the list did not
mention: `thinking_level`, `max_tokens`, `safety_settings`, `service_tier`,
`provider_routing`, `parallel_tool_calls` — and batch wrapping.

For the profiles those plugins are configured with today only
`parallel_tool_calls` was lost, and its value equals the default, so nothing
misbehaved. The trap was the next config line: `basic_agent` picks its profile
from a RUNTIME tool argument, so pointing it at an OpenRouter profile was one
call away from losing the tier, the routing and the token cap.

This pins the property rather than the call shape: what the client ends up
carrying has to match what the profile resolves to.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.config.models import AgentConfig
from agent_system.config.settings import load_settings
from agent_system.llm.factory import (
    create_llm_from_profile,
    resolve_llm_config_for_agent,
)

#: Fields that a hand-written make_llm() call sheds. Each one changes what the
#: provider is asked to do, so losing it is never cosmetic.
CARRIED = ("thinking_level", "max_tokens", "service_tier", "provider_routing",
           "safety_settings")


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture
def real_clients(monkeypatch):
    """conftest.py swaps `make_llm` for a fake so bootstrap opens no sockets.

    That fake carries none of the fields under test, so checking "the client
    kept its profile" against it would compare against a stub and pass for the
    wrong reason - or, as here, fail for one. Put the real factory back for the
    duration of the test; constructing a client opens no connection.
    """
    from agent_system.llm import clients

    original = getattr(clients, "_orig_make_llm", None)
    if original is None:
        return
    monkeypatch.setattr(clients, "make_llm", original)


#: Only the OpenAI-compatible clients expose these fields under these names.
#: Gemini's SDK client maps them into its own generation config, so asserting
#: attribute names there would test the mapping, not the forwarding. The
#: OpenAI-compatible route is also exactly where the loss hurt: every
#: OpenRouter profile runs through it.
CARRYING_PROVIDERS = ("openai_httpx", "openai_responses")


def _profile_with_extras(config) -> str:
    """A profile that actually sets the fields — otherwise this proves nothing."""
    for name in config.llm_system.profiles:
        kw = resolve_llm_config_for_agent(config, AgentConfig(llm_profile=name))
        if kw.get("provider") not in CARRYING_PROVIDERS:
            continue
        if sum(1 for f in CARRIED if kw.get(f) is not None) >= 3:
            return name
    pytest.skip("no OpenAI-compatible profile sets enough fields to check")


class TestTheFullPathCarriesTheProfile:
    def test_a_client_built_from_a_profile_keeps_its_fields(self, config, real_clients):
        profile = _profile_with_extras(config)
        expected = resolve_llm_config_for_agent(
            config, AgentConfig(llm_profile=profile))

        client = create_llm_from_profile(config, profile)

        lost = [f for f in CARRIED
                if expected.get(f) is not None
                and getattr(client, f, None) != expected[f]]
        assert not lost, f"profile {profile!r}: these never reached the client: {lost}"


class TestNoPluginHandRollsTheArguments:
    """Anti-drift: the next plugin that needs a client must not copy the old
    pattern back in. `make_llm` itself stays legitimate — clients.py and the
    factory are its home."""

    PLUGINS = ("basic_agent", "context_summarizer", "agent_continuation",
               "llm_router")

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_the_plugin_does_not_call_make_llm(self, plugin):
        root = Path(__file__).parents[2] / "src" / "plugins" / plugin
        assert root.is_dir(), f"{plugin} moved — this check would be vacuous"

        offenders = [
            f"{path.relative_to(root)}:{i}"
            for path in root.rglob("*.py")
            if "test" not in path.name
            for i, line in enumerate(path.read_text(encoding="utf-8",
                                                    errors="replace").splitlines(), 1)
            if "make_llm(" in line and not line.lstrip().startswith("#")
        ]
        assert not offenders, (
            f"{plugin} builds a client by hand again — use "
            f"create_llm_from_profile, which forwards every resolved field: "
            f"{offenders}")
