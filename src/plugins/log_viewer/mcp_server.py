"""Log Viewer MCP Server

Provides MCP tools for log management and querying.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from agent_system.mcp.base import MCPServer

logger = logging.getLogger(__name__)


class LogViewerMCPServer(MCPServer):
    """MCP server component for log viewer plugin"""
    
    def __init__(self, name: str, config: Dict[str, Any], ssl_verify: bool = True):
        super().__init__(name, config, ssl_verify)
        self.log_files = config.get('log_files', ['logs/agent.log', 'logs/api.log'])
        
        logger.info(f"LogViewerMCPServer initialized: {name}")
    
    def get_default_action(self) -> str:
        """Return the default action for log viewer"""
        return "list_log_files"
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """MCP call interface - handle tool calls"""
        if tool == "list_log_files":
            return await self._list_log_files(params)
        elif tool == "get_log_tail":
            return await self._get_log_tail(params)
        elif tool == "search_logs":
            return await self._search_logs(params)
        else:
            raise ValueError(f"Unknown tool: {tool}")
    
    def get_tools(self) -> List[Dict[str, Any]]:
        """Return available MCP tools"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_log_files",
                    "description": "List available log files and their status",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_log_tail",
                    "description": "Get the last N lines from a log file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "log_file": {
                                "type": "string",
                                "description": "Path to the log file"
                            },
                            "lines": {
                                "type": "integer",
                                "description": "Number of lines to retrieve (default: 50)",
                                "default": 50
                            }
                        },
                        "required": ["log_file"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_logs",
                    "description": "Search for patterns in log files",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Search pattern (regex supported)"
                            },
                            "log_file": {
                                "type": "string",
                                "description": "Specific log file to search (optional)"
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum number of results (default: 100)",
                                "default": 100
                            }
                        },
                        "required": ["pattern"]
                    }
                }
            }
        ]
    
    async def _list_log_files(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List available log files and their status"""
        return {"logs": self._get_available_logs()}
    
    async def _get_log_tail(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Get the last N lines from a log file"""
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
    
    async def _get_log_tail(self, log_file: str, lines: int) -> Dict[str, Any]:
        """Get tail of log file"""
        if log_file not in self.log_files:
            return {"error": f"Log file {log_file} not allowed"}
        
        log_path = Path(log_file)
        if not log_path.exists():
            return {"error": f"Log file {log_file} not found"}
        
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
                tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                
                return {
                    "log_file": log_file,
                    "lines": [line.rstrip() for line in tail_lines],
                    "total_lines": len(all_lines),
                    "returned_lines": len(tail_lines)
                }
        except Exception as e:
            return {"error": f"Failed to read log: {str(e)}"}
    
    async def _search_logs(self, pattern: str, log_file: str = None, max_results: int = 100) -> Dict[str, Any]:
        """Search for pattern in logs"""
        import re
        
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex pattern: {str(e)}"}
        
        results = []
        files_to_search = [log_file] if log_file else self.log_files
        
        for file_path in files_to_search:
            if file_path not in self.log_files:
                continue
                
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
                                "line": line.rstrip(),
                                "matches": [m.group() for m in regex.finditer(line)]
                            })
                            
                            if len(results) >= max_results:
                                break
                    
                    if len(results) >= max_results:
                        break
                        
            except Exception as e:
                logger.warning(f"Failed to search {file_path}: {e}")
                continue
        
        return {
            "pattern": pattern,
            "results": results,
            "total_matches": len(results),
            "truncated": len(results) >= max_results
        }