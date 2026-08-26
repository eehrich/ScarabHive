"""A key belongs to the host it is sent to.

`provider=openai_responses` defaults its base_url to OpenRouter. The key
lookup used to be `OPENROUTER_API_KEY or OPENAI_API_KEY` — so on a host with
only the OpenAI key configured, the client came up healthy and put the OpenAI
secret into an `Authorization: Bearer` header addressed to openrouter.ai. The
failure surfaced as a 401 on the first turn, by which point the secret had
already been handed to a third party.

The radius of that grew the day the `turbo` profile moved to OpenRouter:
`turbo` is the schema default of three plugins and a link in basic_agent's
chain, so it is the profile you get when you choose nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

OPENROUTER = None  # provider default
OPENAI = "https://api.openai.com/v1"


def build_client(cfg):
    """The REAL construction path.

    conftest.py replaces `registry.build_client` globally with a fake that
    opens no sockets. That fake accepts every argument and carries no
    `api_key`, so a test importing the module-level name would assert
    against a stub — it would pass whatever the key logic does. Third time
    this trap has cost a test in this repo; the original hides under
    `_orig_build_client`.
    """
    from agent_system.llm import registry

    real = getattr(registry, "_orig_build_client", registry.build_client)
    return real(cfg)


@pytest.fixture
def keys(monkeypatch):
    def set_only(**pairs):
        for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
    return set_only


def _build(base_url, api_key=None):
    from agent_system.config.models import LLMModelConfig
    return build_client(LLMModelConfig(
        provider="openai_responses", model="m", api_key=api_key,
        base_url=base_url, request_timeout=60))


class TestTheKeyFollowsTheEndpoint:
    def test_the_openai_key_is_not_sent_to_openrouter(self, keys):
        keys(OPENAI_API_KEY="sk-openai-secret")

        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            _build(OPENROUTER)

    def test_the_openrouter_key_is_used_for_openrouter(self, keys):
        keys(OPENROUTER_API_KEY="sk-or-value")

        assert _build(OPENROUTER).api_key == "sk-or-value"

    def test_the_openai_key_is_used_for_openai(self, keys):
        """Counter-check: the refusal above must come from the endpoint
        mismatch, not from the provider rejecting every environment key."""
        keys(OPENAI_API_KEY="sk-openai-secret")

        assert _build(OPENAI).api_key == "sk-openai-secret"

    def test_an_explicit_key_still_wins(self, keys):
        keys()
        client = _build(OPENROUTER, api_key="sk-explicit")
        assert client.api_key == "sk-explicit"

    def test_the_error_names_the_variable_and_the_host(self, keys):
        """The model never sees this, an operator does — it has to say which
        key is missing and where the request would have gone."""
        keys()
        with pytest.raises(ValueError) as exc:
            _build(OPENROUTER)
        assert "OPENROUTER_API_KEY" in str(exc.value)
        assert "openrouter.ai" in str(exc.value)
