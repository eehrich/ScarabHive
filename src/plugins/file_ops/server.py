"""File Ops MCP Server implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from agent_system.config import AgentSystemConfig, MCPConfig
from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .security import PathValidator, SecurityError
from .operations import FileOperations
from .search import FileSearchEngine


logger = logging.getLogger(__name__)


class FileOpsServer(SchemaBasedMCPServer):
    """MCP server providing secure file operations with search capabilities."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """
        Initialize file operations server.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract configuration
        allowed_dirs = getattr(mcp_config, "allowed_directories", [])
        if not allowed_dirs:
            # Default to project root subdirectories
            project_root = Path(system_config.project_root)
            allowed_dirs = [
                str(project_root / "src"),
                str(project_root / "docs"),
                str(project_root / "tests"),
                str(project_root / "tmp")
            ]
        
        # Initialize components
        self.validator = PathValidator(allowed_dirs)
        self.operations = FileOperations()
        
        # Initialize search engine with configuration
        search_config = getattr(mcp_config, "search", {})
        self.search_engine = FileSearchEngine(self.validator.allowed_dirs, search_config)
        
        logger.info(f"FileOperationsServer initialized with {len(allowed_dirs)} allowed directories")
        logger.info(f"Search indexing: {search_config.get('enable_indexing', True)}")
    
    async def read_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Read text file contents with pagination."""
        status = params.get("_status")
        
        try:
            file_path = params["file_path"]
            offset = params.get("offset", 0)
            limit = params.get("limit")
            encoding = params.get("encoding", "utf-8")
            
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
                await status.end(f"Read {safe_path.name}: {lines_read}/{total_lines} lines", meta={
                    "file": str(safe_path),
                    "lines_read": lines_read,
                    "total_lines": total_lines
                })
            
            return result
        
        except FileNotFoundError:
            return {
                "status": "error",
                "error": f"File not found: {params.get('file_path')}",
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in read_file: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def create_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new file."""
        status = params.get("_status")
        
        try:
            file_path = params["file_path"]
            content = params["content"]
            overwrite = params.get("overwrite", False)
            create_dirs = params.get("create_dirs", True)
            encoding = params.get("encoding", "utf-8")
            
            if status:
                await status.progress(f"Creating: {Path(file_path).name}")
            
            # Validate path
            safe_path = self.validator.validate_path(file_path)
            
            # Create file
            result = await self.operations.create_file_safe(
                safe_path,
                content=content,
                overwrite=overwrite,
                create_dirs=create_dirs,
                encoding=encoding
            )
            
            if status:
                bytes_written = result.get("bytes_written", 0)
                await status.end(f"Created {safe_path.name}: {bytes_written} bytes", meta={
                    "file": str(safe_path),
                    "bytes": bytes_written
                })
            
            return result
        
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in create_file: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def edit_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Edit an existing file."""
        status = params.get("_status")
        
        try:
            file_path = params["file_path"]
            mode = params["mode"]
            
            if status:
                await status.progress(f"Editing ({mode}): {Path(file_path).name}")
            
            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)
            
            # Edit file
            result = await self.operations.edit_file_safe(
                safe_path,
                mode=mode,
                content=params.get("content"),
                old_string=params.get("old_string"),
                new_string=params.get("new_string"),
                line_number=params.get("line_number")
            )
            
            if status:
                changes = result.get("changes", {})
                if mode == "replace":
                    await status.end(f"Edited {safe_path.name}: {changes.get('replacements', 0)} replacements", meta={
                        "file": str(safe_path),
                        "mode": mode,
                        "replacements": changes.get("replacements", 0)
                    })
                elif mode == "append":
                    await status.end(f"Edited {safe_path.name}: appended content", meta={
                        "file": str(safe_path),
                        "mode": mode
                    })
                elif mode == "insert":
                    await status.end(f"Edited {safe_path.name}: inserted at line {params.get('line_number', 0)}", meta={
                        "file": str(safe_path),
                        "mode": mode,
                        "line": params.get("line_number", 0)
                    })
            
            return result
        
        except FileNotFoundError:
            return {
                "status": "error",
                "error": f"File not found: {params.get('file_path')}",
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in edit_file: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def delete_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a file."""
        status = params.get("_status")
        
        try:
            file_path = params["file_path"]
            confirm = params.get("confirm", False)
            
            if status:
                await status.progress(f"Deleting: {Path(file_path).name}")
            
            if not confirm:
                return {
                    "status": "error",
                    "error": "Deletion requires confirm=true parameter",
                    "error_type": "ConfirmationRequired"
                }
            
            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)
            
            # Delete file
            result = await self.operations.delete_file_safe(safe_path)
            
            if status:
                await status.end(f"Deleted: {safe_path.name}", meta={
                    "file": str(safe_path)
                })
            
            return result
        
        except FileNotFoundError:
            return {
                "status": "error",
                "error": f"File not found: {params.get('file_path')}",
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in delete_file: {e}", exc_info=True)
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
                include_hidden=include_hidden
            )
            
            if status:
                total = result.get("total_items", 0)
                await status.end(f"Listed {safe_path.name}: {total} items", meta={
                    "directory": str(safe_path),
                    "total_items": total,
                    "recursive": recursive
                })
            
            return result
        
        except FileNotFoundError:
            return {
                "status": "error",
                "error": f"Directory not found: {params.get('dir_path')}",
                "error_type": "DirectoryNotFoundError"
            }
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in list_directory: {e}", exc_info=True)
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
            
            if status:
                await status.progress(f"Searching files: {pattern}")
            
            result = await self.search_engine.search_files(pattern, max_results)
            
            if status:
                found = result.get("total_found", 0)
                await status.end(f"File search '{pattern}': {found} matches", meta={
                    "pattern": pattern,
                    "found": found,
                    "truncated": result.get("truncated", False)
                })
            
            return result
        
        except Exception as e:
            self.logger.error(f"Unexpected error in search_files: {e}", exc_info=True)
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
                context_lines=context_lines
            )
            
            if status:
                matches = result.get("total_matches", 0)
                files = result.get("total_files", 0)
                await status.end(f"Grep '{query[:30]}': {matches} matches in {files} files", meta={
                    "query": query[:50],
                    "matches": matches,
                    "files": files,
                    "truncated": result.get("truncated", False)
                })
            
            return result
        
        except Exception as e:
            self.logger.error(f"Unexpected error in grep_search: {e}", exc_info=True)
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
                await status.end(f"Semantic search '{query[:30]}': {count} matches", meta={
                    "query": query[:50],
                    "count": count,
                    "filter": filter_pattern
                })
            
            return result
        
        except Exception as e:
            self.logger.error(f"Unexpected error in semantic_search: {e}", exc_info=True)
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
