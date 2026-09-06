"""The per-request walkers answer from declarations, without building.

Every walk over the registry used to build what it looked at: the UI agent
list, the tool-visibility filter that runs for every server on every single
request, the job manager. Under a lazy start the first such walk would
materialize the entire process, which is the one thing the whole design
exists to avoid -- so the questions those walkers ask have to be answerable
without an instance.

What the tests below pin is exactly that: the ANSWER stays the same, and
``_servers`` stays empty. A Runtime that was declared but never started is
the only fixture that can tell the two apart -- with everything built, a
walker that builds and a walker that does not are indistinguishable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.config.models import (
    AgentConfig, AgentMetadata, AgentSystemConfig, LLMModelConfig, LLMProfile,
    LLMSystemConfig, MCPConfig, PluginsConfig, ToolConfig,
)
from agent_system.mcp.base import MCPRegistry
from agent_system.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]

#: one agent per visibility -- all FOUR, because the flag pair has four
#: states and leaving "ui" out left (True, False) unmeasured: the mutation
#: ``mcp_public = (visibility == "both")`` stayed green, and three shipped
#: servers really are visibility "ui" (file_ops_test_agent, okf_agent,
#: mcp_external_test_agent).
VISIBILITIES = {"probe_private": "private", "probe_tool": "tool",
                "probe_both": "both", "probe_ui": "ui"}


@pytest.fixture
def declared_only() -> Runtime:
    """Three lazy agents, declared and NOT started."""
    servers = {
        name: MCPConfig(type="basic_agent", enabled=True,
                        agent_config=AgentConfig(llm_profile="normal"),
                        metadata=AgentMetadata(visibility=visibility))
        for name, visibility in VISIBILITIES.items()
    }
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(plugin_dirs=[str(REPO / "src" / "plugins")], servers=servers),
    )
    runtime = Runtime(config)

    assert runtime.registry._servers == {}, "fixture: declaring already built something"
    assert all(runtime.describe(n).lazy for n in VISIBILITIES), \
        "fixture: these have to be lazy declarations or the walk has nothing to answer from"
    return runtime


class TestTheViewAnswersWithoutAnInstance:
    def test_each_visibility_maps_to_the_flags_the_instance_would_get(self, declared_only):
        views = {n: declared_only.registry.describe(n) for n in VISIBILITIES}
        flags = {n: (v.mcp_public, v.mcp_tool_visible) for n, v in views.items()}

        # all four corners of the pair, or one of the two mappings is free
        assert flags == {
            "probe_private": (False, False),
            "probe_tool": (False, True),
            "probe_both": (True, True),
            "probe_ui": (True, False),
        }, flags
        assert all(v.is_agent and not v.built for v in views.values())
        assert declared_only.registry._servers == {}, "describing built something"

    def test_the_answer_is_the_same_one_the_built_instance_gives(self, declared_only):
        """The whole point: same answer, no instance. If these two ever drift,
        the walkers start disagreeing with what the process actually serves."""
        before = {n: declared_only.registry.describe(n) for n in VISIBILITIES}

        for name in VISIBILITIES:
            declared_only.materialize(name)
        after = {n: declared_only.registry.describe(n) for n in VISIBILITIES}

        for name in VISIBILITIES:
            assert (after[name].is_agent, after[name].mcp_public, after[name].mcp_tool_visible) == \
                   (before[name].is_agent, before[name].mcp_public, before[name].mcp_tool_visible), name
            assert after[name].built and not before[name].built

    def test_the_instance_wins_once_it_exists(self, declared_only):
        """A subclass may set its own flags in __init__, and a running process
        serves what the instance says -- not what the config said."""
        agent = declared_only.materialize("probe_private")
        agent._mcp_public = True

        assert declared_only.registry.describe("probe_private").mcp_public is True

    def test_an_unknown_name_has_no_view(self, declared_only):
        assert declared_only.registry.describe("probe_nonexistent") is None

    def test_an_unbound_registry_says_nothing(self):
        """Unbound it is the plain dict it always was, and describe() has to
        admit it knows nothing -- the walkers fall back to the instance."""
        assert MCPRegistry().describe("anything") is None


class TestOnlyALazyDeclarationMayAnswer:
    """The declaration speaks for its instance ONLY where it provably agrees.

    ``apply_to`` copies the configured visibility onto the instance only for a
    plugin factory, and returns early for anything that is not an ``Agent``.
    A non-agent plugin therefore never carries the two flags: built it reads
    (True, True) through the getattr defaults, while its declaration defaults
    to "private" -> (False, False). Answering from that declaration would drop
    file_ops, terminal and every other tool server out of every agent's tool
    list the moment the start stops building them.

    So ``view()`` answers from a declaration only for a ``lazy`` type -- the
    one case ``materialize`` guarantees to be an Agent with a factory, and
    exactly the set a lazy start defers.
    """

    @pytest.fixture
    def mixed(self) -> Runtime:
        """One lazy agent and one NON-lazy, non-agent plugin, both declared."""
        config = AgentSystemConfig(
            llm_system=LLMSystemConfig(
                models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                profiles={"normal": LLMProfile(model_ref="m")},
                default_profile="normal",
            ),
            plugins=PluginsConfig(
                plugin_dirs=[str(REPO / "src" / "plugins")],
                servers={
                    "probe_lazy": MCPConfig(
                        type="basic_agent", enabled=True,
                        agent_config=AgentConfig(llm_profile="normal"),
                        metadata=AgentMetadata(visibility="both")),
                    # no metadata at all -- the shape 40+ shipped plugins have
                    "probe_plain": MCPConfig(type="file_ops", enabled=True),
                },
            ),
        )
        runtime = Runtime(config)
        assert runtime.describe("probe_lazy").lazy, "fixture: the lazy half is not lazy"
        assert not runtime.describe("probe_plain").lazy, "fixture: the plain half IS lazy"
        assert runtime.describe("probe_plain").visibility == "private", \
            "fixture: without this default the two branches would not disagree at all"
        return runtime

    def test_a_non_lazy_declaration_refuses_to_answer(self, mixed):
        assert mixed.registry.describe("probe_plain") is None
        assert mixed.registry.describe("probe_lazy") is not None
        assert mixed.registry._servers == {}, "asking built something"

    def test_and_the_built_plugin_answers_the_opposite_of_its_declaration(self, mixed):
        """The measurement behind the rule. If these ever agree, the guard in
        ``view()`` has become redundant and belongs deleted -- not kept."""
        mixed.materialize("probe_plain")
        view = mixed.registry.describe("probe_plain")

        assert (view.mcp_public, view.mcp_tool_visible) == (True, True), \
            "a non-agent plugin carries no flags, so the getattr defaults rule"
        assert view.is_agent is False and view.built is True
        # and that is the exact opposite of what "private" would have said
        assert mixed.describe("probe_plain").visibility == "private"

    def test_the_walker_keeps_a_plain_plugin_visible(self, mixed):
        """The consequence, through the production caller: a tool server with
        no visibility metadata stays in the tool list."""
        from agent_system.servers.agent.tool_discovery import ToolDiscoveryService

        mixed.materialize("probe_plain")
        mixed.materialize("probe_lazy")
        service = ToolDiscoveryService(
            agent_name="probe_caller",
            agent_config=AgentConfig(llm_profile="normal", tools=ToolConfig()),
            mcp_integration_manager=None,
            registry=mixed.registry,
        )

        # a set: the walk's ORDER is registration order, not a contract
        assert set(service._get_registry_tools()) == {"probe_lazy", "probe_plain"}


class TestToolVisibilityDoesNotBuild:
    """``_is_tool_visible`` runs for EVERY server on EVERY request."""

    def _service(self, runtime):
        from agent_system.servers.agent.tool_discovery import ToolDiscoveryService
        return ToolDiscoveryService(
            agent_name="probe_caller",
            agent_config=AgentConfig(llm_profile="normal", tools=ToolConfig()),
            mcp_integration_manager=None,
            registry=runtime.registry,
        )

    def test_a_tool_invisible_agent_is_filtered_without_being_built(self, declared_only):
        service = self._service(declared_only)

        assert service._is_tool_visible("probe_tool") is True
        assert service._is_tool_visible("probe_both") is True
        assert service._is_tool_visible("probe_private") is False
        assert declared_only.registry._servers == {}, "the visibility check built a server"

    def test_the_built_and_the_declared_answer_agree(self, declared_only):
        service = self._service(declared_only)
        declared = {n: service._is_tool_visible(n) for n in VISIBILITIES}

        for name in VISIBILITIES:
            declared_only.materialize(name)
        built = {n: service._is_tool_visible(n) for n in VISIBILITIES}

        assert declared == built, (declared, built)

    def test_a_mock_registry_is_still_steered_through_get(self):
        """``describe()`` is asked with ``isinstance``, not ``is not None``.

        A Mock answers any call with a truthy Mock whose every attribute is
        truthy too, so ``view.mcp_tool_visible`` would be true for every
        server and this filter would quietly become "everything is visible" --
        while the ``get()`` those tests steer is never consulted again. The
        suite is full of Mock registries; the real-``MCPRegistry`` sibling
        below cannot see this, because there ``describe()`` honestly returns
        None either way.
        """
        from unittest.mock import MagicMock

        class Hidden:
            _mcp_tool_visible = False

        registry = MagicMock()
        registry.get.return_value = Hidden()
        service = self._service(type("R", (), {"registry": registry})())

        assert service._is_tool_visible("hidden") is False
        registry.get.assert_called_once_with("hidden")

    def test_an_unbound_registry_still_reads_the_instance(self):
        """The fallback path, or converting the walker would have broken every
        test and caller that hands it a bare MCPRegistry."""
        registry = MCPRegistry()

        class Hidden:
            _mcp_tool_visible = False

        class Plain:
            pass

        registry.register("hidden", Hidden())
        registry.register("plain", Plain())
        service = self._service(type("R", (), {"registry": registry})())

        assert service._is_tool_visible("hidden") is False
        assert service._is_tool_visible("plain") is True, "no flag at all means visible"
