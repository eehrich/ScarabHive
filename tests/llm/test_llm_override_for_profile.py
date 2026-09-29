"""override_for_profile: the one way every entry point builds a switch.

The API's llm_profile, the CLI's --llm, the chat's /model, use_advanced_model
and a caller's switch a sub-agent follows all build (client, label) here.
Driven through the real factory; only registry.build_client is replaced, and it
records the resolved spec, so what the client would carry is what is checked.
"""
from types import SimpleNamespace

import pytest

from agent_system.config.models import (AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile,
                                        LLMSystemConfig)
from agent_system.llm import registry as llm_registry
from agent_system.llm.factory import UnknownLLMProfile, override_for_profile

CONFIG = AgentSystemConfig(llm_system=LLMSystemConfig(
    models={"m": LLMModelConfig(provider="openai", model="gpt-x", api_key="fake-key"),
            "n": LLMModelConfig(provider="openai", model="gpt-y", api_key="fake-key")},
    profiles={"picked": LLMProfile(model_ref="m"), "other": LLMProfile(model_ref="n")},
    default_profile="picked",
))


@pytest.fixture
def built(monkeypatch):
    """The specs the factory built clients from, in order."""
    specs = []

    def build(spec, ssl_verify=None, **_):
        specs.append(spec)
        return SimpleNamespace(model=spec.model)

    monkeypatch.setattr(llm_registry, "build_client", build)
    return specs


def test_the_label_names_the_profile_and_the_model_it_runs(built):
    client, label = override_for_profile(CONFIG, AgentConfig(llm_profile="other"), "picked")

    assert label == "picked:openai/gpt-x"
    assert client.model == "gpt-x"


def test_the_agents_own_params_apply_and_typed_ones_win_over_them(built):
    agent = AgentConfig(llm_profile="other",
                        llm_params={"*": {"context_window": 4242, "max_tokens": 100}})

    _, label = override_for_profile(CONFIG, agent, "picked", {"max_tokens": 7})

    spec = built[-1]
    assert spec.context_window == 4242, "the agent's own params did not reach the switch"
    assert spec.max_tokens == 7, "what was typed for this run did not win"
    # The label names what was typed, not the agent's own params.
    assert label == "picked:openai/gpt-x +params(max_tokens=7)"


def test_a_profile_the_config_does_not_have_is_refused_with_the_ones_it_has(built):
    with pytest.raises(UnknownLLMProfile) as caught:
        override_for_profile(CONFIG, AgentConfig(llm_profile="picked"), "gone")

    assert isinstance(caught.value, ValueError), "callers catch it as the ValueError it was"
    assert caught.value.available == ["other", "picked"]
    assert "'gone'" in str(caught.value) and "other" in str(caught.value)
    assert not built, "a refused profile still built a client"
