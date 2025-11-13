"""Message Debugger Plugin - Web endpoints and UI.

Provides REST API endpoints and web panel for viewing captured message snapshots.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse

from agent_system.plugins.schema_router import create_schema_router

logger = logging.getLogger(__name__)


class MessageDebuggerWebFactory:
    """Web UI factory for message debugger plugin."""
    
    def __init__(self, message_history: List[Dict[str, Any]], name: str = "message_debugger", server=None):
        """Initialize web factory with shared message history.
        
        Args:
            message_history: Shared list of message snapshots
            name: Plugin instance name for dynamic routing
            server: Server instance for schema access
        """
        self.name = name
        self.message_history = message_history
        self.server = server
        self.plugin_dir = Path(__file__).parent
    
    # Handler methods (called by schema router)
    
    async def list_snapshots(
        self,
        request: Request,
        agent_name: str | None = Query(default=None, description="Filter by agent name"),
        session_id: str | None = Query(default=None, description="Filter by session ID"),
        limit: int = Query(default=50, ge=1, le=500, description="Maximum snapshots to return")
    ):
        """List captured message snapshots with optional filtering."""
        snapshots = self.message_history
        
        # Apply filters
        if agent_name:
            snapshots = [s for s in snapshots if s.get('agent_name') == agent_name]
        if session_id:
            snapshots = [s for s in snapshots if s.get('session_id') == session_id]
        
        # Apply limit (most recent first)
        snapshots = list(reversed(snapshots[-limit:]))
        
        return {
            'total': len(self.message_history),
            'filtered': len(snapshots),
            'snapshots': snapshots
        }
    
    async def get_snapshot(self, request: Request, index: int):
        """Get detailed information for a specific snapshot."""
        if index < 0 or index >= len(self.message_history):
            raise HTTPException(status_code=404, detail="Snapshot not found")
        
        return self.message_history[index]
    
    async def clear_snapshots(self, request: Request):
        """Clear all captured message snapshots."""
        count = len(self.message_history)
        self.message_history.clear()
        return {
            'status': 'cleared',
            'removed_count': count
        }
    
    async def get_stats(self, request: Request):
        """Get statistics about captured messages."""
        if not self.message_history:
            return {
                'total_snapshots': 0,
                'unique_agents': [],
                'unique_sessions': [],
                'total_messages': 0,
                'total_tokens': 0
            }
        
        agents = set()
        sessions = set()
        total_messages = 0
        total_tokens = 0
        
        for snapshot in self.message_history:
            if snapshot.get('agent_name'):
                agents.add(snapshot['agent_name'])
            if snapshot.get('session_id'):
                sessions.add(snapshot['session_id'])
            total_messages += snapshot.get('message_count', 0)
            total_tokens += snapshot.get('total_estimated_tokens') or 0
        
        return {
            'total_snapshots': len(self.message_history),
            'unique_agents': sorted(list(agents)),
            'unique_sessions': sorted(list(sessions)),
            'total_messages': total_messages,
            'total_tokens': total_tokens,
            'average_messages_per_snapshot': total_messages / len(self.message_history) if self.message_history else 0,
            'average_tokens_per_snapshot': total_tokens / len(self.message_history) if self.message_history else 0
        }
    
    async def render_panel(self, request: Request) -> HTMLResponse:
        """Render the message debugger panel HTML.
        
        Returns:
            HTML response with panel content
        """
        template_path = self.plugin_dir / 'templates' / 'panel.html'
        
        if not template_path.exists():
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>Template not found: {template_path}</p></body></html>",
                status_code=500,
                media_type="text/html"
            )
        
        try:
            html_content = template_path.read_text(encoding='utf-8')
            return HTMLResponse(
                content=html_content,
                media_type="text/html"
            )
        except Exception as e:
            logger.error(f"Failed to load message debugger panel template: {e}")
            return HTMLResponse(
                content=f"<html><body><h1>Error</h1><p>Failed to load panel: {e}</p></body></html>",
                status_code=500,
                media_type="text/html"
            )
    
    async def serve_css(self, request: Request):
        """Serve the panel CSS file."""
        css_path = self.plugin_dir / "static" / "panel.css"
        return FileResponse(
            css_path,
            media_type="text/css"
        )
    
    async def serve_js(self, request: Request):
        """Serve the panel JavaScript file."""
        js_path = self.plugin_dir / "static" / "panel.js"
        return FileResponse(
            js_path,
            media_type="application/javascript"
        )
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router for web UI."""
        # Get schema from server if available
        schema = self.server.get_schema_data() if self.server and hasattr(self.server, 'get_schema_data') else {}
        
        # Generate router from schema
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self
        )

