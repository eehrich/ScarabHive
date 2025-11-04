"""Safe file operation implementations with atomic writes and error handling."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles


logger = logging.getLogger(__name__)


class FileOperations:
    """Safe file operation implementations."""
    
    async def read_file_safe(
        self,
        path: Path,
        offset: int = 0,
        limit: Optional[int] = None,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Read file contents with pagination support.
        
        Args:
            path: File path to read
            offset: Starting line number (0-indexed)
            limit: Maximum number of lines to read
            encoding: Text encoding
        
        Returns:
            Dict with status, content, total_lines, lines_read, offset, encoding, file_path
        """
        try:
            lines = []
            total_lines = 0
            
            async with aiofiles.open(path, 'r', encoding=encoding, errors='replace') as f:
                async for line in f:
                    if total_lines >= offset and (limit is None or len(lines) < limit):
                        lines.append(line.rstrip('\n'))
                    total_lines += 1
            
            return {
                "status": "success",
                "content": '\n'.join(lines),
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
        Create a new file atomically.
        
        Args:
            path: File path to create
            content: File content
            overwrite: Allow overwriting existing file
            create_dirs: Auto-create parent directories
            encoding: Text encoding
        
        Returns:
            Dict with status, file_path, bytes_written, created_dirs
        """
        try:
            # Check if file exists
            if path.exists() and not overwrite:
                return {
                    "status": "error",
                    "error": f"File already exists: {path}. Use overwrite=true to replace.",
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
                async with aiofiles.open(temp_path, 'w', encoding=encoding) as f:
                    await f.write(content)
                
                # Atomic rename
                temp_path.replace(path)
                
                bytes_written = path.stat().st_size
                
                return {
                    "status": "success",
                    "file_path": str(path),
                    "bytes_written": bytes_written,
                    "created_dirs": created_dirs
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
        mode: str,
        content: Optional[str] = None,
        old_string: Optional[str] = None,
        new_string: Optional[str] = None,
        line_number: Optional[int] = None,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Edit an existing file using various modes.
        
        Args:
            path: File to edit
            mode: Edit mode (append, replace, insert)
            content: Content to add (append/insert modes)
            old_string: String to replace (replace mode)
            new_string: Replacement string (replace mode)
            line_number: Line number to insert at (insert mode)
            encoding: Text encoding
        
        Returns:
            Dict with status, file_path, mode, changes
        """
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"File not found: {path}",
                    "error_type": "FileNotFoundError",
                    "file_path": str(path)
                }
            
            # Read current content
            async with aiofiles.open(path, 'r', encoding=encoding) as f:
                current_content = await f.read()
            
            # Apply edit based on mode
            if mode == "append":
                if content is None:
                    return {
                        "status": "error",
                        "error": "Parameter 'content' is required for append mode",
                        "error_type": "ValidationError"
                    }
                new_content = current_content + content
                changes = {"appended_bytes": len(content)}
            
            elif mode == "replace":
                if old_string is None or new_string is None:
                    return {
                        "status": "error",
                        "error": "Parameters 'old_string' and 'new_string' are required for replace mode",
                        "error_type": "ValidationError"
                    }
                
                # Count replacements
                replacements = current_content.count(old_string)
                if replacements == 0:
                    return {
                        "status": "error",
                        "error": f"String not found in file: {old_string[:50]}...",
                        "error_type": "StringNotFoundError",
                        "file_path": str(path)
                    }
                
                new_content = current_content.replace(old_string, new_string)
                
                # Find modified lines
                old_lines = current_content.splitlines()
                new_lines = new_content.splitlines()
                modified_lines = [
                    i for i, (old, new) in enumerate(zip(old_lines, new_lines))
                    if old != new
                ]
                
                changes = {
                    "replacements": replacements,
                    "lines_modified": modified_lines
                }
            
            elif mode == "insert":
                if content is None:
                    return {
                        "status": "error",
                        "error": "Parameter 'content' is required for insert mode",
                        "error_type": "ValidationError"
                    }
                if line_number is None:
                    return {
                        "status": "error",
                        "error": "Parameter 'line_number' is required for insert mode",
                        "error_type": "ValidationError"
                    }
                
                lines = current_content.splitlines(keepends=True)
                
                # Validate line number
                if line_number < 0 or line_number > len(lines):
                    return {
                        "status": "error",
                        "error": f"Invalid line_number {line_number}. File has {len(lines)} lines.",
                        "error_type": "ValidationError",
                        "file_path": str(path)
                    }
                
                # Insert content at line
                lines.insert(line_number, content + '\n')
                new_content = ''.join(lines)
                changes = {"inserted_at_line": line_number}
            
            else:
                return {
                    "status": "error",
                    "error": f"Invalid mode: {mode}. Must be one of: append, replace, insert",
                    "error_type": "ValidationError"
                }
            
            # Write updated content atomically
            temp_path = path.with_suffix(path.suffix + ".tmp")
            
            try:
                async with aiofiles.open(temp_path, 'w', encoding=encoding) as f:
                    await f.write(new_content)
                
                # Atomic rename
                temp_path.replace(path)
            
            finally:
                if temp_path.exists():
                    temp_path.unlink()
            
            return {
                "status": "success",
                "file_path": str(path),
                "mode": mode,
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
    
    async def list_directory_safe(
        self,
        dir_path: Path,
        recursive: bool = False,
        pattern: Optional[str] = None,
        include_hidden: bool = False
    ) -> Dict[str, Any]:
        """
        List directory contents with filtering.
        
        Args:
            dir_path: Directory to list
            recursive: Recursively list subdirectories
            pattern: Glob pattern to filter files
            include_hidden: Include hidden files
        
        Returns:
            Dict with status, dir_path, files, directories
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
            
            files = []
            directories = []
            
            # Choose glob method
            if recursive:
                items = dir_path.rglob(pattern or "*")
            else:
                items = dir_path.glob(pattern or "*")
            
            for item in items:
                # Skip hidden files unless requested
                if not include_hidden and item.name.startswith('.'):
                    continue
                
                if item.is_file():
                    files.append(str(item))
                elif item.is_dir():
                    directories.append(str(item))
            
            return {
                "status": "success",
                "dir_path": str(dir_path),
                "files": sorted(files),
                "directories": sorted(directories),
                "total_files": len(files),
                "total_directories": len(directories)
            }
        
        except Exception as e:
            logger.error(f"Error listing directory {dir_path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "dir_path": str(dir_path)
            }
