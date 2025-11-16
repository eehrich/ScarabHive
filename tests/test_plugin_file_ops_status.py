"""Tests for file_ops status message handling.

These tests verify that status.end() and status.error() are called correctly
for all file_ops operations, especially replace_lines mode.
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
async def test_replace_lines_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that replace_lines mode calls status.end()."""
    # Create test file
    test_file = tmp_allowed_dir / "test_replace_lines_status.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")

    # Call replace_lines
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "replace_lines",
        "start_line": 2,
        "end_line": 3,
        "content": "New Line 2-3",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"
    assert result["changes"]["lines_replaced"] == 2

    # Verify status.end() was called
    mock_status.end.assert_called_once()
    call_args = mock_status.end.call_args[0][0]
    assert "replaced 2 lines" in call_args
    assert "(2-3)" in call_args


@pytest.mark.asyncio
async def test_replace_mode_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that replace mode calls status.end()."""
    # Create test file
    test_file = tmp_allowed_dir / "test_replace_status.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Call replace
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "replace",
        "old_string": "Line 2",
        "new_string": "Modified Line 2",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"
    assert result["changes"]["replacements"] == 1

    # Verify status.end() was called
    mock_status.end.assert_called_once()
    call_args = mock_status.end.call_args[0][0]
    assert "1 replacements" in call_args


@pytest.mark.asyncio
async def test_replace_mode_error_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that replace mode calls status.error() on string not found."""
    # Create test file
    test_file = tmp_allowed_dir / "test_replace_error.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Call replace with non-existent string
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "replace",
        "old_string": "Non-existent string",
        "new_string": "Modified",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"
    assert result["error_type"] == "StringNotFoundError"

    # Verify status.error() was called
    mock_status.error.assert_called_once()
    call_args = mock_status.error.call_args[0][0]
    assert "String not found" in call_args


@pytest.mark.asyncio
async def test_replace_lines_invalid_range_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that replace_lines calls status.error() on invalid line range."""
    # Create test file
    test_file = tmp_allowed_dir / "test_replace_lines_error.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Call replace_lines with invalid range
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "replace_lines",
        "start_line": 5,
        "end_line": 10,
        "content": "New content",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"
    assert result["error_type"] == "ValidationError"
    assert "start_line" in result["error"]

    # Verify status.error() was called
    mock_status.error.assert_called_once()
    call_args = mock_status.error.call_args[0][0]
    assert "Invalid" in call_args or "error" in call_args.lower()


@pytest.mark.asyncio
async def test_append_mode_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that append mode calls status.end()."""
    # Create test file
    test_file = tmp_allowed_dir / "test_append_status.txt"
    test_file.write_text("Line 1\n")

    # Call append
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "append",
        "content": "Appended line\n",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"

    # Verify status.end() was called
    mock_status.end.assert_called_once()
    call_args = mock_status.end.call_args[0][0]
    assert "appended content" in call_args


@pytest.mark.asyncio
async def test_insert_mode_calls_status_end(server, tmp_allowed_dir, mock_status):
    """Test that insert mode calls status.end()."""
    # Create test file
    test_file = tmp_allowed_dir / "test_insert_status.txt"
    test_file.write_text("Line 1\nLine 2\n")

    # Call insert
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "insert",
        "line_number": 1,
        "content": "Inserted line",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "success"

    # Verify status.end() was called
    mock_status.end.assert_called_once()
    call_args = mock_status.end.call_args[0][0]
    assert "inserted at line" in call_args


@pytest.mark.asyncio
async def test_file_not_found_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that file not found calls status.error()."""
    # Call with non-existent file
    result = await server.edit_file({
        "file_path": str(tmp_allowed_dir / "non_existent.txt"),
        "mode": "append",
        "content": "Test",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"
    assert result["error_type"] == "FileNotFoundError"

    # Verify status.error() was called
    mock_status.error.assert_called_once()
    call_args = mock_status.error.call_args[0][0]
    assert "not found" in call_args.lower()


@pytest.mark.asyncio
async def test_replace_whitespace_mismatch_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that replace calls status.error() on whitespace mismatch."""
    # Create test file with spaces
    test_file = tmp_allowed_dir / "test_whitespace.txt"
    test_file.write_text("Line 1\n  Line 2 with spaces\nLine 3\n")

    # Call replace with tabs instead of spaces
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "replace",
        "old_string": "\tLine 2 with spaces",  # Tab instead of spaces
        "new_string": "Modified",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"
    assert result["error_type"] in ["StringNotFoundError", "WhitespaceMatchError"]

    # Verify status.error() was called
    mock_status.error.assert_called_once()


@pytest.mark.asyncio
async def test_replace_crlf_lf_mismatch_calls_status_error(server, tmp_allowed_dir, mock_status):
    """Test that replace calls status.error() on CRLF/LF mismatch."""
    # Create test file with LF
    test_file = tmp_allowed_dir / "test_crlf.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Call replace with CRLF
    result = await server.edit_file({
        "file_path": str(test_file),
        "mode": "replace",
        "old_string": "Line 2\r\n",  # CRLF instead of LF
        "new_string": "Modified",
        "_status": mock_status
    })

    # Verify result
    assert result["status"] == "error"

    # Verify status.error() was called
    mock_status.error.assert_called_once()
