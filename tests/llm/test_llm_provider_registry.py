"""The LLM provider registry: manifest scan, lazy loading, and closure.

The registry replaced the make_llm if-chain (2026-08). Its contract:
`plugins_llm/*/plugin.toml` declares which provider names a plugin serves
(`provides` / `provides_batch`), and the first build for a name imports
exactly that plugin. Three failure modes matter:

* a provider configured in llm.yaml that NO plugin declares (broken
  dispatch — every agent using it dies at build time),
* a manifest that declares a name its PROVIDERS dict does not export
  (found only at first use, so it must be a loud, named error),
* the conftest seam: bootstrap must hit the fake, real-path tests the
  original — mixing them up makes hundreds of tests measure nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import LLMModelConfig
from agent_system.llm import registry


@pytest.fixture(autouse=True)
def fresh_registry():
    """Scan/import caches are process-global; every test here starts from a
    cold registry so no test inherits (or masks) another's state — and the
    reset semantics themselves stay exercised."""
    registry.reset_for_tests()
    yield
    registry.reset_for_tests()


def _real_build():
    return getattr(registry, "_orig_build_client", registry.build_client)


class TestManifestScan:
    def test_every_shipped_provider_is_served_by_a_plugin(self):
        """Closure over the REAL config: every provider the shipped llm.yaml
        (plus includes) configures must be declared by some plugin — there is
        no Literal in core anymore, the manifests are the only vocabulary."""
        from agent_system.config.settings import load_settings
        models = (load_settings().llm_system.models or {})
        used = {m.provider for m in models.values()}
        assert len(used) >= 4, f"only {used} providers in the shipped config"

        known = registry.known_providers()
        # 'batch' is resolver-internal: mapped to the underlying provider
        # via batch_provider before the registry sees it.
        missing = used - known - {"batch"}
        assert not missing, (
            f"providers configured in the shipped config but served by no "
            f"plugin under src/plugins_llm: {sorted(missing)}")

    def test_config_validation_rejects_an_unknown_provider(self):
        """The Literal used to catch typos at config load; now the
        LLMSystemConfig validator must — against the manifests."""
        from pydantic import ValidationError
        from agent_system.config.models import LLMSystemConfig
        with pytest.raises(ValidationError, match="no plugin"):
            LLMSystemConfig(models={"m": LLMModelConfig(
                provider="antropic", model="x")})

    def test_config_validation_accepts_plugin_and_pseudo_providers(self):
        from agent_system.config.models import LLMSystemConfig
        LLMSystemConfig(models={
            "a": LLMModelConfig(provider="anthropic", model="x"),
            "b": LLMModelConfig(provider="batch", batch_provider="openai",
                                model="y"),
        })

    def test_batch_backends_are_declared_for_all_mapped_providers(self):
        """The resolver maps batch_provider to gemini/openai/anthropic; each
        needs a provides_batch declaration or batch jobs lose their backend."""
        registry._scan_manifests()
        declared = set(registry._batch_dirs or {})
        assert {"gemini", "openai", "anthropic"} <= declared, (
            f"batch backends missing: only {sorted(declared)} declared")

    def test_manifests_agree_with_the_provider_dicts(self):
        """A manifest may promise a name the entrypoint does not export —
        that surfaces only at first use in production, so the suite checks
        every plugin's promise against its PROVIDERS/BATCH_BACKENDS here."""
        import importlib
        registry._scan_manifests()
        for provider, dir_name in sorted((registry._provider_dirs or {}).items()):
            module = importlib.import_module(f"plugins_llm.{dir_name}.provider")
            assert provider in module.PROVIDERS, (
                f"{dir_name}/plugin.toml declares '{provider}' but "
                f"PROVIDERS does not export it")
        for name, dir_name in sorted((registry._batch_dirs or {}).items()):
            module = importlib.import_module(f"plugins_llm.{dir_name}.provider")
            assert name in module.BATCH_BACKENDS, (
                f"{dir_name}/plugin.toml declares provides_batch '{name}' but "
                f"BATCH_BACKENDS does not export it")


class TestDispatch:
    def test_unknown_provider_names_the_known_ones(self):
        with pytest.raises(registry.ProviderNotFoundError) as exc:
            registry.get_provider("no_such_provider")
        msg = str(exc.value)
        assert "no_such_provider" in msg
        assert "openai_httpx" in msg, "the error should list known providers"

    def test_build_client_reaches_the_declared_factory(self):
        cfg = LLMModelConfig(provider="openai_httpx", model="m",
                             api_key="sk-test",
                             base_url="https://openrouter.ai/api/v1")
        client = _real_build()(cfg)
        assert type(client).__name__ == "HTTPXOpenAIClient"

    def test_ollama_compat_delegates_to_the_openai_factory(self):
        """The openai_compat mode used to construct OpenAIAsyncClient in the
        make_llm branch; the delegation must still produce exactly that, with
        the ollama defaults (key 'ollama', localhost base) applied."""
        cfg = LLMModelConfig(provider="ollama", model="qwen3", temperature=0.3)
        client = _real_build()(cfg)
        assert type(client).__name__ == "OpenAIAsyncClient"
        assert client.api_key == "ollama"
        assert "127.0.0.1:11434" in client._base_url

    def test_ollama_native_mode_builds_the_native_client(self):
        cfg = LLMModelConfig(provider="ollama", model="qwen3",
                             ollama_mode="native", context_window=8192)
        client = _real_build()(cfg)
        assert type(client).__name__ == "OllamaNativeAsyncClient"

    def test_batch_backend_lookup_finds_the_plugin_factory(self, monkeypatch):
        factory = registry.get_batch_backend("anthropic")
        assert factory is not None
        # Deterministic, not env-dependent: load_settings() (run by earlier
        # tests in this process) writes real API keys into os.environ, which
        # used to turn this branch into dead code. Clear the variable so the
        # no-key path is ALWAYS the one measured: None (skip), no exception.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = LLMModelConfig(provider="anthropic", model="m")
        assert factory(cfg) is None

    def test_unknown_batch_backend_is_none_not_an_error(self):
        assert registry.get_batch_backend("no_such") is None


class TestBatchRegistration:
    """The wiring initialization._register_batch_clients provides is the ONLY
    link between registry backends and the queue manager — and it swallows
    errors per provider, so without this test the registry lookup could be
    deleted and the suite would stay green (measured during review)."""

    @staticmethod
    def _run(providers, monkeypatch=None):
        import asyncio
        import logging
        from types import SimpleNamespace
        from agent_system.llm.batch.initialization import _register_batch_clients

        registered = {}
        manager = SimpleNamespace(
            register_batch_client=lambda name, client: registered.__setitem__(name, client))
        asyncio.run(_register_batch_clients(
            manager, providers, batch_system_config=None,
            log=logging.getLogger("test")))
        return registered

    def test_backends_reach_the_queue_manager_through_the_registry(self):
        registered = self._run({
            "anthropic": LLMModelConfig(provider="batch", batch_provider="anthropic",
                                        model="claude-x", api_key="sk-test"),
            "openai": LLMModelConfig(provider="batch", batch_provider="openai",
                                     model="gpt-x", api_key="sk-test"),
        })
        assert type(registered.get("anthropic")).__name__ == "AnthropicBatchClient", (
            "the registry lookup in _register_batch_clients no longer reaches "
            "the queue manager — batch models silently lose their backend")
        assert type(registered.get("openai")).__name__ == "OpenAIBatchClient"

    def test_missing_key_skips_without_registering(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        registered = self._run({
            "anthropic": LLMModelConfig(provider="batch", batch_provider="anthropic",
                                        model="claude-x"),
        })
        assert registered == {}

    def test_unknown_batch_provider_registers_nothing(self):
        registered = self._run({
            "bogus": LLMModelConfig(provider="batch", batch_provider="anthropic",
                                    model="m", api_key="sk-test"),
        })
        assert registered == {}


class TestSystemHttpxTimeoutDefault:
    """The one resolver line the registry rework actually changed: without a
    model-level override, the SYSTEM httpx_timeouts must be stamped into the
    spec — and as a copy, not the shared config instance."""

    @staticmethod
    def _config(model_timeouts=None):
        from agent_system.config.models import (
            AgentSystemConfig, HTTPXTimeoutConfig, LLMProfile, LLMSystemConfig,
        )
        return AgentSystemConfig(llm_system=LLMSystemConfig(
            httpx_timeouts=HTTPXTimeoutConfig(read=123.0),
            profiles={"p": LLMProfile(model_ref="m")},
            models={"m": LLMModelConfig(
                provider="openai_httpx", model="x/y", api_key="sk-test",
                httpx_timeouts=model_timeouts)},
        ))

    def test_the_system_default_lands_in_the_spec(self):
        from agent_system.llm.factory import resolve_llm_config_for_agent
        from agent_system.config.models import AgentConfig
        config = self._config()
        resolved = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="p"))
        assert resolved.spec.httpx_timeouts is not None, (
            "system httpx_timeouts never reached the spec — provider "
            "factories silently fall back to their hardcoded read timeouts")
        assert resolved.spec.httpx_timeouts.read == 123.0
        assert resolved.spec.httpx_timeouts is not config.llm_system.httpx_timeouts, (
            "the spec aliases the system config object — a factory mutating "
            "it would edit the system default process-wide")

    def test_a_model_override_beats_the_system_default(self):
        from agent_system.llm.factory import resolve_llm_config_for_agent
        from agent_system.config.models import AgentConfig, HTTPXTimeoutConfig
        config = self._config(model_timeouts=HTTPXTimeoutConfig(read=77.0))
        resolved = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="p"))
        assert resolved.spec.httpx_timeouts.read == 77.0
        # Review finding: the copy protection used to cover only the
        # system-default branch — a model-level httpx_timeouts stayed the
        # SHARED instance. The spec is a deep copy now, in every branch.
        assert resolved.spec.httpx_timeouts is not (
            config.llm_system.models["m"].httpx_timeouts), (
            "model-level httpx_timeouts is aliased into the spec")

    def test_the_spec_never_aliases_the_registry_entry(self):
        """Even with NOTHING to stamp (no system default, no batch, no
        routing merge), the spec must not BE the shared models[...] entry —
        a factory normalizing a nested field in place would otherwise edit
        the system config process-wide."""
        from agent_system.llm.factory import resolve_llm_config_for_agent
        from agent_system.config.models import (
            AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile,
            LLMSystemConfig, ModelCapabilitiesConfig,
        )
        config = AgentSystemConfig(llm_system=LLMSystemConfig(
            profiles={"p": LLMProfile(model_ref="m")},
            models={"m": LLMModelConfig(
                provider="ollama", model="q",
                capabilities=ModelCapabilitiesConfig(json_mode=True),
                provider_routing={"order": ["a"]})},
        ))
        entry = config.llm_system.models["m"]
        spec = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="p")).spec
        assert spec is not entry
        assert spec.capabilities is not entry.capabilities
        assert spec.provider_routing is not entry.provider_routing


class TestSeam:
    def test_conftest_installed_the_fake_and_kept_the_original(self):
        """If this fails, either bootstrap opens sockets in tests again or
        real-path tests silently measure the fake."""
        assert hasattr(registry, "_orig_build_client"), (
            "conftest no longer preserves the original build_client")
        assert registry.build_client is not registry._orig_build_client, (
            "the fake is not installed — bootstrap builds real clients in tests")
