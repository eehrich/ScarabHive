"""The chat probe: one short request the way the default agent would send it. The root conftest fakes
only the network edge (registry.build_client); config, agent, profile and factory are the real ones."""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.settings import load_settings
from plugins.setup import probe


@pytest.fixture(scope="module")
def config():
    return load_settings()


class Refusing:
    model = "some/model"

    def __init__(self, error=None, answer="", delay=0.0):
        self.error, self.answer, self.delay = error, answer, delay

    async def chat(self, messages, cancellation_token=None):
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer


class TestTheProbe:
    async def test_it_goes_through_the_configured_default_agent(self, config):
        from agent_system.config.settings import get_tool_server_config
        agent, profile = probe.default_chat_profile(config)
        # the profile an agent run starts on (factory.resolve_llm_config_for_agent reads the same property)
        first = get_tool_server_config(config.default_agent, config).agent_config.default_llm_profile
        assert (agent, profile) == (config.default_agent, first), (agent, profile)

        result = await probe.probe_chat(config)

        assert result["ok"] is True, result
        assert (result["agent"], result["profile"]) == (agent, profile)
        assert result["model"], "the client the factory built carries no model"

    async def test_a_refused_key_is_reported_with_the_providers_words(self, config, monkeypatch):
        monkeypatch.setattr(probe, "create_llm_from_profile",
                            lambda cfg, profile: Refusing(error=RuntimeError('HTTP 401: {"message":"User not found."}')))

        result = await probe.probe_chat(config)

        assert result["ok"] is False and "401" in result["error"] and "User not found" in result["error"], result

    async def test_a_silent_provider_ends_at_the_timeout(self, config, monkeypatch):
        monkeypatch.setattr(probe, "create_llm_from_profile", lambda cfg, profile: Refusing(answer="OK", delay=5))

        result = await probe.probe_chat(config, timeout=0.05)

        assert result["ok"] is False and "no answer within" in result["error"], result

    async def test_an_empty_answer_is_no_answer(self, config, monkeypatch):
        monkeypatch.setattr(probe, "create_llm_from_profile", lambda cfg, profile: Refusing(answer="  "))

        assert (await probe.probe_chat(config))["ok"] is False

    async def test_an_agent_without_a_profile_is_said_so(self):
        config = SimpleNamespace(default_agent=None)

        result = await probe.probe_chat(config)

        assert result["ok"] is False and "names no LLM profile" in result["error"], result


async def test_a_batch_profile_is_refused_before_anything_is_queued(config, monkeypatch):
    """The batch client queues the request as a real batch job and waits for the batch: no probe for it."""
    batch = next((name for name, profile in config.llm_system.profiles.items()
                  if config.llm_system.models.get(profile.model_ref) is not None
                  and config.llm_system.models[profile.model_ref].provider == "batch"), None)
    if batch is None:
        pytest.skip("the configuration has no batch profile")
    built = []
    monkeypatch.setattr(probe, "create_llm_from_profile", lambda cfg, profile: built.append(profile))

    result = await probe.probe_chat(config, batch)

    assert result["ok"] is False and "batch" in result["error"] and built == [], result
