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
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiofiles
from filelock import FileLock
from filelock import Timeout as LockTimeout

from agent_system.utils.vector_store import (
    VectorStore,
    VectorStoreError,
    compute_embeddings,
)

from . import symbols, textsearch


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
        #: True once a pass has finished or a state file was read back. NOT
        #: `count() > 0`: the first batch of a full build lands long before the
        #: tree is covered. NOT `bool(file_mtimes)` either -- a tree with
        #: nothing indexable would then read as "still building" forever.
        self._index_built = False
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
        that has semantic search configured (file_ops, amiga_fs,
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
        if self.config.get("enable_semantic_search", True):
            try:
                self._init_vector_store()
            except VectorStoreError as e:
                logger.error(f"FILE_OPS: no vector store, indexing stops: {e}")
                return
        first_run = not self.file_mtimes

        while True:
            try:
                if first_run:
                    await self.rebuild_index(incremental=False)  # Full rebuild on first run
                    first_run = False
                else:
                    await self.rebuild_index(incremental=True)  # Incremental updates after that

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
                await asyncio.sleep(60)  # Retry after 1 minute

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
                    await asyncio.to_thread(self._load_state)
                await self._run_index_pass(status_callback, incremental)
            finally:
                if pass_lock is not None:
                    pass_lock.release()
        return True

    def _persist_path(self) -> Path:
        """Where the semantic index lives -- one answer for the store and its lock."""
        return Path(self.config.get(
            "chroma_db_path",  # Keep old config name for compatibility
            "data/cache/file_ops_chromadb"
        ))

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
        """Full or incremental rebuild of the SEMANTIC index.

        What is tracked besides the vectors is one mtime per file, which is
        what makes the incremental pass incremental. The word and filename
        indexes that used to be built here are gone: since glob and grep walk
        the disk directly nobody read them, and keeping them current was the
        expensive part — every changed file scanned every word list.

        The walk is the same pruning walk the text search uses, with the same
        excludes and the same .gitignore handling. Two walkers meant two
        readings of "which files exist", and the old one still descended into
        every excluded directory.

        Args:
            status_callback: Optional callback function for progress updates
            incremental: If True, only update modified files (default). If False, full rebuild.
        """
        logger.info("=" * 60)
        if incremental and self.file_mtimes:
            logger.info("FILE_OPS: Starting incremental index update...")
        else:
            logger.info("FILE_OPS: Starting full index rebuild...")
        logger.info("=" * 60)
        start_time = time.time()

        if status_callback:
            if incremental and self.file_mtimes:
                await status_callback("Starting incremental index update...")
            else:
                await status_callback("Starting full index rebuild...")

        # For incremental: keep what is known, only update changed files
        if incremental and self.file_mtimes:
            new_mtimes = self.file_mtimes.copy()
            new_symbol_ids = {p: list(ids) for p, ids in self.file_symbol_ids.items()}
        else:
            new_mtimes: Dict[Path, float] = {}
            new_symbol_ids: Dict[Path, List[str]] = {}

        files_indexed = 0
        files_updated = 0
        files_removed = 0

        # Semantic search via VectorStore
        semantic_enabled = self.config.get("enable_semantic_search", True)
        if semantic_enabled and not incremental:
            # Only clear for full rebuild
            logger.info("FILE_OPS: Initializing VectorStore for semantic search...")
            if status_callback:
                await status_callback("Initializing VectorStore for semantic search...")
            self._init_vector_store()
            # Clear existing semantic index (delete all documents)
            if self._vector_store:
                # An index that cannot even be counted is unreadable — persisted
                # by an older chromadb, say ('PersistentData' object has no
                # attribute 'max_seq_id'). Dropping and recreating it IS the
                # recovery. What must not happen is carrying on regardless: the
                # rebuild then writes into a broken collection, and semantic
                # search answers "found nothing" instead of "index is broken".
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
        elif semantic_enabled and incremental:
            # For incremental: just init, don't clear
            self._init_vector_store()

        max_size_kb = self.config.get("max_file_size_for_indexing_kb", 1024)
        max_size = max_size_kb * 1024

        # Collect file data for batch VectorStore insertion
        chroma_docs = []
        chroma_ids = []
        chroma_metadatas = []
        chroma_ids_to_delete = []  # For incremental updates

        # Track seen files for incremental cleanup
        seen_files: Set[Path] = set()

        # Progress tracking
        last_progress_time = time.time()
        progress_interval = 2.0  # Update every 2 seconds

        options = self._search_backend_options()
        configured = [Path(d).resolve() for d in self.allowed_dirs]
        for base_dir in textsearch.deduplicate_roots(self.allowed_dirs):
            if not base_dir.exists():
                continue

            walk = textsearch.walk_files(
                base_dir, options["excludes"],
                include_hidden=options["include_hidden"],
                ignore_spec=textsearch.gitignore_spec(base_dir),
                protected=textsearch.sub_roots(base_dir, configured))
            for file_path in walk:
                seen_files.add(file_path)

                # Skip large files
                try:
                    stat = file_path.stat()
                    size = stat.st_size
                    if size > max_size:
                        # It may have been indexed while it was smaller. Just
                        # skipping it here left its old documents in the store
                        # AND its mtime in the bookkeeping, so nothing ever
                        # touched it again: the index kept answering with code
                        # that had since been rewritten. Growing past the
                        # ceiling means leaving the index, not freezing in it.
                        if file_path in new_mtimes:
                            new_mtimes.pop(file_path)
                            if semantic_enabled and self._vector_store:
                                chroma_ids_to_delete.extend(
                                    new_symbol_ids.pop(file_path, []))
                        continue

                    current_mtime = stat.st_mtime

                    # For incremental: skip unchanged files
                    if incremental and file_path in new_mtimes:
                        if new_mtimes[file_path] == current_mtime:
                            files_indexed += 1
                            continue
                        files_updated += 1
                        if semantic_enabled and self._vector_store:
                            chroma_ids_to_delete.extend(
                                new_symbol_ids.pop(file_path, []))

                    if self._is_text_file(file_path):
                        new_mtimes[file_path] = current_mtime

                        # Content is read only for the vectors: without
                        # semantic search there is nothing to put it into.
                        if semantic_enabled and self._vector_store:
                            content = await self._read_content(file_path)
                            if content:
                                # One document per symbol, not per file: the
                                # model reads 256 tokens, so a whole file would
                                # be indexed by its head alone (see `symbols`).
                                ids_here = []
                                # The path relative to its root: the part a
                                # question can name (see `symbols`).
                                for doc in symbols.documents(
                                        _relative_to(file_path, base_dir), content):
                                    doc_id = f"{file_path}#{doc.line}"
                                    ids_here.append(doc_id)
                                    chroma_docs.append(doc.text)
                                    chroma_ids.append(doc_id)
                                    chroma_metadatas.append({
                                        "file_path": str(file_path),
                                        "filename": file_path.name,
                                        "extension": file_path.suffix,
                                        "size_bytes": str(size),
                                        "mtime": str(current_mtime),
                                        "line": str(doc.line),
                                        "symbol": doc.label,
                                    })
                                new_symbol_ids[file_path] = ids_here

                    files_indexed += 1

                    # Hand the loop back now and then. An incremental pass over
                    # an unchanged tree awaits nothing at all -- measured live
                    # on 18.09.2026 it held the event loop for 8.3 s while it
                    # stat'ed 3.648 files, and everything else this process
                    # serves waited that long.
                    if files_indexed % 200 == 0:
                        await asyncio.sleep(0)

                    # Periodic progress updates
                    current_time = time.time()
                    if (current_time - last_progress_time) >= progress_interval:
                        if incremental:
                            logger.info(f"FILE_OPS: Indexing progress: {files_indexed} files checked, {files_updated} updated, {len(chroma_docs)} symbols for semantic search...")
                        else:
                            logger.info(f"FILE_OPS: Indexing progress: {files_indexed} files, {len(chroma_docs)} symbols for semantic search...")
                        if status_callback:
                            await status_callback(f"Indexed {files_indexed} files, {len(chroma_docs)} symbols for semantic search...")
                        last_progress_time = current_time

                except (OSError, PermissionError) as e:
                    logger.debug(f"Skipping file {file_path}: {e}")
                    continue

        # For incremental: forget files that are gone
        if incremental:
            deleted_files = set(new_mtimes.keys()) - seen_files
            if deleted_files:
                files_removed = len(deleted_files)
                logger.info(f"FILE_OPS: Removing {files_removed} deleted files from index...")
                for file_path in deleted_files:
                    del new_mtimes[file_path]
                    if semantic_enabled and self._vector_store:
                        chroma_ids_to_delete.extend(
                            new_symbol_ids.pop(file_path, []))

        # Delete old VectorStore entries for updated/deleted files.
        #
        # A failure here ends the pass. Carrying on used to log a warning and
        # then commit the new bookkeeping -- which no longer lists those ids,
        # so the documents stayed in the store with nobody left to delete them
        # and the search kept answering with functions that had been renamed
        # away. Ending without committing leaves the OLD bookkeeping in place,
        # and the next pass tries the same deletion again.
        if semantic_enabled and chroma_ids_to_delete and self._vector_store:
            logger.info(f"FILE_OPS: Removing {len(chroma_ids_to_delete)} updated/deleted files from VectorStore...")
            await asyncio.to_thread(self._vector_store.delete,
                                    self._collection_name, chroma_ids_to_delete)

        # Batch insert into VectorStore (with chunking to avoid batch size limits)
        if semantic_enabled and chroma_docs and self._vector_store:
            logger.info(f"FILE_OPS: Creating embeddings for {len(chroma_docs)} symbols...")
            logger.info("FILE_OPS: This may take several minutes depending on file count and GPU availability")
            if status_callback:
                await status_callback(f"Creating embeddings for {len(chroma_docs)} symbols (this may take a while)...")
            # No try/except around this either, and for the same reason: a
            # batch that fails (model not loadable, store error, disk full)
            # used to be logged and then booked as if it had been written --
            # the state claimed documents that were never stored, and because
            # the mtimes were committed too, no later pass ever looked at those
            # files again. The running process answered "nothing found" for
            # them until it was restarted.
            embedding_start = time.time()

            # The vectors are computed HERE, in batches, and handed over
            # ready-made. Letting the store embed document by document was
            # measured at 26 ms each -- 22 minutes for this repository's
            # 51.730 documents, against seven when they go through one batched
            # encode. The work runs in a worker thread: it is minutes of CPU,
            # and on the event loop every other tool call of this server would
            # wait for it.
            for i in range(0, len(chroma_docs), EMBED_BATCH_SIZE):
                batch_end = min(i + EMBED_BATCH_SIZE, len(chroma_docs))
                logger.info(f"FILE_OPS: Processing embedding batch {i//EMBED_BATCH_SIZE + 1} ({i+1}-{batch_end} of {len(chroma_docs)})")

                await asyncio.to_thread(
                    self._add_embedded,
                    chroma_ids[i:batch_end],
                    chroma_docs[i:batch_end],
                    chroma_metadatas[i:batch_end],
                )

            embedding_time = time.time() - embedding_start
            logger.info(f"FILE_OPS: Created embeddings for {len(chroma_docs)} symbols in {embedding_time:.2f}s")

        # The caller holds `_index_lock` for the whole pass, so this swap is
        # already exclusive. Taking it again here would deadlock: asyncio.Lock
        # is not reentrant.
        self.file_mtimes = new_mtimes
        self.file_symbol_ids = new_symbol_ids
        self._index_built = True
        if semantic_enabled and self._vector_store:
            self._save_state()

        elapsed = time.time() - start_time
        if incremental and self.file_mtimes:
            final_msg = f"FILE_OPS: Incremental update complete! Checked {files_indexed} files, updated {files_updated}, removed {files_removed} in {elapsed:.2f}s"
        else:
            final_msg = f"FILE_OPS: Full index rebuild complete! Indexed {files_indexed} files in {elapsed:.2f}s"
        logger.info("=" * 60)
        logger.info(final_msg)
        logger.info("=" * 60)
        if status_callback:
            await status_callback(final_msg)

    def _add_embedded(self, ids: List[str], documents: List[str],
                      metadatas: List[Dict[str, Any]]) -> None:
        """Embed a batch and hand the vectors to the store.

        Blocking on purpose: the caller runs it in a worker thread.
        """
        # Checked here because this runs in a thread that shutdown cannot
        # interrupt: without it a late batch reopens the store that stop() has
        # just closed.
        if not self._vector_store or self._closing:
            return
        self._vector_store.add(
            collection=self._collection_name,
            ids=ids,
            documents=documents,
            embeddings=compute_embeddings(documents),
            metadatas=metadatas,
        )

    def _load_state(self) -> None:
        """Read back which files are already in the store, and as what.

        Without this every restart of the API was a full rebuild: the vectors
        survived on disk, the knowledge of what they cover did not, so the
        first semantic search paid for the whole tree again (measured on this
        repository: 51.730 documents, 423 s). The store and the state are
        written together and validated against each other below — a state file
        that does not match its collection is thrown away, never trusted.
        """
        if not (self._vector_store and self._state_path
                and self._state_path.is_file()):
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            files = raw["files"]
            stored = sum(len(entry["ids"]) for entry in files.values())
            count = self._vector_store.count(self._collection_name)
        except Exception as e:
            logger.warning(f"FILE_OPS: index state unreadable ({e}) — rebuilding")
            return

        # A store that was cleared, half-written or built by another version
        # answers with a different count. Rebuilding is cheap; answering out of
        # an index that does not hold what we think it holds is not.
        if count != stored:
            logger.info(
                f"FILE_OPS: index state says {stored} documents, the store holds "
                f"{count} — rebuilding")
            return
        # Documents of another format would sit next to the new ones: only
        # the files that change are re-embedded, and the ranking would compare
        # vectors of two kinds of text.
        if raw.get("format") != symbols.DOCUMENT_FORMAT:
            logger.info(f"FILE_OPS: index built from document format {raw.get('format', 1)}, "
                        f"now {symbols.DOCUMENT_FORMAT} — rebuilding")
            return

        self.file_mtimes = {Path(p): entry["mtime"] for p, entry in files.items()}
        self.file_symbol_ids = {Path(p): entry["ids"] for p, entry in files.items()}
        self._index_built = True
        logger.info(f"FILE_OPS: index state loaded — {len(files)} files, "
                    f"{stored} documents, no rebuild needed")

    def _save_state(self) -> None:
        """Persist mtimes and document ids next to the vectors."""
        if not self._vector_store or not self._state_path:
            return
        try:
            payload = {
                "collection": self._collection_name,
                "format": symbols.DOCUMENT_FORMAT,
                "files": {
                    str(path): {"mtime": mtime,
                                "ids": self.file_symbol_ids.get(path, [])}
                    for path, mtime in self.file_mtimes.items()
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
        glob answers in 1.6 seconds when nobody indexes first. What the index
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
                textsearch.find_files,
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
        fresh index was the cost, not the search. The candidate-file shortcut
        that the word index used to provide is gone with it; the pruning walk
        reads the files directly and is faster than assembling the index that
        would have narrowed them.

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
                textsearch.grep,
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
            # instance that builds knows better -- its bookkeeping is filled
            # only when a pass has completed (or was read back from disk). One
            # that only reads has nothing but the count.
            builds = bool(self.config.get("enable_indexing", True))
            if builds:
                ready = self._index_built
            else:
                # count() takes the store's lock, which the indexing thread
                # holds for a whole 2000-document batch -- on the event loop
                # that would stall every other request of this process.
                ready = bool(await asyncio.to_thread(
                    self._vector_store.count, self._collection_name))
            if not ready:
                if builds and self.config.get("index_on_startup", True):
                    detail = ("The semantic index is being built in the background. "
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
                return {
                    "status": "success",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": "No files found"
                }

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
            for i in range(len(ids)):
                metadata = metadatas[i] if i < len(metadatas) else {}
                # The id carries the line ("path#42"); the path itself comes
                # from the metadata, so a filter still matches on the file.
                file_path = metadata.get("file_path") or str(ids[i]).rsplit("#", 1)[0]
                distance = distances[i] if i < len(distances) else 0.0

                # Filter by pattern if specified.
                #
                # Against the file NAME as well as the whole path: the stored
                # path is absolute, so "test_*.py" -- the obvious way to ask
                # for tests -- matched nothing at all and the tool answered
                # with an empty result instead of saying the pattern was the
                # problem. "*.py" worked only because fnmatch lets * cross
                # separators.
                if filter_pattern:
                    if not (fnmatch.fnmatch(file_path, filter_pattern)
                            or fnmatch.fnmatch(Path(file_path).name, filter_pattern)):
                        continue

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
                    "extension": metadata.get("extension", "")
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
                return {
                    "status": "success",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": (f"The {fetch} nearest symbols contain none whose "
                                f"path or name matches '{filter_pattern}'. Try the "
                                f"same query without the filter."),
                }

            return {
                "status": "success",
                "query": query,
                "results": matches,
                "count": len(matches),
                "max_results": max_results
            }

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
