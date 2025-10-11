"""
Test that config-based agents receive the fully merged multi-section prompt.

This test verifies that the merged prompt (base + agent sections) is correctly
set in agent.agent_config.system_prompt so the Agent class can use it.
"""
from unittest.mock import Mock, patch
from agent_system.config.models import ConfigBasedAgentDefinition, AgentConfig, MCPConfig
from agent_system.servers.config_agent_factory import create_config_based_agent_factory


def test_agent_receives_merged_prompt():
    """Verify that created agent has merged prompt in agent_config.system_prompt."""
    
    # Create config with template
    config = AgentConfig(
        system_template="config/prompts/financial_analyst_prompt.yaml",
        max_steps=15
    )
    
    definition = ConfigBasedAgentDefinition(
        enabled=True,
        description="Financial analyst",
        base_type="agent",
        agent_config=config
    )
    
    # Create factory
    factory = create_config_based_agent_factory(
        name="financial_analyst",
        definition=definition,
        global_mcp_config=None
    )
    
    # Mock system config and mcp config
    mock_system_config = Mock()
    mock_system_config.context = Mock()
    mock_system_config.context.auto_datetime = False
    
    mock_mcp_config = MCPConfig()
    
    # Mock the Agent class to avoid full initialization
    with patch("agent_system.servers.config_agent_factory._get_agent_class_for_base_type") as mock_get_class:
        # Create a mock agent class
        mock_agent_class = Mock()
        mock_agent_instance = Mock()
        mock_agent_class.return_value = mock_agent_instance
        mock_get_class.return_value = mock_agent_class
        
        # Create agent via factory
        result_agent = factory("test_fa", mock_system_config, mock_mcp_config)
        
        # Verify agent was created
        assert result_agent is mock_agent_instance
        
        # Get the mcp_config that was passed to the agent constructor
        call_args = mock_agent_class.call_args
        assert call_args is not None
        
        # Extract mcp_config argument
        passed_mcp_config = call_args.kwargs.get('mcp_config') or call_args.args[2]
        
        # Verify agent_config.system_prompt has merged content
        merged_prompt = passed_mcp_config.agent_config.system_prompt
        
        assert merged_prompt is not None, "Merged prompt should not be None"
        assert len(merged_prompt) > 1000, f"Merged prompt should be substantial (got {len(merged_prompt)} chars)"
        
        # Should contain financial analyst content (from agent template)
        assert "financial" in merged_prompt.lower() or "stock" in merged_prompt.lower(), \
            "Should contain financial analyst content"
        
        # Should contain tools_prompt section (from base template)
        assert "# tools_prompt" in merged_prompt, \
            "Should contain tools_prompt section header from base template"
        
        # Should contain general_instructions section (from base template)
        assert "# general_instructions_prompt" in merged_prompt, \
            "Should contain general_instructions_prompt section header from base template"
        
        # Verify system_template was cleared (to prevent double-rendering)
        assert passed_mcp_config.agent_config.system_template is None, \
            "system_template should be cleared after merging"


def test_agent_with_inline_prompt_override():
    """Verify inline prompt overrides only system_prompt section."""
    
    config = AgentConfig(
        system_template="config/prompts/financial_analyst_prompt.yaml",
        system_prompt="INLINE OVERRIDE CONTENT",
        max_steps=10
    )
    
    definition = ConfigBasedAgentDefinition(
        enabled=True,
        description="Test agent",
        base_type="agent",
        agent_config=config
    )
    
    factory = create_config_based_agent_factory(
        name="test_agent",
        definition=definition,
        global_mcp_config=None
    )
    
    mock_system_config = Mock()
    mock_system_config.context = Mock()
    mock_system_config.context.auto_datetime = False
    
    mock_mcp_config = MCPConfig()
    
    with patch("agent_system.servers.config_agent_factory._get_agent_class_for_base_type") as mock_get_class:
        mock_agent_class = Mock()
        mock_agent_instance = Mock()
        mock_agent_class.return_value = mock_agent_instance
        mock_get_class.return_value = mock_agent_class
        
        agent = factory("test_agent", mock_system_config, mock_mcp_config)
        
        call_args = mock_agent_class.call_args
        passed_mcp_config = call_args.kwargs.get('mcp_config') or call_args.args[2]
        merged_prompt = passed_mcp_config.agent_config.system_prompt
        
        # Should contain inline override
        assert "INLINE OVERRIDE CONTENT" in merged_prompt, \
            "Should contain inline override content"
        
        # Should still contain inherited sections
        assert "# tools_prompt" in merged_prompt, \
            "Should still inherit tools_prompt from base"
        assert "# general_instructions_prompt" in merged_prompt, \
            "Should still inherit general_instructions from base"


def test_agent_without_template_uses_base_only():
    """Verify agent without template uses only base template sections."""
    
    config = AgentConfig(
        max_steps=10
    )
    
    definition = ConfigBasedAgentDefinition(
        enabled=True,
        description="Test agent",
        base_type="agent",
        agent_config=config
    )
    
    factory = create_config_based_agent_factory(
        name="test_agent",
        definition=definition,
        global_mcp_config=None
    )
    
    mock_system_config = Mock()
    mock_system_config.context = Mock()
    mock_system_config.context.auto_datetime = False
    
    mock_mcp_config = MCPConfig()
    
    with patch("agent_system.servers.config_agent_factory._get_agent_class_for_base_type") as mock_get_class:
        mock_agent_class = Mock()
        mock_agent_instance = Mock()
        mock_agent_class.return_value = mock_agent_instance
        mock_get_class.return_value = mock_agent_class
        
        agent = factory("test_agent", mock_system_config, mock_mcp_config)
        
        call_args = mock_agent_class.call_args
        passed_mcp_config = call_args.kwargs.get('mcp_config') or call_args.args[2]
        merged_prompt = passed_mcp_config.agent_config.system_prompt
        
        # Should have base template content
        assert merged_prompt is not None
        assert len(merged_prompt) > 100
        
        # Should have sections from base template
        assert "# tools_prompt" in merged_prompt or "tool" in merged_prompt.lower()
