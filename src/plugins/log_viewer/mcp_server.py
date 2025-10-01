"""Log Viewer MCP Server

Provides MCP tools for log management and querying.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class LogViewerMCPServer(SchemaBasedMCPServer):
    """MCP server component for log viewer plugin"""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with new signature."""
        super().__init__(name, system_config, mcp_config)
        self.log_files = getattr(mcp_config, 'log_files', ['logs/agent.log', 'logs/api.log'])
        
        logger.info(f"LogViewerMCPServer initialized: {name}")
    
    def get_default_action(self) -> str:
        """Return the default action for log viewer"""
        return "list_log_files"
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """MCP call interface - handle tool calls"""
        status = params.get("_status")
        
        # Status is mandatory for proper operation tracking
        if not status:
            raise ValueError("Missing required _status parameter - call should be made through call_with_status")

        # Publish status for operation start
        await status.progress(f"Processing {tool} operation")

        try:
            if tool == "list_log_files":
                result = await self._list_log_files(params)
            elif tool == "get_log_tail":
                result = await self._get_log_tail(params)
            elif tool == "search_logs":
                result = await self._search_logs(params)
            else:
                result = {"error": f"Unknown tool: {tool}"}
        except Exception as e:
            result = {"error": str(e)}
            logger.exception(f"Error in tool {tool}: {e}")

        # Publish status for operation completion
        status_msg = f"Completed {tool} operation"
        if "error" in result:
            await status.error(status_msg)
        else:
            await status.end(status_msg)

        return result
    

    
    async def _list_log_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List available log files and their status"""
        return {"logs": self._get_available_logs()}
    
    async def _get_log_tail(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get the last N lines from a log file"""
        # Check for cancellation before log file access
        cancellation_token = params.get("_cancellation_token")
        if cancellation_token and cancellation_token.is_cancelled:
            return {"error": "Log tail request cancelled by user", "cancelled": True}

        log_file = params.get("log_file")
        lines = params.get("lines", 50)
        
        if not log_file:
            raise ValueError("Missing required parameter: log_file")
        
        if log_file not in self.log_files:
            return {"error": f"Log file {log_file} not in allowed list"}
        
        log_path = Path(log_file)
        if not log_path.exists():
            return {"error": f"Log file {log_file} not found"}
        
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                
                return {
                    "log_file": log_file,
                    "lines": [line.rstrip('\n\r') for line in tail_lines],
                    "total_lines": len(all_lines),
                    "returned_lines": len(tail_lines)
                }
        except Exception as e:
            return {"error": f"Failed to read log file: {str(e)}"}
    
    async def _search_logs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Search for patterns in log files"""
        pattern = params.get("pattern")
        log_file = params.get("log_file")  # Optional - search specific file
        max_results = params.get("max_results", 100)
        
        if not pattern:
            raise ValueError("Missing required parameter: pattern")
        
        import re
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex pattern: {str(e)}"}
        
        results = []
        search_files = [log_file] if log_file else self.log_files
        
        for file_path in search_files:
            if file_path not in self.log_files:
                continue  # Skip files not in allowed list
                
            log_path = Path(file_path)
            if not log_path.exists():
                continue
                
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append({
                                "file": file_path,
                                "line_number": line_num,
                                "line": line.rstrip('\n\r'),
                                "match": True
                            })
                            
                            if len(results) >= max_results:
                                break
                    
                    if len(results) >= max_results:
                        break
                        
            except Exception as e:
                results.append({
                    "file": file_path,
                    "error": f"Failed to read file: {str(e)}"
                })
        
        return {
            "pattern": pattern,
            "results": results,
            "total_matches": len([r for r in results if r.get("match")]),
            "files_searched": len([f for f in search_files if Path(f).exists()]),
            "truncated": len(results) >= max_results
        }
    
    def _get_available_logs(self) -> List[Dict[str, Any]]:
        """Get list of available log files with metadata"""
        available_logs = []
        for log_file in self.log_files:
            log_path = Path(log_file)
            if log_path.exists():
                stat = log_path.stat()
                available_logs.append({
                    "name": log_file,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "exists": True
                })
            else:
                available_logs.append({
                    "name": log_file,
                    "exists": False
                })
        return available_logs