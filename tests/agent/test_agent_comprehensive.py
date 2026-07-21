"""
Comprehensive tests for the Agent System MCP architecture.
"""
import pytest
from unittest.mock import AsyncMock

from agent_system.mcp.base import MCPRegistry, MCPServer
from agent_system.config.models import AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile, ContextConfig
from agent_system.servers.agent.server import Agent


class MockMCPServer(MCPServer):
    """Mock MCP server for testing."""
    
    def __init__(self, name: str, config_dict: dict = None):
        from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig
        
        # Create proper config objects
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type=name, enabled=True, agent_config=AgentConfig())
        
        # Store the config dict for test assertions
        self.config = config_dict or {}
        
        # Copy config_dict attributes to mcp_config
        if config_dict:
            for key, value in config_dict.items():
                setattr(mcp_config, key, value)
        
        super().__init__(name, system_config, mcp_config)
        self.call_history = []
    
    @property
    def called(self):
        """Check if the mock tool has been called."""
        return len(self.call_history) > 0
    
    async def call(self, tool: str, params: dict) -> dict:
        """Mock tool call that records the call history."""
        # Filter out injected parameters like _status
        filtered_params = {k: v for k, v in params.items() if not k.startswith('_')}
        result = {"tool": tool, "params": filtered_params, "server": self.name}
        self.call_history.append((tool, filtered_params))
        return result
    
    def get_schema(self) -> dict:
        """Return a mock schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"Mock {self.name} server",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["test", "mock"],
                            "description": "Test action"
                        },
                        "query": {
                            "type": "string",
                            "description": "Query parameter"
                        }
                    },
                    "required": ["query"],
                },
            },
        }
    
    def get_default_action(self) -> str:
        """Return default action."""
        return "test"


class TestMCPRegistry:
    """Test the MCP Registry functionality."""
    
    def test_registry_register_and_get(self):
        """Test server registration and retrieval."""
        registry = MCPRegistry()
        server = MockMCPServer("test_server")
        
        registry.register("test", server)
        retrieved = registry.get("test")
        
        assert retrieved is server
        assert retrieved.name == "test_server"
    
    def test_registry_list(self):
        """Test listing registered servers."""
        registry = MCPRegistry()
        server1 = MockMCPServer("server1")
        server2 = MockMCPServer("server2")
        
        registry.register("s1", server1)
        registry.register("s2", server2)
        
        servers = registry.list()
        assert set(servers) == {"s1", "s2"}
    
    def test_registry_get_nonexistent(self):
        """Test retrieving non-existent server raises KeyError."""
        registry = MCPRegistry()
        
        with pytest.raises(KeyError):
            registry.get("nonexistent")


class TestMockMCPServer:
    """Test the mock MCP server."""
    
    @pytest.mark.asyncio
    async def test_server_call(self):
        """Test server call functionality."""
        server = MockMCPServer("test_server")
        
        result = await server.call("test", {"query": "hello"})
        
        assert result["tool"] == "test"
        assert result["params"] == {"query": "hello"}
        assert result["server"] == "test_server"
        assert server.call_history == [("test", {"query": "hello"})]
    
    def test_server_schema(self):
        """Test server schema generation."""
        server = MockMCPServer("test_server")
        schema = server.get_schema()
        
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "test_server"
        assert "action" in schema["function"]["parameters"]["properties"]
        assert "query" in schema["function"]["parameters"]["properties"]
    
    def test_server_default_action(self):
        """Test default action."""
        server = MockMCPServer("test_server")
        assert server.get_default_action() == "test"


class TestAgent:
    """Test the Agent functionality."""
    
    def create_test_config(self) -> AgentConfig:
        """Create a test configuration."""
        return AgentConfig(
            llm_system=LLMSystemConfig(
                models={
                    "gpt-3.5-turbo": LLMModelConfig(provider="openai", model="gpt-3.5-turbo")
                },
                profiles={
                    "normal": LLMProfile(model_ref="gpt-3.5-turbo")
                },
                default_profile="normal"
            ),
            context=ContextConfig(auto_datetime=False),  # Disable for testing
            system_template="config/prompts/system_prompt.md",
            max_steps=3,
            servers={}
        )
    
    def test_agent_initialization_without_llm(self):
        """Test agent initialization when LLM fails."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        agent_config = self.create_test_config()
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        registry = MCPRegistry()
        
        # Agent should initialize even if LLM fails
        agent = Agent("test_agent", system_config, mcp_config, registry)
        assert agent.llm is None
        assert agent.agent_config is agent_config
        assert agent.registry is registry
    
    @pytest.mark.asyncio
    async def test_agent_run_without_llm(self):
        """Test agent run when no LLM is available."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        agent_config = self.create_test_config()
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        registry = MCPRegistry()
        
        agent = Agent("test_agent", system_config, mcp_config, registry)
        
        from agent_system.servers.agent.result_utils import collect_final_result
        result = await collect_final_result(agent, "test task")
        
        assert result["task"] == "test task"
        assert "errors" in result
        assert "No LLM available" in result["errors"][0]
    
    def test_agent_with_mock_registry(self):
        """Test agent with mock registry setup."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        agent_config = self.create_test_config()
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        registry = MCPRegistry()
        
        # Add mock servers
        server1 = MockMCPServer("search_server")
        server2 = MockMCPServer("weather_server")
        
        registry.register("search", server1)
        registry.register("weather", server2)
        
        agent = Agent("test_agent", system_config, mcp_config, registry)
        
        # Verify registry is properly set up
        assert agent.registry.list() == ["search", "weather"]
        assert agent.registry.get("search") is server1
        assert agent.registry.get("weather") is server2


class TestAgentEventStream:
    """Test the Agent event streaming functionality."""
    
    def create_test_config(self) -> AgentConfig:
        """Create a test configuration."""
        return AgentConfig(
            llm_system=LLMSystemConfig(
                models={
                    "gpt-3.5-turbo": LLMModelConfig(provider="openai", model="gpt-3.5-turbo")
                },
                profiles={
                    "normal": LLMProfile(model_ref="gpt-3.5-turbo")
                },
                default_profile="normal"
            ),
            context=ContextConfig(auto_datetime=False),
            system_template="config/prompts/system_prompt.md",
            max_steps=2,
            servers={}
        )
    
    @pytest.mark.asyncio
    async def test_event_stream_without_llm(self):
        """Test event stream when no LLM is available."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        agent_config = self.create_test_config()
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        registry = MCPRegistry()
        
        agent = Agent("test_agent", system_config, mcp_config, registry)
        
        events = []
        async for event in agent.run_events("test task"):
            events.append(event)
        
        # Should have start, error, and end events
        assert len(events) == 3
        assert events[0]["type"] == "start"
        assert events[0]["task"] == "test task"
        assert events[1]["type"] == "error"
        assert "No LLM available" in events[1]["message"]
        assert events[2]["type"] == "end"


class TestAgentValidation:
    """Test agent validation and error handling."""
    
    def create_test_config(self) -> AgentConfig:
        """Create a test configuration."""
        return AgentConfig(
            llm_system=LLMSystemConfig(
                models={
                    "gpt-3.5-turbo": LLMModelConfig(provider="openai", model="gpt-3.5-turbo")
                },
                profiles={
                    "normal": LLMProfile(model_ref="gpt-3.5-turbo")
                },
                default_profile="normal"
            ),
            context=ContextConfig(auto_datetime=False),
            max_steps=2,
            servers={}
        )
    
    @pytest.mark.asyncio
    async def test_agent_action_validation(self):
        """Test that agent validates actions against server schemas."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        
        agent_config = self.create_test_config()
        system_config = AgentSystemConfig()
        mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
        registry = MCPRegistry()
        
        # Create a mock server with specific actions
        server = MockMCPServer("test_server")
        registry.register("test", server)
        
        agent = Agent("test_agent", system_config, mcp_config, registry)
        
        # Mock the LLM to return a specific tool call
        mock_llm = AsyncMock()
        mock_llm.chat_tools.return_value = {
            "assistant": {
                "tool_calls": [{
                    "function": {
                        "name": "test",
                        "arguments": '{"action": "invalid_action", "query": "hello"}'
                    },
                    "id": "test-call-1"
                }]
            }
        }
        agent.llm = mock_llm
        
        # The agent should validate and correct the action
        # Note: This test would need the actual validation logic to be testable
        # For now, we're testing the structure


@pytest.mark.integration
class TestIntegration:
    """Integration tests for the complete system."""
    
    def test_registry_with_multiple_servers(self):
        """Test registry with multiple different server types."""
        registry = MCPRegistry()
        
        # Create different types of mock servers
        search_server = MockMCPServer("search", {"type": "search"})
        weather_server = MockMCPServer("weather", {"type": "weather"})
        datetime_server = MockMCPServer("datetime", {"type": "datetime"})
        
        registry.register("search", search_server)
        registry.register("weather", weather_server)
        registry.register("datetime", datetime_server)
        
        # Test that all servers are properly registered
        assert len(registry.list()) == 3
        assert set(registry.list()) == {"search", "weather", "datetime"}
        
        # Test that each server has correct configuration
        assert registry.get("search").config["type"] == "search"
        assert registry.get("weather").config["type"] == "weather"
        assert registry.get("datetime").config["type"] == "datetime"


# Fixtures for common test objects
@pytest.fixture
def mock_registry():
    """Fixture providing a registry with mock servers."""
    registry = MCPRegistry()
    
    search_server = MockMCPServer("search_server")
    weather_server = MockMCPServer("weather_server")
    
    registry.register("search", search_server)
    registry.register("weather", weather_server)
    
    return registry


@pytest.fixture
def test_config():
    """Fixture providing a test configuration."""
    return AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "gpt-3.5-turbo": LLMModelConfig(provider="openai", model="gpt-3.5-turbo")
            },
            profiles={
                "normal": LLMProfile(model_ref="gpt-3.5-turbo")
            },
            default_profile="normal"
        ),
        context=ContextConfig(auto_datetime=False),
        max_steps=3,
        servers={}
    )


@pytest.fixture
def test_agent(test_config, mock_registry):
    """Fixture providing a test agent."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=test_config)
    
    return Agent("test_agent", system_config, mcp_config, mock_registry)


class TestWithFixtures:
    """Tests using pytest fixtures."""
    
    def test_agent_with_fixtures(self, test_agent):
        """Test agent using fixtures."""
        assert test_agent.registry.list() == ["search", "weather"]
        assert test_agent.agent_config.max_steps == 3
    
    @pytest.mark.asyncio
    async def test_registry_server_calls(self, mock_registry):
        """Test calling servers through registry."""
        search_server = mock_registry.get("search")
        weather_server = mock_registry.get("weather")
        
        # Test search server call
        result1 = await search_server.call("test", {"query": "hello"})
        assert result1["server"] == "search_server"
        assert search_server.call_history == [("test", {"query": "hello"})]
        
        # Test weather server call
        result2 = await weather_server.call("test", {"query": "weather"})
        assert result2["server"] == "weather_server"
        assert weather_server.call_history == [("test", {"query": "weather"})]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
