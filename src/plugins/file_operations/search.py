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
    
    async def _background_indexer(self):
        """Periodically rebuild index in background."""
        while True:
            try:
                await self.rebuild_index()
                
                # Wait for next cycle
                interval = self.config.get("auto_reindex_interval_seconds", 300)
                await asyncio.sleep(interval)
            
            except Exception as e:
                logger.error(f"Indexing error: {e}", exc_info=True)
                await asyncio.sleep(60)  # Retry after 1 minute
    
    async def rebuild_index(self):
        """Full index rebuild."""
        logger.info("Starting file index rebuild...")
        start_time = time.time()
        
        new_text_index: Dict[str, List[tuple[Path, int]]] = {}
        new_file_index: Dict[str, List[Path]] = {}
        new_mtimes: Dict[Path, float] = {}
        files_indexed = 0
        
        max_size_kb = self.config.get("max_file_size_for_indexing_kb", 1024)
        max_size = max_size_kb * 1024
        
        for base_dir in self.allowed_dirs:
            if not base_dir.exists():
                continue
            
            async for file_path in self._iter_files(base_dir):
                # Skip excluded patterns
                if self._is_excluded(file_path):
                    continue
                
                # Skip large files
                try:
                    size = file_path.stat().st_size
                    if size > max_size:
                        continue
                    
                    # Index filename
                    filename_lower = file_path.name.lower()
                    new_file_index.setdefault(filename_lower, []).append(file_path)
                    
                    # Index file content for text files
                    if self._is_text_file(file_path):
                        await self._index_file_content(file_path, new_text_index)
                        new_mtimes[file_path] = file_path.stat().st_mtime
                    
                    files_indexed += 1
                
                except (OSError, PermissionError) as e:
                    logger.debug(f"Skipping file {file_path}: {e}")
                    continue
        
        # Atomic swap under lock
        async with self._index_lock:
            self.text_index = new_text_index
            self.file_name_index = new_file_index
            self.file_mtimes = new_mtimes
        
        elapsed = time.time() - start_time
        logger.info(f"Index rebuilt: {files_indexed} files in {elapsed:.2f}s")
    
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
            '.c', '.cpp', '.h', '.hpp', '.java', '.rs', '.go'
        }
        return path.suffix.lower() in text_extensions
    
    async def _index_file_content(self, file_path: Path, index: Dict[str, List[tuple[Path, int]]]):
        """Index file content for grep search."""
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()
            
            # Simple word-based tokenization
            words = re.findall(r'\w+', content.lower())
            for pos, word in enumerate(words):
                if len(word) >= 3:  # Index words with 3+ characters
                    index.setdefault(word, []).append((file_path, pos))
        
        except Exception as e:
            logger.debug(f"Failed to index {file_path}: {e}")
    
    async def search_files(self, pattern: str, max_results: int = 50) -> Dict[str, Any]:
        """
        Fast file search by glob pattern.
        
        Args:
            pattern: Glob pattern (e.g., '*.py', '**/*.test.ts')
            max_results: Maximum number of files to return
        
        Returns:
            Dict with status, files, total_found, truncated
        """
        try:
            self._ensure_indexing_started()  # Start indexing if not already started
            
            results: Set[Path] = set()
            pattern_lower = pattern.lower()
            
            # Use index for filename search
            async with self._index_lock:
                for filename, paths in self.file_name_index.items():
                    if fnmatch.fnmatch(filename, pattern_lower):
                        results.update(paths)
                        if len(results) >= max_results:
                            break
            
            # If not enough results, try path-based glob on allowed dirs
            if len(results) < max_results:
                for base_dir in self.allowed_dirs:
                    if not base_dir.exists():
                        continue
                    
                    for path in base_dir.rglob(pattern):
                        if path.is_file() and not self._is_excluded(path):
                            results.add(path)
                            if len(results) >= max_results:
                                break
            
            files_list = sorted([str(p) for p in results])[:max_results]
            
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
        tokens = [t for t in tokens if len(t) >= 3]  # Only tokens with 3+ chars
        
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
                if fnmatch.fnmatch(str(path), include_pattern):
                    filtered.append(path)
            return filtered
        
        return list(candidate_files)
    
    async def stop(self):
        """Stop background indexing task."""
        if self._indexing_task:
            self._indexing_task.cancel()
            try:
                await self._indexing_task
            except asyncio.CancelledError:
                pass
