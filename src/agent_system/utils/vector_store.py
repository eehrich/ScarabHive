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
"""

import functools
import logging
import math
import platform
import struct
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Vector dimension for MiniLM-L6-v2 (default embedding model)
EMBEDDING_DIM = 384

# Cached backend detection
_VECTOR_BACKEND: Optional[str] = None
_ONNX_PROVIDERS: Optional[List[str]] = None


def get_available_onnx_providers() -> List[str]:
    """Detect available ONNX Runtime execution providers.
    
    Returns providers in priority order: GPU first, then CPU fallback.
    Result is cached for performance.
    """
    global _ONNX_PROVIDERS
    if _ONNX_PROVIDERS is not None:
        return _ONNX_PROVIDERS

    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
        logger.debug(f"Available ONNX providers: {available}")
        
        # Prefer GPU providers, fallback to CPU
        preferred_order = [
            "CUDAExecutionProvider",
            "ROCMExecutionProvider", 
            "DmlExecutionProvider",  # DirectML for Windows
            "CoreMLExecutionProvider",  # Apple Silicon
            "AzureExecutionProvider",
            "CPUExecutionProvider"
        ]
        
        # Filter to only available providers, maintaining priority order
        _ONNX_PROVIDERS = [p for p in preferred_order if p in available]
        
        # If none of our preferred are available, use whatever is available
        if not _ONNX_PROVIDERS:
            _ONNX_PROVIDERS = available if available else ["CPUExecutionProvider"]
        
        logger.info(f"Selected ONNX providers: {_ONNX_PROVIDERS}")
        return _ONNX_PROVIDERS
        
    except ImportError:
        logger.warning("onnxruntime not installed, using CPU provider only")
        _ONNX_PROVIDERS = ["CPUExecutionProvider"]
        return _ONNX_PROVIDERS
    except Exception as e:
        logger.warning(f"Failed to detect ONNX providers: {e}, using CPU")
        _ONNX_PROVIDERS = ["CPUExecutionProvider"]
        return _ONNX_PROVIDERS


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
        "  pip install sqlite-vec  (fallback)"
    )


class VectorStoreError(Exception):
    """Vector store operation errors."""
    pass


# ---------------------------------------------------------------------------
# Standalone embedding utilities (no VectorStore instance required)
# ---------------------------------------------------------------------------
_embedding_models: dict = {}


def _load_sentence_transformer(model_name: str):
    """Load a SentenceTransformer model, using local cache when available.

    Models are cached per name for the lifetime of the process.
    """
    if model_name not in _embedding_models:
        from sentence_transformers import SentenceTransformer
        try:
            _embedding_models[model_name] = SentenceTransformer(
                model_name, device="cpu", local_files_only=True,
            )
        except OSError:
            _embedding_models[model_name] = SentenceTransformer(
                model_name, device="cpu",
            )
        logger.info(f"SentenceTransformer loaded ({model_name})")
    return _embedding_models[model_name]


def get_embedding_model():
    """Lazy-load and cache the default SentenceTransformer (all-MiniLM-L6-v2)."""
    return _load_sentence_transformer("all-MiniLM-L6-v2")


def compute_embedding(text: str) -> List[float]:
    """Return 384-dim embedding for *text*."""
    model = get_embedding_model()
    return model.encode(text, convert_to_numpy=True).tolist()


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def texts_are_duplicate(
    text_a: str,
    text_b: str,
    threshold: float = 0.85,
) -> tuple[bool, float]:
    """Check whether two texts are near-duplicates via embedding similarity.

    Returns ``(is_duplicate, similarity_score)``.

    Typical cosine ranges for German prose:
    - Unrelated scenes: 0.30 – 0.50
    - Same setting, advancing plot: 0.50 – 0.70
    - True continuation (same characters, different events): 0.70 – 0.85
    - Near-identical / duplicate content: > 0.85
    """
    if not text_a.strip() or not text_b.strip():
        return False, 0.0
    emb_a = compute_embedding(text_a)
    emb_b = compute_embedding(text_b)
    sim = cosine_similarity(emb_a, emb_b)
    return sim >= threshold, sim


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
    
    Automatically selects the best backend for the platform:
    - ChromaDB on Linux/macOS
    - sqlite-vec on Windows (due to ChromaDB Rust bindings issues)
    
    Args:
        persist_path: Directory path for persistent storage
        embedding_model: Name of sentence-transformers model (default: all-MiniLM-L6-v2)
        
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
        self._backend = get_vector_backend()
        
        # ChromaDB state
        self._chroma_client = None
        self._chroma_collections: Dict[str, Any] = {}
        self._chroma_embedding_fn = None
        
        # sqlite-vec state
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._sqlite_vec_initialized = False
        self._sentence_transformer = None

        # CONCURRENCY: callers (memory / lessons_learned / context_engineer)
        # fan VectorStore calls out via asyncio.to_thread, so two operations can
        # hit the SINGLE sqlite3.Connection (check_same_thread=False) and the
        # shared chroma-collection dict from different threads at once -> "recursive
        # use of cursors", interleaved writes, corrupted reads. A reentrant lock
        # serializes every public operation per instance (reentrant so methods
        # that call other locked methods don't deadlock). Held for the whole op
        # incl. embedding, since the embedding model isn't guaranteed thread-safe
        # either - correctness over parallel embedding.
        self._lock = threading.RLock()

        logger.info(f"VectorStore initialized: path={persist_path}, backend={self._backend}")
    
    @_synchronized
    def close(self) -> None:
        """Close connections and release resources."""
        if self._sqlite_conn is not None:
            try:
                self._sqlite_conn.close()
            except Exception:
                pass
            self._sqlite_conn = None
        self._chroma_client = None
        self._chroma_collections.clear()
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
        return self._backend
    
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
        if self._backend == "chromadb":
            self._chromadb_add(collection, ids, documents, embeddings, metadatas)
        else:
            self._sqlite_vec_add(collection, ids, documents, embeddings, metadatas)
    
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
        if self._backend == "chromadb":
            return self._chromadb_query(collection, query_text, query_embedding, n_results, where, include)
        else:
            return self._sqlite_vec_query(collection, query_text, query_embedding, n_results)
    
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
        if self._backend == "chromadb":
            self._chromadb_delete(collection, ids, where)
        else:
            self._sqlite_vec_delete(collection, ids)
    
    @_synchronized
    def get_or_create_collection(self, name: str) -> Any:
        """Get or create a collection by name.
        
        For ChromaDB, returns the collection object.
        For sqlite-vec, initializes the table and returns None.
        """
        if self._backend == "chromadb":
            return self._get_chromadb_collection(name)
        else:
            self._ensure_sqlite_vec_table(name)
            return None
    
    @_synchronized
    def count(self, collection: str) -> int:
        """Get the number of documents in a collection."""
        if self._backend == "chromadb":
            coll = self._get_chromadb_collection(collection)
            return coll.count()
        else:
            self._ensure_sqlite_vec_table(collection)
            cursor = self._get_sqlite_conn().execute(
                f"SELECT COUNT(*) FROM vec_{collection}"
            )
            return cursor.fetchone()[0]
    
    @_synchronized
    def delete_collection(self, name: str) -> None:
        """Delete an entire collection."""
        if self._backend == "chromadb":
            try:
                client = self._get_chromadb_client()
                client.delete_collection(name)
                self._chroma_collections.pop(name, None)
                logger.info(f"Deleted ChromaDB collection: {name}")
            except Exception as e:
                logger.warning(f"Failed to delete ChromaDB collection {name}: {e}")
        else:
            try:
                conn = self._get_sqlite_conn()
                conn.execute(f"DROP TABLE IF EXISTS vec_{name}")
                conn.commit()
                logger.info(f"Deleted sqlite-vec table: vec_{name}")
            except Exception as e:
                logger.warning(f"Failed to delete sqlite-vec table vec_{name}: {e}")
    
    @_synchronized
    def reset(self) -> None:
        """Reset/clear all data (for testing)."""
        if self._backend == "chromadb":
            try:
                client = self._get_chromadb_client()
                client.reset()
                self._chroma_collections.clear()
                logger.info("ChromaDB reset complete")
            except Exception as e:
                logger.warning(f"ChromaDB reset failed: {e}")
        else:
            # Drop all vec_ tables
            conn = self._get_sqlite_conn()
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec_%'"
            ).fetchall()
            for (table_name,) in tables:
                conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.commit()
            logger.info("sqlite-vec reset complete")
    
    # ===== ChromaDB Backend =====
    
    def _get_chromadb_client(self):
        """Get or create ChromaDB client."""
        if self._chroma_client is None:
            import chromadb
            from chromadb.config import Settings
            
            # WORKAROUND: On Windows, Rust bindings hang/crash (GitHub issue #5937)
            # Use Python SegmentAPI implementation instead
            if platform.system() == "Windows":
                settings = Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                    chroma_api_impl="chromadb.api.segment.SegmentAPI",  # Bypass Rust bindings
                    persist_directory=str(self.persist_path),
                    is_persistent=True
                )
                self._chroma_client = chromadb.Client(settings)
                logger.info(f"ChromaDB client initialized (SegmentAPI) at {self.persist_path}")
            else:
                self._chroma_client = chromadb.PersistentClient(
                    path=str(self.persist_path),
                    settings=Settings(
                        anonymized_telemetry=False,
                        allow_reset=True
                    )
                )
                logger.info(f"ChromaDB client initialized at {self.persist_path}")
        return self._chroma_client
    
    def _get_chromadb_embedding_fn(self):
        """Get or create ChromaDB embedding function."""
        if self._chroma_embedding_fn is None:
            from chromadb.utils import embedding_functions
            
            providers = get_available_onnx_providers()
            self._chroma_embedding_fn = embedding_functions.ONNXMiniLM_L6_V2(
                preferred_providers=providers
            )
            logger.debug("ChromaDB embedding function created")
        return self._chroma_embedding_fn
    
    def _get_chromadb_collection(self, name: str):
        """Get or create ChromaDB collection."""
        if name not in self._chroma_collections:
            client = self._get_chromadb_client()
            embedding_fn = self._get_chromadb_embedding_fn()
            
            try:
                # Try to get existing collection
                self._chroma_collections[name] = client.get_collection(
                    name=name,
                    embedding_function=embedding_fn
                )
                logger.debug(f"Loaded ChromaDB collection: {name}")
            except Exception:
                # Create new collection
                self._chroma_collections[name] = client.create_collection(
                    name=name,
                    metadata={"hnsw:space": "cosine"},
                    embedding_function=embedding_fn
                )
                logger.info(f"Created ChromaDB collection: {name}")
        
        return self._chroma_collections[name]
    
    def _chromadb_add(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]],
        embeddings: Optional[List[List[float]]],
        metadatas: Optional[List[Dict[str, Any]]]
    ) -> None:
        """Add documents to ChromaDB collection."""
        coll = self._get_chromadb_collection(collection)
        
        kwargs: Dict[str, Any] = {"ids": ids}
        if documents:
            kwargs["documents"] = documents
        if embeddings:
            kwargs["embeddings"] = embeddings
        if metadatas:
            kwargs["metadatas"] = metadatas
        
        coll.upsert(**kwargs)
    
    def _chromadb_query(
        self,
        collection: str,
        query_text: Optional[str],
        query_embedding: Optional[List[float]],
        n_results: int,
        where: Optional[Dict[str, Any]],
        include: Optional[List[str]]
    ) -> Dict[str, Any]:
        """Query ChromaDB collection."""
        coll = self._get_chromadb_collection(collection)
        
        # Prevent warning: adjust n_results if collection has fewer items
        count = coll.count()
        actual_n_results = min(n_results, max(1, count)) if count > 0 else n_results
        
        kwargs: Dict[str, Any] = {"n_results": actual_n_results}
        if query_text:
            kwargs["query_texts"] = [query_text]
        if query_embedding:
            kwargs["query_embeddings"] = [query_embedding]
        if where:
            kwargs["where"] = where
        if include:
            kwargs["include"] = include
        
        results = coll.query(**kwargs)
        
        # Normalize format: ChromaDB 0.6.x returns flat lists, 1.x returns list-of-lists
        # Keep as list-of-lists for consistency with batch query interface
        if results.get('ids'):
            # Check if already nested (ChromaDB 1.x) or flat (ChromaDB 0.6.x)
            if not isinstance(results['ids'][0], list):
                # ChromaDB 0.6.x format - wrap in list
                return {
                    "ids": [results.get("ids", [])],
                    "distances": [results.get("distances", [])],
                    "documents": [results.get("documents", [])],
                    "metadatas": [results.get("metadatas", [])],
                }
        
        # ChromaDB 1.x format or empty - return as-is
        return results
    
    def _chromadb_delete(
        self,
        collection: str,
        ids: Optional[List[str]],
        where: Optional[Dict[str, Any]]
    ) -> None:
        """Delete from ChromaDB collection."""
        coll = self._get_chromadb_collection(collection)
        
        kwargs: Dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
        
        if kwargs:
            coll.delete(**kwargs)
    
    # ===== sqlite-vec Backend =====
    
    def _get_sqlite_conn(self) -> sqlite3.Connection:
        """Get or create sqlite-vec connection."""
        if self._sqlite_conn is None:
            import sqlite_vec
            
            db_path = self.persist_path / "vectors.db"
            self._sqlite_conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._sqlite_conn.row_factory = sqlite3.Row
            
            # Load sqlite-vec extension
            self._sqlite_conn.enable_load_extension(True)
            sqlite_vec.load(self._sqlite_conn)
            self._sqlite_conn.enable_load_extension(False)
            
            logger.info(f"sqlite-vec connection initialized at {db_path}")
        
        return self._sqlite_conn
    
    def _get_embedding_function(self):
        """Get or create embedding function for sqlite-vec backend.
        
        Tries multiple approaches in order:
        1. sentence-transformers (best quality)
        2. ChromaDB's ONNX embedding (if available)
        3. Simple hash-based fallback (for testing)
        """
        if self._sentence_transformer is None:
            # Try sentence-transformers first
            try:
                self._sentence_transformer = _load_sentence_transformer(self.embedding_model_name)
                logger.info(f"Using SentenceTransformer: {self.embedding_model_name}")
                return self._sentence_transformer
            except Exception as e:
                logger.warning(f"SentenceTransformer unavailable: {e}")
            
            # Try ChromaDB's ONNX embedding
            try:
                from chromadb.utils import embedding_functions
                ef = embedding_functions.ONNXMiniLM_L6_V2()
                # Wrap in a callable class
                class ONNXWrapper:
                    def __init__(self, ef):
                        self._ef = ef
                    def encode(self, text, convert_to_numpy=True):
                        import numpy as np
                        result = self._ef([text])[0]
                        return np.array(result) if convert_to_numpy else result
                self._sentence_transformer = ONNXWrapper(ef)
                logger.info("Using ChromaDB ONNX embedding function")
                return self._sentence_transformer
            except Exception as e:
                logger.warning(f"ChromaDB ONNX embedding unavailable: {e}")
            
            # Fallback: simple hash-based embedding (for testing only!)
            class SimpleEmbedding:
                """Simple deterministic embedding for testing when ML libraries fail."""
                def __init__(self, dim=384):
                    self.dim = dim
                def encode(self, text, convert_to_numpy=True):
                    import hashlib
                    import numpy as np
                    # Create deterministic embedding from text hash
                    h = hashlib.sha256(text.encode()).digest()
                    # Expand hash to embedding dimension
                    expanded = h * (self.dim // len(h) + 1)
                    values = [b / 255.0 for b in expanded[:self.dim]]
                    # Normalize
                    norm = sum(v*v for v in values) ** 0.5
                    values = [v / norm for v in values]
                    return np.array(values) if convert_to_numpy else values
            
            self._sentence_transformer = SimpleEmbedding(EMBEDDING_DIM)
            logger.warning("Using simple hash-based embedding (quality limited, for testing only)")
        
        return self._sentence_transformer
    
    def _compute_embedding(self, text: str) -> List[float]:
        """Compute embedding for text."""
        model = self._get_embedding_function()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    
    def _serialize_embedding(self, embedding: List[float]) -> bytes:
        """Serialize embedding to bytes for sqlite-vec."""
        return struct.pack(f'{len(embedding)}f', *embedding)
    
    def _ensure_sqlite_vec_table(self, collection: str) -> None:
        """Ensure sqlite-vec virtual table exists."""
        conn = self._get_sqlite_conn()
        
        # Create vec0 virtual table
        conn.execute(f'''
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_{collection} USING vec0(
                item_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            )
        ''')
        
        # Create metadata table
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS meta_{collection} (
                item_id TEXT PRIMARY KEY,
                document TEXT,
                metadata TEXT
            )
        ''')
        conn.commit()
    
    def _sqlite_vec_add(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]],
        embeddings: Optional[List[List[float]]],
        metadatas: Optional[List[Dict[str, Any]]]
    ) -> None:
        """Add documents to sqlite-vec collection."""
        self._ensure_sqlite_vec_table(collection)
        conn = self._get_sqlite_conn()
        
        for i, item_id in enumerate(ids):
            # Get or compute embedding
            if embeddings and i < len(embeddings):
                emb = embeddings[i]
            elif documents and i < len(documents):
                emb = self._compute_embedding(documents[i])
            else:
                raise VectorStoreError(f"No document or embedding for id {item_id}")
            
            # Get document and metadata
            doc = documents[i] if documents and i < len(documents) else None
            meta = metadatas[i] if metadatas and i < len(metadatas) else None
            
            # Upsert vector
            conn.execute(
                f"INSERT OR REPLACE INTO vec_{collection}(item_id, embedding) VALUES (?, ?)",
                (item_id, self._serialize_embedding(emb))
            )
            
            # Upsert metadata
            import json
            conn.execute(
                f"INSERT OR REPLACE INTO meta_{collection}(item_id, document, metadata) VALUES (?, ?, ?)",
                (item_id, doc, json.dumps(meta) if meta else None)
            )
        
        conn.commit()
    
    def _sqlite_vec_query(
        self,
        collection: str,
        query_text: Optional[str],
        query_embedding: Optional[List[float]],
        n_results: int
    ) -> Dict[str, Any]:
        """Query sqlite-vec collection."""
        self._ensure_sqlite_vec_table(collection)
        conn = self._get_sqlite_conn()
        
        # Get or compute query embedding
        if query_embedding:
            emb = query_embedding
        elif query_text:
            emb = self._compute_embedding(query_text)
        else:
            raise VectorStoreError("Either query_text or query_embedding required")
        
        # Query vectors (sqlite-vec requires k parameter in MATCH clause)
        rows = conn.execute(f'''
            SELECT v.item_id, v.distance, m.document, m.metadata
            FROM vec_{collection} v
            LEFT JOIN meta_{collection} m ON v.item_id = m.item_id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
        ''', (self._serialize_embedding(emb), n_results)).fetchall()
        
        import json
        ids = []
        distances = []
        documents = []
        metadatas = []
        
        for row in rows:
            ids.append(row["item_id"])
            distances.append(row["distance"])
            documents.append(row["document"])
            meta_str = row["metadata"]
            metadatas.append(json.loads(meta_str) if meta_str else None)
        
        # Return format compatible with ChromaDB (list of lists for batch queries)
        return {
            "ids": [ids],
            "distances": [distances],
            "documents": [documents],
            "metadatas": [metadatas],
        }
    
    def _sqlite_vec_delete(
        self,
        collection: str,
        ids: Optional[List[str]]
    ) -> None:
        """Delete from sqlite-vec collection."""
        if not ids:
            return
        
        self._ensure_sqlite_vec_table(collection)
        conn = self._get_sqlite_conn()
        
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM vec_{collection} WHERE item_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM meta_{collection} WHERE item_id IN ({placeholders})", ids)
        conn.commit()


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
