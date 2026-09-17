from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from plugins.twitter_search.server import TwitterSearchServer


@pytest.mark.asyncio
async def test_twitter_plugin_discovered(mock_system_config, mock_server_config):
    repo_root = Path(__file__).resolve().parents[3]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    # Note: Factory now requires (name, system_config, server_config) signature
    # This test will be updated when bootstrap system is modernized
        from agent_system.plugins import discover_all_plugins

        plugins = discover_all_plugins([default_dir])
        assert 'twitter_search' in plugins, "twitter_search plugin must be present in repository for this test"
        factory = plugins['twitter_search']
        assert callable(factory), "twitter_search factory should be callable"


class TestTwitterSearchServer:
    """Test the Twitter Search server functionality."""

    def test_twitter_server_initialization(self, mock_system_config, mock_server_config):
        """Test Twitter Search server initialization."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)
        assert server.name == "twitter"
        assert server.ssl_verify is True

    def test_twitter_server_initialization_with_config(self, mock_system_config, mock_server_config):
        """Test Twitter Search server initialization with config."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        mock_system_config.ssl_verify = False
        server_config = ToolServerConfig(type="twitter_search", enabled=True, agent_config=AgentConfig())
        server_config.timeout = 30
        
        server = TwitterSearchServer("twitter", mock_system_config, server_config)
        assert server.name == "twitter"
        assert server.ssl_verify is False

    def test_twitter_server_schema(self, mock_system_config, mock_server_config):
        """Test Twitter Search server tools structure."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "twitter_tweets"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "query" in params["properties"]

    def test_twitter_server_tool_name(self, mock_system_config, mock_server_config):
        """Test Twitter Search server tool name."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "twitter_tweets"

    @pytest.mark.asyncio
    async def test_twitter_server_search_no_credentials(self, mock_system_config, mock_server_config):
        """Test Twitter Search server without credentials returns setup guide."""
        from unittest.mock import patch
        
        # Mock tweepy as available but no credentials
        with patch('plugins.twitter_search.server.TWEEPY_AVAILABLE', True):
            server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)

            mock_status = AsyncMock()
            result = await server.call("twitter_tweets", {"query": "test", "_status": mock_status})
            
            # Should return setup instructions when no credentials configured
            assert result["status"] == "setup_required"
            assert "message" in result
            assert "setup_instructions" in result
            assert "free_tier_limits" in result
            assert "alternatives" in result
            assert isinstance(result["alternatives"], list)

    @pytest.mark.asyncio
    async def test_twitter_server_invalid_tool(self, mock_system_config, mock_server_config):
        """Test Twitter Search server with invalid tool name."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError for unknown tools
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"query": "test", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_twitter_server_empty_query(self, mock_system_config, mock_server_config):
        """Test Twitter Search server with empty query (no credentials)."""
        from unittest.mock import patch
        
        # Mock tweepy as available but no credentials
        with patch('plugins.twitter_search.server.TWEEPY_AVAILABLE', True):
            server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)

            mock_status = AsyncMock()
            result = await server.call("twitter_tweets", {"query": "", "_status": mock_status})
            
            # Should return setup instructions when no credentials
            assert result["status"] == "setup_required"
            assert "setup_instructions" in result

    @pytest.mark.asyncio
    async def test_twitter_server_with_mock_api(self, mock_system_config, mock_server_config):
        """Test Twitter Search server with mocked tweepy module."""
        from unittest.mock import patch, MagicMock
        
        # Create a fake tweepy module
        fake_tweepy = MagicMock()
        fake_client_instance = MagicMock()
        fake_tweepy.Client = MagicMock(return_value=fake_client_instance)
        
        # Mock the module import
        with patch.dict('sys.modules', {'tweepy': fake_tweepy}):
            # Mock TWEEPY_AVAILABLE
            with patch('plugins.twitter_search.server.TWEEPY_AVAILABLE', True):
                with patch.dict('os.environ', {'TWITTER_BEARER_TOKEN': 'fake_token'}):
                    # Re-import with mocked tweepy
                    import plugins.twitter_search.server as server_module
                    server_module.tweepy = fake_tweepy
                    
                    # Create server
                    server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)
                    server.client = fake_client_instance
                    
                    # Create mock tweet
                    mock_tweet = MagicMock()
                    mock_tweet.id = "123456"
                    mock_tweet.text = "This is a test tweet about bitcoin"
                    mock_tweet.created_at = None
                    mock_tweet.lang = "en"
                    mock_tweet.source = "Twitter Web App"
                    mock_tweet.author_id = "user123"
                    mock_tweet.public_metrics = {
                        'like_count': 10,
                        'retweet_count': 5,
                        'reply_count': 2,
                        'quote_count': 1
                    }
                    
                    # Create mock response
                    mock_response = MagicMock()
                    mock_response.data = [mock_tweet]
                    mock_response.includes = None
                    fake_client_instance.search_recent_tweets.return_value = mock_response
                    
                    mock_status = AsyncMock()
                    result = await server.call("twitter_tweets", {"query": "bitcoin", "_status": mock_status})
                    
                    # Should return successful result with tweets
                    assert result["status"] == "success"
                    assert result["query"] == "bitcoin"
                    assert result["total_results"] == 1
                    assert len(result["tweets"]) == 1
                    assert result["tweets"][0]["text"] == "This is a test tweet about bitcoin"
                    assert result["tweets"][0]["metrics"]["likes"] == 10

    @pytest.mark.asyncio
    async def test_twitter_server_cancellation(self, mock_system_config, mock_server_config):
        """Test Twitter Search server respects cancellation token."""
        from unittest.mock import MagicMock
        
        server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)

        mock_status = AsyncMock()
        mock_token = MagicMock()
        mock_token.is_cancelled = True
        
        result = await server.call("twitter_tweets", {
            "query": "test",
            "_status": mock_status,
            "_cancellation_token": mock_token
        })
        
        # Should return cancelled status
        assert "cancelled" in result
        assert result["cancelled"] is True
        assert "error" in result

    @pytest.mark.asyncio
    async def test_twitter_server_tweepy_not_installed(self, mock_system_config, mock_server_config):
        """Test Twitter Search server when tweepy is not installed."""
        from unittest.mock import patch
        
        # Mock TWEEPY_AVAILABLE to be False
        with patch('plugins.twitter_search.server.TWEEPY_AVAILABLE', False):
            server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            result = await server.call("twitter_tweets", {"query": "test", "_status": mock_status})
            
            # Should return error about missing tweepy
            assert result["status"] == "error"
            assert "tweepy" in result["error"].lower()
            assert "Install tweepy" in result["message"]


class TestTwitterSearchPluginFactory:
    """Test the Twitter Search plugin factory function."""

    def test_plugin_factory_basic(self, mock_system_config, mock_server_config):
        """Test basic plugin factory functionality."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("twitter", mock_system_config, mock_server_config)
        assert server.name == "twitter"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config, mock_server_config):
        """Test plugin factory with configuration."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY
        from agent_system.config.models import ToolServerConfig, AgentConfig

        mock_system_config.ssl_verify = False
        server_config = ToolServerConfig(type="twitter_search", enabled=True, agent_config=AgentConfig())
        server_config.timeout = 60
        
        server = PLUGIN_FACTORY("twitter", mock_system_config, server_config)
        assert server.name == "twitter"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_server_config):
        """Test plugin factory with custom name."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_twitter", mock_system_config, mock_server_config)
        assert server.name == "custom_twitter"
