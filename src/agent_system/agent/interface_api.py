from __future__ import annotations

import json
import asyncio
import logging
import os
import time
from ..utils.id import short_id
import yaml
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Any

import uvicorn
from fastapi import FastAPI, Request, Query, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.datastructures import UploadFile  # Use starlette's UploadFile for isinstance checks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
# Response is not needed here; FastAPI/Starlette response classes are imported where required

from ..config.settings import load_settings
from ..config.models import AgentConfig
from api.endpoints import router as api_router
from ..mcp.base import MCPRegistry
from ..servers.bootstrap import bootstrap_servers
from ..utils.logging import setup_logging
from ..llm.models import ChatMessage
from ..mcp.status import (
    status_bus,
    StatusEvent,
    publish_status,
    get_status_metrics,
)
from ..mcp.integration import initialize_mcp, shutdown_mcp

# Import services
from ..services import ConfigService, MCPService, ToolService, AgentService
from ..services.session_manager import SessionManager


# Global registry for MCP endpoints access
_app_registry: Optional[MCPRegistry] = None
_app_config: Optional[AgentConfig] = None
_mcp_integration = None
_mcp_server_handler: Optional[Any] = None  # MCPServerHandler instance for server mode

# Global services (initialized in build_app)
_config_service: Optional[ConfigService] = None
_mcp_service: Optional[MCPService] = None
_tool_service: Optional[ToolService] = None
_agent_service: Optional[AgentService] = None
_session_manager: Optional[SessionManager] = None
_session_service: Optional[Any] = None  # SessionService, imported at runtime to avoid circular import


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown events."""
    logger = logging.getLogger(__name__)
    logger.info("FastAPI application starting up")

    # Startup logic here if needed
    yield

    # Shutdown logic
    logger.info("FastAPI application shutting down gracefully")
    try:
        # Clean up any resources here
        # The status_bus and other components will clean themselves up
        pass
    except Exception as e:
        logger.error("Error during shutdown cleanup: %s", e)
    finally:
        logger.info("FastAPI application shutdown complete")


# Module level templates and static path setup
templates = Jinja2Templates(directory=str(Path(__file__).parents[3] / "templates"))
static_path = Path(__file__).parents[3] / "static"

# Global application state
_app_start_time = None


def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Build and configure the FastAPI application."""

    # Initialize ConfigService and load configuration
    if not config_path:
        cfg_path = str(Path(__file__).parents[3] / "config" / "config.yaml")
    else:
        cfg_path = config_path
    
    # Create ConfigService
    global _config_service
    _config_service = ConfigService()
    config = _config_service.load_config(config_path=cfg_path)
    
    # Setup logging via ConfigService
    _config_service.setup_logging()
    
    # Log configuration status
    logger = logging.getLogger(__name__)
    logger.info(f"Loading configuration from: {cfg_path}")
    if config.llm_system:
        logger.debug(f"LLM system loaded with {len(config.llm_system.profiles)} profiles")
    else:
        logger.warning("No llm_system configuration loaded")
    
    # Count configured servers
    plugin_count = len(config.plugins.servers) if config.plugins else 0
    mcp_remote_count = len(config.external_servers.remote_servers) if config.external_servers else 0
    if plugin_count or mcp_remote_count:
        logger.debug(f"MCP system loaded with {plugin_count} plugins and {mcp_remote_count} remote servers")
    else:
        logger.warning("No plugins or mcp_servers configuration loaded")

    # Initialize MCP integration
    async def _init_mcp_for_app(app: FastAPI):
        global _mcp_integration, _mcp_service, _tool_service, _agent_service, _session_manager, _session_service
        logger = logging.getLogger(__name__)
        logger.info("Starting MCP integration initialization...")
        try:
            mcp_integration = await initialize_mcp(config, app)
            _mcp_integration = mcp_integration
            
            # Initialize services
            _mcp_service = MCPService(mcp_integration, config)
            _tool_service = ToolService(mcp_integration, config)
            
            # Initialize SessionManager (persistent session storage)
            from pathlib import Path
            storage_path = Path(__file__).parents[3] / "data" / "sessions"
            _session_manager = SessionManager(storage_path=str(storage_path))
            logger.info(f"SessionManager initialized with storage_path={storage_path}")
            
            # Initialize SessionService (session loading/saving logic)
            from agent_system.services.session_service import SessionService
            _session_service = SessionService(_session_manager)
            logger.info("SessionService initialized")
            
            # Inject session manager into session endpoints NOW (after initialization)
            from api.session_endpoints import set_session_manager
            set_session_manager(_session_manager)
            logger.info("SessionManager injected into session endpoints")
            
            # Agent will be initialized later when needed
            # (requires agent instance from bootstrap_servers)
            
            # Make integration accessible to mcp module
            from ..mcp import integration as _mcp_mod
            _mcp_mod.mcp_integration = mcp_integration
            
            logger.info("MCP integration and services initialized for API")

            # Apply plugin web capabilities
            from ..plugins.web_adapter import plugin_web_registry
            plugin_web_registry.apply_to_app(app)
            logger.info("Plugin web capabilities applied to app")

        except Exception as e:
            logger.exception("Failed to initialize MCP integration for API: %s", e)

    # FastAPI lifespan management
    @asynccontextmanager
    async def custom_lifespan(app: FastAPI):
        # Startup
        global _app_start_time
        _app_start_time = time.time()

        logger = logging.getLogger(__name__)
        logger.info("Lifespan startup: Initializing MCP integration...")
        await _init_mcp_for_app(app)
        logger.info("MCP integration initialized during lifespan startup")
        yield
        # Shutdown
        try:
            await shutdown_mcp()
            logger.info("MCP integration shut down during lifespan")
        except Exception as e:
            logger.exception("Error shutting down MCP integration during lifespan: %s", e)

    # Create FastAPI app
    app = FastAPI(title="Agent System (MCP)", lifespan=custom_lifespan)
    
    # Helper function for optional user authentication
    async def _get_current_user_optional(request: Request) -> Optional[Any]:
        """Get current user if authenticated, None otherwise.
        
        Checks multiple auth methods in order (via get_current_user dependency):
        1. Bearer token in Authorization header
        2. JWT token in access_token cookie (for EventSource/browser)
        3. X-API-Key header (for programmatic access)
        """
        try:
            from ..auth.dependencies import get_current_user as get_user_dep
            from ..auth.database import get_db
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

    # Mount static files
    if static_path.exists():
        static_files = StaticFiles(directory=str(static_path))
        app.mount("/static", static_files, name="static")

        # Add no-cache headers for static files when cache is disabled
        if config.network.disable_cache:
            @app.middleware("http")
            async def _no_cache_static_middleware(request: Request, call_next: Callable):
                if request.url.path.startswith("/static"):
                    response = await call_next(request)
                    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    response.headers["Pragma"] = "no-cache"
                    response.headers["Expires"] = "0"
                    return response
                return await call_next(request)

    # Initialize logging with role-specific logfile
    def _role_logfile(base: str, role: str) -> str:
        p = Path(base)
        stem = p.stem or "agent"
        suffix = "".join(p.suffixes) or ".log"
        return str(p.with_name(f"{stem}-{role}{suffix}"))

    # Use file_api config or generate from default file
    if not config.logging.file_api:
        default_log = _role_logfile(config.logging.file or "logs/agent.log", "api")
        logging.getLogger(__name__).warning(
            "No file_api configured in logging settings, falling back to default: %s", default_log
        )
        log_path = default_log
    else:
        log_path = config.logging.file_api

    # Check for environment variable override
    env_level = os.getenv("AGENT_LOG_LEVEL")
    level_to_use = env_level if env_level else config.logging.level
    if env_level:
        logging.getLogger(__name__).info("Overriding log level from environment: %s", env_level)
        config.logging.level = env_level

    log_file = setup_logging(config.logging.enabled, level_to_use, log_path)
    if log_file:
        logging.getLogger(__name__).info("Logging initialized, file=%s", log_file)

    # Disable SSL verification if configured
    if not config.network.ssl_verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
        logging.getLogger(__name__).info("SSL verification disabled - set environment variables for global SSL bypass")

    # Bootstrap MCP servers and plugin registry
    registry = MCPRegistry()
    bootstrap_servers(config, registry)

    # Get entry agent from config
    entry_name = config.default_agent or 'agent'
    logging.getLogger(__name__).debug(f"Using entry agent: '{entry_name}'")

    # Reuse existing agent from registry if available
    selected_agent = None
    try:
        if entry_name in registry.list():
            candidate = registry.get(entry_name)
            from ..servers.agent.server import Agent as _Agent
            if isinstance(candidate, _Agent):
                selected_agent = candidate
                # Apply server-level configuration overrides
                try:
                    # Check if this is a config-based agent first, then fallback to MCP server config
                    agent_cfg = _config_service.get_agent_config(entry_name, config)
                    if agent_cfg:
                        server_cfg = agent_cfg
                    else:
                        # Not a config-based agent, try MCP server config
                        server_mcp = _config_service.get_mcp_server_config(entry_name, config)
                        server_cfg = server_mcp.model_dump() if server_mcp and hasattr(server_mcp, 'model_dump') else {}

                    overrides = server_cfg.get('agent_config', {}) if isinstance(server_cfg, dict) else {}
                    if isinstance(overrides, dict) and overrides:
                        # Check if we need to update tools config
                        tools_cfg = overrides.get('tools', {})
                        needs_copy = (
                            ('allowed' in tools_cfg or 'blocked' in tools_cfg) or 
                            'max_steps' in server_cfg
                        )
                        if needs_copy:
                            updates = {}
                            # Update tools.allowed if specified and agent doesn't have it set
                            if 'allowed' in tools_cfg:
                                agent_tools = selected_agent.agent_config.tools if selected_agent.agent_config.tools else None
                                if agent_tools is None or not agent_tools.allowed:
                                    try:
                                        from ..config.models import ToolConfig
                                        new_tools = ToolConfig(
                                            allowed=tools_cfg.get('allowed', []),
                                            blocked=agent_tools.blocked if agent_tools else []
                                        )
                                        updates['tools'] = new_tools
                                    except Exception as e:
                                        logger.debug(f"Failed to apply tools.allowed override: {e}")
                            # Update tools.blocked if specified and agent doesn't have it set
                            if 'blocked' in tools_cfg and 'tools' not in updates:
                                agent_tools = selected_agent.agent_config.tools if selected_agent.agent_config.tools else None
                                if agent_tools is None or not agent_tools.blocked:
                                    try:
                                        from ..config.models import ToolConfig
                                        new_tools = ToolConfig(
                                            allowed=agent_tools.allowed if agent_tools else [],
                                            blocked=tools_cfg.get('blocked', [])
                                        )
                                        updates['tools'] = new_tools
                                    except Exception as e:
                                        logger.debug(f"Failed to apply tools.blocked override: {e}")
                            if 'max_steps' in server_cfg and isinstance(server_cfg.get('max_steps'), int):
                                try:
                                    updates['max_steps'] = int(server_cfg.get('max_steps'))
                                except Exception as e:
                                    logger.debug(f"Failed to apply max_steps override: {e}")
                            if updates:
                                selected_agent.agent_config = selected_agent.agent_config.model_copy(update=updates)
                            logging.getLogger(__name__).debug("Applied entry agent server overrides for %s", entry_name)
                except Exception as e:
                    logging.getLogger(__name__).debug("Failed to apply entry agent overrides for %s: %s", entry_name, e)
    except Exception as e:
        logger.debug(f"Failed to get entry agent from registry: {e}")
        selected_agent = None

    # Create new agent if not found in registry
    if selected_agent is None:
        from ..servers.agent.server import Agent as CoreAgent
        try:
            # Check if this is a config-based agent first, then fallback to MCP server config
            agent_cfg = _config_service.get_agent_config(entry_name, config)
            if agent_cfg:
                server_cfg = agent_cfg
            else:
                # Not a config-based agent, try MCP server config
                server_mcp = _config_service.get_mcp_server_config(entry_name, config)
                server_cfg = server_mcp.model_dump() if server_mcp and hasattr(server_mcp, 'model_dump') else {}
            
            if not server_cfg:
                logging.getLogger(__name__).warning(
                    "No server configuration found for agent '%s', using defaults", entry_name
                )
        except Exception as e:
            logger.debug(f"Failed to load server config: {e}")
        
        # Build MCPConfig for agent - use ConfigService
        from agent_system.config.models import MCPConfig, AgentConfig, ToolConfig
        mcp_cfg = _config_service.get_default_mcp_config(config)
        
        if not mcp_cfg:
            # Create default MCPConfig if not found
            logging.getLogger(__name__).warning(
                "No default_config found in plugins configuration, creating default MCPConfig with llm_profile='normal'"
            )
            tool_cfg = ToolConfig()
            agent_cfg = AgentConfig(llm_profile="normal", tools=tool_cfg)
            mcp_cfg = MCPConfig(type="agent", enabled=True, agent_config=agent_cfg)
        
        selected_agent = CoreAgent(entry_name, config, mcp_cfg, registry)
        registry.register(entry_name, selected_agent)
    else:
        # Bind reused agent to current registry
        try:
            selected_agent.registry = registry
        except Exception as e:
            logger.warning(f"Failed to bind registry to agent: {e}", exc_info=True)

    agent = selected_agent

    # Inject agent into session endpoints for message formatting
    from api.session_endpoints import set_default_agent
    set_default_agent(agent)
    logger.info("Default agent injected into session endpoints for formatting")

    # Store registry and config globally
    global _app_registry, _app_config, _mcp_server_handler
    _app_registry = registry
    _app_config = config

    # Initialize MCP server mode if enabled (Epic 0037)
    server_mode_enabled = config.server_mode and config.server_mode.enabled
    if server_mode_enabled:
        logger.info("Initializing MCP server handler for server mode")
        try:
            from ..mcp.server_handler import MCPServerHandler
            _mcp_server_handler = MCPServerHandler(config, registry)
            logger.info("MCP server handler initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize MCP server handler: %s", e, exc_info=True)
            _mcp_server_handler = None
    else:
        logger.debug("MCP server mode is disabled")
        _mcp_server_handler = None

    # Include API router
    app.include_router(api_router)

    # Initialize authentication system if enabled
    if config.auth and config.auth.enabled:
        logger.info("Multi-user authentication enabled, initializing auth system...")
        
        # Setup auth database and configuration
        from agent_system.auth.database import setup_database
        from agent_system.auth.security import set_jwt_config
        from agent_system.auth.middleware import configure_cors, configure_security_middleware
        from agent_system.auth.models import UserCreate, UserRole
        from pathlib import Path as AuthPath
        
        # Configure JWT settings
        set_jwt_config(
            secret_key=config.auth.secret_key,
            algorithm=config.auth.algorithm,
            expire_minutes=config.auth.access_token_expire_minutes
        )
        
        # Setup database
        db_path = AuthPath(config.auth.database_path)
        db = setup_database(db_path)
        logger.info(f"User database initialized at: {db_path}")
        
        # Create default admin user if no users exist
        users = db.list_users(limit=1)
        if not users:
            logger.info("No users found, creating default admin user...")
            try:
                default_admin = UserCreate(
                    username=config.auth.default_admin_username,
                    email=config.auth.default_admin_email,
                    password=config.auth.default_admin_password,
                    full_name="Default Administrator",
                    role=UserRole.ADMIN,
                    is_active=True
                )
                db.create_user(default_admin)
                logger.warning(
                    f"Default admin user created: {config.auth.default_admin_username} / "
                    f"{config.auth.default_admin_password} - CHANGE PASSWORD IMMEDIATELY!"
                )
            except Exception as e:
                logger.error(f"Failed to create default admin user: {e}")
        
        # Configure CORS if enabled
        if config.auth.cors_enabled:
            configure_cors(
                app,
                allow_origins=config.auth.cors_origins,
                allow_credentials=config.auth.cors_credentials,
                allow_methods=config.auth.cors_methods,
                allow_headers=config.auth.cors_headers,
            )
        
        # Configure security middleware
        configure_security_middleware(
            app,
            rate_limit_enabled=config.auth.rate_limit_enabled,
            requests_per_minute=config.auth.requests_per_minute,
            security_headers_enabled=config.auth.security_headers_enabled,
            trusted_hosts=config.auth.trusted_hosts,
        )
        
        # Include auth and admin routers
        from api.auth_endpoints import router as auth_router
        from api.admin_endpoints import router as admin_router
        from api.menu_endpoints import menu_router
        from api.session_endpoints import session_router
        
        app.include_router(auth_router)
        app.include_router(admin_router)
        app.include_router(menu_router)
        app.include_router(session_router)
        
        # Note: set_session_manager() is called later in async lifespan startup
        # after SessionManager is actually initialized
        
        logger.info("Authentication system initialized successfully")
    else:
        logger.info("Multi-user authentication is disabled")

    # Health check endpoint
    @app.get("/health")
    def health():
        global _app_start_time

        uptime_seconds = time.time() - _app_start_time if _app_start_time else 0

        agent_config = {}
        try:
            agent_config_path = Path(__file__).parents[3] / "config" / "config.yaml"
            with open(agent_config_path, 'r', encoding='utf-8') as f:
                agent_config = yaml.safe_load(f) or {}
        except Exception as e:
            logger.debug(f"Failed to load config for health check: {e}")

        return {
            "status": "ok",
            "version": agent_config.get("version", "unknown"),
            "name": agent_config.get("name", "AgentSystem"),
            "uptime_seconds": round(uptime_seconds, 2),
            "timestamp": datetime.now().isoformat()
        }

    def _get_agent_with_overrides(agent_name: Optional[str] = None, llm_profile: Optional[str] = None):
        """Get agent instance with optional overrides.
        
        Args:
            agent_name: Name of agent to use (None = use default global agent)
            llm_profile: LLM profile to use (None = use agent's configured profile)
            
        Returns:
            Tuple of (agent_instance, llm_override, llm_profile_info)
            - agent_instance: The selected agent
            - llm_override: LLM client to pass to run_events (None if using agent's default)
            - llm_profile_info: Profile info string for status display (None if no override)
        """
        selected_agent = agent
        llm_override = None
        llm_profile_info = None
        
        # Override agent if specified
        if agent_name and agent_name != selected_agent.name:
            try:
                selected_agent = _app_registry.get(agent_name)  # type: ignore[attr-defined]
                from ..servers.agent.server import Agent as _Agent
                if not isinstance(selected_agent, _Agent):
                    raise HTTPException(status_code=400, detail=f"'{agent_name}' is not an agent")
            except KeyError:
                raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to get agent: {str(e)}")
        
        # Create LLM override if profile specified
        if llm_profile and config.llm_system and config.llm_system.profiles:
            if llm_profile not in config.llm_system.profiles:
                raise HTTPException(status_code=400, detail=f"LLM profile '{llm_profile}' not found")
            
            try:
                # Resolve profile to model config using the factory
                from ..llm.factory import resolve_llm_config_for_agent
                from ..config.models import AgentConfig
                
                # Create temporary agent config with override profile
                temp_agent_config = AgentConfig(llm_profile=llm_profile)
                llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
                
                # Create new LLM with resolved config
                from ..llm.clients import make_llm
                llm_override = make_llm(**llm_kwargs)
                
                # Build profile info string for status display (matching agent's format)
                model = llm_kwargs.get('model', 'unknown')
                provider = llm_kwargs.get('provider', 'unknown')
                llm_profile_info = f"{llm_profile}:{provider}/{model}"
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to apply LLM profile: {str(e)}")
        
        return selected_agent, llm_override, llm_profile_info

    @app.get("/config")
    def get_config():
        return config.model_dump()

    @app.get("/agents")
    def list_agents():
        """List registered agent-like servers that are publicly visible (UI dropdown).
        
        Returns agents with _mcp_public=True OR agents without _mcp_public attribute (backward compat).
        Agents with visibility='tool' or 'private' (_mcp_public=False) are excluded.
        """
        agents = []
        try:
            for name in _app_registry.list():  # type: ignore[attr-defined]
                try:
                    srv = _app_registry.get(name)  # type: ignore[attr-defined]
                    from ..servers.agent.server import Agent as _Agent
                    if isinstance(srv, _Agent):
                        # Filter by _mcp_public flag (visibility control)
                        # Default to True if attribute doesn't exist (backward compatibility with plugin agents)
                        if hasattr(srv, '_mcp_public'):
                            if not srv._mcp_public:
                                logger.debug(f"Skipping agent '{name}' in UI list (_mcp_public=False)")
                                continue
                        # else: No _mcp_public attribute → show in UI (backward compat)
                        agents.append(name)
                except Exception as e:
                    logger.debug(f"Failed to check agent {name}: {e}")
                    continue
        except Exception as e:
            logger.debug(f"Failed to list agents: {e}")
        return {"agents": agents}

    @app.get("/llm/profiles")
    def list_llm_profiles():
        """List available LLM profiles with their descriptions."""
        profiles = []
        default_profile = None
        try:
            if config.llm_system and config.llm_system.profiles:
                for profile_name, profile_config in config.llm_system.profiles.items():
                    profiles.append({
                        "name": profile_name,
                        "model_ref": profile_config.model_ref,
                        "description": profile_config.description or profile_name,
                        "max_steps": profile_config.max_steps
                    })
                default_profile = config.llm_system.default_profile
        except Exception as e:
            logger.debug(f"Failed to list LLM profiles: {e}")
        return {
            "profiles": profiles,
            "default": default_profile or "normal"
        }

    @app.get("/agents/{agent_name}/allowed-tools")
    async def get_agent_allowed_tools(agent_name: str):
        """Return the effective allowed tools list for an agent after pattern filtering."""
        try:
            srv = _app_registry.get(agent_name)  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"Failed to get agent {agent_name}: {e}")
            return {"error": "agent not found", "agent": agent_name}
        from ..servers.agent.server import Agent as _Agent
        if not isinstance(srv, _Agent):
            return {"error": "not an agent", "agent": agent_name}
        # Use unified discovery so API shows same filtered set as runtime
        try:
            available = await srv.list_allowed_tool_servers()
            patterns = getattr(srv.agent_config, 'allowed_tools', None)
            return {"agent": agent_name, "patterns": patterns or [], "available": sorted(available), "effective": sorted(available)}
        except Exception as e:
            return {"agent": agent_name, "error": str(e)}

    @app.get("/agents/{agent_name}/allowed-tools/debug")
    async def get_agent_allowed_tools_debug(agent_name: str):
        """Return detailed pattern match diagnostics for an agent's allowed tools.

        Provides for each available tool server which allow pattern(s) matched.
        If no allow list configured, returns an informational note.
        """
        try:
            srv = _app_registry.get(agent_name)  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"Failed to get agent {agent_name}: {e}")
            return {"error": "agent not found", "agent": agent_name}
        from ..servers.agent.server import Agent as _Agent
        if not isinstance(srv, _Agent):
            return {"error": "not an agent", "agent": agent_name}
        try:
            patterns = getattr(srv.agent_config, 'allowed_tools', None)
            available = await srv.list_allowed_tool_servers() if patterns else await srv.list_allowed_tool_servers()
            diagnostics = []
            if patterns:
                for tool in available:
                    matched_by = []
                    for pat in patterns:
                        if srv._is_tool_allowed(tool, [pat]):  # type: ignore[attr-defined]
                            matched_by.append(pat)
                    diagnostics.append({"tool": tool, "matched_patterns": matched_by})
            else:
                diagnostics = [{"tool": t, "matched_patterns": ["<implicit:all>"]} for t in available]
            return {"agent": agent_name, "patterns": patterns or [], "diagnostics": diagnostics}
        except Exception as e:
            return {"agent": agent_name, "error": str(e)}

    @app.get("/agents/{agent_name}/system-prompt")
    async def get_agent_system_prompt(agent_name: str):
        """Return the currently rendered system & tools prompt for the agent.

        Renders on demand using the same logic as execution, including:
          - allowed tool filtering
          - max_steps (minus one for planning budget inside prompt)
          - datetime context (if enabled)
        """
        try:
            srv = _app_registry.get(agent_name)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(f"Failed to get agent {agent_name}: {e}", exc_info=True)
            return {"error": "agent not found", "agent": agent_name}
        from ..servers.agent.server import Agent as _Agent
        if not isinstance(srv, _Agent):
            return {"error": "not an agent", "agent": agent_name}
        try:
            return await srv.get_current_system_prompt()
        except Exception as e:  # pragma: no cover - defensive
            return {"error": str(e), "agent": agent_name}

    @app.post("/run")
    async def run(
        request: Request, 
        traceparent: Optional[str] = Header(default=None),
        session_id: Optional[str] = Query(default=None),
        agent_name: Optional[str] = Query(default=None),
        llm_profile: Optional[str] = Query(default=None)
    ):
        """Run agent with optional multimodal input (text + images).

        This handler accepts either:
        - multipart/form-data with fields 'task' and repeated 'files' entries, or
        - application/json with {"task": "..."}, or
        - query param ?task=... (fallback used by some clients)

        Query parameters:
        - session_id: Optional session ID for conversation continuity
        - agent_name: Optional agent to use instead of default
        - llm_profile: Optional LLM profile override (turbo, normal, think, etc.)
        """
        logger = logging.getLogger(__name__)
        request_id = short_id()

        # Try to parse task and files from the request in a flexible way
        task = None
        upload_files: list[UploadFile] = []
        content_type = request.headers.get('content-type', '')
        logger.debug("/run content-type: %s", content_type)

        # JSON body: {"task": "...", "session_id": "...", "agent_name": "...", "llm_profile": "..."}
        if content_type.startswith('application/json'):
            body = await request.json()
            logger.debug("/run parsed JSON body: %s", body)
            if isinstance(body, dict):
                task = body.get('task')
                # Allow overrides from JSON body
                if not session_id and 'session_id' in body:
                    session_id = body.get('session_id')
                if not agent_name and 'agent_name' in body:
                    agent_name = body.get('agent_name')
                if not llm_profile and 'llm_profile' in body:
                    llm_profile = body.get('llm_profile')

        # multipart/form-data: parse form and files
        elif content_type.startswith('multipart/form-data'):
            form = await request.form()
            try:
                logger.debug("/run parsed form keys: %s", list(form.keys()))
            except Exception as e:
                logger.debug("/run parsed form (unable to list keys): %s", e)
            # Extract task field
            if 'task' in form:
                task = form['task']
            # Allow overrides from form data
            if not session_id and 'session_id' in form:
                session_id = form.get('session_id')
            if not agent_name and 'agent_name' in form:
                agent_name = form.get('agent_name')
            if not llm_profile and 'llm_profile' in form:
                llm_profile = form.get('llm_profile')
            # Collect UploadFile instances - use getlist() for repeated fields
            if hasattr(form, 'getlist'):
                files_list = form.getlist('files')
            else:
                files_list = [form.get('files')] if 'files' in form else []
            
            for file_val in files_list:
                if file_val and isinstance(file_val, UploadFile):
                    upload_files.append(file_val)
            logger.debug("/run collected upload_files count=%d", len(upload_files))

        # Fallback: query param
        if not task:
            query_task = request.query_params.get('task')
            if query_task:
                task = query_task

        logger.info("/run invoked, task=%s, files=%d, request_id=%s, session_id=%s, agent=%s, llm_profile=%s", 
                   task, len(upload_files), request_id, session_id, agent_name or "default", llm_profile or "default")

        # Get current user (optional authentication)
        current_user = await _get_current_user_optional(request)
        
        # Determine user_id for session management
        user_id = current_user.username if current_user else "anonymous"

        # Get agent with LLM override
        selected_agent, llm_override, llm_profile_info = _get_agent_with_overrides(agent_name, llm_profile)
        
        # Load existing session if session_id provided
        session_exists = False
        if session_id and _session_service:
            session_exists, msg_count = await _session_service.load_and_restore_session(
                selected_agent, user_id, session_id
            )

        from agent_system.servers.agent.result_utils import collect_final_result

        # If no uploaded files, treat as text-only
        if not upload_files:
            if not task:
                raise HTTPException(status_code=400, detail="Missing 'task' in request")
            # Pass LLM override to collect_final_result
            result = await collect_final_result(selected_agent, task, request_id=request_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info)
            
            # Format summary from Markdown to HTML for web display
            if result.get("summary") and selected_agent._hook_manager:
                try:
                    formatted_summary, _ = await selected_agent._hook_manager.execute_format_output_hooks(
                        output=result["summary"],
                        request_id=request_id,
                        session_id=session_id or "unknown",
                        output_format='html'
                    )
                    result["summary"] = formatted_summary
                except Exception as e:
                    logger.warning(f"Failed to format summary to HTML: {e}")
                    # Keep original markdown on error
            
            return result

        # Process uploaded files for multimodal input
        from ..llm.capabilities import get_model_capabilities
        from ..utils.image_processor import create_multimodal_message, ImageProcessingError
        import tempfile
        from pathlib import Path

        # Validate model supports images
        model_name = selected_agent.llm.model if hasattr(selected_agent.llm, 'model') else None
        if model_name:
            caps = get_model_capabilities(model_name)
            if not caps.image_input:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model {model_name} does not support image input"
                )

        # Save uploaded files to temp directory
        temp_files = []
        image_paths = []
        try:
            temp_dir = Path(tempfile.mkdtemp())

            for upload_file in upload_files:
                temp_path = temp_dir / upload_file.filename
                with open(temp_path, 'wb') as f:
                    content = await upload_file.read()
                    f.write(content)
                temp_files.append(temp_path)
                image_paths.append(str(temp_path))
                logger.debug("Saved uploaded file %s (%d bytes) -> %s", upload_file.filename, len(content), temp_path)

            # Create multimodal message
            try:
                multimodal_msg = create_multimodal_message(task, image_paths)
                logger.info("Created multimodal message with %d image(s)", len(image_paths))
            except ImageProcessingError as e:
                logger.exception("Image processing failed while creating multimodal message: %s", e)
                raise HTTPException(status_code=400, detail=str(e))

            # Stream events for multimodal message (same as /events endpoint)
            async def event_stream():
                # Initial keep-alive line
                yield ":ok\n\n"
                
                # Track if this is a new session
                was_new_session = (session_id is None) or (not session_exists)
                actual_session_id = session_id
                
                try:
                    async for event in selected_agent.run_events(multimodal_msg, request_id=request_id, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                        event_type = event.get("type")
                        
                        # Capture session_id from start event (created on first call)
                        if event_type == "start" and event.get("session_id"):
                            old_session_id = actual_session_id
                            actual_session_id = event["session_id"]
                            logger.debug(f"[SESSION_SAVE] Session ID captured from start event: {old_session_id} -> {actual_session_id}")
                        
                        # Ensure proper JSON serialization
                        if hasattr(event, 'to_dict'):
                            payload = event.to_dict()
                        else:
                            payload = event
                        
                        yield f"event: {event_type}\n"
                        yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                        
                        if event_type == "end":
                            break
                            
                except Exception as e:
                    logger.exception("Error streaming multimodal events: %s", e)
                    error_event = {"type": "error", "message": str(e)}
                    yield "event: error\n"
                    yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                finally:
                    # Save session after completion
                    if _session_service and actual_session_id:
                        agent_name_used = agent_name or "default"
                        llm_profile_used = llm_profile or "normal"
                        await _session_service.save_session(
                            selected_agent,
                            user_id,
                            actual_session_id,
                            agent_name_used,
                            llm_profile_used,
                            was_new_session
                        )
                    
                    # Cleanup temp files after streaming completes
                    for temp_file in temp_files:
                        try:
                            temp_file.unlink()
                        except Exception as e:
                            logger.warning("Failed to delete temp file %s: %s", temp_file, e)
                    if temp_files:
                        try:
                            temp_dir.rmdir()
                        except Exception as e:
                            logger.warning("Failed to delete temp dir %s: %s", temp_dir, e)

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Unexpected error in /run: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/events")
    async def events(
        request: Request,
        task: str, 
        session_id: Optional[str] = Query(default=None),
        agent_name: Optional[str] = Query(default=None),
        llm_profile: Optional[str] = Query(default=None)
    ):
        """Stream agent events for a task.
        
        Query parameters:
        - task: The task to execute
        - session_id: Optional session ID for conversation continuity
        - agent_name: Optional agent to use instead of default
        - llm_profile: Optional LLM profile override (turbo, normal, think, etc.)
        
        Authentication:
        - If user is authenticated (JWT token or API key), sessions are saved to their account
        - If not authenticated, sessions use "anonymous" user_id
        """
        logger = logging.getLogger(__name__)
        request_id = short_id()
        
        # Get current user (optional authentication)
        current_user = await _get_current_user_optional(request)
        
        # Determine user_id for session management
        user_id = current_user.username if current_user else "anonymous"
        
        logger.info("SSE /events connected, task=%s, request_id=%s, session_id=%s, agent=%s, llm_profile=%s, user_id=%s", 
                   task, request_id, session_id, agent_name or "default", llm_profile or "default", user_id)

        # Get agent with LLM override
        selected_agent, llm_override, llm_profile_info = _get_agent_with_overrides(agent_name, llm_profile)
        
        # Load existing session if session_id provided
        session_exists = False
        if session_id and _session_service:
            session_exists, msg_count = await _session_service.load_and_restore_session(
                selected_agent, user_id, session_id
            )

        async def event_stream():
            yield ":ok\n\n"
            
            was_new_session = (session_id is None) or (not session_exists)
            actual_session_id = session_id
            
            try:
                async for ev in selected_agent.run_events(task, request_id, actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                    logger.debug("SSE event: %s", ev.get("type"))
                    
                    if ev.get("type") == "start" and ev.get("session_id"):
                        old_session_id = actual_session_id
                        actual_session_id = ev["session_id"]
                        logger.debug(f"[SESSION_SAVE] Session ID captured from start event: {old_session_id} -> {actual_session_id}")
                    
                    if hasattr(ev, 'to_dict'):
                        payload = ev.to_dict()
                    else:
                        payload = ev
                    
                    # Format final event summary to HTML
                    if ev.get("type") == "final" and ev.get("summary") and selected_agent._hook_manager:
                        try:
                            formatted_summary, content_format = await selected_agent._hook_manager.execute_format_output_hooks(
                                output=payload["summary"],
                                request_id=request_id,
                                session_id=actual_session_id or "unknown",
                                output_format='html'
                            )
                            payload["summary"] = formatted_summary
                            payload["content_format"] = content_format
                        except Exception as e:
                            logger.error(f"[FORMAT_HTML] Failed to format summary to HTML: {e}", exc_info=True)
                    
                    try:
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    except (TypeError, ValueError) as e:
                        logger.error("Failed to serialize event %s: %s", ev, e)
                        error_payload = {"type": "error", "message": f"Serialization error: {str(e)}"}
                        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
            finally:
                # ALWAYS persist session after streaming, even if client disconnects
                logger.debug(f"[SESSION_SAVE] Stream finished, persisting session {actual_session_id}")
                if actual_session_id and _session_service:
                    agent_name_used = agent_name or "default"
                    llm_profile_used = llm_profile or "normal"
                    await _session_service.save_session(
                        selected_agent,
                        user_id,
                        actual_session_id,
                        agent_name_used,
                        llm_profile_used,
                        was_new_session
                    )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/cancel/{request_id}")
    async def cancel_request(request_id: str):
        """Cancel an active request by its ID."""
        logger = logging.getLogger(__name__)
        logger.info("Cancel request received for request_id=%s", request_id)

        success = await agent.cancel_request(request_id)
        if success:
            return {"status": "cancelled", "request_id": request_id}
        else:
            return {"status": "not_found", "request_id": request_id, "message": "Request not found or already completed"}

    @app.post("/events/{request_id}/append")
    async def append_event(request_id: str, request: Request, session_id: Optional[str] = Query(default=None)):
        """Append a user message to an existing active request or session.

        If `session_id` query parameter is provided, append directly to session.
        Body: { "content": "the user message" }
        """
        logger = logging.getLogger(__name__)
        from fastapi import HTTPException
        try:
            body = await request.json()
        except Exception as e:
            logger.debug("Invalid JSON body for append to %s: %s", request_id, e)
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        content = body.get('content')
        if not content:
            raise HTTPException(status_code=400, detail="Missing 'content' in body")

        if session_id:
            # Append directly to persisted session using agent method
            logger.debug("Appending to session %s: %.120s", session_id, content)
            success = await agent.append_to_session(session_id, content)
            if not success:
                raise HTTPException(status_code=404, detail="Session not found")
            return {"status": "appended", "session_id": session_id}

        logger.debug("Append request received for request_id=%s: %.120s", request_id, content)
        try:
            appended = await agent.append_user_message(request_id, content)
        except HTTPException:
            # Let agent-level HTTPExceptions bubble up
            raise
        except Exception as e:
            logger.exception("Unexpected error in append_user_message for %s: %s", request_id, e)
            raise HTTPException(status_code=500, detail=str(e))

        if appended:
            return {"status": "appended", "request_id": request_id}

        # If request not found/finished, try to append into the persisted session for this request
        async with agent._request_lock:
            sid = agent._request_to_session.get(request_id)
        if sid:
            logger.debug("Request %s already finished; appending to session %s", request_id, sid)
            success = await agent.append_to_session(sid, content)
            if success:
                return {"status": "appended", "session_id": sid}

        raise HTTPException(status_code=404, detail="Request not found or already completed")

    @app.post("/sessions")
    async def create_session():
        """Create a new session id for multi-turn conversations."""
        sid = short_id()
        # Pre-create empty session in agent
        async def _create():
            async with agent._request_lock:
                agent._sessions.setdefault(sid, [])
        await _create()
        return {"session_id": sid}

    @app.post("/sessions/{session_id}/append")
    async def append_to_session_endpoint(session_id: str, request: Request):
        """Append a user message directly to a session (no active request required)."""
        logger = logging.getLogger(__name__)
        try:
            body = await request.json()
            content = body.get('content')
            if not content:
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail="Missing 'content' in body")

            logger.debug("Session append request for session_id=%s: %.120s", session_id, content)
            success = await agent.append_to_session(session_id, content)
            if not success:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Session not found")

            return {"status": "appended", "session_id": session_id}
        except Exception as e:
            logger.exception("Failed to append to session %s: %s", session_id, e)
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/sessions/{session_id}/force_optimize")
    async def force_optimize_session(session_id: str):
        """Trigger the token optimizer / summarizer for a persisted session immediately."""
        logger = logging.getLogger(__name__)
        try:
            # Ensure session exists
            async with agent._request_lock:
                if session_id not in agent._sessions:
                    from fastapi import HTTPException
                    raise HTTPException(status_code=404, detail="Session not found")

            # Run optimizer and summarizer
            actions = {"optimizer": False, "summarizer": False}

            if getattr(agent, 'token_optimizer', None):
                try:
                    # token_optimizer.optimize_messages expects messages list; retrieve session messages
                    async with agent._request_lock:
                        msgs = list(agent._sessions.get(session_id, []))
                    # Run optimizer
                    new_msgs = await agent.token_optimizer.optimize_messages(msgs)
                    # Persist optimized messages
                    async with agent._request_lock:
                        agent._sessions[session_id] = new_msgs
                    actions['optimizer'] = True
                except Exception as e:
                    logger.exception("Failed to run token optimizer for session %s: %s", session_id, e)

            # Context management and summarization are now handled by hook plugins
            # (context_optimizer and context_summarizer) automatically during LLM calls
            # No manual summarization endpoint needed
            actions['summarizer'] = False  # Not applicable with hook-based management

            return {"status": "ok", "session_id": session_id, "actions": actions}
        except Exception as e:
            logger.exception("force_optimize_session failed for %s: %s", session_id, e)
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/sessions/force_optimize")
    async def force_optimize_all_sessions():
        """Trigger optimizer/summarizer for all persisted sessions."""
        logger = logging.getLogger(__name__)
        try:
            results = {}
            async with agent._request_lock:
                sids = list(agent._sessions.keys())

            for sid in sids:
                actions = {"optimizer": False, "summarizer": False}
                try:
                    if getattr(agent, 'token_optimizer', None):
                        msgs = list(agent._sessions.get(sid, []))
                        new_msgs = await agent.token_optimizer.optimize_messages(msgs)
                        async with agent._request_lock:
                            agent._sessions[sid] = new_msgs
                        actions['optimizer'] = True
                except Exception as e:
                    logger.debug("Optimizer failed for session %s: %s", sid, e)

                try:
                    # Context management and summarization are now handled by hook plugins automatically
                    # No manual summarization needed
                    actions['summarizer'] = False
                except Exception as e:
                    logger.debug("Summarizer failed for session %s: %s", sid, e)

                results[sid] = actions

            return {"status": "ok", "results": results}
        except Exception as e:
            logger.exception("force_optimize_all_sessions failed: %s", e)
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/status/stream")
    async def status_stream(
        request: Request,
        server: Optional[str] = Query(default=None, description="Filter by server name"),
        request_id: Optional[str] = Query(default=None, description="Filter by request id"),
        heartbeat: int = Query(default=15, ge=5, le=120, description="Heartbeat interval seconds"),
        close_after: Optional[int] = Query(
            default=None,
            ge=0,
            description="(Testing/diagnostics) Close stream after emitting this many events",
        ),
    ):
        """Server-Sent Events endpoint for unified status events (Task 0187).

        Streams events from the in-process StatusBus. Supports optional filtering
        by server and/or request_id. Emits periodic heartbeat comments so that
        intermediaries keep the connection alive. Clients can simply listen for
        'message' events and parse the JSON payload.
        """
        logger = logging.getLogger(__name__)
        # Optional simple auth if AGENT_STATUS_REQUIRE_AUTH=1 and header X-Status-Token must match AGENT_STATUS_TOKEN
        if os.getenv("AGENT_STATUS_REQUIRE_AUTH") == "1":
            expected = os.getenv("AGENT_STATUS_TOKEN", "")
            provided = request.headers.get("X-Status-Token", "")
            if not expected or provided != expected:
                from fastapi import HTTPException
                raise HTTPException(status_code=401, detail="Unauthorized status stream")

        logger.info("SSE /status/stream connected (server=%s request_id=%s)", server, request_id)
        queue = await status_bus.subscribe(server=server, request_id=request_id)

        async def event_gen():
            sent_events = 0
            try:
                yield ":ok\n\n"  # initial comment
                if close_after is not None and close_after <= 0:
                    # Immediate close requested (testing)
                    return
                loop = asyncio.get_event_loop()
                last_hb = loop.time()
                while True:
                    now = loop.time()
                    if now - last_hb >= heartbeat:
                        yield f":hb {int(now)}\n\n"
                        last_hb = now

                    try:
                        ev: StatusEvent = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        ev = None
                    except asyncio.CancelledError:
                        break

                    if ev is not None:
                        try:
                            payload = ev.to_dict()
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            sent_events += 1
                            if close_after is not None and sent_events >= close_after:
                                logger.debug(
                                    "/status/stream close_after=%s reached (events=%s)",
                                    close_after,
                                    sent_events,
                                )
                                break
                        except Exception as e:
                            logger.error(f"Failed to serialize status event: {e}", exc_info=True)

                    if await request.is_disconnected():
                        logger.info("Client disconnected from /status/stream")
                        break
            finally:
                # Drain any remaining queued events deterministically so that
                # terminal StatusPhase.END/StatusPhase.ERROR messages are delivered to the
                # client even if the generator is exiting due to client
                # disconnect or server-initiated close. This avoids races that
                # make clients miss final events.
                try:
                    while not queue.empty():
                        try:
                            ev: StatusEvent = queue.get_nowait()
                        except Exception as e:
                            logger.debug(f"Failed to get event from queue during drain: {e}")
                            break
                        try:
                            payload = ev.to_dict()
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        except Exception as e:
                            logger.error(f"Failed to serialize status event during drain: {e}", exc_info=True)
                except Exception as e:
                    # Ignore issues while draining to ensure cleanup continues
                    logger.debug(f"Exception during status queue drain: {e}")
                try:
                    status_bus.unsubscribe(queue)
                except Exception:  # pragma: no cover - defensive
                    pass
                if logger.handlers:  # avoid errors during interpreter shutdown
                    logger.debug("/status/stream subscriber cleaned up")

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        response = templates.TemplateResponse(request, "index.html")

        # Disable caching if configured
        if config.network.disable_cache:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response
    
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        """Login page for multi-user authentication"""
        response = templates.TemplateResponse(request, "login.html")
        
        # Disable caching for login page
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        
        return response
    
    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        # Redirect to main page with integrated status
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)

    @app.get("/status/meta")
    async def status_meta(request: Request):
        if os.getenv("AGENT_STATUS_REQUIRE_AUTH") == "1":
            expected = os.getenv("AGENT_STATUS_TOKEN", "")
            provided = request.headers.get("X-Status-Token", "")
            if not expected or provided != expected:
                from fastapi import HTTPException
                raise HTTPException(status_code=401, detail="Unauthorized")
        return get_status_metrics()

    @app.post("/status/publish-test")
    async def status_publish_test(server: str = Query(..., description="Server name for test event"), message: str = Query("Test event", description="Message text")):
        """Diagnostic endpoint to publish a test status event for the given server.

        Helps verifying that the SSE `/status/stream` is delivering events to connected clients.
        """
        try:
            await publish_status(server, message, phase="progress")
            return {"result": "published", "server": server, "message": message}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/debug/toggle")
    async def debug_toggle():
        """Toggle application debug logging and return current debug state.

        This endpoint is used by the status toolbar integration tests to flip
        debug mode on/off for the running FastAPI app instance. It does not
        modify global logging configuration permanently; instead it stores a
        simple flag on `app.state.debug_enabled` for the lifetime of this app.
        """
        try:
            # Initialize flag if missing
            if not hasattr(app.state, 'debug_enabled'):
                app.state.debug_enabled = False

            # Toggle flag
            app.state.debug_enabled = not app.state.debug_enabled

            # Attempt to adjust root logger level for convenience (non-fatal)
            try:
                root_logger = logging.getLogger()
                root_logger.setLevel(logging.DEBUG if app.state.debug_enabled else logging.INFO)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to set root logger level during debug toggle: {e}", exc_info=True)

            return {"debug": bool(app.state.debug_enabled)}
        except Exception as e:
            logging.getLogger(__name__).exception("Debug toggle failed: %s", e)
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/debug/context")
    async def debug_context(agent_name: str | None = None):
        """Diagnostic endpoint to get current context management state and conversation messages."""
        try:
            # Get the agent from registry if available
            agent = None
            if _app_registry:
                # Use provided agent_name, or fall back to configured default
                entry_name = agent_name or config.default_agent or 'agent'
                agent = _app_registry.get(entry_name)

            if not agent or not hasattr(agent, 'context_manager'):
                return {
                    "error": "Agent or context manager not available",
                    "context_window": "N/A",
                    "prediction_threshold": 0,
                    "summarization_threshold": "N/A",
                    "actual_usage": {"total_tokens": 0, "last_call_tokens": 0},
                    "messages": []
                }

            # Context management is now handled by hook plugins (no centralized stats available)

            # Get current conversation messages if available
            messages = []
            if hasattr(agent, '_current_messages') and agent._current_messages:
                # Estimate tokens for each message and prepare for display
                from ..llm.token_utils import estimate_token_count
                for i, msg in enumerate(agent._current_messages):
                    # Make sure msg is a ChatMessage object before estimating tokens
                    if not isinstance(msg, ChatMessage):
                        # Attempt to convert dict to ChatMessage if possible
                        try:
                            msg = ChatMessage(**msg)
                        except (TypeError, ValueError):
                            # Skip if conversion fails
                            continue

                    # Use token_utils for estimation
                    estimated_tokens = estimate_token_count([msg])
                    messages.append({
                        "role": msg.role,
                        "content": msg.content,
                        "estimated_tokens": estimated_tokens,
                        "has_tool_calls": bool(msg.tool_calls)
                    })

            return {
                "context_window": agent.llm.context_window if hasattr(agent, 'llm') else "N/A",
                "prediction_threshold": 0,  # No longer tracked centrally
                "summarization_threshold": "N/A",  # Now in hook plugin config
                "actual_usage": {"total_tokens": 0, "last_call_tokens": 0},  # No longer tracked centrally
                "warning_levels": {},  # No longer tracked centrally
                "messages": messages,
                "message_count": len(messages),
                "note": "Context management migrated to hook plugins"
            }
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Debug context endpoint failed: %s", e)
            return {
                "error": f"Debug endpoint failed: {str(e)}",
                "context_window": "Error",
                "prediction_threshold": 0,
                "summarization_threshold": "Error",
                "actual_usage": {"total_tokens": 0, "last_call_tokens": 0},
                "messages": []
            }

    @app.get("/mcp/status")
    async def mcp_status(force_refresh: bool = False):
        """Get MCP server status including plugins and external servers.
        
        Args:
            force_refresh: If True, invalidates cache before fetching status
        """
        try:
            logger = logging.getLogger(__name__)
            
            # Use MCPService for comprehensive status
            global _mcp_service, _app_registry, _mcp_integration
            
            if not _mcp_service:
                return {"error": "MCP service not initialized"}
            
            if not _app_registry:
                return {"error": "Registry not initialized"}
            
            # If force_refresh requested, invalidate cache first
            if force_refresh and _mcp_integration:
                try:
                    await _mcp_integration.invalidate_tools_cache()
                    logger.debug("Cache invalidated due to force_refresh=True")
                except Exception as e:
                    logger.warning(f"Failed to invalidate cache: {e}")
            
            # Delegate to MCPService
            status = await _mcp_service.get_comprehensive_status(
                registry=_app_registry,
                check_connectivity=True  # Always check connectivity for accurate status
            )
            
            return status
            
        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"MCP status error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to get MCP status: {str(e)}"}

    @app.get("/mcp/cache/statistics")
    async def mcp_cache_statistics():
        """Get MCP tool cache statistics for monitoring."""
        try:
            global _mcp_integration

            if not _mcp_integration:
                return {"error": "MCP integration not initialized"}

            # Get cache statistics from MCP integration
            stats = await _mcp_integration.get_cache_statistics()
            return {
                "success": True,
                "cache": stats
            }

        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"MCP cache statistics error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to get cache statistics: {str(e)}"}

    @app.post("/mcp/cache/invalidate")
    async def mcp_cache_invalidate():
        """Manually invalidate the MCP tool cache."""
        try:
            global _mcp_integration

            if not _mcp_integration:
                return {"error": "MCP integration not initialized"}

            # Invalidate the cache
            await _mcp_integration.invalidate_tools_cache()

            return {
                "success": True,
                "message": "Cache invalidated successfully"
            }

        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"MCP cache invalidation error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to invalidate cache: {str(e)}"}

    # ===========================
    # MCP Server Mode Endpoints (Epic 0037)
    # ===========================

    @app.post("/mcp")
    async def mcp_server_endpoint(request: Request):
        """
        MCP JSON-RPC 2.0 server endpoint.
        
        Handles MCP protocol requests when running in server mode.
        Exposes activated plugins as MCP tools to remote MCP clients.
        
        Requires MCP server mode to be enabled in configuration.
        Authentication required if server_mode.authentication.required is true.
        """
        logger = logging.getLogger(__name__)
        
        # Check if MCP server mode is enabled
        if not _mcp_server_handler:
            raise HTTPException(
                status_code=501,
                detail="MCP server mode is not enabled. Set mcp_server_mode.enabled: true in config/mcp_server_mode.yaml"
            )
        
        # Check authentication if required
        server_config = config.server_mode
        if server_config.authentication.required:
            # Try to get user from JWT or API key
            current_user = None
            
            # Try JWT first (Bearer token)
            if "jwt" in server_config.authentication.methods:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    try:
                        from agent_system.auth.dependencies import get_current_user_from_token
                        from agent_system.auth.database import get_db
                        
                        token = auth_header.split(" ", 1)[1]
                        db = await anext(get_db())  # Get database instance
                        current_user = await get_current_user_from_token(token, db)
                    except Exception as e:
                        logger.debug(f"JWT authentication failed: {e}")
            
            # Try API key if JWT failed
            if not current_user and "api_key" in server_config.authentication.methods:
                api_key = request.headers.get("X-API-Key")
                if api_key:
                    try:
                        from agent_system.auth.database import get_db, verify_api_key
                        
                        db = await anext(get_db())
                        current_user = await verify_api_key(db, api_key)
                    except Exception as e:
                        logger.debug(f"API key authentication failed: {e}")
            
            # If authentication required but no valid credentials
            if not current_user:
                raise HTTPException(
                    status_code=401,
                    detail="Authentication required. Provide valid JWT token (Authorization: Bearer <token>) or API key (X-API-Key: <key>)"
                )
            
            logger.info(f"MCP request authenticated for user: {current_user.username}")
        
        # Delegate request to MCP server handler
        try:
            return await _mcp_server_handler.handle_request(request)
        except Exception as e:
            logger.exception("MCP server request failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/mcp/sse")
    async def mcp_server_sse(request: Request):
        """
        MCP server SSE stream endpoint (MCP spec compliant).
        
        According to MCP spec, this endpoint:
        1. Accepts client SSE connections
        2. Sends an 'endpoint' event with the POST URI for client messages
        3. Sends server messages as 'message' events (tools/resources/prompts notifications)
        
        This implements server-initiated notifications for:
        - tools/list: Tool availability changes
        - resources/list: Resource availability changes  
        - prompts/list: Prompt availability changes
        
        Spec: https://modelcontextprotocol.io/specification/2024-11-05/basic/transports#http-with-sse
        """
        if not _mcp_server_handler:
            raise HTTPException(
                status_code=501,
                detail="MCP server mode is not enabled"
            )
        
        logger.info("MCP SSE client connected")
        
        # Create SSE event generator following MCP spec
        async def event_generator():
            try:
                # 1. Send 'endpoint' event with POST URI (required by MCP spec)
                endpoint_uri = str(request.url_for("mcp_server_endpoint"))
                endpoint_event = f"event: endpoint\ndata: {endpoint_uri}\n\n"
                yield endpoint_event
                logger.debug(f"Sent endpoint event: {endpoint_uri}")
                
                # 2. Send initial server capabilities and available tools
                # Send tools/list notification
                try:
                    tools_result = await _mcp_server_handler._handle_tools_list({}, None)
                    tools_notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed",
                        "params": tools_result
                    }
                    yield f"event: message\ndata: {json.dumps(tools_notification, ensure_ascii=False)}\n\n"
                    logger.debug(f"Sent tools list notification: {len(tools_result.get('tools', []))} tools")
                except Exception as e:
                    logger.error(f"Failed to send tools list: {e}")
                
                # Send resources/list notification
                try:
                    resources_result = await _mcp_server_handler._handle_resources_list({}, None)
                    resources_notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/resources/list_changed",
                        "params": resources_result
                    }
                    yield f"event: message\ndata: {json.dumps(resources_notification, ensure_ascii=False)}\n\n"
                    logger.debug("Sent resources list notification")
                except Exception as e:
                    logger.debug(f"Resources not available: {e}")
                
                # Send prompts/list notification
                try:
                    prompts_result = await _mcp_server_handler._handle_prompts_list({}, None)
                    prompts_notification = {
                        "jsonrpc": "2.0",
                        "method": "notifications/prompts/list_changed",
                        "params": prompts_result
                    }
                    yield f"event: message\ndata: {json.dumps(prompts_notification, ensure_ascii=False)}\n\n"
                    logger.debug("Sent prompts list notification")
                except Exception as e:
                    logger.debug(f"Prompts not available: {e}")
                
                # 3. Keep connection alive with periodic heartbeats
                while True:
                    # Check if client disconnected
                    if await request.is_disconnected():
                        logger.debug("MCP SSE client disconnected")
                        break
                    
                    # Send heartbeat as a 'message' event with JSON-RPC notification
                    heartbeat_message = {
                        "jsonrpc": "2.0",
                        "method": "notifications/heartbeat",
                        "params": {
                            "timestamp": datetime.now().astimezone().isoformat()
                        }
                    }
                    yield f"event: message\ndata: {json.dumps(heartbeat_message, ensure_ascii=False)}\n\n"
                    
                    # Wait before next heartbeat (30 seconds)
                    await asyncio.sleep(30)
                    
            except asyncio.CancelledError:
                logger.debug("MCP SSE stream cancelled")
            except Exception as e:
                logger.exception("MCP SSE stream error: %s", e)
        
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive"
            }
        )

    @app.get("/mcp/server-info")
    async def mcp_server_info():
        """
        Get MCP server information (non-MCP REST endpoint).
        
        Provides server capabilities, exposed plugins, and session statistics
        without requiring MCP protocol.
        """
        if not _mcp_server_handler:
            return {
                "enabled": False,
                "message": "MCP server mode is not enabled"
            }
        
        try:
            server_config = config.server_mode
            session_stats = _mcp_server_handler.get_session_stats()
            
            return {
                "enabled": True,
                "endpoint": server_config.endpoint,
                "sse_endpoint": server_config.sse_endpoint,
                "exposed_plugins": server_config.expose_plugins,
                "authentication_required": server_config.authentication.required,
                "authentication_methods": server_config.authentication.methods,
                "rate_limit_enabled": server_config.rate_limit.enabled,
                "session_ttl": server_config.session_ttl,
                "max_concurrent_sessions": server_config.max_concurrent_sessions,
                "sessions": session_stats
            }
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Failed to get MCP server info: %s", e)
            return {"error": str(e)}

    # ===========================
    # End MCP Server Mode Endpoints
    # ===========================

    # ===========================
    # Hook Introspection Endpoints
    # ===========================

    @app.get("/hooks")
    async def list_hooks(hook_type: str | None = None):
        """List all registered hooks, optionally filtered by type."""
        try:
            from agent_system.hooks import get_hook_registry
            registry = get_hook_registry()
            hooks_dict = registry.list_hooks()
            
            if hook_type:
                # Filter by type
                return {hook_type: hooks_dict.get(hook_type, [])}
            
            return hooks_dict
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Failed to list hooks: %s", e)
            return {"error": str(e)}

    @app.get("/hooks/{hook_name}")
    async def get_hook_info(hook_name: str):
        """Get detailed information about a specific hook."""
        try:
            from agent_system.hooks import get_hook_registry
            registry = get_hook_registry()
            info = registry.get_hook_info(hook_name)
            
            if not info:
                return {"error": f"Hook '{hook_name}' not found"}
            
            return info
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Failed to get hook info: %s", e)
            return {"error": str(e)}

    @app.get("/hooks/stats/all")
    async def get_all_hooks_stats():
        """Get execution statistics for all hooks."""
        try:
            from agent_system.hooks import get_hook_registry
            registry = get_hook_registry()
            hooks_dict = registry.list_hooks()
            all_stats = {}
            
            for hook_type, hook_names in hooks_dict.items():
                for name in hook_names:
                    stats = registry.get_stats(name)
                    if stats:
                        all_stats[name] = stats
            
            return all_stats
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Failed to get hooks stats: %s", e)
            return {"error": str(e)}

    @app.get("/hooks/stats/{hook_name}")
    async def get_hook_stats(hook_name: str):
        """Get execution statistics for a specific hook."""
        try:
            from agent_system.hooks import get_hook_registry
            registry = get_hook_registry()
            stats = registry.get_stats(hook_name)
            
            if stats is None:
                info = registry.get_hook_info(hook_name)
                if not info:
                    return {"error": f"Hook '{hook_name}' not found"}
                return {hook_name: {}}
            
            return {hook_name: stats}
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Failed to get hook stats: %s", e)
            return {"error": str(e)}

    # ===========================
    # End Hook Introspection Endpoints
    # ===========================

    @app.get("/favicon.ico")
    async def favicon():
        favicon_path = Path(__file__).parents[3] / "static" / "favicon.ico"
        if favicon_path.exists():
            return FileResponse(favicon_path)
        else:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Favicon not found")

    return app


# Compatibility shim: expose a simple getter so tests can patch this module
# function to supply a mock MCPIntegration. It delegates to the real
# integration module when available.
def get_mcp_integration(app: Optional[FastAPI] = None):
    from ..mcp.integration import get_mcp_integration as _get
    return _get(app)


def run() -> None:
    """Run the FastAPI server with proper configuration."""
    # Set UTF-8 environment for Windows compatibility
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

    # Load configuration
    cfg_path = str(Path(__file__).parents[3] / "config" / "config.yaml")
    config = load_settings(cfg_path)

    # Build the application
    app_obj = build_app(cfg_path)

    # Get server configuration
    host = os.getenv("HOST") or config.network.host or "127.0.0.1"
    # Allow tests to override the server port explicitly via TEST_SERVER_PORT
    # so they don't accidentally collide with a locally running production
    # instance on the default port (8000).
    test_port_env = os.getenv("TEST_SERVER_PORT")
    port_env = os.getenv("PORT")
    if test_port_env:
        port = int(test_port_env)
    else:
        port = int(port_env) if port_env else int(getattr(config.network, "port", 8000))

    # Configure log level. Allow AGENT_LOG_LEVEL to override for the running
    # uvicorn process as well so console logging can be forced without editing
    # config.yaml.
    uvicorn_log_level = (os.getenv("AGENT_LOG_LEVEL") or (config.logging.level if config.logging.enabled else "info")).lower()

    # Run the server - uvicorn handles SIGINT/SIGTERM gracefully by default
    logger = logging.getLogger(__name__)
    logger.info("Starting FastAPI server on %s:%s", host, port)

    uvicorn.run(
        app_obj,
        host=host,
        port=port,
        log_level=uvicorn_log_level,
        access_log=config.logging.enabled,
        use_colors=False,
        log_config=None  # Disable uvicorn's logging config to preserve our setup
    )


if __name__ == "__main__":
    run()
