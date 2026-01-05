from __future__ import annotations

import asyncio  # noqa: F401 - used in nested closures in event_stream()
import json
import logging
import os
import time
from .utils.id import short_id
import yaml
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

import uvicorn
from fastapi import FastAPI, Request, Query, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.datastructures import UploadFile  # Use starlette's UploadFile for isinstance checks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config.models import AgentConfig
from .api.endpoints import router as api_router
from .mcp.base import MCPRegistry
from .utils.logging import setup_logging
from .mcp.status import get_status_metrics
from .mcp.integration import initialize_mcp, shutdown_mcp
from .llm.batch.initialization import init_batch_system, shutdown_batch_system, start_batch_queue_manager

# Import services
from .services import ConfigService, MCPService, ToolService, AgentService
from .services.session_manager import SessionManager, SessionPermissionError


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
_initialization_service: Optional[Any] = None  # InitializationService
_session_manager: Optional[SessionManager] = None
_session_service: Optional[Any] = None  # SessionService, imported at runtime to avoid circular import

# Security: Track request_id -> user_id mapping for status stream authorization
_request_user_map: dict[str, str] = {}  # request_id -> user_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for startup and shutdown events."""
    logger = logging.getLogger(__name__)
    logger.info("FastAPI application starting up")

    # Startup: Initialize MCP integration
    global _mcp_integration, _mcp_service, _tool_service, _session_manager, _session_service

    # Get config from global service
    if _config_service is None:
        logger.error("ConfigService not initialized before lifespan startup")
    else:
        config = _config_service.get_config()

        # Initialize MCP integration
        logger.info("Starting MCP integration initialization...")
        try:
            from .mcp.integration import initialize_mcp
            from .services.mcp_service import MCPService
            from .services.tool_service import ToolService
            from .services.session_manager import SessionManager

            mcp_integration = await initialize_mcp(config, app)
            _mcp_integration = mcp_integration

            # Initialize services
            _mcp_service = MCPService(mcp_integration, config)
            _tool_service = ToolService(mcp_integration, config)

            # Initialize SessionManager
            from pathlib import Path
            # Allow tests to override session storage path via environment variable
            env_storage_path = os.getenv("AGENT_SESSION_STORAGE_PATH")
            if env_storage_path:
                storage_path = Path(env_storage_path)
            else:
                storage_path = Path(__file__).parents[2] / "data" / "sessions"
            _session_manager = SessionManager(storage_path=str(storage_path))
            logger.info(f"SessionManager initialized with storage_path={storage_path}")

            # Initialize SessionService
            from .services.session_service import SessionService
            _session_service = SessionService(_session_manager)
            logger.info("SessionService initialized")

            # Store session manager in app state for dependency injection
            app.state.session_manager = _session_manager
            logger.info("SessionManager stored in app.state for dependency injection")

            # Make integration accessible to mcp module
            from .mcp import integration as _mcp_mod
            _mcp_mod.mcp_integration = mcp_integration

            logger.info("MCP integration startup complete")
        except Exception as e:
            logger.error(f"Failed to initialize MCP integration: {e}", exc_info=True)
            raise

    yield

    # Shutdown logic
    logger.info("FastAPI application shutting down gracefully")
    try:
        # Shutdown MCP integration
        if _mcp_integration:
            from .mcp.integration import shutdown_mcp
            await shutdown_mcp()
            logger.info("MCP integration shut down")
    except Exception as e:
        logger.error("Error during shutdown cleanup: %s", e)
    finally:
        logger.info("FastAPI application shutdown complete")


# Module level templates and static path setup
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
static_path = Path(__file__).parents[2] / "static"

# Global application state
_app_start_time = None

# Batch queue manager - uses centralized initialization from llm.batch.initialization


async def _init_batch_queue_manager(config, logger):
    """Initialize batch queue manager if any model has batch enabled.
    
    Delegates to the centralized init_batch_system() utility function
    which can be reused by CLI and other entry points.
    """
    await init_batch_system(config, custom_logger=logger)


# Note: _register_batch_clients is now in llm.batch.initialization module


async def _shutdown_batch_queue_manager(logger):
    """Stop the batch queue manager during shutdown.
    
    Delegates to the centralized shutdown_batch_system() utility function.
    """
    await shutdown_batch_system(custom_logger=logger)


def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    from pathlib import Path

    # Initialize ConfigService and load configuration
    if not config_path:
        cfg_path = str(Path(__file__).parents[2] / "config" / "config.yaml")
    else:
        cfg_path = config_path

    # Create ConfigService
    global _config_service
    _config_service = ConfigService()
    config = _config_service.load_config(config_path=cfg_path)

    # Setup logging via ConfigService
    _config_service.setup_logging()
    
    # Get logger AFTER logging is configured
    logger = logging.getLogger(__name__)

    # Store config for lazy batch queue manager initialization
    # This allows LLMFactory to create the manager on first use
    from .llm.factory import set_batch_config
    set_batch_config(config)

    # Configure status bus with config values
    from .mcp.status import status_bus
    if hasattr(config, 'status') and config.status:
        status_bus.default_queue_maxsize = config.status.queue_maxsize
        logger.debug(f"Status bus configured: queue_maxsize={config.status.queue_maxsize}")

    # Log configuration status
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

    # Initialize centralized initialization service
    # This handles SessionManager, SessionService, and dependency injection
    from .services.initialization_service import InitializationService
    global _initialization_service, _session_manager, _session_service
    _initialization_service = InitializationService(config)
    _session_manager = _initialization_service.session_manager
    _session_service = _initialization_service.session_service
    logger.info("InitializationService created (SessionManager and SessionService ready)")

    # Initialize MCP integration
    async def _init_mcp_for_app(app: FastAPI):
        global _mcp_integration, _mcp_service, _tool_service, _agent_service
        logger = logging.getLogger(__name__)
        logger.info("Starting MCP integration initialization...")
        try:
            # Start the Batch Queue Manager (async operations: register providers, start background tasks)
            # The manager was already created and registered in build_app() sync section
            await start_batch_queue_manager(config, custom_logger=logger)
            
            mcp_integration = await initialize_mcp(config, app)
            _mcp_integration = mcp_integration

            # Mark that bootstrap_servers() was already called by initialize_mcp
            mcp_integration.servers_bootstrapped = True

            # Initialize services
            _mcp_service = MCPService(mcp_integration, config)
            _tool_service = ToolService(mcp_integration, config)

            # CRITICAL: Inject session_service into ALL agents in plugin_registry
            # This ensures hooks and tools can access session management
            # Must be done AFTER bootstrap_servers() in initialize_mcp() created agents
            _initialization_service.initialize_for_api(
                plugin_registry=mcp_integration.plugin_registry,
                skip_bootstrap=True  # Already done by initialize_mcp
            )

            # Store session manager in app state for dependency injection (after initialization)
            app.state.session_manager = _session_manager
            logger.info("SessionManager stored in app.state for dependency injection")

            # Agent will be initialized later when needed
            # (requires agent instance from bootstrap_servers)

            # Make integration accessible to mcp module
            from .mcp import integration as _mcp_mod
            _mcp_mod.mcp_integration = mcp_integration

            logger.info("MCP integration and services initialized for API")

            # Apply plugin web capabilities
            from .plugins.web_adapter import plugin_web_registry
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
        
        # Start profiling if enabled
        from .utils.profiling import start_profiling, stop_profiling, PROFILING_ENABLED
        if PROFILING_ENABLED:
            await start_profiling()
            logger.info("Performance profiling started")
        
        # Start memory profiling if enabled
        from .utils.memory_profiling import (
            start_memory_profiling, stop_memory_profiling, MEMORY_PROFILING_ENABLED
        )
        if MEMORY_PROFILING_ENABLED:
            # Setup profiling logger if not already done (e.g., if only memory profiling enabled)
            if not PROFILING_ENABLED:
                from .utils.profiling import setup_profiling_logger
                setup_profiling_logger()
            await start_memory_profiling()
            logger.info("Memory profiling started")
        
        yield
        # Shutdown
        try:
            # Stop profiling
            if PROFILING_ENABLED:
                await stop_profiling()
                logger.info("Performance profiling stopped")
            
            # Stop memory profiling
            if MEMORY_PROFILING_ENABLED:
                await stop_memory_profiling()
                logger.info("Memory profiling stopped")
            
            # Shutdown batch queue manager first
            await _shutdown_batch_queue_manager(logger)
            
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
        3. JWT token in 'token' query parameter (for EventSource - workaround for no custom headers)
        4. X-API-Key header (for programmatic access)
        """
        try:
            from .auth.dependencies import get_current_user as get_user_dep
            from .auth.database import get_db
            from fastapi.security import HTTPBearer

            bearer_scheme = HTTPBearer(auto_error=False)
            credentials = await bearer_scheme(request)
            x_api_key = request.headers.get("X-API-Key")

            # NEW: Check for token in query parameters (EventSource workaround)
            query_token = request.query_params.get("token")
            if query_token and not credentials:
                # Create fake credentials object from query token
                from fastapi.security import HTTPAuthorizationCredentials
                credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=query_token)

            # Debug: Check what auth methods are available
            has_bearer = credentials is not None
            has_cookie = request.cookies.get("access_token") is not None
            has_api_key = x_api_key is not None
            has_query_token = query_token is not None
            logger.debug(f"[AUTH_DEBUG] Auth methods - Bearer: {has_bearer}, Cookie: {has_cookie}, QueryToken: {has_query_token}, API-Key: {has_api_key}")

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
        # NOTE: We add headers in StaticFiles response hook instead of middleware
        # to avoid BaseHTTPMiddleware overhead (100ms+ per request)
        if config.network.disable_cache:
            original_static_call = static_files.__call__
            
            async def static_with_no_cache(scope, receive, send):
                if scope["type"] != "http":
                    await original_static_call(scope, receive, send)
                    return
                
                async def send_with_no_cache(message):
                    if message["type"] == "http.response.start":
                        headers = list(message.get("headers", []))
                        headers.extend([
                            (b"cache-control", b"no-cache, no-store, must-revalidate"),
                            (b"pragma", b"no-cache"),
                            (b"expires", b"0"),
                        ])
                        message = {**message, "headers": headers}
                    await send(message)
                
                await original_static_call(scope, receive, send_with_no_cache)
            
            static_files.__call__ = static_with_no_cache

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

    # Bootstrap MCP servers and plugin registry using InitializationService
    # This handles bootstrap_servers() and session_service injection
    # Note: Batch queue manager is created lazily by LLMFactory when first needed
    registry = MCPRegistry()
    if not _mcp_integration or not _mcp_integration.servers_bootstrapped:
        # Use InitializationService for consistent bootstrap + injection
        registry = _initialization_service.bootstrap_and_inject(
            registry=registry,
            inject_sessions=True
        )
        # Mark as bootstrapped to prevent duplicate calls
        if _mcp_integration:
            _mcp_integration.servers_bootstrapped = True
        logging.getLogger(__name__).info("Bootstrapped servers using InitializationService")
    else:
        # Servers already bootstrapped by initialize_mcp, just populate local registry
        # by copying from plugin_registry and inject sessions
        from .servers.agent.server import Agent as _Agent
        for server_name in _mcp_integration.plugin_registry.list_servers():
            server_adapter = _mcp_integration.plugin_registry.get_server(server_name)
            if server_adapter and hasattr(server_adapter, 'plugin_server'):
                registry.register(server_name, server_adapter.plugin_server)
        logging.getLogger(__name__).debug(f"Populated local registry with {len(registry.list())} servers from plugin_registry")

        # Inject session_service into local registry agents
        from .services.agent_injection import inject_session_service_into_agents
        inject_session_service_into_agents(registry, _session_service)

    # Get entry agent from config
    entry_name = config.default_agent or 'agent'
    logging.getLogger(__name__).debug(f"Using entry agent: '{entry_name}'")

    # Reuse existing agent from registry if available
    selected_agent = None
    try:
        if entry_name in registry.list():
            candidate = registry.get(entry_name)
            from .servers.agent.server import Agent as _Agent
            if isinstance(candidate, _Agent):
                selected_agent = candidate
                # CRITICAL: Inject _session_service into existing agent (same as CLI does)
                # Agents from bootstrap_servers were created without session_service
                # ALWAYS inject, even if attribute exists, to refresh the reference
                selected_agent._session_service = _session_service
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
                                        from .config.models import ToolConfig
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
                                        from .config.models import ToolConfig
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
        from .servers.agent.server import Agent as CoreAgent
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
        from .config.models import MCPConfig, AgentConfig, ToolConfig
        mcp_cfg = _config_service.get_default_mcp_config(config)

        if not mcp_cfg:
            # Create default MCPConfig if not found
            logging.getLogger(__name__).warning(
                "No default_config found in plugins configuration, creating default MCPConfig with llm_profile='normal'"
            )
            tool_cfg = ToolConfig()
            agent_cfg = AgentConfig(llm_profile="normal", tools=tool_cfg)
            mcp_cfg = MCPConfig(type="agent", enabled=True, agent_config=agent_cfg)

        selected_agent = CoreAgent(entry_name, config, mcp_cfg, registry, session_service=_session_service)
        registry.register(entry_name, selected_agent)
    else:
        # Bind reused agent to current registry and update session_service
        try:
            selected_agent.registry = registry
            # Update session_service for existing agent
            if _session_service and hasattr(selected_agent, '_session_service'):
                selected_agent._session_service = _session_service
        except Exception as e:
            logger.warning(f"Failed to bind registry to agent: {e}", exc_info=True)

    agent = selected_agent

    # Store agent and registry in app state for dependency injection
    app.state.agent = agent
    app.state.mcp_registry = registry
    logger.info("Default agent and registry stored in app.state for dependency injection")

    # Store registry and config globally
    global _app_registry, _app_config, _mcp_server_handler
    _app_registry = registry
    _app_config = config

    # Initialize MCP server mode if enabled (Epic 0037)
    server_mode_enabled = config.server_mode and config.server_mode.enabled
    if server_mode_enabled:
        logger.info("Initializing MCP server handler for server mode")
        try:
            from .mcp.server_handler import MCPServerHandler
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

    # Include debug/profiling router (available when AGENT_ENABLE_PROFILING=1)
    from .api.debug_endpoints import router as debug_router
    app.include_router(debug_router)

    # Add profiling middleware if enabled
    from .utils.profiling import PROFILING_ENABLED, create_profiling_middleware
    if PROFILING_ENABLED:
        import asyncio
        # Schedule middleware installation (needs event loop)
        @app.on_event("startup")
        async def _install_profiling_middleware():
            await create_profiling_middleware(app)
            logger.info("Profiling middleware installed")

    # Initialize authentication system if enabled
    if config.auth and config.auth.enabled:
        logger.info("Multi-user authentication enabled, initializing auth system...")

        # Setup auth database and configuration
        from .auth.database import setup_database
        from .auth.security import set_jwt_config
        from .auth.middleware import configure_cors, configure_security_middleware
        from .auth.models import UserCreate, UserRole
        from pathlib import Path as AuthPath

        # Configure JWT settings
        set_jwt_config(
            secret_key=config.auth.secret_key,
            algorithm=config.auth.algorithm,
            expire_minutes=config.auth.access_token_expire_minutes,
            refresh_expire_days=config.auth.refresh_token_expire_days
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
                # Generate secure random password if not configured
                admin_password = config.auth.default_admin_password
                if not admin_password:
                    import secrets
                    admin_password = secrets.token_urlsafe(16)

                default_admin = UserCreate(
                    username=config.auth.default_admin_username,
                    email=config.auth.default_admin_email,
                    password=admin_password,
                    full_name="Default Administrator",
                    role=UserRole.ADMIN,
                    is_active=True
                )
                db.create_user(default_admin)
                logger.warning(
                    "╔═══════════════════════════════════════════════════════════════╗"
                )
                logger.warning(
                    "║  DEFAULT ADMIN USER CREATED - SAVE THESE CREDENTIALS!        ║"
                )
                logger.warning(
                    "╠═══════════════════════════════════════════════════════════════╣"
                )
                logger.warning(
                    f"║  Username: {config.auth.default_admin_username:<50}║"
                )
                logger.warning(
                    f"║  Password: {admin_password:<50}║"
                )
                logger.warning(
                    f"║  Email:    {config.auth.default_admin_email:<50}║"
                )
                logger.warning(
                    "╠═══════════════════════════════════════════════════════════════╣"
                )
                logger.warning(
                    "║  ⚠️  CHANGE PASSWORD IMMEDIATELY AFTER FIRST LOGIN!          ║"
                )
                logger.warning(
                    "╚═══════════════════════════════════════════════════════════════╝"
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
        from .api.auth_endpoints import router as auth_router
        from .api.admin_endpoints import router as admin_router
        from .api.menu_endpoints import menu_router
        from .api.session_endpoints import session_router

        app.include_router(auth_router)
        app.include_router(admin_router)
        app.include_router(menu_router)
        app.include_router(session_router)

        # Note: SessionManager is stored in app.state during async lifespan startup

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
            agent_config_path = Path(__file__).parents[2] / "config" / "config.yaml"
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
                from .servers.agent.server import Agent as _Agent
                if not isinstance(selected_agent, _Agent):
                    raise HTTPException(status_code=400, detail=f"'{agent_name}' is not an agent")
                # CRITICAL: Inject _session_service (same as CLI line 1252 and build_app line 354-357)
                # ALWAYS inject, even if attribute exists, to refresh the reference
                selected_agent._session_service = _session_service
                logger.debug(f"Injected SessionService into agent '{agent_name}' via /run endpoint")
            except KeyError:
                raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to get agent: {str(e)}")

        # Create LLM override if profile specified
        if llm_profile and config.llm_system and config.llm_system.profiles:
            if llm_profile not in config.llm_system.profiles:
                raise HTTPException(status_code=400, detail=f"LLM profile '{llm_profile}' not found")

            try:
                # Use factory function that properly handles batch mode
                from .llm.factory import create_llm_from_profile, resolve_llm_config_for_agent
                from .config.models import AgentConfig

                llm_override = create_llm_from_profile(
                    config=config,
                    llm_profile=llm_profile,
                    ssl_verify=getattr(config.network, "ssl_verify", None)
                )

                # Get profile info for status display
                temp_agent_config = AgentConfig(llm_profile=llm_profile)
                llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
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
                    from .servers.agent.server import Agent as _Agent
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
        from .servers.agent.server import Agent as _Agent
        if not isinstance(srv, _Agent):
            return {"error": "not an agent", "agent": agent_name}
        try:
            available, allowed_patterns, blocked_patterns = await srv.list_usable_tools()
            patterns = srv.agent_config.tools.allowed if srv.agent_config.tools else None
            return {
                "agent": agent_name, 
                "patterns": patterns or [], 
                "blocked_patterns": blocked_patterns or [],
                "available": sorted(available), 
                "effective": sorted(available)
            }
        except Exception as e:
            return {"agent": agent_name, "error": str(e)}

    @app.get("/agents/{agent_name}/allowed-tools/debug")
    async def get_agent_allowed_tools_debug(agent_name: str):
        """Return detailed pattern match diagnostics for an agent's allowed tools.

        Provides:
        - Phase 1: Server-level filtering (which servers pass the allow patterns)
        - Phase 2: Tool-level filtering (actual tools after expansion and blocked filtering)
        
        This shows the complete two-phase filtering process.
        """
        try:
            srv = _app_registry.get(agent_name)  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"Failed to get agent {agent_name}: {e}")
            return {"error": "agent not found", "agent": agent_name}
        from .servers.agent.server import Agent as _Agent
        if not isinstance(srv, _Agent):
            return {"error": "not an agent", "agent": agent_name}
        try:
            patterns = srv.agent_config.tools.allowed if srv.agent_config.tools else None
            available, allowed_patterns, blocked_patterns = await srv.list_usable_tools()
            
            # Phase 1: Server-level diagnostics (which servers matched which patterns)
            server_diagnostics = []
            if patterns:
                for tool in available:
                    matched_by = []
                    for pat in patterns:
                        if srv._is_tool_allowed(tool, [pat]):  # type: ignore[attr-defined]
                            matched_by.append(pat)
                    server_diagnostics.append({"server": tool, "matched_patterns": matched_by})
            else:
                server_diagnostics = [{"server": t, "matched_patterns": ["<implicit:all>"]} for t in available]
            
            # Phase 2: Get actual expanded and filtered tools via ToolSchemaBuilder
            final_tools = []
            tool_details = []
            try:
                from .servers.agent.tool_schema_builder import ToolSchemaBuilder
                
                # Create tool schema builder (same as used during chat)
                tool_builder = ToolSchemaBuilder(
                    agent_name=agent_name,
                    mcp_integration_manager=srv._mcp_integration_manager,
                    server_getter_func=srv._get_server_from_any_registry
                )
                
                # Build schemas with filtering applied
                tools_schema, tool_name_mapping, usable_tools, display_tools = await tool_builder.build_schemas(
                    available_tools=available,
                    allowed_patterns=allowed_patterns,
                    blocked_patterns=blocked_patterns
                )
                
                # Extract final tool names from schemas
                for schema in tools_schema:
                    if schema.get("type") == "function" and "function" in schema:
                        func = schema["function"]
                        tool_name = func.get("name", "")
                        server_name = tool_name_mapping.get(tool_name, "")
                        final_tools.append(tool_name)
                        tool_details.append({
                            "tool": tool_name,
                            "server": server_name,
                            "full_path": f"{server_name}/{tool_name}" if server_name else tool_name
                        })
            except Exception as e:
                logger.warning(f"Failed to build tool schemas for debug: {e}")
                tool_details = [{"error": str(e)}]
            
            return {
                "agent": agent_name,
                "allowed_patterns": patterns or [],
                "blocked_patterns": blocked_patterns or [],
                "phase1_server_filtering": {
                    "description": "Servers that passed allowed pattern matching",
                    "servers": server_diagnostics
                },
                "phase2_tool_filtering": {
                    "description": "Final tools after expansion and blocked pattern filtering",
                    "total_tools": len(final_tools),
                    "tools": tool_details
                }
            }
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
        from .servers.agent.server import Agent as _Agent
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

        # Register request ownership for status stream security
        _request_user_map[request_id] = user_id

        # Get agent with LLM override
        selected_agent, llm_override, llm_profile_info = _get_agent_with_overrides(agent_name, llm_profile)

        # Load existing session if session_id provided
        session_exists = False
        if session_id and _session_service:
            try:
                session_exists, msg_count = await _session_service.load_and_restore_session(
                    selected_agent, user_id, session_id
                )
            except SessionPermissionError as e:
                raise HTTPException(
                    status_code=403,
                    detail=f"Permission denied: {e}"
                )

        # CRITICAL: Always set/update session metadata (even for existing sessions)
        # This ensures user_id is available for tool execution AND respects llm_profile overrides
        if session_id:
            effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
            selected_agent._session_tracker.set_session_metadata(session_id, {
                "user_id": user_id,
                "agent_name": selected_agent.name,
                "llm_profile": effective_llm_profile
            })

        from .servers.agent.result_utils import collect_final_result

        # If no uploaded files, treat as text-only
        if not upload_files:
            if not task:
                raise HTTPException(status_code=400, detail="Missing 'task' in request")

            try:
                # Pass LLM override to collect_final_result
                result = await collect_final_result(
                    selected_agent, task,
                    request_id=request_id,
                    session_id=session_id,
                    llm_override=llm_override,
                    llm_profile_info_override=llm_profile_info
                )

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

                # Save session after execution (if session_id was provided or created)
                if session_id and _session_service:
                    effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                    was_new_session = not session_exists
                    await _session_service.save_session(
                        selected_agent,
                        user_id,
                        session_id,
                        selected_agent.name,
                        effective_llm_profile,
                        was_new_session
                    )
                    logger.debug(f"[SESSION_SAVE] Saved session {session_id} after /run (text-only)")

                return result
            finally:
                # Cleanup: Remove request_id from ownership map
                _request_user_map.pop(request_id, None)

        # Process uploaded files for multimodal input
        from .llm.capabilities import get_model_capabilities
        from .utils.multimodal_processor import (
            create_multimodal_message_extended,
            ImageProcessingError, 
            AudioProcessingError, 
            TextFileProcessingError,
            detect_file_type
        )
        import tempfile
        from pathlib import Path

        # Categorize uploaded files by type
        image_paths = []
        audio_paths = []
        text_paths = []
        temp_files = []
        
        try:
            temp_dir = Path(tempfile.mkdtemp())

            for upload_file in upload_files:
                temp_path = temp_dir / upload_file.filename
                with open(temp_path, 'wb') as f:
                    content = await upload_file.read()
                    f.write(content)
                temp_files.append(temp_path)
                
                # Categorize by file type
                file_type = detect_file_type(temp_path)
                if file_type == 'image':
                    image_paths.append(str(temp_path))
                elif file_type == 'audio':
                    audio_paths.append(str(temp_path))
                elif file_type == 'text':
                    text_paths.append(str(temp_path))
                else:
                    logger.warning("Unsupported file type for %s, skipping", upload_file.filename)
                    
                logger.debug("Saved uploaded file %s (%d bytes) -> %s [%s]", 
                           upload_file.filename, len(content), temp_path, file_type)

            # Get model name for capability checks (use override if provided)
            if llm_override and hasattr(llm_override, 'model'):
                model_name = llm_override.model
            else:
                model_name = selected_agent.llm.model if hasattr(selected_agent.llm, 'model') else None

            # Validate model supports images if we have any
            if image_paths and model_name:
                caps = get_model_capabilities(model_name)
                if not caps.image_input:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Model {model_name} does not support image input"
                    )

            # Validate model supports audio if we have any
            if audio_paths and model_name:
                caps = get_model_capabilities(model_name)
                if not caps.audio_input:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Model {model_name} does not support audio input"
                    )

            # Create multimodal message with all file types
            try:
                multimodal_msg = create_multimodal_message_extended(
                    text=task,
                    image_paths=image_paths if image_paths else None,
                    audio_paths=audio_paths if audio_paths else None,
                    text_file_paths=text_paths if text_paths else None
                )
                logger.info(
                    "Created multimodal message with %d image(s), %d audio(s), %d text file(s)", 
                    len(image_paths), len(audio_paths), len(text_paths)
                )
            except (ImageProcessingError, AudioProcessingError, TextFileProcessingError) as e:
                logger.exception("File processing failed while creating multimodal message: %s", e)
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

                        # CRITICAL: Always set/update session metadata (even for existing sessions)
                        # This ensures user_id is available for tool execution AND respects llm_profile overrides
                        if was_new_session or event_type == "start":
                            # Use override llm_profile if provided, otherwise agent's default
                            effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                            selected_agent._session_tracker.set_session_metadata(actual_session_id, {
                                "user_id": user_id,
                                "agent_name": selected_agent.name,
                                "llm_profile": effective_llm_profile
                            })
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
                        # Use actual agent name and effective llm_profile (respecting overrides)
                        effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                        await _session_service.save_session(
                            selected_agent,
                            user_id,
                            actual_session_id,
                            selected_agent.name,
                            effective_llm_profile,
                            was_new_session
                        )

                    # Cleanup: Remove request_id from ownership map
                    _request_user_map.pop(request_id, None)

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
        agent: Optional[str] = Query(default=None, alias="agent"),  # Accept both 'agent' and 'agent_name'
        agent_name: Optional[str] = Query(default=None),
        llm_profile: Optional[str] = Query(default=None)
    ):
        """Stream agent events for a task.

        Query parameters:
        - task: The task to execute
        - session_id: Optional session ID for conversation continuity
        - agent or agent_name: Optional agent to use instead of default
        - llm_profile: Optional LLM profile override (turbo, normal, think, etc.)

        Authentication:
        - If user is authenticated (JWT token or API key), sessions are saved to their account
        - If not authenticated, sessions use "anonymous" user_id
        """
        logger = logging.getLogger(__name__)
        request_id = short_id()

        # Prioritize 'agent' parameter over 'agent_name' for backwards compatibility
        agent_name = agent or agent_name

        # Get current user (optional authentication)
        current_user = await _get_current_user_optional(request)

        # Determine user_id for session management
        user_id = current_user.username if current_user else "anonymous"

        # Register request ownership for status stream security
        _request_user_map[request_id] = user_id

        logger.info("SSE /events connected, task=%s, request_id=%s, session_id=%s, agent=%s, llm_profile=%s, user_id=%s",
                   task, request_id, session_id, agent_name or "default", llm_profile or "default", user_id)

        # Get agent with LLM override
        selected_agent, llm_override, llm_profile_info = _get_agent_with_overrides(agent_name, llm_profile)

        # Load existing session if session_id provided
        session_exists = False
        if session_id and _session_service:
            try:
                session_exists, msg_count = await _session_service.load_and_restore_session(
                    selected_agent, user_id, session_id
                )
            except SessionPermissionError as e:
                raise HTTPException(
                    status_code=403,
                    detail=f"Permission denied: {e}"
                )

        # CRITICAL: Always set/update session metadata (even for existing sessions)
        # This ensures user_id is available for tool execution AND respects llm_profile overrides
        # load_and_restore_session sets metadata from disk, but we need to override with current request's llm_profile
        if session_id:
            # Use override llm_profile if provided, otherwise agent's default
            effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
            selected_agent._session_tracker.set_session_metadata(session_id, {
                "user_id": user_id,
                "agent_name": selected_agent.name,
                "llm_profile": effective_llm_profile
            })

        async def event_stream():
            yield ":ok\n\n"

            was_new_session = (session_id is None) or (not session_exists)
            actual_session_id = session_id

            # Keep-alive mechanism: Send periodic heartbeat comments to prevent connection timeout
            # Browser/proxy may drop connection if no data sent for 30-60 seconds during long LLM calls
            keepalive_interval = config.status.sse_keepalive_interval
            
            # Capture asyncio functions at closure level to avoid scoping issues
            import asyncio as _asyncio
            get_time = _asyncio.get_event_loop().time
            create_task = _asyncio.create_task
            sleep = _asyncio.sleep
            CancelledError = _asyncio.CancelledError
            Queue = _asyncio.Queue
            QueueEmpty = _asyncio.QueueEmpty
            
            last_event_time = get_time()
            
            # Event batching for status events (optimization to reduce overhead)
            status_batch = []
            last_batch_time = get_time()
            batch_interval = 0.3  # Send batches every 0.3 seconds max (increased from 50ms for better UX)
            batch_flush_queue = Queue()  # Queue for timer-triggered flushes
            
            async def send_keepalive_if_needed():
                """Send SSE comment to keep connection alive if no recent data"""
                nonlocal last_event_time
                now = get_time()
                if now - last_event_time > keepalive_interval:
                    last_event_time = now
                    return ":keepalive\n\n"
                return None
            
            async def flush_status_batch():
                """Send accumulated status events as batch"""
                nonlocal status_batch, last_batch_time, last_event_time
                if status_batch:
                    # Send batch as single SSE event with array
                    batch_payload = {"type": "status_batch", "events": status_batch}
                    yield f"data: {json.dumps(batch_payload, ensure_ascii=False)}\n\n"
                    status_batch = []
                    last_batch_time = get_time()
                    last_event_time = last_batch_time
            
            async def batch_timer():
                """Background timer to flush status batch after timeout"""
                nonlocal status_batch, last_batch_time
                try:
                    while True:
                        await sleep(batch_interval)
                        now = get_time()
                        # Check if batch has pending events and timeout elapsed
                        if status_batch and (now - last_batch_time) >= batch_interval:
                            # Flush directly - we're in a separate task
                            logger.debug(f"[BATCH] Timer triggered flush: {len(status_batch)} events")
                            await batch_flush_queue.put(True)
                except CancelledError:
                    pass
            
            # Start background timer
            timer_task = create_task(batch_timer())

            try:
                async for ev in selected_agent.run_events(task, request_id, actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                    # Check if background timer triggered flush FIRST (before processing event)
                    try:
                        while not batch_flush_queue.empty():
                            await batch_flush_queue.get()
                            if status_batch:
                                async for batch_msg in flush_status_batch():
                                    yield batch_msg
                    except QueueEmpty:
                        pass
                    
                    # Send keepalive before processing event (in case event processing is slow)
                    keepalive_msg = await send_keepalive_if_needed()
                    if keepalive_msg:
                        yield keepalive_msg
                    
                    # Update last event time since we're sending real data
                    last_event_time = get_time()
                    
                    logger.debug("SSE event: %s", ev.get("type"))

                    if ev.get("type") == "start" and ev.get("session_id"):
                        old_session_id = actual_session_id
                        actual_session_id = ev["session_id"]
                        logger.debug(f"[SESSION_SAVE] Session ID captured from start event: {old_session_id} -> {actual_session_id}")

                    # CRITICAL: Always set/update session metadata (even for existing sessions)
                    # This ensures user_id is available for tool execution AND respects llm_profile overrides
                    if was_new_session or ev.get("type") == "start":
                        effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                        selected_agent._session_tracker.set_session_metadata(actual_session_id, {
                            "user_id": user_id,
                            "agent_name": agent_name or "default",
                            "llm_profile": effective_llm_profile
                        })
                        logger.debug(f"[SESSION] Set metadata for session {actual_session_id}: user_id={user_id}")

                    if hasattr(ev, 'to_dict'):
                        payload = ev.to_dict()
                    else:
                        payload = ev

                    # Format output for final event and thinking_complete event
                    if selected_agent._hook_manager:
                        # Format final event summary to HTML
                        if ev.get("type") == "final" and ev.get("summary"):
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

                        # Also format thinking_complete content to HTML (for streaming)
                        elif ev.get("type") == "thinking_complete" and ev.get("assistant", {}).get("content"):
                            try:
                                formatted_content, content_format = await selected_agent._hook_manager.execute_format_output_hooks(
                                    output=payload["assistant"]["content"],
                                    request_id=request_id,
                                    session_id=actual_session_id or "unknown",
                                    output_format='html'
                                )
                                payload["assistant"]["content"] = formatted_content
                                payload["content_format"] = content_format
                            except Exception as e:
                                logger.error(f"[FORMAT_HTML] Failed to format thinking_complete to HTML: {e}", exc_info=True)

                    try:
                        # Send status events immediately (no batching for status)
                        # Batching caused delays during LLM calls when no events are generated
                        if payload.get("type") == "status":
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        else:
                            # Send non-status events immediately (but flush status batch first)
                            if status_batch:
                                async for batch_msg in flush_status_batch():
                                    yield batch_msg
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    except (TypeError, ValueError) as e:
                        logger.error("Failed to serialize event %s: %s", ev, e)
                        error_payload = {"type": "error", "message": f"Serialization error: {str(e)}"}
                        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
            except CancelledError:
                # Cancel background timer
                timer_task.cancel()
                try:
                    await timer_task
                except CancelledError:
                    pass
                
                # Flush any remaining status events before cancellation
                if status_batch:
                    try:
                        async for batch_msg in flush_status_batch():
                            yield batch_msg
                    except Exception:
                        pass
                
                # Stream was cancelled - send cancellation event to WebUI
                logger.info(f"Stream cancelled for request {request_id}, sending cancellation event")
                cancelled_payload = {"type": "cancelled", "request_id": request_id, "message": "Request cancelled by user"}
                try:
                    yield f"data: {json.dumps(cancelled_payload, ensure_ascii=False)}\n\n"
                except Exception as e:
                    logger.warning(f"Failed to send cancellation event: {e}")
                raise  # Re-raise to ensure proper cleanup
            except Exception as e:
                # Other errors - send error event
                logger.error(f"Error in event stream for request {request_id}: {e}", exc_info=True)
                error_payload = {"type": "error", "message": str(e), "request_id": request_id}
                try:
                    yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                except Exception:
                    pass  # Best effort
                raise
            finally:
                # Cancel background timer if still running
                if not timer_task.done():
                    timer_task.cancel()
                    try:
                        await timer_task
                    except CancelledError:
                        pass
                
                # ALWAYS persist session after streaming, even if client disconnects
                logger.debug(f"[SESSION_SAVE] Stream finished, persisting session {actual_session_id}")
                if actual_session_id and _session_service:
                    # Use actual agent name and effective llm_profile (respecting overrides)
                    effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                    await _session_service.save_session(
                        selected_agent,
                        user_id,
                        actual_session_id,
                        selected_agent.name,
                        effective_llm_profile,
                        was_new_session
                    )

                # Cleanup: Remove request_id from ownership map to prevent memory leak
                _request_user_map.pop(request_id, None)

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
        sid = agent._session_tracker.get_session_for_request(request_id)
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
        # Pre-create empty session in agent using the component API
        agent._session_tracker.set_session_messages(sid, [])
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
            if not agent._session_tracker.has_session(session_id):
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Session not found")

            # Run optimizer and summarizer
            actions = {"optimizer": False, "summarizer": False}

            if getattr(agent, 'token_optimizer', None):
                try:
                    # token_optimizer.optimize_messages expects messages list; retrieve session messages
                    msgs = agent._session_tracker.get_session_messages(session_id)
                    # Run optimizer
                    new_msgs = await agent.token_optimizer.optimize_messages(msgs)
                    # Persist optimized messages
                    agent._session_tracker.set_session_messages(session_id, new_msgs)
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
            sids = agent._session_tracker.get_all_session_ids()

            for sid in sids:
                actions = {"optimizer": False, "summarizer": False}
                try:
                    if getattr(agent, 'token_optimizer', None):
                        msgs = agent._session_tracker.get_session_messages(sid)
                        new_msgs = await agent.token_optimizer.optimize_messages(msgs)
                        agent._session_tracker.set_session_messages(sid, new_msgs)
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
                        from .auth.dependencies import get_current_user_from_token
                        from .auth.database import get_db

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
                        from .auth.database import get_db, verify_api_key

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
                # Get heartbeat interval from server_mode config (default 30s if not configured)
                heartbeat_interval = 30.0  # Default fallback
                if config.server_mode and hasattr(config.server_mode, 'sse_heartbeat_interval'):
                    heartbeat_interval = config.server_mode.sse_heartbeat_interval
                
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

                    # Wait before next heartbeat
                    await asyncio.sleep(heartbeat_interval)

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
            from .hooks import get_hook_registry
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
            from .hooks import get_hook_registry
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
            from .hooks import get_hook_registry
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
            from .hooks import get_hook_registry
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
        favicon_path = Path(__file__).parents[2] / "static" / "favicon.ico"
        if favicon_path.exists():
            return FileResponse(favicon_path)
        else:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Favicon not found")

    return app


def run() -> None:
    """Run the FastAPI server with proper configuration."""
    # Set UTF-8 environment for Windows compatibility
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

    # Build the application (loads config internally)
    cfg_path = str(Path(__file__).parents[2] / "config" / "config.yaml")
    app_obj = build_app(cfg_path)

    # Get config from global ConfigService (already loaded in build_app)
    config = _config_service.get_config()

    # Get server configuration
    host = os.getenv("HOST") or config.network.host or "127.0.0.1"
    port_env = os.getenv("PORT")
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
