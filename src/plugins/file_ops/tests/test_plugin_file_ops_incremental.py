"""Tests for incremental indexing in file_ops plugin."""

from __future__ import annotations

import asyncio
import gc
import pytest
import time
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, MCPConfig
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
    
    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(tmp_allowed_dir)]
    mcp_config.search = {
        "enable_indexing": True,
        "enable_semantic_search": False,  # Disable for faster tests
        "index_on_startup": False
    }
    
    server = FileOpsServer("file_ops", system_config, mcp_config)
    yield server
    
    # Cleanup
    await server.search_engine.stop()
    gc.collect()


@pytest.mark.asyncio
async def test_full_index_build(file_ops_server, tmp_allowed_dir):
    """Test initial full index build."""
    # Create test files
    (tmp_allowed_dir / "file1.py").write_text("def hello(): pass")
    (tmp_allowed_dir / "file2.txt").write_text("Hello World")
    (tmp_allowed_dir / "file3.md").write_text("# Title")
    
    # Build full index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    
    # Verify all files are indexed
    assert len(file_ops_server.search_engine.file_mtimes) == 3


@pytest.mark.asyncio
async def test_incremental_no_changes(file_ops_server, tmp_allowed_dir):
    """Test incremental update when no files changed."""
    # Create test files
    (tmp_allowed_dir / "file1.py").write_text("def hello(): pass")
    (tmp_allowed_dir / "file2.txt").write_text("Hello World")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    initial_mtimes = file_ops_server.search_engine.file_mtimes.copy()
    
    # Wait a bit to ensure timestamp difference
    await asyncio.sleep(0.1)
    
    # Run incremental update (no changes)
    start_time = time.time()
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    elapsed = time.time() - start_time
    
    # Should be very fast (no files to reindex)
    assert elapsed < 1.0, f"Incremental update too slow: {elapsed:.2f}s"
    
    # Index should be unchanged
    assert file_ops_server.search_engine.file_mtimes == initial_mtimes


@pytest.mark.asyncio
async def test_incremental_file_modified(file_ops_server, tmp_allowed_dir):
    """Test incremental update detects and reindexes modified files."""
    # Create test file
    test_file = tmp_allowed_dir / "modified.py"
    test_file.write_text("old content")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    old_mtime = file_ops_server.search_engine.file_mtimes[test_file]
    
    # Wait and modify file
    await asyncio.sleep(0.1)
    test_file.write_text("new content with different words")
    
    # Incremental update should detect change
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    new_mtime = file_ops_server.search_engine.file_mtimes[test_file]
    
    # mtime should be updated
    assert new_mtime > old_mtime
    
    # Search for new content should work
    result = await file_ops_server.search_engine.grep_search(
        query="different",
        is_regex=False,
        case_sensitive=False
    )
    
    assert result["status"] == "success"
    assert result["total_matches"] >= 1


@pytest.mark.asyncio
async def test_incremental_file_added(file_ops_server, tmp_allowed_dir):
    """Test incremental update detects new files."""
    # Create initial file
    (tmp_allowed_dir / "file1.py").write_text("old file")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    assert len(file_ops_server.search_engine.file_mtimes) == 1
    
    # Add new file
    (tmp_allowed_dir / "file2.py").write_text("new file")
    
    # Incremental update should detect new file
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    assert len(file_ops_server.search_engine.file_mtimes) == 2
    
    # New file should be searchable
    result = await file_ops_server.search_engine.search_files("file2.py")
    assert result["status"] == "success"
    assert result["total_found"] >= 1


@pytest.mark.asyncio
async def test_incremental_file_deleted(file_ops_server, tmp_allowed_dir):
    """Test incremental update detects deleted files."""
    # Create test files
    file1 = tmp_allowed_dir / "keep.py"
    file2 = tmp_allowed_dir / "delete.py"
    file1.write_text("keep this")
    file2.write_text("delete this")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    assert len(file_ops_server.search_engine.file_mtimes) == 2
    
    # Delete one file
    file2.unlink()
    
    # Incremental update should detect deletion
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    assert len(file_ops_server.search_engine.file_mtimes) == 1
    assert file1 in file_ops_server.search_engine.file_mtimes
    assert file2 not in file_ops_server.search_engine.file_mtimes


@pytest.mark.asyncio
async def test_incremental_multiple_changes(file_ops_server, tmp_allowed_dir):
    """Test incremental update with multiple simultaneous changes."""
    # Create initial files
    keep = tmp_allowed_dir / "keep.py"
    modify = tmp_allowed_dir / "modify.py"
    delete = tmp_allowed_dir / "delete.py"
    
    keep.write_text("unchanged")
    modify.write_text("old content")
    delete.write_text("will be deleted")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    assert len(file_ops_server.search_engine.file_mtimes) == 3
    
    # Wait and make multiple changes
    await asyncio.sleep(0.1)
    modify.write_text("new content")
    delete.unlink()
    new = tmp_allowed_dir / "new.py"
    new.write_text("brand new")
    
    # Incremental update should handle all changes
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    
    # Verify results
    assert len(file_ops_server.search_engine.file_mtimes) == 3
    assert keep in file_ops_server.search_engine.file_mtimes
    assert modify in file_ops_server.search_engine.file_mtimes
    assert new in file_ops_server.search_engine.file_mtimes
    assert delete not in file_ops_server.search_engine.file_mtimes


@pytest.mark.asyncio
async def test_ensure_index_fresh_throttling(file_ops_server, tmp_allowed_dir):
    """Test that _ensure_index_fresh throttles updates."""
    # Create test file
    (tmp_allowed_dir / "test.py").write_text("content")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    
    # Call _ensure_index_fresh multiple times quickly
    start_time = time.time()
    for _ in range(5):
        await file_ops_server.search_engine._ensure_index_fresh()
    elapsed = time.time() - start_time
    
    # Should be fast (throttled, not running 5 full updates)
    assert elapsed < 1.0, f"_ensure_index_fresh not throttling properly: {elapsed:.2f}s"
    
    # After 30+ seconds, should actually update
    file_ops_server.search_engine._last_incremental_update = time.time() - 31
    (tmp_allowed_dir / "new.py").write_text("new file")
    
    await file_ops_server.search_engine._ensure_index_fresh()
    
    # New file should be indexed
    assert len(file_ops_server.search_engine.file_mtimes) == 2


@pytest.mark.asyncio
async def test_search_sees_files_added_after_the_index_was_built(file_ops_server, tmp_allowed_dir):
    """A glob walks the disk, so a stale index cannot hide a new file."""
    # Create initial file
    (tmp_allowed_dir / "file1.py").write_text("old content")
    
    # Build initial index
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    
    # Add new file
    await asyncio.sleep(0.1)
    (tmp_allowed_dir / "file2.py").write_text("new content")
    
    # Mark as needing update (simulate time passing)
    file_ops_server.search_engine._last_incremental_update = time.time() - 31
    
    result = await file_ops_server.search_engine.search_files("*.py")
    
    assert result["status"] == "success"
    # Both files are found, the new one included
    assert result["total_found"] >= 2


@pytest.mark.asyncio
async def test_background_indexer_incremental(file_ops_server, tmp_allowed_dir):
    """Test that background indexer uses incremental updates."""
    # Create initial file
    (tmp_allowed_dir / "file1.py").write_text("initial")
    
    # Manually simulate background indexer behavior
    # First run: full rebuild
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    assert len(file_ops_server.search_engine.file_mtimes) == 1
    
    # Add file
    (tmp_allowed_dir / "file2.py").write_text("second")
    
    # Second run: incremental
    start_time = time.time()
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    elapsed = time.time() - start_time
    
    # Should be fast (incremental)
    assert elapsed < 1.0
    assert len(file_ops_server.search_engine.file_mtimes) == 2


@pytest.mark.asyncio 
async def test_incremental_performance(file_ops_server, tmp_allowed_dir):
    """Test that incremental updates are significantly faster than full rebuild."""
    # Create many files
    for i in range(50):
        (tmp_allowed_dir / f"file{i}.py").write_text(f"content {i}" * 100)
    
    # Full rebuild
    start = time.time()
    await file_ops_server.search_engine.rebuild_index(incremental=False)
    full_time = time.time() - start
    
    # Modify just one file
    await asyncio.sleep(0.1)
    (tmp_allowed_dir / "file0.py").write_text("modified content")
    
    # Incremental update
    start = time.time()
    await file_ops_server.search_engine.rebuild_index(incremental=True)
    incremental_time = time.time() - start
    
    # Incremental should be faster (at least 2x) or both very fast (< 0.1s each)
    # On fast systems, both may be near-instantaneous, so we allow that case
    is_faster = incremental_time < full_time / 2
    both_fast = full_time < 0.1 and incremental_time < 0.1
    assert is_faster or both_fast, \
        f"Incremental ({incremental_time:.2f}s) not significantly faster than full ({full_time:.2f}s)"
