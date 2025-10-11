"""
Test Agent Visibility System

Verifies that metadata.visibility correctly controls:
1. _mcp_public flag (UI dropdown visibility)
2. _mcp_tool_visible flag (tool discovery for other agents)
"""
from agent_system.config.models import (
    ConfigBasedAgentDefinition,
    AgentConfig,
    AgentMetadata
)
from agent_system.servers.config_agent_factory import create_config_based_agent_factory
from agent_system.config.models import MCPConfig
from unittest.mock import Mock


class TestAgentVisibility:
    """Test suite for agent visibility flags."""

    def test_visibility_ui_only(self):
        """Test visibility='ui': Show in UI, NOT available as tool."""
        config = AgentConfig(max_steps=10)
        metadata = AgentMetadata(
            visibility="ui",
            author="Test",
            version="1.0.0"
        )
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="UI-only agent",
            base_type="agent",
            agent_config=config,
            metadata=metadata
        )
        
        factory = create_config_based_agent_factory("test_ui_agent", definition)
        
        mock_system_config = Mock()
        mock_system_config.llm_system = Mock()
        mock_system_config.context = Mock()
        mock_system_config.context.auto_datetime = False
        
        agent = factory("test_ui_agent", mock_system_config, MCPConfig())
        
        assert agent._mcp_public is True, "UI agent should be public"
        assert agent._mcp_tool_visible is False, "UI agent should NOT be visible as tool"

    def test_visibility_tool_only(self):
        """Test visibility='tool': Available as tool, NOT in UI."""
        config = AgentConfig(max_steps=10)
        metadata = AgentMetadata(
            visibility="tool",
            author="Test",
            version="1.0.0"
        )
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Tool-only agent",
            base_type="agent",
            agent_config=config,
            metadata=metadata
        )
        
        factory = create_config_based_agent_factory("test_tool_agent", definition)
        
        mock_system_config = Mock()
        mock_system_config.llm_system = Mock()
        mock_system_config.context = Mock()
        mock_system_config.context.auto_datetime = False
        
        agent = factory("test_tool_agent", mock_system_config, MCPConfig())
        
        assert agent._mcp_public is False, "Tool agent should NOT be public"
        assert agent._mcp_tool_visible is True, "Tool agent should be visible as tool"

    def test_visibility_both(self):
        """Test visibility='both': Show in UI AND available as tool."""
        config = AgentConfig(max_steps=10)
        metadata = AgentMetadata(
            visibility="both",
            author="Test",
            version="1.0.0"
        )
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Dual-purpose agent",
            base_type="agent",
            agent_config=config,
            metadata=metadata
        )
        
        factory = create_config_based_agent_factory("test_both_agent", definition)
        
        mock_system_config = Mock()
        mock_system_config.llm_system = Mock()
        mock_system_config.context = Mock()
        mock_system_config.context.auto_datetime = False
        
        agent = factory("test_both_agent", mock_system_config, MCPConfig())
        
        assert agent._mcp_public is True, "Both agent should be public"
        assert agent._mcp_tool_visible is True, "Both agent should be visible as tool"

    def test_visibility_private(self):
        """Test visibility='private': Neither UI nor tool."""
        config = AgentConfig(max_steps=10)
        metadata = AgentMetadata(
            visibility="private",
            author="Test",
            version="1.0.0"
        )
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Private agent",
            base_type="agent",
            agent_config=config,
            metadata=metadata
        )
        
        factory = create_config_based_agent_factory("test_private_agent", definition)
        
        mock_system_config = Mock()
        mock_system_config.llm_system = Mock()
        mock_system_config.context = Mock()
        mock_system_config.context.auto_datetime = False
        
        agent = factory("test_private_agent", mock_system_config, MCPConfig())
        
        assert agent._mcp_public is False, "Private agent should NOT be public"
        assert agent._mcp_tool_visible is False, "Private agent should NOT be visible as tool"

    def test_default_visibility_is_ui(self):
        """Test default visibility is 'ui' when not specified."""
        config = AgentConfig(max_steps=10)
        # No metadata specified
        definition = ConfigBasedAgentDefinition(
            enabled=True,
            description="Default visibility agent",
            base_type="agent",
            agent_config=config,
            metadata=None
        )
        
        factory = create_config_based_agent_factory("test_default_agent", definition)
        
        mock_system_config = Mock()
        mock_system_config.llm_system = Mock()
        mock_system_config.context = Mock()
        mock_system_config.context.auto_datetime = False
        
        agent = factory("test_default_agent", mock_system_config, MCPConfig())
        
        # Default should be 'private' (secure by default - not visible anywhere)
        assert agent._mcp_public is False, "Default should NOT be UI-visible"
        assert agent._mcp_tool_visible is False, "Default should NOT be tool-visible"
