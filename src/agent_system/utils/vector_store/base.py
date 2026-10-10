"""What a vector backend is, and which one this process uses.

`get_vector_backend` picks the backend by what is installed, `VectorBackend` is
the contract `VectorStore` delegates every operation to, and `VectorStoreError`
is what a backend raises for a request it cannot serve. Its own module because
both backends and the store build on it, and the store imports the backends.
"""

import logging
import platform
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Cached backend detection
_VECTOR_BACKEND: Optional[str] = None


def get_vector_backend() -> str:
    """Detect which vector backend to use.
    
    Returns:
        'chromadb' (preferred, uses SegmentAPI workaround on Windows)
        'sqlite-vec' as fallback
        
    Note: On Windows, ChromaDB's Rust bindings hang/crash (GitHub issue #5937).
    We use chroma_api_impl="chromadb.api.segment.SegmentAPI" to bypass this.
    """
    global _VECTOR_BACKEND
    if _VECTOR_BACKEND is not None:
        return _VECTOR_BACKEND
    
    # Try ChromaDB first (works on all platforms with SegmentAPI workaround)
    import importlib.util
    if importlib.util.find_spec("chromadb") is not None:
        _VECTOR_BACKEND = "chromadb"
        if platform.system() == "Windows":
            logger.info("Vector backend: chromadb (SegmentAPI mode for Windows)")
        else:
            logger.info("Vector backend: chromadb")
        return _VECTOR_BACKEND
    
    # Fallback to sqlite-vec
    if importlib.util.find_spec("sqlite_vec") is not None:
        _VECTOR_BACKEND = "sqlite-vec"
        logger.info("Vector backend: sqlite-vec (fallback)")
        return _VECTOR_BACKEND
    
    raise RuntimeError(
        "No vector backend available. Install one of:\n"
        "  pip install chromadb  (recommended)\n"
        "  pip install sqlite-vec sentence-transformers  (fallback: without chromadb,\n"
        "  the embedding model runs on sentence-transformers)"
    )


class VectorStoreError(Exception):
    """Vector store operation errors."""
    pass


class VectorBackend(ABC):
    """One engine behind `VectorStore`, for one persist path.

    `VectorStore` creates one per instance and calls it only under the path's
    lock (`_synchronized`): a backend is never used from two threads at once
    and takes no lock of its own for that. The answers have one shape across
    backends -- a query returns ChromaDB's list-of-lists, a missing collection
    lists as empty -- so no caller needs to know which one runs. The arguments
    and their meaning are documented on the `VectorStore` methods of the same
    name.
    """

    #: The name `get_vector_backend` reports for this backend.
    name: str = ""

    @abstractmethod
    def add(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]],
        embeddings: Optional[List[List[float]]],
        metadatas: Optional[List[Dict[str, Any]]],
    ) -> None:
        """Upsert *ids*; documents without a vector are embedded first."""

    @abstractmethod
    def query(
        self,
        collection: str,
        query_text: Optional[str],
        query_embedding: Optional[List[float]],
        n_results: int,
        where: Optional[Dict[str, Any]],
        include: Optional[List[str]],
    ) -> Dict[str, Any]:
        """The nearest entries; *where* and *include* only where the backend supports them."""

    @abstractmethod
    def delete(
        self,
        collection: str,
        ids: Optional[List[str]],
        where: Optional[Dict[str, Any]],
    ) -> None:
        """Remove *ids*, or what *where* matches where the backend supports it."""

    @abstractmethod
    def get_or_create_collection(self, name: str) -> Any:
        """The collection's handle, created when missing; None where there is no handle."""

    def refresh_collection(self, name: str) -> Any:
        """Fetch *name* again: a backend that caches no handle has nothing to drop first."""
        return self.get_or_create_collection(name)

    @abstractmethod
    def list_ids(self, collection: str) -> List[str]:
        """Every id in the collection; empty when it does not exist."""

    @abstractmethod
    def list_entries(self, collection: str) -> List[Dict[str, Any]]:
        """Every id with its metadata; empty when the collection does not exist."""

    @abstractmethod
    def count(self, collection: str) -> int:
        """The number of entries in the collection."""

    @abstractmethod
    def delete_collection(self, name: str) -> None:
        """Drop the collection; a failure is logged, not raised."""

    @abstractmethod
    def reset(self) -> None:
        """Drop every collection of the store."""

    @abstractmethod
    def close(self) -> None:
        """Release what this instance holds; what the path shares is the store's to release."""
