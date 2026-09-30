"""A blank key is no key: "  " or a stray newline from a secrets file counted
as set and went out as the x-api-key header."""

import pytest

from agent_system.config.models import LLMModelConfig
from plugins.llm_anthropic.provider import build_anthropic, make_batch_backend


def _cfg(api_key):
    return LLMModelConfig(provider="anthropic", model="claude-x", api_key=api_key)


def test_a_blank_entry_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-env\n")
    client = build_anthropic(_cfg("   "))
    assert client.api_key == "sk-fake-env"


def test_blank_everywhere_is_no_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", " \n")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
        build_anthropic(_cfg(" "))
    assert make_batch_backend(_cfg(" ")) is None
