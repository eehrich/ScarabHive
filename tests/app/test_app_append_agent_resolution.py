"""Tests for resolve_agent_for_request: mid-run appends must reach the agent
instance that owns the run, not the global default agent."""
from types import SimpleNamespace

import pytest

from agent_system.app import resolve_agent_for_request
from agent_system.config.settings import load_settings as load_config
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.agent.server import Agent


class FakeJobManager:
    def __init__(self, job=None, raise_error=False):
        self._job = job
        self._raise = raise_error

    async def get_job(self, request_id):
        if self._raise:
            raise RuntimeError("job manager unavailable")
        return self._job


class FakeRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, name):
        return self._mapping[name]


def _build_real_agent(name: str) -> Agent:
    from agent_system.config.models import ToolServerConfig

    system_config = load_config("config/config.yaml")
    agent_config = system_config.agent_config if getattr(system_config, 'agent_config', None) else None
    if not agent_config:
        from agent_system.config.models import AgentConfig
        agent_config = AgentConfig()

    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent(name, system_config, server_config, ToolServerRegistry())


DEFAULT = SimpleNamespace(name="basic_agent")


@pytest.mark.asyncio
async def test_unknown_job_falls_back_to_default():
    result = await resolve_agent_for_request("req1", FakeJobManager(job=None), FakeRegistry({}), DEFAULT)
    assert result is DEFAULT


@pytest.mark.asyncio
async def test_job_manager_error_falls_back_to_default():
    result = await resolve_agent_for_request("req1", FakeJobManager(raise_error=True), FakeRegistry({}), DEFAULT)
    assert result is DEFAULT


@pytest.mark.asyncio
async def test_default_placeholder_skips_registry_lookup():
    job = SimpleNamespace(agent_name="default")
    # Registry would raise KeyError if consulted — placeholder must short-circuit
    result = await resolve_agent_for_request("req1", FakeJobManager(job=job), FakeRegistry({}), DEFAULT)
    assert result is DEFAULT


@pytest.mark.asyncio
async def test_same_name_as_default_skips_registry_lookup():
    job = SimpleNamespace(agent_name=DEFAULT.name)
    result = await resolve_agent_for_request("req1", FakeJobManager(job=job), FakeRegistry({}), DEFAULT)
    assert result is DEFAULT


@pytest.mark.asyncio
async def test_missing_agent_in_registry_falls_back_to_default():
    job = SimpleNamespace(agent_name="research_agent")
    result = await resolve_agent_for_request("req1", FakeJobManager(job=job), FakeRegistry({}), DEFAULT)
    assert result is DEFAULT


@pytest.mark.asyncio
async def test_non_agent_candidate_falls_back_to_default():
    job = SimpleNamespace(agent_name="some_tool_server")
    registry = FakeRegistry({"some_tool_server": object()})
    result = await resolve_agent_for_request("req1", FakeJobManager(job=job), registry, DEFAULT)
    assert result is DEFAULT


@pytest.mark.asyncio
async def test_resolves_owning_agent_instance():
    owning_agent = _build_real_agent("research_agent")
    job = SimpleNamespace(agent_name="research_agent")
    registry = FakeRegistry({"research_agent": owning_agent})
    result = await resolve_agent_for_request("req1", FakeJobManager(job=job), registry, DEFAULT)
    assert result is owning_agent


@pytest.mark.asyncio
async def test_none_registry_falls_back_to_default():
    job = SimpleNamespace(agent_name="research_agent")
    result = await resolve_agent_for_request("req1", FakeJobManager(job=job), None, DEFAULT)
    assert result is DEFAULT
