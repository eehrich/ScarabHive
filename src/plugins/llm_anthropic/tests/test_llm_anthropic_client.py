"""Tests for Anthropic Claude client."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.llm.models import ChatMessage


@pytest.fixture
def anthropic_client():
    """Create an AnthropicAsyncClient instance for testing."""
    # Import inside the patch context to ensure module-level patch works
    with patch('anthropic.AsyncAnthropic') as mock_anthropic:
        mock_instance = MagicMock()
        mock_anthropic.return_value = mock_instance
        
        from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
        
        client = AnthropicAsyncClient(
            model="claude-sonnet-4-20250514",
            api_key="test-api-key",
            context_window=200000,
            request_timeout=180
        )
        client._mock = mock_instance
        return client


class TestAnthropicClientMessageConversion:
    """Test message format conversion from ChatMessage to Anthropic format."""

    def test_convert_simple_user_message(self, anthropic_client):
        """Test conversion of simple user message."""
        messages = [
            ChatMessage(role="user", content="Hello, how are you?")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert system_prompt is None
        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello, how are you?"

    def test_convert_system_message(self, anthropic_client):
        """Test that system messages are extracted with cache_control."""
        messages = [
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        # With enable_prompt_caching=True (default), system prompt becomes cached content blocks
        assert isinstance(system_prompt, list)
        assert len(system_prompt) == 1
        assert system_prompt[0]["type"] == "text"
        assert system_prompt[0]["text"] == "You are a helpful assistant."
        assert system_prompt[0]["cache_control"] == {"type": "ephemeral"}
        assert len(converted) == 1  # Only user message
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello"

    def test_convert_multiple_system_messages(self, anthropic_client):
        """Test that multiple system messages are concatenated with cache_control."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="system", content="Be concise."),
            ChatMessage(role="user", content="Hello")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert isinstance(system_prompt, list)
        assert len(system_prompt) == 1
        assert system_prompt[0]["text"] == "You are helpful.\nBe concise."
        assert system_prompt[0]["cache_control"] == {"type": "ephemeral"}
        assert len(converted) == 1

    def test_convert_assistant_message(self, anthropic_client):
        """Test conversion of assistant message."""
        messages = [
            ChatMessage(role="assistant", content="I'm doing well, thank you!")
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "assistant"
        assert converted[0]["content"] == "I'm doing well, thank you!"

    def test_convert_tool_response(self, anthropic_client):
        """Test conversion of tool response message."""
        messages = [
            ChatMessage(
                role="tool",
                content='{"temperature": 22, "condition": "sunny"}',
                tool_call_id="toolu_123",
                name="get_weather"
            )
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "user"  # Tool results go to user role
        assert len(converted[0]["content"]) == 1
        assert converted[0]["content"][0]["type"] == "tool_result"
        assert converted[0]["content"][0]["tool_use_id"] == "toolu_123"

    def test_convert_assistant_with_tool_calls(self, anthropic_client):
        """Test conversion of assistant message with tool calls."""
        messages = [
            ChatMessage(
                role="assistant",
                content="Let me check the weather.",
                tool_calls=[
                    {
                        "id": "toolu_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Paris"}'
                        }
                    }
                ]
            )
        ]
        
        system_prompt, converted = anthropic_client._convert_messages(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "assistant"
        content_blocks = converted[0]["content"]
        assert len(content_blocks) == 2
        
        # First block is text
        assert content_blocks[0]["type"] == "text"
        assert content_blocks[0]["text"] == "Let me check the weather."
        
        # Second block is tool_use
        assert content_blocks[1]["type"] == "tool_use"
        assert content_blocks[1]["id"] == "toolu_123"
        assert content_blocks[1]["name"] == "get_weather"
        assert content_blocks[1]["input"] == {"location": "Paris"}


class TestAnthropicClientToolConversion:
    """Test tool schema conversion to Anthropic format."""

    def test_convert_simple_tool(self, anthropic_client):
        """Test conversion of simple OpenAI tool schema."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "City name"
                            }
                        },
                        "required": ["location"]
                    }
                }
            }
        ]
        
        anthropic_tools = anthropic_client._convert_tools(tools)
        
        assert len(anthropic_tools) == 1
        assert anthropic_tools[0]["name"] == "get_weather"
        assert anthropic_tools[0]["description"] == "Get weather for a location"
        assert "input_schema" in anthropic_tools[0]
        assert anthropic_tools[0]["input_schema"]["type"] == "object"

    def test_convert_multiple_tools(self, anthropic_client):
        """Test conversion of multiple tools."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "tool1",
                    "description": "First tool",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "tool2",
                    "description": "Second tool",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]
        
        anthropic_tools = anthropic_client._convert_tools(tools)
        
        assert len(anthropic_tools) == 2
        assert anthropic_tools[0]["name"] == "tool1"
        assert anthropic_tools[1]["name"] == "tool2"


class TestAnthropicImageConversion:
    """Test image content conversion using anthropic_utils."""

    def test_convert_base64_image(self):
        """Test conversion of base64 image."""
        from plugins.llm_anthropic import anthropic_utils
        
        item = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/png"

    def test_convert_data_url_image(self):
        """Test conversion of data URL image."""
        from plugins.llm_anthropic import anthropic_utils
        
        item = {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/jpeg"

    def test_convert_url_image(self):
        """Test conversion of URL image."""
        from plugins.llm_anthropic import anthropic_utils
        
        item = {
            "type": "image_url",
            "image_url": {
                "url": "https://example.com/image.png"
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/image.png"


class TestAnthropicClientUsageExtraction:
    """Usage in OpenAI semantics: prompt_tokens is the whole input.

    Real SDK models, not MagicMock: a MagicMock answers every unset attribute
    with another MagicMock, so the None fields the SDK really sends were never
    exercised and arithmetic on them passed silently.
    """

    def test_extract_basic_usage(self, anthropic_client):
        from anthropic.types import Usage

        result = anthropic_client._extract_usage(Usage(input_tokens=100, output_tokens=50))

        assert result == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    def test_extract_cached_usage(self, anthropic_client):
        """Anthropic's input_tokens excludes cache reads and writes; the prompt
        must include both, or every warm call looks nearly free and >100% cached."""
        from anthropic.types import Usage

        result = anthropic_client._extract_usage(Usage(
            input_tokens=100, output_tokens=50,
            cache_read_input_tokens=80, cache_creation_input_tokens=20))

        assert result["prompt_tokens"] == 200
        assert result["total_tokens"] == 250
        assert result["prompt_tokens_details"] == {
            "cached_tokens": 80, "cache_creation_tokens": 20}

    def test_extract_write_only_usage_keeps_details(self, anthropic_client):
        """A cold call only writes the cache (first call of every session, every
        call after the prefix changes); its writes must still reach the pricing layer."""
        from anthropic.types import Usage
        from agent_system.llm import pricing

        result = anthropic_client._extract_usage(Usage(
            input_tokens=50, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=12000))

        assert result["prompt_tokens"] == 12050
        assert result["total_tokens"] == 12055
        assert result["prompt_tokens_details"] == {
            "cached_tokens": 0, "cache_creation_tokens": 12000}
        assert pricing.normalize_usage(result).cache_write_tokens == 12000

    def test_extract_usage_delta_without_input_tokens(self, anthropic_client):
        """MessageDeltaUsage declares input_tokens Optional; None must not raise."""
        from anthropic.types import MessageDeltaUsage

        result = anthropic_client._extract_usage(MessageDeltaUsage(output_tokens=5))

        assert result == {"prompt_tokens": 0, "completion_tokens": 5, "total_tokens": 5}

    def test_extracted_usage_prices_cache_reads_on_top_of_uncached_input(
            self, anthropic_client, tmp_path, monkeypatch):
        """The contract with the pricing layer: reads are a subset of prompt_tokens,
        so a warm call pays full input for the uncached part plus the read rate."""
        import yaml
        from anthropic.types import Usage
        from agent_system.llm import pricing

        table = tmp_path / "llm_pricing.yaml"
        table.write_text(yaml.safe_dump(
            {"cheap": {"input": 1.0, "output": 1.0, "cached_input": 0.1}}), encoding="utf-8")
        monkeypatch.setattr(pricing, "_cache", {"path": None, "mtime": None, "table": {}})

        usage = anthropic_client._extract_usage(Usage(
            input_tokens=1_000_000, output_tokens=0,
            cache_read_input_tokens=1_000_000, cache_creation_input_tokens=0))
        cost, estimated = pricing.resolve_call_cost(usage, "cheap", path=table)

        assert estimated is True
        assert cost == pytest.approx(1.1)


class TestAnthropicClientStreaming:
    """Test streaming chat functionality."""

    @pytest.mark.asyncio
    async def test_streaming_text_response(self, anthropic_client):
        """Test streaming text response."""
        # Create mock stream events
        text_event = MagicMock()
        text_event.type = "content_block_delta"
        text_event.delta = MagicMock()
        text_event.delta.type = "text_delta"
        text_event.delta.text = "Hello"
        
        final_message = MagicMock()
        final_message.usage = MagicMock()
        final_message.usage.input_tokens = 10
        final_message.usage.output_tokens = 5
        
        # Create async iterator helper
        async def async_iter_events():
            yield text_event
        
        # Create mock stream context manager
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)
        mock_stream.__aiter__ = lambda self: async_iter_events()
        mock_stream.get_final_message = AsyncMock(return_value=final_message)
        
        anthropic_client._client.messages.stream = MagicMock(return_value=mock_stream)
        
        messages = [ChatMessage(role="user", content="Hi")]
        tools = []
        
        chunks = []
        async for chunk in anthropic_client.chat_tools_streaming(messages, tools):
            chunks.append(chunk)
        
        # Should have content delta and final
        assert any(c.get("type") == "content_delta" for c in chunks)
        assert any(c.get("type") == "final" for c in chunks)

    @pytest.mark.asyncio
    async def test_final_usage_counts_cache_reads_and_survives_a_sparse_delta(
            self, anthropic_client):
        """A message_delta usage without input_tokens used to raise TypeError after
        the content had streamed; the final message's usage is the one reported."""
        from anthropic.types import MessageDeltaUsage, Usage

        delta_event = MagicMock()
        delta_event.type = "message_delta"
        delta_event.usage = MessageDeltaUsage(output_tokens=5)

        final_message = MagicMock()
        final_message.usage = Usage(input_tokens=10, output_tokens=5,
                                    cache_read_input_tokens=1000,
                                    cache_creation_input_tokens=0)

        async def events():
            yield delta_event

        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=None)
        stream.__aiter__ = lambda self: events()
        stream.get_final_message = AsyncMock(return_value=final_message)
        anthropic_client._client.messages.stream = MagicMock(return_value=stream)

        chunks = [chunk async for chunk in anthropic_client.chat_tools_streaming(
            [ChatMessage(role="user", content="Hi")], [])]

        final = next(chunk for chunk in chunks if chunk.get("type") == "final")
        assert final["usage"]["prompt_tokens"] == 1010
        assert final["usage"]["total_tokens"] == 1015
        assert final["usage"]["prompt_tokens_details"]["cached_tokens"] == 1000


class TestAnthropicToolInputIsNotRepaired:
    """The stream hands tool input on as the model sent it.

    It used to parse the input and repair what did not parse: a repaired guess
    ran as if it were the call, and input nothing could repair ran with {}
    without an error. tool_execution rejects malformed arguments and the model
    sends the call again -- but only if they reach it unchanged.
    """

    @pytest.mark.asyncio
    async def test_malformed_tool_input_reaches_the_caller_as_sent(self, anthropic_client):
        from agent_system.utils.json_utils import repair_json

        raw = '{"doc": "synopsis", "data": {"background": "cut off'
        assert isinstance(repair_json(raw), dict), (
            "json-repair makes nothing of this -- the test would measure nothing")

        start = MagicMock()
        start.type = "content_block_start"
        start.content_block = MagicMock()
        start.content_block.type = "tool_use"
        start.content_block.id = "toolu_1"
        start.content_block.name = "write_doc"
        delta = MagicMock()
        delta.type = "content_block_delta"
        delta.delta = MagicMock()
        delta.delta.type = "input_json_delta"
        delta.delta.partial_json = raw
        stop = MagicMock()
        stop.type = "content_block_stop"

        final_message = MagicMock()
        final_message.usage.input_tokens = 10
        final_message.usage.output_tokens = 5

        async def events():
            for event in (start, delta, stop):
                yield event

        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=None)
        stream.__aiter__ = lambda self: events()
        stream.get_final_message = AsyncMock(return_value=final_message)
        anthropic_client._client.messages.stream = MagicMock(return_value=stream)

        chunks = [chunk async for chunk in anthropic_client.chat_tools_streaming(
            [ChatMessage(role="user", content="Hi")], [])]

        final = next(chunk for chunk in chunks if chunk.get("type") == "final")
        (call,) = final["assistant"]["tool_calls"]
        assert call["function"]["arguments"] == raw


class TestAnthropicClientSchemaClean:
    """Test JSON schema cleaning."""

    def test_clean_schema_removes_unsupported(self, anthropic_client):
        """Test that unsupported schema fields are removed."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"}
            },
            "additionalProperties": False,  # Should be removed
            "$schema": "http://json-schema.org/draft-07/schema#"  # Should be removed
        }
        
        cleaned = anthropic_client._clean_schema(schema)
        
        assert "additionalProperties" not in cleaned
        assert "$schema" not in cleaned
        assert cleaned["type"] == "object"
        assert "properties" in cleaned

    def test_clean_nested_schema(self, anthropic_client):
        """Test cleaning of nested schemas."""
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True
                    }
                }
            }
        }
        
        cleaned = anthropic_client._clean_schema(schema)
        
        # Check nested additionalProperties is removed
        assert "additionalProperties" not in cleaned["properties"]["items"]["items"]


class TestAnthropicPromptCaching:
    """Test prompt caching cache_control injection."""

    @pytest.fixture
    def client_no_caching(self):
        """Create an AnthropicAsyncClient with prompt caching disabled."""
        with patch('anthropic.AsyncAnthropic') as mock_anthropic:
            mock_anthropic.return_value = MagicMock()
            from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
            return AnthropicAsyncClient(
                model="claude-sonnet-4-20250514",
                api_key="test-api-key",
                enable_prompt_caching=False,
            )

    def test_system_prompt_cached_by_default(self, anthropic_client):
        """System prompt becomes content block with cache_control when caching enabled."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hi"),
        ]
        system_prompt, _ = anthropic_client._convert_messages(messages)
        assert isinstance(system_prompt, list)
        assert system_prompt[0]["cache_control"] == {"type": "ephemeral"}

    def test_system_prompt_plain_when_caching_disabled(self, client_no_caching):
        """System prompt stays a plain string when caching is disabled."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hi"),
        ]
        system_prompt, _ = client_no_caching._convert_messages(messages)
        assert system_prompt == "You are helpful."

    def test_tool_cache_control_on_last_tool(self, anthropic_client):
        """cache_control is added to the last tool definition only."""
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "tool_b", "description": "B", "parameters": {"type": "object", "properties": {}}}},
        ]
        converted = anthropic_client._convert_tools(tools)
        assert "cache_control" not in converted[0]
        assert converted[-1]["cache_control"] == {"type": "ephemeral"}

    def test_tool_no_cache_control_when_disabled(self, client_no_caching):
        """No cache_control on tools when caching is disabled."""
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {"type": "object", "properties": {}}}},
        ]
        converted = client_no_caching._convert_tools(tools)
        assert "cache_control" not in converted[0]


def _make_client(mode):
    """AnthropicAsyncClient with a given prompt_cache_mode (SDK mocked)."""
    with patch('anthropic.AsyncAnthropic') as mock_anthropic:
        mock_anthropic.return_value = MagicMock()
        from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
        return AnthropicAsyncClient(
            model="claude-sonnet-4-20250514",
            api_key="test-api-key",
            prompt_cache_mode=mode,
        )


def _tail_marked(converted_messages):
    """True if the last converted message carries a cache_control block."""
    if not converted_messages:
        return False
    content = converted_messages[-1].get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and "cache_control" in b for b in content
    )


def _count_markers(system_prompt, converted_messages, tools):
    """Total cache_control blocks across system + messages + tools."""
    n = 0
    if isinstance(system_prompt, list):
        n += sum(1 for b in system_prompt if isinstance(b, dict) and "cache_control" in b)
    for msg in converted_messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            n += sum(1 for b in content if isinstance(b, dict) and "cache_control" in b)
    for t in (tools or []):
        if isinstance(t, dict) and "cache_control" in t:
            n += 1
    return n


class TestAnthropicMultiTurnCaching:
    """Native SDK path is now mode-aware and consistent with the OpenRouter
    (httpx) path: multi_turn caches the growing conversation tail, and a shared
    cap keeps <= 4 cache_control blocks. Same shared policy (cache_key.py)."""

    _CONV = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Q1"),
        ChatMessage(role="assistant", content="A1"),
        ChatMessage(role="user", content="Q2"),
    ]
    _SINGLE = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Q1"),
    ]

    def test_multi_turn_marks_conversation_tail(self):
        """multi_turn seeds the tail marker from turn 1 (declared conversation)."""
        _, converted = _make_client("multi_turn")._convert_messages(self._SINGLE)
        assert _tail_marked(converted) is True

    def test_auto_no_tail_on_single_shot(self):
        """auto marks no tail without real history (no assistant/tool message)."""
        _, converted = _make_client("auto")._convert_messages(self._SINGLE)
        assert _tail_marked(converted) is False

    def test_auto_marks_tail_with_history(self):
        """auto marks the tail once a real conversation exists."""
        _, converted = _make_client("auto")._convert_messages(self._CONV)
        assert _tail_marked(converted) is True

    def test_default_none_no_tail_on_single_shot(self):
        """No prompt_cache_mode behaves like auto (no wasted single-shot write)."""
        _, converted = _make_client(None)._convert_messages(self._SINGLE)
        assert _tail_marked(converted) is False

    def test_task_sequence_opts_out_of_tail(self):
        """task_sequence uses the ladder, not the multi-turn tail."""
        _, converted = _make_client("task_sequence")._convert_messages(self._CONV)
        assert _tail_marked(converted) is False

    def test_off_opts_out_of_tail(self):
        """off disables the conversation tail marker."""
        _, converted = _make_client("off")._convert_messages(self._CONV)
        assert _tail_marked(converted) is False

    def test_system_still_cached_regardless_of_mode(self):
        """The static system prefix is cached in every mode (marker on system)."""
        for mode in (None, "auto", "multi_turn", "task_sequence", "off"):
            system_prompt, _ = _make_client(mode)._convert_messages(self._SINGLE)
            assert isinstance(system_prompt, list)
            assert any(
                isinstance(b, dict) and "cache_control" in b for b in system_prompt
            ), f"system not cached for mode={mode}"

    def test_cap_never_exceeds_four_blocks(self):
        """System + tools + tail can never trip the hard 4-block HTTP-400 limit."""
        client = _make_client("multi_turn")
        system_prompt, converted = client._convert_messages(self._CONV)
        tools = [
            {"type": "function", "function": {"name": f"t{i}", "description": "d",
             "parameters": {"type": "object", "properties": {}}}}
            for i in range(3)
        ]
        anthropic_tools = client._convert_tools(tools)
        client._cap_anthropic_cache(system_prompt, converted, anthropic_tools)
        assert _count_markers(system_prompt, converted, anthropic_tools) <= 4

    def test_cap_keeps_latest_blocks(self):
        """The cap drops the EARLIEST markers (a later breakpoint subsumes them),
        so the conversation tail — the most valuable marker — always survives."""
        client = _make_client("multi_turn")
        system_prompt, converted = client._convert_messages(self._CONV)
        tools = [
            {"type": "function", "function": {"name": f"t{i}", "description": "d",
             "parameters": {"type": "object", "properties": {}}}}
            for i in range(5)
        ]
        anthropic_tools = client._convert_tools(tools)
        client._cap_anthropic_cache(system_prompt, converted, anthropic_tools)
        # tail (last message) keeps its marker
        assert _tail_marked(converted) is True

    def test_tail_marks_tool_result_ending_turn(self):
        """A turn ending on a tool_result block still gets the tail marker (native
        format has no text block there) — parity with the OpenRouter path so the
        Multi-Turn breakpoint advances to the turn end."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Q1"),
            ChatMessage(role="assistant", content="", tool_calls=[
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]),
            ChatMessage(role="tool", tool_call_id="c1", content="TOOL RESULT"),
        ]
        _, converted = _make_client("multi_turn")._convert_messages(messages)
        last = converted[-1]
        content = last.get("content")
        assert isinstance(content, list)
        # the tool_result block carries cache_control
        assert any(
            isinstance(b, dict) and b.get("type") == "tool_result" and "cache_control" in b
            for b in content
        ), f"tool_result tail not marked: {content}"

    def test_no_caching_disables_tail_and_cap(self):
        """enable_prompt_caching=False: no markers at all, cap is a no-op."""
        with patch('anthropic.AsyncAnthropic') as mock_anthropic:
            mock_anthropic.return_value = MagicMock()
            from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
            client = AnthropicAsyncClient(
                model="claude-sonnet-4-20250514", api_key="k",
                enable_prompt_caching=False, prompt_cache_mode="multi_turn",
            )
        system_prompt, converted = client._convert_messages(self._CONV)
        client._cap_anthropic_cache(system_prompt, converted, None)
        assert _tail_marked(converted) is False
        assert system_prompt == "You are helpful."


class TestAnthropicSaysWhyTheAnswerEnded:
    """The stream's stop_reason reaches the agent loop in its words: a cut answer
    (max_tokens) as "length" for the truncation guard, a refusal as
    "content_filter" for the fallback chain. Without it both guards were dead
    for Anthropic."""

    @pytest.mark.parametrize("stop_reason, finish_reason", [
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
        ("end_turn", "stop"),
        ("pause_turn", "pause_turn"),  # no word for it in the loop: passed on as sent
    ])
    @pytest.mark.asyncio
    async def test_the_stop_reason_reaches_the_final_event(self, anthropic_client, stop_reason, finish_reason):
        from anthropic.types import Usage

        final_message = MagicMock()
        final_message.usage = Usage(input_tokens=10, output_tokens=5)
        final_message.stop_reason = stop_reason

        async def events():
            return
            yield

        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=None)
        stream.__aiter__ = lambda self: events()
        stream.get_final_message = AsyncMock(return_value=final_message)
        anthropic_client._client.messages.stream = MagicMock(return_value=stream)

        chunks = [chunk async for chunk in anthropic_client.chat_tools_streaming(
            [ChatMessage(role="user", content="Hi")], [])]

        final = next(chunk for chunk in chunks if chunk.get("type") == "final")
        assert final["finish_reason"] == finish_reason


class TestAnthropicChatCancellation:
    """chat() took the token and never looked at it: a cancelled call kept billing."""

    @pytest.mark.asyncio
    async def test_a_cancelled_request_is_not_sent(self, anthropic_client):
        import asyncio
        from types import SimpleNamespace

        anthropic_client._client.messages.create = AsyncMock(side_effect=AssertionError("must not be sent"))

        with pytest.raises(asyncio.CancelledError):
            await anthropic_client.chat([ChatMessage(role="user", content="hi")],
                                        cancellation_token=SimpleNamespace(is_cancelled=True))

        # Without the check before the call the request goes out and is billed;
        # the cancel would then only be the answer to its own failure.
        anthropic_client._client.messages.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_call_ends_it(self, anthropic_client):
        import asyncio
        from types import SimpleNamespace

        token = SimpleNamespace(is_cancelled=False)

        async def slow_answer(**kwargs):
            token.is_cancelled = True
            await asyncio.sleep(30)  # only the cancel can end this

        anthropic_client._client.messages.create = AsyncMock(side_effect=slow_answer)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                anthropic_client.chat([ChatMessage(role="user", content="hi")], cancellation_token=token),
                timeout=5)

    @pytest.mark.asyncio
    async def test_without_a_token_the_answer_comes_back(self, anthropic_client):
        block = MagicMock()
        block.text = "Hello"
        anthropic_client._client.messages.create = AsyncMock(return_value=MagicMock(content=[block]))

        assert await anthropic_client.chat([ChatMessage(role="user", content="hi")]) == "Hello"
