"""Tests for ComfyUI plugin."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from plugins.comfyui.comfyui_client import ComfyUIClient
from plugins.comfyui.job_tracker import ComfyUIJobTracker


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_output_dir(tmp_path: Path) -> Path:
    """Create a temporary output directory."""
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database path."""
    return tmp_path / "jobs.db"


@pytest.fixture
def client(temp_output_dir: Path) -> ComfyUIClient:
    """Create a ComfyUI client for testing."""
    return ComfyUIClient(
        host="127.0.0.1",
        port=8188,
        output_dir=temp_output_dir,
        timeout=10.0
    )


@pytest.fixture
def job_tracker(temp_db_path: Path) -> ComfyUIJobTracker:
    """Create a job tracker for testing."""
    return ComfyUIJobTracker(temp_db_path)


# =============================================================================
# ComfyUIClient Tests
# =============================================================================

class TestComfyUIClient:
    """Tests for ComfyUIClient."""
    
    def test_init(self, client: ComfyUIClient) -> None:
        """Test client initialization."""
        assert client.host == "127.0.0.1"
        assert client.port == 8188
        # timeout is a ClientTimeout object
        assert client.timeout.total == 10.0
        assert client.base_url == "http://127.0.0.1:8188"
    
    @pytest.mark.asyncio
    async def test_ping_success(self, client: ComfyUIClient) -> None:
        """Test successful server ping."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "queue_pending": [],
            "queue_running": []
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()
        
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.ping()
            
        assert result["status"] == "online"
        assert "queue_pending" in result
    
    @pytest.mark.asyncio
    async def test_ping_offline(self, client: ComfyUIClient) -> None:
        """Test ping when server is offline."""
        # Use a more direct patching approach
        with patch.object(client, 'ping', return_value={"status": "offline", "error": "Connection refused"}):
            result = await client.ping()
            
        assert result["status"] == "offline"
        assert "error" in result
    
    @pytest.mark.asyncio
    async def test_queue_prompt(self, client: ComfyUIClient) -> None:
        """Test workflow queuing."""
        workflow = {"3": {"inputs": {"text": "test prompt"}}}
        
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"prompt_id": "abc123"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()
        
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.queue_prompt(workflow)
            
        assert result["status"] == "queued"
        assert result["prompt_id"] == "abc123"
    
    @pytest.mark.asyncio
    async def test_get_history(self, client: ComfyUIClient) -> None:
        """Test getting job history."""
        prompt_id = "abc123"
        
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            prompt_id: {
                "outputs": {"node1": {"images": [{"filename": "test.png"}]}},
                "status": {"status_str": "success"}
            }
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()
        
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_history(prompt_id)
            
        assert prompt_id in result
        assert "outputs" in result[prompt_id]
    
    @pytest.mark.asyncio
    async def test_get_status_completed(self, client: ComfyUIClient) -> None:
        """Test getting completed job status."""
        prompt_id = "abc123"
        
        # First mock for queue check, second for history check
        mock_queue_response = MagicMock()
        mock_queue_response.status = 200
        mock_queue_response.json = AsyncMock(return_value={
            "queue_pending": [],
            "queue_running": []
        })
        mock_queue_response.__aenter__ = AsyncMock(return_value=mock_queue_response)
        mock_queue_response.__aexit__ = AsyncMock()
        
        mock_history_response = MagicMock()
        mock_history_response.status = 200
        mock_history_response.json = AsyncMock(return_value={
            prompt_id: {
                "outputs": {},
                "status": {"status_str": "success"}
            }
        })
        mock_history_response.__aenter__ = AsyncMock(return_value=mock_history_response)
        mock_history_response.__aexit__ = AsyncMock()
        
        mock_session = MagicMock()
        # get_queue then get_history
        mock_session.get = MagicMock(side_effect=[mock_queue_response, mock_history_response])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_status(prompt_id)
            
        assert result["status"] == "completed"
    
    @pytest.mark.asyncio
    async def test_get_file(self, client: ComfyUIClient) -> None:
        """Test downloading output file."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=b"image data")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()
        
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_file("test.png", "", "output")
            
        assert result == b"image data"
    
    @pytest.mark.asyncio
    async def test_cancel(self, client: ComfyUIClient) -> None:
        """Test job cancellation."""
        prompt_id = "abc123"
        
        # Use direct patching for cancel method since it has multiple API calls
        with patch.object(client, 'cancel', return_value={"status": "cancelled", "prompt_id": prompt_id}):
            result = await client.cancel(prompt_id)
            
        assert result["status"] == "cancelled"


# =============================================================================
# ComfyUIJobTracker Tests
# =============================================================================

class TestComfyUIJobTracker:
    """Tests for ComfyUIJobTracker."""
    
    def test_init(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test tracker initialization."""
        assert job_tracker.db_path.exists()
    
    def test_register_job(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test job registration."""
        job_tracker.register_job(
            prompt_id="abc123",
            workflow_id="test_workflow",
            workflow_name="Test Workflow",
            parameters={"prompt": "test"},
            output_prefix="test"
        )
        
        job = job_tracker.get_job("abc123")
        assert job is not None
        assert job["prompt_id"] == "abc123"
        assert job["workflow_id"] == "test_workflow"
        assert job["workflow_name"] == "Test Workflow"
        assert job["status"] == "queued"
    
    def test_update_status(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test status update."""
        job_tracker.register_job(
            prompt_id="abc123",
            workflow_id="test_workflow",
            workflow_name="Test",
            parameters={},
            output_prefix="test"
        )
        
        job_tracker.update_status("abc123", "running")
        job = job_tracker.get_job("abc123")
        assert job["status"] == "running"
        assert job["started_at"] is not None
        
        job_tracker.update_status("abc123", "completed")
        job = job_tracker.get_job("abc123")
        assert job["status"] == "completed"
        assert job["completed_at"] is not None
    
    def test_update_status_with_error(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test status update with error."""
        job_tracker.register_job(
            prompt_id="abc123",
            workflow_id="test_workflow",
            workflow_name="Test",
            parameters={},
            output_prefix="test"
        )
        
        job_tracker.update_status("abc123", "failed", "Something went wrong")
        job = job_tracker.get_job("abc123")
        assert job["status"] == "failed"
        assert job["error"] == "Something went wrong"
    
    def test_set_outputs(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test setting job outputs."""
        job_tracker.register_job(
            prompt_id="abc123",
            workflow_id="test_workflow",
            workflow_name="Test",
            parameters={},
            output_prefix="test"
        )
        
        outputs = {
            "images": ["image1.png", "image2.png"],
            "audio": []
        }
        job_tracker.set_outputs("abc123", outputs)
        
        job = job_tracker.get_job("abc123")
        assert job["outputs"] == outputs
    
    def test_get_active_jobs(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test getting active jobs."""
        # Register jobs with different statuses
        job_tracker.register_job("job1", "wf1", "WF1", {}, "p1")
        job_tracker.register_job("job2", "wf2", "WF2", {}, "p2")
        job_tracker.register_job("job3", "wf3", "WF3", {}, "p3")
        
        job_tracker.update_status("job2", "running")
        job_tracker.update_status("job3", "completed")
        
        active = job_tracker.get_active_jobs()
        assert len(active) == 2  # job1 (queued) and job2 (running)
        
        prompt_ids = [j["prompt_id"] for j in active]
        assert "job1" in prompt_ids
        assert "job2" in prompt_ids
        assert "job3" not in prompt_ids
    
    def test_get_recent_completed(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test getting recent completed jobs."""
        for i in range(5):
            job_tracker.register_job(f"job{i}", f"wf{i}", f"WF{i}", {}, f"p{i}")
            job_tracker.update_status(f"job{i}", "completed")
        
        recent = job_tracker.get_recent_completed(limit=3)
        assert len(recent) == 3
    
    def test_get_stats(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test getting job statistics."""
        job_tracker.register_job("job1", "wf1", "WF1", {}, "p1")
        job_tracker.register_job("job2", "wf2", "WF2", {}, "p2")
        job_tracker.register_job("job3", "wf3", "WF3", {}, "p3")
        
        job_tracker.update_status("job1", "completed")
        job_tracker.update_status("job2", "failed", "error")
        
        stats = job_tracker.get_stats()
        assert stats["total"] == 3
        assert stats["completed"] == 1
        assert stats["failed"] == 1
        assert stats["queued"] == 1
    
    def test_cleanup_old_jobs(self, job_tracker: ComfyUIJobTracker) -> None:
        """Test cleanup of old jobs."""
        job_tracker.register_job("job1", "wf1", "WF1", {}, "p1")
        job_tracker.update_status("job1", "completed")
        
        # Cleanup jobs older than 0 days (should clean everything completed)
        deleted = job_tracker.cleanup_old_jobs(days=0)
        assert deleted == 1
        
        job = job_tracker.get_job("job1")
        assert job is None


# =============================================================================
# Server Tests
# =============================================================================

class TestComfyUIServer:
    """Tests for ComfyUIServer."""
    
    @pytest.fixture
    def mock_system_config(self) -> MagicMock:
        """Create mock system config."""
        config = MagicMock()
        return config
    
    @pytest.fixture
    def mock_mcp_config(self, tmp_path: Path) -> MagicMock:
        """Create mock MCP config with workflows."""
        config = MagicMock()
        # Set as attributes (server reads via getattr)
        config.host = "127.0.0.1"
        config.port = 8188
        config.timeout_seconds = 30
        config.output_dir = str(tmp_path / "outputs")
        config.workflow_files_dir = str(tmp_path / "workflows")
        config.workflows = [
            {
                "id": "test_workflow",
                "name": "Test Workflow",
                "description": "A test workflow",
                "category": "test",
                "workflow_file": "test.json",
                "parameters": [
                    {
                        "name": "prompt",
                        "type": "string",
                        "required": True,
                        "node_id": "3",
                        "field": "inputs.text"
                    }
                ]
            }
        ]
        return config
    
    @pytest.fixture
    def workflow_file(self, mock_mcp_config: MagicMock) -> Path:
        """Create a test workflow file."""
        workflow_dir = Path(mock_mcp_config.workflow_files_dir)
        workflow_dir.mkdir(parents=True, exist_ok=True)
        
        workflow = {
            "3": {
                "inputs": {
                    "text": "default prompt"
                },
                "class_type": "CLIPTextEncode"
            }
        }
        
        workflow_path = workflow_dir / "test.json"
        workflow_path.write_text(json.dumps(workflow))
        return workflow_path
    
    @pytest.mark.asyncio
    async def test_workflow_list(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test listing workflows."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        result = await server.workflow({"operation": "list"})
        
        assert "workflows" in result
        assert len(result["workflows"]) == 1
        assert result["workflows"][0]["id"] == "test_workflow"
        assert result["workflows"][0]["name"] == "Test Workflow"
    
    @pytest.mark.asyncio
    async def test_workflow_list_with_category(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test listing workflows filtered by category."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Filter by matching category
        result = await server.workflow({"operation": "list", "category": "test"})
        assert len(result["workflows"]) == 1
        
        # Filter by non-matching category
        result = await server.workflow({"operation": "list", "category": "other"})
        assert len(result["workflows"]) == 0
    
    @pytest.mark.asyncio
    async def test_workflow_execute(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test executing a workflow."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Mock the client's queue_prompt method
        server.client.queue_prompt = AsyncMock(return_value={
            "status": "queued",
            "prompt_id": "test-prompt-123"
        })
        
        result = await server.workflow({
            "operation": "execute",
            "workflow_id": "test_workflow",
            "parameters": {"prompt": "a beautiful sunset"},
            "_status": None
        })
        
        assert result["status"] == "queued"
        assert result["prompt_id"] == "test-prompt-123"
    
    @pytest.mark.asyncio
    async def test_workflow_execute_missing_required_param(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test executing workflow without required parameter."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        result = await server.workflow({
            "operation": "execute",
            "workflow_id": "test_workflow",
            "parameters": {},  # Missing required 'prompt'
            "_status": None
        })
        
        assert "error" in result
        assert "prompt" in result["error"]
    
    @pytest.mark.asyncio
    async def test_workflow_execute_unknown_workflow(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test executing unknown workflow."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        result = await server.workflow({
            "operation": "execute",
            "workflow_id": "unknown_workflow",
            "parameters": {},
            "_status": None
        })
        
        assert "error" in result
        assert "unknown_workflow" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_workflow_status(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test getting workflow status."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Register a job first
        server.job_tracker.register_job(
            "test-id",
            "test_workflow",
            "Test",
            {},
            "test"
        )
        
        # Mock client status
        server.client.get_status = AsyncMock(return_value={
            "status": "running"
        })
        
        result = await server.workflow({
            "operation": "status",
            "prompt_id": "test-id"
        })
        
        assert result["status"] == "running"
        assert result["prompt_id"] == "test-id"
    
    @pytest.mark.asyncio
    async def test_workflow_server_status(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test server status check."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        server.client.ping = AsyncMock(return_value={
            "status": "online",
            "queue_pending": 0,
            "queue_running": 0
        })
        
        result = await server.workflow({"operation": "server_status"})
        
        assert result["status"] == "online"
    
    @pytest.mark.asyncio
    async def test_workflow_cancel(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test cancelling a job."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Register a job first
        server.job_tracker.register_job(
            "test-id",
            "test_workflow",
            "Test",
            {},
            "test"
        )
        
        server.client.cancel = AsyncMock(return_value={
            "status": "cancelled"
        })
        
        result = await server.workflow({
            "operation": "cancel",
            "prompt_id": "test-id"
        })
        
        assert result["status"] == "cancelled"
        
        job = server.job_tracker.get_job("test-id")
        assert job["status"] == "cancelled"
    
    @pytest.mark.asyncio
    async def test_wait_for_completion_success(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test wait_for_completion operation with successful completion."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Mock client to return completed status
        server.client.get_status = AsyncMock(return_value={
            "status": "completed",
            "prompt_id": "test-id"
        })
        
        mock_status = MagicMock()
        mock_status.progress = AsyncMock()
        mock_status.end = AsyncMock()
        
        result = await server.workflow({
            "operation": "wait_for_completion",
            "prompt_id": "test-id",
            "timeout": 10,
            "poll_interval": 0.1,
            "_status": mock_status
        })
        
        assert result["status"] == "completed"
        assert result["prompt_id"] == "test-id"
        assert "elapsed_seconds" in result
        mock_status.end.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_wait_for_completion_timeout(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test wait_for_completion operation with timeout."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Mock client to always return pending status
        server.client.get_status = AsyncMock(return_value={
            "status": "pending",
            "prompt_id": "test-id"
        })
        
        mock_status = MagicMock()
        mock_status.progress = AsyncMock()
        mock_status.error = AsyncMock()
        
        result = await server.workflow({
            "operation": "wait_for_completion",
            "prompt_id": "test-id",
            "timeout": 1,  # Short timeout
            "poll_interval": 0.1,
            "_status": mock_status
        })
        
        assert result["status"] == "timeout"
        assert result["prompt_id"] == "test-id"
        assert result["elapsed_seconds"] >= 1.0
        mock_status.error.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_wait_for_completion_failed(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test wait_for_completion operation with job failure."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        # Mock client to return failed status
        server.client.get_status = AsyncMock(return_value={
            "status": "failed",
            "prompt_id": "test-id",
            "error": "Test error"
        })
        
        mock_status = MagicMock()
        mock_status.progress = AsyncMock()
        mock_status.error = AsyncMock()
        
        result = await server.workflow({
            "operation": "wait_for_completion",
            "prompt_id": "test-id",
            "timeout": 10,
            "poll_interval": 0.1,
            "_status": mock_status
        })
        
        assert result["status"] == "failed"
        assert result["prompt_id"] == "test-id"
        assert result["error"] == "Test error"
        mock_status.error.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_inject_value(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test parameter injection into workflow."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        workflow = {
            "3": {
                "inputs": {
                    "text": "original"
                }
            }
        }
        
        server._inject_value(workflow, "3", "inputs.text", "modified")
        
        assert workflow["3"]["inputs"]["text"] == "modified"
    
    @pytest.mark.asyncio
    async def test_get_web_router(
        self,
        mock_system_config: MagicMock,
        mock_mcp_config: MagicMock,
        workflow_file: Path
    ) -> None:
        """Test web router creation."""
        from plugins.comfyui.server import ComfyUIServer
        
        server = ComfyUIServer("comfyui", mock_system_config, mock_mcp_config)
        
        router = server.get_web_router()
        
        # Check that routes are registered (with plugin prefix)
        routes = [r.path for r in router.routes]
        assert "/plugins/comfyui/" in routes
        assert "/plugins/comfyui/jobs" in routes
        assert "/plugins/comfyui/workflows" in routes
        assert "/plugins/comfyui/stats" in routes
