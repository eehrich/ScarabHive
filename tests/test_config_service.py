"""
Tests for ConfigService

Comprehensive test suite for configuration loading and management service.
"""
import logging
import pytest
from pathlib import Path
from unittest.mock import patch

from agent_system.services.config_service import ConfigService
from agent_system.config.models import AgentSystemConfig, MCPSystemConfig, MCPConfig


@pytest.fixture
def config_service():
    """Fixture providing a fresh ConfigService instance."""
    return ConfigService()


@pytest.fixture
def mock_config():
    """Fixture providing a mock AgentSystemConfig."""
    return AgentSystemConfig(
        mcp_system=MCPSystemConfig(
            servers={
                "test_server": MCPConfig(
                    enabled=True,
                    command="test_command",
                    args=["arg1", "arg2"]
                ),
                "disabled_server": MCPConfig(
                    enabled=False,
                    command="disabled_command"
                )
            },
            plugin_dirs=["src/plugins", "external/plugins"]
        )
    )


class TestConfigServiceInit:
    """Test ConfigService initialization."""

    def test_init_empty(self, config_service):
        """Test that ConfigService initializes with empty state."""
        assert config_service._config is None
        assert config_service._config_path is None

    def test_get_config_before_load(self, config_service):
        """Test get_config returns None before loading."""
        assert config_service.get_config() is None


class TestConfigServiceLoading:
    """Test configuration loading functionality."""

    def test_load_config_default(self, config_service, mock_config):
        """Test loading config from default location."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config):
            config = config_service.load_config()
            
            assert config is not None
            assert config == mock_config
            assert config_service._config == mock_config

    def test_load_config_custom_path(self, config_service, mock_config):
        """Test loading config from custom path."""
        custom_path = "custom/config.yaml"
        
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config) as mock_load:
            config = config_service.load_config(config_path=custom_path)
            
            mock_load.assert_called_once_with(custom_path)
            assert config == mock_config
            assert config_service._config_path == Path(custom_path)

    def test_load_config_caching(self, config_service, mock_config):
        """Test that config is cached and not reloaded unnecessarily."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config) as mock_load:
            # First load
            config1 = config_service.load_config("config.yaml")
            # Second load with same path
            config2 = config_service.load_config("config.yaml")
            
            # load_settings should only be called once
            assert mock_load.call_count == 1
            assert config1 is config2

    def test_load_config_force_reload(self, config_service, mock_config):
        """Test force_reload bypasses cache."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config) as mock_load:
            # First load
            config_service.load_config("config.yaml")
            # Force reload
            config_service.load_config("config.yaml", force_reload=True)
            
            # load_settings should be called twice
            assert mock_load.call_count == 2

    def test_load_config_different_path(self, config_service, mock_config):
        """Test loading different config path reloads."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config) as mock_load:
            # First load
            config_service.load_config("config1.yaml")
            # Load different path
            config_service.load_config("config2.yaml")
            
            # load_settings should be called twice
            assert mock_load.call_count == 2

    def test_load_config_error_handling(self, config_service):
        """Test error handling during config loading."""
        with patch('agent_system.services.config_service.load_settings', side_effect=ValueError("Invalid config")):
            with pytest.raises(ValueError, match="Invalid config"):
                config_service.load_config()

    def test_get_config_after_load(self, config_service, mock_config):
        """Test get_config returns loaded config."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config):
            config_service.load_config()
            
            cached_config = config_service.get_config()
            assert cached_config == mock_config


class TestConfigServiceLogging:
    """Test logging setup functionality."""

    def test_setup_logging_default(self, config_service):
        """Test default logging setup."""
        with patch('agent_system.services.config_service.logging.basicConfig') as mock_config:
            config_service.setup_logging()
            
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs['level'] == logging.INFO
            assert call_kwargs['force'] is True

    def test_setup_logging_verbose(self, config_service):
        """Test verbose logging setup."""
        with patch('agent_system.services.config_service.logging.basicConfig') as mock_config:
            config_service.setup_logging(verbose=True)
            
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs['level'] == logging.DEBUG

    def test_setup_logging_custom_level(self, config_service):
        """Test custom log level."""
        with patch('agent_system.services.config_service.logging.basicConfig') as mock_config:
            config_service.setup_logging(log_level="WARNING")
            
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs['level'] == logging.WARNING

    def test_setup_logging_invalid_level(self, config_service):
        """Test invalid log level defaults to INFO."""
        with patch('agent_system.services.config_service.logging.basicConfig') as mock_config:
            config_service.setup_logging(log_level="INVALID")
            
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs['level'] == logging.INFO


class TestMCPServerConfig:
    """Test MCP server configuration access."""

    def test_get_mcp_server_config_found(self, config_service, mock_config):
        """Test getting existing MCP server config."""
        config_service._config = mock_config
        
        server_config = config_service.get_mcp_server_config("test_server")
        
        assert server_config is not None
        assert server_config.enabled is True
        assert server_config.command == "test_command"

    def test_get_mcp_server_config_not_found(self, config_service, mock_config):
        """Test getting non-existent MCP server config."""
        config_service._config = mock_config
        
        server_config = config_service.get_mcp_server_config("nonexistent")
        
        assert server_config is None

    def test_get_mcp_server_config_no_config_loaded(self, config_service):
        """Test getting MCP server config when no config is loaded."""
        server_config = config_service.get_mcp_server_config("test_server")
        
        assert server_config is None

    def test_get_mcp_server_config_with_explicit_config(self, config_service, mock_config):
        """Test getting MCP server config with explicit config parameter."""
        server_config = config_service.get_mcp_server_config(
            "test_server",
            config=mock_config
        )
        
        assert server_config is not None
        assert server_config.command == "test_command"


class TestListMCPServers:
    """Test MCP server listing functionality."""

    def test_list_all_servers(self, config_service, mock_config):
        """Test listing all MCP servers."""
        config_service._config = mock_config
        
        servers = config_service.list_mcp_servers()
        
        assert len(servers) == 2
        assert "test_server" in servers
        assert "disabled_server" in servers

    def test_list_enabled_servers_only(self, config_service, mock_config):
        """Test listing only enabled servers."""
        config_service._config = mock_config
        
        servers = config_service.list_mcp_servers(enabled_only=True)
        
        assert len(servers) == 1
        assert "test_server" in servers
        assert "disabled_server" not in servers

    def test_list_servers_no_config_loaded(self, config_service):
        """Test listing servers when no config is loaded."""
        servers = config_service.list_mcp_servers()
        
        assert servers == {}

    def test_list_servers_with_explicit_config(self, config_service, mock_config):
        """Test listing servers with explicit config parameter."""
        servers = config_service.list_mcp_servers(config=mock_config)
        
        assert len(servers) == 2


class TestPluginDirs:
    """Test plugin directory access."""

    def test_get_plugin_dirs(self, config_service, mock_config):
        """Test getting plugin directories."""
        config_service._config = mock_config
        
        plugin_dirs = config_service.get_plugin_dirs()
        
        assert len(plugin_dirs) == 2
        assert all(isinstance(p, Path) for p in plugin_dirs)
        # Use Path comparison to handle OS-specific separators
        assert plugin_dirs[0] == Path("src/plugins")
        assert plugin_dirs[1] == Path("external/plugins")

    def test_get_plugin_dirs_no_config(self, config_service):
        """Test getting plugin dirs when no config is loaded."""
        plugin_dirs = config_service.get_plugin_dirs()
        
        assert plugin_dirs == []

    def test_get_plugin_dirs_empty(self, config_service):
        """Test getting plugin dirs when none are configured."""
        empty_config = AgentSystemConfig(
            mcp_system=MCPSystemConfig(servers={}, plugin_dirs=[])
        )
        config_service._config = empty_config
        
        plugin_dirs = config_service.get_plugin_dirs()
        
        assert plugin_dirs == []

    def test_get_plugin_dirs_filters_empty_strings(self, config_service):
        """Test that empty strings are filtered from plugin dirs."""
        config = AgentSystemConfig(
            mcp_system=MCPSystemConfig(
                servers={},
                plugin_dirs=["src/plugins", "", "external/plugins"]
            )
        )
        config_service._config = config
        
        plugin_dirs = config_service.get_plugin_dirs()
        
        # Should filter out empty string
        assert len(plugin_dirs) == 2


class TestCacheClear:
    """Test cache clearing functionality."""

    def test_clear_cache(self, config_service, mock_config):
        """Test clearing configuration cache."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config):
            # Load config
            config_service.load_config("config.yaml")
            
            assert config_service._config is not None
            assert config_service._config_path is not None
            
            # Clear cache
            config_service.clear_cache()
            
            assert config_service._config is None
            assert config_service._config_path is None

    def test_reload_after_clear(self, config_service, mock_config):
        """Test that reload works after clearing cache."""
        with patch('agent_system.services.config_service.load_settings', return_value=mock_config) as mock_load:
            # Load, clear, reload
            config_service.load_config("config.yaml")
            config_service.clear_cache()
            config_service.load_config("config.yaml")
            
            # load_settings should be called twice
            assert mock_load.call_count == 2


class TestIntegration:
    """Integration tests with real config loading."""

    def test_full_workflow(self, config_service, tmp_path):
        """Test complete workflow: load, access, reload."""
        # Create a temporary config file
        config_file = tmp_path / "test_config.yaml"
        config_content = """
mcp_system:
  servers:
    test_server:
      enabled: true
      command: test_cmd
      args: [arg1, arg2]
  plugin_dirs:
    - src/plugins
"""
        config_file.write_text(config_content)
        
        # Load config
        config = config_service.load_config(str(config_file))
        
        # Access various parts
        assert config is not None
        assert len(config_service.list_mcp_servers()) == 1
        
        server_config = config_service.get_mcp_server_config("test_server")
        assert server_config is not None
        assert server_config.command == "test_cmd"
        
        plugin_dirs = config_service.get_plugin_dirs()
        assert len(plugin_dirs) == 1
        
        # Test caching
        config2 = config_service.load_config(str(config_file))
        assert config is config2
        
        # Test force reload
        config3 = config_service.load_config(str(config_file), force_reload=True)
        assert config3 is not config  # Different instance after reload
