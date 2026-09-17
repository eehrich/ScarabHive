"""Tests for file_ops manage tool (create, delete, move, rename operations)."""

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
    gc.collect()


# === CREATE OPERATION TESTS ===

@pytest.mark.asyncio
async def test_manage_create_file(file_ops_server, tmp_allowed_dir):
    """Test manage operation: create file."""
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "new_file.txt"),
        "content": "Hello World"
    })

    assert result["status"] == "success"
    assert "file_path" in result
    assert result["bytes_written"] == 11

    # Verify file exists
    created_file = tmp_allowed_dir / "new_file.txt"
    assert created_file.exists()
    assert created_file.read_text() == "Hello World"


@pytest.mark.asyncio
async def test_manage_create_file_with_dirs(file_ops_server, tmp_allowed_dir):
    """Test manage create auto-creates parent directories."""
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "subdir" / "nested" / "file.txt"),
        "content": "Nested content"
    })

    assert result["status"] == "success"
    
    created_file = tmp_allowed_dir / "subdir" / "nested" / "file.txt"
    assert created_file.exists()
    assert created_file.read_text() == "Nested content"


@pytest.mark.asyncio
async def test_manage_create_file_exists_fails(file_ops_server, tmp_allowed_dir):
    """Test manage create fails if file already exists."""
    existing_file = tmp_allowed_dir / "existing.txt"
    existing_file.write_text("Original content")

    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(existing_file),
        "content": "New content"
    })

    assert result["status"] == "error"
    assert "already exists" in result["error"].lower()
    
    # Original content preserved
    assert existing_file.read_text() == "Original content"


@pytest.mark.asyncio
async def test_manage_create_missing_content(file_ops_server, tmp_allowed_dir):
    """Test manage create fails without content parameter."""
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "new_file.txt")
    })

    assert result["status"] == "error"
    assert "content" in result["error"].lower()


# === DELETE OPERATION TESTS ===

@pytest.mark.asyncio
async def test_manage_delete_file(file_ops_server, tmp_allowed_dir):
    """Test manage operation: delete file."""
    test_file = tmp_allowed_dir / "to_delete.txt"
    test_file.write_text("Delete me")

    result = await file_ops_server.manage({
        "operation": "delete",
        "path": str(test_file)
    })

    assert result["status"] == "success"
    assert result["type"] == "file"
    assert not test_file.exists()


@pytest.mark.asyncio
async def test_manage_delete_empty_directory(file_ops_server, tmp_allowed_dir):
    """Test manage operation: delete empty directory."""
    empty_dir = tmp_allowed_dir / "empty_dir"
    empty_dir.mkdir()

    result = await file_ops_server.manage({
        "operation": "delete",
        "path": str(empty_dir)
    })

    assert result["status"] == "success"
    assert result["type"] == "directory"
    assert not empty_dir.exists()


@pytest.mark.asyncio
async def test_manage_delete_non_empty_directory_fails(file_ops_server, tmp_allowed_dir):
    """Test manage delete non-empty directory fails without recursive flag."""
    non_empty_dir = tmp_allowed_dir / "non_empty"
    non_empty_dir.mkdir()
    (non_empty_dir / "file.txt").write_text("content")

    result = await file_ops_server.manage({
        "operation": "delete",
        "path": str(non_empty_dir)
    })

    assert result["status"] == "error"
    # Error message varies by platform/language (e.g. "not empty", German "nicht leer", WinError 145)
    error_lower = result["error"].lower()
    assert ("not empty" in error_lower or "recursive" in error_lower or 
            "nicht leer" in error_lower or "winerror 145" in error_lower)
    assert non_empty_dir.exists()  # Directory still exists


@pytest.mark.asyncio
async def test_manage_delete_directory_recursive(file_ops_server, tmp_allowed_dir):
    """Test manage delete non-empty directory with recursive flag."""
    non_empty_dir = tmp_allowed_dir / "to_delete_recursive"
    non_empty_dir.mkdir()
    (non_empty_dir / "file1.txt").write_text("content 1")
    (non_empty_dir / "subdir").mkdir()
    (non_empty_dir / "subdir" / "file2.txt").write_text("content 2")

    result = await file_ops_server.manage({
        "operation": "delete",
        "path": str(non_empty_dir),
        "recursive": True
    })

    assert result["status"] == "success"
    assert result["type"] == "directory"
    assert not non_empty_dir.exists()


@pytest.mark.asyncio
async def test_manage_delete_nonexistent_fails(file_ops_server, tmp_allowed_dir):
    """Test manage delete nonexistent path fails."""
    result = await file_ops_server.manage({
        "operation": "delete",
        "path": str(tmp_allowed_dir / "nonexistent.txt")
    })

    assert result["status"] == "error"
    # Error message can be "not found" or "does not exist"
    error_lower = result["error"].lower()
    assert "not found" in error_lower or "does not exist" in error_lower


# === MOVE OPERATION TESTS ===

@pytest.mark.asyncio
async def test_manage_move_file(file_ops_server, tmp_allowed_dir):
    """Test manage operation: move file."""
    source_file = tmp_allowed_dir / "source.txt"
    source_file.write_text("Move me")
    dest_path = tmp_allowed_dir / "destination.txt"

    result = await file_ops_server.manage({
        "operation": "move",
        "path": str(source_file),
        "destination": str(dest_path)
    })

    assert result["status"] == "success"
    assert result["type"] == "file"
    assert not source_file.exists()
    assert dest_path.exists()
    assert dest_path.read_text() == "Move me"


@pytest.mark.asyncio
async def test_manage_move_file_to_new_directory(file_ops_server, tmp_allowed_dir):
    """Test manage move file creates parent directories."""
    source_file = tmp_allowed_dir / "source.txt"
    source_file.write_text("Move to new dir")
    dest_path = tmp_allowed_dir / "new_dir" / "subdir" / "moved.txt"

    result = await file_ops_server.manage({
        "operation": "move",
        "path": str(source_file),
        "destination": str(dest_path)
    })

    assert result["status"] == "success"
    assert not source_file.exists()
    assert dest_path.exists()
    assert dest_path.read_text() == "Move to new dir"


@pytest.mark.asyncio
async def test_manage_move_directory(file_ops_server, tmp_allowed_dir):
    """Test manage operation: move directory."""
    source_dir = tmp_allowed_dir / "source_dir"
    source_dir.mkdir()
    (source_dir / "file.txt").write_text("content")
    dest_dir = tmp_allowed_dir / "dest_dir"

    result = await file_ops_server.manage({
        "operation": "move",
        "path": str(source_dir),
        "destination": str(dest_dir)
    })

    assert result["status"] == "success"
    assert result["type"] == "directory"
    assert not source_dir.exists()
    assert dest_dir.exists()
    assert (dest_dir / "file.txt").read_text() == "content"


@pytest.mark.asyncio
async def test_manage_move_destination_exists_fails(file_ops_server, tmp_allowed_dir):
    """Test manage move fails if destination exists."""
    source_file = tmp_allowed_dir / "source.txt"
    source_file.write_text("Source")
    dest_file = tmp_allowed_dir / "dest.txt"
    dest_file.write_text("Destination")

    result = await file_ops_server.manage({
        "operation": "move",
        "path": str(source_file),
        "destination": str(dest_file)
    })

    assert result["status"] == "error"
    assert "exists" in result["error"].lower()
    
    # Both files unchanged
    assert source_file.exists()
    assert dest_file.read_text() == "Destination"


@pytest.mark.asyncio
async def test_manage_move_missing_destination(file_ops_server, tmp_allowed_dir):
    """Test manage move fails without destination parameter."""
    source_file = tmp_allowed_dir / "source.txt"
    source_file.write_text("Source")

    result = await file_ops_server.manage({
        "operation": "move",
        "path": str(source_file)
    })

    assert result["status"] == "error"
    assert "destination" in result["error"].lower()


# === RENAME OPERATION TESTS ===

@pytest.mark.asyncio
async def test_manage_rename_file(file_ops_server, tmp_allowed_dir):
    """Test manage operation: rename file."""
    original_file = tmp_allowed_dir / "original.txt"
    original_file.write_text("Rename me")

    result = await file_ops_server.manage({
        "operation": "rename",
        "path": str(original_file),
        "new_name": "renamed.txt"
    })

    assert result["status"] == "success"
    assert result["type"] == "file"
    assert not original_file.exists()
    
    renamed_file = tmp_allowed_dir / "renamed.txt"
    assert renamed_file.exists()
    assert renamed_file.read_text() == "Rename me"


@pytest.mark.asyncio
async def test_manage_rename_directory(file_ops_server, tmp_allowed_dir):
    """Test manage operation: rename directory."""
    original_dir = tmp_allowed_dir / "original_dir"
    original_dir.mkdir()
    (original_dir / "file.txt").write_text("content")

    result = await file_ops_server.manage({
        "operation": "rename",
        "path": str(original_dir),
        "new_name": "renamed_dir"
    })

    assert result["status"] == "success"
    assert result["type"] == "directory"
    assert not original_dir.exists()
    
    renamed_dir = tmp_allowed_dir / "renamed_dir"
    assert renamed_dir.exists()
    assert (renamed_dir / "file.txt").read_text() == "content"


@pytest.mark.asyncio
async def test_manage_rename_target_exists_fails(file_ops_server, tmp_allowed_dir):
    """Test manage rename fails if target name already exists."""
    original_file = tmp_allowed_dir / "original.txt"
    original_file.write_text("Original")
    existing_file = tmp_allowed_dir / "existing.txt"
    existing_file.write_text("Existing")

    result = await file_ops_server.manage({
        "operation": "rename",
        "path": str(original_file),
        "new_name": "existing.txt"
    })

    assert result["status"] == "error"
    assert "exists" in result["error"].lower()
    
    # Both files unchanged
    assert original_file.read_text() == "Original"
    assert existing_file.read_text() == "Existing"


@pytest.mark.asyncio
async def test_manage_rename_with_path_separator_fails(file_ops_server, tmp_allowed_dir):
    """Test manage rename fails if new_name contains path separators."""
    original_file = tmp_allowed_dir / "original.txt"
    original_file.write_text("Original")

    result = await file_ops_server.manage({
        "operation": "rename",
        "path": str(original_file),
        "new_name": "subdir/renamed.txt"
    })

    assert result["status"] == "error"
    assert "separator" in result["error"].lower() or "path" in result["error"].lower()


@pytest.mark.asyncio
async def test_manage_rename_missing_new_name(file_ops_server, tmp_allowed_dir):
    """Test manage rename fails without new_name parameter."""
    original_file = tmp_allowed_dir / "original.txt"
    original_file.write_text("Original")

    result = await file_ops_server.manage({
        "operation": "rename",
        "path": str(original_file)
    })

    assert result["status"] == "error"
    assert "new_name" in result["error"].lower()


# === VALIDATION TESTS ===

@pytest.mark.asyncio
async def test_manage_invalid_operation(file_ops_server, tmp_allowed_dir):
    """Test manage fails with invalid operation."""
    result = await file_ops_server.manage({
        "operation": "invalid_op",
        "path": str(tmp_allowed_dir / "file.txt")
    })

    assert result["status"] == "error"
    assert "unknown operation" in result["error"].lower() or "invalid" in result["error"].lower()


@pytest.mark.asyncio
async def test_manage_missing_operation(file_ops_server, tmp_allowed_dir):
    """Test manage fails without operation parameter."""
    result = await file_ops_server.manage({
        "path": str(tmp_allowed_dir / "file.txt")
    })

    assert result["status"] == "error"
    assert "operation" in result["error"].lower()


@pytest.mark.asyncio
async def test_manage_missing_path(file_ops_server, tmp_allowed_dir):
    """Test manage fails without path parameter."""
    result = await file_ops_server.manage({
        "operation": "delete"
    })

    assert result["status"] == "error"
    assert "path" in result["error"].lower()


@pytest.mark.asyncio
async def test_manage_security_path_outside_allowed(file_ops_server, tmp_path):
    """Test manage blocks access outside allowed directories."""
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("Outside")

    result = await file_ops_server.manage({
        "operation": "delete",
        "path": str(outside_file)
    })

    assert result["status"] == "error"
    assert result["error_type"] == "SecurityError"
    assert outside_file.exists()  # File not deleted
