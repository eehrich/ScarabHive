"""Tests for file_ops search_files glob pattern functionality."""

from __future__ import annotations

import gc
import pytest
from pathlib import Path
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
    system_config.project_root = str(tmp_allowed_dir.parent)

    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(tmp_allowed_dir)]
    mcp_config.search = {
        "enable_indexing": False  # Disable for faster tests
    }

    server = FileOpsServer("file_ops", system_config, mcp_config)
    yield server

    # Cleanup: stop background indexing and close resources
    await server.search_engine.stop()
    # Force garbage collection to close any file handles
    gc.collect()


@pytest.mark.asyncio
async def test_search_files_with_path_pattern(file_ops_server, tmp_allowed_dir):
    """Test that glob patterns with path components work correctly."""
    # Create subdirectory and files in tmp_allowed_dir
    workspace_dir = tmp_allowed_dir / "data" / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Create test files
    (workspace_dir / "test1.txt").write_text("content1")
    (workspace_dir / "test2.txt").write_text("content2")
    (workspace_dir / "readme.md").write_text("readme")

    # Create file in parent dir (should not match)
    (tmp_allowed_dir / "other.txt").write_text("other")

    # Test 1: Pattern with subdirectory - *.txt in workspace folder
    result = await file_ops_server.search_files({
        "pattern": "data/workspace/*.txt"
    })

    assert result["status"] == "success", f"Search failed: {result.get('error')}"
    assert result["total_found"] == 2, f"Expected 2 files, found {result['total_found']}: {result['files']}"

    file_names = [Path(f).name for f in result["files"]]
    assert "test1.txt" in file_names
    assert "test2.txt" in file_names
    assert "readme.md" not in file_names
    assert "other.txt" not in file_names

    # Test 2: Pattern with prefix - test* in workspace folder
    result = await file_ops_server.search_files({
        "pattern": "data/workspace/test*"
    })

    assert result["status"] == "success"
    assert result["total_found"] == 2, f"Expected 2 files, found {result['total_found']}: {result['files']}"

    file_names = [Path(f).name for f in result["files"]]
    assert "test1.txt" in file_names
    assert "test2.txt" in file_names
    assert "readme.md" not in file_names

    # Test 3: Simple filename pattern (no path) - should still work
    result = await file_ops_server.search_files({
        "pattern": "*.txt"
    })

    assert result["status"] == "success"
    # Should find all .txt files (test1.txt, test2.txt, other.txt)
    assert result["total_found"] >= 3, f"Expected at least 3 files, found {result['total_found']}: {result['files']}"


@pytest.mark.asyncio
async def test_search_files_glob_with_double_star(file_ops_server, tmp_allowed_dir):
    """Test that ** glob pattern works for recursive search."""
    # Create nested structure
    (tmp_allowed_dir / "level1" / "level2").mkdir(parents=True, exist_ok=True)
    (tmp_allowed_dir / "level1" / "file1.py").write_text("# python")
    (tmp_allowed_dir / "level1" / "level2" / "file2.py").write_text("# python")
    (tmp_allowed_dir / "level1" / "level2" / "file3.txt").write_text("text")

    # Test recursive pattern
    result = await file_ops_server.search_files({
        "pattern": "**/*.py"
    })

    assert result["status"] == "success"
    assert result["total_found"] == 2, f"Expected 2 Python files, found {result['total_found']}: {result['files']}"

    file_names = [Path(f).name for f in result["files"]]
    assert "file1.py" in file_names
    assert "file2.py" in file_names
    assert "file3.txt" not in file_names


@pytest.mark.asyncio
async def test_search_files_relative_to_allowed_dir(tmp_path):
    """Test that patterns are correctly interpreted relative to allowed_dirs."""
    # Create structure: project/src/main.py
    project_dir = tmp_path / "project"
    src_dir = project_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "main.py").write_text("main")
    (src_dir / "utils.py").write_text("utils")

    # Initialize with project dir as allowed
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_path)

    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(project_dir)]
    mcp_config.search = {"enable_indexing": False}

    server = FileOpsServer("file_ops", system_config, mcp_config)

    try:
        # Pattern should be relative to allowed_dir (project/)
        result = await server.search_files({
            "pattern": "src/*.py"
        })

        assert result["status"] == "success"
        assert result["total_found"] == 2, f"Expected 2 files, found {result['total_found']}: {result['files']}"

        file_names = [Path(f).name for f in result["files"]]
        assert "main.py" in file_names
        assert "utils.py" in file_names
    finally:
        await server.search_engine.stop()
        gc.collect()


@pytest.mark.asyncio
async def test_search_files_with_indexing_enabled(tmp_path):
    """Test that glob patterns work correctly with indexing enabled."""
    # Create structure
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    workspace_dir = allowed_dir / "data" / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Create test files
    (workspace_dir / "test1.txt").write_text("content1")
    (workspace_dir / "test2.txt").write_text("content2")
    (workspace_dir / "readme.md").write_text("readme")
    (allowed_dir / "other.txt").write_text("other")

    # Initialize with indexing ENABLED
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_path)

    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(allowed_dir)]
    mcp_config.search = {
        "enable_indexing": True,
        "index_on_startup": False  # Don't start background task
    }

    server = FileOpsServer("file_ops", system_config, mcp_config)

    try:
        # Manually trigger indexing
        await server.search_engine.rebuild_index(incremental=False)

        # Test with path pattern
        result = await server.search_files({
            "pattern": "data/workspace/*.txt"
        })

        assert result["status"] == "success", f"Search failed: {result.get('error')}"
        # This will likely FAIL because filename index only matches against filename
        assert result["total_found"] == 2, (
            f"Expected 2 files, found {result['total_found']}: {result['files']}\n"
            f"Bug: filename index doesn't match path patterns correctly"
        )

        file_names = [Path(f).name for f in result["files"]]
        assert "test1.txt" in file_names
        assert "test2.txt" in file_names

    finally:
        await server.search_engine.stop()
        gc.collect()


@pytest.mark.asyncio
async def test_search_files_allowed_dir_is_subdirectory(tmp_path):
    """Test the actual bug: allowed_dir set to subdirectory, pattern includes that path."""
    # This mimics the production config:
    # allowed_directories: ["data/workspace"]
    # pattern: "data/workspace/*.txt"

    # Create the full structure
    project_root = tmp_path
    workspace_dir = project_root / "data" / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Create test files
    (workspace_dir / "test1.txt").write_text("content1")
    (workspace_dir / "test2.txt").write_text("content2")
    (workspace_dir / "readme.md").write_text("readme")

    # Configure with allowed_dir = "data/workspace" (NOT project_root!)
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(project_root)

    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(workspace_dir)]  # The subdirectory!
    mcp_config.search = {"enable_indexing": False}

    server = FileOpsServer("file_ops", system_config, mcp_config)

    try:
        # BUG: User searches for "data/workspace/*.txt"
        # But allowed_dir IS data/workspace
        # So rglob looks for data/workspace/data/workspace/*.txt (wrong!)
        result = await server.search_files({
            "pattern": "data/workspace/*.txt"
        })

        assert result["status"] == "success", f"Search failed: {result.get('error')}"
        assert result["total_found"] == 2, (
            f"BUG: Expected 2 files, found {result['total_found']}: {result['files']}\n"
            f"Pattern 'data/workspace/*.txt' should match files in allowed_dir 'data/workspace'"
        )

        file_names = [Path(f).name for f in result["files"]]
        assert "test1.txt" in file_names
        assert "test2.txt" in file_names

    finally:
        await server.search_engine.stop()
        gc.collect()
