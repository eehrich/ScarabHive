"""The Runtime is the one place a config becomes running servers.

What this pins is the seam the lazy start needs later: a server can be
DESCRIBED without being built, and building it happens in exactly one place --
with the registration in both registries and the agent post-processing that
used to sit inline in bootstrap_servers.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    AgentMetadata, MCPConfig, PluginsConfig,
)
from agent_system.plugins.mcp_adapter import plugin_mcp_registry
from agent_system.runtime import Runtime, ServerDecl

REPO = Path(__file__).resolve().parents[2]


def _config(servers: dict[str, MCPConfig], plugin_dirs: list[str] | None = None) -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(
            plugin_dirs=plugin_dirs or [str(REPO / "src" / "plugins")],
            servers=servers,
        ),
    )


def _agent_server(**kwargs) -> MCPConfig:
    return MCPConfig(type="basic_agent", enabled=True,
                     agent_config=AgentConfig(llm_profile="normal"), **kwargs)


def test_declaring_does_not_build():
    runtime = Runtime(_config({"probe": _agent_server()}))

    decl = runtime.describe("probe")

    assert isinstance(decl, ServerDecl)
    assert decl.type == "basic_agent"
    assert decl.mcp_config.agent_config.llm_profile == "normal"
    assert runtime.registry._servers == {}, "declaring a server already built it"


def test_materialize_builds_once_and_registers_in_both_registries():
    runtime = Runtime(_config({"probe_once": _agent_server()}))

    first = runtime.materialize("probe_once")
    second = runtime.materialize("probe_once")

    assert first is second, "the second call built a new instance"
    assert runtime.registry.get("probe_once") is first
    assert "probe_once" in plugin_mcp_registry.plugin_servers
    assert first.registry is runtime.registry, "the agent did not get the shared registry"


def test_materialize_applies_the_instance_config():
    metadata = AgentMetadata(author="probe", version="9.9", visibility="both")
    servers = {"probe_meta": _agent_server(
        metadata=metadata,
        description="what this instance is",
        self_tool_descriptions={"probe_meta_execute_task": "run it"},
    )}
    runtime = Runtime(_config(servers))

    agent = runtime.materialize("probe_meta")

    assert agent._description == "what this instance is"
    assert agent._metadata["version"] == "9.9"
    assert agent._mcp_public is True and agent._mcp_tool_visible is True
    assert agent._self_tool_descriptions == {"probe_meta_execute_task": "run it"}


def test_visibility_defaults_to_private():
    """Secure by default: an agent nobody made visible is neither in the UI
    list nor a tool."""
    runtime = Runtime(_config({"probe_hidden": _agent_server()}))

    agent = runtime.materialize("probe_hidden")

    assert agent._mcp_public is False
    assert agent._mcp_tool_visible is False


def test_the_manifest_supplies_the_visibility_when_the_instance_does_not(tmp_path):
    """Priority: instance metadata first, then the plugin manifest, then
    private. No shipped manifest declares one today, so the middle rung needs
    a plugin of its own to be measured at all."""
    root = tmp_path / "plugins"
    plugin = root / "visible_probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        '[plugin]\nname = "visible_probe"\nvisibility = "ui"\n', encoding="utf-8")
    (plugin / "plugin.py").write_text(
        "from agent_system.plugins.factory_utils import make_agent_plugin_factory\n"
        "from agent_system.servers.agent.server import Agent\n"
        "PLUGIN_FACTORY = make_agent_plugin_factory(Agent)\n",
        encoding="utf-8")

    config = _config({"probe_manifest": MCPConfig(
        type="visible_probe", enabled=True,
        agent_config=AgentConfig(llm_profile="normal"))},
        plugin_dirs=[str(root)])
    runtime = Runtime(config)

    assert runtime.describe("probe_manifest").visibility == "ui"
    agent = runtime.materialize("probe_manifest")
    assert agent._mcp_public is True
    assert agent._mcp_tool_visible is False


def test_the_direct_agent_type_stays_out_of_the_plugin_registry():
    """Asymmetry kept from bootstrap on purpose: the ``type: agent`` branch
    registered its agent in the MCPRegistry and did nothing else -- no plugin
    registry, no metadata, no description, no visibility. No shipped config
    uses the type, so both readings are equivalent today; this pins the one
    that keeps behaviour identical rather than the one that looks tidier."""
    runtime = Runtime(_config({"probe_direct": MCPConfig(
        type="agent", enabled=True,
        agent_config=AgentConfig(llm_profile="normal"),
        description="would be applied for a plugin agent",
        metadata=AgentMetadata(visibility="both"))}))

    agent = runtime.materialize("probe_direct")

    assert agent.registry is runtime.registry, "the shared registry it always got"
    assert "probe_direct" not in plugin_mcp_registry.plugin_servers
    assert getattr(agent, "_description", None) is None, "the direct branch applied no description"
    assert agent._mcp_public is False, "visibility 'both' was applied although the branch never did"


def test_a_disabled_server_is_not_declared():
    runtime = Runtime(_config({"probe_off": MCPConfig(
        type="basic_agent", enabled=False,
        agent_config=AgentConfig(llm_profile="normal"))}))

    assert runtime.describe("probe_off") is None
    with pytest.raises(KeyError):
        runtime.materialize("probe_off")


def test_an_unknown_type_is_reported_and_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_system.runtime"):
        runtime = Runtime(_config({"probe_unknown": MCPConfig(
            type="no_such_plugin", enabled=True)}))

    assert runtime.describe("probe_unknown") is None
    assert "no_such_plugin" in caplog.text


def test_one_broken_server_does_not_stop_the_others(tmp_path, caplog):
    """The start-up error policy: log and carry on. One plugin that explodes
    must not cost the process every other server."""
    root = tmp_path / "plugins"
    broken = root / "broken_probe"
    broken.mkdir(parents=True)
    (broken / "plugin.toml").write_text('[plugin]\nname = "broken_probe"\n', encoding="utf-8")
    (broken / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8")

    assert "test" not in str(Path.cwd()), \
        "fixture: under a working directory containing 'test' the policy re-raises"

    config = _config(
        {"probe_broken": MCPConfig(type="broken_probe", enabled=True),
         "probe_good": _agent_server()},
        plugin_dirs=[str(REPO / "src" / "plugins"), str(root)])

    with caplog.at_level(logging.ERROR, logger="agent_system.runtime"):
        runtime = Runtime(config).start()

    assert "probe_broken" not in runtime.registry.list()
    assert runtime.registry.get("probe_good") is not None, "a later server was skipped as well"
    assert "boom" in caplog.text


def test_under_a_test_directory_a_broken_server_raises(tmp_path, monkeypatch):
    """The other half of that policy, and the reason it is not silent: in a
    test tree a swallowed failure is a green lie."""
    root = tmp_path / "plugins"
    broken = root / "raise_probe"
    broken.mkdir(parents=True)
    (broken / "plugin.toml").write_text('[plugin]\nname = "raise_probe"\n', encoding="utf-8")
    (broken / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8")

    monkeypatch.setattr("agent_system.runtime.Path.cwd", lambda: Path("/somewhere/tests/here"))

    config = _config({"probe_broken": MCPConfig(type="raise_probe", enabled=True)},
                     plugin_dirs=[str(root)])

    with pytest.raises(RuntimeError, match="boom"):
        Runtime(config).start()


def test_materialize_injects_the_session_service():
    """An agent built later (lazily, or on demand) must not miss what every
    agent built at start got -- so the injection belongs in the build path,
    not only in the walk that runs once after bootstrap."""
    session_service = object()
    runtime = Runtime(_config({"probe_session": _agent_server()}),
                      session_service=session_service)

    agent = runtime.materialize("probe_session")

    assert agent._session_service is session_service
