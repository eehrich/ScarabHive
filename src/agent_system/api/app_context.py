"""What the app's own routes share: the AppContext build_app makes, and the lookups around it.

The routes in ``api/*_routes.py`` were closures inside ``build_app`` (app.py), and
shared what they captured there: the configuration build_app started with, the
entry agent, the endpoint security enforcer, and the checks built on those.
``AppContext`` holds exactly that, once per app (``app.state.context``); a handler
reaches it with ``Depends(app_context)``. A class rather than module state because
it is per app: tests build several apps in one process, and a module-level value
would make one app answer with another app's agent or auth.

Two configurations, on purpose. ``AppContext.config`` is the one build_app started
with: the enforcer and the middleware were built from it, and a reload does not
rebuild them, so every auth and role-gate check judges by it. ``live_config()`` is
the one the app runs on now (POST /admin/reload-config replaces app.state.config):
whatever answers a question ABOUT the configuration reads that one. Every handler
reads the same one it read as a closure.

Not in here: the API's shared services (session_service, app_registry,
tool_integration, ...). They belong to whichever build_app ran last and are read at
call time from ``agent_system.app_state``, as everywhere else.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, status

from agent_system import app_state
from agent_system.auth.enforcement import AnonymousUser, EndpointSecurityEnforcer
from agent_system.services.background_job_manager import get_background_job_manager

logger = logging.getLogger(__name__)


async def parse_json_body(request: Request) -> Any:
    """Parse the request's JSON body — THE single place mapping malformed
    input to HTTP 400 (client error) instead of an unhandled 500. Used by
    every endpoint that reads a JSON body (/run, /events, session appends)."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON body: could not be parsed"
        )


async def parse_json_object(request: Request) -> dict:
    """The request's JSON body as an object; no body at all (``null``) reads as {}.

    A JSON array or bare string parses fine but has no ``.get`` -- answered
    "bad request" here rather than as the AttributeError's 500 in the handler.
    """
    body = await parse_json_body(request)
    if body is not None and not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return body or {}


def errors_as_200(failed_to: str, *, prefixed: bool = True):
    """Decorate a route that answers a failure with 200 and ``{"error": ...}`` instead of a 500.

    The tool and hook status routes do: the pages that poll them show the text
    in place of the data. Logged with its traceback under the route's own module;
    ``prefixed`` puts "Failed to <failed_to>: " in front of the error's text.
    """
    def decorate(route):
        @functools.wraps(route)
        async def answered(*args, **kwargs):
            try:
                return await route(*args, **kwargs)
            except Exception as e:
                logging.getLogger(route.__module__).exception("Failed to %s: %s", failed_to, e)
                return {"error": f"Failed to {failed_to}: {e}" if prefixed else str(e)}
        return answered
    return decorate


def registry_of(request: Request) -> Any:
    """The registry the app serving *request* looks agents up in: its own, else the process-wide one."""
    return getattr(request.app.state, "tool_registry", None) or app_state.app_registry


class NotAnAgent(LookupError):
    """The name is registered, but as a tool server: something a run cannot run on."""


def registered_agent(registry: Any, name: str) -> Any:
    """The Agent registered as *name* in *registry*.

    KeyError when nothing is (no registry either), NotAnAgent when what is
    registered is a tool server; anything else the registry raises passes
    through -- each caller answers those three cases its own way (404, 400,
    an error dict, a quiet None).
    """
    from agent_system.servers.agent.server import Agent as _Agent

    found = registry.get(name) if registry is not None else None
    if found is None:
        raise KeyError(name)
    if not isinstance(found, _Agent):
        raise NotAnAgent(name)
    return found


def lookup_agent(source: Any, name: str) -> Any:
    """registered_agent in the registry of the app serving *source* (a Request, or the app itself)."""
    app = source.app if isinstance(source, Request) else source
    return registered_agent(getattr(app.state, "tool_registry", None) or app_state.app_registry, name)


def chat_agent(request: Request, agent_name: Optional[str]):
    """The agent a chat surface is talking to, or None.

    Plugin commands are per AGENT: the list holds only what that agent's
    own allowlist lets it dispatch, so every chat endpoint that touches
    them has to know which agent is meant. A name that is not a registered
    agent yields None rather than falling back to the default -- answering
    for a different agent would list commands the named one may not run.
    """
    from agent_system.servers.agent.server import Agent as _Agent

    if not agent_name:
        entry = getattr(request.app.state, "agent", None)
        return entry if isinstance(entry, _Agent) else None
    try:
        return lookup_agent(request, agent_name)
    except Exception as e:
        logger.debug("No agent '%s': %s", agent_name, e)
        return None


# Helper function for optional user authentication
async def _get_current_user_optional(request: Request) -> Optional[Any]:
    """Get current user if authenticated, None otherwise.

    Checks multiple auth methods in order (via get_current_user dependency):
    1. Bearer token in Authorization header
    2. JWT token in access_token cookie (browser, including EventSource)
    3. X-API-Key header (for programmatic access)

    No ``?token=`` query parameter: see EndpointSecurityMiddleware._extract_user_info.
    """
    try:
        from agent_system.auth.dependencies import get_current_user as get_user_dep
        from agent_system.auth.database import get_db
        from fastapi.security import HTTPBearer

        bearer_scheme = HTTPBearer(auto_error=False)
        credentials = await bearer_scheme(request)
        x_api_key = request.headers.get("X-API-Key")

        # Debug: Check what auth methods are available
        has_bearer = credentials is not None
        has_cookie = request.cookies.get("access_token") is not None
        has_api_key = x_api_key is not None
        logger.debug(f"[AUTH_DEBUG] Auth methods - Bearer: {has_bearer}, Cookie: {has_cookie}, API-Key: {has_api_key}")

        # Get database instance (NOT a generator!)
        db = get_db()

        # Call get_current_user with the database instance
        user = await get_user_dep(
            request=request,
            credentials=credentials,
            x_api_key=x_api_key,
            db=db
        )
        if user:
            logger.debug(f"[AUTH_DEBUG] ✅ Authenticated user: {user.username}, role: {user.role}")
        else:
            logger.debug("[AUTH_DEBUG] ⚠️ get_current_user returned None")
        return user
    except Exception as e:
        # User not authenticated
        logger.debug(f"[AUTH_DEBUG] ❌ Authentication failed: {e}")
        return None


class AppContext:
    """The state the app's own routes share, and the checks built on it (``app.state.context``)."""

    def __init__(self, app: FastAPI, config: Any, agent: Any, config_path: str,
                 security_enforcer: EndpointSecurityEnforcer) -> None:
        self.app = app
        #: The config build_app started with -- what the auth checks judge by (see the module docstring).
        self.config = config
        #: The entry agent: what a run without an agent_name runs on, and whose tracker
        #: answers for a session nobody else names.
        self.agent = agent
        #: The config file build_app loaded (GET /health reads its version from it).
        self.config_path = config_path
        self._security_enforcer = security_enforcer

    def live_config(self):
        """The config THIS app currently runs on.

        POST /admin/reload-config replaces app.state.config (admin_endpoints),
        while ``self.config`` stays the config build_app started
        with. Anything that answers a question ABOUT the configuration --
        profiles, defaults, an override -- has to read the live one, or it
        keeps answering from the state at process start. Per app on purpose:
        a module-level ConfigService is overwritten by the next build_app and
        would make one app answer with another app's config.
        """
        return getattr(self.app.state, "config", None) or self.config

    async def enforce_endpoint_security(self, request: Request) -> Any:
        """Enforce security for an endpoint and return the user.

        This is the central security enforcement function that should be called
        at the start of protected endpoints. It:
        1. Checks if auth is required for this endpoint
        2. Validates user authentication
        3. Checks role permissions
        4. Returns user (or AnonymousUser if permitted)

        Raises:
            HTTPException: 401 if auth required but not provided
            HTTPException: 403 if user lacks required role

        Returns:
            User object or AnonymousUser
        """
        return await self._security_enforcer.enforce_endpoint_security(
            request,
            _get_current_user_optional
        )

    def validate_llm_access(self, user: Any, is_llm_request: bool = False) -> None:
        """Validate that user is allowed to make LLM requests.

        Args:
            user: User object (User or AnonymousUser)
            is_llm_request: Whether this is an LLM API call

        Raises:
            HTTPException: 403 if user not allowed LLM access
        """
        if not is_llm_request:
            return

        if not self.config.auth.enabled:
            return

        llm_security = self.config.auth.llm_security

        if not llm_security.require_valid_user:
            return

        # Check if anonymous user
        if isinstance(user, AnonymousUser) or (user and not getattr(user, 'is_authenticated', True)):
            if llm_security.max_requests_per_hour_anonymous == 0:
                logger.warning("[SECURITY] Anonymous LLM request blocked")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Anonymous users are not allowed to make LLM requests. Please log in."
                )

        if user is None:
            logger.warning("[SECURITY] LLM request without user context blocked")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="LLM requests require authentication"
            )

    def gate_refuses(self, target: Any, who: Any) -> bool:
        """Whether *who* may not run *target* under its role gate (metadata.min_role).

        *who* is what the endpoint resolved: an account, an AnonymousUser, or None.
        Judged against the auth this process enforces -- the start config the
        enforcer and the middleware were built from, not a reloaded one: a reload
        that switched auth off would otherwise open every gated agent to callers
        the middleware still makes sign in.

        The reason is logged here and goes no further: every endpoint answers a
        refusal exactly as it answers an agent that does not exist, so the answer
        does not tell a caller which agents are there behind a gate.
        """
        from agent_system.auth.agent_access import agent_run_denial

        reason = agent_run_denial(getattr(target, "min_role", None), who, self.config.auth)
        if reason:
            logger.info("Refused agent '%s' to %s: %s", getattr(target, "name", None) or "?",
                        getattr(who, "username", None) or "an unidentified caller", reason)
        return bool(reason)

    async def gate_refuses_caller(self, request: Request, target: Any) -> bool:
        """gate_refuses for an endpoint that has not resolved its caller.

        The caller is resolved only when a gate is in play, so an ungated agent
        is answered exactly as before.
        """
        if getattr(target, "min_role", None) is None or not self.config.auth.enabled:
            return False
        return self.gate_refuses(target, await self.enforce_endpoint_security(request))

    async def verify_session_owner(self, sid: str, current_user: Any,
                                   tracker: Any = None) -> None:
        """Raise 403 if the authenticated user does not own the session.

        These legacy endpoints act on the shared in-memory session tracker keyed
        only by session_id, so without this any authenticated user could
        read/mutate another user's session (IDOR). No-op when auth is disabled
        (current_user is None -> single-user mode) or no owner is recorded.

        *tracker* is the one the CALLER is about to act on. It matters: every
        registered agent carries its own SessionTracker (measured: 122 agents,
        none sharing the default's), so checking the default agent's tracker
        while writing another agent's found no owner for a session that has
        one -- and a not-yet-persisted session has no owner on disk either, so
        the check passed for anybody. Callers that pass nothing keep the old
        behaviour of asking the entry agent.

        A run of this process holding the session names its owner first, whichever
        agent it runs on: a session such a run has not saved yet has an owner in no
        other tracker and not on disk -- and an append to it goes to that run.

        The id is checked first (400): the disk lookup joins it into a path, and
        a name no session can have names nobody's session. A lookup that FAILS
        refuses (503) rather than reading as "no owner": that read let anybody
        through whenever the store could not be asked. The disk scan walks every
        user directory, so it runs off the event loop.
        """
        from agent_system.services.session_paths import validate_session_id

        if current_user is None:
            return
        try:
            validate_session_id(sid)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Not a session id: {sid!r}")
        if tracker is None:
            tracker = getattr(self.agent, "_session_tracker", None)
        owner = ((await get_background_job_manager().active_sessions()).get(sid) or {}).get("user_id")
        try:
            # In-memory session metadata next (covers sessions not yet persisted),
            # then the persisted owner on disk.
            if owner is None and tracker is not None:
                owner = (tracker.get_session_metadata(sid) or {}).get("user_id")
            manager = getattr(app_state.session_service, "session_manager", None)
            if owner is None and manager is not None:
                owner = await asyncio.to_thread(manager._find_session_owner, sid)
        except FileNotFoundError:
            owner = None  # no session store on disk yet: nobody owns anything there
        except Exception:
            logger.exception("Could not look up the owner of session %s", sid)
            raise HTTPException(status_code=503, detail="Could not check who owns this session")
        if owner is not None and owner != current_user.username:
            raise HTTPException(status_code=403, detail="You do not have access to this session")


async def app_context(request: Request) -> AppContext:
    """The AppContext of the app serving *request*: ``ctx: AppContext = Depends(app_context)``."""
    return request.app.state.context
