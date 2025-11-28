"""Background file indexing and search engine for fast file/text search."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiofiles


logger = logging.getLogger(__name__)


# Cache for available ONNX providers (computed once)
_AVAILABLE_ONNX_PROVIDERS: Optional[List[str]] = None


def _get_available_onnx_providers() -> List[str]:
    """Detect available ONNX Runtime execution providers.
    
    Returns providers in priority order: GPU first, then CPU fallback.
    Caches result for performance.
    """
    global _AVAILABLE_ONNX_PROVIDERS
    if _AVAILABLE_ONNX_PROVIDERS is not None:
        return _AVAILABLE_ONNX_PROVIDERS

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
        _AVAILABLE_ONNX_PROVIDERS = [p for p in preferred_order if p in available]
        
        # If none of our preferred are available, use whatever is available
        if not _AVAILABLE_ONNX_PROVIDERS:
            _AVAILABLE_ONNX_PROVIDERS = available if available else ["CPUExecutionProvider"]
        
        logger.info(f"Selected ONNX providers for file_ops: {_AVAILABLE_ONNX_PROVIDERS}")
        return _AVAILABLE_ONNX_PROVIDERS
        
    except ImportError:
        logger.warning("onnxruntime not installed, using CPU provider only")
        _AVAILABLE_ONNX_PROVIDERS = ["CPUExecutionProvider"]
        return _AVAILABLE_ONNX_PROVIDERS
    except Exception as e:
        logger.warning(f"Failed to detect ONNX providers: {e}, using CPU")
        _AVAILABLE_ONNX_PROVIDERS = ["CPUExecutionProvider"]
        return _AVAILABLE_ONNX_PROVIDERS


class ChromaDBError(Exception):
    """ChromaDB-related errors."""
    pass


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

        # In-memory indexes
        self.text_index: Dict[str, List[tuple[Path, int]]] = {}  # word -> [(file, position)]
        self.file_name_index: Dict[str, List[Path]] = {}  # filename_lower -> [paths]
        self.file_mtimes: Dict[Path, float] = {}  # Track modifications

        # Indexing control
        self._indexing_task: Optional[asyncio.Task] = None
        self._index_lock = asyncio.Lock()
        self._indexing_started = False

        # ChromaDB for semantic search
        self.chroma_client = None
        self.chroma_collection = None
        self._chroma_initialized = False

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

    def _init_chromadb(self):
        """Initialize ChromaDB persistent client for semantic search."""
        if self._chroma_initialized:
            return

        try:
            logger.info("FILE_OPS: Initializing ChromaDB...")
            import chromadb
            from chromadb.config import Settings
            from chromadb.utils import embedding_functions

            # Get ChromaDB path from config or use default
            chroma_path = Path(self.config.get(
                "chroma_db_path",
                "data/cache/file_ops_chromadb"
            ))
            chroma_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"FILE_OPS: ChromaDB path: {chroma_path}")

            logger.debug("FILE_OPS: Creating ChromaDB client...")
            self.chroma_client = chromadb.PersistentClient(
                path=str(chroma_path),
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
            logger.debug("FILE_OPS: ChromaDB client created")

            # Create embedding function with dynamically detected providers
            logger.debug("FILE_OPS: Creating embedding function...")
            available_providers = _get_available_onnx_providers()
            embedding_fn = embedding_functions.ONNXMiniLM_L6_V2(
                preferred_providers=available_providers
            )
            logger.debug("FILE_OPS: Embedding function created")

            # Get or create collection (don't delete existing unless force_recreate is set)
            collection_name = "file_ops_semantic_index"
            force_recreate = self.config.get("force_recreate_chroma_collection", False)

            if force_recreate:
                logger.info(f"FILE_OPS: Force recreating ChromaDB collection '{collection_name}'")
                try:
                    self.chroma_client.delete_collection(collection_name)
                    logger.debug("FILE_OPS: Deleted existing collection")
                except Exception as delete_error:
                    logger.debug(f"FILE_OPS: No existing collection to delete: {delete_error}")

            # Try to get existing collection first
            try:
                logger.debug(f"FILE_OPS: Attempting to get existing collection '{collection_name}'...")
                self.chroma_collection = self.chroma_client.get_collection(
                    name=collection_name,
                    embedding_function=embedding_fn
                )
                logger.info(f"FILE_OPS: Using existing ChromaDB collection '{collection_name}'")
            except Exception:
                # Collection doesn't exist, create it
                logger.debug(f"FILE_OPS: Creating new collection '{collection_name}'...")
                try:
                    self.chroma_collection = self.chroma_client.create_collection(
                        name=collection_name,
                        metadata={"description": "Semantic index for file content"},
                        embedding_function=embedding_fn
                    )
                    logger.info(f"FILE_OPS: Created new ChromaDB collection '{collection_name}'")
                except Exception as create_error:
                    logger.error(f"FILE_OPS: Failed to create ChromaDB collection: {create_error}")
                    raise ChromaDBError(f"Collection creation failed: {create_error}")

            self._chroma_initialized = True
            logger.info(f"FILE_OPS: ChromaDB initialized successfully at {chroma_path}")

        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}", exc_info=True)
            raise ChromaDBError(f"ChromaDB initialization failed: {e}")

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
                interval = self.config.get("auto_reindex_interval_seconds", 300)
                await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"Indexing error: {e}", exc_info=True)
                await asyncio.sleep(60)  # Retry after 1 minute

    async def rebuild_index(self, status_callback=None, incremental=True):
        """Full or incremental index rebuild with optional progress callbacks.

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

        # For incremental: keep existing indexes, only update changed files
        if incremental and self.file_mtimes:
            new_text_index = self.text_index.copy()
            new_file_index = self.file_name_index.copy()
            new_mtimes = self.file_mtimes.copy()
        else:
            new_text_index: Dict[str, List[tuple[Path, int]]] = {}
            new_file_index: Dict[str, List[Path]] = {}
            new_mtimes: Dict[Path, float] = {}

        files_indexed = 0
        files_updated = 0
        files_removed = 0

        # ChromaDB semantic index
        semantic_enabled = self.config.get("enable_semantic_search", True)
        if semantic_enabled and not incremental:
            # Only clear for full rebuild
            logger.info("FILE_OPS: Initializing ChromaDB for semantic search...")
            if status_callback:
                await status_callback("Initializing ChromaDB for semantic search...")
            self._init_chromadb()
            # Clear existing semantic index (delete all documents)
            if self.chroma_collection:
                try:
                    # Get all IDs and delete them
                    existing_data = self.chroma_collection.get()
                    if existing_data and existing_data["ids"]:
                        logger.info(f"FILE_OPS: Clearing {len(existing_data['ids'])} existing documents from index...")
                        if status_callback:
                            await status_callback(f"Clearing {len(existing_data['ids'])} existing documents from index...")
                        self.chroma_collection.delete(ids=existing_data["ids"])
                        logger.info(f"FILE_OPS: Cleared {len(existing_data['ids'])} existing documents from ChromaDB")
                except Exception as e:
                    logger.warning(f"FILE_OPS: Failed to clear ChromaDB collection: {e}")
        elif semantic_enabled and incremental:
            # For incremental: just init, don't clear
            self._init_chromadb()

        max_size_kb = self.config.get("max_file_size_for_indexing_kb", 1024)
        max_size = max_size_kb * 1024

        # Collect file data for batch ChromaDB insertion
        chroma_docs = []
        chroma_ids = []
        chroma_metadatas = []
        chroma_ids_to_delete = []  # For incremental updates

        # Track seen files for incremental cleanup
        seen_files: Set[Path] = set()

        # Progress tracking
        last_progress_time = time.time()
        progress_interval = 2.0  # Update every 2 seconds

        for base_dir in self.allowed_dirs:
            if not base_dir.exists():
                continue

            async for file_path in self._iter_files(base_dir):
                # Skip excluded patterns
                if self._is_excluded(file_path):
                    continue

                seen_files.add(file_path)

                # Skip large files
                try:
                    size = file_path.stat().st_size
                    if size > max_size:
                        continue

                    current_mtime = file_path.stat().st_mtime

                    # For incremental: skip unchanged files
                    if incremental and file_path in new_mtimes:
                        if new_mtimes[file_path] == current_mtime:
                            # File hasn't changed, skip
                            files_indexed += 1
                            continue
                        else:
                            # File modified - remove old entries
                            files_updated += 1
                            # Remove from text index
                            for word_list in new_text_index.values():
                                word_list[:] = [(p, pos) for p, pos in word_list if p != file_path]
                            # Remove from file index
                            filename_lower = file_path.name.lower()
                            if filename_lower in new_file_index:
                                new_file_index[filename_lower] = [p for p in new_file_index[filename_lower] if p != file_path]
                                # Remove key if list is empty
                                if not new_file_index[filename_lower]:
                                    del new_file_index[filename_lower]
                            # Mark for ChromaDB deletion
                            if semantic_enabled and self.chroma_collection:
                                chroma_ids_to_delete.append(str(file_path))

                    # Index filename
                    filename_lower = file_path.name.lower()
                    if filename_lower not in new_file_index:
                        new_file_index[filename_lower] = []
                    if file_path not in new_file_index[filename_lower]:
                        new_file_index[filename_lower].append(file_path)

                    # Index file content for text files
                    if self._is_text_file(file_path):
                        content = await self._index_file_content(file_path, new_text_index)
                        new_mtimes[file_path] = current_mtime

                        # Add to ChromaDB batch if semantic search enabled
                        if semantic_enabled and content and self.chroma_collection:
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

        # For incremental: remove deleted files from index
        if incremental:
            deleted_files = set(new_mtimes.keys()) - seen_files
            if deleted_files:
                files_removed = len(deleted_files)
                logger.info(f"FILE_OPS: Removing {files_removed} deleted files from index...")
                for file_path in deleted_files:
                    # Remove from mtimes
                    del new_mtimes[file_path]
                    # Remove from text index
                    for word_list in new_text_index.values():
                        word_list[:] = [(p, pos) for p, pos in word_list if p != file_path]
                    # Remove from file index
                    filename_lower = file_path.name.lower()
                    if filename_lower in new_file_index:
                        new_file_index[filename_lower] = [p for p in new_file_index[filename_lower] if p != file_path]
                        # Remove key if list is empty
                        if not new_file_index[filename_lower]:
                            del new_file_index[filename_lower]
                    # Mark for ChromaDB deletion
                    if semantic_enabled and self.chroma_collection:
                        chroma_ids_to_delete.append(str(file_path))

        # Delete old ChromaDB entries for updated/deleted files
        if semantic_enabled and chroma_ids_to_delete and self.chroma_collection:
            try:
                logger.info(f"FILE_OPS: Removing {len(chroma_ids_to_delete)} updated/deleted files from ChromaDB...")
                self.chroma_collection.delete(ids=chroma_ids_to_delete)
            except Exception as e:
                logger.warning(f"FILE_OPS: Failed to delete from ChromaDB: {e}")

        # Batch insert into ChromaDB (with chunking to avoid batch size limits)
        if semantic_enabled and chroma_docs and self.chroma_collection:
            logger.info(f"FILE_OPS: Creating embeddings for {len(chroma_docs)} files...")
            logger.info("FILE_OPS: This may take several minutes depending on file count and GPU availability")
            if status_callback:
                await status_callback(f"Creating embeddings for {len(chroma_docs)} files (this may take a while)...")
            try:
                embedding_start = time.time()

                # ChromaDB has a max batch size (~5000), so chunk large inserts
                BATCH_SIZE = 5000
                for i in range(0, len(chroma_docs), BATCH_SIZE):
                    batch_end = min(i + BATCH_SIZE, len(chroma_docs))
                    logger.info(f"FILE_OPS: Processing embedding batch {i//BATCH_SIZE + 1} ({i+1}-{batch_end} of {len(chroma_docs)})")

                    self.chroma_collection.add(
                        ids=chroma_ids[i:batch_end],
                        documents=chroma_docs[i:batch_end],
                        metadatas=chroma_metadatas[i:batch_end]
                    )

                embedding_time = time.time() - embedding_start
                logger.info(f"FILE_OPS: Created embeddings for {len(chroma_docs)} files in {embedding_time:.2f}s")
            except Exception as e:
                logger.error(f"FILE_OPS: ChromaDB batch insert failed: {e}", exc_info=True)

        # Atomic swap under lock
        async with self._index_lock:
            self.text_index = new_text_index
            self.file_name_index = new_file_index
            self.file_mtimes = new_mtimes

        elapsed = time.time() - start_time
        if incremental and self.file_mtimes:
            final_msg = f"FILE_OPS: Incremental update complete! Checked {files_indexed} files, updated {files_updated}, removed {files_removed if 'files_removed' in locals() else 0} in {elapsed:.2f}s"
        else:
            final_msg = f"FILE_OPS: Full index rebuild complete! Indexed {files_indexed} files in {elapsed:.2f}s"
        logger.info("=" * 60)
        logger.info(final_msg)
        logger.info("=" * 60)
        if status_callback:
            await status_callback(final_msg)

    async def _iter_files(self, base_dir: Path):
        """Async generator for all files in directory tree."""
        stack = [base_dir]

        while stack:
            current = stack.pop()

            try:
                for item in current.iterdir():
                    if item.is_dir():
                        # Don't follow symlinks to avoid loops
                        if not item.is_symlink():
                            stack.append(item)
                    elif item.is_file():
                        yield item

            except (OSError, PermissionError) as e:
                logger.debug(f"Cannot access {current}: {e}")
                continue

    def _is_excluded(self, path: Path) -> bool:
        """Check if path matches exclude patterns."""
        exclude_patterns = self.config.get("exclude_patterns", [
            "**/.git/**",
            "**/__pycache__/**",
            "**/*.pyc",
            "**/node_modules/**",
            "**/.venv/**",
            "**/*.min.js"
        ])

        path_str = str(path)
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(path_str, pattern):
                return True

        return False

    def _is_text_file(self, path: Path) -> bool:
        """Heuristic check if file is text (by extension)."""
        text_extensions = {
            '.py', '.txt', '.md', '.yaml', '.yml', '.json', '.toml',
            '.js', '.ts', '.jsx', '.tsx', '.css', '.scss', '.html',
            '.xml', '.csv', '.log', '.ini', '.cfg', '.conf',
            '.sh', '.bash', '.zsh', '.fish', '.ps1',
            '.c', '.cpp', '.h', '.hpp', '.java', '.rs', '.go',
            '.toml'  # Ensure .toml files are indexed
        }
        return path.suffix.lower() in text_extensions

    async def _index_file_content(self, file_path: Path, index: Dict[str, List[tuple[Path, int]]]) -> Optional[str]:
        """
        Index file content for grep search.

        Args:
            file_path: Path to file
            index: Text index to populate

        Returns:
            File content string (for ChromaDB) or None on error
        """
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()

            # Simple word-based tokenization
            words = re.findall(r'\w+', content.lower())
            for pos, word in enumerate(words):
                if len(word) >= 2:  # Index words with 2+ characters (reduced from 3 for better coverage)
                    index.setdefault(word, []).append((file_path, pos))

            return content  # Return content for ChromaDB

        except Exception as e:
            logger.debug(f"Failed to index {file_path}: {e}")
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

    async def search_files(self, pattern: str, max_results: int = 50) -> Dict[str, Any]:
        """
        Fast file search by glob pattern.

        Args:
            pattern: Glob pattern (e.g., '*.py', '**/*.test.ts', 'src/**/*.py')
                    Patterns can include path components and will be matched
                    relative to each allowed directory.
            max_results: Maximum number of files to return

        Returns:
            Dict with status, files, total_found, truncated
        """
        try:
            logger.debug(f"FILE_OPS: search_files called with pattern='{pattern}', max_results={max_results}")
            self._ensure_indexing_started()  # Start indexing if not already started
            await self._ensure_index_fresh()  # Quick incremental update if needed

            results: Set[Path] = set()
            pattern_lower = pattern.lower()

            # Determine if pattern contains path separators
            has_path_sep = "/" in pattern or "\\" in pattern
            logger.debug(f"FILE_OPS: Pattern has path separator: {has_path_sep}")

            # Use index for filename-only search
            if not has_path_sep:
                logger.debug("FILE_OPS: Searching in filename index...")
                async with self._index_lock:
                    for filename, paths in self.file_name_index.items():
                        if fnmatch.fnmatch(filename, pattern_lower):
                            results.update(paths)
                            if len(results) >= max_results:
                                break
                logger.debug(f"FILE_OPS: Found {len(results)} results in filename index")

            # If not enough results, try path-based glob on allowed dirs
            if len(results) < max_results:
                logger.debug(f"FILE_OPS: Searching with rglob in {len(self.allowed_dirs)} directories...")
                for base_dir in self.allowed_dirs:
                    if not base_dir.exists():
                        logger.debug(f"FILE_OPS: Skipping non-existent directory: {base_dir}")
                        continue

                    logger.debug(f"FILE_OPS: Searching in {base_dir}...")
                    # For patterns with path components, try to match relative to base_dir
                    # If pattern starts with a subdir of base_dir, make it relative
                    search_pattern = pattern
                    if has_path_sep:
                        # Try to make pattern relative to base_dir
                        try:
                            # Convert to Path for comparison
                            pattern_parts = Path(pattern).parts
                            base_parts = base_dir.parts

                            # If pattern starts with parts that match base_dir, strip them
                            if len(pattern_parts) > 0 and len(base_parts) > 0:
                                # Check if pattern starts with end of base_dir path
                                # E.g., base_dir ends with "data/workspace" and pattern is "data/workspace/*.txt"
                                matching_parts = 0
                                for i in range(len(base_parts)):
                                    base_suffix = base_parts[-(i+1):]
                                    if len(pattern_parts) > i and pattern_parts[:i+1] == base_suffix:
                                        matching_parts = i + 1

                                if matching_parts > 0:
                                    # Remove the matching prefix from pattern
                                    search_pattern = str(Path(*pattern_parts[matching_parts:]))
                        except Exception:
                            # If path manipulation fails, use original pattern
                            pass

                    logger.debug(f"FILE_OPS: Using search pattern '{search_pattern}' in {base_dir}")
                    file_count = 0
                    for path in base_dir.rglob(search_pattern):
                        file_count += 1
                        if file_count % 100 == 0:
                            logger.debug(f"FILE_OPS: Processed {file_count} files so far...")
                        if path.is_file() and not self._is_excluded(path):
                            results.add(path)
                            if len(results) >= max_results:
                                logger.debug(f"FILE_OPS: Reached max_results ({max_results}), stopping search")
                                break
                    logger.debug(f"FILE_OPS: Processed {file_count} files in {base_dir}, found {len(results)} matches")

            files_list = sorted([str(p) for p in results])[:max_results]
            logger.debug(f"FILE_OPS: Returning {len(files_list)} files")

            return {
                "status": "success",
                "files": files_list,
                "total_found": len(files_list),
                "truncated": len(results) > max_results
            }

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
        context_lines: int = 2
    ) -> Dict[str, Any]:
        """
        Fast text search using index.

        Args:
            query: Search query (literal or regex)
            is_regex: Treat query as regex pattern
            include_pattern: Only search files matching glob
            case_sensitive: Case-sensitive search
            max_results: Maximum matches to return
            context_lines: Lines of context before/after

        Returns:
            Dict with status, matches, total_matches, total_files, truncated
        """
        try:
            self._ensure_indexing_started()  # Start indexing if not already started
            await self._ensure_index_fresh()  # Quick incremental update if needed

            matches = []
            files_searched = 0

            # Get candidate files from index (if query has searchable tokens)
            candidate_files = await self._get_candidate_files(query, include_pattern, case_sensitive)

            # Compile regex if needed
            pattern = None
            if is_regex:
                flags = 0 if case_sensitive else re.IGNORECASE
                try:
                    pattern = re.compile(query, flags)
                except re.error as e:
                    return {
                        "status": "error",
                        "error": f"Invalid regex: {e}",
                        "error_type": "RegexError"
                    }

            # Search in candidate files
            for file_path in candidate_files:
                if len(matches) >= max_results:
                    break

                try:
                    async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = await f.readlines()

                    files_searched += 1

                    for line_num, line in enumerate(lines, 1):
                        # Check match
                        if is_regex:
                            if not pattern.search(line):
                                continue
                        else:
                            check_line = line if case_sensitive else line.lower()
                            check_query = query if case_sensitive else query.lower()
                            if check_query not in check_line:
                                continue

                        # Found match - extract context
                        context_before = [
                            line_text.rstrip('\n') for line_text in
                            lines[max(0, line_num - context_lines - 1):line_num - 1]
                        ]
                        context_after = [
                            line_text.rstrip('\n') for line_text in
                            lines[line_num:line_num + context_lines]
                        ]

                        matches.append({
                            "file_path": str(file_path),
                            "line_number": line_num,
                            "line_content": line.rstrip('\n'),
                            "context_before": context_before,
                            "context_after": context_after
                        })

                        if len(matches) >= max_results:
                            break

                except Exception as e:
                    logger.debug(f"Search error in {file_path}: {e}")

            return {
                "status": "success",
                "matches": matches,
                "total_matches": len(matches),
                "total_files": files_searched,
                "truncated": len(matches) >= max_results
            }

        except Exception as e:
            logger.error(f"Grep search error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def _get_candidate_files(
        self,
        query: str,
        include_pattern: Optional[str],
        case_sensitive: bool
    ) -> List[Path]:
        """Get candidate files from index based on query tokens."""
        candidate_files: Set[Path] = set()

        # Extract tokens from query
        query_lower = query if case_sensitive else query.lower()
        tokens = re.findall(r'\w+', query_lower)
        tokens = [t for t in tokens if len(t) >= 2]  # Only tokens with 2+ chars (reduced from 3 for better coverage)

        async with self._index_lock:
            if tokens and self.text_index:
                # Find files containing query tokens
                for token in tokens[:5]:  # Limit to first 5 tokens
                    if token in self.text_index:
                        for file_path, _ in self.text_index[token]:
                            candidate_files.add(file_path)
            else:
                # Fallback: all indexed files
                candidate_files = set(self.file_mtimes.keys())

        # Filter by include_pattern if specified
        if include_pattern:
            filtered = []
            for path in candidate_files:
                # Try matching against full path and relative path
                path_str = str(path)
                # Convert to forward slashes for consistent matching
                path_normalized = path_str.replace('\\', '/')
                pattern_normalized = include_pattern.replace('\\', '/')

                # Match against full path or just filename
                if (fnmatch.fnmatch(path_normalized, pattern_normalized) or
                    fnmatch.fnmatch(path_normalized, f"**/{pattern_normalized}") or
                    fnmatch.fnmatch(path.name, pattern_normalized)):
                    filtered.append(path)
            return filtered

        return list(candidate_files)

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
            # Initialize ChromaDB if needed
            if not self._chroma_initialized:
                self._init_chromadb()

            # Ensure index is fresh before semantic search
            await self._ensure_index_fresh()

            if not self.chroma_collection:
                return {
                    "status": "error",
                    "error": "Semantic search not available (ChromaDB not initialized)",
                    "error_type": "ChromaDBNotInitialized"
                }

            # Perform semantic search
            results = self.chroma_collection.query(
                query_texts=[query],
                n_results=max_results,
                include=["documents", "metadatas", "distances"]
            )

            if not results["ids"] or len(results["ids"][0]) == 0:
                return {
                    "status": "success",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": "No files found"
                }

            # Format results
            matches = []
            for i in range(len(results["ids"][0])):
                file_path = results["ids"][0][i]
                metadata = results["metadatas"][0][i]
                distance = results["distances"][0][i]

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

        except ChromaDBError as e:
            logger.error(f"ChromaDB semantic search error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": "ChromaDBError"
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

        # Cleanup ChromaDB resources
        if self.chroma_client:
            try:
                # ChromaDB PersistentClient doesn't have explicit close,
                # but we should clear references
                self.chroma_collection = None
                self.chroma_client = None
                self._chroma_initialized = False
            except Exception as e:
                logger.debug(f"ChromaDB cleanup warning: {e}")

        # Clear in-memory indexes
        self.text_index.clear()
        self.file_name_index.clear()
        self.file_mtimes.clear()
