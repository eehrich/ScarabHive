"""Tests for file_ops plugin read_only mode."""

from __future__ import annotations

import gc
import pytest
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
def tmp_allowed_dir(tmp_path):
    """Create a temporary allowed directory."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    return allowed_dir


@pytest.fixture
async def readonly_file_ops_server(tmp_allowed_dir):
    """Create file operations server with read_only mode enabled."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_allowed_dir.parent)

    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tmp_allowed_dir)]
    server_config.read_only = True  # Enable read_only mode
    server_config.search = {
        "enable_indexing": False
    }

    server = FileOpsServer("file_ops", system_config, server_config)
    yield server

    await server.search_engine.stop()
    gc.collect()


@pytest.fixture
async def normal_file_ops_server(tmp_allowed_dir):
    """Create file operations server without read_only mode."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_allowed_dir.parent)

    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tmp_allowed_dir)]
    server_config.read_only = False  # Explicitly set to False
    server_config.search = {
        "enable_indexing": False
    }

    server = FileOpsServer("file_ops", system_config, server_config)
    yield server

    await server.search_engine.stop()
    gc.collect()


# ============================================================================
# read_only attribute tests
# ============================================================================

@pytest.mark.asyncio
async def test_readonly_attribute_true(readonly_file_ops_server):
    """Test that read_only attribute is set when config has read_only=True."""
    assert readonly_file_ops_server.read_only is True


@pytest.mark.asyncio
async def test_readonly_attribute_false(normal_file_ops_server):
    """Test that read_only attribute is False when config has read_only=False."""
    assert normal_file_ops_server.read_only is False


@pytest.mark.asyncio
async def test_readonly_attribute_default(tmp_allowed_dir):
    """Test that read_only defaults to False when not specified."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_allowed_dir.parent)

    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tmp_allowed_dir)]
    # Note: read_only is NOT set
    server_config.search = {"enable_indexing": False}

    server = FileOpsServer("file_ops", system_config, server_config)
    try:
        assert server.read_only is False
    finally:
        await server.search_engine.stop()
        gc.collect()


# ============================================================================
# get_template_vars tests
# ============================================================================

@pytest.mark.asyncio
async def test_get_template_vars_readonly_true(readonly_file_ops_server):
    """Test that get_template_vars exposes read_only=True."""
    vars = readonly_file_ops_server.get_template_vars()
    assert "read_only" in vars
    assert vars["read_only"] is True


@pytest.mark.asyncio
async def test_get_template_vars_readonly_false(normal_file_ops_server):
    """Test that get_template_vars exposes read_only=False."""
    vars = normal_file_ops_server.get_template_vars()
    assert "read_only" in vars
    assert vars["read_only"] is False


# ============================================================================
# manage() blocked in read_only mode
# ============================================================================

@pytest.mark.asyncio
async def test_readonly_blocks_manage_create(readonly_file_ops_server, tmp_allowed_dir):
    """Test that manage create operation is blocked in read_only mode."""
    result = await readonly_file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "test.txt"),
        "content": "Hello"
    })
    
    assert result["status"] == "error"
    assert "read_only" in result["error"].lower() or "read-only" in result["error"].lower()


@pytest.mark.asyncio
async def test_readonly_blocks_manage_delete(readonly_file_ops_server, tmp_allowed_dir):
    """Test that manage delete operation is blocked in read_only mode."""
    # Create a file first (directly, not via manage)
    test_file = tmp_allowed_dir / "to_delete.txt"
    test_file.write_text("content")
    
    result = await readonly_file_ops_server.manage({
        "operation": "delete",
        "path": str(test_file)
    })
    
    assert result["status"] == "error"
    assert "read_only" in result["error"].lower() or "read-only" in result["error"].lower()
    # File should still exist
    assert test_file.exists()


@pytest.mark.asyncio
async def test_readonly_blocks_manage_move(readonly_file_ops_server, tmp_allowed_dir):
    """Test that manage move operation is blocked in read_only mode."""
    # Create a file first (directly)
    test_file = tmp_allowed_dir / "to_move.txt"
    test_file.write_text("content")
    
    result = await readonly_file_ops_server.manage({
        "operation": "move",
        "path": str(test_file),
        "destination": str(tmp_allowed_dir / "moved.txt")
    })
    
    assert result["status"] == "error"
    assert "read_only" in result["error"].lower() or "read-only" in result["error"].lower()
    # File should still be at original location
    assert test_file.exists()


@pytest.mark.asyncio
async def test_readonly_blocks_manage_rename(readonly_file_ops_server, tmp_allowed_dir):
    """Test that manage rename operation is blocked in read_only mode."""
    # Create a file first (directly)
    test_file = tmp_allowed_dir / "to_rename.txt"
    test_file.write_text("content")
    
    result = await readonly_file_ops_server.manage({
        "operation": "rename",
        "path": str(test_file),
        "new_name": "renamed.txt"
    })
    
    assert result["status"] == "error"
    assert "read_only" in result["error"].lower() or "read-only" in result["error"].lower()
    # File should still have original name
    assert test_file.exists()


# ============================================================================
# replace_string_in_file blocked in read_only mode
# ============================================================================

@pytest.mark.asyncio
async def test_readonly_blocks_replace_string(readonly_file_ops_server, tmp_allowed_dir):
    """Test that replace_string_in_file is blocked in read_only mode."""
    # Create a file first (directly)
    test_file = tmp_allowed_dir / "to_replace.txt"
    test_file.write_text("Hello World")
    
    result = await readonly_file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Hello",
        "newString": "Hi"
    })
    
    assert result["status"] == "error"
    assert "read_only" in result["error"].lower() or "read-only" in result["error"].lower()
    # File should be unchanged
    assert test_file.read_text() == "Hello World"


# ============================================================================
# Read operations allowed in read_only mode
# ============================================================================

@pytest.mark.asyncio
async def test_readonly_allows_read_file(readonly_file_ops_server, tmp_allowed_dir):
    """Test that read_file works in read_only mode."""
    # Create a file first (directly)
    test_file = tmp_allowed_dir / "readable.txt"
    test_file.write_text("Hello World")
    
    result = await readonly_file_ops_server.read_file({
        "filePath": str(test_file)
    })
    
    assert result["status"] == "success"
    assert "Hello World" in result["content"]


@pytest.mark.asyncio
async def test_readonly_allows_list_directory(readonly_file_ops_server, tmp_allowed_dir):
    """Test that list_directory works in read_only mode."""
    # Create some files (directly)
    (tmp_allowed_dir / "file1.txt").write_text("1")
    (tmp_allowed_dir / "file2.txt").write_text("2")
    
    result = await readonly_file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir)
    })
    
    assert result["status"] == "success"
    assert result["total_files"] == 2


@pytest.mark.asyncio
async def test_readonly_allows_search_files(readonly_file_ops_server, tmp_allowed_dir):
    """Test that search_files works in read_only mode."""
    # Create some files (directly)
    (tmp_allowed_dir / "test.py").write_text("python code")
    (tmp_allowed_dir / "test.txt").write_text("text file")
    
    result = await readonly_file_ops_server.search_files({
        "pattern": "*.py"
    })
    
    assert result["status"] == "success"
    assert result["total_found"] >= 1


# ============================================================================
# Normal mode allows write operations
# ============================================================================

@pytest.mark.asyncio
async def test_normal_mode_allows_manage_create(normal_file_ops_server, tmp_allowed_dir):
    """Test that manage create works when read_only=False."""
    result = await normal_file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "test.txt"),
        "content": "Hello"
    })
    
    assert result["status"] == "success"
    assert (tmp_allowed_dir / "test.txt").exists()


@pytest.mark.asyncio
async def test_normal_mode_allows_replace_string(normal_file_ops_server, tmp_allowed_dir):
    """Test that replace_string_in_file works when read_only=False."""
    # Create a file first (directly)
    test_file = tmp_allowed_dir / "to_replace.txt"
    test_file.write_text("Hello World")
    
    result = await normal_file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Hello",
        "newString": "Hi"
    })
    
    assert result["status"] == "success"
    assert test_file.read_text() == "Hi World"
