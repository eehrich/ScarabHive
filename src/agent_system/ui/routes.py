"""Routes of the UI itself: the panel catalogue, the core panels, the kit page, the help library."""
from __future__ import annotations

import asyncio
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
            answer = await asyncio.wait_for(asyncio.to_thread(ask, session_id, user_id), SESSION_PANEL_TIMEOUT)
    except Exception as exc:  # TimeoutError included
        logger.warning("Panel %s: has_session_data(%s) failed (%s) -- offered", instance, session_id,
                       type(exc).__name__ if isinstance(exc, TimeoutError) else exc)
        return True
    return bool(answer)


@router.get("/api/ui/session-panels")
async def session_panels(request: Request, session_id: str):
    """The plugin panels with a session entry that hold data for *session_id*: the chat's "Open in" menu offers
    those (and the core ones). 404 for a session the viewer may not see, as for one that does not exist."""
    from agent_system.auth.session_access import may_see_session, viewer
    from agent_system.plugins.web_adapter import plugin_web_registry

    user = await viewer_user(request)
    if not await may_see_session(request, user, session_id):
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")
    role = "admin" if user is None else user.role.value
    instances = [panel.id for panel in plugin_panels(request.app.state.config.auth)
                 if "session" in panel.contexts and panel.visible_to(role)]  # what the viewer's catalogue holds
    plugins = plugin_web_registry.web_plugins
    held = await asyncio.gather(*(_holds(i, plugins.get(i), session_id, viewer(user)) for i in instances))
    return {"session_id": session_id, "panels": [i for i, yes in zip(instances, held) if yes],
            "hidden": [i for i, yes in zip(instances, held) if not yes]}


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
