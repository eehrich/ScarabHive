"""Integration tests for configuration-based agents (Epic 0043).

This module tests the complete integration of config-based agents:
- Discovery and loading from config
- Bootstrap integration
- API endpoints
- CLI commands
- Validation

Aim: >90% coverage of config agent functionality.
"""

import pytest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from agent_system.config.models import AgentSystemConfig, ConfigBasedAgentDefinition
from agent_system.plugins.config_agent_discovery import (
    discover_config_agents,
    list_config_agents,
    get_config_agent_info,
)
from agent_system.plugins.config_agent_validation import (
    validate_config_agent,
    validate_all_config_agents,
    get_validation_summary,
)
from agent_system.servers.config_agent_factory import create_config_based_agent_factory


class TestConfigAgentDiscovery:
    """Test discovery and loading of config-based agents."""

    @pytest.fixture
    def sample_config(self, tmp_path: Path):
        """Create sample config with config agents."""
        from agent_system.config.models import (
            AgentConfig,
            ToolConfig,
            ContextManagementConfig,
        )
        
        # Create agent configs with inline prompts to avoid file dependencies
        test_agent_config = AgentConfig(
            llm_profile="turbo",
            max_steps=15,
            system_prompt="You are a financial analyst.",  # Inline prompt instead of file
            tools=ToolConfig(
                allowed=["basic_operations/*"],
                blocked=["ssh_control/*"],
            ),
            context_management=ContextManagementConfig(
                enabled=True,
                strategy="SUMMARIZE_OLDEST",
                preserve_recent_messages=10,
            ),
        )
        
        disabled_agent_config = AgentConfig(
            llm_profile="normal",
            max_steps=5,
            system_prompt="You are a simple assistant.",  # Inline prompt
        )
        
        # Create ConfigBasedAgentDefinitions dict (new structure)
        config_dict = {
            "test_agent": ConfigBasedAgentDefinition(
                enabled=True,
                description="Test agent for integration tests",
                base_type="agent",
                agent_config=test_agent_config,
                metadata={
                    "author": "test_author",
                    "version": "1.0.0",
                },
            ),
            "disabled_agent": ConfigBasedAgentDefinition(
                enabled=False,
                description="Disabled test agent",
                base_type="agent",
                agent_config=disabled_agent_config,
            ),
        }
        
        return config_dict

    def test_discover_config_agents(self, sample_config):
        """Test discovering all config agents from config."""
        agents = discover_config_agents(sample_config)
        
        # Should only discover enabled agents (test_agent), not disabled_agent
        assert len(agents) == 1
        assert "test_agent" in agents
        assert "disabled_agent" not in agents
        
        # Check that factory function is callable
        assert callable(agents["test_agent"])
        
        # Check metadata
        test_agent_factory = agents["test_agent"]
        assert hasattr(test_agent_factory, "_plugin_metadata")
        metadata = test_agent_factory._plugin_metadata
        assert metadata["name"] == "test_agent"
        assert metadata["source"] == "config"

    def test_list_config_agents(self, sample_config):
        """Test listing config agents."""
        agents_list = list_config_agents(sample_config)
        
        # Should list all agents (enabled and disabled)
        assert len(agents_list) == 2
        
        # Check structure
        for agent in agents_list:
            assert "name" in agent
            assert "enabled" in agent
            assert "description" in agent

    def test_get_config_agent_info(self, sample_config):
        """Test getting specific agent info."""
        info = get_config_agent_info("test_agent", sample_config)
        
        assert info is not None
        assert info["name"] == "test_agent"
        assert info["enabled"] is True
        assert info["description"] == "Test agent for integration tests"
        
        # Check LLM and steps
        assert info["llm_profile"] == "turbo"
        assert info["max_steps"] == 15
        
        # Check tools
        assert "tools" in info
        assert info["tools"] is not None
        assert len(info["tools"]["allowed"]) == 1
        assert "basic_operations/*" in info["tools"]["allowed"]

    def test_get_config_agent_info_not_found(self, sample_config):
        """Test getting info for non-existent agent."""
        with pytest.raises(KeyError, match="Config agent 'nonexistent' not found"):
            get_config_agent_info("nonexistent", sample_config)


class TestConfigAgentValidation:
    """Test validation of config-based agents."""

    @pytest.fixture
    def sample_config(self, tmp_path: Path):
        """Create sample config with config agents (reused from Discovery)."""
        from agent_system.config.models import (
            AgentConfig,
            ToolConfig,
            ContextManagementConfig,
        )
        
        # Create agent configs with inline prompts to avoid file dependencies
        test_agent_config = AgentConfig(
            llm_profile="turbo",
            max_steps=15,
            system_prompt="You are a financial analyst.",  # Inline prompt
            tools=ToolConfig(
                allowed=["basic_operations/*"],
                blocked=["ssh_control/*"],
            ),
            context_management=ContextManagementConfig(
                enabled=True,
                strategy="SUMMARIZE_OLDEST",
                preserve_recent_messages=10,
            ),
        )
        
        disabled_agent_config = AgentConfig(
            llm_profile="normal",
            max_steps=5,
            system_prompt="You are a simple Q&A assistant.",  # Inline prompt
        )
        
        # Create ConfigBasedAgentDefinitions dict (new structure)
        config_dict = {
            "test_agent": ConfigBasedAgentDefinition(
                enabled=True,
                description="Test agent for integration tests",
                base_type="agent",
                agent_config=test_agent_config,
                metadata={
                    "author": "test_author",
                    "version": "1.0.0",
                },
            ),
            "disabled_agent": ConfigBasedAgentDefinition(
                enabled=False,
                description="Disabled test agent",
                base_type="agent",
                agent_config=disabled_agent_config,
            ),
        }
        
        return config_dict

    @pytest.fixture
    def valid_agent_def(self) -> ConfigBasedAgentDefinition:
        """Create valid agent definition."""
        from agent_system.config.models import AgentConfig
        
        return ConfigBasedAgentDefinition(
            enabled=True,
            description="Valid test agent",
            base_type="agent",
            agent_config=AgentConfig(
                llm_profile="turbo",
                max_steps=10,
                system_prompt="You are a helpful assistant.",  # Inline prompt to avoid file dependency
            ),
        )

    @pytest.fixture
    def invalid_agent_def(self) -> Dict[str, Any]:
        """Create invalid agent definition (dict to bypass pydantic validation)."""
        return {
            "enabled": True,
            "description": "Invalid agent",
            "base_type": "invalid_type",  # Invalid base type
            "llm_profile": "turbo",
            "max_steps": -5,  # Invalid: negative steps
        }

    def test_validate_valid_agent(self, valid_agent_def: ConfigBasedAgentDefinition):
        """Test validation of valid agent."""
        errors = validate_config_agent("test_agent", valid_agent_def)
        
        # validate_config_agent returns list of errors, empty list means valid
        assert isinstance(errors, list)
        assert len(errors) == 0

    def test_validate_agent_with_template_missing(self, valid_agent_def: ConfigBasedAgentDefinition):
        """Test validation when template file is missing."""
        # Replace inline prompt with missing template
        valid_agent_def.agent_config.system_prompt = None
        valid_agent_def.agent_config.system_template = "nonexistent/path.yaml"
        
        errors = validate_config_agent("test_agent", valid_agent_def)
        
        # Should have errors about missing template
        assert isinstance(errors, list)
        assert len(errors) > 0
        assert any("template" in err.lower() or "not found" in err.lower() for err in errors)

    def test_validate_all_agents(self, sample_config):
        """Test validating all config agents."""
        results = validate_all_config_agents(sample_config)
        
        assert len(results) == 2
        
        # Both should be valid (empty error lists)
        assert results["test_agent"] == []
        assert results["disabled_agent"] == []

    def test_get_validation_summary(self):
        """Test getting validation summary."""
        # validate_all_config_agents returns Dict[str, List[str]]
        results = {
            "agent1": [],  # valid (empty list)
            "agent2": [],  # valid (empty list)
            "agent3": ["error1"],  # invalid (has errors)
        }
        
        summary = get_validation_summary(results)
        
        assert "Total agents: 3" in summary
        assert "Passed: 2" in summary
        assert "Failed: 1" in summary


class TestConfigAgentFactory:
    """Test config agent factory creation."""

    @pytest.fixture
    def sample_agent_def(self) -> ConfigBasedAgentDefinition:
        """Create sample agent definition."""
        from agent_system.config.models import AgentConfig, ToolConfig
        
        return ConfigBasedAgentDefinition(
            enabled=True,
            description="Factory test agent",
            base_type="agent",
            agent_config=AgentConfig(
                llm_profile="turbo",
                max_steps=15,
                system_prompt="You are a helpful assistant.",
                tools=ToolConfig(
                    allowed=["basic_operations/*"],
                    blocked=["ssh_control/*"],
                ),
            ),
        )

    @pytest.fixture
    def system_config(self) -> AgentSystemConfig:
        """Create system config."""
        return AgentSystemConfig()

    def test_factory_creation(
        self, sample_agent_def: ConfigBasedAgentDefinition, system_config: AgentSystemConfig
    ):
        """Test creating agent factory from config."""
        factory_func = create_config_based_agent_factory(
            "test_factory", sample_agent_def, None
        )
        
        assert factory_func is not None
        assert callable(factory_func)

    def test_factory_callable_signature(
        self, sample_agent_def: ConfigBasedAgentDefinition, system_config: AgentSystemConfig
    ):
        """Test that factory function has correct signature."""
        factory_func = create_config_based_agent_factory(
            "test_factory", sample_agent_def, None
        )
        
        # Factory should be callable with (agent_name, system_config, mcp_config)
        assert callable(factory_func)
        
        # Test that we can call it (will need mocking for actual agent creation)
        # For now just verify it's a function
        assert hasattr(factory_func, "__call__")

    @pytest.mark.asyncio
    async def test_factory_creates_agent(
        self, sample_agent_def: ConfigBasedAgentDefinition, system_config: AgentSystemConfig
    ):
        """Test creating agent instance from factory."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        factory_func = create_config_based_agent_factory(
            "test_factory", sample_agent_def, None
        )
        
        # Create a minimal MCP config
        mcp_config = MCPConfig(
            type="config_based",
            enabled=True,
            agent_config=AgentConfig()
        )
        
        # Mock the dynamic agent class resolution to avoid full initialization
        with patch("agent_system.servers.config_agent_factory._get_agent_class_for_base_type") as mock_get_class:
            mock_agent = MagicMock()
            mock_agent_class = MagicMock(return_value=mock_agent)
            mock_get_class.return_value = mock_agent_class
            
            # Call the factory
            agent = factory_func("test_agent", system_config, mcp_config)
            
            assert agent is not None
            # Verify agent class resolution was called
            mock_get_class.assert_called_once_with("agent")
            # Verify agent constructor was called
            mock_agent_class.assert_called()


class TestConfigAgentBootstrapIntegration:
    """Test integration with bootstrap process."""

    def test_config_agents_loaded_at_bootstrap(self, tmp_path: Path):
        """Test that config agents are discovered during bootstrap."""
        # This would test the actual bootstrap integration
        # Requires mocking or using the real bootstrap process
        pass  # TODO: Implement when bootstrap integration is finalized


class TestConfigAgentCLICommands:
    """Test CLI commands for config agents."""

    def test_cli_list_command(self):
        """Test 'config-agents list' command."""
        # This would test CLI integration
        # Can use subprocess or direct CLI module testing
        pass  # TODO: Implement CLI command tests

    def test_cli_show_command(self):
        """Test 'config-agents show <name>' command."""
        pass  # TODO: Implement

    def test_cli_validate_command(self):
        """Test 'config-agents validate' command."""
        pass  # TODO: Implement


class TestConfigAgentAPIEndpoints:
    """Test API endpoints for config agents."""

    @pytest.mark.asyncio
    async def test_api_list_config_agents(self):
        """Test GET /api/config-agents endpoint."""
        # Would test the actual API endpoint
        pass  # TODO: Implement API tests

    @pytest.mark.asyncio
    async def test_api_get_config_agent(self):
        """Test GET /api/config-agents/{name} endpoint."""
        pass  # TODO: Implement

    @pytest.mark.asyncio
    async def test_api_validate_config_agents(self):
        """Test POST /api/config-agents/validate endpoint."""
        pass  # TODO: Implement


class TestConfigAgentEndToEnd:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_complete_workflow(self):
        """Test complete workflow: load -> validate -> create -> use agent."""
        # This would test the entire flow from config to agent execution
        pass  # TODO: Implement end-to-end tests
