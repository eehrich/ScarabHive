"""Tests for blocked tools functionality in tool discovery and schema building."""
import pytest
from unittest.mock import Mock, AsyncMock

from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder


class TestToolSchemaBuilderBlockedPatterns:
    """Test blocked patterns functionality in ToolSchemaBuilder."""

    def test_is_tool_blocked_exact_match(self):
        """Test exact match blocking."""
        builder = ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=Mock(),
            server_getter_func=Mock()
        )
        
        # Exact match should block
        assert builder._is_tool_blocked(
            "writer_graph_batch_link",
            "writer_graph",
            ["writer_graph/writer_graph_batch_link"]
        )
        
        # Non-match should not block
        assert not builder._is_tool_blocked(
            "writer_graph_validate",
            "writer_graph",
            ["writer_graph/writer_graph_batch_link"]
        )

    def test_is_tool_blocked_server_wildcard(self):
        """Test server/* wildcard blocking."""
        builder = ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=Mock(),
            server_getter_func=Mock()
        )
        
        # Server/* should block all tools from that server
        assert builder._is_tool_blocked(
            "any_tool",
            "blocked_server",
            ["blocked_server/*"]
        )
        
        # Other servers should not be blocked
        assert not builder._is_tool_blocked(
            "any_tool",
            "other_server",
            ["blocked_server/*"]
        )

    def test_is_tool_blocked_server_level(self):
        """Test server-level blocking (no slash)."""
        builder = ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=Mock(),
            server_getter_func=Mock()
        )
        
        # Server name without slash should block all tools from that server
        assert builder._is_tool_blocked(
            "any_tool",
            "blocked_server",
            ["blocked_server"]
        )
        
        # Other servers should not be blocked
        assert not builder._is_tool_blocked(
            "any_tool",
            "other_server",
            ["blocked_server"]
        )

    def test_is_tool_blocked_fnmatch_wildcard(self):
        """Test fnmatch wildcard blocking."""
        builder = ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=Mock(),
            server_getter_func=Mock()
        )
        
        # Pattern with wildcard should match
        assert builder._is_tool_blocked(
            "batch_link_tool",
            "writer",
            ["*batch*"]
        )
        
        # Non-matching should not block
        assert not builder._is_tool_blocked(
            "validate_tool",
            "writer",
            ["*batch*"]
        )

    def test_apply_blocked_patterns(self):
        """Test _apply_blocked_patterns filters correctly."""
        builder = ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=Mock(),
            server_getter_func=Mock()
        )
        
        tools_schema = [
            {"type": "function", "function": {"name": "writer_graph_validate", "description": "Validate"}},
            {"type": "function", "function": {"name": "writer_graph_batch_link", "description": "Batch link"}},
            {"type": "function", "function": {"name": "web_scraper_scrape", "description": "Scrape"}},
        ]
        
        tool_name_mapping = {
            "writer_graph_validate": "writer_graph",
            "writer_graph_batch_link": "writer_graph",
            "web_scraper_scrape": "web_scraper",
        }
        
        usable_tools = ["writer_graph", "writer_graph_validate", "writer_graph_batch_link", "web_scraper", "web_scraper_scrape"]
        display_tools = ["writer_graph_validate", "writer_graph_batch_link", "web_scraper_scrape"]
        
        blocked_patterns = ["writer_graph/writer_graph_batch_link"]
        
        filtered_schema, filtered_mapping, filtered_usable, filtered_display = builder._apply_blocked_patterns(
            tools_schema, tool_name_mapping, usable_tools, display_tools, blocked_patterns
        )
        
        # writer_graph_batch_link should be removed
        assert len(filtered_schema) == 2
        assert "writer_graph_batch_link" not in filtered_mapping
        assert "writer_graph_batch_link" not in filtered_usable
        assert "writer_graph_batch_link" not in filtered_display
        
        # Other tools should remain
        assert "writer_graph_validate" in filtered_mapping
        assert "web_scraper_scrape" in filtered_mapping

    def test_apply_blocked_patterns_server_wildcard(self):
        """Test blocking all tools from a server with server/* pattern."""
        builder = ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=Mock(),
            server_getter_func=Mock()
        )
        
        tools_schema = [
            {"type": "function", "function": {"name": "writer_graph_validate", "description": "Validate"}},
            {"type": "function", "function": {"name": "writer_graph_batch_link", "description": "Batch link"}},
            {"type": "function", "function": {"name": "web_scraper_scrape", "description": "Scrape"}},
        ]
        
        tool_name_mapping = {
            "writer_graph_validate": "writer_graph",
            "writer_graph_batch_link": "writer_graph",
            "web_scraper_scrape": "web_scraper",
        }
        
        usable_tools = ["writer_graph", "writer_graph_validate", "writer_graph_batch_link", "web_scraper", "web_scraper_scrape"]
        display_tools = ["writer_graph_validate", "writer_graph_batch_link", "web_scraper_scrape"]
        
        # Block all writer_graph tools
        blocked_patterns = ["writer_graph/*"]
        
        filtered_schema, filtered_mapping, filtered_usable, filtered_display = builder._apply_blocked_patterns(
            tools_schema, tool_name_mapping, usable_tools, display_tools, blocked_patterns
        )
        
        # All writer_graph tools should be removed
        assert len(filtered_schema) == 1
        assert "writer_graph_validate" not in filtered_mapping
        assert "writer_graph_batch_link" not in filtered_mapping
        
        # web_scraper should remain
        assert "web_scraper_scrape" in filtered_mapping


@pytest.mark.asyncio
async def test_build_schemas_with_blocked_patterns():
    """Integration test: build_schemas applies blocked patterns after expansion."""
    from dataclasses import dataclass
    
    # Create simple mock tool dataclass
    @dataclass
    class MockTool:
        name: str
        description: str
        input_schema: dict  # snake_case to match MCPTool class definition
    
    # Create mock MCP integration manager
    mock_mcp_integration = Mock()
    mock_mcp_integration.build_tool_schemas = AsyncMock(return_value=([], {}))
    
    # Create mock server that returns tool objects with proper attributes
    mock_server = Mock()
    mock_server.list_tools = AsyncMock(return_value=[
        MockTool(name="writer_graph_validate", description="Validate", input_schema={}),
        MockTool(name="writer_graph_batch_link", description="Batch link", input_schema={}),
        MockTool(name="writer_graph_create", description="Create", input_schema={}),
    ])
    
    def mock_get_server(name):
        if name == "writer_graph":
            return mock_server
        return None
    
    builder = ToolSchemaBuilder(
        agent_name="test_agent",
        mcp_integration_manager=mock_mcp_integration,
        server_getter_func=mock_get_server
    )
    
    # Build schemas with blocked patterns
    blocked_patterns = ["writer_graph/writer_graph_batch_link"]
    tools_schema, tool_name_mapping, usable_tools, display_tools = await builder.build_schemas(
        available_tools=["writer_graph"],
        blocked_patterns=blocked_patterns
    )
    
    # writer_graph_batch_link should be blocked
    tool_names = [t["function"]["name"] for t in tools_schema]
    assert "writer_graph_batch_link" not in tool_names
    assert "writer_graph_validate" in tool_names
    assert "writer_graph_create" in tool_names
