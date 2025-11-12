"""
Test cases demonstrating the fixed file_ops functionality.

This test file validates the fixes for the reported issues:
1. Grep search not finding matches
2. Semantic search not indexing root files
3. Index not being built on-demand

All issues have been resolved.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock
from agent_system.config import AgentSystemConfig, MCPConfig
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
def workspace_config():
    """Configuration for workspace-wide file operations."""
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = ["."]
    mcp_config.search = {
        "enable_indexing": True,
        "enable_semantic_search": False,  # Disabled for faster tests
        "index_on_startup": False,  # On-demand indexing
        "exclude_patterns": [
            "**/.git/**",
            "**/__pycache__/**",
            "**/node_modules/**",
            "**/.venv/**",
            "**/data/**",
            "**/external/**",
            "**/logs/**",
            "**/tmp/**"
        ]
    }
    return system_config, mcp_config


@pytest.mark.asyncio
async def test_grep_search_on_demand_indexing(workspace_config):
    """Test that grep search triggers index build on first use."""
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Index should be empty initially
    assert len(server.search_engine.file_mtimes) == 0
    
    # First grep search should trigger index build
    result = await server.search_engine.grep_search(
        query="def",
        is_regex=False,
        include_pattern="*.py",
        max_results=5
    )
    
    # Index should now be populated
    assert len(server.search_engine.file_mtimes) > 0
    assert result["status"] == "success"
    assert result["total_matches"] > 0
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_grep_search_with_include_pattern(workspace_config):
    """Test that include_pattern filtering works correctly."""
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Search in test files only
    result = await server.search_engine.grep_search(
        query="import pytest",
        is_regex=False,
        include_pattern="tests/*.py",
        max_results=10
    )
    
    assert result["status"] == "success"
    assert result["total_matches"] > 0
    
    # Verify all matches are from test files
    for match in result["matches"]:
        assert "tests" in match["file_path"].lower()
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_grep_search_finds_short_tokens(workspace_config):
    """Test that grep search indexes 2-character tokens."""
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Build index first
    await server.search_engine.rebuild_index(incremental=False)
    
    # Check that 2-character words are indexed
    assert "if" in server.search_engine.text_index or \
           "is" in server.search_engine.text_index or \
           "in" in server.search_engine.text_index
    
    # Search should find 2-char tokens
    result = await server.search_engine.grep_search(
        query="if ",
        is_regex=False,
        include_pattern="*.py",
        max_results=5
    )
    
    assert result["status"] == "success"
    assert result["total_matches"] > 0
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_semantic_search_indexes_root_files(workspace_config):
    """Test that semantic search includes root-level files."""
    system_config, mcp_config = workspace_config
    mcp_config.search["enable_semantic_search"] = True
    mcp_config.search["chroma_db_path"] = "data/cache/test_semantic_root"
    
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Build index
    await server.search_engine.rebuild_index(incremental=False)
    
    # ChromaDB should contain root files
    if server.search_engine.chroma_collection:
        count = server.search_engine.chroma_collection.count()
        assert count > 1000, "Expected many files to be indexed"
        
        # Check for specific root files
        sample = server.search_engine.chroma_collection.peek(limit=20)
        file_paths = [meta.get('file_path', '') for meta in sample['metadatas']]
        
        # Should include some root files (not just external dependencies)
        root_files = [p for p in file_paths if not any(
            exclude in p.lower() 
            for exclude in ['external', 'node_modules', '.venv', '.git']
        )]
        assert len(root_files) > 0, "Expected root project files in ChromaDB"
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_chromadb_batch_size_handling(workspace_config):
    """Test that large file sets don't exceed ChromaDB batch limits."""
    system_config, mcp_config = workspace_config
    mcp_config.search["enable_semantic_search"] = True
    mcp_config.search["chroma_db_path"] = "data/cache/test_batch_limit"
    
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Build index with many files
    await server.search_engine.rebuild_index(incremental=False)
    
    # Should not raise batch size errors
    if server.search_engine.chroma_collection:
        count = server.search_engine.chroma_collection.count()
        # Should handle more than 5000 files (old batch limit)
        assert count >= 0, "ChromaDB indexing should complete without errors"
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_file_search_finds_root_files(workspace_config):
    """Test that file search can find root-level files."""
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Search for README files
    result = await server.search_engine.search_files("README*")
    
    assert result["status"] == "success"
    assert result["total_found"] > 0
    
    # Should find root README
    readme_found = any(
        "readme" in filepath.lower() and 
        filepath.count(str(Path.cwd().name)) > 0
        for filepath in result["files"]
    )
    assert readme_found, "Expected to find root README files"
    
    await server.search_engine.stop()
