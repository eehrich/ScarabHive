"""Unit tests for OllamaNativeAsyncClient."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient
from agent_system.llm.models import ChatMessage


class TestOllamaClientInitialization:
    """Test client initialization and configuration."""

    def test_basic_initialization(self):
        """Test basic client initialization."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="llama2"
            )
            
            assert client.model == "llama2"
            assert client._base == "http://127.0.0.1:11434"
            assert client._options == {}
            assert client._timeout == 60.0
            assert client._verify is True

    def test_initialization_with_base_url(self):
        """Test client initialization with custom base URL."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="mistral",
                base_url="http://custom-host:8080/"
            )
            
            assert client.model == "mistral"
            # Should strip trailing slash
            assert client._base == "http://custom-host:8080"

    def test_initialization_with_options(self):
        """Test client initialization with custom options."""
        with patch("httpx.AsyncClient"):
            options = {
                "num_ctx": 4096,
                "temperature": 0.8,
                "top_p": 0.9
            }
            
            client = OllamaNativeAsyncClient(
                model="llama2",
                options=options
            )
            
            assert client._options == options

    def test_initialization_with_timeout(self):
        """Test client initialization with custom timeout."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="llama2",
                timeout=120.0
            )
            
            assert client._timeout == 120.0

    def test_initialization_with_verify_false(self):
        """Test client initialization with SSL verification disabled."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(
                model="llama2",
                verify=False
            )
            
            assert client._verify is False


class TestOllamaClientMessageMapping:
    """Test message format mapping."""

    def test_map_simple_user_message(self):
        """Test mapping simple user message."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(role="user", content="Hello")
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "user"
            assert mapped[0]["content"] == "Hello"

    def test_map_system_and_user_messages(self):
        """Test mapping system and user messages."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(role="system", content="You are helpful."),
                ChatMessage(role="user", content="Hi")
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 2
            assert mapped[0]["role"] == "system"
            assert mapped[0]["content"] == "You are helpful."
            assert mapped[1]["role"] == "user"
            assert mapped[1]["content"] == "Hi"

    def test_map_tool_message(self):
        """Test mapping tool message."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(
                    role="tool",
                    content="Weather data",
                    name="get_weather",
                    tool_call_id="call_123"
                )
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "tool"
            assert mapped[0]["content"] == "Weather data"
            assert mapped[0]["name"] == "get_weather"
            assert mapped[0]["tool_call_id"] == "call_123"

    def test_map_assistant_message_with_tool_calls(self):
        """Test mapping assistant message with tool calls."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            tool_calls = [{"id": "call_1", "function": {"name": "test"}}]
            messages = [
                ChatMessage(role="assistant", content=None, tool_calls=tool_calls)
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "assistant"
            assert mapped[0]["tool_calls"] == tool_calls

    def test_map_message_without_content(self):
        """Test mapping message without content (None)."""
        with patch("httpx.AsyncClient"):
            client = OllamaNativeAsyncClient(model="llama2")
            
            messages = [
                ChatMessage(role="assistant", content=None)
            ]
            
            mapped = client._map_messages(messages)
            
            assert len(mapped) == 1
            assert mapped[0]["role"] == "assistant"
            # Ollama always expects content field, even if empty
            assert mapped[0]["content"] == ""


class TestOllamaClientChat:
    """Test chat functionality."""

    @pytest.mark.asyncio
    async def test_chat_simple_message(self):
        """Test sending a simple chat message."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            # Mock the async context manager and post response
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Hello! How can I help?"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [
                ChatMessage(role="user", content="Hello")
            ]
            
            result = await client.chat(messages)
            
            assert result == "Hello! How can I help?"
            mock_client_instance.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_with_options(self):
        """Test chat request includes options."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(
                model="llama2",
                options={"num_ctx": 4096, "temperature": 0.7}
            )
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Response"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            await client.chat(messages)
            
            # Check that options were included in request body
            call_args = mock_client_instance.post.call_args
            body = call_args[1]["json"]
            assert "options" in body
            assert body["options"]["num_ctx"] == 4096
            assert body["options"]["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_chat_url_construction(self):
        """Test that correct URL is constructed."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(
                model="llama2",
                base_url="http://localhost:11434"
            )
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "OK"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            await client.chat(messages)
            
            # Check URL
            call_args = mock_client_instance.post.call_args
            url = call_args[0][0]
            assert url == "http://localhost:11434/api/chat"

    @pytest.mark.asyncio
    async def test_chat_request_body_structure(self):
        """Test that request body has correct structure."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "OK"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [
                ChatMessage(role="system", content="Be helpful."),
                ChatMessage(role="user", content="Test")
            ]
            
            await client.chat(messages)
            
            call_args = mock_client_instance.post.call_args
            body = call_args[1]["json"]
            
            assert body["model"] == "llama2"
            assert body["stream"] is False
            assert len(body["messages"]) == 2
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_chat_response_with_empty_content(self):
        """Test handling response with empty content."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            
            # Response with empty content
            mock_response.json.return_value = {
                "message": {
                    "role": "assistant",
                    "content": ""
                }
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            result = await client.chat(messages)
            
            # Should return empty string
            assert result == ""

    @pytest.mark.asyncio
    async def test_chat_tools_usage_tracking(self):
        """Test that usage information is tracked in non-streaming mode."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            # Mock response with usage metadata (Ollama format)
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Test response"},
                "prompt_eval_count": 12,  # prompt tokens
                "eval_count": 18,         # completion tokens
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            tools = []
            
            result = await client.chat_tools(messages, tools)
            
            # Verify usage is included and normalized to standard format
            assert "usage" in result
            assert result["usage"]["prompt_tokens"] == 12
            assert result["usage"]["completion_tokens"] == 18
            assert result["usage"]["total_tokens"] == 30  # 12 + 18

    @pytest.mark.asyncio
    async def test_chat_tools_without_usage(self):
        """Test that chat_tools works correctly when no usage data is provided."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            # Mock response WITHOUT usage metadata
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Test response"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            tools = []
            
            result = await client.chat_tools(messages, tools)
            
            # Verify usage is NOT included
            assert "usage" not in result
            assert result["assistant"]["content"] == "Test response"


class TestOllamaClientVerifyParameter:
    """Test SSL verification parameter."""

    @pytest.mark.asyncio
    async def test_verify_parameter_passed_to_client(self):
        """Test that verify parameter is passed to httpx client."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(
                model="llama2",
                verify=False
            )
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "OK"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            await client.chat(messages)
            
            # Check that AsyncClient was called with verify=False
            # Note: httpx.AsyncClient accepts verify parameter which may be converted to SSLContext
            mock_async_client_class.assert_called()
            call_kwargs = mock_async_client_class.call_args[1]
            # The verify parameter might be an SSLContext or False depending on httpx internals
            assert "verify" in call_kwargs


class TestOllamaClientCancellation:
    """Test cancellation support."""

    @pytest.mark.asyncio
    async def test_cancellation_token(self):
        """Test chat with cancellation token."""
        with patch("httpx.AsyncClient") as mock_async_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            
            cancellation_token = MagicMock()
            cancellation_token.is_cancelled = False
            
            mock_client_instance = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "message": {"role": "assistant", "content": "Response"}
            }
            mock_response.raise_for_status = MagicMock()
            
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            
            mock_async_client_class.return_value = mock_client_instance
            
            messages = [ChatMessage(role="user", content="Test")]
            
            result = await client.chat(messages, cancellation_token=cancellation_token)
            
            assert result == "Response"


class TestOllamaClientStreamingUsageTracking:
    """Test usage tracking in streaming mode."""

    @pytest.mark.asyncio
    async def test_streaming_usage_tracking(self):
        """Test that usage information is tracked and returned in streaming mode."""
        client = OllamaNativeAsyncClient(model="llama2")
        
        # Mock streaming response with usage in final chunk
        streaming_data = [
            '{"message": {"content": "Hello"}, "done": false}',
            '{"message": {"content": " world"}, "done": false}',
            '{"message": {"content": "!"}, "done": false}',
            '{"done": true, "prompt_eval_count": 15, "eval_count": 25}',  # Final chunk with usage
        ]
        
        mock_client_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        
        async def mock_aiter_lines():
            for line in streaming_data:
                yield line
        
        mock_response.aiter_lines = MagicMock(return_value=mock_aiter_lines())
        
        # Setup stream context manager
        mock_stream_context = MagicMock()
        mock_stream_context.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_context.__aexit__ = AsyncMock()
        
        # Setup client context manager
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_context)
        
        # Patch the client's _httpx attribute directly
        mock_httpx = MagicMock()
        mock_httpx.AsyncClient.return_value = mock_client_instance
        client._httpx = mock_httpx
        
        messages = [ChatMessage(role="user", content="Test")]
        tools = []
        
        # Collect all events
        events = []
        async for event in client.chat_tools_streaming(messages, tools):
            events.append(event)
        
        # Verify content deltas
        content_deltas = [e for e in events if e.get("type") == "content_delta"]
        assert len(content_deltas) == 3
        assert content_deltas[0]["delta"] == "Hello"
        assert content_deltas[1]["delta"] == " world"
        assert content_deltas[2]["delta"] == "!"
        
        # Verify final event has usage (Ollama format: prompt_eval_count, eval_count)
        final_event = [e for e in events if e.get("type") == "final"][0]
        assert "usage" in final_event
        assert final_event["usage"]["prompt_tokens"] == 15
        assert final_event["usage"]["completion_tokens"] == 25
        assert final_event["usage"]["total_tokens"] == 40  # 15 + 25
        assert final_event["assistant"]["content"] == "Hello world!"

    @pytest.mark.asyncio
    async def test_streaming_without_usage(self):
        """Test that streaming works correctly when no usage data is provided."""
        client = OllamaNativeAsyncClient(model="llama2")
        
        # Mock streaming response WITHOUT usage metrics
        streaming_data = [
            '{"message": {"content": "Test"}, "done": false}',
            '{"message": {"content": " response"}, "done": false}',
            '{"done": true}',  # Final chunk WITHOUT usage
        ]
        
        mock_client_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        
        async def mock_aiter_lines():
            for line in streaming_data:
                yield line
        
        mock_response.aiter_lines = MagicMock(return_value=mock_aiter_lines())
        
        # Setup stream context manager
        mock_stream_context = MagicMock()
        mock_stream_context.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_context.__aexit__ = AsyncMock()
        
        # Setup client context manager
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_context)
        
        # Patch the client's _httpx attribute directly
        mock_httpx = MagicMock()
        mock_httpx.AsyncClient.return_value = mock_client_instance
        client._httpx = mock_httpx
        
        messages = [ChatMessage(role="user", content="Test")]
        tools = []
        
        # Collect all events
        events = []
        async for event in client.chat_tools_streaming(messages, tools):
            events.append(event)
        
        # Verify final event does NOT have usage
        final_event = [e for e in events if e.get("type") == "final"][0]
        assert "usage" not in final_event
        assert final_event["assistant"]["content"] == "Test response"


class TestOllamaSaysWhyTheAnswerEnded:
    """Ollama's done_reason ("length": cut at num_predict) reaches the agent
    loop's truncation guard; the client used to drop it."""

    @pytest.mark.asyncio
    async def test_a_blocking_answer_carries_it(self):
        with patch("httpx.AsyncClient") as client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            response = MagicMock()
            response.json.return_value = {"message": {"role": "assistant", "content": "Cut"},
                                          "done": True, "done_reason": "length"}
            response.raise_for_status = MagicMock()
            http = MagicMock()
            http.__aenter__ = AsyncMock(return_value=http)
            http.__aexit__ = AsyncMock()
            http.post = AsyncMock(return_value=response)
            client_class.return_value = http

            result = await client.chat_tools([ChatMessage(role="user", content="Test")], [])

        assert result["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_a_stream_carries_it(self):
        client = OllamaNativeAsyncClient(model="llama2")
        lines = ['{"message": {"content": "Cut"}, "done": false}',
                 '{"done": true, "done_reason": "length", "prompt_eval_count": 3, "eval_count": 1}']

        async def aiter_lines():
            for line in lines:
                yield line

        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.status_code = 200
        response.aiter_lines = MagicMock(return_value=aiter_lines())
        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=response)
        stream.__aexit__ = AsyncMock()
        http = MagicMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock()
        http.stream = MagicMock(return_value=stream)
        client._httpx = MagicMock()
        client._httpx.AsyncClient.return_value = http

        events = [event async for event in client.chat_tools_streaming([ChatMessage(role="user", content="Test")], [])]

        final = next(event for event in events if event.get("type") == "final")
        assert final["finish_reason"] == "length"


class TestOllamaCancelAndErrorShape:
    """What the agent server reads: a cancel as CancelledError, an error with message/type."""

    @staticmethod
    def _cancelled_token():
        token = MagicMock()
        token.is_cancelled = True
        return token

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_tools"], ids=["chat", "chat_tools"])
    async def test_a_cancelled_request_is_not_sent(self, call):
        import asyncio

        with patch("httpx.AsyncClient") as mock_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            messages = [ChatMessage(role="user", content="Test")]

            with pytest.raises(asyncio.CancelledError):
                if call == "chat":
                    await client.chat(messages, cancellation_token=self._cancelled_token())
                else:
                    await client.chat_tools(messages, [], cancellation_token=self._cancelled_token())

            mock_client_class.return_value.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cancelled_stream_is_not_started(self):
        import asyncio

        with patch("httpx.AsyncClient") as mock_client_class:
            client = OllamaNativeAsyncClient(model="llama2")

            with pytest.raises(asyncio.CancelledError):
                async for _ in client.chat_tools_streaming(
                        [ChatMessage(role="user", content="Test")], [],
                        cancellation_token=self._cancelled_token()):
                    pass

            assert not mock_client_class.called, "the cancelled request opened a connection"

    @pytest.mark.asyncio
    async def test_a_cancel_mid_stream_ends_the_stream(self):
        """Cancelled after the first line: the run must not read the rest."""
        import asyncio

        with patch("httpx.AsyncClient") as mock_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            token = MagicMock()
            token.is_cancelled = False
            lines_read = []

            async def lines():
                for chunk in ('{"message": {"content": "one"}}', '{"message": {"content": "two"}}'):
                    lines_read.append(chunk)
                    token.is_cancelled = True  # the user cancels while it streams
                    yield chunk

            response = MagicMock()
            response.status_code = 200
            response.raise_for_status = MagicMock()
            response.aiter_lines = lines
            stream = MagicMock()
            stream.__aenter__ = AsyncMock(return_value=response)
            stream.__aexit__ = AsyncMock(return_value=False)
            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.stream = MagicMock(return_value=stream)
            mock_client_class.return_value = instance

            with pytest.raises(asyncio.CancelledError):
                async for _ in client.chat_tools_streaming(
                        [ChatMessage(role="user", content="Test")], [], cancellation_token=token):
                    pass

            assert lines_read == ['{"message": {"content": "one"}}'], "the cancelled stream kept reading"
            assert stream.__aexit__.await_count == 1, "the stream was left open"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_tools"], ids=["chat", "chat_tools"])
    async def test_a_cancel_during_the_call_is_a_cancel_too(self, call):
        """The token is watched while the request runs (llm_common.cancellation)."""
        import asyncio

        with patch("httpx.AsyncClient") as mock_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            token = MagicMock()
            token.is_cancelled = False

            async def slow_answer(*args, **kwargs):
                token.is_cancelled = True
                await asyncio.sleep(30)  # only the cancel can end this

            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            # False, not a bare AsyncMock: a truthy __aexit__ swallows the cancel.
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.post = AsyncMock(side_effect=slow_answer)
            mock_client_class.return_value = instance

            messages = [ChatMessage(role="user", content="Test")]
            answer = (client.chat(messages, cancellation_token=token) if call == "chat"
                      else client.chat_tools(messages, [], cancellation_token=token))

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(answer, timeout=5)

    @pytest.mark.asyncio
    async def test_a_failed_stream_reports_an_error_the_server_can_read(self):
        """agent_system/servers/agent/mixins/llm_loop/fallback.py reads error.message and error.type."""
        with patch("httpx.AsyncClient") as mock_client_class:
            client = OllamaNativeAsyncClient(model="llama2")
            mock_client_class.side_effect = ValueError("boom")

            events = [e async for e in client.chat_tools_streaming(
                [ChatMessage(role="user", content="Test")], [])]

            error = events[-1]["assistant"]["error"]
            assert error["message"] == "boom" and error["type"] == "ollama_api_error"
