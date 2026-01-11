"""Tests for task_switch plugin."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from plugins.task_switch.server import TaskSwitchServer
from agent_system.config.models import MCPConfig, AgentConfig


@pytest.fixture
def mock_system_config():
    config = MagicMock()
    config.ssl_verify = True
    return config


@pytest.fixture
def mock_mcp_config():
    return MCPConfig(type="task_switch", enabled=True)


@pytest.fixture
def server(mock_system_config, mock_mcp_config):
    return TaskSwitchServer("task_switch", mock_system_config, mock_mcp_config)


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.agent_config = AgentConfig(template_vars={"current_task": "init"})
    return agent


class TestTaskSwitchServer:
    def test_init_default_var(self, server):
        assert server._task_var_name == "current_task"

    def test_init_custom_var(self, mock_system_config):
        config = MCPConfig(type="task_switch", enabled=True, config={"task_var_name": "state"})
        server = TaskSwitchServer("task_switch", mock_system_config, config)
        assert server._task_var_name == "state"


class TestSetTask:
    @pytest.mark.asyncio
    async def test_set_task_basic(self, server, mock_agent):
        result = await server.set_task({"task_name": "analyze", "_agent": mock_agent})
        assert result["status"] == "success"
        assert result["previous_task"] == "init"
        assert result["current_task"] == "analyze"
        assert mock_agent.agent_config.template_vars["current_task"] == "analyze"

    @pytest.mark.asyncio
    async def test_set_task_empty_error(self, server, mock_agent):
        result = await server.set_task({"task_name": "", "_agent": mock_agent})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_task_no_agent(self, server):
        result = await server.set_task({"task_name": "test", "_agent": None})
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_set_task_initializes_template_vars(self, server):
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars=None)
        result = await server.set_task({"task_name": "plan", "_agent": agent})
        assert result["status"] == "success"
        assert agent.agent_config.template_vars["current_task"] == "plan"

    @pytest.mark.asyncio
    async def test_set_task_with_status(self, server, mock_agent):
        mock_status = AsyncMock()
        result = await server.set_task({"task_name": "review", "_agent": mock_agent, "_status": mock_status})
        assert result["status"] == "success"
        mock_status.progress.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_transitions(self, server, mock_agent):
        await server.set_task({"task_name": "analyze", "_agent": mock_agent})
        result = await server.set_task({"task_name": "execute", "_agent": mock_agent})
        assert result["previous_task"] == "analyze"
        assert result["current_task"] == "execute"
