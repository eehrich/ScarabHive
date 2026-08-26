"""
Integration tests for LLM factory with the new configuration system.

Tests the full workflow:
1. Load configuration from YAML files
2. Get MCP config with inheritance
3. Resolve LLM configuration from agent config
4. Create LLM client

SCOPE, honestly stated: client construction runs through the conftest fake
(registry.build_client is replaced so bootstrap opens no sockets) — the
"client" assertions here verify resolution and factory WIRING, not that a
provider plugin can actually build. Real construction is covered by
tests/llm/test_llm_provider_registry.py and
test_plugin_llm_clients_full_path.py (via _orig_build_client).
"""
import pytest
from agent_system.config.settings import load_settings, get_mcp_config_by_name
from agent_system.llm.factory import resolve_llm_config_for_agent, LLMFactory


class TestLLMFactoryIntegration:
    """Integration tests for LLM factory with configuration system."""

    @pytest.fixture
    def system_config(self):
        """Load system configuration."""
        return load_settings()

    def test_load_config_and_create_llm_for_basic_agent(self, system_config):
        """Test loading config and creating LLM for basic_agent."""
        # Get MCP config with inheritance
        mcp_config = get_mcp_config_by_name("basic_agent", system_config)
        assert mcp_config is not None
        assert mcp_config.agent_config is not None

        # Resolve LLM config
        resolved = resolve_llm_config_for_agent(system_config, mcp_config.agent_config)

        assert resolved.spec.provider
        assert resolved.spec.model
        assert resolved.spec.context_window

        # Create LLM client
        factory = LLMFactory(system_config, mcp_config.agent_config)
        llm_client = factory.create()

        assert llm_client is not None

    def test_different_profiles_use_different_models(self, system_config):
        """Test that different LLM profiles result in different model configurations."""
        # Get configs for agents with different profiles
        basic_config = get_mcp_config_by_name("basic_agent", system_config)

        # Resolve LLM configs
        basic_llm = resolve_llm_config_for_agent(system_config, basic_config.agent_config)

        # Profile is a list with default as first element
        assert isinstance(basic_config.agent_config.llm_profile, list)
        assert len(basic_config.agent_config.llm_profile) > 0

        # Check that we got a valid model
        assert basic_llm.spec.model is not None
        assert basic_llm.spec.provider is not None

    def test_llm_factory_with_inherited_agent_config(self, system_config):
        """Test LLM factory with agent config that inherits from default."""
        # Get a simple server that inherits agent_config
        mcp_config = get_mcp_config_by_name("duckduckgo_search", system_config)

        assert mcp_config is not None
        assert mcp_config.agent_config is not None  # Should be inherited

        # Should be able to create LLM client
        factory = LLMFactory(system_config, mcp_config.agent_config)
        llm_client = factory.create()

        assert llm_client is not None

    def test_all_configured_agents_can_create_llm(self, system_config):
        """Test that all configured agents can create LLM clients."""
        assert system_config.plugins and system_config.plugins.servers, "No MCP servers configured in system settings"

        # Test a few key servers
        test_servers = ["basic_agent", "web_research_agent", "duckduckgo_search"]

        for server_name in test_servers:
            if server_name not in system_config.plugins.servers:
                continue

            mcp_config = get_mcp_config_by_name(server_name, system_config)

            if mcp_config and mcp_config.agent_config:
                # Should be able to resolve LLM config
                llm_kwargs = resolve_llm_config_for_agent(system_config, mcp_config.agent_config)
                assert llm_kwargs is not None

                # Should be able to create LLM client
                factory = LLMFactory(system_config, mcp_config.agent_config)
                llm_client = factory.create()
                assert llm_client is not None

    def test_httpx_timeout_configuration(self, system_config):
        """Test that HTTPX timeout configuration is properly resolved."""
        mcp_config = get_mcp_config_by_name("basic_agent", system_config)

        resolved = resolve_llm_config_for_agent(system_config, mcp_config.agent_config)

        # The shipped llm.yaml sets llm_system.httpx_timeouts and basic_agent's
        # model carries no override, so the SYSTEM default must be stamped into
        # the spec. The previous assertion here checked request_timeout, a
        # non-optional int with a default — it could never fail.
        assert system_config.llm_system.httpx_timeouts is not None, (
            "llm.yaml no longer sets system httpx_timeouts — this test "
            "stopped measuring the default-stamping branch")
        assert resolved.spec.httpx_timeouts is not None, (
            "system httpx_timeouts never reached the resolved spec")

    def test_error_on_invalid_profile(self, system_config):
        """Test that invalid profile names raise appropriate errors."""
        from agent_system.config.models import AgentConfig

        # Create agent config with invalid profile
        invalid_agent_config = AgentConfig(llm_profile="non_existent_profile")

        with pytest.raises(ValueError, match="Profile.*not found"):
            resolve_llm_config_for_agent(system_config, invalid_agent_config)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
