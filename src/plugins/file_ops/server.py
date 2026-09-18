"""File Ops Tool Server implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from agent_system.config import AgentSystemConfig, ToolServerConfig
from agent_system.tools.schema_based import SchemaBasedToolServer

from .security import PathValidator, SecurityError
from .operations import FileOperations
from .search import FileSearchEngine


logger = logging.getLogger(__name__)


async def _end_or_error(status, result: Dict[str, Any], message: str,
                        meta: Dict[str, Any]) -> None:
    """Close the status scope according to what `result` actually says.

    The reading tools ended unconditionally, so a failure that operations.py
    or search.py RETURNS (rather than raises) closed the scope with a healthy
    line -- "Read config.yaml: 0/0 lines" for a file that could not be read.
    Only the raising paths were reported truthfully.
    """
    if status is None:
        return
    if isinstance(result, dict) and result.get("status") == "error":
        await status.error(
            result.get("error", "Unknown error"),
            meta={**meta, "error_type": result.get("error_type", "UnknownError")},
        )
        return
    await status.end(message, meta=meta)


class FileOpsServer(SchemaBasedToolServer):
    """tool server providing secure file operations with search capabilities."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """
        Initialize file operations server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)

        # Extract configuration
        allowed_dirs = getattr(server_config, "allowed_directories", [])
        if not allowed_dirs:
            # Default to project root subdirectories
            project_root = Path.cwd()
            allowed_dirs = [
                str(project_root / "src"),
                str(project_root / "docs"),
                str(project_root / "tests"),
                str(project_root / "tmp")
            ]
        else:
            # Resolve relative paths relative to project root
            project_root = Path.cwd()
            resolved_dirs = []
            for dir_path in allowed_dirs:
                if dir_path == ".":
                    # Special case: "." means project root
                    resolved_dirs.append(str(project_root))
                else:
                    # Resolve relative to project root
                    resolved_path = (project_root / dir_path).resolve()
                    resolved_dirs.append(str(resolved_path))
            allowed_dirs = resolved_dirs

        # Initialize components
        self.validator = PathValidator(allowed_dirs)
        
        # Read-only mode flag
        self.read_only = getattr(server_config, "read_only", False)
        if self.read_only:
            logger.info("FileOperationsServer running in READ-ONLY mode")
        
        # Get file reading limits from config
        max_unpaginated_kb = getattr(server_config, "max_unpaginated_file_size_kb", 100)
        default_line_limit = getattr(server_config, "default_line_limit", 500)
        self.operations = FileOperations(
            max_unpaginated_kb=max_unpaginated_kb,
            default_line_limit=default_line_limit
        )

        # Initialize search engine with configuration
        search_config = getattr(server_config, "search", {})

        # Off unless an instance asks for it: it is the only feature that
        # builds an index and persists a vector store.
        if "enable_semantic_search" not in search_config:
            search_config["enable_semantic_search"] = False
            logger.info("Semantic search off by default (search.enable_semantic_search)")

        # One collection per INSTANCE, because two instances are two trees.
        # Sharing the default name meant the second instance's full rebuild
        # cleared the first one's index. Instances that deliberately share a
        # tree (a read-write and a read-only twin) can share the index by
        # setting the same `collection_name` — then only one of them needs to
        # build it.
        search_config.setdefault("collection_name", f"file_ops_{name}")

        self.search_engine = FileSearchEngine(self.validator.allowed_dirs, search_config)
        # NOT started here. Building the index at construction looks tempting --
        # the instance that owns an index is not always the one that searches
        # it -- but measured on this machine it started four background builds
        # at once, one per instance that has semantic search configured
        # (file_ops, amiga_fs, agent_file_ops, coder_fs), for trees nobody had
        # asked a question about yet. The first semantic_search starts it.

        logger.info(f"FileOperationsServer initialized with {len(allowed_dirs)} allowed directories")
        logger.info(f"Search indexing: {search_config.get('enable_indexing', True)}")

    def get_template_vars(self) -> Dict[str, Any]:
        """Provide template variables for schema.yaml rendering."""
        vars = super().get_template_vars()
        vars['read_only'] = self.read_only
        return vars

    async def read_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Read text file contents with pagination."""
        status = params.get("_status")

        try:
            file_path = params["filePath"]
            # Copilot uses 1-indexed offset
            offset = params.get("offset", 0)
            if offset > 0:
                offset -= 1  # Convert 1-indexed to 0-indexed for internal use
            limit = params.get("limit")
            encoding = "utf-8"  # Always UTF-8

            if status:
                await status.progress(f"Reading: {Path(file_path).name}")

            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)

            if not safe_path.is_file():
                return {
                    "status": "error",
                    "error": f"Path is not a file: {file_path}",
                    "error_type": "NotAFileError"
                }

            # Read file
            result = await self.operations.read_file_safe(
                safe_path, offset=offset, limit=limit, encoding=encoding
            )

            if status:
                lines_read = result.get("lines_read", 0)
                total_lines = result.get("total_lines", 0)
                await _end_or_error(
                    status, result,
                    f"Read {safe_path.name}: {lines_read}/{total_lines} lines", meta={
                        "file": str(safe_path),
                        "lines_read": lines_read,
                        "total_lines": total_lines
                    })

            return result

        except FileNotFoundError:
            # 'filePath' is the parameter name (line 99) -- 'file_path' never
            # existed here, so this message always read "File not found: None".
            error_msg = f"File not found: {params.get('filePath')}"
            if status:
                await status.error(error_msg, meta={"error_type": "FileNotFoundError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            logger.error(f"Unexpected error in read_file: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def manage(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Unified file/directory management: create, delete, move, rename.

        Operations:
        - create: Create new file with content (fails if exists)
        - delete: Delete file or directory (recursive option for non-empty dirs)
        - move: Move file/directory to new location
        - rename: Rename file/directory (same directory)
        """
        # Safety check: block write operations in read-only mode
        if self.read_only:
            return {
                "status": "error",
                "error": "File operations server is in read-only mode. Write operations are disabled.",
                "error_type": "ReadOnlyError"
            }

        status = params.get("_status")
        operation = params.get("operation")
        path = params.get("path")

        if not operation:
            return {
                "status": "error",
                "error": "Parameter 'operation' is required",
                "error_type": "ValidationError"
            }

        if not path:
            return {
                "status": "error",
                "error": "Parameter 'path' is required",
                "error_type": "ValidationError"
            }

        try:
            if operation == "create":
                return await self._manage_create(params, status)
            elif operation == "delete":
                return await self._manage_delete(params, status)
            elif operation == "move":
                return await self._manage_move(params, status)
            elif operation == "rename":
                return await self._manage_rename(params, status)
            else:
                return {
                    "status": "error",
                    "error": f"Unknown operation: {operation}. Valid: create, delete, move, rename",
                    "error_type": "ValidationError"
                }

        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            logger.error(f"Unexpected error in manage ({operation}): {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def _manage_create(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Create operation: create new file with content."""
        path = params["path"]
        content = params.get("content")

        if content is None:
            return {
                "status": "error",
                "error": "Parameter 'content' is required for create operation",
                "error_type": "ValidationError"
            }

        if status:
            await status.progress(f"Creating: {Path(path).name}")

        safe_path = self.validator.validate_path(path)

        result = await self.operations.create_file_safe(
            safe_path,
            content=content,
            overwrite=False,
            create_dirs=True,
            encoding="utf-8"
        )

        if status and result.get("status") == "success":
            bytes_written = result.get("bytes_written", 0)
            await status.end(f"Created {safe_path.name}: {bytes_written} bytes", meta={
                "file": str(safe_path),
                "bytes": bytes_written
            })

        return result

    async def _manage_delete(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Delete operation: delete file or directory."""
        path = params["path"]
        recursive = params.get("recursive", False)

        if status:
            await status.progress(f"Deleting: {Path(path).name}")

        safe_path = self.validator.validate_path(path, must_exist=True)

        result = await self.operations.delete_path_safe(safe_path, recursive=recursive)

        if status and result.get("status") == "success":
            path_type = result.get("type", "path")
            await status.end(f"Deleted {path_type}: {safe_path.name}", meta={
                "path": str(safe_path),
                "type": path_type,
                "recursive": recursive
            })

        return result

    async def _manage_move(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Move operation: move file/directory to new location."""
        path = params["path"]
        destination = params.get("destination")

        if not destination:
            return {
                "status": "error",
                "error": "Parameter 'destination' is required for move operation",
                "error_type": "ValidationError"
            }

        if status:
            await status.progress(f"Moving: {Path(path).name} → {Path(destination).name}")

        safe_source = self.validator.validate_path(path, must_exist=True)
        safe_dest = self.validator.validate_path(destination)

        result = await self.operations.move_path_safe(safe_source, safe_dest)

        if status and result.get("status") == "success":
            path_type = result.get("type", "path")
            await status.end(f"Moved {path_type}: {safe_source.name} → {safe_dest.name}", meta={
                "source": str(safe_source),
                "destination": str(safe_dest),
                "type": path_type
            })

        return result

    async def _manage_rename(self, params: Dict[str, Any], status) -> Dict[str, Any]:
        """Rename operation: rename file/directory (same directory)."""
        path = params["path"]
        new_name = params.get("new_name")

        if not new_name:
            return {
                "status": "error",
                "error": "Parameter 'new_name' is required for rename operation",
                "error_type": "ValidationError"
            }

        if status:
            await status.progress(f"Renaming: {Path(path).name} → {new_name}")

        safe_path = self.validator.validate_path(path, must_exist=True)

        # Validate destination is also in allowed directories
        new_path = safe_path.parent / new_name
        self.validator.validate_path(str(new_path))

        result = await self.operations.rename_path_safe(safe_path, new_name)

        if status and result.get("status") == "success":
            path_type = result.get("type", "path")
            await status.end(f"Renamed {path_type}: {safe_path.name} → {new_name}", meta={
                "old_path": str(safe_path),
                "new_path": str(new_path),
                "type": path_type
            })

        return result

    async def replace_string_in_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Replace exact string match in file (VSCode/Copilot-style).

        This is a convenience wrapper around edit_file with mode='replace'.
        Uses exact string matching - oldString must match precisely including whitespace.
        """
        # Safety check: block write operations in read-only mode
        if self.read_only:
            return {
                "status": "error",
                "error": "File operations server is in read-only mode. Write operations are disabled.",
                "error_type": "ReadOnlyError"
            }

        status = params.get("_status")

        try:
            file_path = params["filePath"]  # Note: Copilot uses camelCase
            old_string = params["oldString"]
            new_string = params["newString"]

            if status:
                await status.progress(f"Replacing in: {Path(file_path).name}")

            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)

            result = await self.operations.edit_file_safe(
                safe_path,
                old_string=old_string,
                new_string=new_string
            )

            # Check if operation failed
            if result.get("status") == "error":
                if status:
                    await status.error(result.get("error", "Unknown error"), meta={
                        "error_type": result.get("error_type", "UnknownError"),
                        "file": str(safe_path)
                    })
                return result

            # Success
            if status:
                changes = result.get("changes", {})
                replacements = changes.get("replacements", 0)
                await status.end(f"Replaced in {safe_path.name}: {replacements} occurrence(s)", meta={
                    "file": str(safe_path),
                    "replacements": replacements
                })

            return result

        except FileNotFoundError:
            error_msg = f"File not found: {params.get('filePath')}"
            if status:
                await status.error(error_msg, meta={"error_type": "FileNotFoundError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            logger.error(f"Unexpected error in replace_string_in_file: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def list_directory(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List directory contents."""
        status = params.get("_status")

        try:
            dir_path = params["dir_path"]
            recursive = params.get("recursive", False)
            pattern = params.get("pattern")
            include_hidden = params.get("include_hidden", False)

            if status:
                msg = f"Listing: {Path(dir_path).name}"
                if pattern:
                    msg += f" ({pattern})"
                await status.progress(msg)

            # Validate path
            safe_path = self.validator.validate_path(dir_path, must_exist=True)

            # List directory
            result = await self.operations.list_directory_safe(
                safe_path,
                recursive=recursive,
                pattern=pattern,
                include_hidden=include_hidden,
                max_results=params.get("max_results", 200),
                include_ignored=params.get("include_ignored", False),
                excludes=self.search_engine._search_backend_options()["excludes"],
            )

            if status:
                total_files = result.get("total_files", 0)
                total_dirs = result.get("total_directories", 0)
                await _end_or_error(
                    status, result,
                    f"Listed {safe_path.name}: {total_files} files, {total_dirs} directories", meta={
                        "directory": str(safe_path),
                        "total_files": total_files,
                        "total_directories": total_dirs,
                        "recursive": recursive
                    })

            return result

        except FileNotFoundError:
            error_msg = f"Directory not found: {params.get('dir_path')}"
            if status:
                await status.error(error_msg, meta={"error_type": "DirectoryNotFoundError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "DirectoryNotFoundError"
            }
        except SecurityError as e:
            error_msg = str(e)
            if status:
                await status.error(error_msg, meta={"error_type": "SecurityError"})
            return {
                "status": "error",
                "error": error_msg,
                "error_type": "SecurityError"
            }
        except Exception as e:
            logger.error(f"Unexpected error in list_directory: {e}", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def search_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search files by glob pattern."""
        status = params.get("_status")

        try:
            pattern = params["pattern"]
            max_results = params.get("max_results", 50)
            include_ignored = params.get("include_ignored", False)

            if status:
                await status.progress(f"Searching files: {pattern}")

            result = await self.search_engine.search_files(
                pattern, max_results, include_ignored=include_ignored)

            if status:
                found = result.get("total_found", 0)
                await _end_or_error(
                    status, result,
                    f"File search '{pattern}': {found} matches", meta={
                        "pattern": pattern,
                        "found": found,
                        "truncated": result.get("truncated", False)
                    })

            return result

        except Exception as e:
            logger.error(f"Unexpected error in search_files: {e}", exc_info=True)
            if status:
                await status.error(f"Search error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def grep_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search text content across files."""
        status = params.get("_status")

        try:
            query = params["query"]
            is_regex = params.get("is_regex", False)
            include_pattern = params.get("include_pattern")
            case_sensitive = params.get("case_sensitive", False)
            max_results = params.get("max_results", 100)
            context_lines = params.get("context_lines", 2)
            include_ignored = params.get("include_ignored", False)

            if status:
                msg = f"Searching text: '{query[:40]}...'"
                if include_pattern:
                    msg += f" in {include_pattern}"
                await status.progress(msg)

            result = await self.search_engine.grep_search(
                query=query,
                is_regex=is_regex,
                include_pattern=include_pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
                context_lines=context_lines,
                include_ignored=include_ignored
            )

            if status:
                matches = result.get("total_matches", 0)
                files = result.get("total_files", 0)
                await _end_or_error(
                    status, result,
                    f"Grep '{query[:40]}': {matches} matches in {files} files", meta={
                        "query": query[:50],
                        "matches": matches,
                        "files": files,
                        "truncated": result.get("truncated", False)
                    })

            return result

        except Exception as e:
            logger.error(f"Unexpected error in grep_search: {e}", exc_info=True)
            if status:
                await status.error(f"Grep error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def semantic_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Semantic/AI-powered search using embeddings."""
        status = params.get("_status")

        try:
            query = params["query"]
            max_results = params.get("max_results", 10)
            filter_pattern = params.get("filter_pattern")

            if status:
                msg = f"Semantic search: '{query[:40]}...'"
                if filter_pattern:
                    msg += f" ({filter_pattern})"
                await status.progress(msg)

            result = await self.search_engine.semantic_search(
                query=query,
                max_results=max_results,
                filter_pattern=filter_pattern
            )

            if status:
                count = result.get("count", 0)
                await _end_or_error(
                    status, result,
                    f"Semantic search '{query[:40]}': {count} matches", meta={
                        "query": query[:50],
                        "count": count,
                        "filter": filter_pattern
                    })

            return result

        except Exception as e:
            logger.error(f"Unexpected error in semantic_search: {e}", exc_info=True)
            if status:
                await status.error(f"Semantic search error: {e}", meta={"error_type": type(e).__name__})
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }

    async def shutdown(self):
        """Cleanup on shutdown."""
        await self.search_engine.stop()
        await super().shutdown()


PLUGIN_FACTORY = FileOpsServer
