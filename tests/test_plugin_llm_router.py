"""
New tests for LLM Router Plugin with Multi-Tool support
"""

from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch

from agent_system.plugins import discover_all_plugins
from plugins.llm_router.server import LLMRouterServer


@pytest.mark.asyncio
async def test_llm_router_plugin_discovered():
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
    inst = factory('llm_router', {})
    assert inst is not None


class TestLLMRouterServerNew:
    """Test the new multi-tool LLM router server functionality."""

    @pytest.mark.asyncio
    async def test_server_initialization(self):
        """Test server initializes correctly."""
        server = LLMRouterServer("llm_router", {}, True)
        assert server.name == "llm_router"
        assert server.ssl_verify is True
        assert server.config == {}

    @pytest.mark.asyncio
    async def test_server_with_parent_llm_config(self):
        """Test server with parent LLM configuration."""
        config = {
            "parent_llm": {
                "llm": {"provider": "openai", "model": "gpt-5-nano"},
                "llm_system": {
                    "profiles": {
                        "turbo": {"provider": "openai", "model": "gpt-5-nano"},
                        "normal": {"provider": "openai", "model": "gpt-5-nano"}
                    },
                    "models": {
                        "gpt-5-nano": {"provider": "openai"},
                        "gpt-4o": {"provider": "openai"}
                    }
                }
            }
        }
        server = LLMRouterServer("llm_router", config, True)
        assert server.parent_llm == config["parent_llm"]

    @pytest.mark.asyncio
    async def test_get_tools_structure(self):
        """Test that get_tools returns correct multi-tool structure."""
        config = {
            "parent_llm": {
                "llm_system": {
                    "profiles": {"test": {"provider": "openai", "model": "gpt-5-nano"}},
                    "models": {"gpt-5-nano": {"provider": "openai"}}
                }
            }
        }
        server = LLMRouterServer("llm_router", config, True)
        tools = server.get_tools()

        # Should have 2 tools: chat and list_profiles
        assert len(tools) == 2
        
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "chat_agent" in tool_names
        assert "list_profiles" in tool_names

        # Verify chat_agent tool structure
        chat_tool = next(tool for tool in tools if tool["function"]["name"] == "chat_agent")
        assert chat_tool["type"] == "function"
        assert "description" in chat_tool["function"]
        
        chat_params = chat_tool["function"]["parameters"]
        assert chat_params["type"] == "object"
        assert "messages" in chat_params["properties"]
        assert "profile" in chat_params["properties"]
        assert "profile" in chat_params["required"]

    @pytest.mark.asyncio
    async def test_chat_tool_missing_profile(self):
        """Test chat tool with missing profile parameter."""
        server = LLMRouterServer("llm_router", {}, True)
        mock_status = AsyncMock()
        
        result = await server.call("chat_agent", {"message": "Hello", "_status": mock_status})
        assert "error" in result
        assert "Profile parameter is required" in result["error"]

    @pytest.mark.asyncio
    async def test_chat_tool_missing_message(self):
        """Test chat tool with missing message."""
        server = LLMRouterServer("llm_router", {}, True)
        mock_status = AsyncMock()
        
        result = await server.call("chat_agent", {"profile": "test", "_status": mock_status})
        assert "error" in result
        assert "No message or messages provided" in result["error"]

    @pytest.mark.asyncio
    async def test_list_profiles_tool(self):
        """Test list_profiles tool functionality."""
        config = {
            "parent_llm": {
                "llm_system": {
                    "profiles": {
                        "turbo": {"provider": "openai", "model": "gpt-4o-mini"},
                        "normal": {"provider": "openai", "model": "gpt-4o"}
                    },
                    "models": {
                        "gpt-4o-mini": {"provider": "openai"},
                        "gpt-4o": {"provider": "openai"}
                    }
                }
            }
        }
        server = LLMRouterServer("llm_router", config, True)
        mock_status = AsyncMock()
        
        result = await server.call("list_profiles", {"_status": mock_status})
        
        assert "profiles" in result
        assert "total_count" in result
        assert result["total_count"] == 2
        
        profiles = result["profiles"]
        assert "turbo" in profiles
        assert "normal" in profiles

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Test calling unknown tool returns error."""
        server = LLMRouterServer("llm_router", {}, True)
        
        with pytest.raises(ValueError, match="Unknown tool"):
            await server.call("unknown_tool", {"_status": AsyncMock()})

    @pytest.mark.asyncio
    async def test_default_action(self):
        """Test default action is chat."""
        server = LLMRouterServer("llm_router", {}, True)
        assert server.get_default_action() == "chat_agent"

    @pytest.mark.asyncio
    async def test_chat_with_profile_success(self):
        """Test successful chat with profile (mocked)."""
        config = {
            "parent_llm": {
                "llm": {"provider": "openai", "model": "gpt-5-nano"},
                "llm_system": {
                    "profiles": {"test": {"provider": "openai", "model": "gpt-5-nano"}},
                    "models": {"gpt-5-nano": {"provider": "openai"}}
                }
            }
        }
        server = LLMRouterServer("llm_router", config, True)
        mock_status = AsyncMock()
        
        # Mock the make_client method to return a mock client
        mock_client = AsyncMock()
        mock_client.chat.return_value = "Mocked response"
        mock_client.provider = "openai"
        mock_client.model = "gpt-5-nano"
        
        with patch.object(server, '_make_client', return_value=mock_client):
            result = await server.call("chat_agent", {
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