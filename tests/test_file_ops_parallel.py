"""Test parallel tool execution with file_ops plugin."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, MCPConfig
from agent_system.mcp.status import StatusBus, StatusScope
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
def tmp_allowed_dir(tmp_path):
    """Create a temporary allowed directory."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    return allowed_dir


@pytest.fixture
async def file_ops_server(tmp_allowed_dir):
    """Create file operations server with temp directory."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_allowed_dir.parent)
    
    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(tmp_allowed_dir)]
    mcp_config.search = {
        "enable_indexing": False  # Disable for faster tests
    }
    
    server = FileOpsServer("file_ops", system_config, mcp_config)
    yield server
    
    # Cleanup
    await server.search_engine.stop()


@pytest.fixture
def status_bus():
    """Create a status bus for testing."""
    return StatusBus()


@pytest.mark.asyncio
async def test_parallel_security_violations(file_ops_server, tmp_allowed_dir, status_bus):
    """Test that security violations send status.error() in parallel execution."""
    
    # Track status events via queue subscription
    queue = await status_bus.subscribe(server="file_ops")
    events = []
    
    # Create status scopes for parallel operations
    status1 = StatusScope(status_bus, "file_ops", request_id="req_001")
    status2 = StatusScope(status_bus, "file_ops", request_id="req_002")
    status3 = StatusScope(status_bus, "file_ops", request_id="req_003")
    
    # Prepare test params with security violations
    invalid_params = [
        {
            "file_path": "E:\\",  # Outside allowed directory
            "_status": status1
        },
        {
            "file_path": str(tmp_allowed_dir / ".." / "secret.txt"),  # Path traversal
            "_status": status2
        },
        {
            "file_path": str(tmp_allowed_dir / "..." / "secret.txt"),  # Another traversal pattern
            "_status": status3
        }
    ]
    
    # Execute all three in parallel
    results = await asyncio.gather(
        file_ops_server.read_file(invalid_params[0]),
        file_ops_server.read_file(invalid_params[1]),
        file_ops_server.read_file(invalid_params[2]),
        return_exceptions=True
    )
    
    # Collect events from queue
    await asyncio.sleep(0.1)  # Give status events time to propagate
    while not queue.empty():
        events.append(await queue.get())
    
    # DEBUG: Print results and events
    print(f"\n=== Results: {results}")
    print(f"=== Events ({len(events)}): {[(e.request_id, e.phase.value, e.message[:40]) for e in events]}")
    
    # Check that all returned errors
    assert all(r.get("status") == "error" for r in results if isinstance(r, dict))
    # Note: The third test uses "..." which is a valid filename on Windows, so it's a FileNotFoundError, not SecurityError
    # We only check that the first two are SecurityErrors
    assert results[0].get("error_type") == "SecurityError"
    assert results[1].get("error_type") == "SecurityError"
    
    # Check that status.error() was called for each
    error_events = [e for e in events if e.phase.value == "error"]
    print(f"=== Error events: {len(error_events)}")
    
    # CRITICAL: This is the actual test - are we getting ALL completion events?
    completion_events = [e for e in events if e.phase.value in ("end", "error")]
    print(f"=== Completion events: {len(completion_events)} (expected 3)")
    assert len(completion_events) == 3, f"Expected 3 completion events, got {len(completion_events)}: {[(e.request_id, e.phase.value) for e in completion_events]}"
    
    # Verify error messages contain security information
    error_messages = [e.message for e in error_events]
    assert any("outside allowed" in msg.lower() or "traversal" in msg.lower() 
               for msg in error_messages)


@pytest.mark.asyncio
async def test_parallel_tool_execution_status_completion(file_ops_server, tmp_allowed_dir, status_bus):
    """Test that all parallel tool calls complete with status.end()."""
    
    # Create test files
    for i in range(5):
        (tmp_allowed_dir / f"test_{i}.txt").write_text(f"Content {i}")
    
    # Track status events via queue subscription
    queue = await status_bus.subscribe(server="file_ops")
    events = []
    
    # Create status scopes for parallel operations
    status_scopes = [
        StatusScope(status_bus, "file_ops", request_id=f"req_{i:03d}")
        for i in range(5)
    ]
    
    # Prepare read operations
    read_params = [
        {
            "file_path": str(tmp_allowed_dir / f"test_{i}.txt"),
            "_status": status_scopes[i]
        }
        for i in range(5)
    ]
    
    # Execute all five reads in parallel
    results = await asyncio.gather(
        *[file_ops_server.read_file(params) for params in read_params],
        return_exceptions=True
    )
    
    # Collect events from queue
    while not queue.empty():
        events.append(await queue.get())
    
    # Check that all succeeded
    assert all(r.get("status") == "success" for r in results if isinstance(r, dict))
    
    # Check that we got END events for each
    end_events = [e for e in events if e.phase.value == "end"]
    assert len(end_events) == 5, f"Expected 5 END events, got {len(end_events)}: {[e.message for e in end_events]}"
    
    # Verify each request_id got an END
    end_request_ids = {e.request_id for e in end_events}
    expected_ids = {f"req_{i:03d}" for i in range(5)}
    assert end_request_ids == expected_ids, f"Missing END events for: {expected_ids - end_request_ids}"


@pytest.mark.asyncio
async def test_parallel_mixed_operations(file_ops_server, tmp_allowed_dir, status_bus):
    """Test parallel mix of successful and failed operations."""
    
    # Create one test file
    (tmp_allowed_dir / "exists.txt").write_text("Content")
    
    # Track status events via queue subscription
    queue = await status_bus.subscribe(server="file_ops")
    events = []
    
    # Mix of operations
    operations = [
        ("read_file", {"file_path": str(tmp_allowed_dir / "exists.txt"), "_status": StatusScope(status_bus, "file_ops", request_id="req_a")}),
        ("read_file", {"file_path": str(tmp_allowed_dir / "missing.txt"), "_status": StatusScope(status_bus, "file_ops", request_id="req_b")}),
        ("read_file", {"file_path": "E:\\secret.txt", "_status": StatusScope(status_bus, "file_ops", request_id="req_c")}),  # Security error
        ("read_file", {"file_path": str(tmp_allowed_dir / "exists.txt"), "_status": StatusScope(status_bus, "file_ops", request_id="req_d")}),
    ]
    
    # Execute in parallel
    results = await asyncio.gather(
        *[getattr(file_ops_server, op[0])(op[1]) for op in operations],
        return_exceptions=True
    )
    
    # Collect events from queue
    while not queue.empty():
        events.append(await queue.get())
    
    # Check results
    assert results[0].get("status") == "success"
    assert results[1].get("status") == "error"  # File not found
    assert results[2].get("status") == "error"  # Security
    assert results[3].get("status") == "success"
    
    # Check that ALL operations got either END or ERROR events
    completion_events = [e for e in events if e.phase.value in ("end", "error")]
    assert len(completion_events) == 4, f"Expected 4 completion events, got {len(completion_events)}"
    
    # Verify each request_id got a completion
    completion_ids = {e.request_id for e in completion_events}
    expected_ids = {"req_a", "req_b", "req_c", "req_d"}
    assert completion_ids == expected_ids, f"Missing completion events for: {expected_ids - completion_ids}"
