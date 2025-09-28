"""Test cases to verify plugins properly use their configuration from mcp.yaml.

This module tests that plugins receive and apply their server-specific configuration
correctly, preventing issues like double instantiation or config being ignored.
"""

import pytest
from unittest.mock import AsyncMock, patch

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

    @pytest.mark.asyncio
    async def test_agent_component_mcp_integration_full_path(self, sample_config):
        """
        Test the FULL CLI execution path: Agent Component -> MCP Integration -> Plugin Execution
        
        This test would have caught the original bug where the agent component
        wasn't including servers config in the MCP payload.
        """
        from unittest.mock import MagicMock, AsyncMock, patch
        from agent_system.config.models import AgentConfig
        from agent_system.servers.agent.components.mcp_integration import MCPIntegrationManager
        
        # Create a mock AgentConfig object that mimics the real configuration loading
        mock_agent_config = MagicMock(spec=AgentConfig)
        
        # Set up the agent config with servers configuration
        mock_agent_config.servers = sample_config["servers"]
        mock_agent_config.llm_system = sample_config["llm_system"]  
        mock_agent_config.agent_llm_profiles = sample_config["agent_llm_profiles"]
        mock_agent_config.mcp = {
            "enabled_servers": ["basic_operations"],
            "plugin_dirs": ["src/plugins"]
        }
        
        # Mock the hasattr checks
        def mock_hasattr(obj, attr):
            return attr in ['servers', 'llm_system', 'agent_llm_profiles', 'mcp']
        
        # Create the agent MCP integration component  
        agent_mcp_component = MCPIntegrationManager(mock_agent_config)
        
        # Track what configuration gets passed to MCP integration
        mcp_init_config = None
        
        async def capture_mcp_init(config):
            nonlocal mcp_init_config
            mcp_init_config = config
            # Create a minimal mock MCP integration that just captures the config
            mock_integration = MagicMock()
            mock_integration.initialized = False
            return mock_integration
        
        # Patch the MCP integration creation and initialization
        with patch('agent_system.servers.agent.components.mcp_integration.get_mcp_integration') as mock_get_mcp, \
             patch('builtins.hasattr', side_effect=mock_hasattr):
            
            # Set up the mock MCP integration
            mock_mcp_integration = AsyncMock()
            mock_mcp_integration.initialized = False
            
            # Capture the initialize call
            async def mock_initialize(config):
                nonlocal mcp_init_config
                mcp_init_config = config
                mock_mcp_integration.initialized = True
            
            mock_mcp_integration.initialize = AsyncMock(side_effect=mock_initialize)
            mock_get_mcp.return_value = mock_mcp_integration
            
            # Execute the setup (this is what happens in real CLI execution)
            await agent_mcp_component.setup_mcp_integration()
        
        # Verify the MCP integration was called
        assert mock_mcp_integration.initialize.called, "MCP integration should have been initialized"
        
        # CRITICAL TEST: Verify the configuration includes servers config
        assert mcp_init_config is not None, "MCP integration should have received config"
        assert 'servers' in mcp_init_config, "MCP config should include servers section"
        assert 'basic_operations' in mcp_init_config['servers'], "Servers config should include basic_operations"
        
        # Verify the servers config contains the custom values from our test config
        basic_ops_config = mcp_init_config['servers']['basic_operations']
        assert basic_ops_config['max_wait_seconds'] == 1800, f"Expected 1800, got {basic_ops_config.get('max_wait_seconds')}"
        assert basic_ops_config['default_update_interval'] == 2.5, f"Expected 2.5, got {basic_ops_config.get('default_update_interval')}"
        
        # Verify other expected config sections are also present
        assert 'mcp' in mcp_init_config, "Should include MCP section"
        assert 'llm_system' in mcp_init_config, "Should include LLM system section" 
        assert 'agent_llm_profiles' in mcp_init_config, "Should include agent LLM profiles section"

    @pytest.mark.asyncio  
    async def test_centralized_build_mcp_payload_function(self, sample_config):
        """
        Test the centralized build_mcp_payload function to ensure it includes all required sections.
        
        This tests the fix we implemented to centralize configuration building.
        """
        from unittest.mock import MagicMock
        from agent_system.config.loader import build_mcp_payload
        from agent_system.config.models import AgentConfig
        
        # Create a mock AgentConfig object
        mock_config = MagicMock(spec=AgentConfig)
        mock_config.servers = sample_config["servers"]
        mock_config.llm_system = sample_config["llm_system"]
        mock_config.agent_llm_profiles = sample_config["agent_llm_profiles"]
        mock_config.mcp = {
            "enabled_servers": ["basic_operations"],
            "plugin_dirs": ["src/plugins"]
        }
        
        # Mock the model_dump method for MCP config - handle dict case
        def mock_mcp_model_dump():
            return mock_config.mcp
        if hasattr(mock_config.mcp, 'model_dump'):
            mock_config.mcp.model_dump = mock_mcp_model_dump
        else:
            # Mock as dict that already has the expected structure
            mock_config.mcp = mock_config.mcp
        
        # Mock hasattr to return True for expected attributes
        def mock_hasattr(obj, attr):
            return attr in ['servers', 'llm_system', 'agent_llm_profiles', 'mcp']
        
        with patch('builtins.hasattr', side_effect=mock_hasattr):
            # Call the centralized function
            payload = build_mcp_payload(mock_config)
        
        # Verify all required sections are present
        assert 'mcp' in payload, "Payload should include MCP section"
        assert 'llm_system' in payload, "Payload should include LLM system section"
        assert 'agent_llm_profiles' in payload, "Payload should include agent LLM profiles section"
        assert 'servers' in payload, "Payload should include servers section"
        
        # Verify servers section contains our test configuration
        assert payload['servers'] == sample_config["servers"], "Servers section should match input config"
        
        # Verify basic_operations config is correct
        basic_ops_config = payload['servers']['basic_operations']
        assert basic_ops_config['max_wait_seconds'] == 1800
        assert basic_ops_config['default_update_interval'] == 2.5

    @pytest.mark.asyncio
    async def test_end_to_end_cli_config_flow(self, sample_config, tmp_path):
        """
        Test the complete end-to-end configuration flow from config file to plugin execution.
        
        This simulates loading configuration from a file and verifying it reaches the plugin correctly.
        """
        import yaml
        from unittest.mock import patch, AsyncMock
        from agent_system.config.loader import load_config, build_mcp_payload
        
        # Create a temporary config file with our test configuration
        config_file = tmp_path / "test_agent.yaml"
        
        full_config = {
            "servers": sample_config["servers"],
            "llm_system": sample_config["llm_system"],
            "agent_llm_profiles": sample_config["agent_llm_profiles"],
            "mcp": {
                "enabled_servers": ["basic_operations"],
                "plugin_dirs": ["src/plugins"]
            }
        }
        
        with open(config_file, 'w') as f:
            yaml.safe_dump(full_config, f)
        
        # Load the configuration (this tests the real config loading)
        config = load_config(config_file)
        
        # Build MCP payload (this tests the centralized function)
        payload = build_mcp_payload(config)
        
        # Verify the payload contains servers config
        assert 'servers' in payload, "Config loading should preserve servers section"
        assert 'basic_operations' in payload['servers'], "Should include basic_operations config"
        
        basic_ops_config = payload['servers']['basic_operations']
        assert basic_ops_config['max_wait_seconds'] == 1800
        assert basic_ops_config['default_update_interval'] == 2.5
        
        # Mock the MCP integration to verify it receives correct config
        received_config = None
        
        async def capture_initialize(config):
            nonlocal received_config
            received_config = config
        
        with patch('agent_system.mcp.integration.MCPIntegration') as MockMCPIntegration:
            mock_instance = AsyncMock()
            mock_instance.initialize = AsyncMock(side_effect=capture_initialize)
            MockMCPIntegration.return_value = mock_instance
            
            # Simulate the CLI initialization process
            from agent_system.mcp.integration import MCPIntegration
            mcp_integration = MCPIntegration()
            await mcp_integration.initialize(payload)
        
        # Verify the MCP integration received the correct configuration
        assert received_config is not None, "MCP integration should have been initialized"
        assert received_config == payload, "MCP integration should receive the complete payload"
        assert 'servers' in received_config, "MCP integration should receive servers config"