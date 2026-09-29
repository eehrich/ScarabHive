"""Web endpoints of the context summarizer: the panel and the calls it makes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import may_see_session, require_everything, sees_everything
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
                          limit: int = Query(100, ge=1, le=1000),
                          current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """The newest events of a session (of all without one: an admin's), newest first and without their messages;
        the figures count every event asked for. Another user's session answers as one without events."""
        shown = await may_see_session(request, current_user, session_id)
        events = [event for event in self.summarization_history
                  if shown and (session_id is None or event.get('session_id') == session_id)]
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

    async def get_event(self, request: Request, event_id: int,
                        current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """One event with the messages it removed and the summaries that replaced them -- a conversation's text, so
        only its session's user sees it (an event without a session: an admin). Anyone else is told what an event
        that is gone tells."""
        for event in self.summarization_history:
            if event.get('id') == event_id:
                session_id = event.get('session_id')
                if (await may_see_session(request, current_user, session_id) if session_id
                        else sees_everything(request, current_user)):
                    return event
                break
        raise HTTPException(status_code=404, detail=f"Event {event_id} is no longer in the history")

    async def clear_history(self, request: Request,
                            current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """Forget every event, of every session -- an admin's to do."""
        require_everything(request, current_user, "Clearing the events of every session")
        self.summarization_history.clear()
        return {}
