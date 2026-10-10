"""`VectorStore`: one API over the two vector backends, safe to call from any thread.

The store holds what is the same whichever backend runs -- the persist path,
the path's access and the lock that comes with it (`_synchronized`), closing --
and hands every operation to the backend `get_vector_backend` chose:
`ChromaBackend` (chroma.py) or `SqliteVecBackend` (sqlite_vec.py). A backend
object rather than a subclass per backend because callers construct
`VectorStore` itself and the choice is made at run time, and because the lock
and the release of the path's access must be one implementation for both.
"""

import functools
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .base import VectorBackend, get_vector_backend
from .chroma import ChromaBackend, _acquire_access, _release_access
from .sqlite_vec import SqliteVecBackend

logger = logging.getLogger(__name__)


def _synchronized(method):
    """Serialize a VectorStore method on the instance's reentrant lock.

    The bug this guards against: callers fan operations out via
    asyncio.to_thread, so the single sqlite3.Connection / shared chroma dict
    would be used from multiple threads concurrently. See VectorStore.__init__.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class VectorStore:
    """Unified vector store interface supporting multiple backends.
    
    Selects the backend by what is installed (get_vector_backend):
    - ChromaDB wherever it is (on Windows through its SegmentAPI)
    - sqlite-vec without it

    Args:
        persist_path: Directory path for persistent storage
        embedding_model: Embedding model of the sqlite-vec backend (default: all-MiniLM-L6-v2);
            without chromadb it runs on sentence-transformers
        
    Example:
        store = VectorStore("data/cache/vectors")
        store.add("docs", ids=["1"], documents=["Hello world"])
        results = store.query("docs", "Hi there", n_results=5)
    """
    
    def __init__(
        self,
        persist_path: Union[str, Path],
        embedding_model: str = "all-MiniLM-L6-v2"
    ):
        self.persist_path = Path(persist_path)
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self.embedding_model_name = embedding_model
        
        # Detect backend
        backend = get_vector_backend()

        # The path's access, shared with every other VectorStore of this
        # process on the same directory (see _ChromaAccess): the lock below is
        # its lock, and a chroma backend works through its client.
        self._access, self._access_key = _acquire_access(self.persist_path)
        self._released = False
        self._backend: VectorBackend
        if backend == "chromadb":
            self._backend = ChromaBackend(self.persist_path, self._access)
        else:
            self._backend = SqliteVecBackend(self.persist_path, embedding_model)

        # CONCURRENCY: callers (memory / lessons_learned / context_engineer)
        # fan VectorStore calls out via asyncio.to_thread, so two operations can
        # hit the SINGLE sqlite3.Connection (check_same_thread=False) and the
        # shared chroma-collection dict from different threads at once -> "recursive
        # use of cursors", interleaved writes, corrupted reads. A reentrant lock
        # serializes every public operation per instance (reentrant so methods
        # that call other locked methods don't deadlock). Held for the whole op,
        # embedding included -- which needs no lock of its own: the model is one
        # per process, shared by every store, and runs from any thread (_OnnxMiniLM).
        # The lock is the PATH's: an instance that replaces the shared client
        # must not do it under another instance's running query.
        self._lock = self._access.lock

        logger.info(f"VectorStore initialized: path={persist_path}, backend={backend}")
    
    @_synchronized
    def close(self) -> None:
        """Close connections and release resources."""
        self._backend.close()
        if not self._released:
            self._released = True
            if _release_access(self._access, self._access_key):
                self._access.close_client()
        logger.debug("VectorStore closed")

    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - closes connections."""
        self.close()
        return False
    
    @property
    def backend(self) -> str:
        """Return the current backend name."""
        return self._backend.name
    
    # ===== Public API =====
    
    @_synchronized
    def add(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]] = None,
        embeddings: Optional[List[List[float]]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Add documents to a collection.
        
        Args:
            collection: Name of the collection
            ids: Unique IDs for each document
            documents: Text documents (will be embedded if embeddings not provided)
            embeddings: Pre-computed embeddings (optional)
            metadatas: Metadata dicts for each document (optional)
        """
        self._backend.add(collection, ids, documents, embeddings, metadatas)
    
    @_synchronized
    def query(
        self,
        collection: str,
        query_text: Optional[str] = None,
        query_embedding: Optional[List[float]] = None,
        n_results: int = 10,
        where: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Query collection for similar documents.
        
        Args:
            collection: Name of the collection
            query_text: Text to search for (will be embedded)
            query_embedding: Pre-computed query embedding (optional)
            n_results: Number of results to return
            where: Metadata filter (ChromaDB only)
            include: What to include in results (ChromaDB only)
            
        Returns:
            Dict with 'ids', 'distances', 'documents', 'metadatas' keys
        """
        return self._backend.query(collection, query_text, query_embedding, n_results, where, include)
    
    @_synchronized
    def delete(
        self,
        collection: str,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None
    ) -> None:
        """Delete documents from a collection.
        
        Args:
            collection: Name of the collection
            ids: IDs to delete
            where: Metadata filter for deletion (ChromaDB only)
        """
        self._backend.delete(collection, ids, where)
    
    @_synchronized
    def get_or_create_collection(self, name: str) -> Any:
        """Get or create a collection by name.
        
        For ChromaDB, returns the collection object.
        For sqlite-vec, initializes the table and returns None.
        """
        return self._backend.get_or_create_collection(name)
    
    @_synchronized
    def refresh_collection(self, name: str) -> Any:
        """Drop the cached handle for *name* and fetch it again.

        Two VectorStore instances can point at the same collection (a
        read-write and a read-only file_ops twin share one index). Each caches
        its own handle, so when the owner drops and recreates the collection --
        which every full rebuild does -- the other one keeps a handle to
        something that no longer exists, and every call through it fails. This
        is how the other one recovers without restarting the process.
        """
        return self._backend.refresh_collection(name)

    @_synchronized
    def list_ids(self, collection: str) -> List[str]:
        """All IDs of a collection.

        Needed for incremental indexing: whoever wants to know WHAT is
        missing needs the set of what exists — ``count()`` alone
        only says how many. Without this method every caller had to
        either re-index everything or reach into the storage
        behind the backend's back.

        If the collection does not exist, the answer is an empty list
        (no error): "nothing indexed yet" is a valid state,
        not an exceptional case.
        """
        return self._backend.list_ids(collection)

    @_synchronized
    def list_entries(self, collection: str) -> List[Dict[str, Any]]:
        """All ids of a collection together with the metadata stored with them.

        ``list_ids`` answers WHAT is indexed, never in which state. Deciding
        whether an entry is still current — a content hash, a version — needs
        the metadata back, and reading it through the store keeps that
        decision off the two backends: ChromaDB returns it from ``get``,
        sqlite-vec keeps it as JSON in ``meta_<collection>``.

        Returns one dict per entry with ``id`` and ``metadata`` (an empty dict
        when none was stored). A missing collection yields an empty list, just
        as in ``list_ids``: "nothing indexed yet" is a state, not an error.
        """
        return self._backend.list_entries(collection)

    @_synchronized
    def count(self, collection: str) -> int:
        """Get the number of documents in a collection."""
        return self._backend.count(collection)
    
    @_synchronized
    def delete_collection(self, name: str) -> None:
        """Delete an entire collection."""
        self._backend.delete_collection(name)
    
    @_synchronized
    def reset(self) -> None:
        """Reset/clear all data (for testing)."""
        self._backend.reset()


# Convenience function for quick access
def create_vector_store(persist_path: Union[str, Path], **kwargs) -> VectorStore:
    """Create a VectorStore with the detected backend.
    
    Args:
        persist_path: Directory for persistent storage
        **kwargs: Additional arguments passed to VectorStore
        
    Returns:
        Configured VectorStore instance
    """
    return VectorStore(persist_path, **kwargs)
