"""Safe file operation implementations with atomic writes and error handling."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import aiofiles

from . import textsearch


logger = logging.getLogger(__name__)


class FileOperations:
    """Safe file operation implementations."""

    def __init__(self, max_unpaginated_kb: int = 100, default_line_limit: int = 500):
        """Initialize file operations.
        
        Args:
            max_unpaginated_kb: Max file size in KB to read without pagination
            default_line_limit: Default number of lines for paginated reads
        """
        self.max_unpaginated_size = max_unpaginated_kb * 1024  # Convert to bytes
        self.default_line_limit = default_line_limit

    async def read_file_safe(
        self,
        path: Path,
        offset: int = 0,
        limit: Optional[int] = None,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Read file contents with pagination support.
        
        Large files (>100KB) are automatically paginated to prevent memory issues.
        Use offset and limit parameters for explicit pagination control.

        Args:
            path: File path to read
            offset: Starting line number (0-indexed)
            limit: Maximum number of lines to read (default: 500 for large files)
            encoding: Text encoding

        Returns:
            Dict with status, content, total_lines, lines_read, offset, encoding, file_path
        """
        try:
            # Check file size first
            file_size = path.stat().st_size
            
            # If no pagination (offset=0 and no limit), read entire file preserving original line endings
            # BUT only if file is small enough
            if offset == 0 and limit is None and file_size <= self.max_unpaginated_size:
                async with aiofiles.open(path, 'r', encoding=encoding, errors='replace', newline='') as f:
                    content = await f.read()

                # Count lines for metadata
                total_lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)

                return {
                    "status": "success",
                    "content": content,
                    "total_lines": total_lines,
                    "lines_read": total_lines,
                    "offset": 0,
                    "encoding": encoding,
                    "file_path": str(path)
                }

            # Large file without explicit limit - apply default pagination
            if limit is None and file_size > self.max_unpaginated_size:
                limit = self.default_line_limit
                logger.info(
                    f"Large file ({file_size} bytes), applying default pagination: "
                    f"limit={limit} lines. Use offset/limit for more control."
                )

            # Pagination mode: need to process line by line
            lines = []
            total_lines = 0

            async with aiofiles.open(path, 'r', encoding=encoding, errors='replace', newline='') as f:
                async for line in f:
                    if total_lines >= offset and (limit is None or len(lines) < limit):
                        # Preserve line endings - don't strip them
                        lines.append(line)
                    total_lines += 1

            # Join without adding extra newlines (they're already in the lines)
            content = ''.join(lines)

            return {
                "status": "success",
                "content": content,
                "total_lines": total_lines,
                "lines_read": len(lines),
                "offset": offset,
                "encoding": encoding,
                "file_path": str(path)
            }

        except UnicodeDecodeError as e:
            logger.warning(f"Encoding error reading {path}: {e}")
            return {
                "status": "error",
                "error": f"File encoding error. Try a different encoding (current: {encoding})",
                "error_type": "EncodingError",
                "file_path": str(path)
            }
        except Exception as e:
            logger.error(f"Error reading file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    async def create_file_safe(
        self,
        path: Path,
        content: str,
        overwrite: bool = False,
        create_dirs: bool = True,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Create a file atomically, or replace one whole with overwrite.

        Args:
            path: File path to create
            content: File content
            overwrite: Replace the file if it exists (its mode is carried over)
            create_dirs: Auto-create parent directories
            encoding: Text encoding

        Returns:
            Dict with status, file_path, bytes_written, created_dirs, replaced
            (whether a file was there before -- the caller asked, this answers)
        """
        try:
            # A directory first: overwrite is no way out of it, so advising it would mislead.
            if path.is_dir():
                return {
                    "status": "error",
                    "error": f"Is a directory, not a file: {path}",
                    "error_type": "IsADirectoryError",
                    "file_path": str(path)
                }
            existed = path.exists()
            if existed and not overwrite:
                return {
                    "status": "error",
                    "error": f"File already exists: {path}. Use overwrite=true to replace it, "
                             f"or replace_string_in_file to edit part of it.",
                    "error_type": "FileExistsError",
                    "file_path": str(path)
                }

            # Create parent directories if needed
            created_dirs = []
            if create_dirs and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(path.parent))

            # Write to temporary file first
            temp_path = path.with_suffix(path.suffix + ".tmp")

            try:
                async with aiofiles.open(temp_path, 'w', encoding=encoding, newline='') as f:
                    await f.write(content)

                if existed:
                    # The replacement is a new file: without this an executable
                    # script comes back without its bit, a 0600 file world-readable.
                    try:
                        temp_path.chmod(path.stat().st_mode)
                    except OSError as exc:
                        logger.warning(f"Could not carry the mode of {path} over: {exc}")

                # Atomic rename
                temp_path.replace(path)

                bytes_written = path.stat().st_size

                return {
                    "status": "success",
                    "file_path": str(path),
                    "bytes_written": bytes_written,
                    "created_dirs": created_dirs,
                    "replaced": existed
                }

            finally:
                # Cleanup temp file if still exists
                if temp_path.exists():
                    temp_path.unlink()

        except Exception as e:
            logger.error(f"Error creating file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    async def edit_file_safe(
        self,
        path: Path,
        old_string: str,
        new_string: str,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Replace text in an existing file — the one edit primitive.

        Line-number modes (insert, replace_lines) and append used to live here
        with no tool exposing them. They were removed rather than exposed: a
        line number goes stale after the first edit above it, insert counted
        from 0 while read_file counts from 1, and a replace anchored on real
        text fails loudly instead of editing the wrong line.

        Args:
            path: File to edit
            old_string: String to replace
            new_string: Replacement string
            encoding: Text encoding

        Returns:
            Dict with status, file_path, changes
        """
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"File not found: {path}",
                    "error_type": "FileNotFoundError",
                    "file_path": str(path)
                }

            # newline='' keeps the file's own line endings. The default
            # translation turned every CRLF file into LF on its first edit —
            # the whole file rewritten, whatever the edit was.
            async with aiofiles.open(path, 'r', encoding=encoding, newline='') as f:
                current_content = await f.read()

            if old_string is None or new_string is None:
                return {
                    "status": "error",
                    "error": "Parameters 'old_string' and 'new_string' are required",
                    "error_type": "ValidationError"
                }

            # Try exact match first
            replacements = current_content.count(old_string)
            if replacements == 1:
                new_content = current_content.replace(old_string, new_string, 1)

            elif replacements == 0:
                # Flexible matching: the model writes \n, the file may hold \r\n
                normalized_content = current_content.replace('\r\n', '\n')
                normalized_old_string = old_string.replace('\r\n', '\n')
                normalized_new_string = new_string.replace('\r\n', '\n')
                replacements = normalized_content.count(normalized_old_string)

                if replacements == 1:
                    logger.info("Using normalized line endings for replacement (CRLF/LF flexibility)")
                    new_content = normalized_content.replace(
                        normalized_old_string, normalized_new_string, 1)
                    # Back to the file's style, once, for the whole text —
                    # converting new_string beforehand as well made \r\r\n.
                    if '\r\n' in current_content:
                        new_content = new_content.replace('\n', '\r\n')
                elif replacements == 0:
                    # Check if it's only whitespace difference (informative error)
                    stripped_content = normalized_content.replace(' ', '').replace('\t', '')
                    stripped_old_string = normalized_old_string.replace(' ', '').replace('\t', '')

                    if stripped_old_string in stripped_content:
                        # Content matches but whitespace differs - provide detailed analysis
                        # Find the position where content matches
                        pos = stripped_content.find(stripped_old_string)

                        # Reconstruct position in original content
                        char_count = 0
                        for i, char in enumerate(normalized_content):
                            if char not in (' ', '\t'):
                                if char_count == pos:
                                    # Found start position - extract a few lines for comparison
                                    lines_before = normalized_content[:i].count('\n')
                                    start_line = max(0, lines_before)

                                    # Get the relevant lines from file
                                    file_lines = normalized_content.splitlines()
                                    search_lines = normalized_old_string.splitlines()

                                    if start_line < len(file_lines) and search_lines:
                                        # Build corrected oldString with proper whitespace
                                        corrected_lines = []
                                        for j, search_line in enumerate(search_lines):
                                            file_idx = start_line + j
                                            if file_idx < len(file_lines):
                                                file_line = file_lines[file_idx]
                                                # Extract leading whitespace from file
                                                file_leading = len(file_line) - len(file_line.lstrip())
                                                file_ws = file_line[:file_leading]
                                                # Apply to search line content
                                                search_content = search_line.lstrip()
                                                corrected_lines.append(file_ws + search_content)
                                            else:
                                                corrected_lines.append(search_line)

                                        corrected_old_string = '\n'.join(corrected_lines)

                                        # Show first mismatched line with detailed whitespace info
                                        file_line = file_lines[start_line]
                                        search_line = search_lines[0]

                                        # Analyze whitespace
                                        file_leading = len(file_line) - len(file_line.lstrip())
                                        search_leading = len(search_line) - len(search_line.lstrip())

                                        file_ws = file_line[:file_leading]
                                        search_ws = search_line[:search_leading]

                                        ws_details = f"Line {start_line + 1}: "
                                        if file_leading != search_leading:
                                            ws_details += f"Expected {file_leading} leading whitespace chars, got {search_leading}. "

                                        file_tabs = file_ws.count('\t')
                                        file_spaces = file_ws.count(' ')
                                        search_tabs = search_ws.count('\t')
                                        search_spaces = search_ws.count(' ')

                                        if file_tabs != search_tabs or file_spaces != search_spaces:
                                            ws_details += f"File has {file_tabs} tabs + {file_spaces} spaces, search has {search_tabs} tabs + {search_spaces} spaces."

                                        logger.warning(f"String found with whitespace differences in {path}: {ws_details}")
                                        return {
                                            "status": "error",
                                            "error": f"String not found with exact whitespace. {ws_details}",
                                            "error_type": "WhitespaceMatchError",
                                            "file_path": str(path),
                                            "file_sample": file_line,
                                            "search_sample": search_line,
                                            "corrected_old_string": corrected_old_string,
                                            "hint": "Whitespace (spaces/tabs) doesn't match exactly. Use the 'corrected_old_string' value for oldString parameter."
                                        }
                                    break
                                char_count += 1

                        # Fallback if detailed analysis fails
                        logger.warning(f"String found with whitespace differences in {path}")
                        return {
                            "status": "error",
                            "error": "String not found with exact whitespace. Content matches but spaces/tabs differ.",
                            "error_type": "WhitespaceMatchError",
                            "file_path": str(path),
                            "hint": "Whitespace (spaces/tabs) doesn't match exactly. Copy the exact indentation from the file."
                        }

                    # True not found
                    return {
                        "status": "error",
                        "error": f"String not found in file: {old_string[:50]}...",
                        "error_type": "StringNotFoundError",
                        "file_path": str(path),
                        "hint": "String does not exist in file. Check spelling and ensure you have the correct content."
                    }

            if replacements > 1:
                # The schema promises ONE occurrence and asks for a unique
                # oldString. Replacing all of them edited places the model never
                # looked at.
                return {
                    "status": "error",
                    "error": (f"oldString occurs {replacements} times in the file. "
                              "Include more surrounding lines so it matches exactly once."),
                    "error_type": "AmbiguousMatchError",
                    "file_path": str(path),
                    "occurrences": replacements
                }

            # Where the new text now stands: first and last line, 1-indexed.
            # Compared line by line instead, every line after an inserted one
            # counted as modified -- measured up to 53k characters of line
            # numbers in one answer, 28 of 133 answers over 2k.
            flat = current_content.replace('\r\n', '\n')
            added = new_string.replace('\r\n', '\n')
            # Newlines the new text opens with end the line before it: it
            # starts that many lines further down. A closing newline ends its
            # last line, it does not start another.
            lead = len(added) - len(added.lstrip('\n'))
            first = flat.count('\n', 0, max(flat.find(old_string.replace('\r\n', '\n')), 0)) + 1 + lead
            added = added[lead:]
            added = added[:-1] if added.endswith('\n') else added
            changes = {
                "replacements": replacements,
                "lines": [first, first + added.count('\n')]
            }


            # Write updated content atomically
            temp_path = path.with_suffix(path.suffix + ".tmp")

            try:
                async with aiofiles.open(temp_path, 'w', encoding=encoding, newline='') as f:
                    await f.write(new_content)

                # Atomic rename
                temp_path.replace(path)

            finally:
                if temp_path.exists():
                    temp_path.unlink()

            return {
                "status": "success",
                "file_path": str(path),
                "mode": "replace",
                "changes": changes
            }

        except Exception as e:
            logger.error(f"Error editing file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    async def delete_file_safe(self, path: Path) -> Dict[str, Any]:
        """
        Delete a file safely.

        Args:
            path: File to delete

        Returns:
            Dict with status, file_path
        """
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"File not found: {path}",
                    "error_type": "FileNotFoundError",
                    "file_path": str(path)
                }

            if not path.is_file():
                return {
                    "status": "error",
                    "error": f"Path is not a file: {path}",
                    "error_type": "NotAFileError",
                    "file_path": str(path)
                }

            path.unlink()

            return {
                "status": "success",
                "file_path": str(path),
                "message": "File deleted successfully"
            }

        except Exception as e:
            logger.error(f"Error deleting file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    async def delete_path_safe(self, path: Path, recursive: bool = False) -> Dict[str, Any]:
        """
        Delete a file or directory safely.

        Args:
            path: Path to delete (file or directory)
            recursive: If True, delete non-empty directories recursively

        Returns:
            Dict with status, path, type (file/directory)
        """
        import shutil
        
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"Path not found: {path}",
                    "error_type": "FileNotFoundError",
                    "path": str(path)
                }

            if path.is_file():
                path.unlink()
                return {
                    "status": "success",
                    "path": str(path),
                    "type": "file",
                    "message": "File deleted successfully"
                }
            elif path.is_dir():
                if recursive:
                    shutil.rmtree(path)
                    return {
                        "status": "success",
                        "path": str(path),
                        "type": "directory",
                        "message": "Directory deleted recursively"
                    }
                else:
                    # Try to remove empty directory
                    try:
                        path.rmdir()
                        return {
                            "status": "success",
                            "path": str(path),
                            "type": "directory",
                            "message": "Empty directory deleted"
                        }
                    except OSError as e:
                        if "not empty" in str(e).lower() or "directory not empty" in str(e).lower():
                            return {
                                "status": "error",
                                "error": f"Directory not empty: {path}. Use recursive=true to delete non-empty directories.",
                                "error_type": "DirectoryNotEmptyError",
                                "path": str(path)
                            }
                        raise
            else:
                return {
                    "status": "error",
                    "error": f"Unknown path type: {path}",
                    "error_type": "UnknownPathTypeError",
                    "path": str(path)
                }

        except Exception as e:
            logger.error(f"Error deleting path {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "path": str(path)
            }

    async def move_path_safe(self, source: Path, destination: Path) -> Dict[str, Any]:
        """
        Move a file or directory to a new location.

        Args:
            source: Source path
            destination: Destination path

        Returns:
            Dict with status, source, destination, type
        """
        import shutil
        
        try:
            if not source.exists():
                return {
                    "status": "error",
                    "error": f"Source not found: {source}",
                    "error_type": "FileNotFoundError",
                    "source": str(source)
                }

            if destination.exists():
                return {
                    "status": "error",
                    "error": f"Destination already exists: {destination}",
                    "error_type": "FileExistsError",
                    "destination": str(destination)
                }

            # Create parent directories if needed
            destination.parent.mkdir(parents=True, exist_ok=True)

            path_type = "file" if source.is_file() else "directory"
            
            # Use shutil.move for cross-device moves
            shutil.move(str(source), str(destination))

            return {
                "status": "success",
                "source": str(source),
                "destination": str(destination),
                "type": path_type,
                "message": f"{path_type.capitalize()} moved successfully"
            }

        except Exception as e:
            logger.error(f"Error moving {source} to {destination}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "source": str(source),
                "destination": str(destination)
            }

    async def rename_path_safe(self, path: Path, new_name: str) -> Dict[str, Any]:
        """
        Rename a file or directory (same directory).

        Args:
            path: Path to rename
            new_name: New name (without path)

        Returns:
            Dict with status, old_path, new_path, type
        """
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"Path not found: {path}",
                    "error_type": "FileNotFoundError",
                    "path": str(path)
                }

            # Validate new_name doesn't contain path separators
            if "/" in new_name or "\\" in new_name:
                return {
                    "status": "error",
                    "error": f"new_name must not contain path separators: {new_name}",
                    "error_type": "ValidationError"
                }

            new_path = path.parent / new_name

            if new_path.exists():
                return {
                    "status": "error",
                    "error": f"Target already exists: {new_path}",
                    "error_type": "FileExistsError",
                    "new_path": str(new_path)
                }

            path_type = "file" if path.is_file() else "directory"
            path.rename(new_path)

            return {
                "status": "success",
                "old_path": str(path),
                "new_path": str(new_path),
                "type": path_type,
                "message": f"{path_type.capitalize()} renamed successfully"
            }

        except Exception as e:
            logger.error(f"Error renaming {path} to {new_name}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "path": str(path)
            }

    async def list_directory_safe(
        self,
        dir_path: Path,
        recursive: bool = False,
        pattern: Optional[str] = None,
        include_hidden: bool = False,
        max_results: int = 200,
        include_ignored: bool = False,
        excludes: Sequence[str] = textsearch.DEFAULT_EXCLUDES,
    ) -> Dict[str, Any]:
        """
        List directory contents with filtering, capped at max_results entries.

        The listing itself lives in ``textsearch.list_directory`` and runs in
        a worker thread: a recursive walk is blocking disk I/O.

        Args:
            dir_path: Directory to list
            recursive: Recursively list subdirectories
            pattern: Glob pattern to filter entries
            include_hidden: Include hidden files
            max_results: Maximum number of entries (files + directories)
            include_ignored: Also list .gitignored, excluded and hidden entries
            excludes: Exclude globs pruned by a recursive listing

        Returns:
            Dict with status, dir_path, files, directories, truncated, skipped
        """
        try:
            if not dir_path.exists():
                return {
                    "status": "error",
                    "error": f"Directory not found: {dir_path}",
                    "error_type": "DirectoryNotFoundError",
                    "dir_path": str(dir_path)
                }

            if not dir_path.is_dir():
                return {
                    "status": "error",
                    "error": f"Path is not a directory: {dir_path}",
                    "error_type": "NotADirectoryError",
                    "dir_path": str(dir_path)
                }

            return await asyncio.to_thread(
                textsearch.list_directory,
                dir_path,
                recursive=recursive,
                pattern=pattern,
                max_results=max_results,
                excludes=excludes,
                include_hidden=include_hidden,
                include_ignored=include_ignored,
            )

        except Exception as e:
            logger.error(f"Error listing directory {dir_path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "dir_path": str(dir_path)
            }
