"""
New tests for LLM Router Plugin with Multi-Tool support
"""

from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch

from agent_system.plugins import discover_all_plugins
from plugins.llm_router.server import LLMRouterServer


@pytest.mark.asyncio
async def test_llm_router_plugin_discovered(mock_system_config, mock_mcp_config):
    """Test that LLM router plugin is discovered correctly."""
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'llm_router' in plugins
    factory = plugins['llm_router']
    inst = factory('llm_router', mock_system_config, mock_mcp_config)
    assert inst is not None


class TestLLMRouterServerNew:
    """Test the new multi-tool LLM router server functionality."""

    @pytest.mark.asyncio
    async def test_server_initialization(self, mock_system_config, mock_mcp_config):
        """Test server initializes correctly."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        assert server.name == "llm_router"
        # ssl_verify is not relevant for llm_router (no HTTP requests)
        assert hasattr(server, 'llm_config')

    @pytest.mark.asyncio
    async def test_server_with_parent_llm_config(self, mock_system_config, mock_mcp_config):
        """Test server with parent LLM configuration."""
        from types import SimpleNamespace
        
        # Create mock llm_system
        mock_llm_system = SimpleNamespace(
            profiles={
                "turbo": {"provider": "openai", "model": "gpt-5-nano"},
                "normal": {"provider": "openai", "model": "gpt-5-nano"}
            },
            models={
                "gpt-5-nano": {"provider": "openai"},
                "gpt-4o": {"provider": "openai"}
            }
        )
        mock_system_config.llm_system = mock_llm_system
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        assert server.llm_config == mock_llm_system

    @pytest.mark.asyncio
    async def test_get_tools_structure(self, mock_system_config, mock_mcp_config):
        """Test that get_tools returns correct multi-tool structure."""
        from types import SimpleNamespace
        
        mock_llm_system = SimpleNamespace(
            profiles={"test": {"provider": "openai", "model": "gpt-5-nano"}},
            models={"gpt-5-nano": {"provider": "openai"}}
        )
        mock_system_config.llm_system = mock_llm_system
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        # Should have 2 tools: chat and list_profiles
        assert len(tools) == 2
        
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "llm_router_chat" in tool_names
        assert "llm_router_list_profiles" in tool_names

        # Verify chat_agent tool structure
        chat_tool = next(tool for tool in tools if tool["function"]["name"] == "llm_router_chat")
        assert chat_tool["type"] == "function"
        assert "description" in chat_tool["function"]
        
        chat_params = chat_tool["function"]["parameters"]
        assert chat_params["type"] == "object"
        assert "messages" in chat_params["properties"]
        assert "profile" in chat_params["properties"]
        assert "profile" in chat_params["required"]

    @pytest.mark.asyncio
    async def test_chat_tool_missing_profile(self, mock_system_config, mock_mcp_config):
        """Test chat tool with missing profile parameter."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        mock_status = AsyncMock()
        
        result = await server.call("llm_router_chat", {"message": "Hello", "_status": mock_status})
        assert "error" in result
        assert "Profile parameter is required" in result["error"]

    @pytest.mark.asyncio
    async def test_chat_tool_missing_message(self, mock_system_config, mock_mcp_config):
        """Test chat tool with missing message."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        mock_status = AsyncMock()
        
        result = await server.call("llm_router_chat", {"profile": "test", "_status": mock_status})
        assert "error" in result
        assert "No message or messages provided" in result["error"]

    @pytest.mark.asyncio
    async def test_list_profiles_tool(self, mock_system_config, mock_mcp_config):
        """Test list_profiles tool functionality."""
        from types import SimpleNamespace
        
        mock_llm_system = SimpleNamespace(
            profiles={
                "turbo": {"provider": "openai", "model": "gpt-4o-mini"},
                "normal": {"provider": "openai", "model": "gpt-4o"}
            },
            models={
                "gpt-4o-mini": {"provider": "openai"},
                "gpt-4o": {"provider": "openai"}
            }
        )
        mock_system_config.llm_system = mock_llm_system
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        mock_status = AsyncMock()
        
        result = await server.call("llm_router_list_profiles", {"_status": mock_status})
        
        assert "profiles" in result
        assert "total_count" in result
        assert result["total_count"] == 2
        
        profiles = result["profiles"]
        assert "turbo" in profiles
        assert "normal" in profiles

    @pytest.mark.asyncio
    async def test_unknown_tool(self, mock_system_config, mock_mcp_config):
        """Test calling unknown tool raises ValueError."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        
        with pytest.raises(ValueError, match="not found"):
            await server.call("unknown_tool", {"_status": AsyncMock()})

    # get_default_action() removed in modernization; dispatcher handles routing.
    # Old default-action test removed as obsolete.

    @pytest.mark.asyncio
    async def test_chat_with_profile_success(self, mock_system_config, mock_mcp_config):
        """Test successful chat with profile (mocked)."""
        from types import SimpleNamespace
        
        mock_llm_system = SimpleNamespace(
            profiles={"test": {"provider": "openai", "model": "gpt-5-nano"}},
            models={"gpt-5-nano": {"provider": "openai"}}
        )
        mock_system_config.llm_system = mock_llm_system
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_mcp_config)
        mock_status = AsyncMock()
        
        # Mock the make_client method to return a mock client
        mock_client = AsyncMock()
        mock_client.chat.return_value = "Mocked response"
        mock_client.provider = "openai"
        mock_client.model = "gpt-5-nano"
        
        with patch.object(server, '_make_client', return_value=mock_client):
            result = await server.call("llm_router_chat", {
                "message": "Hello world",
                "profile": "test",
                "_status": mock_status
            })
            
            assert "content" in result
            assert result["content"] == "Mocked response"
            assert result["profile"] == "test" 
            assert result["provider"] == "openai"
            assert result["model"] == "gpt-5-nano"
            
            # Verify chat was called
            mock_client.chat.assert_called_once()