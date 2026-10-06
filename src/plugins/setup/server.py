"""Setup: the tools of the setup agent and the Setup panel -- what an installation still lacks.

It reports the keys, the admin's password and the signing key, and tries the
chat. The panel changes the admin's password, writes a key into
config/local.env and gives the installation its own signing key
(agent_system.config.local_layer). A tool never
writes a key: it would pass through the chat.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_system.api.admin_endpoints import ConfigReloadFailed, ConfigReloadUnavailable, reload_app_config
from agent_system.api.debug_endpoints import require_admin_viewer
from agent_system.config.local_layer import WRITING, ensure_signing_key, write_secret
from agent_system.config.settings import LOCAL_SECRETS, environment_takes, set_by_the_environment, take_secret
from agent_system.plugins.schema_router import create_schema_router
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.ui.resources import ui_templates

from .probe import default_chat_profile, probe_chat, runs_as_a_batch
from .status import SHIPPED_SIGNING_KEYS, api_keys, auth_status, keys_status, user_database

logger = logging.getLogger(__name__)
templates = ui_templates(Path(__file__).parent / "templates")


def installation_state(config: Any, signing_key: Optional[str] = None) -> dict[str, Any]:
    """Keys (never a value), the default chat, the admin's password and the signing key.

    *config* is the one the process started with -- the chat's entry agent and its
    client stay until a restart, whatever a reload says, and so do the checking of
    logins and the key they are signed with (*signing_key*). The key a restart
    applies is read from the config file.
    """
    agent, profile = default_chat_profile(config)
    return {"keys": keys_status(getattr(config, "source_path", None)), "chat": {"agent": agent, "profile": profile},
            "auth": auth_status(config, signing_key)}


def running_signing_key() -> Optional[str]:
    """The key this process signs logins with -- only the API with authentication does (set_jwt_config, at start,
    whatever a reload says since). None anywhere else: agent-cli, a woken run, an API without authentication."""
    from agent_system.auth import security
    return security.SECRET_KEY if security.AUTH_ENFORCED else None


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

    #: The config the API runs with since a key was saved here (reload_app_config); None: the one it started with.
    #: The tool has no request to ask the app for it.
    _running_config: Any = None

    # ------------------------------------------------------------------ tools

    async def status(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params["_status"]
        refused = await refusal(params, self.system_config, status, "setup status", "read the setup status")
        if refused:
            return refused
        signing_key = running_signing_key()
        state = await asyncio.to_thread(installation_state, self.system_config, signing_key)
        if signing_key is None and state["auth"]["shared_signing_key"] is False:
            # Not the API: an own key configured says nothing of the one it signs with.
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
        # the config a key saved in the panel reloaded, as the chat's next message builds its client from it
        result = await probe_chat(config, profile, llm_config=self._running_config)
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
        return await asyncio.to_thread(installation_state, self.system_config, running_signing_key())

    async def post_probe(self, request: Request, _admin: None = Depends(require_admin_viewer)) -> dict[str, Any]:
        require_json(request)
        # As the chat's next message: the agent it started with (a reload does not move it), its client built from
        # the config as it runs now (app.py _live_config) -- a key saved here since is the one tried.
        return await probe_chat(self.system_config, llm_config=getattr(request.app.state, "config", None))

    def _config_path(self) -> Path:
        path = getattr(self.system_config, "source_path", None)
        if path is None:
            raise HTTPException(status_code=409, detail="This configuration came from no file: there is no "
                                                        "config/local.env beside it to write to")
        return Path(path)

    async def post_key(self, request: Request, _admin: None = Depends(require_admin_viewer)) -> dict[str, Any]:
        """Write one key into config/local.env (never a tracked file) and into this process, then reload the config:
        the chat's next message builds its client with it. The value never comes back."""
        require_json(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        name, value = (body.get("name"), body.get("value")) if isinstance(body, dict) else (None, None)
        if not isinstance(name, str) or not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Send {\"name\": ..., \"value\": ...}")
        cfg_path = self._config_path()
        await asyncio.to_thread(write_key, cfg_path, name, value)
        try:
            # On the loop, as the admin endpoint does: the reload walks the live plugin instances.
            reload_app_config(request.app)
            self._running_config = getattr(request.app.state, "config", None)
            reloaded = None
        except (ConfigReloadUnavailable, ConfigReloadFailed) as error:
            logger.warning("setup: %s written, the config reload failed: %s", name, error)
            reloaded = str(error)
        logger.info("setup: %s written to %s", name, cfg_path.parent / LOCAL_SECRETS)  # the name only
        return {"name": name, "reload_error": reloaded,
                "state": await asyncio.to_thread(installation_state, self.system_config, running_signing_key())}

    async def post_signing_key(self, request: Request, _admin: None = Depends(require_admin_viewer)) -> dict[str, Any]:
        """Give the installation its own signing key in the local layer; a restart applies it."""
        require_json(request)
        cfg_path = self._config_path()
        try:
            message = await asyncio.to_thread(ensure_signing_key, cfg_path, SHIPPED_SIGNING_KEYS)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=409, detail=f"No signing key written: {error}") from None
        logger.info("setup signing key: %s", message)
        return {"message": message,
                "state": await asyncio.to_thread(installation_state, self.system_config, running_signing_key())}


def require_json(request: Request) -> None:
    """JSON only, so a page elsewhere cannot send it on the admin's cookie (a cross-site form post is a "simple"
    request, which a JSON Content-Type is not)."""
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Send the request as JSON")


def write_key(cfg_path: Path, name: str, value: str) -> None:
    """A key the configuration names, into the local secrets file and this process's environment. HTTPException
    where it cannot or must not be."""
    if name not in api_keys(str(cfg_path)):
        raise HTTPException(status_code=400, detail=f"{name} is no API key the configuration names (the signing "
                                                    "key has a button of its own)")
    if set_by_the_environment(name):
        raise HTTPException(status_code=409, detail=f"{name} is set in the environment the API was started with, "
                                                    "which wins over any file: change it there")
    if not environment_takes(name, value.strip()):  # before the file: written but not taken would be both
        raise HTTPException(status_code=400, detail=f"{name} not written: longer than an environment variable can be")
    try:
        with WRITING:  # file and environment alike: two saves of one name must not leave them apart
            write_secret(cfg_path, name, value)
            take_secret(name, value.strip())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{name} not written: {error}") from None
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"{name} not written: {error}") from None
