"""
Tests for configuration settings loading and MCP config inheritance.

This test module focuses specifically on the settings.py functionality:
- Configuration loading from YAML files
- MCP config inheritance from default_config
- Deep merging of configuration values
"""
import pytest
from agent_system.config.settings import load_settings, get_mcp_config_by_name, _deep_merge_dict
from agent_system.config.models import AgentSystemConfig, MCPConfig


class TestConfigurationLoading:
    """Test configuration loading from YAML files."""
    
    def test_load_settings_returns_agent_system_config(self):
        """Test that load_settings returns an AgentSystemConfig instance."""
        config = load_settings()
        assert isinstance(config, AgentSystemConfig)
        assert config.name == "AgentSystem"
        assert config.version == "0.2.0"
    
    def test_load_settings_loads_llm_system(self):
        """Test that LLM system configuration is loaded from llm.yaml."""
        config = load_settings()
        assert config.llm_system is not None
        assert len(config.llm_system.models) > 0
        assert len(config.llm_system.profiles) > 0
        
        # Check that we have expected profiles
        expected_profiles = ["turbo", "normal", "think"]
        for profile in expected_profiles:
            assert profile in config.llm_system.profiles
    
    def test_load_settings_loads_mcp_system(self):
        """Test that MCP system configuration is loaded from mcp.yaml."""
        config = load_settings()
        assert config.mcp_system is not None
        assert len(config.mcp_system.plugin_dirs) > 0
        assert config.mcp_system.default_config is not None
        assert len(config.mcp_system.servers) > 0
    
    def test_load_settings_includes_context_and_network(self):
        """Test that core configurations are loaded."""
        config = load_settings()
        
        # Test context configuration
        assert config.context.auto_datetime is True
        assert config.context.timezone == "Europe/Berlin"
        
        # Test network configuration
        assert config.network.host == "127.0.0.1"
        assert config.network.port == 8000


class TestMCPConfigInheritance:
    """Test MCP configuration inheritance functionality."""
    
    @pytest.fixture
    def config(self):
        """Load configuration for tests."""
        return load_settings()
    
    def test_get_mcp_config_by_name_returns_none_for_missing_server(self, config):
        """Test that None is returned for non-existent servers."""
        result = get_mcp_config_by_name("non_existent_server", config)
        assert result is None
    
    def test_get_mcp_config_by_name_returns_mcp_config(self, config):
        """Test that a valid MCPConfig is returned for existing servers."""
        result = get_mcp_config_by_name("basic_agent", config)
        assert isinstance(result, MCPConfig)
        assert result.type == "basic_agent"
    
    def test_inheritance_from_default_config(self, config):
        """Test that server configs inherit from default_config."""
        # Get a server that truly inherits - duckduckgo_search only overrides 'type' and 'enabled'
        # It should inherit agent_config from default
        duck_config = get_mcp_config_by_name("duckduckgo_search", config)
        default_config = config.mcp_system.default_config
        
        assert duck_config is not None
        # duckduckgo_search has explicit enabled: true (overrides default's false)
        assert duck_config.enabled is True  # Explicitly set in config
        # Should inherit entire agent_config since none is specified
        assert duck_config.agent_config is not None  
        assert duck_config.agent_config.llm_profile == default_config.agent_config.llm_profile
        assert duck_config.agent_config.max_steps == default_config.agent_config.max_steps
    
    def test_server_config_overrides_default(self, config):
        """Test that server-specific values override defaults."""
        basic_config = get_mcp_config_by_name("basic_agent", config)
        
        assert basic_config is not None
        assert basic_config.type == "basic_agent"  # Specific to this server
        assert basic_config.agent_config is not None
        # Should have overridden allowed_tools
        assert basic_config.agent_config.tools.allowed == ["*"]
    
    def test_deep_merge_preserves_nested_defaults(self, config):
        """Test that deep merge preserves nested default values while overriding specifics."""
        web_config = get_mcp_config_by_name("web_research_agent", config)
        
        assert web_config is not None
        assert web_config.agent_config is not None
        
        # web_research_agent explicitly sets llm_profile: turbo (overrides default's normal)
        assert web_config.agent_config.llm_profile == "turbo"  # Explicitly set
        # max_steps is set at server level (max_steps: 20), not in agent_config
        # But agent_config.max_steps should still be 20 (inherited from default)
        assert web_config.agent_config.max_steps == 20
        
        # Should have specific allowed_tools
        expected_tools = ["web_research_agent/*", "duckduckgo_search/*", "web_scraper/*"]
        assert web_config.agent_config.tools.allowed == expected_tools
    
    def test_load_settings_without_config_parameter(self):
        """Test get_mcp_config_by_name works without explicit config parameter."""
        result = get_mcp_config_by_name("basic_agent")  # No config parameter
        assert isinstance(result, MCPConfig)
        assert result.type == "basic_agent"


class TestDeepMergeDictFunction:
    """Test the deep merge dictionary function."""
    
    def test_simple_merge(self):
        """Test simple key-value merging."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge_dict(base, override)
        
        assert result == {"a": 1, "b": 3, "c": 4}
    
    def test_nested_dict_merge(self):
        """Test merging of nested dictionaries."""
        base = {"config": {"x": 1, "y": 2}, "other": "value"}
        override = {"config": {"y": 3, "z": 4}}
        result = _deep_merge_dict(base, override)
        
        expected = {"config": {"x": 1, "y": 3, "z": 4}, "other": "value"}
        assert result == expected
    
    def test_none_values_preserve_defaults(self):
        """Test that None values in override don't overwrite defaults."""
        base = {"a": 1, "b": 2}
        override = {"b": None, "c": 3}
        result = _deep_merge_dict(base, override)
        
        assert result == {"a": 1, "b": 2, "c": 3}  # b should keep default value
    
    def test_deep_nested_merge(self):
        """Test deeply nested dictionary merging."""
        base = {
            "level1": {
                "level2": {
                    "keep": "default",
                    "override": "old"
                },
                "other": "value"
            }
        }
        override = {
            "level1": {
                "level2": {
                    "override": "new",
                    "add": "extra"
                }
            }
        }
        result = _deep_merge_dict(base, override)
        
        expected = {
            "level1": {
                "level2": {
                    "keep": "default",
                    "override": "new",
                    "add": "extra"
                },
                "other": "value"
            }
        }
        assert result == expected


if __name__ == "__main__":
    # Run tests directly
    pytest.main([__file__, "-v"])