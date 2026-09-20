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


def _build(base_url, api_key=None, provider="openai_responses"):
    from agent_system.config.models import LLMModelConfig
    return build_client(LLMModelConfig(
        provider=provider, model="m", api_key=api_key,
        base_url=base_url, request_timeout=60))


#: Every provider whose client speaks the OpenAI wire format and therefore
#: accepts a foreign base_url. The rule lived only on openai_responses; the
#: two below defaulted to OPENAI_API_KEY no matter which host they addressed.
OPENAI_COMPATIBLE = ["openai_responses", "openai_httpx", "openai"]


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


class TestUnknownHostsGetNoKeyAtAll:
    """The rule is an ALLOWLIST, not "openrouter.ai or else OpenAI".

    `${DEEPSEEK_API_KEY}` expands to an empty string when the variable is
    unset, the empty string is falsy, and the fallback took over: the four
    shipped DeepSeek models (`openai_httpx`, base_url api.deepseek.com) would
    have come up healthy with the OpenAI secret in an Authorization header
    addressed to DeepSeek. Same for any other gateway someone points a model
    at. A loud config error is the only safe answer.
    """

    @pytest.mark.parametrize("base_url", [
        "https://api.deepseek.com/v1",
        "https://api.groq.com/openai/v1",
        "https://api.mistral.ai/v1",
        # Substring, not host: the old check said "openrouter.ai in url".
        "https://myproxy.example.com/openrouter.ai/v1",
        "https://openrouter.ai.example.com/v1",
    ])
    @pytest.mark.parametrize("provider", OPENAI_COMPATIBLE)
    def test_a_foreign_host_never_receives_the_openai_key(
            self, keys, provider, base_url):
        keys(OPENAI_API_KEY="sk-openai-secret",
             OPENROUTER_API_KEY="sk-or-secret")

        with pytest.raises(ValueError) as exc:
            _build(base_url, provider=provider)
        message = str(exc.value)
        assert "api_key" in message, "the error must say how to fix it"
        assert "sk-openai-secret" not in message

    @pytest.mark.parametrize("base_url", [
        "http://localhost:1234/v1",
        "http://127.0.0.1:8000/v1",
        "http://192.0.2.64:11434/v1",
        "http://[::1]:8000/v1",
        # A name without a dot has no TLD: docker-compose and k8s services.
        "http://ollama:11434/v1",
        "http://vllm:8000/v1",
    ])
    def test_local_servers_keep_the_fallback(self, keys, base_url):
        """Counter-check: LM Studio, vLLM, an Ollama box on the LAN — they
        speak the OpenAI format and usually ignore the key, and the secret
        does not leave the network. Breaking those would be the cure killing
        the patient."""
        keys(OPENAI_API_KEY="sk-local")

        assert _build(base_url, provider="openai_httpx").api_key == "sk-local"

    @pytest.mark.parametrize("base_url", [
        # Public IPv6 carries no dot either — the "no TLD" rule must not
        # reach it, or every v6 endpoint becomes "local".
        "http://[2606:4700::1111]/v1",
        # CGNAT belongs to a carrier, not to us.
        "http://100.64.0.1:8000/v1",
    ])
    def test_a_dotless_host_is_not_automatically_local(self, keys, base_url):
        keys(OPENAI_API_KEY="sk-openai-secret")

        with pytest.raises(ValueError) as exc:
            _build(base_url, provider="openai_httpx")
        assert "sk-openai-secret" not in str(exc.value)

    def test_an_explicit_key_works_for_any_host(self, keys):
        """The way to use a gateway: name its key in the model entry."""
        keys()
        client = _build("https://api.deepseek.com/v1", api_key="sk-deepseek",
                        provider="openai_httpx")
        assert client.api_key == "sk-deepseek"


class TestTheRuleCoversEveryOpenAICompatibleProvider:
    """The leak is a property of the WIRE FORMAT, not of one provider.

    openai_httpx and the SDK client accept any OpenAI-compatible base_url —
    openai_httpx is the documented route for OpenRouter models that need
    chat/completions. Both used to read OPENAI_API_KEY regardless of host,
    so pointing either at a gateway and forgetting its key sent the OpenAI
    secret there.
    """

    @pytest.mark.parametrize("provider", OPENAI_COMPATIBLE)
    def test_openrouter_endpoint_refuses_the_openai_key(self, keys, provider):
        keys(OPENAI_API_KEY="sk-openai-secret")

        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            _build("https://openrouter.ai/api/v1", provider=provider)

    @pytest.mark.parametrize("provider", OPENAI_COMPATIBLE)
    def test_openrouter_endpoint_uses_the_openrouter_key(self, keys, provider):
        keys(OPENROUTER_API_KEY="sk-or-value")

        client = _build("https://openrouter.ai/api/v1", provider=provider)
        assert client.api_key == "sk-or-value"

    @pytest.mark.parametrize("provider", OPENAI_COMPATIBLE)
    def test_openai_endpoint_still_uses_the_openai_key(self, keys, provider):
        """Counter-check: the refusal is about the host, not about env keys
        being rejected wholesale."""
        keys(OPENAI_API_KEY="sk-openai-secret")

        client = _build(OPENAI, provider=provider)
        assert client.api_key == "sk-openai-secret"
