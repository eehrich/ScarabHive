"""Tests for LLM Batch API module.

Tests for:
- BatchQueueManager
- OpenAIBatchClient
- GeminiBatchClient
- Batch request/response models
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import warnings

import pytest

# Filter out deprecation warnings from test setup
warnings.filterwarnings("ignore", category=DeprecationWarning)

from agent_system.llm.batch.models import (
    BatchJob,
    BatchRequest,
    BatchResult,
    BatchStatus,
)
from agent_system.llm.batch.queue_manager import BatchQueueManager
from agent_system.llm.batch.openai_batch import OpenAIBatchClient
from agent_system.llm.batch.gemini_batch import GeminiBatchClient


# ==============================================================================
# Model Tests
# ==============================================================================

class TestBatchRequest:
    """Tests for BatchRequest model."""
    
    def test_create_basic_request(self):
        """Test creating a basic batch request."""
        req = BatchRequest(
            request_id="req_123",
            custom_id="custom_123",
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
        )
        
        assert req.request_id == "req_123"
        assert req.custom_id == "custom_123"
        assert req.model == "gpt-4"
        assert len(req.messages) == 1
        assert req.tools is None
    
    def test_create_request_with_tools(self):
        """Test creating a request with tools."""
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}},
            }
        }]
        
        req = BatchRequest(
            request_id="req_456",
            custom_id="custom_456",
            model="gpt-4",
            messages=[{"role": "user", "content": "Weather?"}],
            tools=tools,
        )
        
        assert req.tools == tools
    
    def test_custom_id_defaults_to_request_id(self):
        """Test that custom_id defaults to request_id if not provided."""
        req = BatchRequest(
            request_id="req_789",
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
        )
        
        assert req.custom_id == "req_789"


class TestBatchJob:
    """Tests for BatchJob model."""
    
    def test_create_job(self):
        """Test creating a batch job."""
        requests = [
            BatchRequest(
                request_id="req_1",
                custom_id="custom_1",
                model="gpt-4",
                messages=[{"role": "user", "content": "Test 1"}],
            ),
            BatchRequest(
                request_id="req_2",
                custom_id="custom_2",
                model="gpt-4",
                messages=[{"role": "user", "content": "Test 2"}],
            ),
        ]
        
        job = BatchJob(
            job_id="job_123",
            model="gpt-4",
            provider="openai",
            requests=requests,
        )
        
        assert job.job_id == "job_123"
        assert job.model == "gpt-4"
        assert job.provider == "openai"
        assert len(job.requests) == 2
        assert job.status == BatchStatus.PENDING
    
    def test_is_terminal_state(self):
        """Test terminal state checking."""
        job = BatchJob(
            job_id="job_1",
            model="gpt-4",
            provider="openai",
            requests=[],
        )
        
        job.status = BatchStatus.PENDING
        assert job.is_terminal is False
        
        job.status = BatchStatus.IN_PROGRESS
        assert job.is_terminal is False
        
        job.status = BatchStatus.COMPLETED
        assert job.is_terminal is True
        
        job.status = BatchStatus.FAILED
        assert job.is_terminal is True
        
        job.status = BatchStatus.CANCELLED
        assert job.is_terminal is True
        
        job.status = BatchStatus.EXPIRED
        assert job.is_terminal is True


class TestBatchResult:
    """Tests for BatchResult model."""
    
    def test_success_result(self):
        """Test successful batch result."""
        result = BatchResult(
            request_id="req_123",
            custom_id="custom_123",
            response={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Hello!",
                    }
                }]
            },
        )
        
        assert result.request_id == "req_123"
        assert result.response is not None
        assert result.error is None
    
    def test_error_result(self):
        """Test error batch result."""
        result = BatchResult(
            request_id="req_456",
            custom_id="custom_456",
            error={"message": "Rate limit exceeded"},
        )
        
        assert result.request_id == "req_456"
        assert result.response is None
        assert result.error is not None


# ==============================================================================
# OpenAI Batch Client Tests
# ==============================================================================

class TestOpenAIBatchClient:
    """Tests for OpenAI Batch API client."""
    
    @pytest.fixture
    def sample_job(self):
        """Create sample batch job."""
        requests = [
            BatchRequest(
                request_id="req_1",
                custom_id="custom_1",
                model="gpt-4",
                messages=[{"role": "user", "content": "Test 1"}],
            ),
            BatchRequest(
                request_id="req_2",
                custom_id="custom_2",
                model="gpt-4",
                messages=[{"role": "user", "content": "Test 2"}],
            ),
        ]
        return BatchJob(
            job_id="job_123",
            model="gpt-4",
            provider="openai",
            requests=requests,
        )
    
    def test_jsonl_format(self, sample_job):
        """Test that the client creates correct JSONL format for batches."""
        # We test the internal structure by creating a file and reading it
        client = OpenAIBatchClient(api_key="test_key")
        
        import tempfile
        from pathlib import Path
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "test.jsonl"
            client._create_input_file(sample_job, file_path)
            
            with open(file_path, 'r') as f:
                lines = f.readlines()
            
            assert len(lines) == 2
            
            line1 = json.loads(lines[0])
            assert line1["custom_id"] == "custom_1"
            assert line1["method"] == "POST"
            assert line1["url"] == "/v1/chat/completions"
            assert line1["body"]["model"] == "gpt-4"
            assert line1["body"]["messages"] == [{"role": "user", "content": "Test 1"}]
    
    def test_jsonl_with_tools(self):
        """Test JSONL building with tools."""
        import tempfile
        from pathlib import Path
        
        tools = [{
            "type": "function",
            "function": {"name": "test", "description": "Test tool"},
        }]
        
        job = BatchJob(
            job_id="job_456",
            model="gpt-4",
            provider="openai",
            requests=[
                BatchRequest(
                    request_id="req_1",
                    custom_id="custom_1",
                    model="gpt-4",
                    messages=[{"role": "user", "content": "Test"}],
                    tools=tools,
                ),
            ],
        )
        
        client = OpenAIBatchClient(api_key="test_key")
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "test.jsonl"
            client._create_input_file(job, file_path)
            
            with open(file_path, 'r') as f:
                content = f.read().strip()
            
            line = json.loads(content)
            assert "tools" in line["body"]
            assert line["body"]["tools"] == tools
    
    @pytest.mark.asyncio
    async def test_submit_batch(self, sample_job, tmp_path):
        """Test batch submission flow."""
        client = OpenAIBatchClient(api_key="test_key")
        
        # Mock the HTTP client (client uses _client, not _http_client)
        mock_http = AsyncMock()
        client._client = mock_http
        
        # Mock file upload response
        upload_response = MagicMock()
        upload_response.status_code = 200
        upload_response.json.return_value = {"id": "file_123"}
        
        # Mock batch creation response
        batch_response = MagicMock()
        batch_response.status_code = 200
        batch_response.json.return_value = {
            "id": "batch_abc123",
            "status": "validating",
        }
        
        mock_http.post.side_effect = [upload_response, batch_response]
        
        batch_id = await client.submit_batch(sample_job, tmp_path)
        
        assert batch_id == "batch_abc123"
        assert mock_http.post.call_count == 2
    
    @pytest.mark.asyncio
    async def test_get_batch_status(self):
        """Test batch status retrieval."""
        client = OpenAIBatchClient(api_key="test_key")
        
        mock_http = AsyncMock()
        client._client = mock_http
        
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "id": "batch_123",
            "status": "completed",
            "request_counts": {
                "total": 2,
                "completed": 2,
                "failed": 0,
            },
            "output_file_id": "file_output_123",
        }
        mock_http.get.return_value = response
        
        status_info = await client.get_batch_status("batch_123")
        
        assert status_info["status"] == BatchStatus.COMPLETED.value
        assert status_info["output_file_id"] == "file_output_123"
    
    @pytest.mark.asyncio
    async def test_cancel_batch(self):
        """Test batch cancellation."""
        client = OpenAIBatchClient(api_key="test_key")
        
        mock_http = AsyncMock()
        client._client = mock_http
        
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"id": "batch_123", "status": "cancelling"}
        mock_http.post.return_value = response
        
        result = await client.cancel_batch("batch_123")
        
        assert result["status"] == "cancelling"
        mock_http.post.assert_called_once()


# ==============================================================================
# Gemini Batch Client Tests
# ==============================================================================

class TestGeminiBatchClient:
    """Tests for Gemini Batch API client."""
    
    @pytest.fixture
    def sample_job(self):
        """Create sample batch job."""
        requests = [
            BatchRequest(
                request_id="req_1",
                custom_id="custom_1",
                model="gemini-1.5-flash",
                messages=[{"role": "user", "content": "Test 1"}],
            ),
        ]
        return BatchJob(
            job_id="job_123",
            model="gemini-1.5-flash",
            provider="google",
            requests=requests,
        )
    
    def test_convert_messages_to_contents(self):
        """Test message conversion to Gemini SDK format."""
        client = GeminiBatchClient(api_key="test_key")
        
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 3
        # SDK types.Content has .role and .parts attributes
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "Hello"
        assert contents[1].role == "model"
        assert contents[1].parts[0].text == "Hi there!"
        assert contents[2].role == "user"
    
    def test_convert_multimodal_messages(self):
        """Test multimodal message conversion."""
        client = GeminiBatchClient(api_key="test_key")
        
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}
                },
            ]
        }]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 1
        assert len(contents[0].parts) == 2
        assert contents[0].parts[0].text == "What's in this image?"
        # Second part should have inline_data
        assert contents[0].parts[1].inline_data is not None
        assert contents[0].parts[1].inline_data.mime_type == "image/png"
    
    def test_convert_messages_with_tool_calls(self):
        """Test converting assistant messages with tool calls."""
        client = GeminiBatchClient(api_key="test_key")
        
        messages = [
            {"role": "user", "content": "What's the weather in Paris?"},
            {
                "role": "assistant",
                "content": "Let me check that for you.",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}'
                        }
                    }
                ]
            }
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 2
        # User message
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "What's the weather in Paris?"
        
        # Assistant with tool call
        assert contents[1].role == "model"
        assert len(contents[1].parts) == 2
        assert contents[1].parts[0].text == "Let me check that for you."
        # SDK Part has function_call attribute
        assert contents[1].parts[1].function_call is not None
        assert contents[1].parts[1].function_call.name == "get_weather"
        assert contents[1].parts[1].function_call.args["city"] == "Paris"
    
    def test_convert_messages_with_tool_results(self):
        """Test converting tool response messages."""
        client = GeminiBatchClient(api_key="test_key")
        
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}'
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "name": "get_weather",
                "content": '{"temperature": 20, "condition": "sunny"}'
            },
            {
                "role": "assistant",
                "content": "It's 20°C and sunny in Paris."
            }
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 4
        
        # Tool response - SDK uses from_function_response which creates a Part
        assert contents[2].role == "tool"
        # For function response, check the part has the response data
        # The SDK Part.from_function_response stores it internally
        assert contents[2].parts[0].function_response is not None
        assert contents[2].parts[0].function_response.name == "get_weather"
        assert contents[2].parts[0].function_response.response["temperature"] == 20
        
        # Final assistant response
        assert contents[3].role == "model"
        assert contents[3].parts[0].text == "It's 20°C and sunny in Paris."
    
    def test_convert_messages_with_thought_signature(self):
        """Test that thought_signature is preserved in function calls for Gemini 3 Pro."""
        client = GeminiBatchClient(api_key="test_key")
        
        # Create messages with thought_signature in extra_content (as stored by gemini_sdk_client)
        messages = [
            {"role": "user", "content": "What's the weather in Paris?"},
            {
                "role": "assistant",
                "content": "Let me check that.",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Paris"}'
                        },
                        "extra_content": {
                            "google": {
                                "thought_signature": b"test_signature_bytes_123"
                            }
                        }
                    }
                ]
            }
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 2
        assert contents[1].role == "model"
        assert len(contents[1].parts) == 2
        
        # Check that thought_signature is preserved in SDK Part
        function_call_part = contents[1].parts[1]
        assert function_call_part.function_call is not None
        assert function_call_part.function_call.name == "get_weather"
        assert function_call_part.thought_signature == b"test_signature_bytes_123"
    
    def test_convert_messages_with_direct_thought_signature(self):
        """Test that direct thought_signature field is also supported."""
        client = GeminiBatchClient(api_key="test_key")
        
        # Some code paths might store thought_signature directly on the tool_call
        messages = [
            {"role": "user", "content": "Calculate something"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_456",
                        "type": "function",
                        "function": {
                            "name": "calculate",
                            "arguments": '{"x": 5}'
                        },
                        "thought_signature": b"direct_signature_xyz"
                    }
                ]
            }
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 2
        function_call_part = contents[1].parts[0]
        assert function_call_part.function_call is not None
        assert function_call_part.function_call.name == "calculate"
        assert function_call_part.thought_signature == b"direct_signature_xyz"
    
    def test_convert_messages_with_base64_thought_signature(self):
        """Test that base64-encoded thought_signature (from JSON serialization) is decoded."""
        import base64
        client = GeminiBatchClient(api_key="test_key")
        
        # When messages are JSON-serialized, bytes become base64 strings
        original_bytes = b"original_signature_bytes"
        base64_encoded = base64.b64encode(original_bytes).decode('utf-8')
        
        messages = [
            {"role": "user", "content": "Test"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_789",
                        "type": "function",
                        "function": {
                            "name": "test_func",
                            "arguments": '{}'
                        },
                        "extra_content": {
                            "google": {
                                "thought_signature": base64_encoded  # String, not bytes
                            }
                        }
                    }
                ]
            }
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 2
        function_call_part = contents[1].parts[0]
        assert function_call_part.function_call is not None
        # Should be decoded back to original bytes
        assert function_call_part.thought_signature == original_bytes
    
    def test_convert_messages_without_thought_signature(self):
        """Test that missing thought_signature doesn't cause issues."""
        client = GeminiBatchClient(api_key="test_key")
        
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_789",
                        "type": "function",
                        "function": {
                            "name": "greet",
                            "arguments": '{"name": "World"}'
                        }
                        # No thought_signature or extra_content
                    }
                ]
            }
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 2
        function_call_part = contents[1].parts[0]
        assert function_call_part.function_call is not None
        assert function_call_part.function_call.name == "greet"
        # thought_signature should be None
        assert function_call_part.thought_signature is None


# ==============================================================================
# Queue Manager Tests
# ==============================================================================

class TestBatchQueueManager:
    """Tests for BatchQueueManager."""
    
    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create mock batch config."""
        config = MagicMock()
        config.enabled = True
        config.collection_window_seconds = 1.0  # Short for tests
        config.max_requests_per_batch = 10
        config.poll_interval_seconds = 0.5
        config.max_wait_hours = 24
        config.storage_path = str(tmp_path / "batch")
        return config
    
    @pytest.fixture
    def mock_openai_client(self):
        """Create mock OpenAI batch client."""
        client = AsyncMock()
        client.submit_batch = AsyncMock(return_value="batch_openai_123")
        client.get_batch_status = AsyncMock(return_value={
            "status": BatchStatus.COMPLETED.value,
            "output_file_id": "file_out_123",
        })
        client.get_batch_results = AsyncMock(return_value=[{
            "custom_id": "custom_1",
            "response": {"choices": [{"message": {"content": "Test"}}]},
            "error": None,
        }])
        return client
    
    @pytest.fixture
    def mock_gemini_client(self):
        """Create mock Gemini batch client."""
        client = AsyncMock()
        client.submit_batch = AsyncMock(return_value="batch_gemini_123")
        client.get_batch_status = AsyncMock(return_value={
            "status": BatchStatus.COMPLETED.value,
        })
        client.get_batch_results = AsyncMock(return_value=[{
            "custom_id": "custom_1",
            "response": {"choices": [{"message": {"content": "Test"}}]},
            "error": None,
        }])
        return client
    
    @pytest.fixture
    def manager(self, mock_config, mock_openai_client, mock_gemini_client):
        """Create batch queue manager."""
        manager = BatchQueueManager(mock_config)
        manager.register_batch_client("openai", mock_openai_client)
        manager.register_batch_client("gemini", mock_gemini_client)
        return manager
    
    @pytest.mark.asyncio
    async def test_submit_request_to_queue(self, manager):
        """Test submitting a request to the internal queue."""
        # Access the internal queue directly since submit_request is async
        request = BatchRequest(
            request_id="req_1",
            custom_id="custom_1",
            model="gpt-4",
            messages=[{"role": "user", "content": "Test"}],
        )
        
        # Add directly to queue for testing
        manager._queues["gpt-4"].append(request)
        
        assert "gpt-4" in manager._queues
        assert len(manager._queues["gpt-4"]) == 1
    
    @pytest.mark.asyncio
    async def test_queue_grouping_by_model(self, manager):
        """Test that requests are grouped by model in internal queue."""
        # Add requests directly to queues for testing
        req1 = BatchRequest(
            request_id="req_1",
            custom_id="custom_1",
            model="gpt-4",
            messages=[{"role": "user", "content": "Test 1"}],
        )
        req2 = BatchRequest(
            request_id="req_2",
            custom_id="custom_2",
            model="gpt-4",
            messages=[{"role": "user", "content": "Test 2"}],
        )
        req3 = BatchRequest(
            request_id="req_3",
            custom_id="custom_3",
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Test 3"}],
        )
        
        manager._queues["gpt-4"].append(req1)
        manager._queues["gpt-4"].append(req2)
        manager._queues["gpt-3.5-turbo"].append(req3)
        
        assert len(manager._queues["gpt-4"]) == 2
        assert len(manager._queues["gpt-3.5-turbo"]) == 1
    
    @pytest.mark.asyncio
    async def test_get_metrics(self, manager):
        """Test metrics retrieval."""
        metrics = manager.get_metrics()
        
        assert hasattr(metrics, "total_jobs")
        assert hasattr(metrics, "completed_jobs")
        assert hasattr(metrics, "failed_jobs")
        assert hasattr(metrics, "total_requests")
    
    @pytest.mark.asyncio
    async def test_start_stop(self, manager):
        """Test manager start and stop."""
        await manager.start()
        assert manager._running is True
        
        await manager.stop()
        assert manager._running is False
    
    @pytest.mark.asyncio
    async def test_register_batch_client(self, mock_config):
        """Test registering batch clients."""
        manager = BatchQueueManager(mock_config)
        
        mock_client = AsyncMock()
        manager.register_batch_client("openai", mock_client)
        
        assert "openai" in manager._batch_clients
        assert manager._batch_clients["openai"] == mock_client

    @pytest.mark.asyncio
    async def test_cancel_request_from_queue(self, mock_config):
        """Test cancelling a request that hasn't been submitted yet."""
        manager = BatchQueueManager(mock_config)
        await manager.start()
        
        # Add a request to the queue manually
        request = BatchRequest(
            request_id="test-cancel-1",
            custom_id="custom-1",
            model="gpt-4",
            messages=[{"role": "user", "content": "Test"}],
        )
        manager._queues["openai:gpt-4"].append(request)
        
        # Create a future for this request
        future = asyncio.get_event_loop().create_future()
        manager._request_futures[request.request_id] = future
        
        # Cancel the request
        result = await manager.cancel_request("test-cancel-1")
        
        assert result is True
        assert len(manager._queues["openai:gpt-4"]) == 0
        assert future.done()
        
        await manager.stop()

    @pytest.mark.asyncio
    async def test_cancel_request_after_submission(self, mock_config):
        """Test cancelling a request that has been submitted to provider."""
        manager = BatchQueueManager(mock_config)
        await manager.start()
        
        # Create a job with a request
        request = BatchRequest(
            request_id="test-cancel-2",
            custom_id="custom-2",
            model="gpt-4",
            messages=[{"role": "user", "content": "Test"}],
        )
        job = BatchJob(
            job_id="job-test",
            provider="openai",
            model="gpt-4",
            requests=[request],
        )
        job.provider_job_id = "batch_abc123"
        
        # Set up the manager state
        manager._active_jobs[job.job_id] = job
        manager._request_to_job[request.request_id] = job.job_id
        
        # Register a mock client
        mock_client = AsyncMock()
        mock_client.cancel_batch = AsyncMock(return_value={"status": "cancelled"})
        manager.register_batch_client("openai", mock_client)
        
        # Cancel the request
        result = await manager.cancel_request("test-cancel-2")
        
        assert result is True
        mock_client.cancel_batch.assert_called_once_with("batch_abc123")
        assert job.status == BatchStatus.CANCELLED
        
        await manager.stop()

    @pytest.mark.asyncio  
    async def test_request_to_job_mapping_cleanup(self, mock_config):
        """Test that request-to-job mapping is cleaned up after job completion."""
        manager = BatchQueueManager(mock_config)
        
        # Create a job with requests
        requests = [
            BatchRequest(
                request_id=f"req-{i}",
                custom_id=f"custom-{i}",
                model="gpt-4",
                messages=[{"role": "user", "content": f"Test {i}"}],
            )
            for i in range(3)
        ]
        job = BatchJob(
            job_id="job-cleanup",
            provider="openai",
            model="gpt-4",
            requests=requests,
        )
        
        # Set up mappings
        manager._active_jobs[job.job_id] = job
        for req in requests:
            manager._request_to_job[req.request_id] = job.job_id
        
        # Complete the job
        job.status = BatchStatus.COMPLETED
        await manager._complete_job(job)
        
        # Verify cleanup
        assert job.job_id not in manager._active_jobs
        assert job.job_id in manager._completed_jobs
        for req in requests:
            assert req.request_id not in manager._request_to_job


# ==============================================================================
# Integration Tests
# ==============================================================================

class TestBatchIntegration:
    """Integration tests for batch processing."""
    
    @pytest.fixture
    def batch_config(self, tmp_path):
        """Create batch config for integration tests."""
        config = MagicMock()
        config.enabled = True
        config.collection_window_seconds = 0.5
        config.max_requests_per_batch = 5
        config.poll_interval_seconds = 0.2
        config.max_wait_hours = 24
        config.storage_path = str(tmp_path / "batch")
        return config
    
    @pytest.mark.asyncio
    async def test_manager_lifecycle(self, batch_config):
        """Test complete manager lifecycle."""
        manager = BatchQueueManager(batch_config)
        
        # Mock the batch clients
        mock_openai = AsyncMock()
        mock_openai.cancel_all_pending_batches = AsyncMock(return_value=0)
        manager.register_batch_client("openai", mock_openai)
        
        await manager.start()
        assert manager._running is True
        
        await manager.stop()
        assert manager._running is False
    
    def test_batch_status_enum(self):
        """Test BatchStatus enum values."""
        assert BatchStatus.PENDING.value == "pending"
        assert BatchStatus.IN_PROGRESS.value == "in_progress"
        assert BatchStatus.COMPLETED.value == "completed"
        assert BatchStatus.FAILED.value == "failed"
        assert BatchStatus.CANCELLED.value == "cancelled"
        assert BatchStatus.EXPIRED.value == "expired"
    
    def test_batch_job_request_count(self):
        """Test BatchJob request counting."""
        requests = [
            BatchRequest(
                request_id=f"req_{i}",
                custom_id=f"custom_{i}",
                model="gpt-4",
                messages=[{"role": "user", "content": f"Test {i}"}],
            )
            for i in range(5)
        ]
        
        job = BatchJob(
            job_id="job_test",
            model="gpt-4",
            provider="openai",
            requests=requests,
        )
        
        assert job.request_count == 5


# ==============================================================================
# BatchLLMClient Tests
# ==============================================================================

class TestBatchLLMClient:
    """Tests for BatchLLMClient wrapper."""
    
    @pytest.fixture
    def batch_provider_config(self, tmp_path):
        """Create a batch provider config."""
        from agent_system.config.models import BatchProviderConfig
        config = BatchProviderConfig()
        config.enabled = True
        config.collection_window_seconds = 1.0
        config.max_requests_per_batch = 10
        config.poll_interval_seconds = 1.0
        config.max_wait_hours = 1.0
        config.fallback_to_sync = True
        return config
    
    @pytest.fixture
    def mock_underlying_client(self):
        """Create a mock underlying LLM client."""
        client = AsyncMock()
        client.context_window = 100000
        client.model = "gpt-4"
        client.chat = AsyncMock(return_value="Hello!")
        client.chat_tools = AsyncMock(return_value={
            "assistant": {"content": "Test response", "tool_calls": None}
        })
        client.supports_streaming = MagicMock(return_value=True)
        return client
    
    @pytest.fixture
    def mock_queue_manager(self):
        """Create a mock BatchQueueManager."""
        manager = AsyncMock()
        manager.submit_request = AsyncMock(return_value={
            "assistant": {"content": "Batch response", "tool_calls": None}
        })
        return manager
    
    def test_create_batch_client(self, mock_underlying_client, mock_queue_manager, batch_provider_config):
        """Test creating a BatchLLMClient."""
        from agent_system.llm.batch.batch_client import BatchLLMClient
        
        client = BatchLLMClient(
            underlying_client=mock_underlying_client,
            queue_manager=mock_queue_manager,
            batch_provider_config=batch_provider_config,
            model_name="gpt-4",
            batch_provider="openai",
        )
        
        assert client.model_name == "gpt-4"
        assert client.batch_provider == "openai"
        assert client.context_window == 100000
    
    @pytest.mark.asyncio
    async def test_chat_tools_uses_batch_queue(
        self, mock_underlying_client, mock_queue_manager, batch_provider_config
    ):
        """Test that chat_tools routes through batch queue."""
        from agent_system.llm.batch.batch_client import BatchLLMClient
        from agent_system.llm.models import ChatMessage
        
        client = BatchLLMClient(
            underlying_client=mock_underlying_client,
            queue_manager=mock_queue_manager,
            batch_provider_config=batch_provider_config,
            model_name="gpt-4",
            batch_provider="openai",
        )
        
        messages = [ChatMessage(role="user", content="Hello")]
        tools = [{"type": "function", "function": {"name": "test"}}]
        
        result = await client.chat_tools(messages, tools)
        
        # Should call queue manager, not underlying client
        mock_queue_manager.submit_request.assert_called_once()
        mock_underlying_client.chat_tools.assert_not_called()
        
        assert result["assistant"]["content"] == "Batch response"
    
    @pytest.mark.asyncio
    async def test_fallback_on_batch_failure(
        self, mock_underlying_client, mock_queue_manager, batch_provider_config
    ):
        """Test fallback to sync when batch fails."""
        from agent_system.llm.batch.batch_client import BatchLLMClient
        from agent_system.llm.models import ChatMessage
        
        # Make queue manager fail
        mock_queue_manager.submit_request = AsyncMock(side_effect=Exception("Batch failed"))
        
        client = BatchLLMClient(
            underlying_client=mock_underlying_client,
            queue_manager=mock_queue_manager,
            batch_provider_config=batch_provider_config,
            model_name="gpt-4",
            batch_provider="openai",
        )
        
        messages = [ChatMessage(role="user", content="Hello")]
        tools = [{"type": "function", "function": {"name": "test"}}]
        
        result = await client.chat_tools(messages, tools)
        
        # Should fall back to underlying client
        mock_underlying_client.chat_tools.assert_called_once()
        assert result["assistant"]["content"] == "Test response"
    
    @pytest.mark.asyncio
    async def test_no_fallback_when_disabled(
        self, mock_underlying_client, mock_queue_manager, batch_provider_config
    ):
        """Test that fallback is not used when disabled."""
        from agent_system.llm.batch.batch_client import BatchLLMClient
        from agent_system.llm.models import ChatMessage
        
        # Disable fallback
        batch_provider_config.fallback_to_sync = False
        
        # Make queue manager fail
        mock_queue_manager.submit_request = AsyncMock(side_effect=Exception("Batch failed"))
        
        client = BatchLLMClient(
            underlying_client=mock_underlying_client,
            queue_manager=mock_queue_manager,
            batch_provider_config=batch_provider_config,
            model_name="gpt-4",
            batch_provider="openai",
        )
        
        messages = [ChatMessage(role="user", content="Hello")]
        tools = [{"type": "function", "function": {"name": "test"}}]
        
        with pytest.raises(RuntimeError, match="Batch request failed"):
            await client.chat_tools(messages, tools)
    
    def test_supports_streaming_with_fallback(
        self, mock_underlying_client, mock_queue_manager, batch_provider_config
    ):
        """Test streaming support depends on fallback setting."""
        from agent_system.llm.batch.batch_client import BatchLLMClient
        
        # With fallback enabled
        batch_provider_config.fallback_to_sync = True
        client = BatchLLMClient(
            underlying_client=mock_underlying_client,
            queue_manager=mock_queue_manager,
            batch_provider_config=batch_provider_config,
            model_name="gpt-4",
            batch_provider="openai",
        )
        assert client.supports_streaming() is True
        
        # Without fallback
        batch_provider_config.fallback_to_sync = False
        client2 = BatchLLMClient(
            underlying_client=mock_underlying_client,
            queue_manager=mock_queue_manager,
            batch_provider_config=batch_provider_config,
            model_name="gpt-4",
            batch_provider="openai",
        )
        assert client2.supports_streaming() is False


# ==============================================================================
# Schema Sanitization Tests
# ==============================================================================

class TestSchemaSanitization:
    """Tests for JSON Schema sanitization in Gemini batch client."""
    
    @pytest.fixture
    def gemini_client(self) -> GeminiBatchClient:
        """Create a Gemini batch client for testing."""
        client = GeminiBatchClient(api_key="test-key")
        return client
    
    def test_sanitize_removes_oneof(self, gemini_client):
        """Test that oneOf is removed from schemas."""
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "integer"}
                    ]
                }
            }
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        assert "oneOf" not in result.get("properties", {}).get("value", {})
        # First option should be merged
        assert result["properties"]["value"].get("type") == "string"
    
    def test_sanitize_removes_anyof(self, gemini_client):
        """Test that anyOf is removed from schemas."""
        schema = {
            "type": "object", 
            "properties": {
                "score": {
                    "anyOf": [
                        {"type": "number"},
                        {"type": "null"}
                    ]
                }
            }
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        assert "anyOf" not in result.get("properties", {}).get("score", {})
    
    def test_sanitize_nested_properties(self, gemini_client):
        """Test that nested properties are sanitized recursively."""
        schema = {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {
                            "oneOf": [
                                {"type": "boolean"},
                                {"type": "string"}
                            ]
                        }
                    }
                }
            }
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        inner_prop = result["properties"]["outer"]["properties"]["inner"]
        assert "oneOf" not in inner_prop
        assert inner_prop.get("type") == "boolean"
    
    def test_sanitize_array_items(self, gemini_client):
        """Test that array items are sanitized."""
        schema = {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "number"}
                ]
            }
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        assert "oneOf" not in result.get("items", {})
    
    def test_sanitize_removes_additional_properties(self, gemini_client):
        """Test that additionalProperties is removed from schemas."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "additionalProperties": False,
            "required": ["name"]
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        assert "additionalProperties" not in result
        assert result["type"] == "object"
        assert result["required"] == ["name"]
    
    def test_sanitize_removes_default_and_examples(self, gemini_client):
        """Test that default and examples are removed from schemas."""
        schema = {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "default": 10,
                    "examples": [1, 5, 10]
                },
            }
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        assert "default" not in result["properties"]["count"]
        assert "examples" not in result["properties"]["count"]
        assert result["properties"]["count"]["type"] == "integer"
    
    def test_sanitize_preserves_valid_keywords(self, gemini_client):
        """Test that valid JSON Schema keywords are preserved."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A name"},
                "age": {"type": "integer", "minimum": 0},
            },
            "required": ["name"]
        }
        
        result = gemini_client._sanitize_schema_for_sdk(schema)
        
        assert result["type"] == "object"
        assert result["required"] == ["name"]
        assert result["properties"]["name"]["type"] == "string"
        assert result["properties"]["name"]["description"] == "A name"
        assert result["properties"]["age"]["minimum"] == 0


class TestSDKToolConversion:
    """Tests for SDK tool conversion in Gemini batch client."""
    
    @pytest.fixture
    def gemini_client(self) -> GeminiBatchClient:
        """Create a Gemini batch client for testing."""
        client = GeminiBatchClient(api_key="test-key")
        return client
    
    def test_convert_tools_creates_sdk_types(self, gemini_client):
        """Test that tools are converted to SDK types.Tool objects."""
        tools = [{
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search for items",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}
                    }
                }
            }
        }]
        
        from google.genai import types
        
        result = gemini_client._convert_tools_to_sdk(tools)
        
        assert len(result) == 1
        assert isinstance(result[0], types.Tool)
        assert len(result[0].function_declarations) == 1
        assert result[0].function_declarations[0].name == "search"
    
    def test_convert_tools_sanitizes_schemas(self, gemini_client):
        """Test that tool schemas are sanitized during conversion."""
        tools = [{
            "type": "function",
            "function": {
                "name": "update_record",
                "description": "Update a record",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_ids": {
                            "oneOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "null"}
                            ]
                        },
                        "score": {
                            "oneOf": [
                                {"type": "number"},
                                {"type": "null"}
                            ]
                        }
                    }
                }
            }
        }]
        
        result = gemini_client._convert_tools_to_sdk(tools)
        
        assert len(result) == 1
        func_decl = result[0].function_declarations[0]
        params = func_decl.parameters
        
        # oneOf should be removed from the parameters
        if params and "properties" in params:
            for prop_name, prop_def in params["properties"].items():
                assert "oneOf" not in prop_def, f"oneOf found in {prop_name}"


class TestSDKResultsExtraction:
    """Tests for SDK results extraction with function calls."""
    
    @pytest.fixture
    def gemini_client_sdk(self) -> GeminiBatchClient:
        """Create a Gemini batch client in SDK mode."""
        client = GeminiBatchClient(api_key="test-key")
        return client
    
    @pytest.mark.asyncio
    async def test_get_batch_results_with_function_calls(self, gemini_client_sdk):
        """Test extracting results with function calls from SDK batch job."""
        from google.genai import types
        
        # Mock batch job
        job = BatchJob(
            job_id="test_job",
            model="gemini-2.5-flash-preview-05-20",
            provider="google",
            requests=[
                BatchRequest(
                    request_id="req_1",
                    custom_id="test_1",
                    model="gemini-2.5-flash-preview-05-20",
                    messages=[{"role": "user", "content": "What's the weather?"}],
                )
            ],
            status=BatchStatus.COMPLETED,
            provider_job_id="batch_123",
        )
        
        # Mock SDK response with function_call
        mock_fc = MagicMock()
        mock_fc.name = "get_weather"
        mock_fc.args = {"location": "Berlin"}
        
        mock_fc_part = MagicMock()
        mock_fc_part.function_call = mock_fc
        mock_fc_part.text = None
        
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_fc_part]
        
        # Mock usage metadata
        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.response_token_count = 50
        mock_usage.candidates_token_count = 50
        mock_usage.total_token_count = 150
        mock_usage.cached_content_token_count = None
        
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = mock_usage
        
        mock_inline_response = MagicMock()
        mock_inline_response.response = mock_response
        mock_inline_response.error = None
        
        # Mock SDK batch job with dest.inlined_responses
        mock_dest = MagicMock()
        mock_dest.inlined_responses = [mock_inline_response]
        
        mock_sdk_batch = MagicMock()
        mock_sdk_batch.dest = mock_dest
        
        with patch.object(gemini_client_sdk._sdk_client.batches, 'get', return_value=mock_sdk_batch):
            results = await gemini_client_sdk.get_batch_results(job)
        
        assert len(results) == 1
        assert results[0]["custom_id"] == "test_1"
        assert results[0]["error"] is None
        
        # Check function call extraction
        message = results[0]["response"]["choices"][0]["message"]
        assert "tool_calls" in message
        assert len(message["tool_calls"]) == 1
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"
        
        # Verify arguments are JSON serialized
        args = json.loads(message["tool_calls"][0]["function"]["arguments"])
        assert args["location"] == "Berlin"
    
    @pytest.mark.asyncio
    async def test_get_batch_results_with_text_and_function_call(self, gemini_client_sdk):
        """Test extracting results with both text and function calls."""
        from google.genai import types
        
        job = BatchJob(
            job_id="test_job",
            model="gemini-2.5-flash-preview-05-20",
            provider="google",
            requests=[
                BatchRequest(
                    request_id="req_1",
                    custom_id="test_1",
                    model="gemini-2.5-flash-preview-05-20",
                    messages=[{"role": "user", "content": "Search for cats"}],
                )
            ],
            status=BatchStatus.COMPLETED,
            provider_job_id="batch_123",
        )
        
        # Mock SDK response with both text and function_call
        mock_text_part = MagicMock()
        mock_text_part.text = "Let me search for that."
        mock_text_part.function_call = None
        
        mock_fc = MagicMock()
        mock_fc.name = "web_search"
        mock_fc.args = {"query": "cats"}
        
        mock_fc_part = MagicMock()
        mock_fc_part.text = None
        mock_fc_part.function_call = mock_fc
        
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_text_part, mock_fc_part]
        
        # Mock usage metadata
        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.response_token_count = 50
        mock_usage.candidates_token_count = 50
        mock_usage.total_token_count = 150
        mock_usage.cached_content_token_count = None
        
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = mock_usage
        
        mock_inline_response = MagicMock()
        mock_inline_response.response = mock_response
        mock_inline_response.error = None
        
        mock_dest = MagicMock()
        mock_dest.inlined_responses = [mock_inline_response]
        
        mock_sdk_batch = MagicMock()
        mock_sdk_batch.dest = mock_dest
        
        with patch.object(gemini_client_sdk._sdk_client.batches, 'get', return_value=mock_sdk_batch):
            results = await gemini_client_sdk.get_batch_results(job)
        
        assert len(results) == 1
        message = results[0]["response"]["choices"][0]["message"]
        
        # Should have both content and tool_calls
        assert message["content"] == "Let me search for that."
        assert "tool_calls" in message
        assert len(message["tool_calls"]) == 1
        assert message["tool_calls"][0]["function"]["name"] == "web_search"
    
    @pytest.mark.asyncio
    async def test_get_batch_results_text_only(self, gemini_client_sdk):
        """Test extracting results with text only (no function calls)."""
        job = BatchJob(
            job_id="test_job",
            model="gemini-2.5-flash-preview-05-20",
            provider="google",
            requests=[
                BatchRequest(
                    request_id="req_1",
                    custom_id="test_1",
                    model="gemini-2.5-flash-preview-05-20",
                    messages=[{"role": "user", "content": "Hello"}],
                )
            ],
            status=BatchStatus.COMPLETED,
            provider_job_id="batch_123",
        )
        
        # Mock SDK response with text only
        mock_text_part = MagicMock()
        mock_text_part.text = "Hello! How can I help you?"
        mock_text_part.function_call = None
        
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_text_part]
        
        # Mock usage metadata
        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.response_token_count = 50
        mock_usage.candidates_token_count = 50
        mock_usage.total_token_count = 150
        mock_usage.cached_content_token_count = None
        
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = mock_usage
        
        mock_inline_response = MagicMock()
        mock_inline_response.response = mock_response
        mock_inline_response.error = None
        
        mock_dest = MagicMock()
        mock_dest.inlined_responses = [mock_inline_response]
        
        mock_sdk_batch = MagicMock()
        mock_sdk_batch.dest = mock_dest
        
        with patch.object(gemini_client_sdk._sdk_client.batches, 'get', return_value=mock_sdk_batch):
            results = await gemini_client_sdk.get_batch_results(job)
        
        assert len(results) == 1
        message = results[0]["response"]["choices"][0]["message"]
        
        # Should have content but no tool_calls
        assert message["content"] == "Hello! How can I help you?"
        assert "tool_calls" not in message or not message.get("tool_calls")
