"""Log Viewer web endpoints: the Logs panel and the two calls it makes."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Iterator, List, Optional, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from agent_system.plugins.web_adapter import PluginWebInterface
from agent_system.ui.resources import ui_templates

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    from .mcp_server import LogViewerMCPServer

MAX_ENTRIES = 5000
CHUNK_BYTES = 65536

ENTRY_START = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
# "TS LEVEL logger msg", "TS LEVEL [logger] msg", "TS - logger - LEVEL - msg", "TS | LEVEL | msg"
ENTRY = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d{3})?)"
    r"(?:(?:\s+-\s+(?P<logger>\S+)\s+-|\s+\|)?\s+(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b(?:\s+[-|](?=\s))?)?"
    r"\s*(?P<message>.*)",
    re.DOTALL,
)
LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warning", "WARN": "warning",
          "ERROR": "error", "CRITICAL": "critical", "FATAL": "critical"}


def lines_backwards(path: Path) -> Iterator[str]:
    """The lines of a file from the last to the first, read in chunks from the end."""
    with open(path, "rb") as file:
        position = file.seek(0, 2)
        rest = b""
        while position > 0:
            size = min(CHUNK_BYTES, position)
            position -= size
            file.seek(position)
            parts = (file.read(size) + rest).split(b"\n")
            rest = parts.pop(0)  # may be cut: completed by the next chunk
            for part in reversed(parts):
                yield part.decode("utf-8", errors="replace").rstrip("\r")
        yield rest.decode("utf-8", errors="replace").rstrip("\r")


def parse_entry(text: str) -> dict:
    """An entry: its first line starts with a timestamp, the lines after it (a traceback) belong to it."""
    match = ENTRY.match(text)
    if not match:  # lines at the top of a file that no timestamp line leads
        return {"timestamp": None, "level": None, "message": text}
    message = match["message"]
    if match["logger"]:
        message = f"{match['logger']} {message}"
    return {"timestamp": match["timestamp"], "level": LEVELS.get(match["level"]), "message": message}


class LogViewerWebEndpoints(PluginWebInterface):
    """Web endpoints component for log viewer plugin"""

    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig", server: "LogViewerMCPServer"):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.server = server
        self.plugin_dir = Path(__file__).parent
        self.templates = ui_templates(self.plugin_dir / "templates")

    @property
    def log_files(self) -> List[str]:
        """The MCP server's allowlist: the panel opens exactly what the tools may."""
        return self.server.log_files

    def _find_rotation_files(self, base_log_path: Path) -> List[Path]:
        """The file and its rotations (.1, .2, ... up to the first gap), oldest first: the base file comes last."""
        rotation_files = [base_log_path] if base_log_path.exists() else []
        for index in range(1, 101):
            rotation_path = Path(f"{base_log_path}.{index}")
            if not rotation_path.exists():
                break
            rotation_files.insert(0, rotation_path)
        return rotation_files

    def _read_entries(self, files: List[Path], count: int, levels: Optional[set], search: str) -> List[dict]:
        """The last `count` entries matching levels and search, oldest first, across the files given newest first."""
        own_requests = f"/plugins/{self.name}/"  # the panel's polling in an access log: routine, unlike a warning or error
        found: List[dict] = []

        def take(entry_lines: List[str]) -> bool:
            text = "\n".join(entry_lines)
            if search and search not in text.lower():
                return False
            entry = parse_entry(text)
            if own_requests in entry_lines[0] and entry["level"] in (None, "debug", "info"):
                return False
            if levels is not None and entry["level"] not in levels:
                return False
            found.append(entry)
            return len(found) == count

        for path in files:
            continuation: List[str] = []  # newest first
            for line in lines_backwards(path):
                if not line.strip():
                    continue
                if not ENTRY_START.match(line):
                    continuation.append(line)
                    continue
                if take([line, *reversed(continuation)]):
                    return found[::-1]
                continuation = []
            if continuation and take(continuation[::-1]):
                return found[::-1]
        return found[::-1]

    def get_web_router(self) -> APIRouter:
        from agent_system.plugins.schema_router import create_schema_router

        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return self.plugin_dir / "static"

    async def get_panel(self, request: Request):
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.name})

    async def list_log_files(self, request: Request):
        """Every configured log file; one that exists with the size and newest change across its rotations."""
        available_logs = []
        for log_file in self.log_files:
            rotation_files = self._find_rotation_files(Path(log_file))
            if rotation_files:
                stats = [f.stat() for f in rotation_files]
                available_logs.append({
                    "name": log_file,
                    "size": sum(s.st_size for s in stats),
                    "modified": max(s.st_mtime for s in stats),
                    "exists": True,
                    "rotation_count": len(rotation_files),
                    "rotation_files": [str(f) for f in rotation_files],
                })
            else:
                available_logs.append({"name": log_file, "exists": False, "rotation_count": 0})
        return JSONResponse({"logs": available_logs})

    async def get_log_content(self, request: Request, log_name: str, lines: int = Query(500, ge=1, le=MAX_ENTRIES),
                              levels: str = "", search: str = ""):
        """The last entries of a configured log file and its rotations, filtered by level and search term."""
        # only configured names: whatever else a path names is never opened
        if log_name not in self.log_files:
            raise HTTPException(status_code=404, detail=f"{log_name} is not a configured log file")
        level_filter = {level for level in levels.lower().split(",") if level} or None
        if level_filter and not level_filter <= set(LEVELS.values()):
            raise HTTPException(status_code=422, detail=f"Unknown levels: {', '.join(sorted(level_filter - set(LEVELS.values())))}")
        rotation_files = self._find_rotation_files(Path(log_name))
        if not rotation_files:
            raise HTTPException(status_code=404, detail=f"{log_name} does not exist")
        try:
            entries = await asyncio.to_thread(self._read_entries, rotation_files[::-1], lines, level_filter, search.lower())
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"{log_name} could not be read: {error}")
        return {"entries": entries}
