"""
Batch Monitor Plugin - Test Suite

Tests cover:
- Plugin discovery and initialization
- Web endpoint functionality
- Queue status retrieval
- Metrics aggregation
- Error handling (unavailable batch system)
- JSON response formatting

Target: >80% code coverage
"""

import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_config() -> dict:
    """Mock plugin configuration"""
    return {
        "enabled": True,
        "description": "Batch queue monitoring panel"
    }


@pytest.fixture
def mock_system_config():
    """Mock AgentSystemConfig"""
    return MagicMock()


@pytest.fixture
def mock_mcp_config():
    """Mock MCPConfig"""
    config = MagicMock()
    config.enabled = True
    return config


@pytest.fixture
def web_factory(mock_system_config, mock_mcp_config):
    """BatchMonitorWebFactory instance for testing"""
    from plugins.batch_monitor.server import BatchMonitorWebFactory
    return BatchMonitorWebFactory("batch_monitor", mock_system_config, mock_mcp_config)


@pytest.fixture
def mock_request() -> MagicMock:
    """Mock FastAPI Request object"""
    request = MagicMock(spec=Request)
    request.url = MagicMock()
    request.url.path = "/plugins/batch_monitor/"
    return request


@pytest.fixture
def mock_batch_manager() -> MagicMock:
    """Mock BatchQueueManager with two queues
    
    Structure matches actual BatchQueueManager:
    - _queues: Dict[str, List[BatchRequest]] - pending requests by queue_key
    - _active_jobs: Dict[str, BatchJob] - active jobs by job_id
    - _collection_window: float - seconds to collect requests
    - _poll_interval: float - seconds between polling
    - _metrics: BatchMetrics - cumulative metrics
    """
    from agent_system.llm.batch.models import BatchMetrics, BatchRequest
    
    manager = MagicMock()
    manager._collection_window = 60.0
    manager._poll_interval = 30.0
    
    # _queues contains only pending requests (List[BatchRequest])
    # Empty for gemini, 2 pending for openai
    # Use actual BatchRequest objects so estimate_input_tokens() works
    pending_req1 = BatchRequest(
        request_id="pending1",
        custom_id="pending_custom1",
        model="gpt-4o",
        messages=[{"role": "user", "content": "test message 1"}],
    )
    pending_req2 = BatchRequest(
        request_id="pending2",
        custom_id="pending_custom2",
        model="gpt-4o",
        messages=[{"role": "user", "content": "test message 2"}],
    )
    manager._queues = {
        "gemini:gemini-2.5-flash": [],  # Empty list - will be filtered out
        "openai:gpt-4o": [pending_req1, pending_req2],  # 2 pending - will show
    }
    
    # No active jobs initially
    manager._active_jobs = {}
    
    # Add metrics
    manager._metrics = BatchMetrics()
    
    return manager


@pytest.fixture
def mock_active_job():
    """Create a mock active batch job"""
    from agent_system.llm.batch.models import BatchJob, BatchRequest, BatchStatus
    
    request1 = BatchRequest(
        request_id="req1",
        custom_id="custom1",
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "test"}],
    )
    
    job = BatchJob(
        job_id="abf2d477-0a53-4097-8da9-a84ebe5ed317",
        provider="gemini",
        model="gemini-2.5-flash",
        requests=[request1],
    )
    job.status = BatchStatus.IN_PROGRESS
    job.provider_job_id = "5j7hnh6z8j3v4o9d1ugwajwvw3k8rzztum30"
    job.submitted_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    job.completed_count = 12
    job.failed_count = 0
    
    return job


# =============================================================================
# Plugin Discovery Tests
# =============================================================================

@pytest.mark.asyncio
async def test_batch_monitor_plugin_discovered():
    """Test that batch_monitor plugin is discovered by plugin system"""
    from agent_system.plugins import discover_all_plugins
    from plugins.batch_monitor.server import BatchMonitorWebFactory
    
    repo_root = Path(__file__).resolve().parents[4]
    plugins_dir = repo_root / "src" / "plugins"
    
    if not plugins_dir.exists():
        pytest.skip("Plugins directory not found")
    
    plugins = discover_all_plugins([plugins_dir])
    assert "batch_monitor" in plugins, "batch_monitor plugin not discovered"
    
    factory_class = plugins["batch_monitor"]
    mock_sys_config = MagicMock()
    mock_mcp_config = MagicMock()
    instance = factory_class("batch_monitor", mock_sys_config, mock_mcp_config)
    assert instance is not None
    assert isinstance(instance, BatchMonitorWebFactory)


# =============================================================================
# Factory Initialization Tests
# =============================================================================

def test_web_factory_initialization(mock_system_config, mock_mcp_config):
    """Test BatchMonitorWebFactory initializes correctly"""
    from plugins.batch_monitor.server import BatchMonitorWebFactory
    
    factory = BatchMonitorWebFactory("batch_monitor", mock_system_config, mock_mcp_config)
    
    assert factory.name == "batch_monitor"
    assert factory.plugin_dir.name == "batch_monitor"
    assert factory.templates_dir.name == "templates"
    assert factory.templates is not None


def test_web_factory_router_creation(web_factory):
    """Test that router is created successfully"""
    router = web_factory.get_web_router()
    
    assert router is not None
    assert router.prefix == "/plugins/batch_monitor"


# =============================================================================
# Panel Endpoint Tests
# =============================================================================

@pytest.mark.asyncio
async def test_get_panel_returns_html(web_factory, mock_request: MagicMock):
    """Test that get_panel returns HTML response"""
    response = await web_factory.get_panel(mock_request)
    
    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_panel_contains_expected_content(web_factory, mock_request: MagicMock):
    """Test that panel HTML contains expected elements"""
    response = await web_factory.get_panel(mock_request)
    
    html_content = response.body.decode('utf-8')
    assert "Batch Queue Monitor" in html_content
    assert "icon-only" in html_content  # Refresh/auto-refresh buttons
    assert "stats-grid" in html_content  # Stats section


# =============================================================================
# Queues Endpoint Tests
# =============================================================================

@pytest.mark.asyncio
async def test_get_queues_no_manager(web_factory, mock_request: MagicMock):
    """Test get_queues when batch manager is not available"""
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=None):
        response = await web_factory.get_queues(mock_request)
        
        assert isinstance(response, JSONResponse)
        data = response.body.decode('utf-8')
        
        result = json.loads(data)
        
        assert result["status"] == "unavailable"
        assert "not initialized" in result["message"].lower()
        assert result["queues"] == []


@pytest.mark.asyncio
async def test_get_queues_with_idle_queues(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock
):
    """Test get_queues with idle queues (empty queues filtered)"""
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_queues(mock_request)
        
        assert isinstance(response, JSONResponse)
        data = response.body.decode('utf-8')
        
        result = json.loads(data)
        
        assert result["status"] == "success"
        # Only openai queue shows (has pending), gemini filtered (empty idle)
        assert result["queue_count"] == 1
        assert len(result["queues"]) == 1
        assert result["collection_window_seconds"] == 60.0
        assert result["poll_interval_seconds"] == 30.0
        
        # Check queue details - only openai with pending requests
        queue_keys = [q["queue_key"] for q in result["queues"]]
        assert "openai:gpt-4o" in queue_keys
        assert "gemini:gemini-2.5-flash" not in queue_keys  # filtered out


@pytest.mark.asyncio
async def test_get_queues_with_active_job(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock,
    mock_active_job
):
    """Test get_queues with an active job"""
    # Add active job to _active_jobs dict (key is job_id)
    mock_batch_manager._active_jobs[mock_active_job.job_id] = mock_active_job
    
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_queues(mock_request)
        
        result = json.loads(response.body.decode('utf-8'))
        
        # Find the queue with active job
        active_queue = next(q for q in result["queues"] if q["queue_key"] == "gemini:gemini-2.5-flash")
        
        assert active_queue["status"] == "in_progress"
        assert active_queue["active_job"] is not None
        assert active_queue["active_job"]["job_id"] == mock_active_job.job_id
        assert active_queue["active_job"]["status"] == "in_progress"
        assert active_queue["active_job"]["completed_count"] == 12
        assert active_queue["active_job"]["failed_count"] == 0
        assert "elapsed_seconds" in active_queue["active_job"]


@pytest.mark.asyncio
async def test_get_queues_pending_requests(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock
):
    """Test that pending requests are counted correctly"""
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_queues(mock_request)
        
        result = json.loads(response.body.decode('utf-8'))
        
        # Check openai queue has 2 pending requests
        openai_queue = next(q for q in result["queues"] if q["queue_key"] == "openai:gpt-4o")
        assert openai_queue["pending_requests"] == 2


# =============================================================================
# Queue Detail Endpoint Tests
# =============================================================================

@pytest.mark.asyncio
async def test_get_queue_detail_not_found(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock
):
    """Test get_queue_detail with nonexistent queue key"""
    from fastapi import HTTPException
    
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        with pytest.raises(HTTPException) as exc_info:
            await web_factory.get_queue_detail(mock_request, queue_key="nonexistent:model")
        
        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_queue_detail_success(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock,
    mock_active_job
):
    """Test get_queue_detail with existing queue"""
    # Add active job to _active_jobs dict
    mock_batch_manager._active_jobs[mock_active_job.job_id] = mock_active_job
    
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_queue_detail(mock_request, queue_key="gemini:gemini-2.5-flash")
        
        result = json.loads(response.body.decode('utf-8'))
        
        assert result["status"] == "success"
        assert result["queue_key"] == "gemini:gemini-2.5-flash"
        assert result["provider"] == "gemini"
        assert result["model"] == "gemini-2.5-flash"
        assert result["queue_status"] == "in_progress"
        assert result["pending_requests"] == 0
        assert result["active_job"] is not None
        assert result["active_job"]["job_id"] == mock_active_job.job_id


# =============================================================================
# Metrics Endpoint Tests
# =============================================================================

@pytest.mark.asyncio
async def test_get_metrics_no_manager(web_factory, mock_request: MagicMock):
    """Test get_metrics when batch manager is not available"""
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=None):
        response = await web_factory.get_metrics(mock_request)
        
        result = json.loads(response.body.decode('utf-8'))
        
        assert result["status"] == "unavailable"
        assert "not initialized" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_metrics_success(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock,
    mock_active_job
):
    """Test get_metrics with active jobs"""
    # Add active job to _active_jobs dict
    mock_batch_manager._active_jobs[mock_active_job.job_id] = mock_active_job
    
    # Update metrics to reflect completed requests
    mock_batch_manager._metrics.completed_requests = 12
    mock_batch_manager._metrics.failed_requests = 0
    mock_batch_manager._metrics.total_jobs = 1
    mock_batch_manager._metrics.completed_jobs = 0
    
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_metrics(mock_request)
        
        result = json.loads(response.body.decode('utf-8'))
        
        assert result["status"] == "success"
        assert result["total_queues"] == 2  # openai (pending) + gemini (active job)
        assert result["active_jobs"] == 1
        assert result["total_pending_requests"] == 2
        assert result["total_completed_requests"] == 12
        assert result["total_failed_requests"] == 0


# =============================================================================
# Edge Case Tests
# =============================================================================

@pytest.mark.asyncio
async def test_elapsed_time_calculation(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock,
    mock_active_job
):
    """Test that elapsed time is calculated correctly"""
    # Add active job to _active_jobs dict
    mock_batch_manager._active_jobs[mock_active_job.job_id] = mock_active_job
    
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_queues(mock_request)
        
        result = json.loads(response.body.decode('utf-8'))
        
        active_queue = next(q for q in result["queues"] if q["queue_key"] == "gemini:gemini-2.5-flash")
        assert active_queue["active_job"]["elapsed_seconds"] >= 120  # ~2 minutes


@pytest.mark.asyncio
async def test_active_job_without_submitted_at(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock,
    mock_active_job
):
    """Test that jobs without submitted_at timestamp still work"""
    mock_active_job.submitted_at = None
    # Add active job to _active_jobs dict
    mock_batch_manager._active_jobs[mock_active_job.job_id] = mock_active_job
    
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response = await web_factory.get_queues(mock_request)
        
        result = json.loads(response.body.decode('utf-8'))
        
        active_queue = next(q for q in result["queues"] if q["queue_key"] == "gemini:gemini-2.5-flash")
        assert active_queue["active_job"]["elapsed_seconds"] is None


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.asyncio
async def test_integration_idle_to_active_transition(
    web_factory,
    mock_request: MagicMock,
    mock_batch_manager: MagicMock,
    mock_active_job
):
    """Test queue appears when job becomes active"""
    # First request - gemini queue is empty/idle so not shown
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response1 = await web_factory.get_queues(mock_request)
        result1 = json.loads(response1.body.decode('utf-8'))
        
        # Only openai queue shows (has pending requests)
        assert len(result1["queues"]) == 1
        assert result1["queues"][0]["queue_key"] == "openai:gpt-4o"
    
    # Add active job to _active_jobs dict
    mock_batch_manager._active_jobs[mock_active_job.job_id] = mock_active_job
    
    # Second request - gemini queue now shows because it has an active job
    with patch('agent_system.llm.batch.initialization.get_batch_queue_manager', return_value=mock_batch_manager):
        response2 = await web_factory.get_queues(mock_request)
        result2 = json.loads(response2.body.decode('utf-8'))
        
        # Now both queues show
        assert len(result2["queues"]) == 2
        gemini_queue = next(q for q in result2["queues"] if q["queue_key"] == "gemini:gemini-2.5-flash")
        assert gemini_queue["status"] == "in_progress"
        assert gemini_queue["active_job"] is not None
