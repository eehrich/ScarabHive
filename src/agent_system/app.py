"""The FastAPI application: ``build_app`` and ``run()``.

``build_app`` loads the configuration, bootstraps the tool servers, sets up
logging, authentication and the middleware, registers the lifespan, and
includes the routers. The app's own routes live in ``api/*_routes.py`` and
share an ``AppContext`` (api/app_context.py) that build_app stores on
``app.state.context``; the API's shared services are in ``app_state``.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import sys
import time
import types
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import app_state
from .api import (
    agent_routes,
    chat_routes,
    event_routes,
    hook_routes,
    page_routes,
    run_control_routes,
    run_routes,
    tool_routes,
)
from .api.app_context import AppContext
from .api.endpoints import router as api_router
from .ui.resources import STATIC_DIR, revalidated
from .ui.routes import router as ui_router
from .tools.base import ToolServerRegistry
from .utils.logging import setup_role_logging, unblock_console
from .services.initialization_service import apply_ssl_verify_to_environment
from .tools.integration import initialize_tools, shutdown_tools
from .llm.batch.initialization import init_batch_system, shutdown_batch_system, start_batch_queue_manager

# Import services
from .services import ConfigService, ToolServerService, ToolService
from .services.background_job_manager import get_background_job_manager


def __getattr__(name: str) -> Any:
    """The API's shared services under the names they had here (app_state.LEGACY_APP_NAMES).

    For code outside this repository that still reads them from this module;
    everything here reads ``app_state``. Read-only: see _AppModule.
    """
    legacy = app_state.LEGACY_APP_NAMES.get(name)
    if legacy is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(app_state, legacy)


class _AppModule(types.ModuleType):
    """This module, refusing to have the old names of the shared services set or deleted.

    A write could not reach anyone: every reader reads ``app_state``, so
    ``monkeypatch.setattr(app, "_session_service", x)`` would patch nothing, and
    the attribute it leaves behind would shadow ``__getattr__`` for the rest of
    the process. It fails loudly instead, naming where the service lives now.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        self._refuse(name)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        self._refuse(name)
        super().__delattr__(name)

    @staticmethod
    def _refuse(name: str) -> None:
        legacy = app_state.LEGACY_APP_NAMES.get(name)
        if legacy is not None:
            raise AttributeError(
                f"agent_system.app.{name} is read-only: the service lives in "
                f"agent_system.app_state.{legacy} -- set it there")


sys.modules[__name__].__class__ = _AppModule


# Security: Track request_id -> user_id mapping for status stream authorization.
# Owner is core.request_context (usable from agent layer without upward import);
# re-exported here under the historical name for existing importers.
from .core.request_context import (  # noqa: E402
    request_user_map as _request_user_map,  # noqa: F401 - re-export for tests/importers
)

# The routes moved to api/*_routes.py; these names are still imported from here.
from .api.chat_routes import MAX_CHAT_LINE  # noqa: E402,F401
from .api.run_start import (  # noqa: E402,F401
    asks_a_person as _asks_a_person,
    validate_client_request_id as _validate_client_request_id,
)
from .api.session_writes import resolve_agent_for_request  # noqa: E402,F401


#: Workers in the asyncio default executor — every `asyncio.to_thread` and
#: `run_in_executor(None, ...)` in this process shares them, and there are ~68
#: such call sites across the plugins (session persistence, context_engineer
#: compaction, memory, todo, terminal, the usage tracker's SQLite write).
#:
#: NOT derived from the CPU count. Python's default, min(32, cpu_count + 4), is
#: sized for CPU-bound work; these threads are almost all BLOCKING I/O and
#: spend their time waiting, not computing. On the production container
#: (2 cores) that formula yielded SIX workers for the whole API. With a dozen
#: sub-agents finishing calls at once, every post_llm_call hook queued for a
#: slot — and the usage write holds one for up to its 10 s SQLite busy_timeout,
#: twice the hook's own 5 s limit. Measured 2026-09-01: "Hook
#: 'context_usage_tracker.track_usage' timed out after 5.0s" while the CPU sat
#: at 94 % idle, and the writer admin panel answered in ten seconds.
ASYNC_EXECUTOR_MAX_WORKERS = 32


def _build_default_executor() -> ThreadPoolExecutor:
    """The process-wide executor for off-loop blocking work."""
    return ThreadPoolExecutor(
        max_workers=ASYNC_EXECUTOR_MAX_WORKERS,
        thread_name_prefix="app_asyncio",
    )


# Lives in llm.capabilities so the command-line entry points share it without
# importing FastAPI; imported here for this module and its tests.
from .llm.capabilities import capability_model_name  # noqa: E402,F401


static_path = STATIC_DIR

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


def _build_entry_agent(entry_name: str, config, registry, session_service):
    """The API's entry agent: servers.agent.entry.entry_agent -- the registered
    one, or a build from its MERGED server config -- and, when the name is no
    agent here, one from plugins.default_config: the API cannot exit over a
    config mistake the way the command line does.
    """
    from .servers.agent.server import Agent as CoreAgent
    from .servers.agent.entry import NotAnAgent, entry_agent

    try:
        return entry_agent(entry_name, config, registry, session_service)
    except NotAnAgent as e:
        logging.getLogger(__name__).warning(
            "%s\nThe API runs a generic entry agent from plugins.default_config.", e)
    # Read from the config that was passed in, not from the module-global
    # ConfigService: that global belongs to whichever build_app ran last.
    server_cfg = config.plugins.default_config if config.plugins else None

    if not server_cfg:
        from .config.models import ToolServerConfig, AgentConfig, ToolConfig
        logging.getLogger(__name__).warning(
            "No default_config found in plugins configuration, creating default ToolServerConfig with llm_profile='normal'"
        )
        server_cfg = ToolServerConfig(type="agent", enabled=True,
                            agent_config=AgentConfig(llm_profile="normal", tools=ToolConfig()))

    agent = CoreAgent(entry_name, config, server_cfg, registry, session_service=session_service)
    if entry_name in registry.list():
        # Only reachable when the name is taken by something that is not an
        # Agent -- registering here would drop that server out of the registry
        # for the rest of the process.
        logging.getLogger(__name__).error(
            "Entry agent '%s' collides with a registered %s -- keeping the registered server",
            entry_name, type(registry.get(entry_name)).__name__
        )
    else:
        registry.register(entry_name, agent)
    return agent


def _default_config_path() -> str:
    """The config the API loads when none is passed: AGENT_CONFIG_PATH, else the
    project's config/config.yaml -- the variable agent-cli and agent-run honour too."""
    from .paths import default_config_path

    return str(default_config_path())


def _gated_agent_names(config) -> list[str]:
    """The enabled servers whose MERGED config declares a role gate (metadata.min_role), sorted.

    Merged, because the gate is inherited along the ``type:`` chain like the
    rest of the metadata: an agent that sets nothing itself can still carry one.
    """
    from .config.settings import get_tool_server_config

    names = []
    for name, server in ((config.plugins.servers or {}).items() if config.plugins else ()):
        if not server.enabled:
            continue
        try:
            merged = get_tool_server_config(name, config)
        except Exception:  # noqa: BLE001 - a start-up warning must not stop the start
            continue
        if merged is not None and merged.metadata is not None and merged.metadata.min_role is not None:
            names.append(name)
    return sorted(names)


def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    # Initialize ConfigService and load configuration
    cfg_path = config_path or _default_config_path()

    # Setup early logging BEFORE config loading so YAML errors are captured
    # This ensures config parsing errors appear in the log file: the one the
    # shipped config names (logging.file_api), relative to the working
    # directory as setup_role_logging resolves it -- not the source tree, which
    # a server or test started elsewhere would otherwise write into.
    # A working directory the server cannot write to (a service unit without
    # WorkingDirectory) keeps the console only: a config that writes nothing
    # relative to it (logging off; auth off or an absolute auth.database_path)
    # would otherwise not start for want of a file it never asked for.
    early_handlers: list[logging.Handler] = [logging.StreamHandler()]
    early_log_file = Path("logs") / "api.log"
    try:
        early_log_file.parent.mkdir(parents=True, exist_ok=True)
        early_handlers.insert(0, logging.FileHandler(str(early_log_file), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",  # Match Uvicorn format
        handlers=early_handlers,
        force=True  # Override any existing config
    )
    early_logger = logging.getLogger(__name__)
    early_logger.debug(f"Early logging initialized, loading config from {cfg_path}")

    # Create ConfigService
    app_state.config_service = ConfigService()
    config = app_state.config_service.load_config(config_path=cfg_path)

    # Setup full logging via ConfigService (may reconfigure handlers)
    app_state.config_service.setup_logging()
    unblock_console()  # a console that stops reading must not stop the server
    
    # Get logger AFTER logging is configured
    logger = logging.getLogger(__name__)
    
    # Log startup marker for log analysis and debugging
    startup_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("═" * 80)
    logger.info(f"║  AgentSystem API Server STARTING - {startup_time}")
    logger.info(f"║  Version: {config.version}")
    logger.info(f"║  Config: {cfg_path}")
    logger.info("═" * 80)

    # Store config for lazy batch queue manager initialization
    # This allows LLMFactory to create the manager on first use
    from .llm.factory import set_batch_config
    set_batch_config(config)

    # Configure status bus with config values
    from .tools.status import status_bus
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
        logger.debug(f"tool system loaded with {plugin_count} plugins and {mcp_remote_count} remote servers")
    else:
        logger.warning("No plugins or mcp_servers configuration loaded")

    # Initialize centralized initialization service
    # This handles SessionManager, SessionService, and dependency injection
    from .services.initialization_service import InitializationService
    app_state.initialization_service = InitializationService(config)
    app_state.session_manager = app_state.initialization_service.session_manager
    app_state.session_service = app_state.initialization_service.session_service
    logger.info("InitializationService created (SessionManager and SessionService ready)")

    # Initialize tool integration
    async def _init_mcp_for_app(app: FastAPI):
        logger = logging.getLogger(__name__)
        logger.info("Starting tool integration initialization...")
        try:
            # Start the Batch Queue Manager (async operations: register providers, start background tasks)
            # The manager was already created and registered in build_app() sync section
            await start_batch_queue_manager(config, custom_logger=logger)
            
            tool_integration = await initialize_tools(config, app)
            app_state.tool_integration = tool_integration

            # Mark that bootstrap_servers() was already called by initialize_tools
            tool_integration.servers_bootstrapped = True

            # Initialize services
            app_state.tool_server_service = ToolServerService(tool_integration, config)
            app_state.tool_service = ToolService(tool_integration, config)

            # CRITICAL: Inject session_service into ALL agents in plugin_registry
            # This ensures hooks and tools can access session management
            # Must be done AFTER bootstrap_servers() in initialize_tools() created agents
            app_state.initialization_service.initialize_for_api(
                plugin_registry=tool_integration.plugin_registry,
            )

            # Store session manager in app state for dependency injection (after initialization)
            app.state.session_manager = app_state.session_manager
            logger.info("SessionManager stored in app.state for dependency injection")

            # Session archive: old conversation trees move to data/session_archive
            # (services/session_archive.py). Built here so the panel can reach it
            # whether or not the periodic sweep is enabled; the sweep itself needs
            # a running loop and starts in custom_lifespan below.
            from .core.session_presence import presence_for
            from .services.session_archive import SessionArchive

            async def _busy_sessions() -> set[str]:
                """Sessions this process runs right now -- never archive one of those."""
                return set(await get_background_job_manager().active_sessions())

            archive_config = config.session_archive
            app_state.session_archive = SessionArchive(
                app_state.session_manager,
                archive_path=archive_config.archive_path,
                retention_days=archive_config.retention_days,
                sweep_interval_hours=archive_config.sweep_interval_hours,
                first_sweep_delay_seconds=archive_config.first_sweep_delay_seconds,
                max_trees_per_sweep=archive_config.max_trees_per_sweep,
                presence=presence_for(config),
                busy_sessions=_busy_sessions,
            )
            app.state.session_archive = app_state.session_archive
            logger.info(
                "SessionArchive initialized (retention %d days, sweep %s)",
                archive_config.retention_days,
                "enabled" if archive_config.enabled else "disabled",
            )

            # Agent will be initialized later when needed
            # (requires agent instance from bootstrap_servers)

            # Make integration accessible to mcp module
            from .tools import integration as _tools_mod
            _tools_mod.tool_integration = tool_integration

            logger.info("tool integration and services initialized for API")

            # Apply plugin web capabilities with security
            from .plugins.web_adapter import plugin_web_registry
            plugin_web_registry.apply_to_app(app, auth_config=config.auth)
            logger.info("Plugin web capabilities applied to app with security enforcement")

        except Exception as e:
            logger.exception("Failed to initialize tool integration for API: %s", e)

    # FastAPI lifespan management
    @asynccontextmanager
    async def custom_lifespan(app: FastAPI):
        # Startup
        app_state.app_start_time = time.time()
        # The commit this process starts from: the System panel compares it
        # with the checked-out one to say a restart would deploy newer code.
        from .services.system_status import record_start
        await asyncio.to_thread(record_start)

        logger = logging.getLogger(__name__)
        
        # The default executor every `asyncio.to_thread` in this process lands
        # in — sized for BLOCKING I/O, see _build_default_executor.
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        executor = _build_default_executor()
        loop.set_default_executor(executor)
        logger.info("Configured asyncio default executor: %d workers, "
                    "thread_name_prefix='app_asyncio'",
                    ASYNC_EXECUTOR_MAX_WORKERS)

        # Create shutdown event for graceful SSE stream termination
        app_state.shutdown_event = _asyncio.Event()

        logger.info("Lifespan startup: Initializing tool integration...")
        await _init_mcp_for_app(app)
        logger.info("tool integration initialized during lifespan startup")

        # The archive sweep, now that there is a loop to run it in.
        archive = getattr(app.state, "session_archive", None)
        if archive is not None and config.session_archive.enabled:
            _asyncio.create_task(archive.sweep_loop())
            logger.info(
                "SessionArchive sweep loop started: every %.1f h, first in %.0f s",
                archive.sweep_interval_hours, archive.first_sweep_delay_seconds,
            )

        # Finished jobs keep only how their run ended once COMPLETED_JOB_TTL is past.
        # Nothing else starts this: a module-level lifespan that did was never the
        # app's, and every job kept its whole event buffer until the process ended.
        await get_background_job_manager().start_cleanup_task()

        # Log startup complete marker
        logger.info("═" * 80)
        logger.info("║  AgentSystem API Server READY - accepting connections")
        logger.info("═" * 80)
        
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
        logger.info("═" * 80)
        logger.info("║  AgentSystem API Server SHUTTING DOWN")
        logger.info("═" * 80)
        
        try:
            # Signal all SSE streams to terminate gracefully
            if app_state.shutdown_event:
                logger.info("Signaling SSE streams to terminate...")
                app_state.shutdown_event.set()
                # Give streams a brief moment to notice and exit
                await asyncio.sleep(0.1)

            await get_background_job_manager().stop_cleanup_task()

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
            
            await shutdown_tools()
            logger.info("tool integration shut down during lifespan")
            
            logger.info("═" * 80)
            logger.info("║  AgentSystem API Server STOPPED")
            logger.info("═" * 80)
        except Exception as e:
            logger.exception("Error shutting down tool integration during lifespan: %s", e)

    # Create FastAPI app
    app = FastAPI(title="Agent System", lifespan=custom_lifespan)

    # Initialize security enforcer (always created, respects auth.enabled)
    from .auth.enforcement import EndpointSecurityEnforcer
    _security_enforcer = EndpointSecurityEnforcer(config.auth)
    logger.info(f"Security enforcer initialized: auth.enabled={config.auth.enabled}, "
                f"anonymous_access={config.auth.anonymous_access.enabled}")

    # Mount static files. disable_cache: asked for again before each use -- the plugins'
    # static files follow the same rule (PluginWebRegistry.apply_to_app reads it here)
    app.state.revalidate_static = config.network.disable_cache
    if static_path.exists():
        static_files = StaticFiles(directory=str(static_path))
        app.mount("/static", revalidated(static_files) if app.state.revalidate_static else static_files, name="static")

    # Check for environment variable override
    env_level = os.getenv("AGENT_LOG_LEVEL")
    if env_level:
        logging.getLogger(__name__).info("Overriding log level from environment: %s", env_level)
        config.logging.level = env_level

    log_file = setup_role_logging(config.logging, "api")
    if log_file:
        logging.getLogger(__name__).info("Logging initialized, file=%s", log_file)
    apply_ssl_verify_to_environment(config)

    # The JWT signing key, before any tool server starts -- a key the server
    # refuses (auth.security.check_secret_key) stops it here, not after the
    # whole bootstrap, which a service manager repeats on every restart --
    # and after the role logging, so a published key's error reaches logs/api.log.
    if config.auth and config.auth.enabled:
        from .auth.security import set_jwt_config

        set_jwt_config(
            secret_key=config.auth.secret_key,
            algorithm=config.auth.algorithm,
            expire_minutes=config.auth.access_token_expire_minutes,
            refresh_expire_days=config.auth.refresh_token_expire_days,
            reject_default_key=config.auth.reject_default_secret_key,
        )

    # Bootstrap tool servers and plugin registry using InitializationService
    # This handles bootstrap_servers() and session_service injection
    # Note: Batch queue manager is created lazily by LLMFactory when first needed
    registry = ToolServerRegistry()
    if not app_state.tool_integration or not app_state.tool_integration.servers_bootstrapped:
        # Use InitializationService for consistent bootstrap + injection
        registry = app_state.initialization_service.bootstrap_and_inject(
            registry=registry,
            inject_sessions=True
        )
        # Mark as bootstrapped to prevent duplicate calls
        if app_state.tool_integration:
            app_state.tool_integration.servers_bootstrapped = True
        logging.getLogger(__name__).info("Bootstrapped servers using InitializationService")
    else:
        # Servers already bootstrapped by initialize_tools, just populate local registry
        # by copying from plugin_registry and inject sessions
        for server_name in app_state.tool_integration.plugin_registry.list_servers():
            server_adapter = app_state.tool_integration.plugin_registry.get_server(server_name)
            if server_adapter and hasattr(server_adapter, 'plugin_server'):
                registry.register(server_name, server_adapter.plugin_server)
        logging.getLogger(__name__).debug(f"Populated local registry with {len(registry.list())} servers from plugin_registry")

        # Inject session_service into local registry agents
        from .services.agent_injection import inject_session_service_into_agents
        inject_session_service_into_agents(registry, app_state.session_service)

    # Get entry agent from config
    entry_name = config.default_agent or 'agent'
    logging.getLogger(__name__).debug(f"Using entry agent: '{entry_name}'")

    # The registered agent, rewired to this registry and session service
    # (bootstrap built it without one), or a build. No server overrides are
    # applied on top: the Runtime built it from the MERGED server config,
    # which is where those overrides come from.
    agent = _build_entry_agent(entry_name, config, registry, app_state.session_service)

    # Wire the BackgroundJobManager with the registry + default agent
    # so cancel_job can walk every Agent-typed server that may own a
    # request (linear_book, v5b_story_designer, cover_artist, …) and
    # only fall back to the default agent when no registered server
    # matches. Single source of truth for "cancel a writer-side
    # request anywhere" — every endpoint (/api/requests/{rid}/cancel,
    # /admin/active-sessions/{rid}/cancel, writer-jobs propagation)
    # now delegates to one path.
    try:
        get_background_job_manager().set_agent_registry(
            registry=registry, default_agent=agent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "BackgroundJobManager registry wiring failed: %s — cancel "
            "endpoints will fall back to default-agent-only behaviour",
            exc,
        )

    # Store agent and registry in app state for dependency injection
    app.state.agent = agent
    app.state.tool_registry = registry
    app.state.config = config
    # The Runtime that built the registry: it knows every DECLARED server, not
    # just the built ones, and is the only place that builds one.
    app.state.runtime = app_state.initialization_service.runtime if app_state.initialization_service else None
    # For the deliberate config reload (POST /admin/reload-config, `agent-cli
    # reload`): the service + path let the endpoint re-parse the on-disk config.
    app.state.config_service = app_state.config_service
    app.state.config_path = cfg_path
    # The auth this process enforces -- the enforcer and the middleware were built
    # from it, and a reload (which replaces app.state.config) does not rebuild
    # them. Routers outside this function judge the agent role gate by it.
    app.state.auth_config = config.auth
    logger.info("Default agent, registry, and config stored in app.state for dependency injection")
    # What the app's own routes (api/*_routes.py) share -- this config, the entry
    # agent and the enforcer above -- reached through Depends(app_context).
    app.state.context = AppContext(app=app, config=config, agent=agent, config_path=cfg_path,
                                   security_enforcer=_security_enforcer)

    # The agent role gate (metadata.min_role) compares account roles; with auth
    # off there are none, and every gate stays open. Said once, at start.
    if not config.auth.enabled:
        gated_agents = _gated_agent_names(config)
        if gated_agents:
            logger.warning(
                "auth is disabled: the role gate (metadata.min_role) of %d agent(s) is not "
                "enforced, anyone who reaches the API may run them: %s",
                len(gated_agents), ", ".join(gated_agents))

    # Store registry globally
    app_state.app_registry = registry

    # Include API router
    app.include_router(api_router)
    app.include_router(ui_router)

    # Include debug/profiling router (available when AGENT_ENABLE_PROFILING=1)
    from .api.debug_endpoints import router as debug_router
    app.include_router(debug_router)

    # Add profiling middleware if enabled (must be done synchronously before app starts)
    from .utils.profiling import PROFILING_ENABLED, add_profiling_middleware
    if PROFILING_ENABLED:
        add_profiling_middleware(app)
        logger.info("Profiling middleware installed")

    # Initialize authentication system if enabled
    if config.auth and config.auth.enabled:
        logger.info("Multi-user authentication enabled, initializing auth system...")

        # Setup auth database and configuration
        from .auth.database import setup_database
        from .auth.middleware import configure_cors, configure_security_middleware
        from .auth.models import UserCreate, UserRole
        from pathlib import Path as AuthPath

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
                # The password goes to the console (docker compose logs) only, never through the logger:
                # logs/api.log kept it readable for as long as the file lived, and a log level above
                # WARNING dropped it before anyone saw it.
                logger.warning("Default admin %r created; its password is shown on the console, once",
                               config.auth.default_admin_username)
                print(f"\nDefault admin created -- save these credentials, the log file does not hold them:\n"
                      f"  Username: {config.auth.default_admin_username}\n"
                      f"  Password: {admin_password}\n", file=sys.stderr, flush=True)
            except Exception as e:
                from pydantic import ValidationError
                from .auth.models import validation_reasons
                # a configured password the model refuses would stand in pydantic's own text
                reason = validation_reasons(e) if isinstance(e, ValidationError) else e
                logger.error(f"Failed to create default admin user: {reason}")

        # Configure security middleware
        configure_security_middleware(
            app,
            auth_config=config.auth,
            rate_limit_enabled=config.auth.rate_limit_enabled,
            requests_per_minute=config.auth.requests_per_minute,
            security_headers_enabled=config.auth.security_headers_enabled,
            trusted_hosts=config.auth.trusted_hosts,
            audit_enabled=config.auth.endpoint_security.audit_enabled,
        )

        # Configure CORS if enabled. Registered AFTER the security middleware
        # on purpose: add_middleware prepends, so the last registration is the
        # OUTERMOST layer. CORS must wrap the security stack — otherwise
        # browser preflights (OPTIONS without Authorization) die with a 401
        # inside EndpointSecurityMiddleware and 401/403 responses carry no
        # CORS headers, which browsers report as an opaque "CORS error".
        if config.auth.cors_enabled:
            configure_cors(
                app,
                allow_origins=config.auth.cors_origins,
                allow_credentials=config.auth.cors_credentials,
                allow_methods=config.auth.cors_methods,
                allow_headers=config.auth.cors_headers,
            )

        # Include auth and admin routers
        from .api.auth_endpoints import router as auth_router
        from .api.admin_endpoints import router as admin_router
        from .api.session_endpoints import session_router

        app.include_router(auth_router)
        app.include_router(admin_router)
        app.include_router(session_router)

        # Note: SessionManager is stored in app.state during async lifespan startup

        logger.info("Authentication system initialized successfully")
    else:
        logger.info("Multi-user authentication is disabled")

    # The app's own routes, in the order they were always registered: FastAPI
    # matches in order (/agents/{name}/... before /agents/debug/{name}/...,
    # /sessions/{id}/... before /sessions/force_optimize).
    app.include_router(page_routes.health_router)
    app.include_router(agent_routes.router)
    app.include_router(run_routes.router)
    app.include_router(event_routes.router)
    app.include_router(run_control_routes.router)
    app.include_router(page_routes.router)
    app.include_router(tool_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(hook_routes.router)
    app.include_router(page_routes.favicon_router)

    # Last, so outermost: a remote client meets it before any other layer.
    from .auth.remote_paths import install as install_remote_path_guard
    install_remote_path_guard(app, config.network)

    return app


def run() -> None:
    """Run the FastAPI server with proper configuration."""
    from . import own_console
    if own_console.needed():  # Windows: off the terminal's console first, or a stuck one freezes the server
        raise SystemExit(own_console.relaunch("agent_system.app:run", []))

    # Set UTF-8 environment for Windows compatibility
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

    # Build the application (loads config internally)
    app_obj = build_app()

    # Get config from global ConfigService (already loaded in build_app)
    config = app_state.config_service.get_config()

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
        timeout_graceful_shutdown=5,  # as the systemd unit: an open stream does not hold up a stop
        use_colors=False,
        log_config=None  # Disable uvicorn's logging config to preserve our setup
    )


if __name__ == "__main__":
    # python -m agent_system.app runs this file as __main__, a second copy of the module beside the
    # agent_system.app the rest of the code imports: start the server the way agent-api does instead.
    from agent_system.own_console import api
    api()
