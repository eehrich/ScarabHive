"""
Menu API Endpoints

Provides API endpoints for dropdown menu system.
Plugins can contribute menu definitions and menu items.
"""

from __future__ import annotations

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request

from agent_system.plugins import plugin_web_registry
from agent_system.auth.models import User, UserRole
from agent_system.auth.database import get_db, UserDatabase

logger = logging.getLogger(__name__)

menu_router = APIRouter(prefix="/api", tags=["menus"])


async def get_current_user_optional(
    request: Request,
    db: UserDatabase = Depends(get_db)
) -> Optional[User]:
    """Get current user if authenticated, None otherwise"""
    try:
        from agent_system.auth.dependencies import get_current_user as get_user_dep
        from fastapi.security import HTTPBearer
        
        bearer_scheme = HTTPBearer(auto_error=False)
        credentials = await bearer_scheme(request)
        x_api_key = request.headers.get("X-API-Key")
        
        user = await get_user_dep(
            request=request,
            credentials=credentials,
            x_api_key=x_api_key,
            db=db
        )
        return user
    except Exception:
        # User not authenticated, return None
        return None


@menu_router.get("/menu-definitions")
async def get_menu_definitions(
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Get all dropdown menu definitions from plugins
    
    Returns list of menu definitions including built-in "user" menu
    and any custom menus defined by plugins.
    """
    menus = []
    
    # Built-in user menu (always present when authenticated)
    if current_user:
        menus.append({
            "id": "user",
            "label": f"👤 {current_user.username}",
            "position": "right",
            "order": 1000,  # Always last on right side
            "builtin": True,
            "tooltip": "User account menu"
        })
    
    # Add plugin-defined menus
    for name, plugin in plugin_web_registry.web_plugins.items():
        if hasattr(plugin, 'get_menu_definitions'):
            try:
                plugin_menus = plugin.get_menu_definitions()
                for menu in plugin_menus:
                    # Filter admin-only menus
                    if menu.get("requires_admin", False):
                        if not current_user or current_user.role != UserRole.ADMIN:
                            continue
                    
                    menu_config = dict(menu)
                    menu_config["plugin_name"] = name
                    menus.append(menu_config)
            except Exception as e:
                logger.error(f"Error getting menu definitions from plugin {name}: {e}")
    
    # Sort by position (left first) and order
    menus.sort(key=lambda x: (
        0 if x.get("position", "right") == "left" else 1,
        x.get("order", 100)
    ))
    
    return menus


@menu_router.get("/menu-items")
async def get_menu_items(
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Get all menu items from plugins
    
    Returns list of menu items organized by menu_id.
    Includes built-in user menu items and plugin-contributed items.
    """
    items = []
    
    # Add built-in user menu items
    if current_user:
        items.extend([
            {
                "id": "profile",
                "menu_id": "user",
                "section": "account",
                "label": "Profile",
                "action": "showProfile",
                "icon": "👤",
                "order": 10,
                "builtin": True
            },
            {
                "id": "settings",
                "menu_id": "user",
                "section": "account",
                "label": "Settings",
                "action": "showSettings",
                "icon": "⚙️",
                "order": 20,
                "builtin": True,
                "divider_after": True
            },
            {
                "id": "logout",
                "menu_id": "user",
                "section": "auth",
                "label": "Logout",
                "onclick": "handleLogout",
                "icon": "🚪",
                "order": 1000,  # Always last
                "builtin": True,
                "divider_before": True
            }
        ])
        
        # Add profiling menu items for admins when enabled
        if current_user.role == UserRole.ADMIN:
            from agent_system.utils.profiling import PROFILING_ENABLED
            from agent_system.utils.memory_profiling import MEMORY_PROFILING_ENABLED
            
            # Security Audit (always available for admins)
            items.append({
                "id": "security_audit",
                "menu_id": "user",
                "section": "admin",
                "label": "Security Audit",
                "action": "openPanel",
                "panel_endpoint": "/api/security/audit",
                "panel_title": "Plugin Security Audit",
                "icon": "🔒",
                "order": 54,  # Before performance dashboard
                "builtin": True,
                "requires_admin": True,
                "tooltip": "View plugin endpoint access audit log"
            })
            
            if PROFILING_ENABLED:
                items.append({
                    "id": "performance_dashboard",
                    "menu_id": "user",
                    "section": "admin",  # Same section as user_management
                    "label": "Performance",
                    "action": "openPanel",
                    "panel_endpoint": "/debug/profile/dashboard",
                    "panel_title": "Performance Profiling",
                    "icon": "🔬",
                    "order": 55,  # After user_management (50)
                    "builtin": True,
                    "requires_admin": True,
                    "badge": "Active"
                })
            
            if MEMORY_PROFILING_ENABLED:
                items.append({
                    "id": "memory_dashboard",
                    "menu_id": "user",
                    "section": "admin",  # Same section as user_management
                    "label": "Memory",
                    "action": "openPanel",
                    "panel_endpoint": "/debug/memory/dashboard",
                    "panel_title": "Memory Profiling",
                    "icon": "🧠",
                    "order": 56,  # After performance
                    "builtin": True,
                    "requires_admin": True,
                    "badge": "Active"
                })
    
    # Add plugin menu items
    # Load menu items from schema.yaml (already rendered with Jinja2 templates)
    from agent_system.plugins.mcp_adapter import plugin_mcp_registry
    
    for name, plugin in plugin_web_registry.web_plugins.items():
        try:
            # Get schema from already registered plugin server (includes Jinja2 template rendering)
            plugin_server = plugin_mcp_registry.get_server(name)
            
            if plugin_server and hasattr(plugin_server, 'plugin_schema') and plugin_server.plugin_schema:
                schema = plugin_server.plugin_schema
                logger.debug(f"Got schema for {name} from plugin server")
                
                web_ui = schema.get('web_ui', {})
                menu_config = web_ui.get('menu', {})
                
                if menu_config.get('enabled', False):
                    schema_items = menu_config.get('items', [])
                    logger.debug(f"Loaded {len(schema_items)} menu items from schema for {name}")
                    
                    # Add items with filtering
                    for item in schema_items:
                        # Filter admin-only items
                        if item.get("requires_admin", False):
                            if not current_user or current_user.role != UserRole.ADMIN:
                                continue
                        
                        item_config = dict(item)
                        item_config["plugin_name"] = name
                        items.append(item_config)
                
        except Exception as e:
            logger.error(f"Error getting menu items from plugin {name}: {e}", exc_info=True)
    
    # Sort by menu_id, section, and order
    items.sort(key=lambda x: (
        x.get("menu_id", ""),
        x.get("section", ""),
        x.get("order", 100)
    ))
    
    return items
