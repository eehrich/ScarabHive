"""``GET /agents`` decides from the view, not from a built instance.

The endpoint used to call ``registry.get(name)`` for every registered server
just to run ``isinstance`` and read ``_mcp_public``. Under a lazy start that
walk would materialize the whole process on the first UI request.

What this file can pin TODAY is the arithmetic of the new branch: the right
agents appear and the wrong ones do not. What it cannot pin yet is the saving
itself -- ``MCPRegistry.list()`` still returns only BUILT servers, so a
declared-but-unbuilt agent is not walked at all. That half arrives with the
stage that changes ``list()`` and ``get()`` together; until then the view and
the instance answer identically by construction, and no test can tell which
one the endpoint asked.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_system.app import build_app
from agent_system.config.models import (
    AgentConfig, AgentMetadata, AgentSystemConfig, LLMModelConfig, LLMProfile,
    LLMSystemConfig, MCPConfig, PluginsConfig,
)
from agent_system.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]

VISIBILITIES = {"probe_ui": "ui", "probe_tool": "tool",
                "probe_both": "both", "probe_private": "private"}
#: only these two belong in the UI dropdown
EXPECTED_IN_UI = {"probe_ui", "probe_both"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(build_app())


@pytest.fixture
def registry_of_agents():
    """Four built agents, one per visibility -- plus a PUBLIC non-agent.

    The non-agent is not decoration: a fixture of agents only cannot tell
    "lists agents that are public" from "lists everything that is public",
    and the mutation that drops the is_agent check stays green.
    """
    servers = {
        name: MCPConfig(type="basic_agent", enabled=True,
                        agent_config=AgentConfig(llm_profile="normal"),
                        metadata=AgentMetadata(visibility=visibility))
        for name, visibility in VISIBILITIES.items()
    }
    # A real shipped non-agent server, as visible as a manifest can make it.
    servers["probe_file_ops"] = MCPConfig(
        type="file_ops", enabled=True, metadata=AgentMetadata(visibility="both"))
    runtime = Runtime(AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(plugin_dirs=[str(REPO / "src" / "plugins")], servers=servers),
    ))
    for name in servers:
        runtime.materialize(name)

    listed = set(runtime.registry.list())
    assert listed == set(servers), f"fixture: the endpoint walks list(): {listed}"
    non_agent = runtime.registry.describe("probe_file_ops")
    assert non_agent.is_agent is False, "fixture: the non-agent has to be seen as one"
    assert non_agent.mcp_public is True, "fixture: and public, or is_agent is not what excludes it"
    return runtime.registry


def test_only_the_publicly_visible_agents_are_listed(client, registry_of_agents):
    with patch("agent_system.app._app_registry", registry_of_agents):
        response = client.get("/agents")

    assert response.status_code == 200
    assert set(response.json()["agents"]) == EXPECTED_IN_UI, response.json()


def test_a_registry_without_a_runtime_still_answers(client, registry_of_agents):
    """The fallback branch. Unbinding leaves the instances in place, so the
    answer must not change -- if it does, the two paths disagree and the
    endpoint's result depends on how the registry was made."""
    registry_of_agents._runtime = None

    with patch("agent_system.app._app_registry", registry_of_agents):
        response = client.get("/agents")

    assert set(response.json()["agents"]) == EXPECTED_IN_UI, response.json()


def test_an_agent_without_the_flag_at_all_stays_listed(client, registry_of_agents):
    """The backward-compatibility rule the endpoint documents: an agent that
    carries no ``_mcp_public`` is shown. Only reachable through the fallback
    -- every agent the runtime builds gets the flag from ``apply_to`` -- so
    without deleting it here the branch is never executed by any test."""
    registry_of_agents._runtime = None
    del registry_of_agents._servers["probe_private"]._mcp_public

    with patch("agent_system.app._app_registry", registry_of_agents):
        response = client.get("/agents")

    assert set(response.json()["agents"]) == EXPECTED_IN_UI | {"probe_private"}, response.json()


def test_a_mock_registry_does_not_turn_every_server_into_a_public_agent(client):
    """``describe()`` is asked with ``isinstance``, not ``is not None``.

    A Mock answers any call with a truthy Mock whose every attribute is
    truthy too, so ``view.is_agent and view.mcp_public`` would be true for
    every name and the endpoint would publish the whole registry. The suite
    is full of Mock registries; this is what keeps them on the old path.
    """
    from unittest.mock import MagicMock

    mock_registry = MagicMock()
    mock_registry.list.return_value = ["anything", "at", "all"]
    mock_registry.get.return_value = object()  # not an Agent

    with patch("agent_system.app._app_registry", mock_registry):
        response = client.get("/agents")

    assert response.json()["agents"] == [], response.json()
