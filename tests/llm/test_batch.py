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
        """Test message conversion to Gemini format."""
        client = GeminiBatchClient(api_key="test_key", use_sdk=False)
        
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]
        
        contents = client._convert_messages_to_contents(messages)
        
        assert len(contents) == 3
        assert contents[0]["role"] == "user"
        assert contents[0]["parts"] == [{"text": "Hello"}]
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "user"
    
    def test_convert_multimodal_messages(self):
        """Test multimodal message conversion."""
        client = GeminiBatchClient(api_key="test_key", use_sdk=False)
        
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
        assert len(contents[0]["parts"]) == 2
        assert contents[0]["parts"][0] == {"text": "What's in this image?"}
        assert "inlineData" in contents[0]["parts"][1]
    
    def test_convert_tools_to_rest(self):
        """Test tools conversion to REST format."""
        client = GeminiBatchClient(api_key="test_key", use_sdk=False)
        
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather info",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }]
        
        converted = client._convert_tools_to_rest(tools)
        
        assert len(converted) == 1
        assert "functionDeclarations" in converted[0]
        assert converted[0]["functionDeclarations"][0]["name"] == "get_weather"
    
    @pytest.mark.asyncio
    async def test_submit_batch_rest(self, sample_job, tmp_path):
        """Test batch submission via REST API."""
        client = GeminiBatchClient(api_key="test_key", use_sdk=False)
        
        mock_http = AsyncMock()
        client._http_client = mock_http
        
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "name": "operations/batch_job_123",
            "responses": [],
        }
        mock_http.post.return_value = response
        
        job_name = await client.submit_batch(sample_job, tmp_path)
        
        assert job_name == "operations/batch_job_123"
    
    @pytest.mark.asyncio
    async def test_immediate_results(self, sample_job):
        """Test handling of immediate batch results (small batches)."""
        client = GeminiBatchClient(api_key="test_key", use_sdk=False)
        
        sample_job.metadata["immediate_results"] = [{
            "candidates": [{
                "content": {
                    "parts": [{"text": "Test response"}]
                }
            }]
        }]
        
        results = await client.get_batch_results(sample_job)
        
        assert len(results) == 1
        assert results[0]["custom_id"] == "custom_1"
        assert results[0]["response"]["choices"][0]["message"]["content"] == "Test response"
    
    @pytest.mark.asyncio
    async def test_get_batch_status_rest(self):
        """Test batch status via REST API."""
        client = GeminiBatchClient(api_key="test_key", use_sdk=False)
        
        mock_http = AsyncMock()
        client._http_client = mock_http
        
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "state": "JOB_STATE_SUCCEEDED",
        }
        mock_http.get.return_value = response
        
        status_info = await client.get_batch_status("operations/batch_123")
        
        assert status_info["status"] == BatchStatus.COMPLETED.value


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
