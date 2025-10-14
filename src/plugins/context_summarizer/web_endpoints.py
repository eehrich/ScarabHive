"""Web UI endpoints for Context Summarizer plugin.

Provides REST API endpoints for viewing summarization history and statistics.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


class ContextSummarizerWebFactory:
    """Web UI factory for Context Summarizer plugin.
    
    Provides panel for viewing LLM-based summarization history with
    before/after message comparison and statistics.
    """
    
    def __init__(self, summarization_history: List[Dict[str, Any]]):
        """Initialize web factory.
        
        Args:
            summarization_history: Shared list for tracking summarization events
        """
        self.summarization_history = summarization_history
        self.router = APIRouter()
        
        # Register routes
        self.router.add_api_route(
            "/api/summarization/history",
            self.get_history,
            methods=["GET"],
            response_model=None
        )
        self.router.add_api_route(
            "/api/summarization/stats",
            self.get_stats,
            methods=["GET"],
            response_model=None
        )
    
    async def get_history(self, limit: int = 100) -> Dict[str, Any]:
        """Get recent summarization events.
        
        Args:
            limit: Maximum number of events to return
            
        Returns:
            Dict with summarization history
        """
        try:
            # Return most recent events first
            recent_events = list(reversed(self.summarization_history[-limit:]))
            
            return {
                'success': True,
                'events': recent_events,
                'total_events': len(self.summarization_history)
            }
        except Exception as e:
            logger.exception("Error getting summarization history")
            return {
                'success': False,
                'error': str(e),
                'events': []
            }
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get summarization statistics.
        
        Returns:
            Dict with aggregate statistics
        """
        try:
            if not self.summarization_history:
                return {
                    'success': True,
                    'total_events': 0,
                    'total_tokens_saved': 0,
                    'total_messages_summarized': 0,
                    'average_reduction_ratio': 0.0
                }
            
            total_tokens_saved = sum(event.get('tokens_saved', 0) for event in self.summarization_history)
            total_messages_summarized = sum(event.get('messages_summarized', 0) for event in self.summarization_history)
            avg_reduction = sum(event.get('reduction_ratio', 0) for event in self.summarization_history) / len(self.summarization_history)
            
            return {
                'success': True,
                'total_events': len(self.summarization_history),
                'total_tokens_saved': total_tokens_saved,
                'total_messages_summarized': total_messages_summarized,
                'average_reduction_ratio': avg_reduction
            }
        except Exception as e:
            logger.exception("Error calculating summarization stats")
            return {
                'success': False,
                'error': str(e)
            }
    
    def create_panel(self, templates: Jinja2Templates) -> tuple[str, callable]:
        """Create web UI panel for plugin manager.
        
        Args:
            templates: Jinja2 template renderer
            
        Returns:
            Tuple of (panel_id, render_function)
        """
        async def render_panel(request: Request) -> HTMLResponse:
            """Render the summarization history panel."""
            return templates.TemplateResponse(
                "context_summarizer/panel.html",
                {
                    "request": request,
                    "plugin_name": "context_summarizer",
                    "panel_title": "Context Summarization History"
                }
            )
        
        return ("context_summarizer", render_panel)
