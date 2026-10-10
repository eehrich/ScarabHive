"""
Vector Store Utility - Unified Vector Database Interface

Provides a consistent API for vector similarity search across different backends:
- ChromaDB: Default on Linux/macOS (uses native Rust bindings)
- sqlite-vec: Fallback on Windows (ChromaDB Rust bindings crash - issue #5937)

Usage:
    from agent_system.utils.vector_store import VectorStore, get_vector_backend

    # Auto-detect best backend
    store = VectorStore(persist_path="data/cache/my_vectors")

    # Add documents
    store.add("collection_name", ids=["doc1"], documents=["Hello world"])

    # Query similar documents
    results = store.query("collection_name", query_text="Hi there", n_results=5)

    # Delete documents
    store.delete("collection_name", ids=["doc1"])

The package, by what each module holds:
- ``embeddings``: the embedding model of the process (all-MiniLM-L6-v2 on
  chromadb's ONNX export) and the helpers that embed without a store
- ``base``: backend detection (`get_vector_backend`), the backend contract
  (`VectorBackend`) and `VectorStoreError`
- ``chroma``: the ChromaDB backend and its cross-process guards
- ``sqlite_vec``: the sqlite-vec backend
- ``store``: `VectorStore`, which hands every operation to one of the two

A test that replaces a name replaces it where it is looked up: the model in
``embeddings``, chroma's embedding calls in ``chroma``, `get_vector_backend`
for a new store in ``store``.
"""

from .base import VectorStoreError, get_vector_backend
from .embeddings import (  # noqa: F401 - _load_embedding_model: imported from here outside this repository
    EMBEDDING_DIM,
    _load_embedding_model,
    compute_embedding,
    compute_embeddings,
    cosine_similarity,
    get_available_onnx_providers,
    get_embedding_model,
    texts_are_duplicate,
)
from .store import VectorStore, create_vector_store

__all__ = [
    "EMBEDDING_DIM",
    "VectorStore",
    "VectorStoreError",
    "compute_embedding",
    "compute_embeddings",
    "cosine_similarity",
    "create_vector_store",
    "get_available_onnx_providers",
    "get_embedding_model",
    "get_vector_backend",
    "texts_are_duplicate",
]
