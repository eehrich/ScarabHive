"""Message Debugger Plugin - Web endpoints and UI.

Provides REST API endpoints and web panel for viewing captured message turns
and raw LLM API request/response logs from the SQLite database.

A user reads the captures of their own calls, an admin everyone's (and may
name one user). Each row carries the user whose call it was; the database
refuses a read that does not say whose rows it wants (database.py), so the
rule sits in one place: _owner. A row nobody owns -- a call no run named a
user for, everything captured before rows had owners -- is an admin's.
Clearing and pruning act on everyone's rows and stay an admin's.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Depends, Query, HTTPException, Request

from agent_system.auth.database import UserDatabase
from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import require_everything, sees_everything, viewer
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from .database import EVERYONE, Account

if TYPE_CHECKING:
    from .database import MessageDebuggerDB

logger = logging.getLogger(__name__)

LARGEST_INTEGER = 2**63 - 1  # SQLite's: a larger number cannot even be asked for


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
        self.templates = ui_templates(Path(__file__).parent / "templates")

    @staticmethod
    def _owner(request: Request, current_user: Optional[User], user_id: Optional[str] = None):
        """Whose rows a request reads: an admin everyone's, or the one user it names;
        anyone else their own -- captured since their account was made, as a new
        account under a deleted user's name must not read the old one's. Naming
        another user is an admin's; so are the rows of the identities without an
        account ("anonymous", "cli_user") -- runs whose owner nobody knew were
        recorded under them."""
        if sees_everything(request, current_user):
            return user_id or EVERYONE
        own = viewer(current_user)
        if own in UserDatabase.RESERVED_USERNAMES:
            raise HTTPException(status_code=403, detail="Sign in to see the captures of your own runs.")
        if user_id and user_id != own:
            raise HTTPException(status_code=403, detail="Another user's captures are for admins.")
        return Account(own, current_user.created_at.timestamp() * 1000)

    # ---- Turns endpoints ----
    
    async def list_turns(
        self,
        request: Request,
        agent_name: str | None = Query(default=None, description="Filter by agent name"),
        session_id: str | None = Query(default=None, description="Filter by session ID"),
        request_id: str | None = Query(default=None, description="Filter by request ID"),
        snapshot_type: str | None = Query(default=None, description="Filter by type (pre_llm/post_llm)"),
        user_id: str | None = Query(default=None, description="Admins: only this user's entries"),
        max_id: int | None = Query(default=None, ge=0, le=LARGEST_INTEGER, description=(
            "Only entries up to this id: the list as it stood at an earlier answer's as_of_id")),
        limit: int = Query(default=50, ge=1, le=500, description="Maximum turns to return"),
        offset: int = Query(default=0, ge=0, le=LARGEST_INTEGER, description="Offset for pagination"),
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        """List captured agent-level message turns; ``total`` counts every turn the filters match.

        The answer holds still at ``as_of_id``: the newest turn when it was asked, or ``max_id``. A viewer who
        passes it back sees the same list -- a page more, other filters -- and nothing captured since.
        """
        owner = self._owner(request, current_user, user_id)
        # first: a turn written meanwhile waits
        as_of = self.db.newest_id("turns", owner=owner) if max_id is None else max_id
        filters = dict(owner=owner, agent_name=agent_name, session_id=session_id, request_id=request_id,
                       snapshot_type=snapshot_type, max_id=as_of)
        turns = self.db.get_turns(**filters, limit=limit, offset=offset)
        total = self.db.count_turns(**filters)
        return {
            'as_of_id': as_of,
            'total': total,
            'offset': offset,
            'limit': limit,
            'count': len(turns),
            'turns': turns,
        }
    
    async def get_turn(self, request: Request, turn_id: int, current_user: Optional[User] = Depends(get_optional_user)):
        """Get detailed information for a specific turn."""
        owner = self._owner(request, current_user)
        # Another user's turn is answered like one that does not exist: a 403 would confirm it does.
        turn = self.db.get_turn(turn_id, owner=owner) if abs(turn_id) <= LARGEST_INTEGER else None
        if not turn:
            raise HTTPException(status_code=404, detail=f"Turn {turn_id} not found")
        return turn
    
    # ---- LLM Requests endpoints ----
    
    async def list_llm_requests(
        self,
        request: Request,
        agent_name: str | None = Query(default=None, description="Filter by agent name"),
        session_id: str | None = Query(default=None, description="Filter by session ID"),
        request_id: str | None = Query(default=None, description="Filter by request ID"),
        direction: str | None = Query(default=None, description="Filter by direction (request/response)"),
        provider: str | None = Query(default=None, description="Filter by provider"),
        user_id: str | None = Query(default=None, description="Admins: only this user's entries"),
        max_id: int | None = Query(default=None, ge=0, le=LARGEST_INTEGER, description=(
            "Only entries up to this id: the list as it stood at an earlier answer's as_of_id")),
        limit: int = Query(default=50, ge=1, le=500, description="Maximum entries to return"),
        offset: int = Query(default=0, ge=0, le=LARGEST_INTEGER, description="Offset for pagination"),
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        """List raw LLM API request/response logs; ``total`` counts every entry the filters match.

        Holds still at ``as_of_id`` like the turns list.
        """
        owner = self._owner(request, current_user, user_id)
        as_of = self.db.newest_id("llm_requests", owner=owner) if max_id is None else max_id
        filters = dict(owner=owner, agent_name=agent_name, session_id=session_id, request_id=request_id,
                       direction=direction, provider=provider, max_id=as_of)
        items = self.db.get_llm_requests(**filters, limit=limit, offset=offset)
        total = self.db.count_llm_requests(**filters)
        return {
            'as_of_id': as_of,
            'total': total,
            'offset': offset,
            'limit': limit,
            'count': len(items),
            'requests': items,
        }

    async def get_llm_request(self, request: Request, entry_id: int, current_user: Optional[User] = Depends(get_optional_user)):
        """Get one LLM request log entry by its row ID."""
        owner = self._owner(request, current_user)
        item = self.db.get_llm_request(entry_id, owner=owner) if abs(entry_id) <= LARGEST_INTEGER else None
        if not item:
            raise HTTPException(status_code=404, detail=f"LLM request log entry {entry_id} not found")
        return item
    
    # ---- Stats & maintenance ----
    
    async def get_stats(
        self,
        request: Request,
        user_id: str | None = Query(default=None, description="Admins: only this user's entries"),
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        """Statistics about the captures the viewer may read; ``sees_everything`` tells the panel whether the
        viewer is an admin (the user filter, clearing, pruning)."""
        owner = self._owner(request, current_user, user_id)
        return {**self.db.get_stats(owner=owner), "sees_everything": sees_everything(request, current_user)}
    
    async def clear_all(self, request: Request, current_user: Optional[User] = Depends(get_optional_user)):
        """Clear all captured data."""
        require_everything(request, current_user, "What the message debugger captured")
        result = self.db.clear_all()
        return {'status': 'cleared', **result}
    
    async def prune(
        self,
        request: Request,
        vacuum: bool = Query(default=True, description="Reclaim disk space after pruning"),
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        """Prune old entries down to the size cap and (by default) VACUUM.

        Cost-preserving: like the auto retention this STRIPS the oldest payloads
        (keeping the tiny cost columns) and drops the oldest turn snapshots — it
        does NOT throw away cost history. force=True drives it down to the LOW
        watermark even when between watermarks, then VACUUM shrinks the file on
        disk whenever it has free pages (the auto path never VACUUMs). VACUUM needs free temp space roughly
        the size of the remaining data, so it can fail on a full disk; that is
        reported instead of erroring the request.

        Runs OFF the event loop (it can take many seconds) and under the
        retention lock so it never collides with the per-write auto retention.
        """
        require_everything(request, current_user, "What the message debugger captured")
        result = await asyncio.to_thread(self._run_manual_prune, vacuum)
        return {'status': 'pruned', **result}

    def _run_manual_prune(self, vacuum: bool) -> dict:
        """Synchronous manual prune+VACUUM — invoked via asyncio.to_thread."""
        def _disk_bytes() -> int:
            total = 0
            for suffix in ("", "-wal", "-shm"):
                p = f"{self.db.db_path}{suffix}"
                if os.path.exists(p):
                    total += os.path.getsize(p)
            return total

        # Serialise with the auto retention (same DB, same write lock).
        with self.db._retention_lock:
            size_before = _disk_bytes()
            result = self.db.enforce_retention(force=True, budget=120.0)
            did_work = result["stripped"] or result["turns_deleted"] or result["requests_deleted"]

            result["vacuumed"] = False
            # also when retention had nothing to do: pages freed before -- a clear, the automatic retention --
            # are given back only by a VACUUM
            if vacuum and (did_work or self.db.free_pages()):
                try:
                    self.db.vacuum()
                    result["vacuumed"] = True
                except Exception as e:  # e.g. not enough free space for the temp copy
                    logger.warning("message_debugger manual VACUUM failed: %s", e)
                    result["vacuum_error"] = str(e)

            size_after = _disk_bytes()
            result["freed_mb"] = round((size_before - size_after) / 1024 / 1024, 1)
            result["size_after_mb"] = round(size_after / 1024 / 1024, 1)
            return result
    
    # ---- Panel rendering ----

    async def render_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.name})

    def get_web_router(self) -> APIRouter:
        """Return FastAPI router for web UI."""
        schema = self.server.get_schema_data() if self.server and hasattr(self.server, 'get_schema_data') else {}
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self,
        )

