"""The semantic index, and the thin seam to the index-free text search.

Glob and grep do NOT live here any more: they delegate to ``textsearch``,
which walks the file system on demand. What remains in this module is the
semantic index, and its one consumer,
:meth:`FileSearchEngine.semantic_search`, which needs a corpus to compare
against.

The split has a measured reason. ``search_files`` used to await a full index
build before answering: with the coder configuration (four allowed roots, the
last of them the repository itself, 152.664 files / 127 GB) one call had not
returned after 150 seconds, while the same glob takes under a second when
nobody indexes first. A question about six paths does not need every file in
the tree read.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiofiles
from filelock import FileLock
from filelock import Timeout as LockTimeout

from agent_system.paths import data_path
from agent_system.utils.vector_store import (
    VectorStore,
    VectorStoreError,
    compute_embeddings,
)

from . import ripgrep, symbols, textsearch


logger = logging.getLogger(__name__)

#: Documents per embedding batch. Module level so a test can shrink it --
#: with 2000 no test tree ever reaches the second round, and the chunking was
#: therefore never executed by the suite that claimed to check it.
EMBED_BATCH_SIZE = 2000

#: One index pass at a time across processes -- see rebuild_index.
_PASS_LOCK_NAME = ".file_ops_index_pass.lock"
#: How long a FULL rebuild waits for another process's pass. A full pass over
#: this repository measured 423 s; twice that is a pass that is stuck.
_FULL_PASS_WAIT = 900.0
#: Files stat'ed, read and cut into documents per worker-thread call.
_PREPARE_BATCH = 200
#: How often a pass writes its checkpoint. The state of the coder tree is
#: megabytes of JSON, so not after every batch.
_STATE_SAVE_SECONDS = 30.0
#: How long the background indexer waits after a pass that failed.
_RETRY_SECONDS = 60.0


@dataclass
class _PendingFile:
    """A file read and cut into documents, waiting for its batch to be stored."""

    path: Path
    mtime: float
    digest: str
    ids: List[str] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)
    metadatas: List[Dict[str, Any]] = field(default_factory=list)


def _relative_to(path: Path, root: Path) -> Path:
    """*path* relative to the directory it was found under; its name alone if
    it does not lie there (a walk that followed a link out)."""
    try:
        return path.relative_to(root)
    except ValueError:
        return Path(path.name)


class FileSearchEngine:
    """Fast file search with background indexing."""

    def __init__(self, allowed_dirs: List[Path], config: Dict[str, Any]):
        """
        Initialize search engine with background indexing.

        Args:
            allowed_dirs: List of directories to index
            config: Search configuration (enable_indexing, exclude_patterns, etc.)
        """
        self.allowed_dirs = allowed_dirs
        self.config = config

        # What the semantic index has seen, for incremental updates
        self.file_mtimes: Dict[Path, float] = {}
        #: The document ids each file contributed, so an update can take the
        #: old ones out. A file is many documents now (see :mod:`symbols`), and
        #: its id is no longer derivable from its path alone.
        self.file_symbol_ids: Dict[Path, List[str]] = {}
        #: The content hash of each file, and the files booked as aliases of
        #: another file with the same content (see _run_index_pass).
        self.file_hashes: Dict[Path, str] = {}
        self.file_aliases: Set[Path] = set()
        #: (files done, files walked) while a pass runs, for the answer a
        #: search gives meanwhile.
        self._progress: Optional[Tuple[int, int]] = None

        # Indexing control
        self._indexing_task: Optional[asyncio.Task] = None
        self._index_lock = asyncio.Lock()
        self._indexing_started = False

        # VectorStore for semantic search (replaces direct ChromaDB usage)
        self._vector_store: Optional[VectorStore] = None
        self._vector_store_initialized = False
        self._state_path: Optional[Path] = None
        #: Set by stop(), read by the worker thread that shutdown cannot cancel
        self._closing = False
        #: True once a pass has finished or a COMPLETE state was read back. NOT
        #: `count() > 0`: the first batch of a full build lands long before the
        #: tree is covered. While it is False and the bookkeeping holds files,
        #: a search answers from them and says the index is still being built.
        self._index_built = False
        #: Why the background indexer's last pass failed. Read only while no
        #: index is built: a build that failed every time (no embedding model,
        #: say) answered "being built, try again in a minute" forever, and the
        #: reason stood only in the log.
        self._index_error: Optional[str] = None
        #: The freshness pass a search kicks off, so shutdown can cancel it
        self._fresh_task: Optional[asyncio.Task] = None
        # Per instance, because two file_ops instances are two different trees:
        # with one shared name a full rebuild of the second CLEARED the first,
        # and both answered about files the other one could not even read.
        # The server passes its instance name; the default keeps old stores
        # readable for a lone instance.
        self._collection_name = str(self.config.get("collection_name")
                                    or "file_ops_semantic_index")

    def _ensure_indexing_started(self):
        """Start background indexing if not already started and if enabled.

        Called from the first semantic_search of an instance, and only there.
        Starting it when the server is BUILT was tried and taken back: measured
        on 18.09.2026 it kicked off four full builds at once, one per instance
        that has semantic search configured (file_ops,
        agent_file_ops, coder_fs), for trees nobody had asked about yet.

        The consequence is worth knowing: an instance that only READS a shared
        index (enable_indexing: false) never starts anything, so that index
        exists once the instance that owns it has been asked a question. In the
        coder harness the owner is coder_fs, which the main agent uses.

        Semantic search is the only consumer of the index, so an instance
        without it walks the tree for nothing.
        """
        if (not self._indexing_started and
            self.config.get("enable_semantic_search", True) and
            self.config.get("enable_indexing", True) and
            self.config.get("index_on_startup", True)):
            try:
                # Try to create task if event loop is running
                self._indexing_task = asyncio.create_task(self._background_indexer())
                self._indexing_started = True
            except RuntimeError:
                # No event loop running yet, will be started on first use
                pass

    def _init_vector_store(self):
        """Initialize VectorStore for semantic search."""
        if self._vector_store_initialized:
            return

        try:
            logger.info("FILE_OPS: Initializing VectorStore for semantic search...")
            
            persist_path = self._persist_path()
            self._vector_store = VectorStore(persist_path=persist_path)
            self._state_path = persist_path / f"{self._collection_name}_state.json"

            # Force recreate collection if configured
            if self.config.get("force_recreate_chroma_collection", False):
                logger.info(f"FILE_OPS: Force recreating collection '{self._collection_name}'")
                self._vector_store.delete_collection(self._collection_name)
                self._state_path.unlink(missing_ok=True)

            # Initialize collection
            self._vector_store.get_or_create_collection(self._collection_name)
            self._load_state()

            self._vector_store_initialized = True
            logger.info(f"FILE_OPS: VectorStore initialized (backend: {self._vector_store.backend})")

        except Exception as e:
            logger.error(f"Failed to initialize VectorStore: {e}", exc_info=True)
            raise VectorStoreError(f"VectorStore initialization failed: {e}")

    async def _background_indexer(self):
        """Periodically rebuild index in background."""
        # A full rebuild CLEARS the collection before it refills it, so it may
        # only run when there is nothing worth keeping. Reading the state first
        # is what tells the two apart: a store this instance already filled
        # needs the incremental pass, and a first run after a restart used to
        # wipe the warm index and then answer nothing for minutes while it
        # built the same vectors again.
        #
        # Every pass asks for an INCREMENTAL one, the first too: the pass turns
        # into a full one by itself when the state it loads under the pass lock
        # books nothing. Asking for a full one on the first run threw away a
        # build that had died half way in this very process -- the retry a
        # minute later cleared the collection the checkpoints had kept.
        if self.config.get("enable_semantic_search", True):
            try:
                self._init_vector_store()
            except VectorStoreError as e:
                logger.error(f"FILE_OPS: no vector store, indexing stops: {e}")
                return

        while True:
            try:
                await self.rebuild_index(incremental=True)

                # Wait for next cycle
                # The configured name is `auto_reindex_interval` — schema,
                # plugins.yaml and every agent config write that one. Reading
                # only the seconds-suffixed variant meant no configured value
                # ever applied and the interval was always the 300s default.
                interval = self.config.get(
                    "auto_reindex_interval",
                    self.config.get("auto_reindex_interval_seconds", 300))
                await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"Indexing error: {e}", exc_info=True)
                self._index_error = f"{type(e).__name__}: {e}"[:300]
                await asyncio.sleep(_RETRY_SECONDS)

    async def rebuild_index(self, status_callback=None, incremental=True):
        """Run one index pass, and never two at the same time.

        Measured live on 18.09.2026: after a restart the background indexer and
        the freshness check of the first search each started a pass, and both
        walked the same 3.648 files at the same time. They delete a changed
        file's old documents and add its new ones -- interleaved, one pass can
        delete what the other has just written, and the file is then missing
        from the index until something changes it again.

        A second INCREMENTAL pass is dropped rather than queued: it would only
        repeat the work the running one is already doing. A full rebuild waits,
        because it is always asked for on purpose.

        Returns whether a pass actually ran, so a caller that keeps a "last
        refreshed" stamp does not record a pass that was dropped.
        """
        if incremental and self._index_lock.locked():
            logger.debug("FILE_OPS: an index pass is already running — skipping this one")
            return False
        async with self._index_lock:
            # The same rule across PROCESSES. Every agent-cli that loads
            # file_ops runs its own background indexer over the same store, so
            # N processes walked the same tree N times, each writing on its own
            # stale view of the index (VectorStore now keeps that from losing
            # vectors, but only by reopening the store per batch) and each
            # writing the state file from its own memory.
            pass_lock = self._pass_lock()
            if pass_lock is not None:
                try:
                    if incremental:
                        pass_lock.acquire(timeout=0)      # one syscall, no need for a thread
                    else:
                        await asyncio.to_thread(pass_lock.acquire, timeout=_FULL_PASS_WAIT)
                except LockTimeout:
                    if incremental:
                        logger.debug("FILE_OPS: another process is indexing — skipping this pass")
                        return False
                    raise VectorStoreError(
                        f"another process held the index for {_FULL_PASS_WAIT:.0f}s -- "
                        "the full rebuild did not run")
            try:
                if pass_lock is not None:
                    # Another process may have indexed since this one read the
                    # state -- start from what is on disk, not from memory.
                    # Under the pass lock, so this is where a disagreeing state
                    # may be mended (see _load_state).
                    await asyncio.to_thread(self._load_state, True)
                await self._run_index_pass(status_callback, incremental)
            finally:
                if pass_lock is not None:
                    pass_lock.release()
        return True

    def _persist_path(self) -> Path:
        """Where the semantic index lives -- one answer for the store and its lock."""
        # "chroma_db_path": the old config name, kept for compatibility
        return Path(self.config.get("chroma_db_path")
                    or data_path("cache", "file_ops_chromadb"))

    def _pass_lock(self) -> Optional[FileLock]:
        """The cross-process lock for one index pass, next to the store it writes.

        Derived from the config, NOT from the opened store: the store opens
        inside the first pass, so a lock that waited for it would have let
        exactly that pass -- the first one of every process -- run unguarded.

        thread_local=False: a full rebuild takes it in a worker thread and lets
        go on the event loop, and filelock counts per thread by default -- the
        release would not have released anything.
        """
        if not self.config.get("enable_semantic_search", True):
            return None
        persist_path = self._persist_path()
        persist_path.mkdir(parents=True, exist_ok=True)
        return FileLock(str(persist_path / _PASS_LOCK_NAME), thread_local=False)

    async def _run_index_pass(self, status_callback=None, incremental=True):
        """Full or incremental pass over the SEMANTIC index.

        What is tracked besides the vectors is one mtime and one content hash
        per file, which is what makes the incremental pass incremental and lets
        identical files share their documents.

        The walk is the same pruning walk the text search uses, with the same
        excludes and the same ignore files. Two walkers meant two readings of
        "which files exist", and the old one still descended into every
        excluded directory.

        WHY IT CHECKPOINTS. Measured on 12.11.2026 on the coder tree: 44.563
        indexable files, 735.841 documents -- about 100 minutes of embedding.
        The pass used to hold every document in memory and write the
        bookkeeping once, at its very end. No process lived that long, every
        restart began again from nothing, and the index stayed "being built"
        for good (its state file was from 18.09.). Now each batch is embedded,
        stored and booked as it fills, the state is written every
        ``_STATE_SAVE_SECONDS``, and the next process carries on where this
        one stopped (see _load_state for the store and the state disagreeing).

        WHY IDENTICAL FILES ARE INDEXED ONCE. 88 % of those documents were
        nine checkouts of the same repository under data/workspace. Indexed
        nine times, every question found the same function nine times over and
        the build took nine times as long. A file whose content an indexed file
        already has is booked as an alias of it: no documents of its own, and a
        hit on the original names it (``same_content``). Files are walked
        shallowest first, so the original is the one nearest a root. When an
        original changes or goes, one of its aliases is indexed in its place.

        Args:
            status_callback: Optional callback function for progress updates
            incremental: If True, only update modified files (default). If False, full rebuild.
        """
        logger.info("=" * 60)
        full = not (incremental and self.file_mtimes)
        logger.info("FILE_OPS: Starting %s...",
                    "full index rebuild" if full else "incremental index update")
        logger.info("=" * 60)
        start_time = time.time()
        if status_callback:
            await status_callback("Starting full index rebuild..." if full
                                  else "Starting incremental index update...")

        semantic_enabled = self.config.get("enable_semantic_search", True)
        if semantic_enabled:
            self._init_vector_store()
        if full:
            self.file_mtimes, self.file_symbol_ids = {}, {}
            self.file_hashes, self.file_aliases = {}, set()
            if semantic_enabled and self._vector_store:
                await self._clear_collection(status_callback)
        store_ready = bool(semantic_enabled and self._vector_store)

        paths = await asyncio.to_thread(self._walk_for_index)
        seen = {path for path, _ in paths}
        self._progress = (0, len(paths))
        max_size = int(self.config.get("max_file_size_for_indexing_kb", 1024)) * 1024

        pending: List[_PendingFile] = []
        to_delete: List[str] = []
        pending_docs = 0
        updated = removed = 0
        last_save = time.time()
        known: Dict[str, Path] = {}   # content hash -> the file whose documents it has

        def forget(path: Path) -> None:
            """Take *path* out of the bookkeeping; its documents go with the next flush."""
            to_delete.extend(self.file_symbol_ids.pop(path, []))
            self.file_mtimes.pop(path, None)
            digest = self.file_hashes.pop(path, None)
            self.file_aliases.discard(path)
            if digest and known.get(digest) == path:
                del known[digest]

        async def flush() -> None:
            nonlocal pending_docs, last_save
            if store_ready and to_delete:
                # Before the adds: a changed file's new documents reuse the
                # ids of its old ones ("path#line").
                await asyncio.to_thread(self._vector_store.delete,
                                        self._collection_name, list(to_delete))
            to_delete.clear()
            if store_ready:
                ids = [doc_id for item in pending for doc_id in item.ids]
                texts = [text for item in pending for text in item.texts]
                metadatas = [meta for item in pending for meta in item.metadatas]
                # No try/except: a batch that fails (model not loadable, store
                # error, disk full) used to be logged and booked as written --
                # and no later pass looked at those files again.
                for i in range(0, len(ids), EMBED_BATCH_SIZE):
                    await asyncio.to_thread(self._add_embedded, ids[i:i + EMBED_BATCH_SIZE],
                                            texts[i:i + EMBED_BATCH_SIZE],
                                            metadatas[i:i + EMBED_BATCH_SIZE])
            if self._closing:
                # _add_embedded wrote nothing once stop() began: booking these
                # would claim documents the store does not hold.
                return
            # Booked only now that the documents are in the store.
            for item in pending:
                self.file_mtimes[item.path] = item.mtime
                self.file_symbol_ids[item.path] = item.ids
                self.file_hashes[item.path] = item.digest
            pending.clear()
            pending_docs = 0
            if store_ready and time.time() - last_save >= _STATE_SAVE_SECONDS:
                await asyncio.to_thread(self._save_state, False)
                last_save = time.time()

        def originals() -> Dict[str, Path]:
            return {digest: path for path, digest in self.file_hashes.items()
                    if digest and path not in self.file_aliases}

        known.update(originals())
        done = 0
        for offset in range(0, len(paths), _PREPARE_BATCH):
            if self._closing:
                return
            chunk = paths[offset:offset + _PREPARE_BATCH]
            prepared = await asyncio.to_thread(
                self._prepare_for_index, chunk, max_size, store_ready)
            for (path, base_dir), (kind, mtime, digest, item) in zip(chunk, prepared):
                done += 1
                if kind == "same":
                    continue
                if kind in ("gone", "large", "binary"):
                    # Growing past the ceiling means leaving the index, not
                    # freezing in it: just skipping the file left its old
                    # documents in the store and its mtime in the bookkeeping,
                    # and the index kept answering with rewritten code.
                    if path in self.file_mtimes:
                        forget(path)
                        updated += 1
                    continue
                old_digest = self.file_hashes.get(path)
                if path in self.file_mtimes:
                    if old_digest == digest and digest:
                        # Touched, not changed: the documents stand.
                        self.file_mtimes[path] = mtime
                        continue
                    if (digest and not old_digest and self.file_mtimes[path] == mtime
                            and path not in self.file_aliases
                            and known.get(digest, path) == path):
                        # Booked before hashes were kept, and unchanged: its
                        # documents stand, and its hash is learned.
                        self.file_hashes[path] = digest
                        known[digest] = path
                        continue
                    updated += 1
                    forget(path)
                original = known.get(digest)
                if digest and original is not None and original != path:
                    self.file_mtimes[path] = mtime
                    self.file_symbol_ids[path] = []
                    self.file_hashes[path] = digest
                    self.file_aliases.add(path)
                    continue
                if digest:
                    known[digest] = path
                pending.append(item)
                pending_docs += len(item.ids)
                if pending_docs >= EMBED_BATCH_SIZE:
                    await flush()
            self._progress = (done, len(paths))
            if status_callback:
                await status_callback(f"Indexed {done} of {len(paths)} files...")

        for path in [p for p in self.file_mtimes if p not in seen]:
            forget(path)
            removed += 1
        await flush()

        # Aliases whose original changed or went -- in this pass, or in one
        # that died before its original was stored -- have no documents: the
        # shallowest of each is indexed in its place.
        known = originals()
        heirs: Dict[str, Path] = {}
        for path, digest in self.file_hashes.items():
            if path in self.file_aliases and digest not in known:
                best = heirs.get(digest)
                if best is None or (len(path.parts), str(path)) < (len(best.parts), str(best)):
                    heirs[digest] = path
        base_of = dict(paths)
        queue = sorted(heirs.items(), key=lambda item: str(item[1]))
        for offset in range(0, len(queue), _PREPARE_BATCH):
            if self._closing:
                return
            chunk = queue[offset:offset + _PREPARE_BATCH]
            prepared = await asyncio.to_thread(
                self._prepare_for_index,
                [(heir, base_of.get(heir, heir.parent)) for _, heir in chunk],
                max_size, store_ready, True)
            for (digest, heir), (kind, _, new_digest, item) in zip(chunk, prepared):
                self.file_aliases.discard(heir)
                if kind == "read" and new_digest == digest:
                    pending.append(item)
                    pending_docs += len(item.ids)
                else:
                    forget(heir)   # changed meanwhile: the next pass takes it as new
            if pending_docs >= EMBED_BATCH_SIZE:
                await flush()
        await flush()

        if self._closing:
            return
        # The caller holds `_index_lock` for the whole pass, so the bookkeeping
        # is changed by nobody else meanwhile.
        self._index_built = True
        self._index_error = None
        self._progress = None
        if store_ready:
            await asyncio.to_thread(self._save_state, True)

        final_msg = (f"FILE_OPS: index pass complete: {len(paths)} files walked, "
                     f"{updated} updated, {removed} removed, "
                     f"{len(self.file_aliases)} identical to another, "
                     f"in {time.time() - start_time:.2f}s")
        logger.info("=" * 60)
        logger.info(final_msg)
        logger.info("=" * 60)
        if status_callback:
            await status_callback(final_msg)

    async def _clear_collection(self, status_callback=None) -> None:
        """Empty the collection before a full rebuild."""
        # An index that cannot even be counted is unreadable — persisted by an
        # older chromadb, say ('PersistentData' object has no attribute
        # 'max_seq_id'). Dropping and recreating it IS the recovery. What must
        # not happen is carrying on regardless: the rebuild then writes into a
        # broken collection, and semantic search answers "found nothing"
        # instead of "index is broken".
        try:
            count = self._vector_store.count(self._collection_name)
        except Exception as e:
            logger.warning(
                f"FILE_OPS: semantic index '{self._collection_name}' is unreadable "
                f"({e}) — recreating it")
            count = None
        if count is None or count > 0:
            if count:
                logger.info(f"FILE_OPS: Clearing {count} existing documents from index...")
                if status_callback:
                    await status_callback(f"Clearing {count} existing documents from index...")
            self._vector_store.delete_collection(self._collection_name)
            self._vector_store.get_or_create_collection(self._collection_name)
            try:
                self._vector_store.count(self._collection_name)
            except Exception as e:
                raise VectorStoreError(
                    f"semantic index '{self._collection_name}' is unreadable and could "
                    f"not be recreated: {e}") from e
            logger.info("FILE_OPS: Cleared existing documents from VectorStore")

    def _walk_for_index(self) -> List[Tuple[Path, Path]]:
        """(file, the root it was found under), shallowest first. Blocking: a worker thread runs it."""
        options = self._search_backend_options()
        configured = [Path(d).resolve() for d in self.allowed_dirs]
        found: List[Tuple[Path, Path]] = []
        for base_dir in textsearch.deduplicate_roots(self.allowed_dirs):
            if not base_dir.exists():
                continue
            for path in textsearch.walk_files(
                    base_dir, options["excludes"],
                    include_hidden=options["include_hidden"],
                    respect_ignore=True,
                    protected=textsearch.sub_roots(base_dir, configured)):
                found.append((path, base_dir))
        found.sort(key=lambda item: (len(item[0].parts), str(item[0])))
        return found

    def _prepare_for_index(self, chunk: List[Tuple[Path, Path]], max_size: int,
                           embed: bool, force: bool = False
                           ) -> List[Tuple[str, float, str, Optional["_PendingFile"]]]:
        """Stat, read and cut a batch of files into documents -- off the event loop.

        Returns ``(kind, mtime, content hash, documents)`` per file, kind one
        of same / gone / large / binary / read. *force* reads a file whose
        mtime is booked (an alias taking an original's place).
        """
        out: List[Tuple[str, float, str, Optional[_PendingFile]]] = []
        for path, base_dir in chunk:
            try:
                stat = path.stat()
            except OSError:
                out.append(("gone", 0.0, "", None))
                continue
            if stat.st_size > max_size:
                out.append(("large", stat.st_mtime, "", None))
                continue
            # A booked file without a hash (a state from before they were
            # kept) is read once more: identical files are found by it.
            if (not force and self.file_mtimes.get(path) == stat.st_mtime
                    and self.file_hashes.get(path)):
                out.append(("same", stat.st_mtime, "", None))
                continue
            if not self._is_text_file(path):
                out.append(("binary", stat.st_mtime, "", None))
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                out.append(("gone", 0.0, "", None))
                continue
            digest = hashlib.sha1(content.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()
            item = _PendingFile(path, stat.st_mtime, digest)
            if embed and content:
                # One document per symbol, not per file: the model reads 256
                # tokens, so a whole file would be indexed by its head alone
                # (see `symbols`). The path relative to its root: the part a
                # question can name.
                for doc in symbols.documents(_relative_to(path, base_dir), content):
                    item.ids.append(f"{path}#{doc.line}")
                    item.texts.append(doc.text)
                    item.metadatas.append({
                        "file_path": str(path),
                        "filename": path.name,
                        "extension": path.suffix,
                        "size_bytes": str(stat.st_size),
                        "mtime": str(stat.st_mtime),
                        "line": str(doc.line),
                        "symbol": doc.label,
                    })
            out.append(("read", stat.st_mtime, digest, item))
        return out

    def _add_embedded(self, ids: List[str], documents: List[str],
                      metadatas: List[Dict[str, Any]]) -> None:
        """Embed a batch and hand the vectors to the store.

        Blocking on purpose: the caller runs it in a worker thread.
        """
        # Checked here because this runs in a thread that shutdown cannot
        # interrupt: without it a late batch reopens the store that stop() has
        # just closed.
        if not ids or not self._vector_store or self._closing:
            return
        self._vector_store.add(
            collection=self._collection_name,
            ids=ids,
            documents=documents,
            embeddings=compute_embeddings(documents),
            metadatas=metadatas,
        )

    def _load_state(self, mend: bool = False) -> None:
        """Read back which files are already in the store, and as what.

        Without this every restart of the API was a full rebuild: the vectors
        survived on disk, the knowledge of what they cover did not, so the
        first semantic search paid for the whole tree again (measured on this
        repository: 51.730 documents, 423 s).

        A state that disagrees with its store is MENDED, not thrown away. It
        used to be thrown away, and the first pass then cleared the store and
        began again from nothing -- which is what an index that checkpoints
        meets after every process that dies between a batch and the next state
        write. Mending keeps the promise the old check kept: the bookkeeping
        claims nothing that is not in the store. A file with a document the
        store lacks is dropped (its other documents too) and indexed again by
        the next pass; documents nobody books are deleted. Only an instance
        that builds may mend -- a reader of a shared index must not write it,
        and keeps the old answer: nothing trusted.

        And only with *mend*, which the pass sets while it holds the pass lock.
        Without the lock -- the load when the store opens -- another process
        may be in the middle of a pass whose last checkpoint is up to 30 s old:
        its newest documents look unbooked, and deleting them is the one thing
        a mend must not do. That load uses the state as it stands, marked
        unfinished; the next pass mends it.
        """
        if not (self._vector_store and self._state_path
                and self._state_path.is_file()):
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            files = raw["files"]
            booked = {doc_id for entry in files.values() for doc_id in entry["ids"]}
            count = self._vector_store.count(self._collection_name)
        except Exception as e:
            logger.warning(f"FILE_OPS: index state unreadable ({e}) — rebuilding")
            return
        # Documents of another format would sit next to the new ones: only
        # the files that change are re-embedded, and the ranking would compare
        # vectors of two kinds of text.
        if raw.get("format") != symbols.DOCUMENT_FORMAT:
            logger.info(f"FILE_OPS: index built from document format {raw.get('format', 1)}, "
                        f"now {symbols.DOCUMENT_FORMAT} — rebuilding")
            return

        complete = bool(raw.get("complete", True))  # states before checkpoints were written at the end
        if count != len(booked) and not self.config.get("enable_indexing", True):
            logger.info(f"FILE_OPS: index state books {len(booked)} documents, the store "
                        f"holds {count} — not trusted by this reader")
            return
        if count != len(booked) and not mend:
            complete = False
        elif mend and (count != len(booked) or not complete):
            # The ids, not only their number, once a build died or the counts
            # differ: a document missing and one unbooked cancel out in a count.
            try:
                present = set(self._vector_store.list_ids(self._collection_name))
                if count and not present:
                    # list_ids answers [] when it cannot read: mending
                    # from that would drop every file and rebuild it all.
                    raise VectorStoreError("the store's ids could not be listed")
                dropped = [p for p, entry in files.items()
                           if any(doc_id not in present for doc_id in entry["ids"])]
                stray = present - booked
                for path in dropped:
                    stray.update(doc_id for doc_id in files.pop(path)["ids"]
                                 if doc_id in present)
                stray_ids = sorted(stray)
                for i in range(0, len(stray_ids), EMBED_BATCH_SIZE):
                    self._vector_store.delete(self._collection_name,
                                              stray_ids[i:i + EMBED_BATCH_SIZE])
            except Exception as e:
                logger.warning(f"FILE_OPS: index state could not be mended ({e}) — used "
                               "as it stands, mended at the next pass")
                complete = False
            else:
                logger.info(f"FILE_OPS: index state mended — {len(dropped)} files to index "
                            f"again, {len(stray)} documents nobody books deleted")
                complete = complete and not dropped

        self.file_mtimes = {Path(p): entry["mtime"] for p, entry in files.items()}
        self.file_symbol_ids = {Path(p): entry["ids"] for p, entry in files.items()}
        self.file_hashes = {Path(p): entry.get("hash", "") for p, entry in files.items()}
        self.file_aliases = {Path(p) for p, entry in files.items() if entry.get("alias")}
        self._index_built = complete
        logger.info(f"FILE_OPS: index state loaded — {len(files)} files, "
                    f"{sum(len(e['ids']) for e in files.values())} documents"
                    f"{'' if complete else ', a build still to finish'}")

    def _save_state(self, complete: bool = True) -> None:
        """Persist mtimes, hashes and document ids next to the vectors.

        *complete* is False for the checkpoints a pass writes on its way: a
        process that reads such a state answers, and says the index is still
        being built.
        """
        if not self._vector_store or not self._state_path:
            return
        try:
            payload = {
                "collection": self._collection_name,
                "format": symbols.DOCUMENT_FORMAT,
                "complete": complete,
                "files": {
                    str(path): {"mtime": mtime,
                                "ids": self.file_symbol_ids.get(path, []),
                                "hash": self.file_hashes.get(path, ""),
                                **({"alias": True} if path in self.file_aliases else {})}
                    for path, mtime in list(self.file_mtimes.items())
                },
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._state_path)  # never a half-written state file
        except Exception as e:
            logger.warning(f"FILE_OPS: could not persist index state: {e}")

    def _is_text_file(self, path: Path) -> bool:
        """Whether the file should be indexed for text search.

        ONE implementation, shared with the index-free search in
        ``textsearch``: two readings of "is this text" drift apart, and the
        drift shows up as a file that one tool finds and the other does not.
        The rule, and why it is decided by content rather than by extension,
        lives there.
        """
        return textsearch.is_text_file(path)

    async def _read_content(self, file_path: Path) -> Optional[str]:
        """File content for the vector store, or None when unreadable."""
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return await f.read()
        except Exception as e:
            logger.debug(f"Failed to read {file_path} for indexing: {e}")
            return None

    async def _ensure_index_fresh(self):
        """Pick up what changed since the last pass — never build from scratch.

        The FIRST build belongs to the background indexer, and only there.
        Doing it here made the first semantic search of a process pay for the
        whole tree while the agent waited (measured on this repository: 423 s
        for 51.730 documents). An incremental pass is a different animal: it
        stats the tree and reads only what changed, which is a second or two —
        and without it an agent cannot find the code it wrote a minute ago.
        """
        # A consumer, not a builder: `enable_indexing: false` is how an
        # instance says it only READS an index another one keeps (the coder's
        # read-write twin does this). Updating it here anyway would put two
        # writers on one collection, which is the concurrent rebuild the
        # sharing was meant to avoid.
        if not self.config.get("enable_indexing", True):
            return
        if not self.file_mtimes:
            return

        # Check if enough time has passed since last update (e.g., 30 seconds)
        current_time = time.time()
        last_update = getattr(self, '_last_incremental_update', 0)

        # Don't update too frequently (every 30 seconds max)
        if current_time - last_update > 30:
            try:
                logger.debug("FILE_OPS: Running quick incremental index update...")
                # Only stamp a pass that RAN. A dropped one (another pass was
                # already in flight) used to reset the clock all the same, so
                # for the next 30 s nothing picked up what the running pass had
                # already walked past -- the file the agent just wrote.
                if await self.rebuild_index(incremental=True):
                    self._last_incremental_update = current_time
            except Exception as e:
                logger.warning(f"FILE_OPS: Incremental update failed: {e}")
                # It may be the pass that carries an unfinished build on.
                self._index_error = f"{type(e).__name__}: {e}"[:300]

    def _search_backend_options(self) -> Dict[str, Any]:
        """Configuration both index-free searches share.

        In one place on purpose: glob and grep used to carry their own reading
        of what to skip, and a file that one tool found was invisible to the
        other.
        """
        return {
            "excludes": tuple(self.config.get("exclude_patterns")
                              or textsearch.DEFAULT_EXCLUDES),
            "include_hidden": bool(self.config.get("search_hidden", False)),
        }

    def _with_ripgrep(self, search, *args, **kwargs):
        """Run *search* with the ripgrep this instance is configured for.

        Called in the worker thread, not before it: finding ripgrep may mean
        downloading it once, and that must not hold the event loop.
        """
        return search(*args, rg=ripgrep.locate(self.config.get("ripgrep_path"),
                                               bool(self.config.get("ripgrep_download", True))),
                      **kwargs)

    def _max_search_filesize_bytes(self) -> int:
        """Ceiling for files the CONTENT search opens.

        Grep only. A glob never opens a file — it matches paths — so handing
        it a size ceiling would be a parameter that means nothing, and a
        parameter that means nothing is the kind of thing someone later
        "fixes" by making it do something.

        The guard used to apply to indexing alone. Now that grep reads files
        directly it has to hold there too, or the configured ceiling quietly
        stops bounding anything.
        """
        return int(self.config.get("max_file_size_for_indexing_kb", 1024)) * 1024

    async def search_files(self, pattern: str, max_results: int = 50,
                           include_ignored: bool = False) -> Dict[str, Any]:
        """
        Find files by glob pattern — without touching the index.

        The index is NOT consulted here, and that is the fix: this call used to
        await a full index build, and measured on this repository with the
        coder configuration it had not returned after 150 seconds. The same
        glob answers in 1.6 seconds when nobody indexes first -- and in a
        fifth of a second through ripgrep (see textsearch). What the index
        still serves is semantic search, which needs a corpus.

        Args:
            pattern: Glob pattern (e.g., '*.py', '**/*.test.ts', 'src/**/*.py').
                    Patterns may include path components and are matched
                    relative to each allowed directory.
            max_results: Maximum number of files to return
            include_ignored: Also search what .gitignore and the configured
                    exclude patterns hide. Slower, and the honest answer to
                    "are you sure it is not there?"

        Returns:
            Dict with status, files, total_found, truncated, skipped
        """
        try:
            logger.debug("FILE_OPS: search_files pattern=%r max_results=%s",
                         pattern, max_results)
            # A worker thread, not the event loop: the walk is blocking disk
            # I/O, and with include_ignored it runs for seconds — every other
            # tool call of this server would wait that long.
            return await asyncio.to_thread(
                self._with_ripgrep, textsearch.find_files,
                self.allowed_dirs, pattern,
                max_results=max_results,
                include_ignored=include_ignored,
                **self._search_backend_options(),
            )

        except Exception as e:
            logger.error(f"File search error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def grep_search(
        self,
        query: str,
        is_regex: bool = False,
        include_pattern: Optional[str] = None,
        case_sensitive: bool = False,
        max_results: int = 100,
        context_lines: int = 2,
        include_ignored: bool = False
    ) -> Dict[str, Any]:
        """
        Search file contents — without touching the index.

        Index-free for the same reason as :meth:`search_files`: the wait for a
        fresh index was the cost, not the search. ripgrep reads the files when
        one is found, the pruning walk when not (see textsearch).

        Args:
            query: Search query (literal or regex)
            is_regex: Treat query as regex pattern
            include_pattern: Only search files matching glob
            case_sensitive: Case-sensitive search
            max_results: Maximum matches to return
            context_lines: Lines of context before/after
            include_ignored: Also search ignored and excluded files

        Returns:
            Dict with status, matches, total_matches, total_files, truncated,
            skipped
        """
        try:
            return await asyncio.to_thread(   # see search_files: not on the loop
                self._with_ripgrep, textsearch.grep,
                self.allowed_dirs, query,
                is_regex=is_regex,
                case_sensitive=case_sensitive,
                include_pattern=include_pattern,
                context_lines=context_lines,
                max_results=max_results,
                include_ignored=include_ignored,
                max_filesize_bytes=self._max_search_filesize_bytes(),
                **self._search_backend_options(),
            )

        except Exception as e:
            logger.error(f"Grep search error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def semantic_search(
        self,
        query: str,
        max_results: int = 10,
        filter_pattern: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Semantic/AI-powered search using ChromaDB embeddings.

        Finds files by meaning rather than exact keyword match.
        Example: "authentication logic" will find login/verify functions.

        Args:
            query: Natural language search query
            max_results: Maximum number of results to return
            filter_pattern: Optional file pattern to filter results (e.g., '*.py')

        Returns:
            Dict with status, results (list of matches with similarity scores)
        """
        try:
            # Disabled means disabled, said out loud. Carrying on used to walk
            # every allowed root, open a vector store, and query an empty
            # collection — the answer was "No files found", which reads as a
            # verdict about the code rather than about the config. The file_ops
            # server switches semantic search off unless it is configured, so
            # this is the path every default instance takes.
            if not self.config.get("enable_semantic_search", True):
                return {
                    "status": "error",
                    "error": ("Semantic search is disabled for this instance "
                              "(search.enable_semantic_search). Use grep_search "
                              "or search_files instead."),
                    "error_type": "SemanticSearchDisabled",
                }

            # Initialize VectorStore if needed
            if not self._vector_store_initialized:
                self._init_vector_store()

            # Semantic search is the ONLY consumer of the index since glob and
            # grep stopped waiting for it, so this is where `enable_indexing`
            # and `index_on_startup` still take effect — without this call the
            # background indexer would never start at all and the two config
            # keys would be promises that do nothing.
            self._ensure_indexing_started()

            if not self._vector_store:
                return {
                    "status": "error",
                    "error": "Semantic search not available (VectorStore not initialized)",
                    "error_type": "VectorStoreNotInitialized"
                }

            # An index that is not finished yet is a state, not an answer.
            # Waiting for the first build here is what made this tool unusable
            # on a code tree, and returning "no matches" would read as a
            # verdict about the code.
            #
            # NOT `count() > 0`: the first batch of a full build lands after
            # ~30 s, so from then on the store is non-empty while 96 % of the
            # tree is still missing, and every search in those minutes answered
            # confidently out of whatever happened to be walked first. An
            # instance that builds knows better: its bookkeeping names exactly
            # the files whose documents are stored. It answers from those as
            # soon as there are any, and says that the rest is still missing
            # (`index_complete`) -- refusing until the end was the other half
            # of the failure: on the coder tree the end never came. One that
            # only reads has nothing but the count.
            builds = bool(self.config.get("enable_indexing", True))
            if builds:
                ready = self._index_built or bool(self.file_mtimes)
            else:
                # count() takes the store's lock, which the indexing thread
                # holds for a whole 2000-document batch -- on the event loop
                # that would stall every other request of this process.
                ready = bool(await asyncio.to_thread(
                    self._vector_store.count, self._collection_name))
            if not ready:
                if builds and self._index_error:
                    # "Try again in a minute" is a promise only a build that
                    # is still on its way keeps.
                    detail = (f"Building the semantic index failed: {self._index_error}. "
                              "It is retried every minute, but it will not finish "
                              "while that cause stays. Use grep_search.")
                elif builds and self.config.get("index_on_startup", True):
                    progress = (f" ({self._progress[0]} of {self._progress[1]} files read)"
                                if self._progress else "")
                    detail = (f"The semantic index is being built in the background{progress}. "
                              "Use grep_search now and try again in a minute.")
                elif builds:
                    # index_on_startup: false means nothing ever starts it.
                    # Promising a build that no one will run is worse than
                    # naming the setting that switched it off.
                    detail = ("No index is being built for this instance "
                              "(search.index_on_startup is false), so semantic "
                              "search stays empty. Use grep_search.")
                else:
                    # A reader, not a builder. Saying "it is being built" here
                    # would be a promise nothing in this instance keeps: the
                    # collection belongs to whichever instance has
                    # enable_indexing, and it fills once THAT one is asked.
                    detail = (f"This instance only reads the shared index "
                              f"'{self._collection_name}', which another instance "
                              f"builds, and it is still empty. Use grep_search.")
                return {"status": "error", "error": detail,
                        "error_type": "IndexNotReady"}

            # Kicked off, not awaited. Measured live on 18.09.2026: an
            # incremental pass over the coder tree takes 8.3 s even when
            # NOTHING changed, because it stats 3.648 files -- so awaiting it
            # here made every search that landed outside the 30 s window a
            # ten-second tool call. The pass still runs; the answer comes from
            # the index as it stands, which is at most one pass behind.
            self._fresh_task = asyncio.create_task(self._ensure_index_fresh())

            # Perform semantic search
            # The SAME embedding path the index was built with. Letting the
            # store embed the query would compare vectors from two models --
            # the results still look plausible, they are just reordered.
            store = self._vector_store

            # A filter selects from what came back, it does not narrow the
            # search -- so asking for the 2 best and then keeping only the .py
            # among them returns nothing whenever the 2 best are YAML. Seen
            # live on 18.09.2026: max_results 2 plus "*.py" answered count 0,
            # and the agent read that as "this code does not exist". Fetching
            # deeper when a filter is set costs one query and makes the
            # parameter mean what it looks like it means.
            fetch = min(max_results * 10, 200) if filter_pattern else max_results

            def run_query():
                return store.query(
                    collection=self._collection_name,
                    query_embedding=compute_embeddings([query])[0],
                    n_results=fetch,
                    include=["documents", "metadatas", "distances"],
                )

            try:
                results = await asyncio.to_thread(run_query)
            except Exception as e:
                # A second instance reading the same collection holds its own
                # cached handle to it. When the instance that OWNS the index
                # runs a full rebuild, it drops the collection and creates a
                # new one -- and the reader's handle then points at something
                # that no longer exists. Fetching it again is the whole repair,
                # and it only happens on the pass that rebuilt from scratch.
                logger.info(f"FILE_OPS: query failed ({e}) — reopening "
                            f"'{self._collection_name}' and trying once more")
                await asyncio.to_thread(store.refresh_collection,
                                        self._collection_name)
                results = await asyncio.to_thread(run_query)

            if not results["ids"] or len(results["ids"]) == 0:
                return self._mark_partial({
                    "status": "success",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": "No files found"
                })

            # Format results - VectorStore returns list-of-lists for batch query interface
            # Extract first batch since we only query once
            raw_ids = results.get("ids", [])
            raw_metadatas = results.get("metadatas", [])
            raw_distances = results.get("distances", [])
            
            # Handle nested format: [[id1, id2], ...] -> [id1, id2]
            ids = raw_ids[0] if raw_ids and isinstance(raw_ids[0], list) else raw_ids
            metadatas = raw_metadatas[0] if raw_metadatas and isinstance(raw_metadatas[0], list) else raw_metadatas
            distances = raw_distances[0] if raw_distances and isinstance(raw_distances[0], list) else raw_distances
            
            matches = []
            # Files with the same content as a hit: they have no documents of
            # their own (see _run_index_pass), so a hit names them.
            twins: Dict[str, List[str]] = {}
            if self.file_aliases:
                by_digest: Dict[str, List[str]] = {}
                for alias in self.file_aliases:
                    by_digest.setdefault(self.file_hashes.get(alias, ""), []).append(str(alias))
                for original, digest in self.file_hashes.items():
                    if original not in self.file_aliases and digest in by_digest:
                        twins[str(original)] = sorted(by_digest[digest])

            gone = 0
            for i in range(len(ids)):
                metadata = metadatas[i] if i < len(metadatas) else {}
                # The id carries the line ("path#42"); the path itself comes
                # from the metadata, so a filter still matches on the file.
                file_path = metadata.get("file_path") or str(ids[i]).rsplit("#", 1)[0]
                distance = distances[i] if i < len(distances) else 0.0
                # The hit is reported at the first of its places that the
                # filter wants and that still exists: a document can outlive
                # its file by one pass, and a filter may name a copy.
                #
                # The filter is held against the file NAME as well as the whole
                # path: the stored path is absolute, so "test_*.py" -- the
                # obvious way to ask for tests -- matched nothing at all and
                # the tool answered with an empty result instead of saying the
                # pattern was the problem. "*.py" worked only because fnmatch
                # lets * cross separators.
                places = [file_path] + twins.get(file_path, [])
                place = next((candidate for candidate in places
                              if (not filter_pattern
                                  or fnmatch.fnmatch(candidate, filter_pattern)
                                  or fnmatch.fnmatch(Path(candidate).name, filter_pattern))
                              and Path(candidate).is_file()), None)
                if place is None:
                    gone += not any(Path(candidate).is_file() for candidate in places)
                    continue
                others = [candidate for candidate in places if candidate != place]
                file_path = place

                # Calculate similarity score from distance
                # ChromaDB uses squared L2 distance (0 = identical, larger = less similar)
                # Convert to similarity score: 1 / (1 + distance)
                # This gives range [0, 1] where 1 is perfect match, approaching 0 for very different
                similarity = 1.0 / (1.0 + distance)

                matches.append({
                    "file_path": file_path,
                    "filename": metadata.get("filename", Path(file_path).name),
                    # What makes a hit actionable: the line to read and the
                    # name of what sits there. Without them the caller gets a
                    # file and has to read all of it to find out why it won.
                    "line": int(metadata.get("line", 1) or 1),
                    "symbol": metadata.get("symbol", ""),
                    "similarity_score": round(similarity, 4),
                    "distance": round(distance, 4),  # Include raw distance for debugging
                    "size_bytes": int(metadata.get("size_bytes", 0)),
                    "extension": metadata.get("extension", ""),
                    **({"same_content": others[:3], "same_content_count": len(others)}
                       if others else {}),
                })

            # Sort by similarity score (highest first), then cut to what was
            # asked for -- the deeper fetch above is for the filter, not for
            # the caller.
            matches.sort(key=lambda x: x["similarity_score"], reverse=True)
            matches = matches[:max_results]

            if filter_pattern and not matches:
                # "Nothing matched the filter" is a different answer from
                # "nothing is close to your question", and an agent that is
                # told the first will not conclude the second.
                gone_note = (f" {gone} of them are in files that are gone; the index "
                             "drops them on its next pass." if gone else "")
                return self._mark_partial({
                    "status": "success",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": (f"The {fetch} nearest symbols contain none whose "
                                f"path or name matches '{filter_pattern}'.{gone_note} "
                                f"Try the same query without the filter."),
                })

            return self._mark_partial({
                "status": "success",
                "query": query,
                "results": matches,
                "count": len(matches),
                "max_results": max_results
            })

        except VectorStoreError as e:
            logger.error(f"VectorStore semantic search error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": "VectorStoreError"
            }
        except Exception as e:
            logger.error(f"Semantic search error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    def _mark_partial(self, answer: Dict[str, Any]) -> Dict[str, Any]:
        """Say so in *answer* when the index it came from is not finished."""
        if not self._index_built and not self.config.get("enable_indexing", True):
            # A reader runs no pass, so nothing else would ever tell it that
            # the owner's build has finished since it opened the store.
            self._index_built = self._shared_build_finished()
        if not self._index_built:
            answer["index_complete"] = False
            if self._index_error:
                # "Still being built" is a promise only a build on its way keeps.
                answer["note"] = (f"The semantic index is unfinished, and its last pass "
                                  f"failed: {self._index_error}. It is retried; these "
                                  "results cover only the files indexed so far. "
                                  "grep_search searches everything.")
            else:
                progress = (f" ({self._progress[0]} of {self._progress[1]} files read so far)"
                            if self._progress else "")
                answer["note"] = (f"The semantic index is still being built{progress}: these "
                                  "results cover only the files indexed so far. grep_search "
                                  "searches everything.")
        return answer

    def _shared_build_finished(self) -> bool:
        """Whether the state the owning instance wrote last says its build finished.

        The head of the file is enough: ``complete`` is written before the
        megabytes of ``files``.
        """
        try:
            with open(self._state_path, "rb") as handle:
                return b'"complete": true' in handle.read(512)
        except (OSError, TypeError):
            return False

    async def stop(self):
        """Stop background indexing task and cleanup resources."""
        # Cancel background indexing
        if self._fresh_task and not self._fresh_task.done():
            self._fresh_task.cancel()
            self._fresh_task = None
        if self._indexing_task:
            self._indexing_task.cancel()
            try:
                await self._indexing_task
            except asyncio.CancelledError:
                pass
            self._indexing_task = None
            self._indexing_started = False

        # Cancelling the task above does NOT stop the worker thread it was
        # waiting on -- asyncio.to_thread cannot be interrupted, so a batch of
        # 2000 documents keeps writing while we close the store underneath it.
        # The store would then be reopened lazily by that thread, after the
        # close that was supposed to release it. Waiting for the pass to leave
        # the lock is what makes the shutdown ordered.
        self._closing = True
        try:
            await asyncio.wait_for(self._index_lock.acquire(), timeout=30)
            self._index_lock.release()
        except (asyncio.TimeoutError, RuntimeError) as e:
            logger.warning(f"FILE_OPS: index pass did not finish before shutdown: {e}")

        # Cleanup VectorStore resources
        if self._vector_store:
            try:
                self._vector_store.close()
                self._vector_store = None
                self._vector_store_initialized = False
            except Exception as e:
                logger.debug(f"VectorStore cleanup warning: {e}")

        self.file_mtimes.clear()
        self.file_hashes.clear()
        self.file_aliases.clear()
