"""
Plugin Web Interface and Registry

Provides web capabilities for AgentSystem plugins including endpoints,
static assets, and UI panels. Includes centralized security enforcement
for all plugin-provided HTTP endpoints.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pathlib import Path
from abc import ABC

from fastapi import APIRouter, FastAPI, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette import status

if TYPE_CHECKING:
    from agent_system.config.models import AuthConfig

logger = logging.getLogger(__name__)


class PluginWebInterface(ABC):
    """Base interface for plugins with web capabilities"""
    
    def get_web_router(self) -> Optional[APIRouter]:
        """Return FastAPI router with plugin endpoints
        
        Returns:
            Optional[APIRouter]: Router with plugin-specific endpoints,
                                or None if plugin has no web endpoints
        """
        return None
    
    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets (JS/CSS/HTML)
        
        Returns:
            Optional[Path]: Path to static assets directory,
                           or None if plugin has no static assets
        """
        return None
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Return list of UI panel configurations
        
        Returns:
            List[Dict[str, Any]]: List of panel configuration dictionaries.
                Each panel config should include:
                - id: unique panel identifier
                - title: display title for panel tab
                - url: URL for panel content (usually HTML page)
                - icon: icon name/class for panel tab (optional)
                - position: "left", "right", "bottom", "top" (optional, default: "right")
                - width: CSS width value (optional, default: "400px")
                - height: CSS height value (optional, default: "300px")
        """
        return []
    
    def get_menu_items(self) -> List[Dict[str, Any]]:
        """Return list of dropdown menu item configurations
        
        Plugins can add items to predefined menus (user, admin, tools, help)
        or custom menus defined via get_menu_definitions().
        
        Returns:
            List[Dict[str, Any]]: List of menu item configuration dictionaries.
                Each menu item config should include:
                - id: unique menu item identifier
                - menu_id: which menu to add to ("user", "admin", "tools", "help", or custom)
                - label: display text for menu item
                - url: URL to navigate to (optional if onclick provided)
                - icon: icon emoji or class (optional)
                - requires_admin: bool, show only to admins (default: False)
                - section: section within menu (e.g., "account", "settings") (optional)
                - order: integer for sorting (default: 100)
                - target: "_blank", "_self", etc. (default: "_self")
                - onclick: JavaScript function name (optional, instead of url)
                - divider_after: bool, add divider after this item (default: False)
                - divider_before: bool, add divider before this item (default: False)
                - badge: badge text/count to show (optional)
                - shortcut: keyboard shortcut hint (optional)
        """
        return []
    
    def get_menu_definitions(self) -> List[Dict[str, Any]]:
        """Return custom dropdown menu definitions
        
        Allows plugins to create entirely new dropdown menus in the header.
        Built-in menus: "user" (always present when authenticated)
        
        Returns:
            List[Dict[str, Any]]: List of menu definition dictionaries.
                Each menu definition should include:
                - id: unique menu identifier (used in menu_items)
                - label: button text for menu dropdown
                - icon: icon emoji or class (optional)
                - position: "left" or "right" in header (default: "right")
                - order: integer for menu button positioning (default: 100)
                - requires_admin: bool, show only to admins (default: False)
                - tooltip: hover tooltip text (optional)
                - button_class: additional CSS classes for button (optional)
        """
        return []
    
    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration for endpoints
        
        Returns:
            Dict[str, Any]: Security configuration with keys:
                - require_auth: bool, whether endpoints require authentication
                - cors_origins: List[str], allowed CORS origins 
                - rate_limit: str, rate limit spec like "10/minute" (optional)
                - content_security_policy: str, CSP header value (optional)
        """
        return {
            "require_auth": False,
            "cors_origins": [],
            "rate_limit": None,
            "content_security_policy": None
        }


class PluginEndpointSecurityEnforcer:
    """
    Centralized security enforcement for plugin web endpoints.
    
    This class handles:
    - Plugin-level default policies
    - Per-plugin overrides
    - Pattern-based endpoint rules
    - Audit logging of plugin endpoint access (file + memory)
    """
    
    # Dedicated security audit logger
    _security_logger: Optional[logging.Logger] = None
    
    def __init__(self, auth_config: Optional["AuthConfig"] = None):
        """Initialize the enforcer with auth configuration.
        
        Args:
            auth_config: Authentication configuration from config.yaml
        """
        self.auth_config = auth_config
        self._audit_log: List[Dict[str, Any]] = []
        self._max_memory_entries = 1000  # Limit memory usage
        self._setup_security_logger()
        logger.info("PluginEndpointSecurityEnforcer initialized")
    
    def _setup_security_logger(self) -> None:
        """Setup dedicated security audit file logger."""
        if PluginEndpointSecurityEnforcer._security_logger is not None:
            return
        
        security_logger = logging.getLogger("agent_system.security.audit")
        security_logger.setLevel(logging.INFO)
        
        # Don't propagate to root logger
        security_logger.propagate = False
        
        # Create logs directory if needed
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        
        # File handler with rotation
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(
            log_dir / "security.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        
        # JSON-like format for easy parsing
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        security_logger.addHandler(handler)
        
        PluginEndpointSecurityEnforcer._security_logger = security_logger
        logger.info("Security audit logger initialized: logs/security.log")
    
    def update_config(self, auth_config: "AuthConfig") -> None:
        """Update the auth configuration."""
        self.auth_config = auth_config
        logger.debug("PluginEndpointSecurityEnforcer config updated")
    
    def get_plugin_policy(self, plugin_name: str, path: str, method: str = "GET") -> Dict[str, Any]:
        """Get the security policy for a plugin endpoint.
        
        Args:
            plugin_name: Name of the plugin
            path: Request path (e.g., "/plugins/todo/panel")
            method: HTTP method
        
        Returns:
            Dict with 'requires_auth', 'min_role', and 'description'
        """
        if not self.auth_config or not self.auth_config.enabled:
            return {
                "requires_auth": False,
                "min_role": None,
                "description": "Auth disabled"
            }
        
        plugin_security = self.auth_config.plugin_security
        
        # 1. Check endpoint-specific rules first (highest priority)
        for rule in plugin_security.endpoint_rules:
            pattern = rule.pattern
            if self._match_pattern(pattern, path, method):
                return {
                    "requires_auth": rule.policy == "require_auth",
                    "min_role": rule.min_role,
                    "description": rule.description or f"Matched rule: {pattern}"
                }
        
        # 2. Check plugin-specific overrides
        if plugin_name in plugin_security.plugin_overrides:
            override = plugin_security.plugin_overrides[plugin_name]
            return {
                "requires_auth": override.get("policy", plugin_security.default_policy) == "require_auth",
                "min_role": override.get("min_role", plugin_security.default_min_role),
                "description": f"Plugin override for {plugin_name}"
            }
        
        # 3. Use global default
        return {
            "requires_auth": plugin_security.default_policy == "require_auth",
            "min_role": plugin_security.default_min_role,
            "description": "Default plugin security policy"
        }
    
    def _match_pattern(self, pattern: str, path: str, method: str) -> bool:
        """Match a pattern against path and method.
        
        Supports:
        - Exact match: "/plugins/todo/panel"
        - Wildcard: "/plugins/*/admin/*"
        - Method prefix: "POST /plugins/todo/*"
        """
        pattern = pattern.strip()
        method = method.upper()
        
        # Parse method prefix if present
        pattern_method = "*"
        pattern_path = pattern
        
        parts = pattern.split(" ", 1)
        if len(parts) == 2 and parts[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "*"):
            pattern_method = parts[0].upper()
            pattern_path = parts[1]
        
        # Check method match
        if pattern_method != "*" and pattern_method != method:
            return False
        
        # Check path match with glob
        return fnmatch.fnmatch(path, pattern_path)
    
    def audit_access(
        self,
        plugin_name: str,
        path: str,
        method: str,
        user_id: Optional[str],
        allowed: bool,
        reason: str
    ) -> None:
        """Log plugin endpoint access for auditing.
        
        Logs to:
        - In-memory buffer (last 1000 entries for UI display)
        - logs/security.log file (DENIED only, rotated at 10MB, 5 backups)
        
        Args:
            plugin_name: Name of the plugin
            path: Request path
            method: HTTP method
            user_id: User ID or "anonymous"
            allowed: Whether access was allowed
            reason: Reason for the decision
        """
        if not self.auth_config or not self.auth_config.endpoint_security.audit_enabled:
            return
        
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        user = user_id or "anonymous"
        
        entry = {
            "timestamp": timestamp,
            "plugin_name": plugin_name,
            "path": path,
            "method": method,
            "user_id": user,
            "allowed": allowed,
            "reason": reason
        }
        
        # Keep limited entries in memory for UI display (both allowed and denied)
        self._audit_log.append(entry)
        if len(self._audit_log) > self._max_memory_entries:
            self._audit_log = self._audit_log[-self._max_memory_entries:]
        
        # Log only DENIED to security.log file (reduces log size significantly)
        if not allowed and PluginEndpointSecurityEnforcer._security_logger:
            PluginEndpointSecurityEnforcer._security_logger.warning(
                f"DENIED | {method} {path} | plugin={plugin_name} | user={user} | {reason}"
            )
    
    def get_audit_log(self, plugin_name: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent audit log entries from memory buffer.
        
        Note: For full audit history, check logs/security.log
        
        Args:
            plugin_name: Filter by plugin name (optional)
            limit: Maximum entries to return
        
        Returns:
            List of audit log entries (most recent last)
        """
        entries = self._audit_log
        if plugin_name:
            entries = [e for e in entries if e["plugin_name"] == plugin_name]
        # limit<=0 must mean "nothing", not "everything":
        # entries[-0:] is the full buffer.
        return entries[-limit:] if limit > 0 else []


# Global plugin endpoint security enforcer
_plugin_security_enforcer: Optional[PluginEndpointSecurityEnforcer] = None


def get_plugin_security_enforcer() -> PluginEndpointSecurityEnforcer:
    """Get the global plugin security enforcer."""
    global _plugin_security_enforcer
    if _plugin_security_enforcer is None:
        _plugin_security_enforcer = PluginEndpointSecurityEnforcer()
    return _plugin_security_enforcer


def init_plugin_security(auth_config: "AuthConfig") -> PluginEndpointSecurityEnforcer:
    """Initialize the plugin security enforcer with config."""
    global _plugin_security_enforcer
    if _plugin_security_enforcer is None:
        _plugin_security_enforcer = PluginEndpointSecurityEnforcer(auth_config)
    else:
        _plugin_security_enforcer.update_config(auth_config)
    return _plugin_security_enforcer


class PluginWebRegistry:
    """Registry for plugin web capabilities"""
    
    def __init__(self):
        self.web_plugins: Dict[str, PluginWebInterface] = {}
        self.active_routers: Dict[str, APIRouter] = {}
        self.static_mounts: Dict[str, Path] = {}
        self.security_configs: Dict[str, Dict[str, Any]] = {}
        self.plugin_metadata: Dict[str, Dict[str, Any]] = {}  # Store additional metadata
        logger.info("PluginWebRegistry initialized")
    
    def register_web_plugin(self, name: str, plugin, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Register a plugin's web capabilities
        
        Args:
            name: Plugin name/identifier
            plugin: Plugin instance implementing PluginWebInterface or having web methods
            metadata: Additional plugin metadata including schema path
        """
        logger.info(f"Registering web capabilities for plugin: {name}")
        
        self.web_plugins[name] = plugin
        
        # Store plugin metadata including schema path
        self.plugin_metadata[name] = metadata or {}
        
        # Try to find schema path if not provided
        if 'schema_path' not in self.plugin_metadata[name]:
            for plugin_dir in ["src/plugins", "plugins"]:
                for schema_name in ["schema.yaml", "mcp_schema.yaml", "schema.json", "mcp_schema.json"]:
                    schema_path = Path(plugin_dir) / name / schema_name
                    if schema_path.exists():
                        self.plugin_metadata[name]['schema_path'] = str(schema_path)
                        break
                if 'schema_path' in self.plugin_metadata[name]:
                    break
        
        # Register router if provided
        try:
            if hasattr(plugin, 'get_web_router'):
                router = plugin.get_web_router()
                if router:
                    self.active_routers[name] = router
                    logger.debug(f"Registered web router for plugin {name}")
        except Exception as e:
            logger.warning(f"Failed to get router for plugin {name}: {e}")
        
        # Register static assets if provided  
        try:
            if hasattr(plugin, 'get_static_assets'):
                static_path = plugin.get_static_assets()
                if static_path and static_path.exists():
                    self.static_mounts[name] = static_path
                    logger.debug(f"Registered static assets for plugin {name}: {static_path}")
        except Exception as e:
            logger.warning(f"Failed to get static assets for plugin {name}: {e}")
        
        # Store security configuration
        try:
            if hasattr(plugin, 'get_security_config'):
                security_config = plugin.get_security_config()
                self.security_configs[name] = security_config
                logger.debug(f"Registered security config for plugin {name}")
            else:
                self.security_configs[name] = {}
        except Exception as e:
            logger.warning(f"Failed to get security config for plugin {name}: {e}")
            self.security_configs[name] = {}
    
    def unregister_web_plugin(self, name: str) -> None:
        """Unregister a plugin's web capabilities
        
        Args:
            name: Plugin name/identifier
        """
        logger.info(f"Unregistering web capabilities for plugin: {name}")
        
        self.web_plugins.pop(name, None)
        self.active_routers.pop(name, None)
        self.static_mounts.pop(name, None)
        self.security_configs.pop(name, None)
    
    def get_all_panels(self) -> List[Dict[str, Any]]:
        """Get all registered UI panels
        
        Returns:
            List[Dict[str, Any]]: List of all panel configurations from all plugins
        """
        panels = []
        for name, plugin in self.web_plugins.items():
            try:
                if hasattr(plugin, 'get_panels'):
                    plugin_panels = plugin.get_panels()
                    for panel in plugin_panels:
                        # Ensure panel has required fields and add plugin metadata
                        panel_config = dict(panel)
                        panel_config["plugin_name"] = name
                        
                        # Validate required fields
                        if "id" not in panel_config:
                            logger.warning(f"Panel from plugin {name} missing 'id' field, skipping")
                            continue
                        if "title" not in panel_config:
                            panel_config["title"] = f"Plugin {name}"
                        if "url" not in panel_config:
                            logger.warning(f"Panel {panel_config['id']} from plugin {name} missing 'url' field, skipping")
                            continue
                        
                        # Set defaults for optional fields
                        panel_config.setdefault("position", "right")
                        panel_config.setdefault("width", "400px")
                        panel_config.setdefault("height", "300px")
                        
                        panels.append(panel_config)
                        
            except Exception as e:
                logger.warning(f"Failed to get panels for plugin {name}: {e}")
        
        return panels
    
    def get_security_config(self, plugin_name: str) -> Dict[str, Any]:
        """Get security configuration for a specific plugin
        
        Args:
            plugin_name: Name of the plugin
            
        Returns:
            Dict[str, Any]: Security configuration for the plugin
        """
        return self.security_configs.get(plugin_name, {})
    
    def apply_to_app(self, app: FastAPI, auth_config: Optional["AuthConfig"] = None) -> None:
        """Apply all registered web capabilities to FastAPI app with security enforcement.
        
        Args:
            app: FastAPI application instance
            auth_config: Optional auth configuration for security enforcement
        """
        logger.info("Applying plugin web capabilities to FastAPI app")
        
        # Initialize security enforcer if auth config provided
        security_enforcer = None
        if auth_config:
            security_enforcer = init_plugin_security(auth_config)
            logger.info("Plugin endpoint security enforcer initialized")
        
        # Create security dependency for plugin routes
        async def plugin_security_dependency(request: Request):
            """Dependency that enforces security on plugin endpoints."""
            if not security_enforcer or not auth_config or not auth_config.enabled:
                return None
            
            path = request.url.path
            method = request.method
            
            # Extract plugin name from path (e.g., "/plugins/todo/panel" -> "todo")
            plugin_name = "unknown"
            path_parts = path.strip("/").split("/")
            if len(path_parts) >= 2 and path_parts[0] == "plugins":
                plugin_name = path_parts[1]
            elif len(path_parts) >= 2 and path_parts[0] == "api" and path_parts[1] == "plugins":
                plugin_name = path_parts[2] if len(path_parts) > 2 else "unknown"
            
            # Get security policy
            policy = security_enforcer.get_plugin_policy(plugin_name, path, method)
            
            if not policy["requires_auth"]:
                security_enforcer.audit_access(
                    plugin_name, path, method, None, True, "No auth required"
                )
                return None
            
            # Try to get current user from cookie or header
            user = None
            user_id = None
            try:
                # Import auth utilities
                from agent_system.auth.security import decode_access_token
                from agent_system.auth.database import get_db
                
                token = None
                
                # 1. Try Bearer token from Authorization header
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header[7:]
                
                # 2. Try JWT token from cookie
                if not token:
                    token = request.cookies.get("access_token")
                
                # Validate token
                if token:
                    token_data = decode_access_token(token)
                    if token_data and token_data.username:
                        # Get database and user
                        db = get_db()
                        user_in_db = db.get_user_by_username(token_data.username)
                        if user_in_db and user_in_db.is_active:
                            user = user_in_db
                            user_id = user.username
                            #logger.debug(f"Plugin security: Authenticated user {user_id}")
                
            except Exception as e:
                logger.warning(f"Plugin security auth check failed: {e}")
            
            # Check if user is authenticated
            if user is None:
                # Check if anonymous access is allowed for this endpoint
                if auth_config.anonymous_access.enabled:
                    from agent_system.auth.enforcement import AnonymousUser
                    for allowed in auth_config.anonymous_access.allowed_endpoints:
                        if fnmatch.fnmatch(path, allowed.split(" ")[-1]):
                            security_enforcer.audit_access(
                                plugin_name, path, method, "anonymous", True,
                                "Anonymous access allowed by config"
                            )
                            return AnonymousUser(role=auth_config.anonymous_access.role)
                
                security_enforcer.audit_access(
                    plugin_name, path, method, None, False,
                    "Authentication required but no user found"
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required for this plugin endpoint",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            
            # Check role if required
            min_role = policy.get("min_role")
            if min_role:
                from agent_system.auth.enforcement import has_role
                if not has_role(user, min_role):
                    security_enforcer.audit_access(
                        plugin_name, path, method, user_id, False,
                        f"Insufficient role: required {min_role}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Insufficient permissions. Required role: {min_role}",
                    )
            
            security_enforcer.audit_access(
                plugin_name, path, method, user_id, True,
                f"Authenticated as {user_id}"
            )
            return user
        
        # Include plugin routers with security dependency
        for name, router in self.active_routers.items():
            try:
                # Add security dependency to all routes in this router
                if auth_config and auth_config.enabled:
                    # Apply security dependency to each route individually
                    for route in router.routes:
                        # Add dependency to existing route dependencies
                        if not hasattr(route, 'dependencies') or route.dependencies is None:
                            route.dependencies = []
                        route.dependencies.append(Depends(plugin_security_dependency))
                    app.include_router(router, tags=[f"plugin-{name}"])
                    logger.info(f"Mounted secured web router for plugin {name}")
                else:
                    app.include_router(router, tags=[f"plugin-{name}"])
                    logger.info(f"Mounted web router for plugin {name} (no auth)")
            except Exception as e:
                logger.error(f"Failed to mount router for plugin {name}: {e}")
        
        # Mount static assets (no auth required for static files)
        for name, path in self.static_mounts.items():
            try:
                mount_path = f"/plugins/{name}/static"
                app.mount(mount_path, 
                         StaticFiles(directory=str(path)), 
                         name=f"plugin-{name}-static")
                logger.info(f"Mounted static assets for plugin {name} at {mount_path}")
            except Exception as e:
                logger.error(f"Failed to mount static files for plugin {name}: {e}")
        
        # Add panel discovery endpoint
        @app.get("/api/plugins/panels")
        def get_plugin_panels():
            """Get all available plugin UI panels"""
            try:
                panels = self.get_all_panels()
                logger.debug(f"Returning {len(panels)} plugin panels")
                return {"panels": panels}
            except Exception as e:
                logger.error(f"Failed to get plugin panels: {e}")
                return {"panels": [], "error": str(e)}
        
        # Add security audit endpoints only if audit is enabled
        audit_enabled = (
            auth_config.endpoint_security.audit_enabled 
            if auth_config else False
        )
        
        if audit_enabled:
            # Add plugin security audit panel (admin only)
            @app.get("/api/security/audit")
            async def get_plugin_security_audit_panel(request: Request):
                """HTML panel for plugin security audit log (admin only)."""
                # Check admin access
                if auth_config and auth_config.enabled:
                    from agent_system.auth.dependencies import require_admin
                    from agent_system.auth.database import get_db
                    
                    try:
                        # Use the require_admin dependency properly
                        db = get_db()
                        from fastapi.security import HTTPBearer
                        bearer = HTTPBearer(auto_error=False)
                        credentials = await bearer(request)
                        
                        from agent_system.auth.dependencies import get_current_user, get_current_active_user
                        user = await get_current_user(
                            request=request,
                            credentials=credentials,
                            x_api_key=request.headers.get("x-api-key"),
                            db=db
                        )
                        # Call require_admin to check admin role
                        await require_admin(await get_current_active_user(user))
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.error(f"Auth error in security audit: {e}")
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Authentication required"
                        )
                
                from fastapi.templating import Jinja2Templates
                from pathlib import Path
                
                templates_dir = Path(__file__).parent.parent / "api" / "templates"
                templates = Jinja2Templates(directory=str(templates_dir))
                
                return templates.TemplateResponse(
                    "security_audit.html",
                    {"request": request}
                )
        
            # Add security audit endpoint (admin only) - now uses global middleware
            @app.get("/api/plugins/security/audit")
            async def get_security_audit(
                request: Request,
                category: Optional[str] = None,
                limit: int = 100,
                status_filter: Optional[str] = None
            ):
                """Get security audit log (admin only).
                
                Args:
                    category: Filter by category (plugin, api, agent, auth, mcp, debug, health, other)
                    limit: Maximum entries to return
                    status_filter: Filter by status code range (2xx, 3xx, 4xx, 5xx, all)
                """
                # Check admin access
                if auth_config and auth_config.enabled:
                    from agent_system.auth.dependencies import require_admin, get_current_user, get_current_active_user
                    from agent_system.auth.database import get_db
                    from fastapi.security import HTTPBearer
                    
                    try:
                        db = get_db()
                        bearer = HTTPBearer(auto_error=False)
                        credentials = await bearer(request)
                        
                        user = await get_current_user(
                            request=request,
                            credentials=credentials,
                            x_api_key=request.headers.get("x-api-key"),
                            db=db
                        )
                        await require_admin(await get_current_active_user(user))
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.error(f"Auth error in security audit data: {e}")
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Authentication required"
                        )
                
                # Get from new middleware
                from agent_system.auth.middleware import get_security_audit_middleware
                audit_middleware = get_security_audit_middleware()
                
                if audit_middleware:
                    audit_log = audit_middleware.get_audit_log(category, limit, status_filter)
                else:
                    # Fallback to plugin enforcer (legacy). No category filter
                    # here: its first parameter is a PLUGIN-NAME filter, and
                    # passing a category ("api", "auth", ...) silently matched
                    # nothing. Unfiltered is honest; the legacy entries carry
                    # no category field to filter on.
                    enforcer = get_plugin_security_enforcer()
                    audit_log = enforcer.get_audit_log(limit=limit)
                
                # Get available categories
                categories = ["plugin", "api", "agent", "auth", "mcp", "debug", "health", "other"]
                
                return {
                    "audit_log": audit_log,
                    "categories": categories,
                    "total_plugins": len(self.web_plugins),
                    "plugins_with_routers": list(self.active_routers.keys()),
                }
        
        # Add plugin security status endpoint
        @app.get("/api/plugins/security/status")
        async def get_plugin_security_status(request: Request):
            """Get current plugin security configuration status."""
            if auth_config and auth_config.enabled:
                from agent_system.auth.dependencies import get_current_user, get_current_active_user
                from agent_system.auth.database import get_db
                from fastapi.security import HTTPBearer
                
                try:
                    db = get_db()
                    bearer = HTTPBearer(auto_error=False)
                    credentials = await bearer(request)
                    
                    user = await get_current_user(
                        request=request,
                        credentials=credentials,
                        x_api_key=request.headers.get("x-api-key"),
                        db=db
                    )
                    await get_current_active_user(user)
                except HTTPException:
                    raise
                except Exception as e:
                    logger.error(f"Auth error in security status: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Authentication required"
                    )
            
            enforcer = get_plugin_security_enforcer()
            
            # Build status for each plugin
            plugin_status = {}
            for name in self.active_routers.keys():
                policy = enforcer.get_plugin_policy(name, f"/plugins/{name}/", "GET")
                plugin_status[name] = {
                    "has_router": True,
                    "has_static": name in self.static_mounts,
                    "security_policy": policy,
                    "plugin_security_config": self.security_configs.get(name, {}),
                }
            
            return {
                "auth_enabled": auth_config.enabled if auth_config else False,
                "plugin_security_enabled": (
                    auth_config.enabled and 
                    auth_config.plugin_security.default_policy == "require_auth"
                ) if auth_config else False,
                "default_policy": auth_config.plugin_security.default_policy if auth_config else "allow_anonymous",
                "default_min_role": auth_config.plugin_security.default_min_role if auth_config else None,
                "audit_enabled": auth_config.endpoint_security.audit_enabled if auth_config else False,
                "plugins": plugin_status,
            }
        
        logger.info(f"Applied web capabilities for {len(self.web_plugins)} plugins")


# Global plugin web registry instance
plugin_web_registry = PluginWebRegistry()

def get_web_plugin_registry() -> Optional[Dict[str, Any]]:
    """Get the web plugin registry for API access"""
    global plugin_web_registry
    if not plugin_web_registry:
        return None
    
    # Return plugin information with schema paths
    registry_data = {}
    for name, plugin in plugin_web_registry.web_plugins.items():
        metadata = plugin_web_registry.plugin_metadata.get(name, {})
        registry_data[name] = {
            'name': getattr(plugin, 'name', name),
            'description': getattr(plugin, 'description', ''),
            'schema_path': metadata.get('schema_path'),
            **metadata  # Include any additional metadata
        }
    
    return registry_data