"""Message Debugger Plugin - Web endpoints and UI.

Provides REST API endpoints and web panel for viewing captured message snapshots.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

logger = logging.getLogger(__name__)


class MessageDebuggerWebFactory:
    """Web UI factory for message debugger plugin."""
    
    def __init__(self, message_history: List[Dict[str, Any]], name: str = "message_debugger"):
        """Initialize web factory with shared message history.
        
        Args:
            message_history: Shared list of message snapshots
            name: Plugin instance name for dynamic routing
        """
        self.name = name
        self.message_history = message_history
        self.plugin_dir = Path(__file__).parent
        self.router = APIRouter(prefix=f"/plugins/{self.name}")
        
        # Register all routes
        self._register_routes()
    
    def _register_routes(self):
        """Register all API and panel routes."""
        # API routes
        self.router.add_api_route(
            "/snapshots",
            self.list_snapshots,
            methods=["GET"]
        )
        self.router.add_api_route(
            "/snapshots/{index}",
            self.get_snapshot,
            methods=["GET"]
        )
        self.router.add_api_route(
            "/snapshots",
            self.clear_snapshots,
            methods=["DELETE"]
        )
        self.router.add_api_route(
            "/stats",
            self.get_stats,
            methods=["GET"]
        )
        # Panel route
        self.router.add_api_route(
            "/panel",
            self.render_panel,
            methods=["GET"],
            response_class=HTMLResponse
        )
        # Static file routes
        self.router.add_api_route(
            "/static/panel.css",
            self.serve_css,
            methods=["GET"]
        )
        self.router.add_api_route(
            "/static/panel.js",
            self.serve_js,
            methods=["GET"]
        )
    
    async def list_snapshots(
        self,
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
    
    async def get_snapshot(self, index: int):
        """Get detailed information for a specific snapshot."""
        if index < 0 or index >= len(self.message_history):
            raise HTTPException(status_code=404, detail="Snapshot not found")
        
        return self.message_history[index]
    
    async def clear_snapshots(self):
        """Clear all captured message snapshots."""
        count = len(self.message_history)
        self.message_history.clear()
        return {
            'status': 'cleared',
            'removed_count': count
        }
    
    async def get_stats(self):
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
    
    async def render_panel(self) -> HTMLResponse:
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
    
    async def serve_css(self):
        """Serve the panel CSS file."""
        css_path = self.plugin_dir / "static" / "panel.css"
        return FileResponse(
            css_path,
            media_type="text/css"
        )
    
    async def serve_js(self):
        """Serve the panel JavaScript file."""
        js_path = self.plugin_dir / "static" / "panel.js"
        return FileResponse(
            js_path,
            media_type="application/javascript"
        )
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router for web UI."""
        return self.router
    
    def get_static_assets(self) -> Path | None:
        """Return path to static assets (none for this plugin)."""
        return None
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Return UI panel definitions for integration into main UI."""
        return [
            {
                'id': 'message-debugger',
                'title': 'Message Debugger',
                'icon': '🔍',
                'endpoint': '/plugins/message_debugger/panel',
                'category': 'debugging'
            }
        ]
