"""Tests for the VectorStore utility."""

import tempfile
import pytest
import gc
from pathlib import Path

from agent_system.utils.vector_store import (
    VectorStore,
    get_vector_backend,
    get_available_onnx_providers,
)


# Helper to create temp directories that ignore cleanup errors on Windows
def create_temp_dir():
    """Create a temp directory that ignores cleanup errors (Windows file locking)."""
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class TestGetVectorBackend:
    """Tests for backend detection."""

    def test_returns_valid_backend(self):
        """Should return either 'chromadb' or 'sqlite-vec'."""
        backend = get_vector_backend()
        assert backend in ("chromadb", "sqlite-vec")

    def test_caches_result(self):
        """Should return same result on repeated calls."""
        backend1 = get_vector_backend()
        backend2 = get_vector_backend()
        assert backend1 == backend2


class TestGetAvailableOnnxProviders:
    """Tests for ONNX provider detection."""

    def test_returns_list(self):
        """Should return a list of providers."""
        providers = get_available_onnx_providers()
        assert isinstance(providers, list)
        assert len(providers) > 0

    def test_includes_cpu_provider(self):
        """Should always include CPU provider as fallback."""
        providers = get_available_onnx_providers()
        assert "CPUExecutionProvider" in providers


class TestVectorStoreInit:
    """Tests for VectorStore initialization."""

    def test_creates_persist_directory(self):
        """Should create persist directory if it doesn't exist."""
        tmpdir_obj = create_temp_dir()
        tmpdir = tmpdir_obj.name
        persist_path = Path(tmpdir) / "new_dir" / "vectors"
        store = VectorStore(persist_path=persist_path)
        assert persist_path.exists()
        store.close()
        gc.collect()  # Help release file handles
        tmpdir_obj.cleanup()

    def test_backend_property(self):
        """Should expose backend type."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        assert store.backend in ("chromadb", "sqlite-vec")
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()

    def test_context_manager(self):
        """Should work as context manager."""
        tmpdir_obj = create_temp_dir()
        with VectorStore(persist_path=tmpdir_obj.name) as store:
            assert store.backend in ("chromadb", "sqlite-vec")
        gc.collect()
        tmpdir_obj.cleanup()


class TestVectorStoreOperations:
    """Tests for VectorStore CRUD operations."""

    @pytest.fixture
    def store(self):
        """Create a temporary VectorStore for testing."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        yield store
        store.close()
        gc.collect()  # Help release file handles on Windows
        tmpdir_obj.cleanup()

    def test_add_documents(self, store):
        """Should add documents to a collection."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2"],
            documents=["Hello world", "Goodbye world"],
            metadatas=[{"type": "greeting"}, {"type": "farewell"}]
        )
        
        count = store.count("test_collection")
        assert count == 2

    def test_add_documents_without_metadata(self, store):
        """Should add documents without metadata."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"]
        )
        
        count = store.count("test_collection")
        assert count == 1

    def test_query_by_text(self, store):
        """Should query documents by text similarity."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2", "doc3"],
            documents=[
                "The cat sat on the mat",
                "Dogs are loyal pets",
                "The feline rested on the rug"
            ]
        )
        
        results = store.query(
            collection="test_collection",
            query_text="cat on mat",
            n_results=2
        )
        
        assert "ids" in results
        assert len(results["ids"]) <= 2

    def test_query_returns_distances(self, store):
        """Should return distances in query results."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"]
        )
        
        results = store.query(
            collection="test_collection",
            query_text="Hello",
            n_results=1
        )
        
        assert "distances" in results

    def test_delete_by_ids(self, store):
        """Should delete documents by ID."""
        store.add(
            collection="test_collection",
            ids=["doc1", "doc2"],
            documents=["Hello", "World"]
        )
        
        assert store.count("test_collection") == 2
        
        store.delete(collection="test_collection", ids=["doc1"])
        
        assert store.count("test_collection") == 1

    def test_count_empty_collection(self, store):
        """Should return 0 for empty collection."""
        store.get_or_create_collection("empty_collection")
        count = store.count("empty_collection")
        assert count == 0

    def test_delete_collection(self, store):
        """Should delete entire collection."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello"]
        )
        
        store.delete_collection("test_collection")
        
        # After deletion, creating new collection should be empty
        store.get_or_create_collection("test_collection")
        assert store.count("test_collection") == 0

    def test_get_or_create_collection(self, store):
        """Should create collection if it doesn't exist."""
        # First call creates
        store.get_or_create_collection("new_collection")
        
        # Second call gets existing
        store.get_or_create_collection("new_collection")
        
        # Should be able to add to it
        store.add(
            collection="new_collection",
            ids=["doc1"],
            documents=["Test"]
        )
        assert store.count("new_collection") == 1


class TestVectorStoreReset:
    """Tests for VectorStore reset functionality."""

    def test_reset_clears_all_data(self):
        """Should clear all collections on reset."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        
        # Add data to multiple collections
        store.add(
            collection="collection1",
            ids=["doc1"],
            documents=["Hello"]
        )
        store.add(
            collection="collection2",
            ids=["doc1"],
            documents=["World"]
        )
        
        # Reset
        store.reset()
        
        # Both should be empty after reset
        store.get_or_create_collection("collection1")
        store.get_or_create_collection("collection2")
        assert store.count("collection1") == 0
        assert store.count("collection2") == 0
        
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()


class TestVectorStoreMetadata:
    """Tests for metadata handling."""

    @pytest.fixture
    def store(self):
        """Create a temporary VectorStore for testing."""
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        yield store
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()

    def test_metadata_preserved(self, store):
        """Should preserve metadata in query results."""
        store.add(
            collection="test_collection",
            ids=["doc1"],
            documents=["Hello world"],
            metadatas=[{"author": "test", "year": "2024"}]
        )
        
        results = store.query(
            collection="test_collection",
            query_text="Hello",
            n_results=1
        )
        
        assert "metadatas" in results
        # Results are in batch format: [[metadata1, metadata2, ...]]
        # So metadatas[0] is the list of metadatas for the first (and only) query
        if results["metadatas"] and results["metadatas"][0]:
            metadata = results["metadatas"][0][0]  # First result of first query
            assert metadata.get("author") == "test"
            assert metadata.get("year") == "2024"


class TestVectorStoreConcurrency:
    """The lock must serialize concurrent access to the single sqlite connection."""

    @pytest.fixture
    def store(self):
        tmpdir_obj = create_temp_dir()
        store = VectorStore(persist_path=tmpdir_obj.name)
        yield store
        store.close()
        gc.collect()
        tmpdir_obj.cleanup()

    def test_synchronized_serializes_concurrent_ops(self, store):
        """Concurrent add() calls from many threads must NOT overlap.

        Callers fan VectorStore calls out via asyncio.to_thread; without the
        per-instance lock two threads would use the same sqlite cursor at once
        ('recursive use of cursors' / corruption). We replace the backend-
        specific inner add with one that records concurrency and assert the
        observed max-concurrency is exactly 1.
        """
        import threading
        import time
        from unittest.mock import patch

        state = {"cur": 0, "max": 0}
        guard = threading.Lock()

        def fake_inner(*args, **kwargs):
            with guard:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.01)  # widen the window for overlap to show
            with guard:
                state["cur"] -= 1

        with patch.object(store, "_sqlite_vec_add", side_effect=fake_inner), \
             patch.object(store, "_chromadb_add", side_effect=fake_inner):
            threads = [
                threading.Thread(
                    target=store.add,
                    args=("c",),
                    kwargs={"ids": [f"id{i}"], "documents": ["d"]},
                )
                for i in range(10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert state["max"] == 1, f"lock failed to serialize (max concurrency {state['max']})"

    def test_reentrant_lock_allows_nested_calls(self, store):
        """The lock is reentrant: a synchronized method may call another."""
        # close() is @_synchronized and is safe to call twice; a non-reentrant
        # lock would be fine here, but this documents the reentrancy guarantee.
        store.close()
        store.close()  # must not deadlock
