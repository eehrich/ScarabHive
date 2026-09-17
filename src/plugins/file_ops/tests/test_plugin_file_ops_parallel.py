"""Test parallel tool execution with file_ops plugin."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, ToolServerConfig
from agent_system.tools.status import StatusBus
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

    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tmp_allowed_dir)]
    server_config.search = {
        "enable_indexing": False  # Disable for faster tests
    }

    server = FileOpsServer("file_ops", system_config, server_config)
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

    # Helper to call with status scope (like call_with_status does)
    async def call_with_scope(request_id: str, params: dict):
        from agent_system.tools.status import status_scope
        async with status_scope(status_bus, "file_ops", request_id=request_id) as status:
            params_with_status = params.copy()
            params_with_status["_status"] = status
            return await file_ops_server.read_file(params_with_status)

    # Prepare test params with security violations
    test_cases = [
        ("req_001", {"filePath": str(tmp_allowed_dir / ".." / "outside.txt")}),  # Path traversal
        ("req_002", {"filePath": str(tmp_allowed_dir / ".." / "secret.txt")}),  # Path traversal
        ("req_003", {"filePath": str(tmp_allowed_dir / "..." / "secret.txt")}),  # Another traversal pattern
    ]

    # Execute all three in parallel using context managers
    results = await asyncio.gather(
        call_with_scope(test_cases[0][0], test_cases[0][1]),
        call_with_scope(test_cases[1][0], test_cases[1][1]),
        call_with_scope(test_cases[2][0], test_cases[2][1]),
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
    # Path traversal patterns should trigger SecurityError
    assert results[0].get("error_type") == "SecurityError"
    assert results[1].get("error_type") == "SecurityError"
    # The third test uses "..." which might be FileNotFoundError or SecurityError depending on OS

    # Check completion events - with context manager we should get START + (END or ERROR) for each
    start_events = [e for e in events if e.phase.value == "start"]
    completion_events = [e for e in events if e.phase.value in ("end", "error")]

    print(f"=== Start events: {len(start_events)}")
    print(f"=== Completion events: {len(completion_events)}")

    # With context manager: Should have START for each operation
    assert len(start_events) == 3, f"Expected 3 START events, got {len(start_events)}"

    # Should have ERROR or END for each operation
    assert len(completion_events) == 3, f"Expected 3 completion events, got {len(completion_events)}: {[(e.request_id, e.phase.value) for e in completion_events]}"


@pytest.mark.asyncio
async def test_parallel_tool_execution_status_completion(file_ops_server, tmp_allowed_dir, status_bus):
    """Test that all parallel tool calls complete with status.end()."""

    # Create test files
    for i in range(5):
        (tmp_allowed_dir / f"test_{i}.txt").write_text(f"Content {i}")

    # Track status events via queue subscription
    queue = await status_bus.subscribe(server="file_ops")
    events = []

    # Helper to call with status scope (like call_with_status does)
    async def call_with_scope(request_id: str, file_path: str):
        from agent_system.tools.status import status_scope
        async with status_scope(status_bus, "file_ops", request_id=request_id) as status:
            params = {
                "filePath": file_path,
                "_status": status
            }
            return await file_ops_server.read_file(params)

    # Prepare read operations
    test_cases = [
        (f"req_{i:03d}", str(tmp_allowed_dir / f"test_{i}.txt"))
        for i in range(5)
    ]

    # Execute all five reads in parallel using context managers
    results = await asyncio.gather(
        *[call_with_scope(req_id, file_path) for req_id, file_path in test_cases],
        return_exceptions=True
    )

    # Collect events from queue
    await asyncio.sleep(0.1)
    while not queue.empty():
        events.append(await queue.get())

    # Check that all succeeded
    assert all(r.get("status") == "success" for r in results if isinstance(r, dict))

    # With context manager: Should have START for each
    start_events = [e for e in events if e.phase.value == "start"]
    assert len(start_events) == 5, f"Expected 5 START events, got {len(start_events)}"

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

    # Helper to call with status scope (like call_with_status does)
    async def call_with_scope(request_id: str, file_path: str):
        from agent_system.tools.status import status_scope
        async with status_scope(status_bus, "file_ops", request_id=request_id) as status:
            params = {
                "filePath": file_path,
                "_status": status
            }
            return await file_ops_server.read_file(params)

    # Mix of operations
    test_cases = [
        ("req_a", str(tmp_allowed_dir / "exists.txt")),  # Success
        ("req_b", str(tmp_allowed_dir / "missing.txt")),  # FileNotFoundError
        ("req_c", str(tmp_allowed_dir / ".." / "secret.txt")),  # SecurityError (path traversal)
        ("req_d", str(tmp_allowed_dir / "exists.txt")),  # Success
    ]

    # Execute in parallel using context managers
    results = await asyncio.gather(
        *[call_with_scope(req_id, file_path) for req_id, file_path in test_cases],
        return_exceptions=True
    )

    # Collect events from queue
    await asyncio.sleep(0.1)
    while not queue.empty():
        events.append(await queue.get())

    # Check results
    assert results[0].get("status") == "success"
    assert results[1].get("status") == "error"  # File not found
    assert results[2].get("status") == "error"  # Security
    assert results[3].get("status") == "success"

    # With context manager: Should have START for each
    start_events = [e for e in events if e.phase.value == "start"]
    assert len(start_events) == 4, f"Expected 4 START events, got {len(start_events)}"

    # Check that ALL operations got either END or ERROR events
    completion_events = [e for e in events if e.phase.value in ("end", "error")]
    assert len(completion_events) == 4, f"Expected 4 completion events, got {len(completion_events)}"

    # Verify each request_id got a completion
    completion_ids = {e.request_id for e in completion_events}
    expected_ids = {"req_a", "req_b", "req_c", "req_d"}
    assert completion_ids == expected_ids, f"Missing completion events for: {expected_ids - completion_ids}"
