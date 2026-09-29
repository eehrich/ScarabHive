"""Tests for file_ops manage tool (create, delete, move, rename operations)."""

from __future__ import annotations

import gc
import os
from pathlib import Path

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
    # The refusal names the way out, in one call instead of delete + create.
    assert "overwrite=true" in result["error"]

    # Original content preserved
    assert existing_file.read_text() == "Original content"


@pytest.mark.asyncio
async def test_manage_create_replaces_the_file_when_overwrite_is_set(file_ops_server, tmp_allowed_dir):
    """One call replaces a file whole: no delete first."""
    existing_file = tmp_allowed_dir / "existing.txt"
    existing_file.write_text("Original content")

    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(existing_file),
        "content": "New content",
        "overwrite": True
    })

    assert result["status"] == "success"
    assert existing_file.read_text() == "New content"
    assert result["bytes_written"] == len("New content")
    # The caller asked to be allowed to replace; this says whether it happened.
    assert result["replaced"] is True


@pytest.mark.asyncio
async def test_manage_create_says_nothing_was_replaced_for_a_new_file(file_ops_server, tmp_allowed_dir):
    """overwrite: true on a path that is free still creates, and says so."""
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "fresh.txt"),
        "content": "New content",
        "overwrite": True
    })

    assert result["status"] == "success" and result["replaced"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [False, True])
async def test_manage_create_refuses_a_directory(file_ops_server, tmp_allowed_dir, overwrite):
    """A directory is no file to replace, and overwrite is no way out of it:
    the refusal must not send the model after that option."""
    directory = tmp_allowed_dir / "a_directory"
    directory.mkdir()
    (directory / "keep.txt").write_text("keep me")

    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(directory),
        "content": "New content",
        "overwrite": overwrite
    })

    assert result["status"] == "error" and result["error_type"] == "IsADirectoryError"
    assert "not a file" in result["error"] and "overwrite=true" not in result["error"]
    assert directory.is_dir() and (directory / "keep.txt").read_text() == "keep me"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX mode bits to carry over")
@pytest.mark.asyncio
async def test_manage_create_keeps_the_mode_of_the_file_it_replaces(file_ops_server, tmp_allowed_dir):
    """A rewritten script must stay executable, a 0600 file unreadable to others."""
    script = tmp_allowed_dir / "deploy.sh"
    script.write_text("#!/bin/sh\necho old\n")
    script.chmod(0o755)

    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(script),
        "content": "#!/bin/sh\necho new\n",
        "overwrite": True
    })

    assert result["status"] == "success"
    assert script.stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_manage_create_puts_the_old_mode_on_the_replacement(file_ops_server, tmp_allowed_dir, monkeypatch):
    """What the test above checks by its effect, this one checks where there
    are no POSIX mode bits to compare: the mode goes onto the temp file, before
    it takes the old file's place."""
    target = tmp_allowed_dir / "deploy.sh"
    target.write_text("#!/bin/sh\necho old\n")
    mode, seen = target.stat().st_mode, []
    chmod = Path.chmod

    def recording(self, new_mode, *args, **kwargs):
        seen.append((self.name, new_mode))
        return chmod(self, new_mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", recording)
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(target),
        "content": "#!/bin/sh\necho new\n",
        "overwrite": True
    })

    assert result["status"] == "success"
    [(name, carried)] = seen
    assert name.startswith(".deploy.sh.") and name.endswith(".tmp") and carried == mode


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX mode bits to carry over")
@pytest.mark.asyncio
async def test_an_edit_keeps_the_mode_of_the_file_it_changes(file_ops_server, tmp_allowed_dir):
    """An edit writes a new file too: a script lost its executable bit."""
    script = tmp_allowed_dir / "deploy.sh"
    script.write_text("#!/bin/sh\necho old\n")
    script.chmod(0o750)

    result = await file_ops_server.replace_string_in_file({
        "filePath": str(script), "oldString": "echo old", "newString": "echo new"})

    assert result["status"] == "success", result
    assert script.stat().st_mode & 0o777 == 0o750


@pytest.mark.asyncio
async def test_a_file_named_like_a_temp_file_survives_the_writes_beside_it(file_ops_server, tmp_allowed_dir):
    """The writes went through `<name>.tmp` and deleted a file of that name
    the person kept -- a file no checkpoint of the write names."""
    notes = tmp_allowed_dir / "notes.md"
    notes.write_text("old")
    (tmp_allowed_dir / "notes.md.tmp").write_text("the person's scratch")

    created = await file_ops_server.manage({
        "operation": "create", "path": str(notes), "content": "new", "overwrite": True})
    edited = await file_ops_server.replace_string_in_file({
        "filePath": str(notes), "oldString": "new", "newString": "newer"})

    assert created["status"] == "success" and edited["status"] == "success", (created, edited)
    assert (tmp_allowed_dir / "notes.md.tmp").read_text() == "the person's scratch"
    assert sorted(p.name for p in tmp_allowed_dir.iterdir()) == ["notes.md", "notes.md.tmp"], (
        "a temp file was left behind")
    assert notes.read_text() == "newer"


@pytest.mark.asyncio
async def test_a_temp_name_taken_after_all_is_refused_and_left_alone(file_ops_server, tmp_allowed_dir, monkeypatch):
    """Twelve random hex digits make it unlikely, not impossible: the write
    fails then -- and must not delete the file that has that name."""
    from plugins.file_ops import operations

    monkeypatch.setattr(operations.secrets, "token_hex", lambda n: "0" * (2 * n))
    notes = tmp_allowed_dir / "notes.md"
    notes.write_text("old")
    taken = tmp_allowed_dir / ".notes.md.000000000000.tmp"
    taken.write_text("somebody's")

    result = await file_ops_server.manage({
        "operation": "create", "path": str(notes), "content": "new", "overwrite": True})

    assert result["status"] == "error", result
    assert taken.read_text() == "somebody's"
    assert notes.read_text() == "old"


@pytest.mark.asyncio
async def test_a_write_cancelled_while_it_opens_leaves_no_temp_file(tmp_allowed_dir, monkeypatch):
    """The temp is opened in the thread pool; a cancel that came meanwhile left
    it behind -- with random names, one more per cancel."""
    import asyncio
    import time

    from aiofiles import threadpool
    from plugins.file_ops import operations

    real_open = threadpool.sync_open

    def slow_open(*args, **kwargs):
        time.sleep(0.3)          # a slow disk, a network mount
        return real_open(*args, **kwargs)

    monkeypatch.setattr(threadpool, "sync_open", slow_open)
    target = tmp_allowed_dir / "notes.md"
    target.write_text("old")

    task = asyncio.create_task(operations._write_through_temp(target, "new", "utf-8", keep_mode=True))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)     # the thread's open has finished by now

    assert sorted(p.name for p in tmp_allowed_dir.iterdir()) == ["notes.md"]
    assert target.read_text() == "old"


@pytest.mark.asyncio
async def test_a_name_near_the_limit_can_still_be_written(file_ops_server, tmp_allowed_dir):
    """The temp name is not longer than a long name itself: 243 characters
    went through before temp names carried a random part, and must still."""
    long_file = tmp_allowed_dir / ("n" * 239 + ".txt")
    long_file.write_text("old")

    created = await file_ops_server.manage({
        "operation": "create", "path": str(long_file), "content": "new", "overwrite": True})
    edited = await file_ops_server.replace_string_in_file({
        "filePath": str(long_file), "oldString": "new", "newString": "newer"})

    assert created["status"] == "success" and edited["status"] == "success", (created, edited)
    assert long_file.read_text() == "newer"


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
