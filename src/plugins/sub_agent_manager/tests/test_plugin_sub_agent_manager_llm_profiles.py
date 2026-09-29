"""Tests for sub_agent_manager LLM profile switching functionality.

This module tests the use_advanced_model parameter in sub_agent_manager:
- create operation with use_advanced_model
- continue operation with use_advanced_model
- Schema rendering
- Integration with BasicAgent
"""

from functools import partial
from unittest.mock import Mock

import pytest
try:
    from unittest.mock import AsyncMock  # Python 3.8+
except ImportError:
    # Fallback for Python <3.8
    class AsyncMock(Mock):  # type: ignore[no-redef]
        def __call__(self, *args, **kwargs):
            async def coro(*args, **kwargs):
                return super(AsyncMock, self).__call__(*args, **kwargs)
            return coro(*args, **kwargs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(iter(self.return_value))
            except StopIteration:
                raise StopAsyncIteration

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.servers.agent.components.session_tracking import SessionTracker
from plugins.sub_agent_manager.manager import SubAgentManager
from plugins.sub_agent_manager.server import SubAgentManagerServer


@pytest.fixture
def mock_system_config():
    """Create a mock system config."""
    config = Mock(spec=AgentSystemConfig)
    config.llm_system = Mock()
    config.llm_system.profiles = {
        "normal": Mock(model_ref="gpt-5-mini"),
        "think": Mock(model_ref="gpt-5.1"),
    }
    config.llm_system.models = {
        "gpt-5-mini": Mock(provider="openai_httpx", model="gpt-5-mini"),
        "gpt-5.1": Mock(provider="openai_httpx", model="gpt-5.1"),
    }
    config.network = Mock()
    config.network.ssl_verify = False
    return config


class TestSubAgentManagerSchema:
    """Test SubAgentManager schema includes use_advanced_model parameter."""

    def test_schema_includes_use_advanced_model(self, mock_system_config):
        """Test that manage_sub_agent tool includes use_advanced_model parameter."""
        server_config = ToolServerConfig(
            type="sub_agent_manager",
            enabled=True,
            allowed_agents=["basic_agent", "web_research_agent"]
        )

        server = SubAgentManagerServer("test_manager", mock_system_config, server_config)

        # Get tools
        tools = server.get_tools()

        # Find manage_sub_agent tool
        manage_tool = None
        for tool in tools:
            func = tool.get("function", {})
            if "manage_sub_agent" in func.get("name", ""):
                manage_tool = func
                break

        assert manage_tool is not None

        # Check properties
        params = manage_tool.get("parameters", {})
        props = params.get("properties", {})

        # Should have use_advanced_model property
        assert "use_advanced_model" in props
        assert props["use_advanced_model"]["type"] == "boolean"
        assert props["use_advanced_model"]["default"] is False

        # Description should mention both create and continue
        desc = props["use_advanced_model"]["description"]
        assert "create" in desc.lower()
        assert "continue" in desc.lower()


class TestSubAgentManagerCreateOperation:
    """Test SubAgentManager create operation with use_advanced_model."""

    @pytest.mark.asyncio
    async def test_create_with_use_advanced_model_true(self, mock_system_config):
        """Test that create operation passes use_advanced_model to agent.run_events()."""
        server_config = ToolServerConfig(
            type="sub_agent_manager",
            enabled=True,
            allowed_agents=["basic_agent"]
        )

        server = SubAgentManagerServer("test_manager", mock_system_config, server_config)

        # Mock dependencies
        mock_registry = Mock()
        mock_agent = Mock()
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.llm_profile = "normal"
        mock_agent._session_service = None
        mock_agent._session_tracker = Mock(wraps=SessionTracker())  # the real locks, calls recorded
        mock_agent._session_tracker.set_session_metadata = Mock()

        # Mock run_events to return async generator
        async def mock_run_events(*args, **kwargs):
            yield {"type": "start"}
            yield {"type": "final", "summary": "Task completed"}

        mock_agent.run_events = mock_run_events
        mock_registry.get = Mock(return_value=mock_agent)

        mock_session_service = Mock()
        mock_session_service.session_manager = Mock()
        mock_session_service.session_manager.load_session = AsyncMock(side_effect=FileNotFoundError())
        mock_session_service.session_manager.create_session = AsyncMock()
        mock_session_service.save_session = AsyncMock()

        # Mock manager
        mock_manager = Mock()
        mock_manager.create_sub_session = AsyncMock(return_value="sub_session_1")
        mock_manager.update_sub_session_metadata = AsyncMock()
        mock_manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, mock_manager)
        mock_manager._write_sub_agent = mock_manager.update_sub_session_metadata
        mock_manager.update_sub_agent_activity = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="user_1")
        mock_manager._session_service = mock_session_service

        # Mock internal methods
        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)

        # Test create with use_advanced_model=True
        params = {
            "operation": "create",
            "agent_type": "basic_agent",
            "task": "Complex reasoning task",
            "use_advanced_model": True,
            "_session_id": "parent_session",
            "_request_id": "req_123"
        }

        result = await server.manage_sub_agent(params)

        # Verify result
        assert result["status"] == "completed"
        assert "instance_id" in result

    @pytest.mark.asyncio
    async def test_create_with_use_advanced_model_false(self, mock_system_config):
        """Test that create operation with use_advanced_model=False uses default profile."""
        server_config = ToolServerConfig(
            type="sub_agent_manager",
            enabled=True,
            allowed_agents=["basic_agent"]
        )

        server = SubAgentManagerServer("test_manager", mock_system_config, server_config)

        # Mock dependencies
        mock_registry = Mock()
        mock_agent = Mock()
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.llm_profile = "normal"
        mock_agent._session_service = None
        mock_agent._session_tracker = Mock(wraps=SessionTracker())  # the real locks, calls recorded
        mock_agent._session_tracker.set_session_metadata = Mock()

        # Mock run_events
        async def mock_run_events(*args, **kwargs):
            # Verify use_advanced_model is False
            assert kwargs.get("use_advanced_model") is False
            yield {"type": "final", "summary": "Done"}

        mock_agent.run_events = mock_run_events
        mock_registry.get = Mock(return_value=mock_agent)

        mock_session_service = Mock()
        mock_session_service.session_manager = Mock()
        mock_session_service.session_manager.load_session = AsyncMock(side_effect=FileNotFoundError())
        mock_session_service.session_manager.create_session = AsyncMock()
        mock_session_service.save_session = AsyncMock()

        mock_manager = Mock()
        mock_manager.create_sub_session = AsyncMock(return_value="sub_session_1")
        mock_manager.update_sub_session_metadata = AsyncMock()
        mock_manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, mock_manager)
        mock_manager._write_sub_agent = mock_manager.update_sub_session_metadata
        mock_manager.update_sub_agent_activity = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="user_1")
        mock_manager._session_service = mock_session_service

        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)

        # Test create with use_advanced_model=False (explicit)
        params = {
            "operation": "create",
            "agent_type": "basic_agent",
            "task": "Simple task",
            "use_advanced_model": False,
            "_session_id": "parent_session",
            "_request_id": "req_123"
        }

        result = await server.manage_sub_agent(params)
        assert result["status"] == "completed"


class TestSubAgentManagerContinueOperation:
    """Test SubAgentManager continue operation with use_advanced_model."""

    @pytest.mark.asyncio
    async def test_continue_with_use_advanced_model_true(self, mock_system_config):
        """Test that continue operation can switch to advanced model."""
        server_config = ToolServerConfig(
            type="sub_agent_manager",
            enabled=True,
            allowed_agents=["basic_agent"]
        )

        server = SubAgentManagerServer("test_manager", mock_system_config, server_config)

        # Mock dependencies
        mock_registry = Mock()
        mock_agent = Mock()
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.llm_profile = "normal"
        mock_agent._session_service = None
        mock_agent._session_tracker = Mock(wraps=SessionTracker())  # the real locks, calls recorded
        mock_agent._session_tracker.set_session_metadata = Mock()

        # Track use_advanced_model parameter
        received_use_advanced = None

        async def mock_run_events(*args, **kwargs):
            nonlocal received_use_advanced
            received_use_advanced = kwargs.get("use_advanced_model", False)
            yield {"type": "final", "summary": "Continued"}

        mock_agent.run_events = mock_run_events
        mock_registry.get = Mock(return_value=mock_agent)

        mock_session_service = Mock()
        mock_session_manager = Mock()
        mock_session_manager.load_session = AsyncMock(return_value={
            "agent_name": "basic_agent",
            "parent_session": {"session_id": "parent_session"}
        })
        mock_session_service.session_manager = mock_session_manager
        mock_session_service.save_session = AsyncMock()

        mock_manager = Mock()
        mock_manager.update_sub_session_metadata = AsyncMock()
        mock_manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, mock_manager)
        mock_manager._write_sub_agent = mock_manager.update_sub_session_metadata
        mock_manager.update_sub_agent_activity = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="user_1")
        mock_manager._session_service = mock_session_service

        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)

        # Test continue with use_advanced_model=True (switch to better model)
        params = {
            "operation": "continue",
            "instance_id": "sub_session_1",
            "message": "Need deeper analysis",
            "use_advanced_model": True,
            "_session_id": "parent_session",
            "_request_id": "req_123"
        }

        result = await server.manage_sub_agent(params)

        # Verify result and parameter passing
        assert result["status"] == "completed"
        assert received_use_advanced is True

    @pytest.mark.asyncio
    async def test_continue_with_use_advanced_model_false(self, mock_system_config):
        """Test that continue operation can use standard model."""
        server_config = ToolServerConfig(
            type="sub_agent_manager",
            enabled=True,
            allowed_agents=["basic_agent"]
        )

        server = SubAgentManagerServer("test_manager", mock_system_config, server_config)

        # Mock dependencies
        mock_registry = Mock()
        mock_agent = Mock()
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.llm_profile = "normal"
        mock_agent._session_service = None
        mock_agent._session_tracker = Mock(wraps=SessionTracker())  # the real locks, calls recorded
        mock_agent._session_tracker.set_session_metadata = Mock()

        received_use_advanced = None

        async def mock_run_events(*args, **kwargs):
            nonlocal received_use_advanced
            received_use_advanced = kwargs.get("use_advanced_model", False)
            yield {"type": "final", "summary": "Summary"}

        mock_agent.run_events = mock_run_events
        mock_registry.get = Mock(return_value=mock_agent)

        mock_session_service = Mock()
        mock_session_manager = Mock()
        mock_session_manager.load_session = AsyncMock(return_value={
            "agent_name": "basic_agent",
            "parent_session": {"session_id": "parent_session"}
        })
        mock_session_service.session_manager = mock_session_manager
        mock_session_service.save_session = AsyncMock()

        mock_manager = Mock()
        mock_manager.update_sub_session_metadata = AsyncMock()
        mock_manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, mock_manager)
        mock_manager._write_sub_agent = mock_manager.update_sub_session_metadata
        mock_manager.update_sub_agent_activity = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="user_1")
        mock_manager._session_service = mock_session_service

        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)

        # Test continue with use_advanced_model=False (downgrade to cheaper model)
        params = {
            "operation": "continue",
            "instance_id": "sub_session_1",
            "message": "Simple summary needed",
            "use_advanced_model": False,
            "_session_id": "parent_session",
            "_request_id": "req_123"
        }

        result = await server.manage_sub_agent(params)

        # Verify parameter was passed correctly
        assert result["status"] == "completed"
        assert received_use_advanced is False


class TestSubAgentManagerUseCases:
    """Test real-world use cases for LLM profile switching."""

    @pytest.mark.asyncio
    async def test_escalation_scenario(self, mock_system_config):
        """Test escalation: start standard, upgrade to advanced."""
        # Scenario: Agent starts with default model, realizes task is complex,
        # coordinator continues with advanced model

        server_config = ToolServerConfig(
            type="sub_agent_manager",
            enabled=True,
            allowed_agents=["basic_agent"]
        )

        server = SubAgentManagerServer("test_manager", mock_system_config, server_config)

        # Track which model was used for each call
        model_usage = []

        async def mock_run_events(*args, **kwargs):
            model_usage.append(kwargs.get("use_advanced_model", False))
            yield {"type": "final", "summary": "Done"}

        # Setup mocks
        mock_registry = Mock()
        mock_agent = Mock()
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.llm_profile = ["normal", "think"]
        mock_agent._session_service = None
        mock_agent._session_tracker = Mock(wraps=SessionTracker())  # the real locks, calls recorded
        mock_agent._session_tracker.set_session_metadata = Mock()
        mock_agent.run_events = mock_run_events
        mock_registry.get = Mock(return_value=mock_agent)

        mock_session_service = Mock()
        mock_session_manager = Mock()

        # Create session data that will be returned on successful load
        session_data = {
            "agent_name": "basic_agent",
            "parent_session": {"session_id": "parent_session"},
            "user_id": "user_1",
            "session_id": "sub_session_1"
        }

        # load_session: Return session data for the sub-session, fail for others
        async def load_session_side_effect(user_id, session_id):
            if session_id == "sub_session_1":
                return session_data
            raise FileNotFoundError()

        mock_session_manager.load_session = AsyncMock(side_effect=load_session_side_effect)
        mock_session_manager.create_session = AsyncMock(return_value=session_data)
        mock_session_service.session_manager = mock_session_manager
        mock_session_service.save_session = AsyncMock()

        mock_manager = Mock()
        mock_manager.create_sub_session = AsyncMock(return_value="sub_session_1")
        mock_manager.update_sub_session_metadata = AsyncMock()
        mock_manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, mock_manager)
        mock_manager._write_sub_agent = mock_manager.update_sub_session_metadata
        mock_manager.update_sub_agent_activity = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="user_1")
        mock_manager._session_service = mock_session_service

        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)

        # Step 1: Create with standard model
        result1 = await server.manage_sub_agent({
            "operation": "create",
            "agent_type": "basic_agent",
            "task": "Analyze this",
            "use_advanced_model": False,
            "_session_id": "parent_session",
            "_request_id": "req_1"
        })

        assert result1["status"] == "completed"
        assert model_usage[0] is False  # Used standard model

        # Step 2: Continue with advanced model (escalation)
        result2 = await server.manage_sub_agent({
            "operation": "continue",
            "instance_id": "sub_session_1",
            "message": "Need deeper analysis",
            "use_advanced_model": True,
            "_session_id": "parent_session",
            "_request_id": "req_2"
        })

        assert result2["status"] == "completed"
        assert model_usage[1] is True  # Upgraded to advanced model
