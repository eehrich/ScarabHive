"""Web endpoints of the context engineer: the panel and the calls it makes. Read-only: nothing here opens a session's
stores the way the hook does, which would create their files and register the session."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional
from urllib.request import pathname2url

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import may_see_session
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from .core_memory import CoreMemory, Fact

# the session ids the session manager hands out: the id names the session's directory, so nothing else gets in
SESSION_PATTERN = r"^[A-Za-z0-9_-]+$"
READ_ATTEMPTS = 20
READ_PAUSE_SECONDS = 0.01


def _table_totals(database: Path, table: str, session_id: str | None = None) -> dict[str, int]:
    """Rows and tokens of a session's store, opened read-only: every row, or those tagged with the session."""
    if not database.exists():
        return {"count": 0, "tokens": 0}
    # pathname2url, not as_uri: a UNC path must become file:////server/share, which SQLite accepts
    with closing(sqlite3.connect(f"file:{pathname2url(str(database.absolute()))}?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            return {"count": 0, "tokens": 0}  # a store being created: its file is there before its table
        count, tokens = connection.execute(
            f"SELECT COUNT(*), COALESCE(SUM(token_count), 0) FROM {table}" + (" WHERE session_id = ?" if session_id else ""),
            (session_id,) if session_id else ()).fetchone()
    return {"count": count, "tokens": tokens}


def _read_saved(path: Path) -> str:
    """A file the hook saves by replacing it: Windows refuses to open it while the replace is under way."""
    for attempt in range(READ_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == READ_ATTEMPTS - 1:
                raise
            time.sleep(READ_PAUSE_SECONDS)


def _core_memory(path: Path | None, max_tokens: int) -> dict[str, Any]:
    """The facts as the file holds them, counted as the core memory counts them, without loading it (a load trims and
    saves a file over its budget)."""
    memory = CoreMemory(max_tokens=max_tokens)
    if path is not None and path.exists():
        memory.facts = [Fact.from_dict(fact) for fact in json.loads(_read_saved(path)).get("facts", [])]
        memory._recalculate_tokens()
    order = {category: index for index, category in enumerate(CoreMemory.CATEGORIES)}
    facts = sorted(memory.facts, key=lambda fact: (order.get(fact.category, len(order)), -fact.importance))
    return {
        "facts": [{"content": fact.content, "category": fact.category, "importance": fact.importance} for fact in facts],
        "tokens": memory.get_token_usage(),
        "max_tokens": max_tokens,
    }


class ContextEngineerWebFactory:
    """The panel over the compactions the hook records and the stores it keeps per session."""

    def __init__(self, server, stats_history: list[dict[str, Any]]):
        self.server = server
        self.stats_history = stats_history
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def get_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def get_history(self, request: Request,
                          session_id: str | None = Query(None, pattern=SESSION_PATTERN),
                          limit: int = Query(100, ge=1, le=1000),
                          current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """The newest compactions of a session (of all without one: an admin's), newest first; the figures count every
        one asked for. Another user's session answers as one without compactions."""
        shown = await may_see_session(request, current_user, session_id)
        events = [event for event in self.stats_history
                  if shown and (session_id is None or event.get("session_id") == session_id)]
        return {
            "events": list(reversed(events[-limit:])),
            "stats": {
                "events": len(events),
                "tokens_saved": sum(event.get("tokens_saved", 0) for event in events),
                "average_reduction": sum(event.get("reduction_percent", 0) for event in events) / len(events) if events else None,
                "media_always_compacted": sum(event.get("media_always_compacted", 0) for event in events),
                "media_deduplicated": sum(event.get("media_deduplicated", 0) for event in events),
                "media_compacted_after_event": sum(event.get("media_compacted_after_event", 0) for event in events),
            },
        }

    async def get_session(self, request: Request, session_id: str = Query(..., pattern=SESSION_PATTERN),
                          current_user: Optional[User] = Depends(get_optional_user)) -> dict[str, Any]:
        """What a session's stores hold on disk: tool results, archived messages, core memory facts. A session without
        a directory holds nothing, and so does another user's; a store that cannot be read is answered 503 with the
        reason."""
        hooks = self.server._hooks_impl
        directory = hooks._storage_base / session_id
        if not await may_see_session(request, current_user, session_id):
            nothing = {"count": 0, "tokens": 0}
            return {"tool_results": nothing, "archived": dict(nothing),
                    "core_memory": _core_memory(None, hooks.core_memory_max_tokens)}

        def read() -> dict[str, Any]:
            return {
                # the archive's rows as list reaches them: an untagged row only once the store has been opened anew
                "tool_results": _table_totals(directory / "tool_results.db", "tool_results"),
                "archived": _table_totals(directory / "archive.db", "archived_messages", session_id),
                "core_memory": _core_memory(directory / "core_memory.json", hooks.core_memory_max_tokens),
            }

        try:
            return await asyncio.to_thread(read)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise HTTPException(status_code=503, detail=f"The stores of session {session_id} cannot be read: {error}") from error
