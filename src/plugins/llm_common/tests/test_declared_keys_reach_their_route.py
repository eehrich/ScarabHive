"""A declared key either reaches the client, or its factory says out loud that this route ignores it.

The rule: silence is the one answer a model entry must never get. A typo dies at construction
(model_dialects.declared_choice), and a key a route cannot honour is logged when the client is built --
otherwise an operator sets the documented line, nothing happens, and the fault is looked for in the model.

No table of who-supports-what lives here on purpose: the check drives every factory with every key and
asks the built client whether it took the value. A new key, or a factory that forgets one, is caught
without anybody remembering to update a list.
"""
from __future__ import annotations

import logging

import pytest

from agent_system.config.models import LLMModelConfig
from plugins.llm_common.model_dialects import DIALECT_KEYS

FACTORIES = {
    "openai_httpx": ("plugins.llm_openai_compat.provider", "build_openai_httpx"),
    "openai_responses": ("plugins.llm_openai_compat.provider", "build_openai_responses"),
    "openrouter_sdk": ("plugins.llm_openrouter.provider", "build_openrouter_sdk"),
    "anthropic": ("plugins.llm_anthropic.provider", "build_anthropic"),
    "openai": ("plugins.llm_openai.provider", "build_openai"),
    "ollama": ("plugins.llm_ollama.provider", "build_ollama"),
    "gemini": ("plugins.llm_gemini.provider", "build_gemini"),
    "gemini_sdk": ("plugins.llm_gemini.provider", "build_gemini_sdk"),
}

#: A value per key that differs from every route's default, so "the client took it" is visible.
DECLARED = {
    "tool_schema_dialect": "gemini_function_declarations",
    "assistant_reasoning_field": "reasoning_content",
    "reasoning_details_mode": "strip",
    "thinking_request_shape": "adaptive",
    "stream_silence_timeout": 42.0,
    "provider_affinity_minutes": 7.0,
}


def build(provider: str, **keys):
    module_name, function_name = FACTORIES[provider]
    module = __import__(module_name, fromlist=[function_name])
    config = LLMModelConfig(provider=provider, model="some/model", api_key="test-key",
                            base_url="https://openrouter.ai/api/v1", **keys)
    return getattr(module, function_name)(config)


@pytest.mark.parametrize("provider", sorted(FACTORIES))
@pytest.mark.parametrize("field", sorted(DECLARED))
def test_every_declared_key_is_either_honoured_or_reported(provider, field, caplog):
    value = DECLARED[field]

    with caplog.at_level(logging.WARNING):
        client = build(provider, **{field: value})

    honoured = getattr(client, field, None) == value
    reported = [record.getMessage() for record in caplog.records
                if field in record.getMessage() and "not wired" in record.getMessage()]
    assert honoured != bool(reported), (
        f"{provider}: {field}={value!r} was "
        f"{'both taken and reported' if honoured else 'neither taken nor reported'}")
    if reported:
        assert "some/model" in reported[0], "the warning must name the model entry that carries the key"


@pytest.mark.parametrize("provider", sorted(FACTORIES))
def test_a_model_entry_without_any_declared_key_stays_quiet(provider, caplog):
    with caplog.at_level(logging.WARNING):
        build(provider)

    assert not [r for r in caplog.records if "not wired" in r.getMessage()], caplog.text


def test_the_keys_checked_here_are_the_ones_the_shared_module_knows():
    """Otherwise a fifth key could be added and this file would still look thorough."""
    assert sorted(DECLARED) == sorted(DIALECT_KEYS)
