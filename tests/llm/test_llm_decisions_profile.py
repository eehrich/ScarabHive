"""Decision models: config entry -> registry seam -> the plugin's client.

The path a decision model takes is the one TTS takes, one seam over: an entry
under ``llm_system.decision_models`` names a provider, a plugin declares that
name via ``provides_decisions``, and the registry imports it on first use.
What is NOT on that path is the chat machinery -- these models answer named
questions and have no per-token price entry, so an entry under ``models:``
would be offered to every agent and checked for a price it does not have.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent_system.config.models import DecisionModelConfig, DecisionProfile
from agent_system.llm.decisions import create_decisions_from_profile


def _config(default=None, **models):
    """A config with one decision model and a profile pointing at it.

    ``default`` is set explicitly even when it is None: on a MagicMock every
    unset attribute is truthy, so a forgotten one would look like a configured
    default and pass a test that should fail.
    """
    config = MagicMock()
    config.llm_system.decision_models = models or {
        "jev": DecisionModelConfig(provider="openrouter_decisions",
                                   model="~typesafe/jev-latest"),
    }
    config.llm_system.decision_profiles = {
        "jev": DecisionProfile(model_ref="jev", description="decides"),
    }
    config.llm_system.default_decision_profile = default
    return config


class TestRegistryDispatch:
    def test_unknown_provider_names_the_known_ones(self):
        """The message has to carry the vocabulary: a typo in the config is
        otherwise a name that exists nowhere and points at nothing."""
        from agent_system.llm import registry
        with pytest.raises(registry.ProviderNotFoundError,
                           match="openrouter_decisions"):
            registry.get_decisions_provider("no_such_provider")

    def test_the_manifest_declares_the_provider_the_shipped_config_uses(self):
        """Nothing imports the plugin to answer this -- the registry reads
        manifests only, which is what config validation depends on."""
        from agent_system.llm import registry
        assert "openrouter_decisions" in registry.known_decisions_providers()

    def test_profile_builds_the_real_client_with_the_configured_values(self):
        """The whole seam end to end, and the half that silently rots: a
        factory that drops request_timeout or max_retries builds a client
        that works -- with someone else's numbers."""
        from plugins.llm_decisions.system_one import DecisionsClient
        config = _config(jev=DecisionModelConfig(
            provider="openrouter_decisions", model="typesafe/jev-1.13",
            api_key="sk-test", request_timeout=7, max_retries=5))

        client = create_decisions_from_profile(config, "jev")

        assert isinstance(client, DecisionsClient)
        assert client.model == "typesafe/jev-1.13"
        assert client.api_key == "sk-test"
        assert client.request_timeout == 7
        assert client.max_retries == 5

    def test_a_configured_url_reaches_the_client(self):
        """The client resolves the API key against the host of ITS url. A url
        the factory drops would send a request meant for a proxy to OpenRouter
        -- with OpenRouter's key on it."""
        config = _config(jev=DecisionModelConfig(
            provider="openrouter_decisions", model="jev",
            api_key="sk-proxy", url="https://proxy.internal/decisions"))

        client = create_decisions_from_profile(config, "jev")

        assert client.url == "https://proxy.internal/decisions"

    @pytest.mark.parametrize("provider, host", [("openrouter_decisions", "OPENROUTER"),
                                                ("systemone_decisions", "SYSTEM_ONE"),
                                                ("ollama_decisions", "OLLAMA")])
    def test_no_url_means_the_providers_own_endpoint(self, provider, host):
        """Every provider builds one client; the host each hands it is the
        difference -- its endpoint, and the name its calls are booked under."""
        from plugins.llm_decisions import system_one
        config = _config(jev=DecisionModelConfig(provider=provider, model="jev", api_key="sk-test"))

        client = create_decisions_from_profile(config, "jev")

        assert client.host is getattr(system_one, host)
        assert client.url == client.host.url and client.host.provider == provider


class TestProfileResolution:
    def test_the_resolved_model_config_is_what_reaches_the_seam(self):
        from agent_system.llm import registry
        config = _config()
        with patch.object(registry, "build_decisions_client") as build:
            create_decisions_from_profile(config, "jev")
        (cfg,), _ = build.call_args
        assert cfg is config.llm_system.decision_models["jev"]

    def test_the_default_profile_answers_when_no_name_is_given(self):
        """A caller that does not care which model judges gets the configured
        one -- the field exists because this reads it."""
        from agent_system.llm import registry
        config = _config(default="jev")
        with patch.object(registry, "build_decisions_client") as build:
            create_decisions_from_profile(config)
        (cfg,), _ = build.call_args
        assert cfg is config.llm_system.decision_models["jev"]

    def test_a_named_profile_beats_the_default(self):
        """Otherwise a caller that DID choose would silently get the default."""
        from agent_system.llm import registry
        config = _config(default="jev")
        config.llm_system.decision_profiles["strict"] = DecisionProfile(model_ref="strict")
        config.llm_system.decision_models["strict"] = DecisionModelConfig(
            provider="openrouter_decisions", model="typesafe/jev-1.13")
        with patch.object(registry, "build_decisions_client") as build:
            create_decisions_from_profile(config, "strict")
        (cfg,), _ = build.call_args
        assert cfg.model == "typesafe/jev-1.13"

    def test_no_name_and_no_default_says_which_of_the_two_is_missing(self):
        """Without this the caller sees "profile None not found", which reads
        like a typo instead of a missing default."""
        with pytest.raises(ValueError, match="default_decision_profile is not set"):
            create_decisions_from_profile(_config())

    def test_missing_profile_lists_what_exists(self):
        with pytest.raises(ValueError, match="nonexistent.*not found"):
            create_decisions_from_profile(_config(), "nonexistent")

    def test_a_profile_pointing_at_no_model_names_the_reference(self):
        """Caught here and not at the first request: the profile is config,
        the request is production."""
        config = _config()
        config.llm_system.decision_profiles = {
            "broken": DecisionProfile(model_ref="does-not-exist"),
        }
        with pytest.raises(ValueError, match="does-not-exist.*not found"):
            create_decisions_from_profile(config, "broken")


class TestConfigModels:
    def test_an_unknown_provider_is_refused_at_config_load(self):
        """Same guard the LLM and TTS providers get: a typo that loads fine
        dies at the first decision instead, far from the file it came from."""
        from agent_system.config.models import LLMSystemConfig
        with pytest.raises(ValueError, match="provides_decisions"):
            LLMSystemConfig(decision_models={
                "typo": DecisionModelConfig(provider="openrouter_decision",
                                            model="jev")})

    def test_an_unknown_key_is_refused_instead_of_dropped(self):
        """This is the one section whose endpoint key is not called base_url.
        Silently dropping the habit from two sections up would send the
        request -- and the OpenRouter key -- to OpenRouter instead of to the
        proxy the operator wrote down."""
        with pytest.raises(ValueError, match="base_url"):
            DecisionModelConfig(model="jev", base_url="https://proxy.internal/x")

    def test_a_profile_key_that_belongs_to_a_chat_profile_is_refused(self):
        """The entry this replaced carried max_steps: 500. Copying it over
        must say so rather than ignore half of it."""
        with pytest.raises(ValueError, match="max_steps"):
            DecisionProfile(model_ref="jev", max_steps=500)

    def test_a_profile_pointing_at_no_model_is_refused_at_load(self):
        """Config load is the cheap moment. The expensive one is now inside an
        agent loop, where agent_continuation asks for a decision per step."""
        from agent_system.config.models import LLMSystemConfig
        with pytest.raises(ValueError, match="decision_profiles"):
            LLMSystemConfig(decision_profiles={"a": DecisionProfile(model_ref="gone")})

    def test_a_default_naming_no_profile_is_refused_at_load(self):
        from agent_system.config.models import LLMSystemConfig
        with pytest.raises(ValueError, match="default_decision_profile"):
            LLMSystemConfig(default_decision_profile="gone")

    def test_negative_retries_are_refused(self):
        """Would silently skip every attempt -- the same trap TTSModelConfig
        closes with the same bound."""
        with pytest.raises(ValueError):
            DecisionModelConfig(model="jev", max_retries=-1)

    def test_the_shipped_config_resolves_to_a_client(self):
        """The one thing the unit tests above cannot see: whether the entries
        in config/ actually point at each other. A dangling model_ref loads
        fine and fails at the first decision."""
        import os
        from agent_system.config.settings import load_settings

        settings = load_settings()
        profiles = settings.llm_system.decision_profiles
        assert profiles, (
            "config/llm_openrouter.yaml ships decision_profiles and a plugin "
            "asks for one — an empty section here means the entries were lost, "
            "not that there is nothing to check")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}):
            for name in profiles:
                assert create_decisions_from_profile(settings, name) is not None
            default = settings.llm_system.default_decision_profile
            if default:
                assert default in profiles, (
                    f"default_decision_profile={default!r} names no profile")
                assert create_decisions_from_profile(settings) is not None
