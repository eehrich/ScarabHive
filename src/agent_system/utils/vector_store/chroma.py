"""The ChromaDB backend of `VectorStore`, and the cross-process guards a chroma store needs.

A chroma store on disk is shared: by every `VectorStore` of this process on the
same path (one client, `_ChromaAccess`) and by every process that opens it (the
init and write locks and the write counter next to them). The measurements
behind each guard are on the constants below. `ChromaBackend` is the one
instance's part -- its collection handles and the operations -- and works
through the path's access, which `VectorStore` acquires for either backend:
its lock is the path's, whichever backend runs.
"""

import contextlib
import logging
import platform
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from filelock import FileLock

from .base import VectorBackend
from .embeddings import compute_embedding, compute_embeddings, get_available_onnx_providers

logger = logging.getLogger(__name__)

#: Held while a process opens a chroma store and while it gets or creates a
#: collection -- the two steps that write chroma's schema. Measured 21.09.2026
#: (chromadb 1.5.9): four processes opening a NEW store at once, one died on
#: "table embeddings_queue_config already exists" and its documents were
#: never written. Taken once per process and name; writes have their own lock.
_CHROMA_INIT_LOCK = ".agent_system_init.lock"
#: A first open after a chroma upgrade migrates the whole store; on the
#: 233 MB memory store that is not seconds. Waiting beats dying half-way.
_CHROMA_INIT_TIMEOUT = 600.0
#: Held for every chroma WRITE, across processes, and the counter next to it
#: says how many writes the store has seen. A process whose view is older than
#: the counter reopens the store before it writes. Why both: every process
#: keeps its own copy of a collection's vector index in memory, and saves it
#: with the claim "complete up to my last write". A process that writes on a
#: view without the other processes' writes therefore saves an index that
#: lacks them -- counted, never found again. The lock ALONE is worse than no
#: guard: it lines the writers up, and each saves its stale view after the one
#: before. Measured 21.09.2026 (chromadb 1.5.9, Windows, 16 writers x 200
#: documents, found again by their own vector, of 640): one after another
#: 638; no guard 596-639, a race; the lock alone 200 and 444; lock + reopen
#: 635-639. On Linux (Rust bindings, WSL): one after another 636; the lock
#: alone 405 and 246; lock + reopen 637-638; and no guard left a store whose
#: count() hangs for every process that opens it, twice in two runs. That is a
#: MEASUREMENT, not a unit test -- at test sizes the race does not happen with
#: or without the guard -- and it is re-runnable:
#: scripts/measure_vector_store_processes.py.
_CHROMA_WRITE_LOCK = ".agent_system_write.lock"
_CHROMA_GENERATION = ".agent_system_generation"
_CHROMA_WRITE_TIMEOUT = 600.0


class _ChromaAccess:
    """What one PROCESS holds for one store path -- shared by every VectorStore on it.

    chromadb keeps one System per persist directory in a process-global
    registry and stops it only when the LAST client on it closes. Two
    VectorStores on the same path in one process -- file_ops and
    workspace_file_ops both default to data/cache/file_ops_chromadb -- therefore
    share one in-memory view, and "reopen to see the other processes' writes"
    handed the reopening instance the same stale System back, because the
    other instance still held it. So the client, the write counter it has
    seen, and the lock live here, once per path: a reopen replaces the view
    for every instance at once, and ``epoch`` tells each of them to drop the
    collection handles it cached from the old one. Measured with a second
    store open in every writer process (16 x 200): one access per instance
    638 and 206 of 640 found again, shared 637 and 639.
    """

    # Set once the first time close() meets a chromadb without Client.close().
    _close_unsupported_warned: bool = False

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.client: Any = None
        self.seen_generation: Optional[int] = None
        self.epoch = 0
        self.users = 0

    def close_client(self) -> None:
        """Stop the PATH's chroma client -- for every instance on it; the next access opens a new one."""
        self.epoch += 1
        if self.client is not None:
            # Dropping the reference is NOT enough. chromadb keeps every System in
            # a process-global registry (SharedSystemClient._identifier_to_system,
            # keyed by persist_directory), and each System owns a tokio runtime
            # with its own threads, epoll and eventfd handles. Callers give every
            # session its own persist path, so no client is ever reused and a
            # dropped one leaks ~6 descriptors and ~5 threads that nothing
            # reclaims — measured on the writer host at 229 leaked clients and 460
            # tokio threads, which ran the API into the 1024 descriptor limit and
            # turned every X-API-Key request into a 401. close() drops the
            # refcount and stops the System.
            close = getattr(self.client, "close", None)
            if close is None:
                # chromadb < 1.5 has no close() at all, so the leak cannot be
                # avoided there. That is a property of the installation, not of
                # this call — reported once per process instead of on every
                # cleanup, which would be pure noise.
                if not _ChromaAccess._close_unsupported_warned:
                    _ChromaAccess._close_unsupported_warned = True
                    logger.warning(
                        "This chromadb version has no Client.close(); every "
                        "VectorStore leaks its tokio runtime (~6 file "
                        "descriptors, ~5 threads). Upgrade to chromadb >= 1.5 "
                        "on any host that opens one store per session."
                    )
            else:
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 — cleanup must not raise
                    logger.warning(
                        "ChromaDB client close failed (%s) — its tokio runtime "
                        "stays alive and keeps leaking file descriptors",
                        exc.__class__.__name__,
                    )
            self.client = None
        self.seen_generation = None


_CHROMA_ACCESS: Dict[str, _ChromaAccess] = {}
_CHROMA_ACCESS_GUARD = threading.Lock()


def _acquire_access(persist_path: Path) -> tuple:
    """The access for this path, with one more user on it; and its registry key."""
    key = str(persist_path.resolve())
    with _CHROMA_ACCESS_GUARD:
        access = _CHROMA_ACCESS.get(key)
        if access is None:
            access = _CHROMA_ACCESS[key] = _ChromaAccess()
        access.users += 1
    return access, key


def _release_access(access: _ChromaAccess, key: str) -> bool:
    """One user less on the path's access; True when it was the last, which also leaves the registry."""
    with _CHROMA_ACCESS_GUARD:
        access.users -= 1
        last = access.users == 0
        if last:
            _CHROMA_ACCESS.pop(key, None)
    return last


class ChromaBackend(VectorBackend):
    """A chroma store at one path, through the path's shared client (`_ChromaAccess`).

    Every write runs under `_write`: alone across processes and on a view that
    holds every other process's writes (see _CHROMA_WRITE_LOCK). Opening the
    store and making a collection run under `_init_lock` (_CHROMA_INIT_LOCK).
    The collection handles are this instance's cache; `epoch` tells it when
    another instance on the path replaced the client they came from.
    """

    name = "chromadb"

    def __init__(self, persist_path: Path, access: _ChromaAccess) -> None:
        self.persist_path = persist_path
        # ChromaDB state. The client and the write counter it has seen are the
        # PATH's, shared with every other VectorStore of this process on the
        # same directory (see _ChromaAccess); the collection handles are this
        # instance's cache, valid for as long as the shared client is.
        self._access = access
        self._collections: Dict[str, Any] = {}
        self._epoch = access.epoch
        self._embedding_fn = None

    def close(self) -> None:
        """Drop this instance's collection handles; the client is the path's (_ChromaAccess.close_client)."""
        self._collections.clear()

    @property
    def _client(self) -> Any:
        return self._access.client

    @_client.setter
    def _client(self, client: Any) -> None:
        self._access.client = client

    @property
    def _seen_generation(self) -> Optional[int]:
        """The store's write counter as it stood when the shared client opened,
        or after this process's own last write -- see _CHROMA_WRITE_LOCK."""
        return self._access.seen_generation

    @_seen_generation.setter
    def _seen_generation(self, value: Optional[int]) -> None:
        self._access.seen_generation = value

    def _close_client(self) -> None:
        """Drop this instance's handles and stop the PATH's chroma client -- for every instance on it."""
        self._collections.clear()
        self._access.close_client()

    # ===== Operations =====

    def add(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]],
        embeddings: Optional[List[List[float]]],
        metadatas: Optional[List[Dict[str, Any]]],
    ) -> None:
        """Embed the documents that come without vectors, then upsert them under the write lock."""
        if documents and not embeddings:
            # The model of the collection's own function, through the lock that readies it and without
            # chromadb's padding to 256 tokens: the vectors are the same (_OnnxMiniLM). Before the store's
            # write lock: a first use may wait minutes for another process to ready the model, and a
            # model that fails has written nothing the other processes must reopen for.
            embeddings = compute_embeddings(documents)
        with self._write():
            self._upsert(collection, ids, documents, embeddings, metadatas)

    def query(
        self,
        collection: str,
        query_text: Optional[str],
        query_embedding: Optional[List[float]],
        n_results: int,
        where: Optional[Dict[str, Any]],
        include: Optional[List[str]]
    ) -> Dict[str, Any]:
        """Query ChromaDB collection."""
        coll = self._get_collection(collection)
        
        # Prevent warning: adjust n_results if collection has fewer items
        count = coll.count()
        actual_n_results = min(n_results, max(1, count)) if count > 0 else n_results
        
        kwargs: Dict[str, Any] = {"n_results": actual_n_results}
        if query_text and not query_embedding:
            query_embedding = compute_embedding(query_text)  # as add embeds the documents
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

    def delete(
        self,
        collection: str,
        ids: Optional[List[str]],
        where: Optional[Dict[str, Any]],
    ) -> None:
        """Delete under the write lock."""
        with self._write():
            self._delete(collection, ids, where)

    def get_or_create_collection(self, name: str) -> Any:
        """The collection object."""
        return self._get_collection(name)

    def refresh_collection(self, name: str) -> Any:
        """Drop the cached handle for *name* and fetch it again (see VectorStore.refresh_collection)."""
        self._collections.pop(name, None)
        return self.get_or_create_collection(name)

    def list_ids(self, collection: str) -> List[str]:
        """All ids of the collection; none when it cannot be read."""
        try:
            coll = self._get_collection(collection)
            return list(coll.get(include=[]).get("ids") or [])
        except Exception as e:
            logger.debug("list_ids(%s): %s", collection, e)
            return []

    def list_entries(self, collection: str) -> List[Dict[str, Any]]:
        """All ids with their metadata, from chroma's ``get``; none when it cannot be read."""
        try:
            coll = self._get_collection(collection)
            got = coll.get(include=["metadatas"])
            ids = list(got.get("ids") or [])
            metadatas = list(got.get("metadatas") or [])
            return [
                {"id": item_id,
                 "metadata": dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}}
                for i, item_id in enumerate(ids)
            ]
        except Exception as e:
            logger.debug("list_entries(%s): %s", collection, e)
            return []

    def count(self, collection: str) -> int:
        """Get the number of documents in a collection."""
        coll = self._get_collection(collection)
        return coll.count()

    def delete_collection(self, name: str) -> None:
        """Delete an entire collection."""
        try:
            with self._write():
                client = self._get_client()
                client.delete_collection(name)
            self._collections.pop(name, None)
            logger.info(f"Deleted ChromaDB collection: {name}")
        except Exception as e:
            logger.warning(f"Failed to delete ChromaDB collection {name}: {e}")

    def reset(self) -> None:
        """Reset/clear all data (for testing)."""
        try:
            with self._write():
                client = self._get_client()
                client.reset()
            self._collections.clear()
            logger.info("ChromaDB reset complete")
        except Exception as e:
            logger.warning(f"ChromaDB reset failed: {e}")

    # ===== Internals =====

    def _get_client(self):
        """Get or create ChromaDB client."""
        if self._client is None:
            import chromadb
            from chromadb.config import Settings

            # WORKAROUND: On Windows, Rust bindings hang/crash (GitHub issue #5937)
            # Use Python SegmentAPI implementation instead
            with self._init_lock():  # opening writes the schema -- see _CHROMA_INIT_LOCK
                # Read BEFORE the open: a write in between then costs one reopen
                # too many at the next write, never one too few.
                self._seen_generation = self._read_generation()
                if platform.system() == "Windows":
                    settings = Settings(
                        anonymized_telemetry=False,
                        allow_reset=True,
                        chroma_api_impl="chromadb.api.segment.SegmentAPI",  # Bypass Rust bindings
                        persist_directory=str(self.persist_path),
                        is_persistent=True
                    )
                    self._client = chromadb.Client(settings)
                    logger.info(f"ChromaDB client initialized (SegmentAPI) at {self.persist_path}")
                else:
                    self._client = chromadb.PersistentClient(
                        path=str(self.persist_path),
                        settings=Settings(
                            anonymized_telemetry=False,
                            allow_reset=True
                        )
                    )
                    logger.info(f"ChromaDB client initialized at {self.persist_path}")
        return self._client

    def _init_lock(self) -> FileLock:
        """The cross-process lock for opening this store and making collections."""
        return FileLock(str(self.persist_path / _CHROMA_INIT_LOCK), timeout=_CHROMA_INIT_TIMEOUT)

    def _read_generation(self) -> int:
        """How many chroma writes this store has seen; 0 when nothing says.

        Unreadable counts as 0, which can only make a later write reopen once
        more than it had to -- never skip a reopen it needed.
        """
        try:
            return int((self.persist_path / _CHROMA_GENERATION).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    @contextlib.contextmanager
    def _write(self):
        """One chroma write: alone across processes, on a view with every write before it."""
        with FileLock(str(self.persist_path / _CHROMA_WRITE_LOCK), timeout=_CHROMA_WRITE_TIMEOUT):
            current = self._read_generation()
            if self._client is not None and current != self._seen_generation:
                # Somebody wrote since this client opened: drop it, and the
                # write below opens the store as it is now.
                self._close_client()
            counter = self.persist_path / _CHROMA_GENERATION
            try:
                yield
            except BaseException:
                # Part of it may be in the store. The others must reopen, and so
                # must this process -- its own view is now anybody's guess. One
                # reopen too many is cheap; one too few loses documents.
                counter.write_text(str(current + 1), encoding="utf-8")
                self._seen_generation = None
                raise
            counter.write_text(str(current + 1), encoding="utf-8")
            self._seen_generation = current + 1
    
    def _get_embedding_fn(self):
        """Get or create ChromaDB embedding function."""
        if self._embedding_fn is None:
            from chromadb.utils import embedding_functions
            
            providers = get_available_onnx_providers()
            self._embedding_fn = embedding_functions.ONNXMiniLM_L6_V2(
                preferred_providers=providers
            )
            logger.debug("ChromaDB embedding function created")
        return self._embedding_fn
    
    def _get_collection(self, name: str):
        """Get or create ChromaDB collection."""
        if self._epoch != self._access.epoch:
            # Another instance on this path replaced the shared client; the
            # handles cached here belong to the stopped one.
            self._collections.clear()
            self._epoch = self._access.epoch
        if name not in self._collections:
            client = self._get_client()
            embedding_fn = self._get_embedding_fn()

            # Under the init lock: two processes that both miss the get would
            # both create, and the second one's create raises "already exists".
            with self._init_lock():
                try:
                    # Try to get existing collection
                    self._collections[name] = client.get_collection(
                        name=name,
                        embedding_function=embedding_fn
                    )
                    logger.debug(f"Loaded ChromaDB collection: {name}")
                except Exception:
                    # Create new collection
                    self._collections[name] = client.create_collection(
                        name=name,
                        metadata={"hnsw:space": "cosine"},
                        embedding_function=embedding_fn
                    )
                    logger.info(f"Created ChromaDB collection: {name}")
        
        return self._collections[name]
    
    def _upsert(
        self,
        collection: str,
        ids: List[str],
        documents: Optional[List[str]],
        embeddings: Optional[List[List[float]]],
        metadatas: Optional[List[Dict[str, Any]]]
    ) -> None:
        """Add documents to ChromaDB collection."""
        coll = self._get_collection(collection)
        kwargs: Dict[str, Any] = {"ids": ids}
        if documents:
            kwargs["documents"] = documents
        if embeddings:
            kwargs["embeddings"] = embeddings
        if metadatas:
            kwargs["metadatas"] = metadatas
        
        coll.upsert(**kwargs)

    def _delete(
        self,
        collection: str,
        ids: Optional[List[str]],
        where: Optional[Dict[str, Any]]
    ) -> None:
        """Delete from ChromaDB collection."""
        coll = self._get_collection(collection)
        
        kwargs: Dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
        
        if kwargs:
            coll.delete(**kwargs)
