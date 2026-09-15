"""Tests for Anthropic Message Batches API client."""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from plugins_llm.llm_anthropic.anthropic_batch import AnthropicBatchClient
from agent_system.llm.batch.models import BatchJob, BatchRequest, BatchStatus


@pytest.fixture
def anthropic_batch_client():
    """Create an AnthropicBatchClient instance for testing."""
    with patch('httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        
        client = AnthropicBatchClient(
            api_key="test-api-key",
            default_model="claude-sonnet-4-20250514"
        )
        client._mock = mock_client
        return client


class TestAnthropicBatchMessageConversion:
    """Test OpenAI to Anthropic message format conversion."""

    def test_convert_simple_messages(self, anthropic_batch_client):
        """Test conversion of simple user/assistant messages."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        
        system_prompt, converted = anthropic_batch_client._convert_openai_messages_to_anthropic(messages)
        
        assert system_prompt is None
        assert len(converted) == 2
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello"
        assert converted[1]["role"] == "assistant"
        assert converted[1]["content"] == "Hi there!"

    def test_convert_system_message(self, anthropic_batch_client):
        """Test system message extraction."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        
        system_prompt, converted = anthropic_batch_client._convert_openai_messages_to_anthropic(messages)
        
        assert system_prompt == "You are helpful."
        assert len(converted) == 1

    def test_convert_tool_response(self, anthropic_batch_client):
        """Test tool response conversion."""
        messages = [
            {
                "role": "tool",
                "tool_call_id": "toolu_123",
                "content": '{"result": "success"}'
            },
        ]
        
        system_prompt, converted = anthropic_batch_client._convert_openai_messages_to_anthropic(messages)
        
        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"][0]["type"] == "tool_result"
        assert converted[0]["content"][0]["tool_use_id"] == "toolu_123"

    def test_convert_assistant_with_tool_calls(self, anthropic_batch_client):
        """Test assistant message with tool calls."""
        messages = [
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {
                        "id": "toolu_123",
                        "type": "function",
                        "function": {
                            "name": "get_data",
                            "arguments": '{"id": 1}'
                        }
                    }
                ]
            },
        ]
        
        system_prompt, converted = anthropic_batch_client._convert_openai_messages_to_anthropic(messages)
        
        assert len(converted) == 1
        content = converted[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "tool_use"
        assert content[1]["id"] == "toolu_123"


class TestAnthropicBatchToolConversion:
    """Test OpenAI to Anthropic tool schema conversion."""

    def test_convert_tools(self, anthropic_batch_client):
        """Test tool conversion."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search for data",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"}
                        }
                    }
                }
            }
        ]
        
        converted = anthropic_batch_client._convert_openai_tools_to_anthropic(tools)
        
        assert len(converted) == 1
        assert converted[0]["name"] == "search"
        assert converted[0]["description"] == "Search for data"
        assert "input_schema" in converted[0]


class TestAnthropicBatchResultParsing:
    """Test batch result parsing."""

    def test_parse_succeeded_result(self, anthropic_batch_client):
        """Test parsing of succeeded result."""
        jsonl = json.dumps({
            "custom_id": "req-1",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hello!"}
                    ],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5
                    }
                }
            }
        })
        
        results = anthropic_batch_client._parse_results(jsonl)
        
        assert len(results) == 1
        assert results[0]["custom_id"] == "req-1"
        assert results[0]["error"] is None
        assert results[0]["response"]["choices"][0]["message"]["content"] == "Hello!"
        assert results[0]["usage"]["prompt_tokens"] == 10
        assert results[0]["usage"]["completion_tokens"] == 5

    def test_parse_succeeded_counts_cache_tokens(self, anthropic_batch_client):
        """Same usage semantics as the streaming client: prompt_tokens includes
        cache reads and writes."""
        jsonl = json.dumps({
            "custom_id": "req-1",
            "result": {"type": "succeeded", "message": {
                "content": [{"type": "text", "text": "Hi"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": 80,
                          "cache_creation_input_tokens": 20},
            }},
        })

        (result,) = anthropic_batch_client._parse_results(jsonl)

        assert result["usage"] == {
            "prompt_tokens": 110, "completion_tokens": 5, "total_tokens": 115,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_tokens": 20},
        }
        assert result["response"]["usage"] == result["usage"]

    def test_parse_succeeded_with_null_usage_fields(self, anthropic_batch_client):
        """JSON nulls -- for the whole usage or single counts -- must not drop the result."""
        lines = [
            json.dumps({"custom_id": "req-1", "result": {"type": "succeeded", "message": {
                "content": [{"type": "text", "text": "a"}], "usage": None}}}),
            json.dumps({"custom_id": "req-2", "result": {"type": "succeeded", "message": {
                "content": [{"type": "text", "text": "b"}],
                "usage": {"input_tokens": 10, "output_tokens": 5,
                          "cache_read_input_tokens": None,
                          "cache_creation_input_tokens": None}}}}),
        ]

        first, second = anthropic_batch_client._parse_results("\n".join(lines))

        assert first["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        assert second["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_parse_succeeded_with_tool_use(self, anthropic_batch_client):
        """Test parsing of succeeded result with tool calls."""
        jsonl = json.dumps({
            "custom_id": "req-1",
            "result": {
                "type": "succeeded",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "get_weather",
                            "input": {"location": "Paris"}
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 20, "output_tokens": 15}
                }
            }
        })
        
        results = anthropic_batch_client._parse_results(jsonl)
        
        assert len(results) == 1
        msg = results[0]["response"]["choices"][0]["message"]
        assert msg["content"] == "Let me check."
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_parse_errored_result(self, anthropic_batch_client):
        """Test parsing of errored result."""
        jsonl = json.dumps({
            "custom_id": "req-1",
            "result": {
                "type": "errored",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Invalid request"
                }
            }
        })
        
        results = anthropic_batch_client._parse_results(jsonl)
        
        assert len(results) == 1
        assert results[0]["custom_id"] == "req-1"
        assert results[0]["response"] is None
        assert "Invalid request" in results[0]["error"]

    def test_parse_multiple_results(self, anthropic_batch_client):
        """Test parsing of multiple results."""
        jsonl = "\n".join([
            json.dumps({
                "custom_id": "req-1",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [{"type": "text", "text": "Result 1"}],
                        "usage": {"input_tokens": 5, "output_tokens": 3}
                    }
                }
            }),
            json.dumps({
                "custom_id": "req-2",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [{"type": "text", "text": "Result 2"}],
                        "usage": {"input_tokens": 6, "output_tokens": 4}
                    }
                }
            })
        ])
        
        results = anthropic_batch_client._parse_results(jsonl)
        
        assert len(results) == 2
        assert results[0]["custom_id"] == "req-1"
        assert results[1]["custom_id"] == "req-2"


class TestAnthropicBatchStatusMapping:
    """Test status mapping from Anthropic to internal status.

    The Message Batches object has NO `end_status` field — only
    `processing_status` (in_progress / canceling / ended) and per-request
    counts. Reading a nonexistent field made every ended batch report
    COMPLETED, cancelled and expired ones included, and left the
    FAILED/CANCELLED/EXPIRED branches as dead code. The outcome now comes
    from the counts, so these mocks carry only fields the API really sends.
    """

    async def _status_for(self, client, **payload):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "batch_123", **payload}
        client._client.get = AsyncMock(return_value=mock_response)
        result = await client.get_batch_status("batch_123")
        return result["status"]

    @pytest.mark.asyncio
    async def test_status_in_progress(self, anthropic_batch_client):
        """Test in_progress status mapping."""
        status = await self._status_for(
            anthropic_batch_client, processing_status="in_progress",
            request_counts={"total": 10, "succeeded": 0, "processing": 10})
        assert status == BatchStatus.IN_PROGRESS.value

    @pytest.mark.asyncio
    async def test_status_completed(self, anthropic_batch_client):
        """Test completed status mapping."""
        status = await self._status_for(
            anthropic_batch_client, processing_status="ended",
            request_counts={"total": 10, "succeeded": 10})
        assert status == BatchStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_status_failed(self, anthropic_batch_client):
        """Test failed status mapping."""
        status = await self._status_for(
            anthropic_batch_client, processing_status="ended",
            request_counts={"total": 10, "errored": 10})
        assert status == BatchStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_a_cancelled_batch_is_not_reported_as_completed(
            self, anthropic_batch_client):
        status = await self._status_for(
            anthropic_batch_client, processing_status="ended",
            request_counts={"total": 10, "succeeded": 0, "canceled": 10})
        assert status == BatchStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_an_expired_batch_is_not_reported_as_completed(
            self, anthropic_batch_client):
        status = await self._status_for(
            anthropic_batch_client, processing_status="ended",
            request_counts={"total": 10, "succeeded": 0, "expired": 10})
        assert status == BatchStatus.EXPIRED.value

    @pytest.mark.asyncio
    async def test_partial_success_stays_completed(self, anthropic_batch_client):
        """Individual failures are reported per request by _parse_results —
        the queue manager still needs the successful half."""
        status = await self._status_for(
            anthropic_batch_client, processing_status="ended",
            request_counts={"total": 10, "succeeded": 7, "errored": 3})
        assert status == BatchStatus.COMPLETED.value


class TestAnthropicBatchSubmission:
    """Test batch submission."""

    @pytest.mark.asyncio
    async def test_submit_batch(self, anthropic_batch_client):
        """Test batch submission."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "batch_abc123"}
        
        anthropic_batch_client._client.post = AsyncMock(return_value=mock_response)
        
        job = BatchJob(
            job_id="job_1",
            requests=[
                BatchRequest(
                    custom_id="req-1",
                    model="claude-sonnet-4-20250514",
                    messages=[{"role": "user", "content": "Hello"}],
                    tools=None
                )
            ]
        )
        
        with patch('plugins_llm.llm_anthropic.anthropic_batch.get_job_tracker', return_value=None):
            batch_id = await anthropic_batch_client.submit_batch(job, Path("/tmp"))
        
        assert batch_id == "batch_abc123"
        anthropic_batch_client._client.post.assert_called_once()


class TestAnthropicBatchImageConversion:
    """Test image content conversion using anthropic_utils."""

    def test_convert_base64_image(self):
        """Test base64 image conversion."""
        from plugins_llm.llm_anthropic import anthropic_utils
        
        item = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "abc123"
            }
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/png"
        assert result["source"]["data"] == "abc123"

    def test_convert_url_image(self):
        """Test URL image conversion."""
        from plugins_llm.llm_anthropic import anthropic_utils
        
        item = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.png"}
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "url"
        assert result["source"]["url"] == "https://example.com/img.png"

    def test_convert_data_url_image(self):
        """Test data URL image conversion."""
        from plugins_llm.llm_anthropic import anthropic_utils
        
        item = {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}
        }
        
        result = anthropic_utils._convert_image_content(item)
        
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/jpeg"
        assert result["source"]["data"] == "/9j/4AAQ"
