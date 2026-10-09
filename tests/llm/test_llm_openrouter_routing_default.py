"""System-wide default for OpenRouter provider selection.

``llm_system.openrouter_routing`` puts a ``provider`` object under EVERY
model that talks to OpenRouter -- e.g. ``{sort: price}`` for the cheapest
provider, without touching 37 model entries.

Three things must hold, and each of them has gone wrong before:

* The value must ARRIVE on the production path. So it is checked through
  ``resolve_llm_config_for_agent()`` -- the one place both the agent and the
  profile path run through -- and for the last hop through
  ``registry.build_client()`` itself, because behind the resolver every
  provider factory reads the spec on its own and can forget a field on its
  own.
* It may go to OpenRouter ONLY. ``provider`` is an OpenRouter body field;
  on a foreign endpoint it would be an unknown key in the request.
* What counts is the EFFECTIVE base_url. The openai_responses factory
  (plugins/llm_openai_compat) falls back to OpenRouter when no base_url is
  given -- such an entry talks to OpenRouter although the config field is
  empty.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
)
from agent_system.llm.factory import resolve_llm_config_for_agent


def _real_build_client():
    """conftest.py replaces ``registry.build_client`` globally with a fake so
    that no sockets open during bootstrap. The wiring tests need the
    original -- it lives under ``_orig_build_client``."""
    from agent_system.llm import registry
    return getattr(registry, "_orig_build_client", registry.build_client)

#: Anchored at the file, not at the CWD -- otherwise the catalogue part skips
#: itself silently as soon as pytest runs from a subdirectory.
OPENROUTER_YAML = REPO_ROOT / "config" / "llm_openrouter.yaml"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _resolve(model: LLMModelConfig, openrouter_routing: dict | None = None) -> LLMModelConfig:
    """Send a model through the real resolver; returns the resolved spec
    (ResolvedLLM.spec)."""
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            openrouter_routing=openrouter_routing,
            profiles={"p": LLMProfile(model_ref="m")},
            models={"m": model},
        ),
    )
    return resolve_llm_config_for_agent(cfg, AgentConfig(llm_profile="p")).spec


def _openrouter_model(**overrides) -> LLMModelConfig:
    return LLMModelConfig(
        provider="openai_httpx", model="some/model",
        base_url=OPENROUTER_URL, **overrides)


class TestSystemDefaultReachesOpenRouterModels:
    def test_default_lands_on_a_model_without_own_routing(self):
        kwargs = _resolve(_openrouter_model(), {"sort": "price"})
        assert kwargs.provider_routing == {"sort": "price"}

    def test_absent_default_changes_nothing(self):
        """Off is off -- without the switch the request stays as before."""
        assert _resolve(_openrouter_model()).provider_routing is None

    def test_model_entry_survives_without_the_switch(self):
        kwargs = _resolve(_openrouter_model(provider_routing={"order": ["a"]}))
        assert kwargs.provider_routing == {"order": ["a"]}

    def test_host_match_ignores_case(self):
        """The guard compares lower-cased -- otherwise the spelling in the
        yaml decides whether the switch takes effect."""
        model = _openrouter_model()
        model.base_url = "HTTPS://OpenRouter.AI/api/v1"
        assert _resolve(model, {"sort": "price"}).provider_routing == {
            "sort": "price"}


class TestEffectiveBaseUrlDecides:
    """Every provider factory fills in its own default base_url.

    ``openai_responses`` without a base_url ends up at OpenRouter
    (llm_openai_compat/provider.py), ``openai_httpx`` at api.openai.com.
    Whoever looks only at the config field treats both alike -- and is
    wrong for one of them.
    """

    def test_responses_without_base_url_counts_as_openrouter(self):
        model = LLMModelConfig(provider="openai_responses", model="m")
        assert _resolve(model, {"sort": "price"}).provider_routing == {
            "sort": "price"}

    def test_responses_pointed_elsewhere_stays_out(self):
        model = LLMModelConfig(provider="openai_responses", model="m",
                               base_url="https://api.openai.com/v1")
        assert _resolve(model, {"sort": "price"}).provider_routing is None

    def test_the_configured_default_matches_make_llm(self):
        """Anti-drift: the guard mirrors clients.py. If the default base_url
        changes there, this must notice -- otherwise the guard points at an
        endpoint that no longer exists."""
        client = _real_build_client()(LLMModelConfig(
            provider="openai_responses", model="m", api_key="sk-test"))
        assert "openrouter.ai" in str(client.base_url).lower(), (
            "openai_responses no longer defaults to OpenRouter -- "
            "_targets_openrouter() in factory.py draws the wrong line")

    def test_every_manifest_default_matches_its_factory(self):
        """The default stands TWICE: in the manifest (the resolver reads it)
        and as a literal in the factory (the client uses it). If they drift
        apart, `_targets_openrouter` answers the question "does this entry
        really go to OpenRouter?" wrongly -- provider_routing lands on a
        foreign endpoint or is dropped. The comment in the manifest promises
        exactly this test; until now it existed only for openai_responses.
        """
        import os
        from unittest.mock import patch

        from agent_system.llm import registry

        registry._scan_manifests()
        declared = dict(registry._default_base_urls)
        assert declared, "no plugin declares default_base_url -- test is blind"

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-t",
                                     "OPENROUTER_API_KEY": "sk-t"}):
            for provider, manifest_url in sorted(declared.items()):
                client = _real_build_client()(LLMModelConfig(
                    provider=provider, model="m"))
                # The clients expose the URL differently (public `base_url`
                # or `_base_url` on the SDK wrapper) -- ask for both names so
                # the test measures the wiring and not the naming choice.
                actual = str(getattr(client, "base_url", None)
                             or getattr(client, "_base_url", "")).rstrip("/")
                assert actual == manifest_url.rstrip("/"), (
                    f"{provider}: manifest says {manifest_url}, the factory "
                    f"builds {actual!r}")


class TestModelEntryWinsPerKey:
    def test_own_order_is_kept_alongside_the_default(self):
        """``order`` keeps the implicit prompt cache warm -- the global
        switch must not wipe it."""
        kwargs = _resolve(
            _openrouter_model(provider_routing={"order": ["google-vertex"]}),
            {"sort": "price"})
        assert kwargs.provider_routing == {
            "sort": "price", "order": ["google-vertex"]}

    def test_own_value_beats_the_default_on_the_same_key(self):
        kwargs = _resolve(
            _openrouter_model(provider_routing={"sort": "throughput"}),
            {"sort": "price"})
        assert kwargs.provider_routing == {"sort": "throughput"}

    def test_nested_values_are_replaced_whole_not_merged(self):
        """Deliberately FLAT: a deep merge on a free-form dict would be the
        bigger surprise. The price for that is pinned down here so nobody
        reverses it by accident later -- the default's completion cap does
        NOT survive an own max_price."""
        kwargs = _resolve(
            _openrouter_model(provider_routing={"max_price": {"prompt": 5}}),
            {"max_price": {"prompt": 1, "completion": 2}})
        assert kwargs.provider_routing == {"max_price": {"prompt": 5}}


class TestForeignEndpointsStayUntouched:
    """``provider`` is an OpenRouter field -- elsewhere a foreign body."""

    @pytest.mark.parametrize("provider,base_url", [
        ("openai_httpx", None),
        ("openai_httpx", "https://api.openai.com/v1"),
        ("openai_httpx", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ("openai_httpx", "http://localhost:11434/v1"),
        ("ollama", None),
    ])
    def test_default_does_not_leak(self, provider, base_url):
        model = LLMModelConfig(provider=provider, model="m", base_url=base_url)
        assert _resolve(model, {"sort": "price"}).provider_routing is None

    def test_own_routing_still_passes_through(self):
        """Whoever writes it on the model explicitly gets it -- the switch
        only decides about the DEFAULT."""
        model = LLMModelConfig(
            provider="openai_httpx", model="m",
            base_url="https://api.openai.com/v1",
            provider_routing={"order": ["x"]})
        assert _resolve(model, {"sort": "price"}).provider_routing == {
            "order": ["x"]}


class TestFactoriesForwardRoutingPerProvider:
    """The last hop: every provider factory forwards ``provider_routing`` on
    its own. If one drops out, it loses the routing silently -- the resolver
    next to it stays green because it did its job.
    """

    @pytest.mark.parametrize(
        "provider", ["openai_httpx", "openai_responses", "openrouter_sdk"])
    def test_routing_reaches_the_client(self, provider):
        client = _real_build_client()(LLMModelConfig(
            provider=provider, model="some/model", api_key="sk-test",
            base_url=OPENROUTER_URL, context_window=200000,
            provider_routing={"sort": "price"}))
        assert getattr(client, "provider_routing", None) == {"sort": "price"}, (
            f"the {provider} factory does not forward provider_routing -- "
            f"the routing silently falls by the wayside")


@pytest.mark.skipif(not OPENROUTER_YAML.exists(), reason="no llm_openrouter.yaml")
class TestAgainstTheShippedModels:
    """Against the REAL model catalogue, not a replica.

    A self-built model has exactly the base_url the author happens to think
    of. The switch is meant to catch the shipped entries, though.

    Both tests secure their BASE SET: an empty set satisfies every
    all-statement, and a filter that finds nothing anymore could otherwise
    not be told apart from a working switch.
    """

    @staticmethod
    def _shipped() -> dict[str, LLMModelConfig]:
        """The catalogue comes from the merged config, not from a single
        file: since models inherit from each other (``extends``, resolved in
        settings) a raw YAML entry is incomplete -- it carries only what it
        changes against its parent."""
        from agent_system.config.settings import load_settings

        # Without `model:` an entry is a base class to inherit from -- no
        # profile points at it, it never reaches a client.
        return {n: m for n, m in (load_settings().llm_system.models or {}).items()
                if m.model}

    def test_every_shipped_openrouter_model_gets_the_default(self):
        shipped = self._shipped()
        via_openrouter = {
            name: model for name, model in shipped.items()
            if "openrouter.ai" in (model.base_url or "").lower()}
        assert len(via_openrouter) >= 20, (
            f"found only {len(via_openrouter)} OpenRouter models in the "
            f"catalogue (of {len(shipped)}) -- the test no longer measures "
            f"what it should")

        missed = [
            name for name, model in via_openrouter.items()
            if (_resolve(model, {"sort": "price"}).provider_routing
                or {}).get("sort") != "price"
        ]
        assert not missed, f"switch does not reach these models: {missed}"

    def test_shipped_order_entries_are_not_overwritten(self):
        shipped = self._shipped()
        with_order = {name: model for name, model in shipped.items()
                      if (model.provider_routing or {}).get("order")}
        assert len(with_order) >= 12, (
            f"only {len(with_order)} entries with provider_routing.order -- "
            f"the test no longer measures what it should")

        losses = [
            name for name, model in with_order.items()
            if (_resolve(model, {"sort": "price"}).provider_routing or {}).get("order")
            != model.provider_routing["order"]
        ]
        assert not losses, f"order was lost for: {losses}"
