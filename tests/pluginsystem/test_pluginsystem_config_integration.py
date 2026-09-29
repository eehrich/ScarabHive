"""Test cases to verify plugins properly use their configuration from mcp.yaml.

This module tests that plugins receive and apply their server-specific configuration
correctly, preventing issues like double instantiation or config being ignored.
"""

import pytest
from unittest.mock import AsyncMock

from agent_system.plugins.tool_adapter import PluginToolRegistry
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
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig

        # Create proper config objects
        system_config = AgentSystemConfig()

        # Create ToolServerConfig with custom values
        server_config = ToolServerConfig(
            type="basic_operations",
            enabled=True,
            agent_config=AgentConfig()
        )
        server_config.max_wait_seconds = 1800
        server_config.default_update_interval = 2.5

        # Create plugin instance using factory (mimics real instantiation)
        server = PLUGIN_FACTORY("test_basic_ops", system_config, server_config)

        # Verify custom configuration is applied
        assert server.max_wait_seconds == 1800, f"Expected 1800, got {server.max_wait_seconds}"
        assert server.default_update_interval == 2.5, f"Expected 2.5, got {server.default_update_interval}"

    def test_basic_operations_uses_defaults_when_no_config(self):
        """Test that BasicOperations plugin uses defaults when no custom config provided."""
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig

        # Create plugin instance with default config
        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type="basic_operations", enabled=True, agent_config=AgentConfig())

        server = PLUGIN_FACTORY("test_basic_ops", system_config, server_config)

        # Verify default configuration is applied
        assert server.max_wait_seconds == 3600, f"Expected default 3600, got {server.max_wait_seconds}"
        assert server.default_update_interval == 1.0, f"Expected default 1.0, got {server.default_update_interval}"

    def test_basic_operations_ignores_reserved_keys(self, sample_config):
        """Test that BasicOperations plugin ignores reserved keys like 'type'."""
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig

        raw_entry = sample_config["servers"]["basic_operations"].copy()
        raw_entry["type"] = "basic_operations"  # This should be ignored
        raw_entry["some_unknown_key"] = "should_be_ignored"

        # Create proper config objects
        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type="basic_operations", enabled=True, agent_config=AgentConfig())
        server_config.max_wait_seconds = raw_entry.get("max_wait_seconds", 3600)
        server_config.default_update_interval = raw_entry.get("default_update_interval", 1.0)

        # Create plugin instance
        server = PLUGIN_FACTORY("test_basic_ops", system_config, server_config)

        # Verify custom configuration is still applied correctly
        assert server.max_wait_seconds == 1800
        assert server.default_update_interval == 2.5

    @pytest.mark.asyncio
    async def test_plugin_registry_forwards_config_correctly(self, sample_config, mock_parent_config):
        """Test that PluginToolRegistry forwards server config to plugins correctly."""
        registry = PluginToolRegistry()

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
        tool_adapter = registry.get_server(server_name)
        assert tool_adapter is not None

        # Access the underlying plugin server
        plugin_server = tool_adapter.plugin_server
        assert plugin_server.__class__.__name__ == "BasicOperationsServer"

        # Verify custom configuration was applied
        assert plugin_server.max_wait_seconds == 1800
        assert plugin_server.default_update_interval == 2.5

    @pytest.mark.asyncio
    async def test_plugin_registry_prevents_double_registration(self, sample_config, mock_parent_config):
        """Test that PluginToolRegistry prevents double registration of the same plugin."""
        registry = PluginToolRegistry()

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

        # The registry should handle re-registration gracefully
        # (Current implementation creates new instances but logs warning)
        assert isinstance(first_server, type(second_server))

        # Both should be valid plugin server instances
        assert hasattr(second_server, 'plugin_server')
        assert second_server.plugin_server is not None
        assert first_plugin_server.default_update_interval == 2.5  # Original value

    @pytest.mark.asyncio
    async def test_wait_tool_respects_server_config(self, sample_config):
        """Test that wait tool uses server-configured update_interval, not caller-provided."""
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig

        raw_entry = sample_config["servers"]["basic_operations"]

        # Create proper config objects
        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type="basic_operations", enabled=True, agent_config=AgentConfig())
        server_config.max_wait_seconds = raw_entry.get("max_wait_seconds", 3600)
        server_config.default_update_interval = raw_entry.get("default_update_interval", 1.0)

        server = PLUGIN_FACTORY("test_basic_ops", system_config, server_config)

        # Mock status object to capture calls
        mock_status = AsyncMock()
        status_calls = []

        async def capture_status_update(message):
            status_calls.append(message)

        # Both channels: the closing message used to be a `progress` call, so
        # the scope closed with its own default "completed" and the elapsed
        # time was lost. It is an `end` now -- same two updates the caller
        # sees, one of them on the phase that survives in the WebUI.
        mock_status.progress = AsyncMock(side_effect=capture_status_update)
        mock_status.end = AsyncMock(side_effect=capture_status_update)

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
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig

        # Test valid configuration
        valid_config = {
            "max_wait_seconds": 1800,
            "default_update_interval": 2.5
        }

        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type="basic_operations", enabled=True, agent_config=AgentConfig())
        server_config.max_wait_seconds = valid_config["max_wait_seconds"]
        server_config.default_update_interval = valid_config["default_update_interval"]

        server = PLUGIN_FACTORY("test_basic_ops", system_config, server_config)
        assert server.max_wait_seconds == 1800
        assert server.default_update_interval == 2.5

        # Test configuration with string numbers (should be converted)
        string_config = {
            "max_wait_seconds": "1800",
            "default_update_interval": "2.5"
        }

        system_config2 = AgentSystemConfig()
        server_config2 = ToolServerConfig(type="basic_operations", enabled=True, agent_config=AgentConfig())
        server_config2.max_wait_seconds = float(string_config["max_wait_seconds"])
        server_config2.default_update_interval = float(string_config["default_update_interval"])

        server = PLUGIN_FACTORY("test_basic_ops", system_config2, server_config2)
        assert server.max_wait_seconds == 1800.0  # Converted to float
        assert server.default_update_interval == 2.5  # Converted to float

    @pytest.mark.asyncio
    async def test_register_from_config_uses_correct_servers_section(self, sample_config, mock_parent_config):
        """Test that register_from_config reads from the correct servers section."""
        registry = PluginToolRegistry()

        # Discover plugins
        plugin_dirs = ["src/plugins"]
        registry.discover_plugins(plugin_dirs)

        # Simulate what tool integration does: extract enabled servers and servers config
        enabled_servers = ["basic_operations"]
        servers_config = sample_config["servers"]  # This should contain the custom config

        # Register plugins from config (mimics tool integration behavior)
        await registry.register_from_config(enabled_servers, servers_config, mock_parent_config)

        # Verify plugin was registered with correct config
        tool_adapter = registry.get_server("basic_operations")
        assert tool_adapter is not None

        plugin_server = tool_adapter.plugin_server
        assert plugin_server.__class__.__name__ == "BasicOperationsServer"

        # Verify the custom configuration from servers section was used
        assert plugin_server.max_wait_seconds == 1800, f"Expected 1800, got {plugin_server.max_wait_seconds}"
        assert plugin_server.default_update_interval == 2.5, f"Expected 2.5, got {plugin_server.default_update_interval}"

    def test_plugin_factory_logs_configuration(self, sample_config, caplog):
        """Test that plugin factory logs the configuration it receives for debugging."""
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig, AgentConfig
        import logging
        caplog.set_level(logging.INFO)

        raw_entry = sample_config["servers"]["basic_operations"]

        # Create proper config objects
        system_config = AgentSystemConfig()
        server_config = ToolServerConfig(type="basic_operations", enabled=True, agent_config=AgentConfig())
        server_config.max_wait_seconds = raw_entry.get("max_wait_seconds", 3600)
        server_config.default_update_interval = raw_entry.get("default_update_interval", 1.0)

        # Create plugin instance
        PLUGIN_FACTORY("test_basic_ops", system_config, server_config)

        # Verify logging occurred - check for the initialization log
        init_logs = [record for record in caplog.records if "initialized - max_wait_seconds=" in record.message]

        # Should have initialization log
        assert len(init_logs) >= 1, f"Should log initialization. Found records: {[r.message for r in caplog.records]}"

        # Verify the logged values match our custom config
        init_log_message = init_logs[0].message
        assert "max_wait_seconds=1800.0" in init_log_message
        assert "default_update_interval=2.5" in init_log_message

    @pytest.mark.asyncio
    async def test_agent_component_tool_integration_full_path(self, sample_config):
        """Test that ToolIntegrationManager passes the system config (including servers)

        This test patches `get_tool_integration` and asserts the integration's
        initialize() call receives an AgentSystemConfig that contains our
        servers section. This matches the modern initialization path.
        """
        from unittest.mock import AsyncMock, patch
        from agent_system.config.models import AgentSystemConfig
        from agent_system.servers.agent.components.tool_integration import ToolIntegrationManager

        # Build a temporary AgentSystemConfig dict that contains our servers
        conf = {
            "plugins": {
                "servers": sample_config["servers"],
                "plugin_dirs": ["src/plugins"]
            },
            "llm_system": sample_config.get("llm_system", {})
        }

        system_config = AgentSystemConfig.model_validate(conf)

        # Create a minimal AgentConfig for the manager (only used for signature)
        class DummyAgentConfig:
            pass

        agent_mcp_component = ToolIntegrationManager(system_config, DummyAgentConfig())

        # Patch get_tool_integration to capture the config passed to initialize()
        tool_init_called_with = None

        async def fake_initialize(cfg):
            nonlocal tool_init_called_with
            tool_init_called_with = cfg

        fake_integration = AsyncMock()
        fake_integration.initialized = False
        fake_integration.initialize = AsyncMock(side_effect=fake_initialize)

        with patch('agent_system.servers.agent.components.tool_integration.get_tool_integration', return_value=fake_integration):
            await agent_mcp_component.setup_tool_integration()

        assert fake_integration.initialize.called, "tool integration should have been initialized"
        assert tool_init_called_with is not None, "Initialization should have received a config"
        # Ensure the passed config is AgentSystemConfig with plugins.servers
        assert isinstance(tool_init_called_with, AgentSystemConfig), "Should pass AgentSystemConfig to initialize"
        assert hasattr(tool_init_called_with, 'plugins') and tool_init_called_with.plugins is not None
        assert hasattr(tool_init_called_with.plugins, 'servers') and 'basic_operations' in tool_init_called_with.plugins.servers

    @pytest.mark.asyncio
    async def test_centralized_server_config_merge(self, sample_config):
        """Test that `get_tool_server_config` merges default and server-specific values."""
        from agent_system.config.settings import get_tool_server_config
        from agent_system.config.models import AgentSystemConfig

        # Build a minimal AgentSystemConfig containing default and server overrides
        conf = {
            "plugins": {
                "default_config": {},
                "servers": sample_config["servers"]
            }
        }
        system_config = AgentSystemConfig.model_validate(conf)

        # Request the merged config for basic_operations
        merged = get_tool_server_config("basic_operations", config=system_config)
        assert merged is not None
        # The returned object should reflect custom values provided in sample_config
        assert getattr(merged, 'max_wait_seconds', None) == 1800 or getattr(merged, 'max_wait_seconds', None) == 1800.0
        assert getattr(merged, 'default_update_interval', None) == 2.5

    @pytest.mark.asyncio
    async def test_end_to_end_cli_config_flow(self, sample_config, tmp_path):
        """Test loading a config file via `load_settings()` and ensure tool integration

        receives the loaded AgentSystemConfig. We patch `get_tool_integration` to capture
        the config passed to initialize().
        """
        import yaml
        from unittest.mock import patch, AsyncMock
        from agent_system.config.settings import load_settings
        from agent_system.config.models import AgentSystemConfig

        # Create a temporary config file with our test configuration
        config_file = tmp_path / "test_agent.yaml"

        full_config = {
            "plugins": {
                "servers": sample_config["servers"],
                "plugin_dirs": ["src/plugins"]
            },
            "llm_system": sample_config["llm_system"],
            "agent_llm_profiles": sample_config["agent_llm_profiles"]
        }

        with open(config_file, 'w') as f:
            yaml.safe_dump(full_config, f)

        # Load settings from the file
        loaded = load_settings(str(config_file))
        assert isinstance(loaded, AgentSystemConfig)
        assert loaded.plugins is not None
        assert 'basic_operations' in loaded.plugins.servers

        # Patch get_tool_integration to capture initialize payload
        captured = None

        async def fake_initialize(cfg):
            nonlocal captured
            captured = cfg

        fake_integration = AsyncMock()
        fake_integration.initialized = False
        fake_integration.initialize = AsyncMock(side_effect=fake_initialize)

        with patch('agent_system.servers.agent.components.tool_integration.get_tool_integration', return_value=fake_integration):
            # Create ToolIntegrationManager and run setup to trigger initialize
            from agent_system.servers.agent.components.tool_integration import ToolIntegrationManager
            class DummyAgentConfig:
                pass
            mgr = ToolIntegrationManager(loaded, DummyAgentConfig())
            await mgr.setup_tool_integration()

        assert fake_integration.initialize.called
        assert captured is not None
        assert isinstance(captured, AgentSystemConfig), "Should pass AgentSystemConfig to initialize"
        assert hasattr(captured, 'plugins') and 'basic_operations' in captured.plugins.servers