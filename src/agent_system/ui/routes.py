"""Routes of the UI itself: the panel catalogue, the core panels, the kit page, the help library."""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer

from agent_system.auth.dependencies import require_admin
from agent_system.auth.middleware import AUDIT_CATEGORIES, AUDIT_STATUS_CLASSES, security_audit_log

from .catalog import Panel, PanelSpecError, build_catalog, core_panels, plugin_panel, roles_allowed
from .help import panel_guides
from .help import router as help_router
from .resources import sprite_icons, ui_templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])
router.include_router(help_router)
templates = ui_templates()
CORE_PANELS = ("session", "system", "settings", "help", "memory_profile", "performance")


async def viewer_user(request: Request) -> Any:
    """The signed-in user (401 for nobody); None while auth is off -- one user, the owner."""
    if not request.app.state.config.auth.enabled:
        return None
    from agent_system.auth.database import get_db
    from agent_system.auth.dependencies import get_optional_user

    credentials = await HTTPBearer(auto_error=False)(request)
    user = await get_optional_user(request, credentials, request.headers.get("X-API-Key"), get_db())
    # A deactivated account keeps its token until it expires; it is nobody here.
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def viewer_role(request: Request) -> str:
    """The role panels are filtered for. Without auth there is one user, the owner."""
    user = await viewer_user(request)
    return "admin" if user is None else user.role.value


def plugin_panels(auth_config) -> list[Panel]:
    """Panels declared by the registered web plugins' schemas, each for the roles its routes admit."""
    from agent_system.auth.enforcement import EndpointSecurityEnforcer
    from agent_system.plugins.tool_adapter import plugin_tool_registry
    from agent_system.plugins.web_adapter import get_plugin_security_enforcer, plugin_web_registry

    plugin_security = get_plugin_security_enforcer()
    if auth_config.enabled and plugin_security.auth_config is None:
        # Plugin routes get their security when they are mounted (apply_to_app); unset,
        # they were never mounted -- and nobody can be told who may open them.
        logger.error("Plugin route security is not set up: plugin panels are left out of the catalogue")
        return []
    # Two layers guard a plugin route: the app-wide endpoint rules and the plugin rules.
    endpoint_security = EndpointSecurityEnforcer(auth_config)
    panels = []
    for instance in plugin_web_registry.web_plugins:
        web_ui = plugin_tool_registry.get_server(instance).plugin_schema.get("web_ui") or {}
        try:
            if not isinstance(web_ui, dict):
                raise PanelSpecError(f"{instance}: web_ui must be a mapping")
            if "panel" not in web_ui:
                continue
            panel = plugin_panel(instance, web_ui["panel"], sprite_icons())
        except PanelSpecError as error:
            # Loud: the panel is missing from the launcher until the schema is fixed.
            logger.error("Panel of plugin %s left out of the catalogue: %s", instance, error)
            continue
        # As the route guard reads it: a plugin type's own role holds under any instance name
        plugin_policy = plugin_web_registry.effective_policy(plugin_security, instance, panel.url, "GET")
        endpoint_policy = endpoint_security.get_endpoint_policy("GET", panel.url)
        panel.roles = roles_allowed((plugin_policy["requires_auth"], plugin_policy["min_role"]),
                                    (endpoint_policy.requires_auth, endpoint_policy.min_role))
        panels.append(panel)
    return panels


@router.get("/api/ui/catalog")
async def catalog(request: Request):
    """Every panel this viewer may open, with the fixed category list."""
    from agent_system.utils.memory_profiling import MEMORY_PROFILING_ENABLED
    from agent_system.utils.profiling import PROFILING_ENABLED

    config = request.app.state.config
    role = await viewer_role(request)
    core = core_panels(
        audit_enabled=bool(config.auth.enabled and config.auth.endpoint_security.audit_enabled),
        profiling_enabled=PROFILING_ENABLED,
        memory_profiling_enabled=MEMORY_PROFILING_ENABLED,
    )
    plugins = plugin_panels(config.auth)
    guides = panel_guides(config, [panel.id for panel in plugins])
    for panel in plugins:
        panel.help = guides.get(panel.id, "")
    return build_catalog(role, core, plugins)


#: How long one plugin may take to say whether it holds data for a session: past it, its panel is offered.
SESSION_PANEL_TIMEOUT = 0.5
#: Sync answers under way in a worker thread, by (instance, session, user): the same question asked meanwhile
#: shares the thread.
_ASKING: dict[tuple[str, str, str], asyncio.Task] = {}
#: Questions still running past the limit, by instance: waiting gives up, the thread does not. While one runs,
#: the instance's panel is offered without another thread, so a hung store holds one worker, not one per click.
_OVERDUE: dict[str, set[tuple[str, str, str]]] = {}


async def _answer_in_thread(ask: Any, session_id: str, user_id: str) -> Any:
    """A sync ``has_session_data`` in a worker thread, with the caller's context (as ``asyncio.to_thread``); an
    awaitable it hands back (an async callable that is no coroutine function) is awaited here, once for all."""
    context = contextvars.copy_context()
    answer = await asyncio.get_running_loop().run_in_executor(None, context.run, ask, session_id, user_id)
    return await answer if inspect.isawaitable(answer) else answer


def _released(key: tuple[str, str, str], done: asyncio.Task) -> None:
    """The question is through (a failure after the wait gave up is taken here, or asyncio would log it as never
    retrieved)."""
    _ASKING.pop(key, None)
    overdue = _OVERDUE.get(key[0], set())
    overdue.discard(key)
    if not overdue:
        _OVERDUE.pop(key[0], None)
    if not done.cancelled():
        done.exception()


async def _holds(instance: str, plugin: Any, session_id: str, user_id: str) -> bool:
    """A plugin's ``has_session_data(session_id, user_id)`` (sync or async). Without the method, or when it fails or
    takes too long, the panel is offered: one entry too many beats one hidden."""
    ask = getattr(plugin, "has_session_data", None)
    if not callable(ask):
        return True
    try:
        if inspect.iscoroutinefunction(ask):
            answer = await asyncio.wait_for(ask(session_id, user_id), SESSION_PANEL_TIMEOUT)
        else:
            if instance in _OVERDUE:
                return True
            key = (instance, session_id, user_id)
            running = _ASKING.get(key)
            if running is None:
                running = _ASKING[key] = asyncio.ensure_future(_answer_in_thread(ask, session_id, user_id))
                running.add_done_callback(lambda done: _released(key, done))
            try:
                answer = await asyncio.wait_for(asyncio.shield(running), SESSION_PANEL_TIMEOUT)
            except TimeoutError:
                if not running.done():
                    _OVERDUE.setdefault(instance, set()).add(key)
                raise
    except Exception as exc:  # TimeoutError included
        logger.warning("Panel %s: has_session_data(%s) failed (%s) -- offered", instance, session_id,
                       type(exc).__name__ if isinstance(exc, TimeoutError) else exc)
        return True
    return bool(answer)


@router.get("/api/ui/session-panels")
async def session_panels(request: Request, session_id: str):
    """The plugin panels with a session entry that hold data for *session_id*: the chat's "Open in" menu offers
    those (and the core ones). 404 for a session the viewer may not see (``may_see_session``)."""
    from agent_system.auth.session_access import may_see_session, viewer
    from agent_system.plugins.web_adapter import plugin_web_registry

    user = await viewer_user(request)
    if not await may_see_session(request, user, session_id):
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
    role = "admin" if user is None else user.role.value
    panels = [panel for panel in plugin_panels(request.app.state.config.auth) if "session" in panel.contexts]
    visible = [panel for panel in panels if panel.visible_to(role)]  # what the viewer's catalogue holds
    plugins = plugin_web_registry.web_plugins
    # Instances of a plugin that declares ``session_data_shared`` show one list under one title, answered for by one
    # of them (the sub-agent manager's: the instance that spawned). When that one is beyond the viewer's role, a
    # visible sibling stands in for it.
    def shared(panel: Panel) -> bool:
        return getattr(plugins.get(panel.id), "session_data_shared", False) is True

    titles = {panel.title for panel in visible if shared(panel)}
    asked = visible + [panel for panel in panels if not panel.visible_to(role) and panel.title in titles and shared(panel)]
    answers = await asyncio.gather(*(_holds(p.id, plugins.get(p.id), session_id, viewer(user)) for p in asked))
    held = {panel.id: yes for panel, yes in zip(asked, answers)}
    claimed = {panel.title for panel in asked if held[panel.id] and not panel.visible_to(role)}
    claimed -= {panel.title for panel in visible if held[panel.id]}
    stand_ins = {min(panel.id for panel in visible if panel.title == title and shared(panel)) for title in claimed}
    offered = [panel.id for panel in visible if held[panel.id] or panel.id in stand_ins]
    return {"session_id": session_id, "panels": offered,
            "hidden": [panel.id for panel in visible if panel.id not in offered]}


@router.get("/ui/kit", response_class=HTMLResponse)
async def kit_page(request: Request):
    """Every kit component in every state -- documentation and visual check."""
    return templates.TemplateResponse(request, "kit/kit_page.html", {"icons": sprite_icons()})


# Before /ui/panels/{name}, which would take the name and refuse it.
@router.get("/ui/panels/security_audit", response_class=HTMLResponse,
            dependencies=[Depends(security_audit_log), Depends(require_admin)])
async def security_audit_panel(request: Request):
    """The Security Audit panel: administrators only, and only while the audit log is kept."""
    return templates.TemplateResponse(request, "panels/security_audit.html",
                                      {"categories": AUDIT_CATEGORIES, "statuses": AUDIT_STATUS_CLASSES})


@router.get("/ui/panels/{name}", response_class=HTMLResponse)
async def core_panel(request: Request, name: str):
    if name not in CORE_PANELS:
        raise HTTPException(status_code=404, detail=f"No core panel {name!r}")
    if name == "memory_profile":
        from agent_system.api.debug_endpoints import require_memory_profile_access
        await require_memory_profile_access(request)
    if name == "performance":
        from agent_system.api.debug_endpoints import require_performance_access
        await require_performance_access(request)
    return templates.TemplateResponse(request, f"panels/{name}.html")
