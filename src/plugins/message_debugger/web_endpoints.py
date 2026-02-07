"""Message Debugger Plugin - Web endpoints and UI.

Provides REST API endpoints and web panel for viewing captured message turns
and raw LLM API request/response logs from the SQLite database.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse

from agent_system.plugins.schema_router import create_schema_router

if TYPE_CHECKING:
    from .database import MessageDebuggerDB

logger = logging.getLogger(__name__)


class MessageDebuggerWebFactory:
    """Web UI factory for message debugger plugin.
    
    Queries the SQLite database for turns and LLM request/response logs.
    """
    
    def __init__(
        self,
        db: "MessageDebuggerDB",
        name: str = "message_debugger",
        server=None,
    ):
        """Initialize web factory with database reference.
        
        Args:
            db: SQLite database instance
            name: Plugin instance name for dynamic routing
            server: Server instance for schema access
        """
        self.name = name
        self.db = db
        self.server = server
        self.plugin_dir = Path(__file__).parent
    
    # ---- Turns endpoints ----
    
    async def list_turns(
        self,
        request: Request,
        agent_name: str | None = Query(default=None, description="Filter by agent name"),
        session_id: str | None = Query(default=None, description="Filter by session ID"),
        snapshot_type: str | None = Query(default=None, description="Filter by type (pre_llm/post_llm)"),
        limit: int = Query(default=50, ge=1, le=500, description="Maximum turns to return"),
        offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    ):
        """List captured agent-level message turns."""
        turns = self.db.get_turns(
            agent_name=agent_name,
            session_id=session_id,
            snapshot_type=snapshot_type,
            limit=limit,
            offset=offset,
        )
        total = self.db.count_turns(agent_name=agent_name, session_id=session_id)
        return {
            'total': total,
            'offset': offset,
            'limit': limit,
            'count': len(turns),
            'turns': turns,
        }
    
    async def get_turn(self, request: Request, turn_id: int):
        """Get detailed information for a specific turn."""
        turn = self.db.get_turn(turn_id)
        if not turn:
            raise HTTPException(status_code=404, detail=f"Turn {turn_id} not found")
        return turn
    
    # ---- LLM Requests endpoints ----
    
    async def list_llm_requests(
        self,
        request: Request,
        agent_name: str | None = Query(default=None, description="Filter by agent name"),
        session_id: str | None = Query(default=None, description="Filter by session ID"),
        direction: str | None = Query(default=None, description="Filter by direction (request/response)"),
        provider: str | None = Query(default=None, description="Filter by provider"),
        limit: int = Query(default=50, ge=1, le=500, description="Maximum entries to return"),
        offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    ):
        """List raw LLM API request/response logs."""
        items = self.db.get_llm_requests(
            agent_name=agent_name,
            session_id=session_id,
            direction=direction,
            provider=provider,
            limit=limit,
            offset=offset,
        )
        total = self.db.count_llm_requests(agent_name=agent_name, provider=provider)
        return {
            'total': total,
            'offset': offset,
            'limit': limit,
            'count': len(items),
            'requests': items,
        }
    
    async def get_llm_request(self, request: Request, request_id: int):
        """Get a specific LLM request log by ID."""
        item = self.db.get_llm_request(request_id)
        if not item:
            raise HTTPException(status_code=404, detail=f"LLM request {request_id} not found")
        return item
    
    # ---- Stats & maintenance ----
    
    async def get_stats(self, request: Request):
        """Get statistics about captured data."""
        return self.db.get_stats()
    
    async def clear_all(self, request: Request):
        """Clear all captured data."""
        result = self.db.clear_all()
        return {'status': 'cleared', **result}
    
    # ---- Panel rendering ----
    
    async def render_panel(self, request: Request) -> HTMLResponse:
        """Render the message debugger panel HTML."""
        template_path = self.plugin_dir / 'templates' / 'panel.html'
        
        if not template_path.exists():
            return HTMLResponse(
                content="<html><body><h1>Error</h1><p>Template not found</p></body></html>",
                status_code=500,
            )
        
        try:
            html_content = template_path.read_text(encoding='utf-8')
            return HTMLResponse(content=html_content, media_type="text/html")
        except Exception as e:
            logger.error(f"Failed to load message debugger panel: {e}")
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>{e}</p></body></html>",
                status_code=500,
            )
    
    async def serve_css(self, request: Request):
        """Serve the panel CSS file."""
        return FileResponse(
            self.plugin_dir / "static" / "panel.css",
            media_type="text/css",
        )
    
    async def serve_js(self, request: Request):
        """Serve the panel JavaScript file."""
        return FileResponse(
            self.plugin_dir / "static" / "panel.js",
            media_type="application/javascript",
        )
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router for web UI."""
        schema = self.server.get_schema_data() if self.server and hasattr(self.server, 'get_schema_data') else {}
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self,
        )

