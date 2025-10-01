"""Log Viewer Web Endpoints

Provides web UI endpoints for real-time log streaming and viewing.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.web_adapter import PluginWebInterface

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

# Create a separate logger for log viewer that doesn't write to files to avoid infinite loops
logger = logging.getLogger(__name__)
log_viewer_logger = logging.getLogger('log_viewer_silent')
log_viewer_logger.setLevel(logging.INFO)
# Only add console handler, no file handlers to avoid recursive logging
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
log_viewer_logger.addHandler(console_handler)
log_viewer_logger.propagate = False  # Don't propagate to parent loggers


class LogViewerWebEndpoints(PluginWebInterface):
    """Web endpoints component for log viewer plugin"""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Configuration
        self.log_files = getattr(mcp_config, 'log_files', ['logs/agent.log', 'logs/api.log'])
        self.max_lines = getattr(mcp_config, 'max_lines', 20)  # Reduced from 100 to 20
        self.refresh_interval = getattr(mcp_config, 'refresh_interval', 1.0)
        
        # Initialize templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))
        
        logger.info(f"LogViewerWebEndpoints initialized: {name}")
    
    def _group_multiline_entries(self, lines: List[str]) -> List[Dict[str, Any]]:
        """Group multiline log entries together"""
        grouped = []
        current_entry = None
        line_number = 1
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
                
            # Check if this line starts a new log entry (has timestamp pattern)
            timestamp_pattern = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})'
            if re.match(timestamp_pattern, line):
                # Save previous entry if exists
                if current_entry:
                    grouped.append(current_entry)
                
                # Start new entry
                current_entry = {
                    'main_line': line,
                    'continuation_lines': [],
                    'line_number': line_number,
                    'full_content': line.rstrip()
                }
            else:
                # This is a continuation line
                if current_entry:
                    current_entry['continuation_lines'].append(line.rstrip())
                    current_entry['full_content'] += '\n' + line.rstrip()
            
            line_number += 1
        
        # Add the last entry
        if current_entry:
            grouped.append(current_entry)
            
        return grouped

    def _parse_log_line(self, line: str) -> Dict[str, Any]:
        """Parse log line to extract timestamp, level, and message"""
        # Try to parse format: "2025-09-25 00:23:32,790 INFO api.endpoints Getting plugin UI metadata..."
        pattern = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(\w+)\s+([^\s]+)\s+(.*)$'
        match = re.match(pattern, line)
        
        if match:
            timestamp, level, logger_name, message = match.groups()
            return {
                'timestamp': timestamp,
                'level': level,
                'logger': logger_name,
                'message': f"{logger_name} {message}",  # Include logger name in message
                'line': line,
                'type': 'parsed'
            }
        else:
            # Fallback for unparseable lines
            return {
                'message': line,
                'line': line,
                'level': 'INFO',
                'type': 'raw'
            }
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router with log viewer endpoints"""
        router = APIRouter(prefix=f"/plugins/{self.name}")
        
        @router.get("/logs/list")
        async def list_log_files():
            """List available log files"""
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
            
            return {"logs": available_logs}
        
        @router.get("/logs/content/{log_name:path}")
        async def get_log_content(log_name: str, lines: int = 50, since_timestamp: float = None):
            """Get log file content with optional filtering by timestamp"""
            # Use the silent logger to avoid recursive logging
            log_viewer_logger.debug(f"Fetching log content for {log_name}, lines={lines}, since_timestamp={since_timestamp}")
            
            # Security check - only allow configured log files
            if log_name not in self.log_files:
                return {"error": "Log file not allowed"}
            
            log_path = Path(log_name)
            if not log_path.exists():
                return {"error": f"Log file {log_name} not found"}
            
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    # Get all lines for proper line number calculation
                    file_lines = f.readlines()
                    
                    parsed_lines = []
                    file_stat = log_path.stat()
                    
                    if since_timestamp is None:
                        # Initial load - get last N lines and group multiline entries
                        recent_lines = file_lines[-lines*3:] if len(file_lines) > lines*3 else file_lines
                        grouped_entries = self._group_multiline_entries(recent_lines)
                        
                        # Take only the last N grouped entries
                        grouped_entries = grouped_entries[-lines:]
                        
                        for entry in grouped_entries:
                            # Filter out log viewer requests to avoid recursion
                            if '/plugins/log_viewer' in entry['main_line']:
                                continue
                                
                            parsed_line = self._parse_log_line(entry['main_line'].rstrip())
                            parsed_line['line_number'] = entry['line_number']
                            parsed_line['full_content'] = entry['full_content']
                            parsed_line['has_multiline'] = len(entry['continuation_lines']) > 0
                            parsed_lines.append(parsed_line)
                    else:
                        # Streaming mode - for now, just handle single lines (multiline support complex for streaming)
                        # TODO: Add proper multiline support for streaming mode
                        for line_num, line in enumerate(file_lines, 1):
                            if line.strip():  # Skip empty lines
                                # Filter out log viewer requests to avoid recursion
                                if '/plugins/log_viewer' in line:
                                    continue
                                
                                # Only process lines that look like new log entries (have timestamp)
                                timestamp_pattern = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})'
                                if not re.match(timestamp_pattern, line):
                                    continue  # Skip continuation lines during streaming for now
                                    
                                parsed_line = self._parse_log_line(line.rstrip())
                                
                                # Only include lines newer than since_timestamp
                                try:
                                    # Handle timestamp format: "2025-09-25 01:34:13,790"
                                    timestamp_str = parsed_line['timestamp']
                                    # Convert comma to dot for microseconds
                                    timestamp_str = timestamp_str.replace(',', '.')
                                    line_time = time.mktime(time.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f'))
                                    if line_time <= since_timestamp:
                                        continue
                                    log_viewer_logger.debug(f"Including line {line_num} with timestamp {line_time} > {since_timestamp}")
                                except (ValueError, KeyError) as e:
                                    # If we can't parse timestamp, skip to be safe during streaming
                                    log_viewer_logger.debug(f"Skipping line {line_num} due to timestamp parse error: {e}")
                                    continue
                                
                                parsed_line['line_number'] = line_num
                                parsed_line['full_content'] = line.rstrip()  # For streaming, same as main line for now
                                parsed_line['has_multiline'] = False
                                parsed_lines.append(parsed_line)
                    
                    log_viewer_logger.debug(f"Returning {len(parsed_lines)} lines for {log_name} (streaming: {since_timestamp is not None})")
                    return {
                        "lines": parsed_lines,
                        "total_lines": len(file_lines),
                        "file_size": file_stat.st_size,
                        "last_modified": file_stat.st_mtime,
                        "current_timestamp": time.time()
                    }
            except Exception as e:
                log_viewer_logger.error(f"Failed to read log file {log_name}: {str(e)}")
                return {"error": f"Failed to read log file: {str(e)}"}
        
        @router.get("/logs/download/{log_name:path}")
        async def download_log_file(log_name: str):
            """Download entire log file"""
            # Security check
            if log_name not in self.log_files:
                return {"error": "Log file not allowed"}
            
            log_path = Path(log_name)
            if not log_path.exists():
                return {"error": f"Log file {log_name} not found"}
            
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                
                from fastapi import Response
                return Response(
                    content, 
                    media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename={log_path.name}"}
                )
            except Exception as e:
                return {"error": f"Failed to read log: {str(e)}"}

        @router.get("/logs/poll/{log_name:path}")
        async def poll_log_updates(log_name: str, last_timestamp: float = 0, lines: int = 20):
            """Poll for log updates since last timestamp (non-streaming approach)"""
            log_viewer_logger.debug(f"Polling log updates for {log_name} since {last_timestamp}")
            
            # Security check - only allow configured log files
            if log_name not in self.log_files:
                return {"error": "Log file not allowed"}
            
            log_path = Path(log_name)
            if not log_path.exists():
                return {"error": f"Log file {log_name} not found"}
            
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    # Get recent lines
                    file_lines = f.readlines()
                    recent_lines = file_lines[-lines:] if len(file_lines) > lines else file_lines
                    
                    new_lines = []
                    file_stat = log_path.stat()
                    
                    for line in recent_lines:
                        if line.strip():
                            # Filter out log viewer requests to avoid recursion
                            if '/plugins/log_viewer' in line:
                                continue
                                
                            parsed_line = self._parse_log_line(line.rstrip())
                            
                            # Only include lines newer than last_timestamp
                            try:
                                line_timestamp = parsed_line.get('timestamp', '')
                                if line_timestamp:
                                    # Parse timestamp: "2025-09-25 00:23:32,790"
                                    line_time = time.mktime(time.strptime(line_timestamp[:19], '%Y-%m-%d %H:%M:%S'))
                                    # Add milliseconds
                                    line_time += float(line_timestamp[20:]) / 1000
                                    
                                    if line_time > last_timestamp:
                                        new_lines.append(parsed_line)
                            except (ValueError, KeyError):
                                # If we can't parse timestamp, include the line for safety
                                new_lines.append(parsed_line)
                    
                    return {
                        "new_lines": new_lines,
                        "current_timestamp": time.time(),
                        "file_size": file_stat.st_size,
                        "last_modified": file_stat.st_mtime,
                        "has_updates": len(new_lines) > 0
                    }
            except Exception as e:
                log_viewer_logger.error(f"Failed to poll log file {log_name}: {str(e)}")
                return {"error": f"Failed to poll log file: {str(e)}"}
        
        @router.get("/panel.html", response_class=HTMLResponse)
        async def panel_html(request: Request):
            """Serve the log viewer panel HTML"""
            return self.templates.TemplateResponse(
                request,
                "panel.html", 
                {"plugin_name": self.name, "timestamp": int(time.time())}
            )
        
        @router.get("/panel", response_class=HTMLResponse)
        async def panel(request: Request):
            """Serve the log viewer panel HTML (alternate endpoint)"""
            return self.templates.TemplateResponse(
                request,
                "panel.html", 
                {"plugin_name": self.name, "timestamp": int(time.time())}
            )
        
        @router.get("/static/{file_path:path}")
        async def serve_static(file_path: str):
            """Serve static files (CSS, JS, images)"""
            from fastapi import HTTPException
            from fastapi.responses import FileResponse
            
            static_dir = Path(__file__).parent / "static"
            file_full_path = static_dir / file_path
            
            # Security check - ensure file is within static directory
            try:
                file_full_path = file_full_path.resolve()
                static_dir = static_dir.resolve()
                if not str(file_full_path).startswith(str(static_dir)):
                    raise HTTPException(status_code=404, detail="File not found")
            except (OSError, ValueError):
                raise HTTPException(status_code=404, detail="File not found")
            
            if not file_full_path.exists() or not file_full_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            
            # Determine media type based on file extension
            media_type = "text/plain"
            if file_path.endswith('.css'):
                media_type = "text/css"
            elif file_path.endswith('.js'):
                media_type = "application/javascript"
            elif file_path.endswith('.html'):
                media_type = "text/html"
            elif file_path.endswith(('.png', '.jpg', '.jpeg')):
                media_type = "image/*"
            
            return FileResponse(
                path=file_full_path,
                media_type=media_type,
                headers={"Cache-Control": "max-age=3600"}  # Cache for 1 hour
            )
        
        return router
    
    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets"""
        # Static assets will be in the plugin directory
        static_path = Path(__file__).parent / "static"
        return static_path if static_path.exists() else None
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Return UI panel configuration"""
        return [{
            "id": f"{self.name}_panel",
            "title": "System Logs",
            "url": f"/plugins/{self.name}/panel.html",
            "icon": "file-text",
            "position": "bottom",
            "width": "100%",
            "height": "400px"
        }]
    
    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration"""
        return {
            "require_auth": False,  # For demo purposes
            "cors_origins": [],
            "rate_limit": "30/minute"
        }