"""The sqlite-vec backend of `VectorStore`: the fallback where chromadb is not installed.

One sqlite database per store path (``vectors.db``) holds a ``vec0`` virtual
table of vectors and a ``meta_`` table of documents and metadata per
collection, and answers in ChromaDB's shapes so callers need not know which
backend runs. Without chromadb the embedding model runs on sentence-transformers
(`embeddings._load_embedding_model`).
"""

import json
import logging
import sqlite3
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import VectorBackend, VectorStoreError
from .embeddings import EMBEDDING_DIM, _load_embedding_model

logger = logging.getLogger(__name__)


class SqliteVecBackend(VectorBackend):
    """A sqlite-vec store at one path, embedding with the model it was given."""

    name = "sqlite-vec"

    def __init__(self, persist_path: Path, embedding_model_name: str) -> None:
        self.persist_path = persist_path
        self.embedding_model_name = embedding_model_name
        # sqlite-vec state
        self._conn: Optional[sqlite3.Connection] = None
        self._model = None

    def close(self) -> None:
        """Close the connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ===== Operations =====

    def add(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]],
        embeddings: Optional[List[List[float]]],
        metadatas: Optional[List[Dict[str, Any]]]
    ) -> None:
        """Add documents to sqlite-vec collection."""
        self._ensure_table(collection)
        conn = self._get_conn()

        try:
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

                # Upsert vector. vec0 refuses INSERT OR REPLACE on an id it
                # already holds ("UNIQUE constraint failed", sqlite_vec 0.1.6):
                # a re-add raised where ChromaDB's upsert replaces. Removed
                # first, it replaces here too.
                conn.execute(f"DELETE FROM vec_{collection} WHERE item_id = ?", (item_id,))
                conn.execute(
                    f"INSERT INTO vec_{collection}(item_id, embedding) VALUES (?, ?)",
                    (item_id, self._serialize_embedding(emb))
                )

                # Upsert metadata
                conn.execute(
                    f"INSERT OR REPLACE INTO meta_{collection}(item_id, document, metadata) VALUES (?, ?, ?)",
                    (item_id, doc, json.dumps(meta) if meta else None)
                )

            conn.commit()
        except BaseException:
            # sqlite3 leaves the implicit transaction open on an error: the
            # items before the failing one would be saved by whatever commits
            # on this connection next.
            conn.rollback()
            raise

    def query(
        self,
        collection: str,
        query_text: Optional[str],
        query_embedding: Optional[List[float]],
        n_results: int,
        where: Optional[Dict[str, Any]],
        include: Optional[List[str]]
    ) -> Dict[str, Any]:
        """Query sqlite-vec collection; *where* and *include* are ChromaDB's alone."""
        self._ensure_table(collection)
        conn = self._get_conn()
        
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

    def delete(
        self,
        collection: str,
        ids: Optional[List[str]],
        where: Optional[Dict[str, Any]]
    ) -> None:
        """Delete from sqlite-vec collection; a *where* filter is ChromaDB's alone."""
        if not ids:
            return
        
        self._ensure_table(collection)
        conn = self._get_conn()
        
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM vec_{collection} WHERE item_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM meta_{collection} WHERE item_id IN ({placeholders})", ids)
        conn.commit()

    def get_or_create_collection(self, name: str) -> Any:
        """Initialize the collection's tables; there is no handle to return."""
        self._ensure_table(name)
        return None

    def list_ids(self, collection: str) -> List[str]:
        """All ids of the collection, from ``meta_<collection>``."""
        self._ensure_table(collection)
        rows = self._get_conn().execute(
            f"SELECT item_id FROM meta_{collection}"
        ).fetchall()
        return [r[0] for r in rows]

    def list_entries(self, collection: str) -> List[Dict[str, Any]]:
        """All ids with the metadata kept as JSON in ``meta_<collection>``."""
        self._ensure_table(collection)
        rows = self._get_conn().execute(
            f"SELECT item_id, metadata FROM meta_{collection}"
        ).fetchall()
        return [
            {"id": r[0], "metadata": json.loads(r[1]) if r[1] else {}}
            for r in rows
        ]

    def count(self, collection: str) -> int:
        """Get the number of documents in a collection."""
        self._ensure_table(collection)
        cursor = self._get_conn().execute(
            f"SELECT COUNT(*) FROM vec_{collection}"
        )
        return cursor.fetchone()[0]

    def delete_collection(self, name: str) -> None:
        """Delete an entire collection."""
        try:
            conn = self._get_conn()
            conn.execute(f"DROP TABLE IF EXISTS vec_{name}")
            # The metadata table must go too -- leaving it made list_ids
            # report documents whose vectors no longer existed.
            conn.execute(f"DROP TABLE IF EXISTS meta_{name}")
            conn.commit()
            logger.info(f"Deleted sqlite-vec tables: vec_{name}, meta_{name}")
        except Exception as e:
            logger.warning(f"Failed to delete sqlite-vec table vec_{name}: {e}")

    def reset(self) -> None:
        """Reset/clear all data (for testing)."""
        # Drop all vec_ and meta_ tables
        conn = self._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'vec_%' OR name LIKE 'meta_%')"
        ).fetchall()
        for (table_name,) in tables:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.commit()
        logger.info("sqlite-vec reset complete")

    # ===== Internals =====

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create sqlite-vec connection."""
        if self._conn is None:
            import sqlite_vec
            
            db_path = self.persist_path / "vectors.db"
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            
            # Load sqlite-vec extension
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            
            logger.info(f"sqlite-vec connection initialized at {db_path}")
        
        return self._conn
    
    def _get_embedding_function(self):
        """The embedding model of the sqlite-vec backend (`_load_embedding_model`).

        No stand-in when it cannot load: the vectors of another model -- or of
        a hash, as there used to be -- would be stored for good and rank every
        later query by noise. The ImportError says what to install.
        """
        if self._model is None:
            self._model = _load_embedding_model(self.embedding_model_name)
            logger.info(f"Using embedding model: {self.embedding_model_name}")
        return self._model

    def _compute_embedding(self, text: str) -> List[float]:
        """Compute embedding for text."""
        model = self._get_embedding_function()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    
    def _serialize_embedding(self, embedding: List[float]) -> bytes:
        """Serialize embedding to bytes for sqlite-vec."""
        return struct.pack(f'{len(embedding)}f', *embedding)
    
    def _ensure_table(self, collection: str) -> None:
        """Ensure sqlite-vec virtual table exists."""
        conn = self._get_conn()
        
        # Create vec0 virtual table
        # ``distance_metric=cosine`` ist TRAGEND, nicht Geschmack: ohne die
        # Angabe rechnet vec0 mit L2, ChromaDB aber mit Cosinus. Beide
        # Backends lieferten dann Distanzen auf VERSCHIEDENEN Skalen, und
        # jeder Konsument, der daraus eine Aehnlichkeit macht, bekommt beim
        # Fallback stillschweigend falsche Werte. Gemessen 2026-08-06:
        # dieselbe Suche ergab unter Chroma Distanz ~0,46 (Aehnlichkeit
        # 0,77) und unter sqlite-vec ~1,36 (0,32) — unter einem
        # Mindest-Schwellwert von 0,5 fiel im Fallback JEDER Treffer weg.
        # Die Suche meldete dann null Ergebnisse statt eines Fehlers.
        conn.execute(f'''
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_{collection} USING vec0(
                item_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine
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
