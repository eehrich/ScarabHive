"""Integration tests for SubAgentManagerServer unified handler.

Tests the manage_sub_agent tool with all 5 operations.
"""

import pytest
from unittest.mock import Mock, AsyncMock
from plugins.sub_agent_manager.server import SubAgentManagerServer
from agent_system.config import AgentSystemConfig, MCPConfig


@pytest.fixture
def mock_config():
    """Mock system config."""
    config = Mock(spec=AgentSystemConfig)
    config.session_file_path = "data/sessions"
    # Add session_service and registry mocks
    config.session_service = Mock()
    config.registry = Mock()
    return config


@pytest.fixture
def mock_mcp_config():
    """Mock MCP config with default settings."""
    config = Mock(spec=MCPConfig)
    config.max_sub_agents_per_session = 10
    config.max_nesting_depth = 5
    config.allowed_agents = ["*"]
    config.blocked_agents = []
    return config


@pytest.fixture
def restricted_mcp_config():
    """Mock MCP config with restricted agent access."""
    config = Mock(spec=MCPConfig)
    config.max_sub_agents_per_session = 5
    config.max_nesting_depth = 3
    config.allowed_agents = ["coding_agent", "testing_agent"]
    config.blocked_agents = ["meta_agent"]
    return config


@pytest.fixture
def server(mock_config, mock_mcp_config):
    """Create server instance with mocked dependencies."""
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        mcp_config=mock_mcp_config
    )
    # Mock the manager
    server._manager = AsyncMock()
    return server


@pytest.fixture
def restricted_server(mock_config, restricted_mcp_config):
    """Create server instance with restricted agent access."""
    server = SubAgentManagerServer(
        name="coding_sub_agent_manager",
        system_config=mock_config,
        mcp_config=restricted_mcp_config
    )
    server._manager = AsyncMock()
    return server


class TestAgentFiltering:
    """Test agent name filtering logic."""

    def test_is_agent_allowed_wildcard(self, server):
        """Wildcard allows all agents."""
        assert server._is_agent_allowed("coding_agent") is True
        assert server._is_agent_allowed("web_research_agent") is True
        assert server._is_agent_allowed("meta_agent") is True

    def test_is_agent_allowed_exact_match(self, restricted_server):
        """Exact name matching works."""
        assert restricted_server._is_agent_allowed("coding_agent") is True
        assert restricted_server._is_agent_allowed("testing_agent") is True
        assert restricted_server._is_agent_allowed("web_research_agent") is False

    def test_is_agent_allowed_blacklist_priority(self, restricted_server):
        """Blacklist overrides whitelist."""
        assert restricted_server._is_agent_allowed("meta_agent") is False

    def test_is_agent_allowed_glob_pattern(self, mock_config):
        """Glob patterns work for matching."""
        config = Mock(spec=MCPConfig)
        config.max_sub_agents_per_session = 10
        config.max_nesting_depth = 5
        config.allowed_agents = ["coding_*", "test_*"]
        config.blocked_agents = []

        server = SubAgentManagerServer(
            name="pattern_manager",
            system_config=mock_config,
            mcp_config=config
        )

        assert server._is_agent_allowed("coding_agent") is True
        assert server._is_agent_allowed("coding_helper") is True
        assert server._is_agent_allowed("test_runner") is True
        assert server._is_agent_allowed("web_research_agent") is False


class TestUnifiedHandler:
    """Test unified manage_sub_agent handler."""

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_create(self, server):
        """Unified handler dispatches create operation."""
        # Mock create handler
        server._handle_create = AsyncMock(return_value={
            "instance_id": "sub_001",
            "agent_type": "coding_agent",
            "status": "completed"
        })

        result = await server.manage_sub_agent(params={
            "operation": "create",
            "agent_type": "coding_agent",
            "task": "Write code"
        })
        
        assert result["instance_id"] == "sub_001"
        server._handle_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_continue(self, server):
        """Unified handler dispatches continue operation."""
        server._handle_continue = AsyncMock(return_value={
            "instance_id": "sub_001",
            "status": "completed"
        })

        result = await server.manage_sub_agent(params={
            "operation": "continue",
            "instance_id": "sub_001",
            "message": "Continue task"
        })
        
        assert result["instance_id"] == "sub_001"
        server._handle_continue.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_list(self, server):
        """Unified handler dispatches list operation."""
        server._handle_list = AsyncMock(return_value={
            "sub_agents": [],
            "count": 0
        })

        result = await server.manage_sub_agent(params={
            "operation": "list"
        })
        
        assert result["count"] == 0
        server._handle_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_info(self, server):
        """Unified handler dispatches info operation."""
        server._handle_info = AsyncMock(return_value={
            "instance_id": "sub_001",
            "status": "active"
        })

        result = await server.manage_sub_agent(params={
            "operation": "info",
            "instance_id": "sub_001"
        })
        
        assert result["instance_id"] == "sub_001"
        server._handle_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_delete(self, server):
        """Unified handler dispatches delete operation."""
        server._handle_delete = AsyncMock(return_value={
            "instance_id": "sub_001",
            "status": "archived"
        })

        result = await server.manage_sub_agent(params={
            "operation": "delete",
            "instance_id": "sub_001"
        })
        
        assert result["status"] == "archived"
        server._handle_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_unknown_operation(self, server):
        """Unified handler returns error for unknown operation."""
        result = await server.manage_sub_agent(params={
            "operation": "invalid_op"
        })
        
        assert result["status"] == "error"
        assert "Unknown operation" in result["error"]

    @pytest.mark.asyncio
    async def test_unified_handler_missing_operation(self, server):
        """Unified handler returns error when operation missing."""
        result = await server.manage_sub_agent(params={})
        
        assert result["status"] == "error"
        assert "Missing 'operation'" in result["error"]
