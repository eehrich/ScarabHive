"""File Operations MCP Server implementation."""

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


class FileOperationsServer(SchemaBasedMCPServer):
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
        try:
            file_path = params["file_path"]
            offset = params.get("offset", 0)
            limit = params.get("limit")
            encoding = params.get("encoding", "utf-8")
            
            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)
            
            if not safe_path.is_file():
                return {
                    "status": "error",
                    "error": f"Path is not a file: {file_path}",
                    "error_type": "NotAFileError"
                }
            
            # Read file
            return await self.operations.read_file_safe(
                safe_path, offset=offset, limit=limit, encoding=encoding
            )
        
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
        try:
            file_path = params["file_path"]
            content = params["content"]
            overwrite = params.get("overwrite", False)
            create_dirs = params.get("create_dirs", True)
            encoding = params.get("encoding", "utf-8")
            
            # Validate path
            safe_path = self.validator.validate_path(file_path)
            
            # Create file
            return await self.operations.create_file_safe(
                safe_path,
                content=content,
                overwrite=overwrite,
                create_dirs=create_dirs,
                encoding=encoding
            )
        
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
        try:
            file_path = params["file_path"]
            mode = params["mode"]
            
            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)
            
            # Edit file
            return await self.operations.edit_file_safe(
                safe_path,
                mode=mode,
                content=params.get("content"),
                old_string=params.get("old_string"),
                new_string=params.get("new_string"),
                line_number=params.get("line_number")
            )
        
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
        try:
            file_path = params["file_path"]
            confirm = params.get("confirm", False)
            
            if not confirm:
                return {
                    "status": "error",
                    "error": "Deletion requires confirm=true parameter",
                    "error_type": "ConfirmationRequired"
                }
            
            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)
            
            # Delete file
            return await self.operations.delete_file_safe(safe_path)
        
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
        try:
            dir_path = params["dir_path"]
            recursive = params.get("recursive", False)
            pattern = params.get("pattern")
            include_hidden = params.get("include_hidden", False)
            
            # Validate path
            safe_path = self.validator.validate_path(dir_path, must_exist=True)
            
            # List directory
            return await self.operations.list_directory_safe(
                safe_path,
                recursive=recursive,
                pattern=pattern,
                include_hidden=include_hidden
            )
        
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
    
    async def file_exists(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Check if file or directory exists."""
        try:
            path_str = params["path"]
            
            # Validate path (don't require existence)
            safe_path = self.validator.validate_path(path_str)
            
            return await self.operations.file_exists_safe(safe_path)
        
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in file_exists: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def get_file_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get file metadata."""
        try:
            file_path = params["file_path"]
            
            # Validate path
            safe_path = self.validator.validate_path(file_path, must_exist=True)
            
            return await self.operations.get_file_info_safe(safe_path)
        
        except FileNotFoundError:
            return {
                "status": "error",
                "error": f"Path not found: {params.get('file_path')}",
                "error_type": "FileNotFoundError"
            }
        except SecurityError as e:
            return {
                "status": "error",
                "error": str(e),
                "error_type": "SecurityError"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error in get_file_info: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def search_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search files by glob pattern."""
        try:
            pattern = params["pattern"]
            max_results = params.get("max_results", 50)
            
            return await self.search_engine.search_files(pattern, max_results)
        
        except Exception as e:
            self.logger.error(f"Unexpected error in search_files: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def grep_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search text content across files."""
        try:
            query = params["query"]
            is_regex = params.get("is_regex", False)
            include_pattern = params.get("include_pattern")
            case_sensitive = params.get("case_sensitive", False)
            max_results = params.get("max_results", 100)
            context_lines = params.get("context_lines", 2)
            
            return await self.search_engine.grep_search(
                query=query,
                is_regex=is_regex,
                include_pattern=include_pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
                context_lines=context_lines
            )
        
        except Exception as e:
            self.logger.error(f"Unexpected error in grep_search: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__
            }
    
    async def shutdown(self):
        """Cleanup on shutdown."""
        await self.search_engine.stop()
        await super().shutdown()


PLUGIN_FACTORY = FileOperationsServer
