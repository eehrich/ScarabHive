"""Plugins must build LLM clients through the full path.

Four plugins resolved their config correctly and then hand-listed the
`make_llm` arguments, which silently dropped everything the list did not
mention: `thinking_level`, `max_tokens`, `safety_settings`, `service_tier`,
`provider_routing`, `parallel_tool_calls` — and batch wrapping.

For the profiles those plugins are configured with today only
`parallel_tool_calls` was lost, and its value equals the default, so nothing
misbehaved. The trap was the next config line: `basic_agent` picks its profile
from a RUNTIME tool argument, so pointing it at an OpenRouter profile was one
call away from losing the tier, the routing and the token cap.

This pins the property rather than the call shape: what the client ends up
carrying has to match what the profile resolves to.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.config.models import AgentConfig
from agent_system.config.settings import load_settings
from agent_system.llm.factory import (
    create_llm_from_profile,
    resolve_llm_config_for_agent,
)

#: Fields that a hand-written make_llm() call sheds. Each one changes what the
#: provider is asked to do, so losing it is never cosmetic.
CARRIED = ("thinking_level", "max_tokens", "service_tier", "provider_routing",
           "safety_settings", "parallel_tool_calls", "temperature")


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture
def real_clients(monkeypatch):
    """conftest.py swaps `make_llm` for a fake so bootstrap opens no sockets.

    That fake carries none of the fields under test, so checking "the client
    kept its profile" against it would compare against a stub and pass for the
    wrong reason - or, as here, fail for one. Put the real factory back for the
    duration of the test; constructing a client opens no connection.
    """
    from agent_system.llm import clients

    original = getattr(clients, "_orig_make_llm", None)
    if original is None:
        return
    monkeypatch.setattr(clients, "make_llm", original)


#: The two OpenAI-compatible client families. They do NOT share code: the
#: Responses client builds its own payload and has its own 429 tier-drop, so a
#: field that arrives in one can be missing in the other. `turbo` moved from
#: the first to the second, which is exactly why both are exercised here.
CLIENT_FAMILIES = ("openai_httpx", "openai_responses")


def _synthetic(provider="openai_httpx", openrouter_routing=None, **model_fields):
    """A config built here, not searched for in config/llm.yaml.

    Searching production config for "a profile that sets enough fields" makes
    the behavioural assertion disappear into a skip the day that profile
    changes — silently, with CI still green.
    """
    from agent_system.config.models import (
        AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    )

    return AgentSystemConfig(llm_system=LLMSystemConfig(
        openrouter_routing=openrouter_routing,
        profiles={"p": LLMProfile(model_ref="m")},
        models={"m": LLMModelConfig(
            provider=provider, model="x/y", api_key="sk-test",
            base_url="https://openrouter.ai/api/v1", **model_fields)},
    ))


FIELDS = dict(thinking_level="high", max_tokens=4242, service_tier="flex",
              provider_routing={"order": ["openai"]},
              safety_settings={"HARM_CATEGORY_HARASSMENT": "BLOCK_NONE"},
              parallel_tool_calls=False, temperature=0.25)


class TestTheFullPathCarriesTheProfile:
    @pytest.mark.parametrize("provider", CLIENT_FAMILIES)
    def test_a_client_built_from_a_profile_keeps_its_fields(self, real_clients, provider):
        config = _synthetic(provider=provider, **FIELDS)
        expected = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="p"))
        assert all(expected.get(f) is not None for f in CARRIED),             "the fixture stopped setting the fields under test"

        client = create_llm_from_profile(config, "p")

        lost = [f for f in CARRIED if getattr(client, f, None) != expected[f]]
        assert not lost, f"{provider}: these never reached the client: {lost}"

    def test_the_system_wide_routing_default_reaches_the_client(self, real_clients):
        """`_synthetic` without `openrouter_routing` only exercises the branch
        that returns the model's own value — the merge that matters in
        production stayed dead."""
        config = _synthetic(openrouter_routing={"sort": "price"})

        client = create_llm_from_profile(config, "p")

        assert getattr(client, "provider_routing", None) == {"sort": "price"}

    def test_a_batch_model_comes_back_wrapped(self, real_clients, monkeypatch):
        """The module docstring claims batch wrapping as one of the things the
        hand-rolled call lost. Without this, replacing the whole wrapping block
        in factory.py with `pass` stayed green — batch models would quietly run
        synchronously and give up their 50% discount.

        A queue manager has to exist for the wrapping to happen at all; the
        factory falls back to sync without one and says so in a warning. That
        fallback is correct behaviour, so it is asserted too — otherwise this
        test could pass merely because no manager was registered.
        """
        from types import SimpleNamespace

        from agent_system.llm import factory
        from agent_system.llm.batch.batch_client import BatchLLMClient
        from agent_system.config.models import BatchSystemConfig

        config = _synthetic(provider="batch", batch_provider="openai")
        config.llm_system.batch = BatchSystemConfig()

        # Without a manager: sync fallback, not a crash.
        monkeypatch.setattr(factory, "get_batch_queue_manager", lambda: None)
        assert not isinstance(create_llm_from_profile(config, "p"), BatchLLMClient)

        # With one: wrapped.
        monkeypatch.setattr(factory, "get_batch_queue_manager",
                            lambda: SimpleNamespace(name="stub"))
        client = create_llm_from_profile(config, "p")
        assert isinstance(client, BatchLLMClient),             f"batch model came back as {type(client).__name__}"


class TestNoPluginHandRollsTheArguments:
    """Anti-drift: the next plugin that needs a client must not copy the old
    pattern back in. `make_llm` itself stays legitimate — clients.py and the
    factory are its home."""

    PLUGINS = ("basic_agent", "context_summarizer", "agent_continuation",
               "llm_router")

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_the_plugin_does_not_call_make_llm(self, plugin):
        """Parsed, not grepped — but only one step deep.

        Catches the realistic regression: a direct `make_llm(...)` or an
        aliased `import make_llm as _mk` + `_mk(...)`. It does NOT catch
        `getattr(clients, "make_llm")(...)`, a variable alias, or a plugin that
        constructs `OpenAIResponsesClient(...)` outright — the last of which is
        the same defect and worse. Do not read a green run here as "no plugin
        hand-rolls a client"; the behavioural tests below are what actually
        establish that, and they cover three of the four plugins.
        """
        import ast

        root = Path(__file__).parents[2] / "src" / "plugins" / plugin
        assert root.is_dir(), f"{plugin} moved — this check would be vacuous"

        modules = [p for p in root.rglob("*.py") if "tests" not in p.parts]
        assert modules, f"{plugin} has no modules — this check would be vacuous"

        offenders = []
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            aliases = {
                alias.asname or alias.name
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                for alias in node.names if alias.name == "make_llm"
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = (target.id if isinstance(target, ast.Name)
                        else target.attr if isinstance(target, ast.Attribute) else None)
                if name in aliases or name == "make_llm":
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")

        assert not offenders, (
            f"{plugin} builds a client by hand again — use "
            f"create_llm_from_profile, which forwards every resolved field: "
            f"{offenders}")


class TestThePluginsThemselvesBuildTheRightModel:
    """The check that would have caught the real defect.

    The property test above compares the factory against itself and executes
    not one line of the four plugins — measured at 0% coverage. It therefore
    could not see that `context_summarizer` built its client with
    `AgentConfig(default_llm_profile=X)`, where `default_llm_profile` is a
    read-only PROPERTY: pydantic dropped the keyword without a word and the
    plugin quietly ran on the `normal` default instead of its configured
    profile.

    So this drives the plugin's own builder and asks the only question that
    matters: does the client speak the model the CONFIGURED profile names?
    """

    @pytest.fixture
    def continuation(self, real_clients):
        """agent_continuation builds its evaluator the same way."""
        from types import SimpleNamespace

        from plugins.agent_continuation.hooks import AgentContinuationPlugin

        def build(profile: str, config):
            hook = AgentContinuationPlugin.__new__(AgentContinuationPlugin)
            hook._evaluator_llm = None
            hook._llm_profile = profile
            context = SimpleNamespace(agent=SimpleNamespace(system_config=config))
            return hook._get_evaluator_llm(context)

        return build

    @pytest.fixture
    def router(self, real_clients):
        """llm_router builds one client per profile."""
        from types import SimpleNamespace

        from agent_system.config.models import MCPConfig
        from plugins.llm_router.server import LLMRouterServer

        def build(profile: str, config):
            server = LLMRouterServer(
                "llm_router", config, MCPConfig(type="llm_router", enabled=True))
            return server._make_client(profile)

        return build

    @pytest.fixture
    def summarizer(self, real_clients):
        from types import SimpleNamespace

        from plugins.context_summarizer.server import ContextSummarizerServer

        def build(profile: str, config):
            mcp = SimpleNamespace(config={"llm_profile": profile}, hook_config={},
                                  name="context_summarizer")
            hook = ContextSummarizerServer(
                "context_summarizer", SimpleNamespace(), mcp)._hooks_impl
            context = SimpleNamespace(agent=SimpleNamespace(system_config=config),
                                      llm=None)
            return hook._get_summarizer_llm(context)

        return build

    @pytest.mark.parametrize("profile", ["turbo", "normal"])
    @pytest.mark.parametrize("plugin", ["summarizer", "continuation", "router"])
    def test_the_configured_profile_decides_the_model(
            self, config, request, plugin, profile):
        """All three builders, not just the one that had the bug.

        An earlier version drove only the summarizer; the other two plugins had
        0% coverage and a mutation swapping their profile for a hardcoded one
        stayed green.
        """
        build = request.getfixturevalue(plugin)
        expected = resolve_llm_config_for_agent(
            config, AgentConfig(llm_profile=profile))["model"]

        client = build(profile, config)

        assert client is not None, "no client was built - the test would be vacuous"
        assert getattr(client, "model", None) == expected, (
            f"{plugin}: profile {profile!r} should speak {expected!r}, "
            f"got {getattr(client, 'model', None)!r}")

    def test_two_profiles_really_differ(self, config, summarizer):
        """Counter-check: if both profiles resolved to one model, the test
        above would pass on a hardcoded profile name."""
        a = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="turbo"))["model"]
        b = resolve_llm_config_for_agent(config, AgentConfig(llm_profile="normal"))["model"]
        assert a != b, "turbo and normal resolve to the same model here"


class TestTheShippedConfigurationReachesTheSummarizer:
    """The chain the historic bug lived in, end to end.

    `config/plugins.yaml` sets `llm_profile` for the summarizer. For months
    that value was swallowed on the way to the client, and no test noticed —
    because every test built its own config. Deleting the line from
    plugins.yaml still leaves the whole suite green otherwise.
    """

    def test_the_summarizer_speaks_the_model_from_plugins_yaml(self, real_clients):
        import types

        import yaml

        from agent_system.config.models import MCPConfig
        from plugins.context_summarizer.server import ContextSummarizerServer

        shipped = yaml.safe_load(
            Path("config/plugins.yaml").read_text(encoding="utf-8")
        )["plugins"]["servers"]["context_summarizer"]
        profile = (shipped.get("config") or {}).get("llm_profile")
        assert profile, (
            "config/plugins.yaml no longer sets llm_profile for the summarizer "
            "— it falls back to the schema default, which is what the bug did")

        system_config = load_settings()
        expected = resolve_llm_config_for_agent(
            system_config, AgentConfig(llm_profile=profile))["model"]

        mcp = types.SimpleNamespace(config=shipped["config"], hook_config={},
                                    name="context_summarizer")
        hook = ContextSummarizerServer(
            "context_summarizer", types.SimpleNamespace(), mcp)._hooks_impl
        context = types.SimpleNamespace(
            agent=types.SimpleNamespace(system_config=system_config), llm=None)

        client = hook._get_summarizer_llm(context)

        assert getattr(client, "model", None) == expected, (
            f"plugins.yaml asks for {profile!r} ({expected}), "
            f"the client speaks {getattr(client, 'model', None)!r}")


class TestEveryResolvedFieldReachesMakeLlm:
    """One recorder, the WHOLE handover — not a per-field sample.

    Mutation runs showed 7 of the 15 forwarded fields plus the entire
    make_kwargs base (capabilities, httpx_timeouts, ssl_verify) survived
    removal: the per-field tests only sampled the popular fields. Recording
    the actual make_llm call and comparing the complete kwargs closes all of
    it in one place — including the nastiest survivor, `ssl_verify=False`
    silently turning into the config default.
    """

    @staticmethod
    def _record(monkeypatch):
        from agent_system.llm import clients

        calls = {}

        def recorder(provider, model, api_key, base_url, context_window,
                     ollama_mode, request_timeout, **kwargs):
            calls.update(kwargs, provider=provider, model=model)
            return object()

        monkeypatch.setattr(clients, "make_llm", recorder)
        return calls

    @staticmethod
    def _full_config():
        from agent_system.config.models import (
            AgentSystemConfig, HTTPXTimeoutConfig, LLMModelConfig, LLMProfile,
            LLMSystemConfig, ModelCapabilitiesConfig, NetworkConfig,
        )

        return AgentSystemConfig(
            network=NetworkConfig(ssl_verify=True),
            llm_system=LLMSystemConfig(
                profiles={"p": LLMProfile(model_ref="m")},
                models={"m": LLMModelConfig(
                    provider="openai_httpx", model="x/y", api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                    httpx_timeouts=HTTPXTimeoutConfig(read=99.0),
                    capabilities=ModelCapabilitiesConfig(json_mode=True),
                    include_thoughts=True, enable_prompt_caching=False,
                    thinking_budget=1234,
                    thinking_level="high", modalities=["text"],
                    max_tokens=4242, temperature=0.25,
                    safety_settings={"HARM_CATEGORY_HARASSMENT": "BLOCK_NONE"},
                    service_tier="flex", prompt_cache_key="auto",
                    prompt_cache_mode="multi_turn",
                    prompt_cache_marker_style="openai",
                    provider_routing={"order": ["openai"]},
                    reasoning_details_mode="keep_all",
                    parallel_tool_calls=False,
                )},
            ))

    def test_the_complete_kwargs_arrive(self, monkeypatch):
        calls = self._record(monkeypatch)
        config = self._full_config()

        create_llm_from_profile(config, "p", ssl_verify=False)

        expected = {
            "include_thoughts": True, "enable_prompt_caching": False,
            "thinking_budget": 1234,
            "thinking_level": "high", "modalities": ["text"],
            "max_tokens": 4242, "temperature": 0.25,
            "safety_settings": {"HARM_CATEGORY_HARASSMENT": "BLOCK_NONE"},
            "service_tier": "flex", "prompt_cache_key": "auto",
            "prompt_cache_mode": "multi_turn",
            "prompt_cache_marker_style": "openai",
            "provider_routing": {"order": ["openai"]},
            "reasoning_details_mode": "keep_all",
            "parallel_tool_calls": False,
        }
        missing = {k: v for k, v in expected.items() if calls.get(k) != v}
        assert not missing, f"these never reached make_llm: {missing}"

        assert calls["capabilities"] is not None, "capabilities dropped"
        assert calls["capabilities"].json_mode is True
        assert calls["httpx_timeouts"], "httpx_timeouts dropped"
        assert calls["httpx_timeouts"]["read"] == 99.0

    def test_an_explicit_ssl_verify_false_survives(self, monkeypatch):
        """The nastiest survivor: config says verify, the caller says don't.
        If the fallback overwrote the explicit False, TLS verification would
        be silently re-enabled — or worse, the inverse."""
        calls = self._record(monkeypatch)

        create_llm_from_profile(self._full_config(), "p", ssl_verify=False)

        assert calls["ssl_verify"] is False,             "explicit ssl_verify=False was replaced by the config default"

    def test_without_a_caller_value_the_config_decides(self, monkeypatch):
        calls = self._record(monkeypatch)

        create_llm_from_profile(self._full_config(), "p")

        assert calls["ssl_verify"] is True,             "network.ssl_verify never reached make_llm"
