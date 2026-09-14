"""Routes of the UI itself: the panel catalogue, the core panels, the kit page."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer

from .catalog import Panel, PanelSpecError, build_catalog, core_panels, plugin_panel, roles_allowed
from .resources import sprite_icons, ui_templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])
templates = ui_templates()
CORE_PANELS = ("session", "system", "settings")


async def viewer_role(request: Request) -> str:
    """The role panels are filtered for. Without auth there is one user, the owner."""
    config = request.app.state.config
    if not config.auth.enabled:
        return "admin"
    from agent_system.auth.database import get_db
    from agent_system.auth.dependencies import get_optional_user

    credentials = await HTTPBearer(auto_error=False)(request)
    user = await get_optional_user(request, credentials, request.headers.get("X-API-Key"), get_db())
    # A deactivated account keeps its token until it expires; it is nobody here.
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user.role.value


def plugin_panels(auth_config) -> list[Panel]:
    """Panels declared by the registered web plugins' schemas, each for the roles its routes admit."""
    from agent_system.auth.enforcement import EndpointSecurityEnforcer
    from agent_system.plugins.mcp_adapter import plugin_mcp_registry
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
        web_ui = plugin_mcp_registry.get_server(instance).plugin_schema.get("web_ui") or {}
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
        plugin_policy = plugin_security.get_plugin_policy(instance, panel.url, "GET")
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
    return build_catalog(role, core, plugin_panels(config.auth))


@router.get("/ui/kit", response_class=HTMLResponse)
async def kit_page(request: Request):
    """Every kit component in every state -- documentation and visual check."""
    return templates.TemplateResponse(request, "kit/kit_page.html", {"icons": sprite_icons()})


@router.get("/ui/panels/{name}", response_class=HTMLResponse)
async def core_panel(request: Request, name: str):
    if name not in CORE_PANELS:
        raise HTTPException(status_code=404, detail=f"No core panel {name!r}")
    return templates.TemplateResponse(request, f"panels/{name}.html")
