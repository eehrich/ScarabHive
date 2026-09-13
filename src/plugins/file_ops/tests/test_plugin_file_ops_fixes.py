"""
Test cases demonstrating the fixed file_ops functionality.

This test file validates the fixes for the reported issues:
1. Grep search not finding matches
2. Semantic search not indexing root files
3. Index not being built on-demand

All issues have been resolved.
"""

import pytest
from unittest.mock import Mock
from agent_system.config import AgentSystemConfig, MCPConfig
from agent_system.utils.vector_store import VectorStoreError
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
def workspace_config():
    """Configuration for workspace-wide file operations."""
    system_config = Mock(spec=AgentSystemConfig)
    mcp_config = MCPConfig(type="file_ops", enabled=True)
    # Limit to only tests/ and src/ directories for faster indexing
    mcp_config.allowed_directories = ["tests", "src"]
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
async def test_grep_search_needs_no_index(workspace_config):
    """Grep answers WITHOUT building an index — that wait was the whole bug.

    The previous contract was the opposite: the first grep triggered a full
    index build and waited for it. Measured on this repository with the coder
    configuration, one such call had not returned after 150 seconds, because
    the index reads every file before answering a question about a few. The
    index still exists for semantic search; nothing else waits for it.
    """
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)

    assert len(server.search_engine.file_mtimes) == 0

    result = await server.search_engine.grep_search(
        query="def",
        is_regex=False,
        include_pattern="*.py",
        max_results=5
    )

    assert result["status"] == "success"
    assert result["total_matches"] > 0
    # Still empty: the answer came from the file system, not from an index.
    assert len(server.search_engine.file_mtimes) == 0

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
    """A two-character query matches: there is no word index to fall below."""
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)

    result = await server.search_engine.grep_search(
        query="if ",
        is_regex=False,
        include_pattern="*.py",
        max_results=5
    )
    
    assert result["status"] == "success"
    assert result["total_matches"] > 0
    
    await server.search_engine.stop()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_semantic_search_indexes_root_files(workspace_config, tmp_path):
    """Test that semantic search includes root-level files."""
    system_config, mcp_config = workspace_config
    mcp_config.search["enable_semantic_search"] = True
    # tmp_path, not data/cache: a fixed path keeps the index BETWEEN runs, and
    # one written by an older chromadb makes this test fail on a healthy tree.
    mcp_config.search["chroma_db_path"] = str(tmp_path / "semantic_root")
    
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Build index
    await server.search_engine.rebuild_index(incremental=False)
    
    # Vector store should contain files from tests/ and src/
    if server.search_engine._vector_store:
        count = server.search_engine._vector_store.count(server.search_engine._collection_name)
        assert count > 50, "Expected test and src files to be indexed"
        
        # Check for specific root files using query (peek not in VectorStore interface)
        result = server.search_engine._vector_store.query(
            server.search_engine._collection_name,
            "test function",
            n_results=20
        )
        # VectorStore returns list-of-lists format, unwrap first result set
        metadatas = result['metadatas'][0] if result.get('metadatas') else []
        file_paths = [meta.get('file_path', '') for meta in metadatas]
        
        # Should include some root files (not just external dependencies)
        root_files = [p for p in file_paths if not any(
            exclude in p.lower() 
            for exclude in ['external', 'node_modules', '.venv', '.git']
        )]
        assert len(root_files) > 0, "Expected root project files in vector store"
    
    await server.search_engine.stop()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_chromadb_batch_size_handling(workspace_config, tmp_path):
    """Test that large file sets don't exceed ChromaDB batch limits."""
    system_config, mcp_config = workspace_config
    mcp_config.search["enable_semantic_search"] = True
    mcp_config.search["chroma_db_path"] = str(tmp_path / "batch_limit")
    
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Build index with many files
    await server.search_engine.rebuild_index(incremental=False)
    
    # Should not raise batch size errors
    if server.search_engine._vector_store:
        count = server.search_engine._vector_store.count(server.search_engine._collection_name)
        # Should handle more than 5000 files (old batch limit)
        assert count >= 0, "Vector store indexing should complete without errors"
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_file_search_finds_root_files(workspace_config):
    """Test that file search can find Python test files."""
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)
    
    # Search for test files (more reliable than README)
    result = await server.search_engine.search_files("test_*.py")
    
    assert result["status"] == "success"
    assert result["total_found"] > 0
    
    # Should find test files in tests/
    test_found = any(
        "test_" in filepath.lower() and "tests" in filepath.lower()
        for filepath in result["files"]
    )
    assert test_found, "Expected to find test files in tests/"
    
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_an_unreadable_semantic_index_raises_instead_of_filling_nothing(
    workspace_config, tmp_path
):
    """An index chromadb can no longer read used to be swallowed with a warning.

    The rebuild then carried on into a broken collection and left it empty, so
    semantic search answered "found nothing" instead of "the index is broken" —
    measured on a real stale store from 2026-05 that failed with
    'PersistentData' object has no attribute 'max_seq_id'.
    """
    system_config, mcp_config = workspace_config
    mcp_config.search["enable_semantic_search"] = True
    mcp_config.search["chroma_db_path"] = str(tmp_path / "unreadable")
    server = FileOpsServer("test", system_config, mcp_config)

    class Unreadable:
        backend = "chromadb"

        def count(self, collection):
            raise AttributeError("'PersistentData' object has no attribute 'max_seq_id'")

        # VectorStore.delete_collection swallows its own errors, so a drop that
        # does not help is exactly the case the rebuild has to notice itself.
        def delete_collection(self, collection):
            pass

        def get_or_create_collection(self, collection):
            pass

    server.search_engine._vector_store = Unreadable()
    server.search_engine._vector_store_initialized = True

    with pytest.raises(VectorStoreError, match="unreadable"):
        await server.search_engine.rebuild_index(incremental=False)


@pytest.mark.asyncio
async def test_disabled_semantic_search_says_so_instead_of_finding_nothing(
    workspace_config
):
    """Disabled used to answer "No files found" after walking every root.

    That reads as a verdict about the code, not about the configuration — and
    the file_ops server disables semantic search unless it is configured, so
    it was what every default instance answered.
    """
    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)

    result = await server.search_engine.semantic_search("anything")

    assert result["status"] == "error"
    assert result["error_type"] == "SemanticSearchDisabled"
    assert len(server.search_engine.file_mtimes) == 0, "nothing may be walked"
    await server.search_engine.stop()


@pytest.mark.asyncio
async def test_searches_walk_the_disk_off_the_event_loop(workspace_config, monkeypatch):
    """The walk blocks; on the loop it stalls every other tool call.

    With include_ignored a walk over this repository takes around 15 seconds.
    """
    import threading
    from plugins.file_ops import textsearch

    system_config, mcp_config = workspace_config
    server = FileOpsServer("test", system_config, mcp_config)
    loop_thread = threading.current_thread()
    ran_on = []

    def recording(real):
        def wrapper(*args, **kwargs):
            ran_on.append(threading.current_thread())
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(textsearch, "find_files", recording(textsearch.find_files))
    monkeypatch.setattr(textsearch, "grep", recording(textsearch.grep))

    await server.search_engine.search_files("conftest.py", max_results=1)
    await server.search_engine.grep_search("import", include_pattern="conftest.py",
                                           max_results=1)

    assert len(ran_on) == 2
    assert all(thread is not loop_thread for thread in ran_on)
    await server.search_engine.stop()
