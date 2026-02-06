"""Log Viewer Web Endpoints

Provides web UI endpoints for real-time log streaming and viewing.
"""

from __future__ import annotations

import asyncio
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
    from .mcp_server import LogViewerMCPServer

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

    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig", server: "LogViewerMCPServer"):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.server = server  # MCP server has get_schema_data()

        # Configuration
        default_log_files = [
            'logs/agent-cli.log',
            'logs/api.log',
            'logs/cli.log',
            'logs/http_server.log',
            'logs/llm_router.log'
        ]
        self.log_files = getattr(mcp_config, 'log_files', default_log_files)
        self.max_lines = getattr(mcp_config, 'max_lines', 20)  # Reduced from 100 to 20
        self.refresh_interval = getattr(mcp_config, 'refresh_interval', 1.0)

        # Initialize templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))

        logger.info(f"LogViewerWebEndpoints initialized: {name}")

    def _find_rotation_files(self, base_log_path: Path) -> List[Path]:
        """Find all rotation files for a base log file.
        
        For example, if base_log_path is 'logs/agent.log', returns:
        [Path('logs/agent.log'), Path('logs/agent.log.1'), Path('logs/agent.log.2'), ...]
        in oldest-to-newest order (so .log is last).
        """
        rotation_files = []
        
        # Check base file
        if base_log_path.exists():
            rotation_files.append(base_log_path)
        
        # Check for rotation files (.log.1, .log.2, etc.)
        index = 1
        max_rotations = 100  # Safety limit
        while index <= max_rotations:
            rotation_path = Path(f"{base_log_path}.{index}")
            if rotation_path.exists():
                rotation_files.append(rotation_path)
                index += 1
            else:
                break
        
        # Return in oldest-to-newest order (highest number first, base file last)
        return sorted(rotation_files, key=lambda p: (
            0 if str(p) == str(base_log_path) else int(str(p).rsplit('.', 1)[-1])
        ), reverse=True)

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
            # Support multiple formats:
            # - "2025-09-25 00:23:32,790" (api.log with milliseconds)
            # - "2026-01-05 20:47:21" (profiling.log without milliseconds)
            # - "2025-09-25 00:23:32,790 - name - LEVEL" (early logging with dashes)
            timestamp_pattern = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)'
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

    async def _read_last_lines_async(self, file_path: Path, max_lines: int) -> List[str]:
        """Read last N lines from file efficiently (from end backwards)"""
        try:
            # Run file reading in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._read_last_lines_sync, file_path, max_lines)
        except Exception as e:
            log_viewer_logger.error(f"Error reading file {file_path}: {e}")
            return []

    async def _read_filtered_lines_async(self, file_path: Path, target_count: int,
                                          level_filter: Optional[set] = None,
                                          search_term: Optional[str] = None) -> List[str]:
        """Read lines from file end backwards until target_count filtered lines are found.
        
        This is the key fix: instead of reading N lines and then filtering,
        we keep reading backwards until we have N lines that MATCH the filter.
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._read_filtered_lines_sync, file_path, target_count, level_filter, search_term
            )
        except Exception as e:
            log_viewer_logger.error(f"Error reading filtered lines from {file_path}: {e}")
            return []

    def _read_filtered_lines_sync(self, file_path: Path, target_count: int,
                                   level_filter: Optional[set] = None,
                                   search_term: Optional[str] = None) -> List[str]:
        """Read lines from file end backwards until target_count filtered lines are found.
        
        Returns raw lines (not parsed) that match the filters.
        """
        CHUNK_SIZE = 32768  # 32KB chunks for better performance
        MAX_LINES_TO_SCAN = 50000  # Safety limit to prevent reading entire huge files
        
        # Pattern to detect start of log entry
        timestamp_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)')
        # Pattern to extract log level - handles both formats:
        # "2025-09-25 00:23:32,790 INFO ..." and "2025-09-25 00:23:32,790 - name - INFO ..."
        level_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?(?:\s+-\s+[\w.]+\s+-)?\s+(\w+)')
        
        def line_matches_filter(line: str) -> bool:
            """Check if a line matches level and search filters"""
            # Skip log viewer requests to avoid recursion
            if '/plugins/log_viewer' in line:
                return False
            
            # Level filter check
            if level_filter:
                match = level_pattern.match(line)
                if match:
                    line_level = match.group(1).lower()
                    if line_level not in level_filter:
                        return False
                else:
                    # Can't determine level, skip if filtering by level
                    return False
            
            # Search filter check
            if search_term:
                if search_term not in line.lower():
                    return False
            
            return True
        
        with open(file_path, 'rb') as f:
            # Get file size
            f.seek(0, 2)
            file_size = f.tell()
            
            if file_size == 0:
                return []
            
            # Collect all lines first, then filter
            all_lines: List[str] = []
            remaining = file_size
            lines_scanned = 0
            
            while remaining > 0 and lines_scanned < MAX_LINES_TO_SCAN:
                chunk_size = min(CHUNK_SIZE, remaining)
                remaining -= chunk_size
                
                f.seek(remaining)
                chunk = f.read(chunk_size)
                
                try:
                    text = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    text = chunk.decode('utf-8', errors='replace')
                
                chunk_lines = text.split('\n')
                
                # Merge with previous partial line
                if all_lines:
                    chunk_lines[-1] += all_lines[0]
                    all_lines = chunk_lines + all_lines[1:]
                else:
                    all_lines = chunk_lines
                
                lines_scanned = len(all_lines)
                
                # Quick check: count matching lines so far from the END
                # If we have enough, stop reading
                matching_count = 0
                for line in reversed(all_lines):
                    if timestamp_pattern.match(line) and line_matches_filter(line):
                        matching_count += 1
                        if matching_count >= target_count:
                            break
                
                if matching_count >= target_count:
                    break
            
            # Remove empty trailing line
            if all_lines and not all_lines[-1]:
                all_lines = all_lines[:-1]
            
            # Now filter and return last N matching lines
            # Process from end to preserve order
            filtered_lines: List[str] = []
            current_entry_lines: List[str] = []
            
            for line in reversed(all_lines):
                if timestamp_pattern.match(line):
                    # This is a new log entry start
                    if current_entry_lines:
                        # We were building an entry, check if it matches
                        full_entry = '\n'.join(reversed(current_entry_lines))
                        # The first line (now last in current_entry_lines) is the main line
                        main_line = current_entry_lines[-1]
                        if line_matches_filter(main_line):
                            # Insert at beginning to maintain order
                            filtered_lines.insert(0, full_entry)
                            if len(filtered_lines) >= target_count:
                                break
                    current_entry_lines = [line]
                else:
                    # Continuation line
                    current_entry_lines.append(line)
            
            # Don't forget the last entry (first in file)
            if current_entry_lines and len(filtered_lines) < target_count:
                full_entry = '\n'.join(reversed(current_entry_lines))
                main_line = current_entry_lines[-1]
                if line_matches_filter(main_line):
                    filtered_lines.insert(0, full_entry)
            
            return filtered_lines[-target_count:] if len(filtered_lines) > target_count else filtered_lines

    def _read_last_lines_sync(self, file_path: Path, max_lines: int) -> List[str]:
        """Read last N lines from file efficiently using tail-like approach"""
        CHUNK_SIZE = 8192  # Read in 8KB chunks
        
        with open(file_path, 'rb') as f:
            # Get file size
            f.seek(0, 2)  # Seek to end
            file_size = f.tell()
            
            if file_size == 0:
                return []
            
            # Start from end and read backwards
            lines = []
            remaining = file_size
            
            while remaining > 0 and len(lines) < max_lines:
                # Calculate how much to read
                chunk_size = min(CHUNK_SIZE, remaining)
                remaining -= chunk_size
                
                # Seek to position and read chunk
                f.seek(remaining)
                chunk = f.read(chunk_size)
                
                # Decode chunk (handle potential encoding issues)
                try:
                    text = chunk.decode('utf-8')
                except UnicodeDecodeError:
                    text = chunk.decode('utf-8', errors='replace')
                
                # Split into lines
                chunk_lines = text.split('\n')
                
                # If not first chunk, merge with previous partial line
                if lines:
                    chunk_lines[-1] += lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines
                
                # If we have enough lines, stop
                if len(lines) >= max_lines:
                    break
            
            # Remove empty line at end if exists
            if lines and not lines[-1]:
                lines = lines[:-1]
            
            # Return last N lines in correct order
            return lines[-max_lines:] if len(lines) > max_lines else lines

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
        
        # Try alternate format: "2026-01-05 20:47:21 INFO [logger_name] message" (profiling.log format)
        pattern_alt = r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+\[([^\]]+)\]\s+(.*)$'
        match_alt = re.match(pattern_alt, line)
        
        if match_alt:
            timestamp, level, logger_name, message = match_alt.groups()
            return {
                'timestamp': timestamp + ',000',  # Add fake milliseconds for consistency
                'level': level,
                'logger': logger_name,
                'message': f"{logger_name} {message}",
                'line': line,
                'type': 'parsed'
            }
        
        # Fallback for unparseable lines
        return {
            'message': line,
            'line': line,
            'level': 'INFO',
            'type': 'raw'
        }

    def _sort_by_timestamp(self, parsed_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort parsed log lines by timestamp in chronological order.
        
        This is needed when reading from multiple rotation files to ensure
        entries are displayed in proper time order.
        
        Args:
            parsed_lines: List of parsed log entries with 'timestamp' field
            
        Returns:
            Sorted list of entries (oldest first)
        """
        def get_sort_key(entry: Dict[str, Any]) -> str:
            # Get timestamp, defaulting to empty string for entries without timestamp
            timestamp = entry.get('timestamp', '')
            if timestamp:
                # Normalize timestamp format for proper sorting
                # Format: "2025-09-25 00:23:32,790" -> sortable string
                return timestamp.replace(',', '.')
            return ''  # Entries without timestamp go to beginning
        
        return sorted(parsed_lines, key=get_sort_key)

    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        from agent_system.plugins.schema_router import create_schema_router
        
        # Get schema from server (already loaded with Jinja2 templates rendered)
        schema = self.server.get_schema_data() if hasattr(self.server, 'get_schema_data') else {}
        
        # Generate router from schema
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self
        )
    
    # Handler methods (called by schema router)
    
    async def list_log_files(self, request: Request):
        """List available log files with rotation file information"""
        from fastapi.responses import JSONResponse
        
        available_logs = []
        for log_file in self.log_files:
            log_path = Path(log_file)
            
            # Find all rotation files for this base log
            rotation_files = self._find_rotation_files(log_path)
            
            if rotation_files:
                # Calculate total size across all rotation files
                total_size = sum(f.stat().st_size for f in rotation_files)
                # Get newest modification time
                newest_mtime = max(f.stat().st_mtime for f in rotation_files)
                
                available_logs.append({
                    "name": log_file,
                    "size": total_size,
                    "modified": newest_mtime,
                    "exists": True,
                    "rotation_count": len(rotation_files),
                    "rotation_files": [str(f) for f in rotation_files]
                })
            else:
                available_logs.append({
                    "name": log_file,
                    "exists": False,
                    "rotation_count": 0
                })

        return JSONResponse({"logs": available_logs})

    async def get_log_content(self, request: Request, log_name: str, lines: int = 500, since_timestamp: float = None, 
                              levels: str = None, search: str = None):
        """Get log file content with optional filtering by timestamp, level, and search term
        
        Args:
            log_name: Name of the log file to read
            lines: Maximum number of lines to return (after filtering)
            since_timestamp: Only return lines after this timestamp (for streaming)
            levels: Comma-separated list of log levels to include (e.g., "error,warning")
            search: Search term to filter log messages
        """
        from fastapi.responses import JSONResponse
        
        # Use the silent logger to avoid recursive logging
        log_viewer_logger.debug(f"Fetching log content for {log_name}, lines={lines}, since_timestamp={since_timestamp}, levels={levels}, search={search}")

        # Security check - only allow configured log files
        if log_name not in self.log_files:
            return JSONResponse({"error": "Log file not allowed"})

        log_path = Path(log_name)
        
        # Find all rotation files for this base log
        rotation_files = self._find_rotation_files(log_path)
        
        if not rotation_files:
            return JSONResponse({"error": f"Log file {log_name} not found"})

        # Parse filter parameters
        level_filter = set(levels.lower().split(',')) if levels else None
        search_term = search.lower() if search else None

        def matches_filters(parsed_line: Dict[str, Any]) -> bool:
            """Check if a parsed line matches the current filters"""
            # Level filter
            if level_filter:
                line_level = parsed_line.get('level', '').lower()
                if line_level not in level_filter:
                    return False
            
            # Search filter
            if search_term:
                message = parsed_line.get('message', '').lower()
                full_content = parsed_line.get('full_content', '').lower()
                if search_term not in message and search_term not in full_content:
                    return False
            
            return True

        try:
            # Get file stats from newest file (base log)
            file_stat = log_path.stat() if log_path.exists() else rotation_files[0].stat()
            parsed_lines = []

            if since_timestamp is None:
                # Initial load - read from all rotation files
                all_lines = []
                
                # Read rotation files in order (oldest to newest)
                for rotation_file in rotation_files:
                    file_lines = await self._read_last_lines_async(rotation_file, lines * 10)  # Read more to account for distribution
                    all_lines.extend(file_lines)
                
                # Now process the combined lines
                if level_filter or search_term:
                    # Filter the combined lines
                    grouped_entries = self._group_multiline_entries(all_lines)
                    
                    for entry in grouped_entries:
                        # Filter out log viewer requests to avoid recursion
                        if '/plugins/log_viewer' in entry['main_line']:
                            continue

                        parsed_line = self._parse_log_line(entry['main_line'].rstrip())
                        
                        # Check if matches filters
                        if level_filter:
                            line_level = parsed_line.get('level', '').lower()
                            if line_level not in level_filter:
                                continue
                        
                        if search_term:
                            message = parsed_line.get('message', '').lower()
                            full_content = entry['full_content'].lower()
                            if search_term not in message and search_term not in full_content:
                                continue
                        
                        parsed_line['line_number'] = entry['line_number']
                        parsed_line['full_content'] = entry['full_content']
                        parsed_line['has_multiline'] = len(entry['continuation_lines']) > 0
                        parsed_lines.append(parsed_line)
                else:
                    # No filters - use simple processing
                    grouped_entries = self._group_multiline_entries(all_lines)

                    for entry in grouped_entries:
                        # Filter out log viewer requests to avoid recursion
                        if '/plugins/log_viewer' in entry['main_line']:
                            continue

                        parsed_line = self._parse_log_line(entry['main_line'].rstrip())
                        parsed_line['line_number'] = entry['line_number']
                        parsed_line['full_content'] = entry['full_content']
                        parsed_line['has_multiline'] = len(entry['continuation_lines']) > 0
                        parsed_lines.append(parsed_line)
                
                # Sort by timestamp to ensure chronological order
                # (important when reading from multiple rotation files)
                parsed_lines = self._sort_by_timestamp(parsed_lines)
                
                # Take only the last N entries
                parsed_lines = parsed_lines[-lines:]
            else:
                # Streaming mode - read last N*3 lines efficiently
                buffer_multiplier = 3
                max_lines_to_read = lines * buffer_multiplier
                
                # Read from end of file asynchronously
                recent_lines = await self._read_last_lines_async(log_path, max_lines_to_read)
                
                # Calculate starting line number (approximate)
                start_line_num = 1  # We don't know total lines anymore, but line_num is mainly for debugging
                
                for idx, line in enumerate(recent_lines):
                    line_num = start_line_num + idx
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
                        
                        # Apply filters
                        if matches_filters(parsed_line):
                            parsed_lines.append(parsed_line)

            log_viewer_logger.debug(f"Returning {len(parsed_lines)} lines for {log_name} (streaming: {since_timestamp is not None}, filters: levels={levels}, search={search})")
            return JSONResponse({
                "lines": parsed_lines,
                "total_lines": -1,  # Unknown when reading from end (would need full file scan)
                "file_size": file_stat.st_size,
                "last_modified": file_stat.st_mtime,
                "current_timestamp": time.time()
            })
        except Exception as e:
            log_viewer_logger.error(f"Failed to read log file {log_name}: {str(e)}")
            return JSONResponse({"error": f"Failed to read log file: {str(e)}"})

    async def download_log_file(self, request: Request, log_name: str):
        """Download entire log file"""
        from fastapi import Response
        from fastapi.responses import JSONResponse
        
        # Security check
        if log_name not in self.log_files:
            return JSONResponse({"error": "Log file not allowed"})

        log_path = Path(log_name)
        if not log_path.exists():
            return JSONResponse({"error": f"Log file {log_name} not found"})

        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()

            return Response(
                content,
                media_type="text/plain",
                headers={"Content-Disposition": f"attachment; filename={log_path.name}"}
            )
        except Exception as e:
            return JSONResponse({"error": f"Failed to read log: {str(e)}"})

    async def poll_log_updates(self, request: Request, log_name: str, last_timestamp: float = 0, lines: int = 20):
        """Poll for log updates since last timestamp (non-streaming approach)"""
        from fastapi.responses import JSONResponse
        
        log_viewer_logger.debug(f"Polling log updates for {log_name} since {last_timestamp}")

        # Security check - only allow configured log files
        if log_name not in self.log_files:
            return JSONResponse({"error": "Log file not allowed"})

        log_path = Path(log_name)
        if not log_path.exists():
            return JSONResponse({"error": f"Log file {log_name} not found"})

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

            return JSONResponse({
                "new_lines": new_lines,
                "current_timestamp": time.time(),
                "file_size": file_stat.st_size,
                "last_modified": file_stat.st_mtime,
                "has_updates": len(new_lines) > 0
            })
        except Exception as e:
            log_viewer_logger.error(f"Failed to poll log file {log_name}: {str(e)}")
            return JSONResponse({"error": f"Failed to poll log file: {str(e)}"})

    async def panel_html(self, request: Request) -> HTMLResponse:
        """Serve the log viewer panel HTML"""
        content = self.templates.TemplateResponse(
            request=request,
            name="panel.html",
            context={"plugin_name": self.name, "timestamp": int(time.time())}
        ).body.decode()
        return HTMLResponse(
            content=content,
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "frame-ancestors 'self'"
            }
        )

    async def panel(self, request: Request) -> HTMLResponse:
        """Serve the log viewer panel HTML (alternate endpoint)"""
        content = self.templates.TemplateResponse(
            request=request,
            name="panel.html",
            context={"plugin_name": self.name, "timestamp": int(time.time())}
        ).body.decode()
        return HTMLResponse(
            content=content,
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "frame-ancestors 'self'"
            }
        )

    async def serve_static(self, request: Request, file_path: str):
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

    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets"""
        # Static assets will be in the plugin directory
        static_path = Path(__file__).parent / "static"
        return static_path if static_path.exists() else None