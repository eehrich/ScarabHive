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
def mock_mcp_config_with_allowed_tasks():
    return MCPConfig(
        type="task_switch", 
        enabled=True, 
        config={"allowed_tasks": ["init", "analyze", "execute", "review"]}
    )


@pytest.fixture
def server(mock_system_config, mock_mcp_config):
    return TaskSwitchServer("task_switch", mock_system_config, mock_mcp_config)


@pytest.fixture
def server_with_restrictions(mock_system_config, mock_mcp_config_with_allowed_tasks):
    return TaskSwitchServer("task_switch", mock_system_config, mock_mcp_config_with_allowed_tasks)


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.agent_config = AgentConfig(template_vars={"current_task": "init"})
    return agent


class TestTaskSwitchServer:
    def test_init_default_var(self, server):
        assert server._task_var_name == "current_task"
        assert server._allowed_tasks is None

    def test_init_custom_var(self, mock_system_config):
        config = MCPConfig(type="task_switch", enabled=True, config={"task_var_name": "state"})
        server = TaskSwitchServer("task_switch", mock_system_config, config)
        assert server._task_var_name == "state"

    def test_init_with_allowed_tasks(self, server_with_restrictions):
        assert server_with_restrictions._allowed_tasks == ["init", "analyze", "execute", "review"]

    def test_get_template_vars(self, server_with_restrictions):
        vars = server_with_restrictions.get_template_vars()
        assert "allowed_tasks" in vars
        assert vars["allowed_tasks"] == ["init", "analyze", "execute", "review"]

    def test_get_template_vars_empty_when_no_restrictions(self, server):
        vars = server.get_template_vars()
        assert vars["allowed_tasks"] == []


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


class TestAllowedTasksValidation:
    @pytest.mark.asyncio
    async def test_allowed_task_succeeds(self, server_with_restrictions, mock_agent):
        """Valid task from allowed list should succeed."""
        result = await server_with_restrictions.set_task({"task_name": "analyze", "_agent": mock_agent})
        assert result["status"] == "success"
        assert result["current_task"] == "analyze"

    @pytest.mark.asyncio
    async def test_invalid_task_returns_error(self, server_with_restrictions, mock_agent):
        """Invalid task not in allowed list should return error."""
        result = await server_with_restrictions.set_task({"task_name": "invalid_task", "_agent": mock_agent})
        assert result["status"] == "error"
        assert "Invalid task" in result["error"]
        assert "invalid_task" in result["error"]
        assert "Allowed tasks" in result["error"]

    @pytest.mark.asyncio
    async def test_no_restrictions_allows_any_task(self, server, mock_agent):
        """Without allowed_tasks config, any task should work."""
        result = await server.set_task({"task_name": "arbitrary_task", "_agent": mock_agent})
        assert result["status"] == "success"
        assert result["current_task"] == "arbitrary_task"

    @pytest.mark.asyncio
    async def test_all_allowed_tasks_work(self, server_with_restrictions, mock_agent):
        """All tasks in the allowed list should work."""
        for task in ["init", "analyze", "execute", "review"]:
            result = await server_with_restrictions.set_task({"task_name": task, "_agent": mock_agent})
            assert result["status"] == "success", f"Task '{task}' should be allowed"
            assert result["current_task"] == task
