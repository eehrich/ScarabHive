"""prompt_cache_ttl_minutes: how long a model's prompt cache probably lives, for the system to read.

The backend pin (provider_affinity_minutes) has no point past the cache's life, so an entry that sets no
window of its own pins for as long as its cache lives. Built through the real config and the real provider
factories: a route that forgets the fallback keeps the old flat 30 minutes and only shows up here.
"""
from __future__ import annotations

import pytest

from agent_system.config.settings import load_settings
from plugins.llm_openai_compat.provider import build_openai_httpx, build_openai_responses
from plugins.llm_openrouter.provider import build_openrouter_sdk

# The factories themselves: conftest swaps registry.build_client for a fake in every test.
ROUTES_WITH_THE_PIN = {"openai_httpx": build_openai_httpx, "openai_responses": build_openai_responses,
                       "openrouter_sdk": build_openrouter_sdk}


def _entries_on_a_pinning_route():
    models = load_settings().llm_system.models
    return [(name, cfg) for name, cfg in models.items() if cfg.provider in ROUTES_WITH_THE_PIN]


def test_the_config_names_a_cache_life_somewhere():
    """Without one entry setting it, every assertion below would compare None with None."""
    assert any(cfg.prompt_cache_ttl_minutes for _name, cfg in _entries_on_a_pinning_route())


@pytest.mark.parametrize("name, cfg", _entries_on_a_pinning_route(), ids=lambda value: value
                         if isinstance(value, str) else "")
def test_the_pin_lasts_as_long_as_the_cache_unless_the_entry_says_otherwise(name, cfg):
    # The entries' ${...} keys were expanded when the config loaded, at collection: a checkout
    # without secrets has none, and the pin does not depend on them.
    cfg = cfg.model_copy(update={"api_key": cfg.api_key or "sk-test"})
    expected = (cfg.provider_affinity_minutes if cfg.provider_affinity_minutes is not None
                else cfg.prompt_cache_ttl_minutes)
    assert ROUTES_WITH_THE_PIN[cfg.provider](cfg).provider_affinity_minutes == expected, name


@pytest.mark.parametrize("route", sorted(ROUTES_WITH_THE_PIN))
def test_every_route_pins_for_the_cache_life(route):
    """Each route on its own -- the real config may run none of its entries on one of them."""
    from agent_system.config.models import LLMModelConfig

    cfg = LLMModelConfig(provider=route, model="vendor/model", api_key="sk-or-test",
                         base_url="https://openrouter.ai/api/v1", prompt_cache_ttl_minutes=7)
    assert ROUTES_WITH_THE_PIN[route](cfg).provider_affinity_minutes == 7


def test_an_own_window_wins_and_zero_stays_off():
    from agent_system.config.models import LLMModelConfig
    from plugins.llm_common.model_dialects import affinity_minutes

    def entry(**fields):
        return LLMModelConfig(provider="openai_httpx", model="m", **fields)

    assert affinity_minutes(entry(prompt_cache_ttl_minutes=5)) == 5
    assert affinity_minutes(entry(prompt_cache_ttl_minutes=5, provider_affinity_minutes=12)) == 12
    assert affinity_minutes(entry(prompt_cache_ttl_minutes=5, provider_affinity_minutes=0)) == 0
    assert affinity_minutes(entry()) is None, "neither set: the core default applies"
