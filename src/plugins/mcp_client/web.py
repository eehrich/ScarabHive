"""The OAuth callback: ``GET /plugins/<instance>/oauth/callback`` -- where an authorization server sends the
browser back with the code (or the refusal) for an authorization the plugin's authorize tool started.

The ``state`` decides which authorization it belongs to; one that no authorization waits for is refused,
so a link made up elsewhere completes nothing.
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from agent_system.api.question_routes import question_router

if TYPE_CHECKING:
    from .server import MCPClientServer


def _page(title: str, text: str, status: int) -> HTMLResponse:
    body = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
            f"<body style='font-family:sans-serif;margin:3em'><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(text)}</p></body></html>")
    return HTMLResponse(body, status_code=status)


def build_router(server: "MCPClientServer") -> APIRouter:
    router = APIRouter()
    # /answer and /pending for the servers' questions to the person (elicitation.py)
    router.include_router(question_router(f"/plugins/{server.name}", server.questions, server.answer_url))

    @router.get(server.callback_path)
    async def oauth_callback(state: str = "", code: Optional[str] = None, error: Optional[str] = None,
                             error_description: Optional[str] = None, iss: Optional[str] = None):
        if error:
            server.oauth.fail(state, f"{error}: {error_description}" if error_description else error)
            return _page("Authorization refused", f"The server answered: {error}. Nothing was stored.", 400)
        if not state or not code or not server.oauth.complete(state, code, iss):
            return _page("No authorization waiting",
                         "This link belongs to no authorization that waits (finished, timed out "
                         "or answered already). Start it again with /mcp-authorize.", 400)
        return _page("Authorized", "Access granted. You can close this tab; the MCP server connects now.", 200)

    return router
