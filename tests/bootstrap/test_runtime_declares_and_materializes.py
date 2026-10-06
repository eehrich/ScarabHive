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
    AgentMetadata, ToolServerConfig, PluginsConfig,
)
from agent_system.plugins.tool_adapter import plugin_tool_registry
from agent_system.runtime import Runtime, ServerDecl

REPO = Path(__file__).resolve().parents[2]


def _config(servers: dict[str, ToolServerConfig], plugin_dirs: list[str] | None = None) -> AgentSystemConfig:
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


def _agent_server(**kwargs) -> ToolServerConfig:
    return ToolServerConfig(type="basic_agent", enabled=True,
                     agent_config=AgentConfig(llm_profile="normal"), **kwargs)


def test_declaring_does_not_build():
    runtime = Runtime(_config({"probe": _agent_server()}))

    decl = runtime.describe("probe")

    assert isinstance(decl, ServerDecl)
    assert decl.type == "basic_agent"
    assert decl.server_config.agent_config.llm_profile == "normal"
    assert runtime.registry._servers == {}, "declaring a server already built it"


def test_materialize_builds_once_and_registers_in_both_registries():
    runtime = Runtime(_config({"probe_once": _agent_server()}))

    first = runtime.materialize("probe_once")
    second = runtime.materialize("probe_once")

    assert first is second, "the second call built a new instance"
    assert runtime.registry.get("probe_once") is first
    assert "probe_once" in plugin_tool_registry.plugin_servers
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
    assert agent._tool_public is True and agent._tool_visible is True
    assert agent._self_tool_descriptions == {"probe_meta_execute_task": "run it"}


def test_visibility_defaults_to_private():
    """Secure by default: an agent nobody made visible is neither in the UI
    list nor a tool.

    An END-STATE check, and it stays green if apply_to's visibility block
    disappears: Agent.__init__ sets both flags False itself, so for the
    private case the block is a no-op. What measures the block is the pair
    above and below -- 'both' and the manifest's 'ui'."""
    runtime = Runtime(_config({"probe_hidden": _agent_server()}))

    agent = runtime.materialize("probe_hidden")

    assert agent._tool_public is False
    assert agent._tool_visible is False


def _plugin_declaring_ui(tmp_path) -> Path:
    """A plugin whose manifest says ``visibility = "ui"`` -- no shipped one does."""
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
    return root


@pytest.mark.parametrize("metadata, shown", [
    (AgentMetadata(author="probe", min_role="admin"), "ui"),   # metadata that names no visibility
    (AgentMetadata(visibility="private"), "private"),          # one that names it
], ids=["metadata without visibility", "explicit private"])
def test_the_manifest_counts_unless_the_instance_names_a_visibility(tmp_path, metadata, shown):
    """A metadata block is not a visibility: its field defaults to private, and
    reading that default made every instance with a metadata block -- a
    min_role, an author -- private whatever its manifest said."""
    config = _config({"probe_manifest": ToolServerConfig(
        type="visible_probe", enabled=True, metadata=metadata,
        agent_config=AgentConfig(llm_profile="normal"))},
        plugin_dirs=[str(_plugin_declaring_ui(tmp_path))])

    assert Runtime(config).describe("probe_manifest").visibility == shown


def test_a_metadata_block_in_default_config_names_no_visibility(tmp_path):
    """default_config is merged with its defaults; its metadata was too, so a
    global `min_role` made visibility "private" a NAMED field on every server."""
    config = _config({"probe_manifest": ToolServerConfig(
        type="visible_probe", enabled=True, agent_config=AgentConfig(llm_profile="normal"))},
        plugin_dirs=[str(_plugin_declaring_ui(tmp_path))])
    config.plugins.default_config.metadata = AgentMetadata(min_role="user")

    assert Runtime(config).describe("probe_manifest").visibility == "ui"


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

    config = _config({"probe_manifest": ToolServerConfig(
        type="visible_probe", enabled=True,
        agent_config=AgentConfig(llm_profile="normal"))},
        plugin_dirs=[str(root)])
    runtime = Runtime(config)

    assert runtime.describe("probe_manifest").visibility == "ui"
    agent = runtime.materialize("probe_manifest")
    assert agent._tool_public is True
    assert agent._tool_visible is False


def test_the_direct_agent_type_stays_out_of_the_plugin_registry():
    """Asymmetry kept from bootstrap on purpose: the ``type: agent`` branch
    registered its agent in the ToolServerRegistry and did nothing else -- no plugin
    registry, no metadata, no description, no visibility. No shipped config
    uses the type, so both readings are equivalent today; this pins the one
    that keeps behaviour identical rather than the one that looks tidier."""
    runtime = Runtime(_config({"probe_direct": ToolServerConfig(
        type="agent", enabled=True,
        agent_config=AgentConfig(llm_profile="normal"),
        description="would be applied for a plugin agent",
        metadata=AgentMetadata(visibility="both"))}))

    agent = runtime.materialize("probe_direct")

    assert agent.registry is runtime.registry, "the shared registry it always got"
    assert "probe_direct" not in plugin_tool_registry.plugin_servers
    assert getattr(agent, "_description", None) is None, "the direct branch applied no description"
    assert agent._tool_public is False, "visibility 'both' was applied although the branch never did"


def test_a_disabled_server_is_not_declared():
    runtime = Runtime(_config({"probe_off": ToolServerConfig(
        type="basic_agent", enabled=False,
        agent_config=AgentConfig(llm_profile="normal"))}))

    assert runtime.describe("probe_off") is None
    with pytest.raises(KeyError):
        runtime.materialize("probe_off")


def test_an_unknown_type_is_reported_and_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_system.runtime"):
        runtime = Runtime(_config({"probe_unknown": ToolServerConfig(
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
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8")

    assert "test" not in str(Path.cwd()), \
        "fixture: under a working directory containing 'test' the policy re-raises"

    config = _config(
        {"probe_broken": ToolServerConfig(type="broken_probe", enabled=True),
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
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8")

    monkeypatch.setattr("agent_system.runtime.Path.cwd", lambda: Path("/somewhere/tests/here"))

    config = _config({"probe_broken": ToolServerConfig(type="raise_probe", enabled=True)},
                     plugin_dirs=[str(root)])

    with pytest.raises(RuntimeError, match="boom"):
        Runtime(config).start()


def test_a_deploy_path_that_merely_looks_like_a_test_is_not_one(monkeypatch):
    """``/opt/agentsystem/releases/latest`` contains "test". So does
    ``C:/Users/tester/...``.

    The cwd substring alone would turn one broken plugin into a dead process
    on such a host, where the policy is to log and carry on. And it is not
    sufficient the other way either: the repo root itself contains no "test",
    so the check only ever fires for tests that chdir into a pytest tmp dir.
    pytest's own environment marker is what separates the two.
    """
    from agent_system.runtime import _in_test_cwd

    monkeypatch.setattr("agent_system.runtime.Path.cwd",
                        lambda: Path("/opt/agentsystem/releases/latest"))
    assert _in_test_cwd() is True, "fixture: under pytest this path has to match"

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert _in_test_cwd() is False, "a deploy path was taken for a test tree"


def test_materialize_injects_the_session_service():
    """An agent built later (lazily, or on demand) must not miss what every
    agent built at start got -- so the injection belongs in the build path,
    not only in the walk that runs once after bootstrap."""
    session_service = object()
    runtime = Runtime(_config({"probe_session": _agent_server()}),
                      session_service=session_service)

    agent = runtime.materialize("probe_session")

    assert agent._session_service is session_service
