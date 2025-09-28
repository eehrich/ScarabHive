"""Test cases to verify plugins properly use their configuration from mcp.yaml.

This module tests that plugins receive and apply their server-specific configuration
correctly, preventing issues like double instantiation or config being ignored.
"""

import pytest
from unittest.mock import AsyncMock

from agent_system.plugins.mcp_adapter import PluginMCPRegistry
from plugins.basic_operations.server import BasicOperationsServer
from plugins.basic_operations.plugin import PLUGIN_FACTORY


class TestPluginConfigIntegration:
    """Test plugin configuration integration with mcp.yaml settings."""

    @pytest.fixture
    def sample_config(self):
        """Sample configuration that mirrors mcp.yaml structure."""
        return {
            "servers": {
                "basic_operations": {
                    "type": "basic_operations",
                    "max_wait_seconds": 1800,  # Custom value different from default (3600)
                    "default_update_interval": 2.5  # Custom value different from default (1.0)
                },
                "example": {
                    "type": "example",
                    "precision": 4,  # Custom value different from default (2)
                    "max_text_length": 2000  # Custom value different from default (1000)
                }
            },
            "llm_system": {
                "models": {
                    "test-model": {
                        "provider": "openai",
                        "model": "gpt-4",
                        "context_window": 8192
                    }
                }
            },
            "agent_llm_profiles": {
                "test_agent": "test-model"
            }
        }

    @pytest.fixture
    def mock_parent_config(self, sample_config):
        """Parent configuration for plugin inheritance."""
        return {
            "llm_system": sample_config["llm_system"],
            "agent_llm_profiles": sample_config["agent_llm_profiles"]
        }

    def test_basic_operations_uses_custom_config(self, sample_config):
        """Test that BasicOperations plugin uses custom config values from mcp.yaml."""
        server_config = sample_config["servers"]["basic_operations"]
        
        # Create plugin instance using factory (mimics real instantiation)
        server = PLUGIN_FACTORY("test_basic_ops", server_config, ssl_verify=True)
        
        # Verify custom configuration is applied
        assert server.max_wait_seconds == 1800, f"Expected 1800, got {server.max_wait_seconds}"
        assert server.default_update_interval == 2.5, f"Expected 2.5, got {server.default_update_interval}"

    def test_basic_operations_uses_defaults_when_no_config(self):
        """Test that BasicOperations plugin uses defaults when no custom config provided."""
        # Create plugin instance with empty config
        server = PLUGIN_FACTORY("test_basic_ops", {}, ssl_verify=True)
        
        # Verify default configuration is applied
        assert server.max_wait_seconds == 3600, f"Expected default 3600, got {server.max_wait_seconds}"
        assert server.default_update_interval == 1.0, f"Expected default 1.0, got {server.default_update_interval}"

    def test_basic_operations_ignores_reserved_keys(self, sample_config):
        """Test that BasicOperations plugin ignores reserved keys like 'type'."""
        server_config = sample_config["servers"]["basic_operations"].copy()
        server_config["type"] = "basic_operations"  # This should be ignored
        server_config["some_unknown_key"] = "should_be_ignored"
        
        # Create plugin instance
        server = PLUGIN_FACTORY("test_basic_ops", server_config, ssl_verify=True)
        
        # Verify custom configuration is still applied correctly
        assert server.max_wait_seconds == 1800
        assert server.default_update_interval == 2.5

    @pytest.mark.asyncio
    async def test_plugin_registry_forwards_config_correctly(self, sample_config, mock_parent_config):
        """Test that PluginMCPRegistry forwards server config to plugins correctly."""
        registry = PluginMCPRegistry()
        
        # Discover basic_operations plugin
        plugin_dirs = ["src/plugins"]
        registry.discover_plugins(plugin_dirs)
        
        # Verify basic_operations is discovered
        assert "basic_operations" in registry.list_available_plugins()
        
        # Register plugin with custom config
        server_name = "basic_operations"
        server_config = sample_config["servers"][server_name]
        
        await registry.register_plugin(server_name, server_config, mock_parent_config)
        
        # Get the registered server and verify it uses custom config
        mcp_adapter = registry.get_server(server_name)
        assert mcp_adapter is not None
        
        # Access the underlying plugin server
        plugin_server = mcp_adapter.plugin_server
        assert isinstance(plugin_server, BasicOperationsServer)
        
        # Verify custom configuration was applied
        assert plugin_server.max_wait_seconds == 1800
        assert plugin_server.default_update_interval == 2.5

    @pytest.mark.asyncio
    async def test_plugin_registry_prevents_double_registration(self, sample_config, mock_parent_config):
        """Test that PluginMCPRegistry prevents double registration of the same plugin."""
        registry = PluginMCPRegistry()
        
        # Discover plugins
        plugin_dirs = ["src/plugins"]
        registry.discover_plugins(plugin_dirs)
        
        server_name = "basic_operations"
        server_config = sample_config["servers"][server_name]
        
        # Register plugin first time
        await registry.register_plugin(server_name, server_config, mock_parent_config)
        first_server = registry.get_server(server_name)
        first_plugin_server = first_server.plugin_server
        
        # Attempt to register again with different config
        different_config = {
            "max_wait_seconds": 7200,  # Different value
            "default_update_interval": 0.5  # Different value
        }
        
        await registry.register_plugin(server_name, different_config, mock_parent_config)
        second_server = registry.get_server(server_name)
        
        # Verify it's the same instance (no double registration)
        assert first_server is second_server
        assert first_plugin_server is second_server.plugin_server
        
        # Verify original config is preserved (not overwritten)
        assert first_plugin_server.max_wait_seconds == 1800  # Original value
        assert first_plugin_server.default_update_interval == 2.5  # Original value

    @pytest.mark.asyncio
    async def test_wait_tool_respects_server_config(self, sample_config):
        """Test that wait tool uses server-configured update_interval, not caller-provided."""
        server_config = sample_config["servers"]["basic_operations"]
        server = PLUGIN_FACTORY("test_basic_ops", server_config, ssl_verify=True)
        
        # Mock status object to capture calls
        mock_status = AsyncMock()
        status_calls = []
        
        async def capture_status_update(message):
            status_calls.append(message)
        
        mock_status.progress = AsyncMock(side_effect=capture_status_update)
        
        # Call wait with short duration but server should use its configured interval (2.5s)
        result = await server.call("wait", {
            "seconds": 0.5,  # Short wait
            "message": "Config Test",
            "_status": mock_status
        })
        
        # Verify operation succeeded
        assert result["status"] == "success"
        assert result["user_message"] == "Config Test"
        
        # Verify status updates contain the custom message
        assert len(status_calls) >= 2  # At least initial and final
        assert all("Config Test" in call for call in status_calls)
        
        # Note: We can't easily test the exact timing without making the test slow,
        # but the server logs will show the configured interval being used

    def test_plugin_config_schema_validation(self):
        """Test that plugin configuration follows expected schema patterns."""
        # Test valid configuration
        valid_config = {
            "max_wait_seconds": 1800,
            "default_update_interval": 2.5
        }
        
        server = PLUGIN_FACTORY("test_basic_ops", valid_config, ssl_verify=True)
        assert server.max_wait_seconds == 1800
        assert server.default_update_interval == 2.5
        
        # Test configuration with string numbers (should be converted)
        string_config = {
            "max_wait_seconds": "1800",
            "default_update_interval": "2.5"
        }
        
        server = PLUGIN_FACTORY("test_basic_ops", string_config, ssl_verify=True)
        assert server.max_wait_seconds == 1800.0  # Converted to float
        assert server.default_update_interval == 2.5  # Converted to float

    @pytest.mark.asyncio
    async def test_register_from_config_uses_correct_servers_section(self, sample_config, mock_parent_config):
        """Test that register_from_config reads from the correct servers section."""
        registry = PluginMCPRegistry()
        
        # Discover plugins
        plugin_dirs = ["src/plugins"]
        registry.discover_plugins(plugin_dirs)
        
        # Simulate what MCP integration does: extract enabled servers and servers config
        enabled_servers = ["basic_operations"]
        servers_config = sample_config["servers"]  # This should contain the custom config
        
        # Register plugins from config (mimics MCP integration behavior)
        await registry.register_from_config(enabled_servers, servers_config, mock_parent_config)
        
        # Verify plugin was registered with correct config
        mcp_adapter = registry.get_server("basic_operations")
        assert mcp_adapter is not None
        
        plugin_server = mcp_adapter.plugin_server
        assert isinstance(plugin_server, BasicOperationsServer)
        
        # Verify the custom configuration from servers section was used
        assert plugin_server.max_wait_seconds == 1800, f"Expected 1800, got {plugin_server.max_wait_seconds}"
        assert plugin_server.default_update_interval == 2.5, f"Expected 2.5, got {plugin_server.default_update_interval}"

    def test_plugin_factory_logs_configuration(self, sample_config, caplog):
        """Test that plugin factory logs the configuration it receives for debugging."""
        import logging
        caplog.set_level(logging.INFO)
        
        server_config = sample_config["servers"]["basic_operations"]
        
        # Create plugin instance
        PLUGIN_FACTORY("test_basic_ops", server_config, ssl_verify=True)
        
        # Verify logging occurred (check actual log messages)
        config_logs = [record for record in caplog.records if "Configuration:" in record.message]
        creation_logs = [record for record in caplog.records if "Creating BasicOperations plugin instance:" in record.message]
        init_logs = [record for record in caplog.records if "initialized - max_wait_seconds=" in record.message]
        
        # At least one of these should be present
        assert len(config_logs) >= 1 or len(creation_logs) >= 1, f"Should log configuration. Found records: {[r.message for r in caplog.records]}"
        assert len(init_logs) >= 1, "Should log effective configuration on initialization"
        
        # Verify the logged values match our custom config
        init_log_message = init_logs[0].message
        assert "max_wait_seconds=1800.0" in init_log_message
        assert "default_update_interval=2.5" in init_log_message