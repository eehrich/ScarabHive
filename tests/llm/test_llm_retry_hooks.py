"""
Tests for LLM retry hook notifications across all LLM clients.

Verifies that _notify_retry fires POST_LLM_RESPONSE with:
- error prefixed by [RETRY x/y]
- finish_reason: "retry"
- correct provider/model/url/is_streaming

Tests cover:
1. Base class _notify_retry helper
2. HTTPXOpenAIClient retry hooks
3. GeminiClient retry hooks
4. GeminiSDKClient retry hooks
5. AnthropicAsyncClient retry hooks  
6. OpenAIAsyncClient retry hooks
7. OllamaNativeAsyncClient retry hooks
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.llm.models import LLMClient


# ─── Base class _notify_retry tests ─────────────────────────────────────────


class TestNotifyRetryBase:
    """Test the _notify_retry helper on LLMClient base class."""

    @pytest.mark.asyncio
    async def test_notify_retry_calls_post_response(self):
        """_notify_retry should call _notify_post_response with correct payload."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry(
            provider="test",
            model="test-model",
            url="http://example.com/v1",
            is_streaming=True,
            error_msg="Rate limited",
            attempt=0,
            max_attempts=3,
        )

        client._on_post_llm_response.assert_called_once()
        info = client._on_post_llm_response.call_args[0][0]
        assert info["provider"] == "test"
        assert info["model"] == "test-model"
        assert info["url"] == "http://example.com/v1"
        assert info["is_streaming"] is True
        assert info["error"] == "[RETRY 1/3] Rate limited"
        assert info["finish_reason"] == "retry"
        assert "timestamp_ms" in info

    @pytest.mark.asyncio
    async def test_notify_retry_attempt_numbering(self):
        """Attempt numbers should be 1-indexed in the retry label."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        # Attempt 2 of 5
        await client._notify_retry("p", "m", "", False, "err", 2, 5)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["error"] == "[RETRY 3/5] err"

    @pytest.mark.asyncio
    async def test_notify_retry_with_duration(self):
        """Optional duration_ms should be included when provided."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry("p", "m", "", False, "err", 0, 3, duration_ms=123.4)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["duration_ms"] == 123.4

    @pytest.mark.asyncio
    async def test_notify_retry_with_response_data(self):
        """Optional response_data should be included when provided."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        resp_data = {"candidates": [], "error": "bad request"}
        await client._notify_retry("p", "m", "", True, "err", 0, 3, response_data=resp_data)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["response_data"] == resp_data

    @pytest.mark.asyncio
    async def test_notify_retry_no_hook_set(self):
        """Should not raise when no hook is set."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = None

        # Should not raise
        await client._notify_retry("p", "m", "", False, "err", 0, 3)

    @pytest.mark.asyncio
    async def test_notify_retry_hook_error_swallowed(self):
        """Errors in the hook callback should be swallowed."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock(side_effect=RuntimeError("hook crashed"))

        # Should not raise
        await client._notify_retry("p", "m", "", False, "err", 0, 3)


# ─── HTTPX Client retry hooks ──────────────────────────────────────────────


class TestHTTPXRetryHooks:
    """Test that HTTPXOpenAIClient fires _notify_retry during retries."""

    def _make_client(self):
        from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig
        timeout_config = HTTPXTimeoutConfig(connect=1.0, read=2.0, write=1.0, pool=0.5)
        client = HTTPXOpenAIClient(
            model="gpt-4",
            api_key="test-key",
            timeout_config=timeout_config,
            max_retries=2,
            retry_backoff=0.01,  # Fast for tests
            rate_limit_backoff=0.01,  # Fast for tests
            rate_limit_max_retries=2,
        )
        return client

    @pytest.mark.asyncio
    async def test_429_retry_fires_hook(self):
        """429 rate limit should fire _notify_retry before retry."""
        import httpx

        client = self._make_client()
        hook_calls = []

        async def capture_hook(info):
            hook_calls.append(info)

        client.set_llm_hooks(on_post_response=capture_hook)

        # Create 2 x 429 responses then 1 success
        fail_resp = httpx.Response(429, request=httpx.Request("POST", "http://test"), json={"error": "rate limited"})
        ok_resp = httpx.Response(200, request=httpx.Request("POST", "http://test"), json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        })

        call_count = 0

        async def mock_send(request, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return fail_resp
            return ok_resp

        with patch("httpx.AsyncClient.send", side_effect=mock_send):
            msgs = [{"role": "user", "content": "hello"}]
            tools = [{"type": "function", "function": {"name": "test", "description": "test", "parameters": {"type": "object", "properties": {}}}}]
            await client.chat_tools(msgs, tools)

        # Should have retry hooks for the 2 failed attempts + 1 success post_response
        retry_calls = [c for c in hook_calls if c.get("finish_reason") == "retry"]
        assert len(retry_calls) == 2
        assert "[RETRY 1/" in retry_calls[0]["error"]
        assert "[RETRY 2/" in retry_calls[1]["error"]
        assert "429" in retry_calls[0]["error"] or "Rate limit" in retry_calls[0]["error"]


# ─── OpenAI Client retry hooks ─────────────────────────────────────────────


class TestOpenAIRetryHooks:
    """Test that OpenAIAsyncClient fires hooks correctly."""

    @pytest.mark.asyncio
    async def test_pre_post_hooks_on_chat_tools(self):
        """_chat_tools_chat_completions should fire pre and post hooks."""
        try:
            from plugins.llm_openai.openai_client import OpenAIAsyncClient
        except ImportError:
            pytest.skip("openai package not installed")

        hook_calls = {"pre": [], "post": []}

        async def pre_hook(info):
            hook_calls["pre"].append(info)

        async def post_hook(info):
            hook_calls["post"].append(info)

        # Create client
        with patch("openai.AsyncOpenAI"):
            client = OpenAIAsyncClient(
                model="gpt-4",
                api_key="test-key",
                max_attempts=2,
                base_backoff=0.01,
            )
            client.set_llm_hooks(on_pre_request=pre_hook, on_post_response=post_hook)

        # Mock the OpenAI SDK response
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "hello"
        mock_resp.choices[0].message.tool_calls = None
        mock_resp.choices[0].finish_reason = "stop"
        mock_resp.usage = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.total_tokens = 15
        mock_resp.usage.prompt_tokens_details = None
        mock_resp.usage.completion_tokens_details = None

        with patch.object(client._client, "chat", create=True) as mock_chat:
            mock_completions = MagicMock()
            mock_completions.create = AsyncMock(return_value=mock_resp)
            mock_chat.completions = mock_completions

            from agent_system.llm.models import ChatMessage
            msgs = [ChatMessage(role="user", content="hello")]
            await client._chat_tools_chat_completions(msgs, [])

        assert len(hook_calls["pre"]) == 1
        assert hook_calls["pre"][0]["provider"] == "openai"
        assert hook_calls["pre"][0]["is_streaming"] is False

        assert len(hook_calls["post"]) == 1
        assert hook_calls["post"][0]["provider"] == "openai"
        assert "duration_ms" in hook_calls["post"][0]
        assert hook_calls["post"][0]["finish_reason"] == "stop"


# ─── Ollama Client retry hooks ─────────────────────────────────────────────


class TestOllamaRetryHooks:
    """Test that OllamaNativeAsyncClient fires hooks correctly."""

    @pytest.mark.asyncio
    async def test_pre_hook_fires(self):
        """Pre-request hook should fire before streaming."""
        from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient

        hook_calls = {"pre": [], "post": []}

        async def pre_hook(info):
            hook_calls["pre"].append(info)

        async def post_hook(info):
            hook_calls["post"].append(info)

        client = OllamaNativeAsyncClient(
            model="llama3",
            base_url="http://localhost:11434",
            timeout=5.0,
        )
        client.set_llm_hooks(on_pre_request=pre_hook, on_post_response=post_hook)

        # Mock httpx to return a successful streaming response

        async def mock_stream_response():
            """Simulate a streaming response with done=true."""
            lines = [
                '{"message": {"role": "assistant", "content": "Hi"}, "done": false}',
                '{"message": {"role": "assistant", "content": "!"}, "done": true, "eval_count": 5, "prompt_eval_count": 10}',
            ]
            for line in lines:
                yield line

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.aiter_lines = mock_stream_response
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        from agent_system.llm.models import ChatMessage
        msgs = [ChatMessage(role="user", content="Hi")]

        with patch("httpx.AsyncClient", return_value=mock_client):
            chunks = []
            async for chunk in client.chat_tools_streaming(msgs, []):
                chunks.append(chunk)

        assert len(hook_calls["pre"]) == 1
        assert hook_calls["pre"][0]["provider"] == "ollama"
        assert hook_calls["pre"][0]["is_streaming"] is True

    @pytest.mark.asyncio
    async def test_server_error_retry_fires_hook(self):
        """5xx error should fire _notify_retry on Ollama client."""
        from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient

        retry_calls = []

        async def post_hook(info):
            retry_calls.append(info)

        client = OllamaNativeAsyncClient(
            model="llama3",
            base_url="http://localhost:11434",
            timeout=5.0,
        )
        client.set_llm_hooks(on_post_response=post_hook)



        # First call: 500, second call: success
        async def mock_stream_lines_success():
            lines = [
                '{"message": {"role": "assistant", "content": "ok"}, "done": true, "eval_count": 5, "prompt_eval_count": 10}',
            ]
            for line in lines:
                yield line

        mock_fail_resp = MagicMock()
        mock_fail_resp.status_code = 500
        mock_fail_resp.__aenter__ = AsyncMock(return_value=mock_fail_resp)
        mock_fail_resp.__aexit__ = AsyncMock(return_value=False)

        mock_ok_resp = MagicMock()
        mock_ok_resp.status_code = 200
        mock_ok_resp.raise_for_status = MagicMock()
        mock_ok_resp.aiter_lines = mock_stream_lines_success
        mock_ok_resp.__aenter__ = AsyncMock(return_value=mock_ok_resp)
        mock_ok_resp.__aexit__ = AsyncMock(return_value=False)

        mock_client = MagicMock()
        responses = [mock_fail_resp, mock_ok_resp]
        response_iter = iter(responses)
        mock_client.stream = MagicMock(side_effect=lambda *a, **kw: next(response_iter))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        from agent_system.llm.models import ChatMessage
        msgs = [ChatMessage(role="user", content="Hi")]

        with patch("httpx.AsyncClient", return_value=mock_client):
            chunks = []
            async for chunk in client.chat_tools_streaming(msgs, []):
                chunks.append(chunk)

        # Should have at least one retry notification
        retry_notifications = [c for c in retry_calls if c.get("finish_reason") == "retry"]
        assert len(retry_notifications) >= 1
        assert "[RETRY 1/" in retry_notifications[0]["error"]
        assert "500" in retry_notifications[0]["error"] or "Server error" in retry_notifications[0]["error"]


# ─── GeminiClient retry hooks ──────────────────────────────────────────────


class TestGeminiClientRetryHooks:
    """Test GeminiClient _notify_retry coverage."""

    @pytest.mark.asyncio
    async def test_too_many_states_retry_fires_hook(self):
        """400 'too many states' error should fire _notify_retry in non-streaming."""
        from plugins.llm_gemini.gemini_client import GeminiClient

        retry_calls = []

        async def post_hook(info):
            retry_calls.append(info)

        client = GeminiClient(
            model="gemini-2.5-pro",
            api_key="test-key",
        )
        client.max_retries = 1
        client.rate_limit_max_retries = 1
        client.set_llm_hooks(on_post_response=post_hook)

        import httpx

        # First call: 400 too many states, second: success
        call_count = 0

        async def mock_post(url, json=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    400,
                    request=httpx.Request("POST", str(url)),
                    text="Error: too many states in schema"
                )
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={
                    "candidates": [{
                        "content": {"parts": [{"text": "hello"}]},
                        "finishReason": "STOP"
                    }],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
                }
            )

        with patch("httpx.AsyncClient") as MockAsyncClient:
            mock_instance = MagicMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(side_effect=mock_post)
            MockAsyncClient.return_value = mock_instance

            from agent_system.llm.models import ChatMessage
            msgs = [ChatMessage(role="user", content="hello")]
            await client.chat_tools(msgs, [])

        retry_notifications = [c for c in retry_calls if c.get("finish_reason") == "retry"]
        assert len(retry_notifications) >= 1
        assert "too many states" in retry_notifications[0]["error"]


# ─── Anthropic Client retry hooks ──────────────────────────────────────────


class TestAnthropicRetryHooks:
    """Test AnthropicAsyncClient _notify_retry coverage."""

    @pytest.mark.asyncio
    async def test_retry_hook_calls_with_correct_provider(self):
        """_notify_retry should use 'anthropic' as provider."""
        # Test the base-class method behavior since actual Anthropic client
        # requires the anthropic package and complex mocking
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry("anthropic", "claude-3-opus", "", True, "Rate limit (429)", 0, 3)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["provider"] == "anthropic"
        assert info["model"] == "claude-3-opus"
        assert info["is_streaming"] is True
        assert "[RETRY 1/3]" in info["error"]
        assert "Rate limit" in info["error"]

    @pytest.mark.asyncio
    async def test_overloaded_retry_hook(self):
        """Overloaded errors should produce correct retry messages."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry("anthropic", "claude-3-opus", "", True, "Service overloaded", 1, 4)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["error"] == "[RETRY 2/4] Service overloaded"
        assert info["finish_reason"] == "retry"


# ─── GeminiSDK Client retry hooks ──────────────────────────────────────────


class TestGeminiSDKRetryHooks:
    """Test GeminiSDKClient _notify_retry coverage at base-class level."""

    @pytest.mark.asyncio
    async def test_malformed_function_call_retry(self):
        """MALFORMED_FUNCTION_CALL retry should have correct error message."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry("gemini", "gemini-2.5-pro", "https://api.google.com/v1", True, "MALFORMED_FUNCTION_CALL (empty response)", 0, 4)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["error"] == "[RETRY 1/4] MALFORMED_FUNCTION_CALL (empty response)"
        assert info["provider"] == "gemini"

    @pytest.mark.asyncio
    async def test_max_tokens_retry(self):
        """MAX_TOKENS retry should have correct error message."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry("gemini", "gemini-2.5-flash", "", True, "MAX_TOKENS (stuck in thinking)", 1, 4)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["error"] == "[RETRY 2/4] MAX_TOKENS (stuck in thinking)"


# ─── Integration: _notify_retry format consistency ─────────────────────────


class TestRetryHookFormatConsistency:
    """Ensure retry hook format is consistent across all providers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider,model,error_msg", [
        ("openai", "gpt-4", "Rate limit (429)"),
        ("gemini", "gemini-2.5-pro", "Server error (500)"),
        ("anthropic", "claude-3-opus", "Service overloaded"),
        ("ollama", "llama3", "Stream interrupted: connection reset"),
        ("openai", "gpt-4-turbo", "Server error (502)"),
    ])
    async def test_retry_format(self, provider, model, error_msg):
        """All providers should produce [RETRY x/y] prefix format."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        await client._notify_retry(provider, model, "", True, error_msg, 0, 3)

        info = client._on_post_llm_response.call_args[0][0]
        assert info["error"].startswith("[RETRY 1/3]")
        assert error_msg in info["error"]
        assert info["finish_reason"] == "retry"
        assert info["provider"] == provider
        assert info["model"] == model

    @pytest.mark.asyncio
    async def test_all_attempts_numbered_correctly(self):
        """Test that retry numbering works for all attempts 0..n-1."""
        client = LLMClient.__new__(LLMClient)
        client._on_pre_llm_request = None
        client._on_post_llm_response = AsyncMock()

        max_attempts = 5
        for attempt in range(max_attempts):
            await client._notify_retry("test", "model", "", False, "err", attempt, max_attempts)

        assert client._on_post_llm_response.call_count == max_attempts
        for i, call in enumerate(client._on_post_llm_response.call_args_list):
            info = call[0][0]
            expected = f"[RETRY {i + 1}/{max_attempts}] err"
            assert info["error"] == expected, f"Attempt {i}: expected '{expected}', got '{info['error']}'"
