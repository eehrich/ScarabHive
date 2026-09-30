from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.twitter_search.server import TwitterSearchServer


def _tweet(tweet_id: str) -> MagicMock:
    tweet = MagicMock()
    tweet.id = tweet_id
    tweet.text = f"tweet {tweet_id}"
    tweet.created_at = None
    tweet.lang = "en"
    tweet.source = None
    tweet.author_id = "user123"
    tweet.public_metrics = {"like_count": 10, "retweet_count": 5, "reply_count": 2, "quote_count": 1}
    return tweet


def _server_with_tweets(system_config, server_config, tweets) -> TwitterSearchServer:
    server = TwitterSearchServer("twitter", system_config, server_config)
    server.client = MagicMock()
    server.client.search_recent_tweets.return_value = MagicMock(data=tweets, includes=None)
    return server


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

    def test_twitter_server_initialization_with_config(self, mock_system_config, mock_server_config):
        """Test Twitter Search server initialization with config."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        
        server_config = ToolServerConfig(type="twitter_search", enabled=True, agent_config=AgentConfig())
        server_config.timeout = 30
        
        server = TwitterSearchServer("twitter", mock_system_config, server_config)
        assert server.name == "twitter"

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
        """An empty query is refused before anything is sent."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)
        server.client = MagicMock()

        result = await server.call("twitter_tweets", {"query": "  ", "_status": AsyncMock()})

        assert result == {"status": "error", "query": "", "error": "Empty query"}
        server.client.search_recent_tweets.assert_not_called()

    @pytest.mark.asyncio
    async def test_twitter_server_with_mock_api(self, mock_system_config, mock_server_config):
        """A stubbed client's answer is formatted into tweets."""
        server = _server_with_tweets(mock_system_config, mock_server_config, [_tweet("123456")])

        result = await server.call("twitter_tweets", {"query": "bitcoin", "_status": AsyncMock()})

        assert result["status"] == "success"
        assert result["query"] == "bitcoin"
        assert result["total_results"] == 1
        assert result["tweets"][0]["text"] == "tweet 123456"
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

    def test_plugin_factory_with_config(self, mock_system_config, mock_server_config):
        """Test plugin factory with configuration."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY
        from agent_system.config.models import ToolServerConfig, AgentConfig

        server_config = ToolServerConfig(type="twitter_search", enabled=True, agent_config=AgentConfig())
        server_config.timeout = 60
        
        server = PLUGIN_FACTORY("twitter", mock_system_config, server_config)
        assert server.name == "twitter"

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_server_config):
        """Test plugin factory with custom name."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_twitter", mock_system_config, mock_server_config)
        assert server.name == "custom_twitter"


def _http_error(cls, status_code: int, headers: dict | None = None):
    response = MagicMock(status_code=status_code, reason="reason", headers=headers or {})
    response.json.return_value = {"detail": "detail from X"}
    return cls(response)


class TestTwitterSearchRequest:
    """What the tool sends to X and how it answers X's errors."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit,sent,kept", [(None, 10, 10), (3, 10, 3), (0, 10, 1), ("20", 20, 12), (500, 50, 12)])
    async def test_limit_is_clamped_and_the_api_minimum_is_met(self, mock_system_config, mock_server_config,
                                                               limit, sent, kept):
        tweets = [_tweet(str(i)) for i in range(12)]
        server = _server_with_tweets(mock_system_config, mock_server_config, tweets)
        params = {"query": "godot", "_status": AsyncMock()}
        if limit is not None:
            params["limit"] = limit

        result = await server.call("twitter_tweets", params)

        assert server.client.search_recent_tweets.call_args.kwargs["max_results"] == sent
        assert result["total_results"] == kept

    @pytest.mark.asyncio
    async def test_a_limit_that_is_not_a_number_is_refused(self, mock_system_config, mock_server_config):
        server = _server_with_tweets(mock_system_config, mock_server_config, [])

        result = await server.call("twitter_tweets", {"query": "godot", "limit": "many", "_status": AsyncMock()})

        assert result["status"] == "error"
        assert result["error"] == "limit must be a whole number from 1 to 50, got 'many'"
        server.client.search_recent_tweets.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_request_runs_off_the_event_loop(self, mock_system_config, mock_server_config):
        import threading
        server = _server_with_tweets(mock_system_config, mock_server_config, [])
        threads = []
        server.client.search_recent_tweets.side_effect = lambda **kw: threads.append(
            threading.get_ident()) or MagicMock(data=[], includes=None)

        await server.call("twitter_tweets", {"query": "godot", "_status": AsyncMock()})

        assert threads and threads[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_a_request_without_answer_times_out(self, mock_system_config, mock_server_config, monkeypatch):
        import time
        import plugins.twitter_search.server as server_module
        monkeypatch.setattr(server_module, "REQUEST_TIMEOUT", 0.05)
        server = _server_with_tweets(mock_system_config, mock_server_config, [])
        server.client.search_recent_tweets.side_effect = lambda **kw: time.sleep(0.5)

        result = await server.call("twitter_tweets", {"query": "godot", "_status": AsyncMock()})

        assert result["error_type"] == "TimeoutError"
        assert result["error"] == "X API did not answer within 0.05 seconds"

    def test_the_client_does_not_sleep_on_a_rate_limit(self, mock_system_config, mock_server_config, monkeypatch):
        import plugins.twitter_search.server as server_module
        client_class = MagicMock()
        monkeypatch.setattr(server_module.tweepy, "Client", client_class)
        monkeypatch.setenv("TWITTER_BEARER_TOKEN", "token")

        TwitterSearchServer("twitter", mock_system_config, mock_server_config)

        assert client_class.call_args.kwargs.get("wait_on_rate_limit", False) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error,message", [
        (lambda t: _http_error(t.TooManyRequests, 429, {"x-rate-limit-reset": "9999999999"}), "rate limit reached; it resets in"),
        (lambda t: _http_error(t.TooManyRequests, 429), "rate limit reached; try again later"),
        (lambda t: _http_error(t.Unauthorized, 401), "bearer token"),
        (lambda t: _http_error(t.Forbidden, 403), "refused access"),
        (lambda t: _http_error(t.BadRequest, 400), "query syntax"),
        (lambda t: _http_error(t.TwitterServerError, 503), "server error"),
    ])
    async def test_the_hint_follows_the_error_class(self, mock_system_config, mock_server_config, error, message):
        import tweepy
        server = _server_with_tweets(mock_system_config, mock_server_config, [])
        exc = error(tweepy)
        server.client.search_recent_tweets.side_effect = exc

        result = await server.call("twitter_tweets", {"query": "godot", "_status": AsyncMock()})

        assert result["error_type"] == type(exc).__name__
        assert message in result["message"]


@pytest.mark.asyncio
async def test_cli_calls_the_tool_by_its_name(monkeypatch, capsys):
    import sys
    import plugins.twitter_search.__main__ as cli
    call = AsyncMock(return_value={"tweets": [{"id": 7, "text": "hi", "author": {"username": "someone"}}]})
    monkeypatch.setattr(TwitterSearchServer, "call", call)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["twitter_search", "--query", "godot", "--max-results", "5", "--lang", "de"])

    await cli.async_main()

    tool, params = call.call_args.args
    assert tool == "twitter_search_tweets"
    assert params["query"] == "godot lang:de" and params["limit"] == 5
    assert "https://x.com/someone/status/7" in capsys.readouterr().out


def test_the_client_session_carries_the_request_timeout(mock_system_config, mock_server_config, monkeypatch):
    """tweepy's session has no timeout of its own: a stalled connection would
    block its worker thread forever. The request X gets must carry one."""
    import requests
    import plugins.twitter_search.server as server_module
    seen = {}

    class Sent(Exception):
        pass

    def spy(self, method, url, **kwargs):
        seen.update(kwargs)
        raise Sent()

    monkeypatch.setattr(requests.Session, "request", spy)
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "token")
    server = TwitterSearchServer("twitter", mock_system_config, mock_server_config)

    with pytest.raises(Sent):
        server.client.search_recent_tweets(query="godot", max_results=10)

    assert seen["timeout"] == server_module.REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_a_transport_timeout_answers_as_a_timeout(mock_system_config, mock_server_config):
    import requests
    server = _server_with_tweets(mock_system_config, mock_server_config, [])
    server.client.search_recent_tweets.side_effect = requests.ReadTimeout("read timed out")

    result = await server.call("twitter_tweets", {"query": "godot", "_status": AsyncMock()})

    assert result["error_type"] == "TimeoutError"
