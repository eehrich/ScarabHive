"""Setup: the tools of the setup agent and the Setup panel -- what an installation still lacks.

It reports the keys, the admin's password and the signing key, and tries the
chat; the panel changes the admin's password. Keys are entered in
config/secrets.env by hand until the panel can write them
(docs/einrichtung_konzept.md).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_system.api.debug_endpoints import require_admin_viewer
from agent_system.plugins.schema_router import create_schema_router
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.ui.resources import ui_templates

from .probe import default_chat_profile, probe_chat, runs_as_a_batch
from .status import auth_status, keys_status, user_database

logger = logging.getLogger(__name__)
templates = ui_templates(Path(__file__).parent / "templates")


def installation_state(config: Any, auth_config: Any = None, signing_key: Optional[str] = None) -> dict[str, Any]:
    """Keys (never a value), the default chat, the admin's password and the signing key.

    *config* is the one the process started with -- the chat's entry agent and its
    client stay until a restart, whatever a reload says, and so do the checking of
    logins and the key they are signed with (*signing_key*). *auth_config* is the one
    a reload set since: its signing key is what a restart applies.
    """
    agent, profile = default_chat_profile(config)
    return {"keys": keys_status(getattr(config, "source_path", None)), "chat": {"agent": agent, "profile": profile},
            "auth": auth_status(config, signing_key, auth_config)}


#: agent-cli's identity when no --session-user is given: a name no account may take (reserved
#: since 22.09.2026 -- an account made before is judged as the account it is).
CLI_USER = "cli_user"


def caller_is_admin(params: dict[str, Any], config: Any) -> Optional[bool]:
    """An active admin, or the owner: without authentication there is one user, and agent-cli
    at the machine runs as cli_user. The status names the admin and says whether a known
    password still opens it, and a probe costs a request: neither is for whoever chats with
    an agent that happens to be allowed these tools. None where the user database cannot be
    read: nobody is let in, and nobody is told "administrators only" for it.

    Not "no user database in this process": that is every CLI process, the one the API
    starts to wake a web user's session among them.
    """
    from agent_system.auth.models import UserRole
    if not config.auth.enabled:
        return True
    user_id = str(params.get("_user_id") or "")
    users = user_database(config.auth)
    try:
        account = users.get_user_by_username(user_id) if users is not None else None
    except sqlite3.Error as error:
        # Locked, or no users table: whose name this is cannot be told -- an old account named
        # cli_user among them. Nobody is let in; a bug (no sqlite3.Error) stays loud.
        logger.warning("setup: the user database could not be read (%s); nobody counts as an admin", error)
        return None
    if account is None:
        return user_id == CLI_USER
    return bool(account.is_active and account.role == UserRole.ADMIN)


async def refusal(params: dict[str, Any], config: Any, status: Any, tool: str, action: str) -> Optional[dict[str, Any]]:
    """None for an admin; else the error result -- off the loop, as a locked user database
    answers after its busy timeout."""
    allowed = await asyncio.to_thread(caller_is_admin, params, config)
    if allowed:
        return None
    if allowed is None:
        await status.error(f"{tool}: the user database could not be read")
        return {"status": "error", "error": "the user database could not be read (locked, or not a user database): "
                                            "try again in a moment; if it persists, the file needs checking"}
    await status.error(f"{tool}: administrators only")
    return {"status": "error", "error": f"only an administrator may {action}"}


class SetupServer(SchemaBasedToolServer):
    """Tools for the setup agent, and the Setup panel."""

    # ------------------------------------------------------------------ tools

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params["_status"]
        refused = await refusal(params, self.system_config, status, "setup status", "read the setup status")
        if refused:
            return refused
        state = await asyncio.to_thread(installation_state, self.system_config)
        if state["auth"]["shared_signing_key"] is False:
            # This process need not be the API: an own key configured says nothing of the one it signs with.
            state["auth"]["shared_signing_key"] = None
        unset = [key["name"] for key in state["keys"] if key["state"] != "set"]
        await status.end(f"{len(state['keys'])} keys, {len(unset)} not set"
                         + (f": {', '.join(unset)}"[:100] if unset else ""))
        return {"status": "success", **state}

    async def probe_chat(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params["_status"]
        config = self.system_config
        refused = await refusal(params, config, status, "probe_chat", "probe the chat")
        if refused:
            return refused
        profile = params.get("profile")
        if profile is not None and not isinstance(profile, str):
            await status.error("probe_chat: profile must be a string")
            return {"status": "error", "error": "profile must be the name of an LLM profile"}
        profiles = sorted(config.llm_system.profiles) if config.llm_system else []
        if profile and profile not in profiles:
            await status.error(f"probe_chat: no profile {profile!r}"[:140])
            profiles = [name for name in profiles if not runs_as_a_batch(config, name)]
            named = ", ".join(profiles[:30]) + (f" and {len(profiles) - 30} more" if len(profiles) > 30 else "")
            return {"status": "error", "error": f"no LLM profile named {profile!r}: omit it to probe the default "
                                                f"chat, or name one of: {named}"}
        result = await probe_chat(config, profile)
        if result["ok"]:
            await status.end(f"chat answers: {result['profile']} ({result.get('model')})"[:140])
        else:
            await status.end(f"chat does not answer: {result['error']}"[:140])
        return {"status": "success", **result}

    # ------------------------------------------------------------------ panel

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.name, schema=self.get_schema_data(), handler_class=self)

    def get_static_assets(self) -> Path:
        """The panel's script, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"

    async def render_panel(self, request: Request):
        """The page carries no data; what it shows comes from the endpoints below, admin only
        (require_admin_viewer: without authentication the one user is the owner)."""
        return templates.TemplateResponse(request, "panel.html", {"plugin": self.name})

    async def get_state(self, request: Request, _admin: None = Depends(require_admin_viewer)) -> dict[str, Any]:
        from agent_system.auth import security
        # Whether the API signs is settled at start (set_jwt_config, the middleware), whatever a
        # reload says since; a reload replaces app.state.config, whose key a restart applies.
        signing_key = security.SECRET_KEY if self.system_config.auth.enabled else None
        return await asyncio.to_thread(installation_state, self.system_config, request.app.state.config, signing_key)

    async def post_probe(self, request: Request, _admin: None = Depends(require_admin_viewer)) -> dict[str, Any]:
        # A probe costs a request: JSON only, so a page elsewhere cannot send one on the admin's cookie
        # (a cross-site form post is a "simple" request, which a JSON Content-Type is not).
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Send the request as JSON")
        # What the chat started with, as the tool does: a reload moves neither its agent nor its client.
        return await probe_chat(self.system_config)
