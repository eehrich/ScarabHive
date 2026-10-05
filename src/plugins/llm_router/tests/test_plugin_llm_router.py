"""
New tests for LLM Router Plugin with Multi-Tool support
"""

import pytest
from unittest.mock import AsyncMock, patch

from plugins.llm_router.server import LLMRouterServer





class TestLLMRouterServerNew:
    """Test the new multi-tool LLM router server functionality."""

    @pytest.mark.asyncio
    async def test_server_initialization(self, mock_system_config, mock_server_config):
        """Test server initializes correctly."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        assert server.name == "llm_router"
        # ssl_verify is not relevant for llm_router (no HTTP requests)
        assert hasattr(server, 'llm_config')

    @pytest.mark.asyncio
    async def test_server_with_parent_llm_config(self, mock_system_config, mock_server_config):
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
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        assert server.llm_config == mock_llm_system

    @pytest.mark.asyncio
    async def test_get_tools_structure(self, mock_system_config, mock_server_config):
        """Test that get_tools returns correct multi-tool structure."""
        from types import SimpleNamespace
        
        mock_llm_system = SimpleNamespace(
            profiles={"test": {"provider": "openai", "model": "gpt-5-nano"}},
            models={"gpt-5-nano": {"provider": "openai"}}
        )
        mock_system_config.llm_system = mock_llm_system
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
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
    async def test_chat_tool_missing_profile(self, mock_system_config, mock_server_config):
        """Test chat tool with missing profile parameter."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        mock_status = AsyncMock()
        
        result = await server.call("llm_router_chat", {"message": "Hello", "_status": mock_status})
        assert "error" in result
        assert "Profile parameter is required" in result["error"]

    @pytest.mark.asyncio
    async def test_chat_tool_missing_message(self, mock_system_config, mock_server_config):
        """Test chat tool with missing message."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        mock_status = AsyncMock()
        
        result = await server.call("llm_router_chat", {"profile": "test", "_status": mock_status})
        assert "error" in result
        assert "No message or messages provided" in result["error"]

    @pytest.mark.asyncio
    async def test_list_profiles_tool(self, mock_system_config, mock_server_config):
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
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        mock_status = AsyncMock()
        
        result = await server.call("llm_router_list_profiles", {"_status": mock_status})
        
        assert "profiles" in result
        assert "total_count" in result
        assert result["total_count"] == 2
        
        profiles = result["profiles"]
        assert "turbo" in profiles
        assert "normal" in profiles
        # Tool results cost tokens: no copy of the profile entry, no max_steps
        # the router's single call never uses.
        assert set(profiles["turbo"]) == {"description", "model_ref", "provider", "model"}

    @pytest.mark.asyncio
    async def test_unknown_tool(self, mock_system_config, mock_server_config):
        """Test calling unknown tool raises ValueError."""
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        
        with pytest.raises(ValueError, match="not found"):
            await server.call("unknown_tool", {"_status": AsyncMock()})

    # get_default_action() removed in modernization; dispatcher handles routing.
    # Old default-action test removed as obsolete.

    @pytest.mark.asyncio
    async def test_chat_with_profile_success(self, mock_system_config, mock_server_config):
        """Test successful chat with profile (mocked)."""
        from types import SimpleNamespace
        
        mock_llm_system = SimpleNamespace(
            profiles={"test": {"model_ref": "nano"}},
            models={"nano": {"provider": "openai_httpx", "model": "gpt-5-nano"}}
        )
        mock_system_config.llm_system = mock_llm_system
        
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        mock_status = AsyncMock()
        
        # Mock the make_client method to return a mock client. The answer's
        # provider comes from the model entry, not from what the client
        # calls itself (openai_httpx's client says "openai").
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
            assert result["provider"] == "openai_httpx"
            assert result["model"] == "gpt-5-nano"
            
            # Verify chat was called
            mock_client.chat.assert_called_once()

class TestLLMRouterCancellation:
    """A cancelled request must not look like a forced tool kill."""

    @staticmethod
    def _server(mock_system_config, mock_server_config):
        from types import SimpleNamespace

        mock_system_config.llm_system = SimpleNamespace(
            profiles={"test": {"provider": "openai", "model": "gpt-5-nano"}},
            models={"gpt-5-nano": {"provider": "openai"}})
        return LLMRouterServer("llm_router", mock_system_config, mock_server_config)

    @pytest.mark.asyncio
    async def test_a_cancelled_chat_answers_instead_of_escaping(self, mock_system_config, mock_server_config):
        """An escaping CancelledError is reported as "force-cancelled" by the agent server."""
        import asyncio

        from agent_system.core.cancellation import CancellationToken

        server = self._server(mock_system_config, mock_server_config)
        mock_status = AsyncMock()
        mock_client = AsyncMock()
        token = CancellationToken("req-1")

        async def cancel_then_raise(*args, **kwargs):
            token.cancel()
            raise asyncio.CancelledError("Request cancelled by user")

        mock_client.chat.side_effect = cancel_then_raise

        with patch.object(server, "_make_client", return_value=mock_client):
            result = await server.call("llm_router_chat", {
                "message": "Hello", "profile": "test",
                "_status": mock_status, "_cancellation_token": token,
            })

        assert result["cancelled"] is True
        # Ending the scope would report the cancel as a completed call; the
        # tool base reports the error result instead (as it does for the
        # cancel the check before the call answers).
        mock_status.end.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancel_the_user_did_not_ask_for_still_escapes(self, mock_system_config, mock_server_config):
        import asyncio

        from agent_system.core.cancellation import CancellationToken

        server = self._server(mock_system_config, mock_server_config)
        mock_client = AsyncMock()
        mock_client.chat.side_effect = asyncio.CancelledError()

        with patch.object(server, "_make_client", return_value=mock_client):
            with pytest.raises(asyncio.CancelledError):
                await server.call("llm_router_chat", {
                    "message": "Hello", "profile": "test",
                    "_status": AsyncMock(), "_cancellation_token": CancellationToken("req-2"),
                })

    @pytest.mark.asyncio
    async def test_the_answer_says_whether_the_cancel_was_forced(self, mock_system_config, mock_server_config):
        """docs/plugin_authoring.md defines the shape: error, cancelled, forced."""
        from agent_system.core.cancellation import CancellationToken

        server = self._server(mock_system_config, mock_server_config)
        token = CancellationToken("req-3")
        token.cancel()

        result = await server.call("llm_router_chat", {
            "message": "Hello", "profile": "test",
            "_status": AsyncMock(), "_cancellation_token": token,
        })

        assert result == {"error": "LLM routing request cancelled by user",
                          "cancelled": True, "forced": False}


class TestLLMRouterChatArguments:
    """A bad argument answers an error; nothing empty reaches the provider."""

    @staticmethod
    async def _call(mock_system_config, mock_server_config, **args):
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        client = AsyncMock()
        client.chat.return_value = "ok"
        with patch.object(server, "_make_client", return_value=client):
            result = await server.call("llm_router_chat", {
                "profile": "test", "_status": AsyncMock(), **args})
        return result, client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [
        {"messages": [{"content": "no role"}]},
        {"messages": ["plain text"]},
        {"messages": "plain text"},
    ])
    async def test_malformed_messages_answer_an_error(self, mock_system_config, mock_server_config, args):
        result, client = await self._call(mock_system_config, mock_server_config, **args)
        assert "{role, content}" in result["error"]
        client.chat.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [
        {"message": ""},
        {"messages": []},
        {"messages": None},
        {"message": 5},
    ])
    async def test_an_empty_prompt_is_a_missing_one(self, mock_system_config, mock_server_config, args):
        result, client = await self._call(mock_system_config, mock_server_config, **args)
        assert result == {"error": "No message or messages provided"}
        client.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_messages_fall_back_to_message(self, mock_system_config, mock_server_config):
        result, client = await self._call(
            mock_system_config, mock_server_config, messages=None, message="hi")
        assert result["content"] == "ok"
        assert client.chat.call_args.args[0][0].content == "hi"

    @pytest.mark.asyncio
    async def test_content_parts_are_passed_on(self, mock_system_config, mock_server_config):
        parts = [{"type": "text", "text": "hi"}]
        result, client = await self._call(
            mock_system_config, mock_server_config,
            messages=[{"role": "user", "content": parts}])
        assert result["content"] == "ok"
        assert client.chat.call_args.args[0][0].get_text_content() == "hi"


class TestLLMRouterHooks:
    """The router's requests reach the hooks as agent-less calls (llm/hook_notify.py)."""

    @pytest.mark.asyncio
    async def test_requests_reach_the_hook_registry_without_an_agent(
            self, mock_system_config, mock_server_config, monkeypatch):
        from agent_system.hooks import get_hook_registry
        from agent_system.llm.models import LLMClient

        usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}

        class FakeClient(LLMClient):
            provider = "openai"
            model = "some/model"

            async def chat(self, messages, cancellation_token=None, status_scope=None):
                await self._notify_pre_request(
                    {"provider": "openai_httpx", "model": self.model, "url": "http://127.0.0.1:9/x",
                     "payload": {"messages": []}})
                await self._notify_post_response(
                    {"provider": "openai_httpx", "model": self.model, "url": "http://127.0.0.1:9/x",
                     "duration_ms": 12.0, "usage": usage, "finish_reason": "stop"})
                return "ok"

        seen = []

        async def capture(hook_type, context, **kwargs):
            seen.append((hook_type.name, context))
            return []

        monkeypatch.setattr(get_hook_registry(), "execute_hooks", capture)
        server = LLMRouterServer("llm_router", mock_system_config, mock_server_config)
        with patch.object(server, "_make_client", return_value=FakeClient()):
            result = await server.call("llm_router_chat", {
                "profile": "test", "message": "hi", "_status": AsyncMock(),
                "_session_id": "s-1", "_agent": object()})

        assert result["content"] == "ok"
        assert [name for name, _ in seen] == ["PRE_LLM_REQUEST", "POST_LLM_RESPONSE"]
        post = seen[1][1]
        # No agent: that is what makes the usage tracker book it, once.
        assert post.agent is None
        assert post.llm_usage == usage
        assert post.session_id == "s-1"
        assert post.llm_model == "some/model"


class TestLLMRouterCLI:
    """python -m plugins.llm_router builds the server from the loaded config."""

    def test_a_message_goes_to_the_profile(self, monkeypatch, capsys):
        from types import SimpleNamespace

        import agent_system.config.settings as settings
        from plugins.llm_router import __main__ as cli

        config = SimpleNamespace(
            network=None,
            llm_system=SimpleNamespace(
                profiles={"fast": {"model_ref": "m"}},
                models={"m": {"provider": "openai_httpx", "model": "some/model"}}))
        monkeypatch.setattr(settings, "load_settings", lambda path=None: config)
        client = AsyncMock()
        client.chat.return_value = "pong"
        client.model = "some/model"
        monkeypatch.setattr(LLMRouterServer, "_make_client", lambda self, profile: client)

        with pytest.raises(SystemExit) as exit_info:
            cli.cli_main(["--profile", "fast", "--message", "ping"])

        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert "fast: openai_httpx/some/model" in out
        assert "pong" in out
        assert client.chat.call_args.args[0][0].content == "ping"

    def test_without_a_profile_it_refuses(self, monkeypatch, capsys):
        import agent_system.config.settings as settings
        from plugins.llm_router import __main__ as cli

        # Never the real config (and its secrets), even when the check fails.
        monkeypatch.setattr(settings, "load_settings", lambda path=None: pytest.fail("config loaded"))

        with pytest.raises(SystemExit) as exit_info:
            cli.cli_main(["--message", "ping"])
        assert exit_info.value.code == 2
        assert "--profile" in capsys.readouterr().err
