"""Tests for file_ops status message handling.

These tests verify that status.end() and status.error() are called correctly
for file_ops operations with the current API (replace_string_in_file).
"""

import gc
import pytest
from unittest.mock import AsyncMock, MagicMock, Mock
from agent_system.config import AgentSystemConfig, MCPConfig
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
def tmp_allowed_dir(tmp_path):
    """Create a temporary allowed directory."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    return allowed_dir


@pytest.fixture
async def server(tmp_allowed_dir):
    """Create file_ops server instance."""
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
    gc.collect()


@pytest.fixture
def mock_status():
    """Create mock status object."""
    status = MagicMock()
    status.start = AsyncMock()
    status.progress = AsyncMock()
    status.end = AsyncMock()
    status.error = AsyncMock()
    return status


@pytest.mark.asyncio
async def test_replace_string_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that replace_string_in_file calls status.end() on success."""
    # Create test file
    test_file = tmp_allowed_dir / "test_replace_status.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")

    # Call replace_string_in_file
    result = await server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Line 2\nLine 3",
        "newString": "Modified Line 2\nModified Line 3",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"
    assert result["changes"]["replacements"] == 1

    # Verify status.end() was called
    mock_status.end.assert_called_once()
    call_args = mock_status.end.call_args[0][0]
    assert "1 replacement" in call_args or "Replaced" in call_args


@pytest.mark.asyncio
async def test_replace_string_error_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that replace_string_in_file calls status.error() when string not found."""
    # Create test file
    test_file = tmp_allowed_dir / "test_replace_error.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Call replace_string_in_file with non-existent string
    result = await server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Non-existent string that will not be found",
        "newString": "Modified",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"

    # Verify status.error() was called
    mock_status.error.assert_called_once()
    call_args = mock_status.error.call_args[0][0]
    assert "not found" in call_args.lower() or "error" in call_args.lower()


@pytest.mark.asyncio
async def test_read_file_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that read_file calls status.end()."""
    # Create test file
    test_file = tmp_allowed_dir / "test_read_status.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Call read_file
    result = await server.read_file({
        "filePath": str(test_file),
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"
    assert "Line 1" in result["content"]

    # Verify status.end() was called
    mock_status.end.assert_called_once()


@pytest.mark.asyncio
async def test_create_file_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that create_file calls status.end()."""
    test_file = tmp_allowed_dir / "new_file.txt"

    # Call create_file
    result = await server.create_file({
        "filePath": str(test_file),
        "content": "New file content",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"

    # Verify status.end() was called
    mock_status.end.assert_called_once()
    call_args = mock_status.end.call_args[0][0]
    assert "created" in call_args.lower() or "success" in call_args.lower()


@pytest.mark.asyncio
async def test_list_directory_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that list_directory calls status.end()."""
    # Create some test files
    (tmp_allowed_dir / "file1.txt").write_text("test")
    (tmp_allowed_dir / "file2.txt").write_text("test")

    # Call list_directory
    result = await server.list_directory({
        "dir_path": str(tmp_allowed_dir),
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"
    assert len(result["files"]) + len(result["directories"]) >= 2

    # Verify status.end() was called
    mock_status.end.assert_called_once()


@pytest.mark.asyncio
async def test_file_not_found_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that operations call status.error() when file not found."""
    non_existent = tmp_allowed_dir / "does_not_exist.txt"

    # Call read_file on non-existent file
    result = await server.read_file({
        "filePath": str(non_existent),
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"

    # Verify status.error() was called
    mock_status.error.assert_called_once()
    call_args = mock_status.error.call_args[0][0]
    assert "not found" in call_args.lower() or "error" in call_args.lower()


@pytest.mark.asyncio
async def test_permission_denied_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that operations call status.error() when accessing disallowed path."""
    # Try to access file outside allowed directories
    forbidden_file = "/etc/passwd"  # Unix example - will fail on sandbox check

    # Call read_file on forbidden file
    result = await server.read_file({
        "filePath": forbidden_file,
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"

    # Verify status.error() was called
    mock_status.error.assert_called_once()
    call_args = mock_status.error.call_args[0][0]
    # Can be either "not allowed" for permission or "not found" if sandbox rejects it
    assert any(keyword in call_args.lower() for keyword in ["not allowed", "permission", "not found", "error"])


