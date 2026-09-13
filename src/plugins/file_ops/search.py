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
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiofiles

from agent_system.utils.vector_store import VectorStore, VectorStoreError

from . import textsearch


logger = logging.getLogger(__name__)


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

        # Indexing control
        self._indexing_task: Optional[asyncio.Task] = None
        self._index_lock = asyncio.Lock()
        self._indexing_started = False

        # VectorStore for semantic search (replaces direct ChromaDB usage)
        self._vector_store: Optional[VectorStore] = None
        self._vector_store_initialized = False
        self._collection_name = "file_ops_semantic_index"

    def _ensure_indexing_started(self):
        """Start background indexing if not already started and if enabled."""
        if (not self._indexing_started and
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
            
            # Get persist path from config or use default
            persist_path = Path(self.config.get(
                "chroma_db_path",  # Keep old config name for compatibility
                "data/cache/file_ops_chromadb"
            ))
            
            self._vector_store = VectorStore(persist_path=persist_path)
            
            # Force recreate collection if configured
            if self.config.get("force_recreate_chroma_collection", False):
                logger.info(f"FILE_OPS: Force recreating collection '{self._collection_name}'")
                self._vector_store.delete_collection(self._collection_name)
            
            # Initialize collection
            self._vector_store.get_or_create_collection(self._collection_name)
            
            self._vector_store_initialized = True
            logger.info(f"FILE_OPS: VectorStore initialized (backend: {self._vector_store.backend})")

        except Exception as e:
            logger.error(f"Failed to initialize VectorStore: {e}", exc_info=True)
            raise VectorStoreError(f"VectorStore initialization failed: {e}")

    async def _background_indexer(self):
        """Periodically rebuild index in background."""
        # First run: full rebuild
        first_run = True

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
        else:
            new_mtimes: Dict[Path, float] = {}

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
                        continue

                    current_mtime = stat.st_mtime

                    # For incremental: skip unchanged files
                    if incremental and file_path in new_mtimes:
                        if new_mtimes[file_path] == current_mtime:
                            files_indexed += 1
                            continue
                        files_updated += 1
                        if semantic_enabled and self._vector_store:
                            chroma_ids_to_delete.append(str(file_path))

                    if self._is_text_file(file_path):
                        new_mtimes[file_path] = current_mtime

                        # Content is read only for the vectors: without
                        # semantic search there is nothing to put it into.
                        if semantic_enabled and self._vector_store:
                            content = await self._read_content(file_path)
                            if content:
                                chroma_docs.append(content)
                                chroma_ids.append(str(file_path))
                                chroma_metadatas.append({
                                    "file_path": str(file_path),
                                    "filename": file_path.name,
                                    "extension": file_path.suffix,
                                    "size_bytes": str(size),
                                    "mtime": str(current_mtime)
                                })

                    files_indexed += 1

                    # Periodic progress updates
                    current_time = time.time()
                    if (current_time - last_progress_time) >= progress_interval:
                        if incremental:
                            logger.info(f"FILE_OPS: Indexing progress: {files_indexed} files checked, {files_updated} updated, {len(chroma_docs)} for semantic search...")
                        else:
                            logger.info(f"FILE_OPS: Indexing progress: {files_indexed} files, {len(chroma_docs)} for semantic search...")
                        if status_callback:
                            await status_callback(f"Indexed {files_indexed} files, {len(chroma_docs)} for semantic search...")
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
                        chroma_ids_to_delete.append(str(file_path))

        # Delete old VectorStore entries for updated/deleted files
        if semantic_enabled and chroma_ids_to_delete and self._vector_store:
            try:
                logger.info(f"FILE_OPS: Removing {len(chroma_ids_to_delete)} updated/deleted files from VectorStore...")
                self._vector_store.delete(self._collection_name, ids=chroma_ids_to_delete)
            except Exception as e:
                logger.warning(f"FILE_OPS: Failed to delete from VectorStore: {e}")

        # Batch insert into VectorStore (with chunking to avoid batch size limits)
        if semantic_enabled and chroma_docs and self._vector_store:
            logger.info(f"FILE_OPS: Creating embeddings for {len(chroma_docs)} files...")
            logger.info("FILE_OPS: This may take several minutes depending on file count and GPU availability")
            if status_callback:
                await status_callback(f"Creating embeddings for {len(chroma_docs)} files (this may take a while)...")
            try:
                embedding_start = time.time()

                # VectorStore handles batching internally, but we chunk large inserts
                BATCH_SIZE = 5000
                for i in range(0, len(chroma_docs), BATCH_SIZE):
                    batch_end = min(i + BATCH_SIZE, len(chroma_docs))
                    logger.info(f"FILE_OPS: Processing embedding batch {i//BATCH_SIZE + 1} ({i+1}-{batch_end} of {len(chroma_docs)})")

                    self._vector_store.add(
                        collection=self._collection_name,
                        ids=chroma_ids[i:batch_end],
                        documents=chroma_docs[i:batch_end],
                        metadatas=chroma_metadatas[i:batch_end]
                    )

                embedding_time = time.time() - embedding_start
                logger.info(f"FILE_OPS: Created embeddings for {len(chroma_docs)} files in {embedding_time:.2f}s")
            except Exception as e:
                logger.error(f"FILE_OPS: VectorStore batch insert failed: {e}", exc_info=True)

        # Atomic swap under lock
        async with self._index_lock:
            self.file_mtimes = new_mtimes

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
        """Ensure index is up-to-date with a quick incremental update if needed."""
        # If no index exists yet, build initial index
        if not self.file_mtimes:
            try:
                logger.info("FILE_OPS: No index found - building initial index...")
                await self.rebuild_index(incremental=False)
                self._last_incremental_update = time.time()
                return
            except Exception as e:
                logger.error(f"FILE_OPS: Failed to build initial index: {e}")
                return

        # Check if enough time has passed since last update (e.g., 30 seconds)
        current_time = time.time()
        last_update = getattr(self, '_last_incremental_update', 0)

        # Don't update too frequently (every 30 seconds max)
        if current_time - last_update > 30:
            try:
                logger.debug("FILE_OPS: Running quick incremental index update...")
                await self.rebuild_index(incremental=True)
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
            await self._ensure_index_fresh()

            if not self._vector_store:
                return {
                    "status": "error",
                    "error": "Semantic search not available (VectorStore not initialized)",
                    "error_type": "VectorStoreNotInitialized"
                }

            # Perform semantic search
            results = self._vector_store.query(
                collection=self._collection_name,
                query_text=query,
                n_results=max_results,
                include=["documents", "metadatas", "distances"]
            )

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
                file_path = ids[i]
                metadata = metadatas[i] if i < len(metadatas) else {}
                distance = distances[i] if i < len(distances) else 0.0

                # Filter by pattern if specified
                if filter_pattern:
                    if not fnmatch.fnmatch(file_path, filter_pattern):
                        continue

                # Calculate similarity score from distance
                # ChromaDB uses squared L2 distance (0 = identical, larger = less similar)
                # Convert to similarity score: 1 / (1 + distance)
                # This gives range [0, 1] where 1 is perfect match, approaching 0 for very different
                similarity = 1.0 / (1.0 + distance)

                matches.append({
                    "file_path": file_path,
                    "filename": metadata.get("filename", Path(file_path).name),
                    "similarity_score": round(similarity, 4),
                    "distance": round(distance, 4),  # Include raw distance for debugging
                    "size_bytes": int(metadata.get("size_bytes", 0)),
                    "extension": metadata.get("extension", "")
                })

            # Sort by similarity score (highest first)
            matches.sort(key=lambda x: x["similarity_score"], reverse=True)

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
        if self._indexing_task:
            self._indexing_task.cancel()
            try:
                await self._indexing_task
            except asyncio.CancelledError:
                pass
            self._indexing_task = None
            self._indexing_started = False

        # Cleanup VectorStore resources
        if self._vector_store:
            try:
                self._vector_store.close()
                self._vector_store = None
                self._vector_store_initialized = False
            except Exception as e:
                logger.debug(f"VectorStore cleanup warning: {e}")

        self.file_mtimes.clear()
