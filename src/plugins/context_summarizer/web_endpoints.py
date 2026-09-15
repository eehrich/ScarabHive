"""Web endpoints of the context summarizer: the panel and the calls it makes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

# the messages an event removed and the summaries that replaced them: only the event's own answer carries them
MESSAGES = ('before_messages', 'after_messages')


class ContextSummarizerWebFactory:
    """The panel over the summarization events the hook records in memory."""

    def __init__(self, server, summarization_history: list[dict[str, Any]]):
        self.server = server
        self.summarization_history = summarization_history
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def render_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def get_history(self, request: Request, session_id: str | None = None,
                          limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        """The newest events of a session (of all without one), newest first and without their messages; the figures
        count every event asked for."""
        events = [event for event in self.summarization_history if session_id is None or event.get('session_id') == session_id]
        applied = [event for event in events if event.get('status') == 'success']
        return {
            'events': [{key: value for key, value in event.items() if key not in MESSAGES}
                       for event in reversed(events[-limit:])],
            'stats': {
                'events': len(events),
                'applied': len(applied),
                'rejected': sum(event.get('status') == 'rejected' for event in events),
                'skipped': sum(event.get('status') == 'skipped' for event in events),
                'tokens_saved': sum(event.get('tokens_saved', 0) for event in events),  # only an applied run saves any
                'messages_summarized': sum(event.get('messages_summarized', 0) for event in applied),
                'average_reduction': sum(event.get('reduction_ratio', 0) for event in applied) / len(applied) if applied else None,
            },
        }

    async def get_event(self, request: Request, event_id: int) -> dict[str, Any]:
        """One event with the messages it removed and the summaries that replaced them."""
        for event in self.summarization_history:
            if event.get('id') == event_id:
                return event
        raise HTTPException(status_code=404, detail=f"Event {event_id} is no longer in the history")

    async def clear_history(self, request: Request) -> dict[str, Any]:
        """Forget every event, of every session."""
        self.summarization_history.clear()
        return {}
