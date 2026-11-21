"""
Tests for agent allowed tools filtering.

These tests verify that the allowed_patterns filtering works correctly
after tool expansion (i.e., individual tool names are filtered, not just server names).
"""

from __future__ import annotations

import pytest

from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder


class TestToolSchemaBuilderAllowedPatterns:
    """Test allowed pattern matching logic in ToolSchemaBuilder."""

    @pytest.fixture
    def schema_builder(self):
        """Create a ToolSchemaBuilder for testing."""
        # Mock tool integration manager
        class MockToolIntegrationManager:
            async def build_tool_schemas(self, tools):
                return [], {}

        # Mock server getter
        def mock_server_getter(name):
            return None

        return ToolSchemaBuilder(
            agent_name="test_agent",
            tool_integration_manager=MockToolIntegrationManager(),
            server_getter_func=mock_server_getter
        )

    def test_is_tool_allowed_exact_match(self, schema_builder):
        """Test exact match pattern for allowed tools."""
        tool_name = "doc_graph_batch_link"
        server_name = "doc_graph"
        allowed_patterns = ["doc_graph/doc_graph_batch_link"]

        assert schema_builder._is_tool_allowed(tool_name, server_name, allowed_patterns)

    def test_is_tool_allowed_server_wildcard(self, schema_builder):
        """Test server/* wildcard pattern for allowed tools."""
        tool_name = "doc_graph_batch_link"
        server_name = "doc_graph"
        allowed_patterns = ["doc_graph/*"]

        assert schema_builder._is_tool_allowed(tool_name, server_name, allowed_patterns)

    def test_is_tool_allowed_server_level(self, schema_builder):
        """Test server-level pattern (no slash) for allowed tools."""
        tool_name = "doc_graph_batch_link"
        server_name = "doc_graph"
        allowed_patterns = ["doc_graph"]

        assert schema_builder._is_tool_allowed(tool_name, server_name, allowed_patterns)

    def test_is_tool_allowed_fnmatch_wildcard(self, schema_builder):
        """Test fnmatch wildcard pattern for allowed tools."""
        tool_name = "doc_graph_batch_link"
        server_name = "doc_graph"
        allowed_patterns = ["doc_graph/*link"]

        assert schema_builder._is_tool_allowed(tool_name, server_name, allowed_patterns)

    def test_is_tool_not_allowed(self, schema_builder):
        """Test tool that doesn't match any allowed pattern."""
        tool_name = "doc_graph_batch_link"
        server_name = "doc_graph"
        allowed_patterns = ["doc_content/*"]

        assert not schema_builder._is_tool_allowed(tool_name, server_name, allowed_patterns)

    def test_apply_allowed_patterns(self, schema_builder):
        """Test applying allowed patterns to filter tool schemas."""
        # Create test data
        tools_schema = [
            {"type": "function", "function": {"name": "doc_graph_batch_link"}},
            {"type": "function", "function": {"name": "doc_graph_validate"}},
            {"type": "function", "function": {"name": "doc_content_report"}},
        ]
        tool_name_mapping = {
            "doc_graph_batch_link": "doc_graph",
            "doc_graph_validate": "doc_graph",
            "doc_content_report": "doc_content",
        }
        usable_tools = ["doc_graph", "doc_content"]
        display_tools = ["doc_graph_batch_link", "doc_graph_validate", "doc_content_report"]
        allowed_patterns = ["doc_graph/doc_graph_batch_link", "doc_content/*"]

        # Apply allowed patterns
        filtered_schema, filtered_mapping, filtered_usable, filtered_display = (
            schema_builder._apply_allowed_patterns(
                tools_schema, tool_name_mapping, usable_tools, display_tools, allowed_patterns
            )
        )

        # Verify only allowed tools remain
        assert len(filtered_schema) == 2
        assert filtered_schema[0]["function"]["name"] == "doc_graph_batch_link"
        assert filtered_schema[1]["function"]["name"] == "doc_content_report"

        assert "doc_graph_batch_link" in filtered_mapping
        assert "doc_content_report" in filtered_mapping
        assert "doc_graph_validate" not in filtered_mapping

        assert "doc_graph_batch_link" in filtered_display
        assert "doc_content_report" in filtered_display
        assert "doc_graph_validate" not in filtered_display

    def test_apply_allowed_patterns_server_wildcard(self, schema_builder):
        """Test applying server/* pattern to allow all tools from a server."""
        # Create test data
        tools_schema = [
            {"type": "function", "function": {"name": "doc_graph_batch_link"}},
            {"type": "function", "function": {"name": "doc_graph_validate"}},
            {"type": "function", "function": {"name": "doc_content_report"}},
        ]
        tool_name_mapping = {
            "doc_graph_batch_link": "doc_graph",
            "doc_graph_validate": "doc_graph",
            "doc_content_report": "doc_content",
        }
        usable_tools = ["doc_graph", "doc_content"]
        display_tools = ["doc_graph_batch_link", "doc_graph_validate", "doc_content_report"]
        allowed_patterns = ["doc_graph/*"]

        # Apply allowed patterns
        filtered_schema, filtered_mapping, filtered_usable, filtered_display = (
            schema_builder._apply_allowed_patterns(
                tools_schema, tool_name_mapping, usable_tools, display_tools, allowed_patterns
            )
        )

        # Verify only doc_graph tools remain
        assert len(filtered_schema) == 2
        assert filtered_schema[0]["function"]["name"] == "doc_graph_batch_link"
        assert filtered_schema[1]["function"]["name"] == "doc_graph_validate"

        assert "doc_graph_batch_link" in filtered_mapping
        assert "doc_graph_validate" in filtered_mapping
        assert "doc_content_report" not in filtered_mapping


def test_build_schemas_with_allowed_patterns():
    """Test build_schemas() applies allowed_patterns correctly.
    
    This test verifies the integration of allowed_patterns filtering
    by testing the actual filtering logic rather than the full build_schemas flow.
    """
    from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder
    
    # Create a schema builder (mocks don't matter for this test)
    class MockToolIntegrationManager:
        async def build_tool_schemas(self, tools):
            return [], {}
    
    schema_builder = ToolSchemaBuilder(
        agent_name="test_agent",
        tool_integration_manager=MockToolIntegrationManager(),
        server_getter_func=lambda x: None
    )
    
    # Create test data simulating what would be built from servers
    tools_schema = [
        {"type": "function", "function": {"name": "doc_graph_batch_link", "description": "test"}},
        {"type": "function", "function": {"name": "doc_graph_validate", "description": "test"}},
    ]
    tool_name_mapping = {
        "doc_graph_batch_link": "doc_graph",
        "doc_graph_validate": "doc_graph",
    }
    usable_tools = ["doc_graph"]
    display_tools = ["doc_graph_batch_link", "doc_graph_validate"]
    
    # Apply allowed patterns
    filtered_schema, filtered_mapping, filtered_usable, filtered_display = schema_builder._apply_allowed_patterns(
        tools_schema, tool_name_mapping, usable_tools, display_tools,
        allowed_patterns=["doc_graph/doc_graph_batch_link"]
    )
    
    # Verify only allowed tool remains
    assert len(filtered_schema) == 1
    assert filtered_schema[0]["function"]["name"] == "doc_graph_batch_link"
    assert "doc_graph_batch_link" in filtered_mapping
    assert "doc_graph_validate" not in filtered_mapping
    assert "doc_graph_batch_link" in filtered_display
    assert "doc_graph_validate" not in filtered_display
