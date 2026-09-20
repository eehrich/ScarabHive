"""
Integration tests for LLM factory with the new configuration system.

Tests the full workflow:
1. Load configuration from YAML files
2. Get tool server config with inheritance
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
from agent_system.config.settings import load_settings, get_tool_server_config
from agent_system.llm.factory import resolve_llm_config_for_agent, LLMFactory


class TestLLMFactoryIntegration:
    """Integration tests for LLM factory with configuration system."""

    @pytest.fixture
    def system_config(self):
        """Load system configuration."""
        return load_settings()

    @staticmethod
    def _agent_servers(system_config, how_many=1):
        """Names of configured servers that carry an agent_config, from the CONFIG.

        Not a name written down here. Three tests in this file asked for
        ``basic_agent``, which stopped being a server instance and is now only the
        ``type`` of default_config -- they got None and failed on it, and a fourth
        walked a hand-written list of three names with a ``continue``, so the same
        disappearance went unnoticed there. A server name is a config value, and a
        test that pins one measures the config instead of the code.
        """
        servers = (system_config.plugins.servers if system_config.plugins else {}) or {}
        found = []
        for name in sorted(servers):
            config = get_tool_server_config(name, system_config)
            if config and config.agent_config and config.agent_config.llm_profile:
                found.append((name, config))
            if len(found) == how_many:
                return found
        pytest.fail(
            f"fixture: only {len(found)} of {how_many} servers carry an agent_config "
            f"with a profile (of {len(servers)} configured) -- this test would be "
            f"measuring nothing")

    @pytest.fixture
    def agent_server(self, system_config):
        """One configured agent server, with its inherited config."""
        return self._agent_servers(system_config)[0][1]

    def test_load_config_and_create_llm_for_an_agent_server(self, system_config, agent_server):
        """Test loading config and creating an LLM for a configured agent."""
        server_config = agent_server
        assert server_config is not None
        assert server_config.agent_config is not None

        # Resolve LLM config
        resolved = resolve_llm_config_for_agent(system_config, server_config.agent_config)

        assert resolved.spec.provider
        assert resolved.spec.model
        assert resolved.spec.context_window

        # Create LLM client
        factory = LLMFactory(system_config, server_config.agent_config)
        llm_client = factory.create()

        assert llm_client is not None

    def test_a_profile_chain_resolves_to_a_model(self, system_config, agent_server):
        """A profile list resolves to a model and a provider.

        Named after what it does. It used to be called "different profiles use
        different models" and loaded exactly one config, so it compared nothing --
        the name promised a difference the body never looked for.
        """
        # Profile is a list with default as first element
        assert isinstance(agent_server.agent_config.llm_profile, list)
        assert len(agent_server.agent_config.llm_profile) > 0

        resolved = resolve_llm_config_for_agent(system_config, agent_server.agent_config)
        assert resolved.spec.model is not None
        assert resolved.spec.provider is not None

    def test_llm_factory_with_inherited_agent_config(self, system_config):
        """Test LLM factory with agent config that inherits from default."""
        # Get a simple server that inherits agent_config
        server_config = get_tool_server_config("duckduckgo_search", system_config)

        assert server_config is not None
        assert server_config.agent_config is not None  # Should be inherited

        # Should be able to create LLM client
        factory = LLMFactory(system_config, server_config.agent_config)
        llm_client = factory.create()

        assert llm_client is not None

    def test_all_configured_agents_can_create_llm(self, system_config):
        """Test that all configured agents can create LLM clients."""
        assert system_config.plugins and system_config.plugins.servers, "No tool servers configured in system settings"

        # Taken from the config rather than written down: the list here used to name
        # three servers and `continue` past any that were missing, so when one of them
        # stopped existing this test went on passing while three others went red on it.
        checked = self._agent_servers(system_config, how_many=3)

        for server_name, server_config in checked:
            # Should be able to resolve LLM config
            llm_kwargs = resolve_llm_config_for_agent(system_config, server_config.agent_config)
            assert llm_kwargs is not None, server_name

            # Should be able to create LLM client
            factory = LLMFactory(system_config, server_config.agent_config)
            assert factory.create() is not None, server_name

    def test_httpx_timeout_configuration(self, system_config, agent_server):
        """Test that HTTPX timeout configuration is properly resolved."""
        resolved = resolve_llm_config_for_agent(system_config, agent_server.agent_config)

        # The shipped llm.yaml sets llm_system.httpx_timeouts, so unless the agent's
        # model carries an override the SYSTEM default must be stamped into the spec.
        # The previous assertion here checked request_timeout, a
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

    def test_a_profile_naming_no_model_is_refused(self, system_config):
        """The other half of that guard: the PROFILE exists, the model it names does not.

        Two different lines in resolve_llm_config_for_agent, and only the first had a
        test -- a mutation that let a dangling model_ref fall through to some other
        model stayed green. Which is the half that matters more: a typo in a profile's
        model_ref used to surface at the first call rather than at config load, and a
        silent fallback would have picked a model nobody asked for.
        """
        from agent_system.config.models import AgentConfig, LLMProfile

        broken = system_config.model_copy(deep=True)
        broken.llm_system.profiles["dangling"] = LLMProfile(model_ref="no_such_model")

        with pytest.raises(ValueError, match="Model reference"):
            resolve_llm_config_for_agent(broken, AgentConfig(llm_profile=["dangling"]))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
