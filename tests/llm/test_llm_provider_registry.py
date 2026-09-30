"""The LLM provider registry: manifest scan, lazy loading, and closure.

The registry replaced the make_llm if-chain (2026-08). Its contract:
`plugins/*/plugin.toml` declares which provider names a plugin serves
(`provides` for chat, plus `provides_batch` / `provides_tts` /
`provides_decisions` — `registry.SEAMS` is the full list, and these tests read
it rather than keeping a copy), and the first build for a name imports exactly
that plugin. Three failure modes matter:

* a provider configured in llm.yaml that NO plugin declares (broken
  dispatch — every agent using it dies at build time),
* a manifest that declares a name its PROVIDERS dict does not export
  (found only at first use, so it must be a loud, named error),
* the conftest seam: bootstrap must hit the fake, real-path tests the
  original — mixing them up makes hundreds of tests measure nothing.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

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
            f"plugin under src/plugins: {sorted(missing)}")

    def test_the_root_lookup_survives_the_module_discovery_hand_builds(
            self, tmp_path):
        """The providers share `plugins` with the tool servers since
        2026-09-20, and that package has a second registrar.

        plugins/discovery.py puts a hand-built types.ModuleType into
        sys.modules whenever nothing has imported the package yet. Such a
        module carries __spec__ = None, and importlib.util.find_spec on a
        module already in sys.modules reads exactly that attribute and raises
        ValueError instead of searching — measured, below, through discovery
        itself. Until the move the registry asked for a package name discovery
        never touches, so the collision could not happen.

        An empty directory named `plugins` is enough: what is being measured
        is which directory the lookup reports, not what is in it.
        """
        import importlib.util

        from agent_system.plugins.discovery import discover_plugins

        fake_root = tmp_path / registry.PLUGIN_PACKAGE
        fake_root.mkdir()

        # Hand-rolled, not monkeypatch.delitem: with raising=False and no
        # entry to record, its undo leaves whatever the test ADDED behind —
        # and what this test adds is a stand-in for `plugins` that every
        # later test in the process would then import from.
        outside = object()
        saved = sys.modules.pop(registry.PLUGIN_PACKAGE, outside)
        try:
            discover_plugins(fake_root)
            registered = sys.modules[registry.PLUGIN_PACKAGE]
            assert registered.__spec__ is None, (
                "discovery now registers a module WITH a spec — this test no "
                "longer reproduces the collision it was written for")
            with pytest.raises(ValueError):
                importlib.util.find_spec(registry.PLUGIN_PACKAGE)

            assert registry._plugins_root() == fake_root
        finally:
            sys.modules.pop(registry.PLUGIN_PACKAGE, None)
            if saved is not outside:
                sys.modules[registry.PLUGIN_PACKAGE] = saved

    def test_the_root_lookup_finds_the_real_package(self):
        """The other half: with the package properly imported, the lookup
        lands on src/plugins — the directory the manifests are read from."""
        import plugins  # noqa: F401  — the real package, not a stand-in

        assert registry._plugins_root() == REPO_ROOT / "src" / "plugins"

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

    def test_every_shipped_tts_provider_is_served_by_a_plugin(self):
        """Same closure for TTS: every provider the shipped tts_models
        configure must be declared via provides_tts in some manifest."""
        from agent_system.config.settings import load_settings
        used = {m.provider for m in
                (load_settings().llm_system.tts_models or {}).values()}
        assert used, "no tts_models in the shipped config — test went blind"
        missing = used - registry.known_tts_providers()
        assert not missing, (
            f"TTS providers configured but served by no plugin: {sorted(missing)}")

    def test_config_validation_rejects_an_unknown_tts_provider(self):
        from pydantic import ValidationError
        from agent_system.config.models import LLMSystemConfig, TTSModelConfig
        with pytest.raises(ValidationError, match="provides_tts"):
            LLMSystemConfig(tts_models={"t": TTSModelConfig(
                provider="gemini_tst", model="x")})

    def test_batch_backends_are_declared_for_all_mapped_providers(self):
        """The resolver maps batch_provider to gemini/openai/anthropic; each
        needs a provides_batch declaration or batch jobs lose their backend."""
        registry._scan_manifests()
        declared = set(registry._owners["provides_batch"])
        assert {"gemini", "openai", "anthropic"} <= declared, (
            f"batch backends missing: only {sorted(declared)} declared")

    def test_manifests_agree_with_the_provider_dicts(self):
        """A manifest may promise a name the entrypoint does not export —
        that surfaces only at first use in production, so the suite checks
        every plugin's promise against registry.SEAMS here.

        Reads the production table, not a copy of it: a copy is one more place
        to forget the fifth seam in, and forgetting it there looks exactly
        like a seam that passes.
        """
        import importlib
        registry._scan_manifests()
        checked = 0
        for key, (attr, _label) in registry.SEAMS.items():
            for name, dir_name in sorted(registry._owners[key].items()):
                checked += 1
                module = importlib.import_module(f"plugins.{dir_name}.provider")
                assert name in (getattr(module, attr, None) or {}), (
                    f"{dir_name}/plugin.toml declares {key} '{name}' but "
                    f"{attr} does not export it")
        # An empty table would make every assertion above unreachable, and the
        # test would report success for having looked at nothing.
        assert checked, "no seam was checked — is registry.SEAMS empty?"

    def test_no_plugin_exports_a_name_its_manifest_never_declared(self):
        """The other direction: an undeclared export is invisible to config
        validation (which reads the manifests) but used to be taken over by
        the loader anyway — so which plugin owned a contested name depended
        on load order, not on the manifest."""
        import importlib
        registry._scan_manifests()
        checked = 0
        for key, (attr, _label) in registry.SEAMS.items():
            owners = registry._owners[key]
            for dir_name in sorted(set(owners.values())):
                checked += 1
                module = importlib.import_module(f"plugins.{dir_name}.provider")
                declared = {n for n, d in owners.items() if d == dir_name}
                exported = set(getattr(module, attr, None) or {})
                assert exported <= declared, (
                    f"{dir_name}/provider.py exports {sorted(exported - declared)} "
                    f"in {attr} without declaring it in plugin.toml — the "
                    f"loader ignores those, config validation never sees them")
        assert checked, "no seam was checked — is registry.SEAMS empty?"

    @staticmethod
    def _manifests():
        """Every plugins manifest as (dir name, [plugin] table).

        Read here instead of taken from the registry's scan: the scan keeps
        only what it claims, and both tests below are about what a manifest
        SAYS — including the manifests the scan skips. A plugin.toml that
        does not parse raises out of here on purpose: production only warns
        (registry._read_manifest), which makes such a plugin vanish quietly.
        """
        import tomllib
        out = []
        for plugin_dir in sorted(registry._plugins_root().iterdir()):
            toml_path = plugin_dir / "plugin.toml"
            if not toml_path.is_file():
                continue
            with toml_path.open("rb") as fh:
                out.append((plugin_dir.name, tomllib.load(fh).get("plugin", {})))
        return out

    def test_a_rescan_fills_the_same_dicts_instead_of_new_ones(self):
        """The state is filled IN PLACE, and that is not a style question.

        Whoever holds one of these dicts across a rescan -- a test, or a table
        of references like the one this file used to carry -- would otherwise
        be reading the previous scan's state while everything looks fine. That
        is exactly why the table lives in production now: rebinding is the
        version of this bug that reads as correct in a diff.
        """
        registry.reset_for_tests()
        registry._scan_manifests()
        held = {key: registry._owners[key] for key in registry.SEAMS}

        registry.reset_for_tests()
        registry._scan_manifests()

        for key in registry.SEAMS:
            assert registry._owners[key] is held[key], f"{key}: rebound, not refilled"
        assert held["provides"], "the held dict is empty after the rescan"

    def test_a_manifest_that_throws_mid_scan_leaves_nothing_behind(self):
        """The scan publishes at the END, so a failure leaves no half-scan.

        It used to get that for free by rebinding the globals. Filling them in
        place instead — which is what makes a table of references safe — would
        have kept whatever was claimed before the failure, and the NEXT scan
        would then re-claim those names and warn that each plugin collides
        with itself. A self-collision warning reads like a real duplicate
        manifest and sends the reader somewhere there is nothing to find.
        """
        registry.reset_for_tests()

        seen = []

        def _explode(plugin_dir):
            # One good manifest first, so there IS something half-claimed and
            # one half-read endpoint default to leave behind, then a manifest
            # whose `provides` is not a list: _read_manifest hands that over
            # unchecked and claim() iterates it.
            seen.append(plugin_dir)
            if len(seen) == 1:
                return {"type": ["llm-provider"], "provides": ["ghost"],
                        "default_base_url": {"ghost": "https://ghost.invalid"}}
            return {"type": ["llm-provider"], "provides": 5}

        with patch.object(registry, "_read_manifest", _explode):
            with pytest.raises(TypeError):
                registry._scan_manifests()

        assert not registry._scanned
        for key in registry.SEAMS:
            assert registry._owners[key] == {}, f"{key}: a half-scan survived"
        # Same rule for the endpoint defaults: they are read per manifest in
        # the same loop, so publishing them early leaves the same half-state.
        assert registry._default_base_urls == {}, "half the base urls survived"

        # And the real scan afterwards is clean — no name claimed twice.
        registry._scan_manifests()
        assert registry._owners["provides"], "the scan after the failure found nothing"

    def test_every_seam_a_manifest_declares_is_one_the_registry_dispatches(self):
        """A provides_* key nothing dispatches is dead: the plugin looks
        wired and serves nothing.

        Only the NAME of the key is measured here: whether the plugin that
        declares it is scanned at all is the test below.
        """
        declared = {k for _, meta in self._manifests()
                    for k in meta if k.startswith("provides")}
        known = set(registry.SEAMS)
        assert declared <= known, (
            f"manifest seams the registry does not dispatch: "
            f"{sorted(declared - known)} — add a row to registry.SEAMS (and "
            f"its get_/build_/known_ trio), or drop the dead key")

    def test_a_reset_drops_every_seam_it_knows_about(self):
        """Each get_* reads its factory cache BEFORE it rescans, so an entry
        that survives a reset short-circuits every later scan — and a test
        that thought it had a clean registry does not.

        Driven from SEAMS rather than naming one seam: the reset forgot the
        decisions seam for a day and stayed green, because the only test was
        about the seams that were already there.
        """
        registry.get_decisions_provider("openrouter_decisions")
        registry.get_provider("openai_httpx")
        loaded = {key for key in registry.SEAMS if registry._exports[key]}
        assert loaded, "nothing was loaded, so nothing is being reset"

        registry.reset_for_tests()

        assert not registry._scanned, "the scan flag survived — a later scan is skipped"
        for key in registry.SEAMS:
            assert registry._owners[key] == {}, f"{key}: owners survived"
            assert registry._exports[key] == {}, f"{key}: factories survived"
        assert registry._default_base_urls == {}

    def test_a_manifest_that_declares_a_seam_is_scanned_at_all(self):
        """_scan_manifests skips every plugin without type = ["llm-provider"],
        so a provides_* key on any other manifest is dead: nothing claims the
        name, and config load then fails with "unknown provider ... no plugin
        declares it" — which sends the reader to the config file, where the
        mistake is not.

        Written after nearly making it: llm_decisions declared
        provides_decisions while its type still said ["library"].
        """
        wrong = sorted(
            f"{name} (type={meta.get('type')})"
            for name, meta in self._manifests()
            if any(k.startswith("provides") for k in meta)
            and "llm-provider" not in (meta.get("type") or []))
        assert not wrong, (
            f"manifests declaring a seam the registry never scans: {wrong} — "
            f"add \"llm-provider\" to their type, or drop the dead key")

    def test_the_loader_ignores_an_undeclared_export(self):
        """Mechanism behind the test above, measured directly: a factory the
        manifest does not declare must not end up in the dispatch table."""
        module = types.SimpleNamespace(
            PROVIDERS={"openai_httpx": lambda cfg, ssl_verify=None: "declared",
                       "smuggled": lambda cfg, ssl_verify=None: "undeclared"},
        )
        registry._scan_manifests()
        with patch.object(registry.importlib, "import_module", return_value=module):
            registry._load_plugin("llm_openai_compat")
        # Anchor first: without it a _load_plugin that takes over NOTHING
        # passes this test, since it only asserts an absence.
        chat = registry._exports["provides"]
        assert chat["openai_httpx"](None) == "declared"
        assert "smuggled" not in chat


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

        # The manager receives a FACTORY per provider (the client is built on
        # first use, see BatchQueueManager.register_batch_client_factory);
        # resolving it here is what the queue manager does in _client_for.
        registered = {}
        manager = SimpleNamespace(
            register_batch_client_factory=lambda name, factory: registered.__setitem__(name, factory))
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
        assert set(registered) == {"anthropic", "openai"}, (
            "the registry lookup in _register_batch_clients no longer reaches "
            "the queue manager — batch models silently lose their backend")
        assert type(registered["anthropic"]()).__name__ == "AnthropicBatchClient"
        assert type(registered["openai"]()).__name__ == "OpenAIBatchClient"

    def test_missing_key_resolves_to_no_client(self, monkeypatch):
        """Registration no longer looks at the key (that would mean building
        the client at startup); the factory answers None on first use and the
        queue manager forgets the provider then."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        registered = self._run({
            "anthropic": LLMModelConfig(provider="batch", batch_provider="anthropic",
                                        model="claude-x"),
        })
        assert set(registered) == {"anthropic"}
        assert registered["anthropic"]() is None

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

    def test_the_models_own_request_timeout_is_its_read_timeout(self):
        """deepseek-pro set request_timeout: 480 and ran with the system's 180."""
        from agent_system.llm.factory import resolve_llm_config_for_agent
        from agent_system.config.models import AgentConfig
        config = self._config()
        config.llm_system.models["m"] = config.llm_system.models["m"].model_copy(
            update={"request_timeout": 480})
        config.llm_system.models["m"].model_fields_set.add("request_timeout")
        resolved = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="p"))
        assert resolved.spec.httpx_timeouts.read == 480.0
        assert config.llm_system.httpx_timeouts.read == 123.0, "the system default was edited"
        # Unset, the system default still applies.
        assert resolve_llm_config_for_agent(self._config(), AgentConfig(llm_profile="p")).spec.httpx_timeouts.read == 123.0

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


class TestBatchModelsFailAtConfigLoad:
    """`provider: batch` used to skip the typo guard entirely.

    The validator exists so a wrong `provider:` costs the config load, not
    the first agent build. A batch entry is the case where that matters most:
    its real provider hides in `batch_provider`, so a typo there produced a
    config that loaded fine and an agent that died at build time with a
    message about a field the operator hadn't touched.
    """

    def _cfg(self, **kwargs):
        from agent_system.config.models import LLMModelConfig, LLMSystemConfig
        return LLMSystemConfig(models={"m": LLMModelConfig(
            provider="batch", model="x", **kwargs)})

    def test_batch_without_batch_provider_is_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="no batch_provider"):
            self._cfg()

    def test_unknown_batch_provider_is_rejected_and_lists_the_known_ones(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as exc:
            self._cfg(batch_provider="gemnii")
        msg = str(exc.value)
        assert "gemnii" in msg and "anthropic" in msg

    def test_a_correct_batch_model_still_loads(self):
        """Counter-check: the guard rejects typos, not batch models."""
        assert self._cfg(batch_provider="gemini").models["m"].batch_provider == "gemini"

    def test_a_typo_in_a_batch_providers_key_is_rejected(self):
        """The KEYS of llm_system.batch.providers are the same vocabulary —
        and were the only part of it nothing checked. A misspelled key was
        accepted and ignored, so the provider silently ran on hardcoded
        defaults instead of its configured poll interval and
        cancel_on_startup."""
        from pydantic import ValidationError
        from agent_system.config.models import (
            BatchProviderConfig, BatchSystemConfig, LLMSystemConfig)

        with pytest.raises(ValidationError, match="openai_htpx"):
            LLMSystemConfig(batch=BatchSystemConfig(
                providers={"openai_htpx": BatchProviderConfig()}))

    def test_correct_batch_providers_keys_load(self):
        from agent_system.config.models import (
            BatchProviderConfig, BatchSystemConfig, LLMSystemConfig)

        cfg = LLMSystemConfig(batch=BatchSystemConfig(
            providers={name: BatchProviderConfig()
                       for name in registry.known_batch_providers()}))
        assert set(cfg.batch.providers) == set(registry.known_batch_providers())

    def test_every_batch_provider_resolves_to_a_declared_client(self):
        """The batch vocabulary and the client each entry maps to both come
        from the manifests — a mapping pointing at a provider nobody declares
        would break at the first build."""
        for batch_provider in registry.known_batch_providers():
            target = registry.batch_client_provider(batch_provider)
            assert target in registry.known_providers(), (
                f"batch_provider '{batch_provider}' maps to provider "
                f"'{target}', which no plugin declares")

    def test_an_undeclared_batch_provider_is_a_named_error(self):
        with pytest.raises(registry.ProviderNotFoundError) as exc:
            registry.batch_client_provider("gemnii")
        assert "gemnii" in str(exc.value) and "provides_batch" in str(exc.value)

    def test_a_batch_provider_pairs_with_the_client_of_the_same_name(self):
        """Every LLM client brings its OWN batch backend under its own name —
        there is no mapping table anywhere. The core used to translate
        `openai` into `openai_httpx`, which is how a provider list ends up in
        core code again.
        """
        registry._scan_manifests()
        for name in registry.known_batch_providers():
            assert registry.batch_client_provider(name) == name

    def test_both_openai_clients_have_their_own_batch_backend(self):
        """The SDK client and the httpx client are separate providers, so
        each declares its own batch backend (they share the /v1/batches
        implementation through the registry, not through a rename)."""
        declared = registry.known_batch_providers()
        assert {"openai", "openai_httpx"} <= declared, (
            f"an OpenAI client lost its batch backend: {sorted(declared)}")
        from agent_system.config.models import LLMModelConfig
        cfg = LLMModelConfig(provider="openai_httpx", model="m", api_key="sk-t")
        built = {n: type(registry.get_batch_backend(n)(cfg)).__name__
                 for n in ("openai", "openai_httpx")}
        assert built == {"openai": "OpenAIBatchClient",
                         "openai_httpx": "OpenAIBatchClient"}, built

    #: Pydantic FIELD declarations whose DEFAULT VALUE legitimately names
    #: something provider-shaped: a config default has to name a value, and
    #: the cache marker style names a WIRE FORMAT ("the OpenAI-style
    #: breakpoint field", "Anthropic-style cache_control") that collides with
    #: provider names only by coincidence — a model of any provider can be
    #: told to use either.
    #:
    #: Only the value is exempt, never the annotation: exempting the whole
    #: statement would let `provider: Literal["openai", "gemini", ...]` back
    #: in — the very shape this test was written against (measured: it did
    #: pass through until the exemption was narrowed to node.value).
    #: Every entry here was verified to actually excuse something; a dead
    #: exemption is a future hole (`ollama_mode` would have covered a later
    #: `"openai"` in its Literal).
    EXEMPT_FIELD_DEFAULTS = {"provider"}
    #: Fields whose Literal spells out WIRE FORMATS that happen to share a
    #: name with a provider: `prompt_cache_marker_style` selects the marker
    #: field ("OpenAI-style prompt_cache_breakpoint" vs. "Anthropic-style
    #: cache_control"), and a model of ANY provider can be told to use
    #: either. This is the only annotation-level exemption — `provider`
    #: deliberately does NOT get one, so re-adding its old
    #: `Literal["openai", "gemini", ...]` fails here.
    EXEMPT_FIELD_ANNOTATIONS = {"prompt_cache_marker_style"}
    #: Module constants that name wire formats, not providers.
    EXEMPT_CONSTANTS = {"MARKER_STYLE_OPENAI", "MARKER_STYLE_ANTHROPIC"}

    def test_the_core_names_no_providers(self):
        """The whole point of the plugin seam: which providers exist, which
        client a batch model uses, and which endpoint one defaults to are
        manifest facts. They keep coming back as if-chains, Literals and
        dicts in core code — the batch mapping did twice. This fails when the
        next one appears.

        Two shapes count: a provider name in a DECLARATION (Literal, dict,
        constant) is core-owned vocabulary, and one anywhere else is dispatch
        logic (`provider == "gemini"`). Exemptions are by NAME, not by line —
        a line number silently stops matching the thing it excused.
        """
        import ast

        names = (set(registry.known_providers())
                 | set(registry.known_batch_providers())
                 | set(registry.known_tts_providers()))
        names.discard("batch")  # the resolver's own pseudo-provider
        assert len(names) >= 5, "provider scan came up empty — check would be vacuous"

        core = REPO_ROOT / "src" / "agent_system"
        # The whole core, not three hand-picked globs: the first version of
        # this test scanned llm/ + config/models.py and left a real dispatch
        # (`native_providers = {"gemini", "google"}`) standing in utils/.
        skip = {
            # Package version report, not provider dispatch: it reads
            # `anthropic.__version__` / `openai.__version__` from the INSTALLED
            # DISTRIBUTIONS, whose names happen to match provider names.
            core / "app.py",
        }
        files = [p for p in sorted(core.rglob("*.py")) if p not in skip]
        assert len(files) > 50, "core modules not found — check would be vacuous"

        offenders = []
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            exempt_nodes = set()

            def exempt(part):
                if part is not None:
                    exempt_nodes.update(id(c) for c in ast.walk(part))

            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    # The VALUE only — exempting the whole statement would
                    # excuse the annotation, i.e. a Literal listing providers.
                    if node.target.id in self.EXEMPT_FIELD_DEFAULTS:
                        exempt(node.value)
                    if node.target.id in self.EXEMPT_FIELD_ANNOTATIONS:
                        exempt(node.annotation)
                        exempt(node.value)
                elif isinstance(node, ast.Assign) and any(
                        t.id in self.EXEMPT_CONSTANTS
                        for t in node.targets if isinstance(t, ast.Name)):
                    exempt(node.value)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and node.value in names
                        and id(node) not in exempt_nodes):
                    offenders.append(
                        f"{path.name}:{node.lineno}: {node.value!r}")
        assert not offenders, (
            "core code names plugin providers — declare it in the plugin's "
            f"manifest and read it via the registry instead: {offenders}")


class TestEmptyChain:
    def test_an_empty_llm_profile_list_is_rejected(self):
        """`llm_profile: []` resolved to the string "normal" — a profile the
        operator never wrote, so the error arrived later and pointed at a
        name that appears nowhere in their config."""
        from pydantic import ValidationError
        from agent_system.config.models import AgentConfig
        with pytest.raises(ValidationError, match="not a chain"):
            AgentConfig(llm_profile=[])

    def test_a_normal_chain_still_loads(self):
        from agent_system.config.models import AgentConfig
        assert AgentConfig(llm_profile=["a", "b"]).default_llm_profile == "a"


class TestProviderRoutingIsDecoupled:
    """The merged routing dict is only shallow-fresh.

    `model_copy(update=..., deep=True)` applies update values AS-IS, so
    `{**system_defaults, **per_model}` handed the spec the system config's
    OWN nested objects — an in-place edit in any factory would travel back
    into the process-wide config.
    """

    def _resolved(self):
        from agent_system.llm.factory import resolve_llm_config_for_agent
        from agent_system.config.models import (
            AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile,
            LLMSystemConfig,
        )
        config = AgentSystemConfig(llm_system=LLMSystemConfig(
            openrouter_routing={"order": ["provider-a"], "sort": "price"},
            profiles={"p": LLMProfile(model_ref="m")},
            models={"m": LLMModelConfig(
                provider="openai_responses", model="x",
                base_url="https://openrouter.ai/api/v1",
                provider_routing={"sort": "throughput"})},
        ))
        return config, resolve_llm_config_for_agent(
            config, AgentConfig(llm_profile="p")).spec

    def test_the_merge_actually_happened(self):
        """Anchor: without the merge there is nothing to alias, and the
        decoupling assert below would pass on an empty dict."""
        config, spec = self._resolved()
        assert spec.provider_routing == {"order": ["provider-a"],
                                         "sort": "throughput"}

    def test_nested_values_are_not_the_system_configs_objects(self):
        config, spec = self._resolved()
        system_order = config.llm_system.openrouter_routing["order"]
        assert spec.provider_routing["order"] is not system_order
        spec.provider_routing["order"].append("__leak__")
        assert system_order == ["provider-a"], (
            "editing the spec's routing changed the system config")


class TestSeam:
    def test_conftest_installed_the_fake_and_kept_the_original(self):
        """If this fails, either bootstrap opens sockets in tests again or
        real-path tests silently measure the fake."""
        assert hasattr(registry, "_orig_build_client"), (
            "conftest no longer preserves the original build_client")
        assert registry.build_client is not registry._orig_build_client, (
            "the fake is not installed — bootstrap builds real clients in tests")
