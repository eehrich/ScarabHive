from __future__ import annotations

import asyncio  # noqa: F401 - used in nested closures in event_stream() and lifespan
import types
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
import time
from urllib.parse import urlparse
from datetime import datetime  # noqa: F401 - used in health endpoint
from .utils.id import short_id
from agent_system.utils import yaml_io
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional

import anyio
import uvicorn
from fastapi import FastAPI, Request, Query, Header, HTTPException, Response, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile  # Use starlette's UploadFile for isinstance checks
from fastapi.staticfiles import StaticFiles

from .api.endpoints import router as api_router
from .ui.resources import STATIC_DIR, revalidated, ui_templates
from .ui.routes import router as ui_router
from .tools.base import ToolServerRegistry
# Module level: inside list_agents it would sit in a try/except that
# skips the server -- an import cycle would then empty the UI dropdown
# in silence instead of failing loud at start.
from .runtime import ServerView
from .utils.logging import setup_role_logging
from .services.initialization_service import apply_ssl_verify_to_environment
from .tools.status import get_status_metrics
from .tools.integration import initialize_tools, shutdown_tools
from .llm.batch.initialization import init_batch_system, shutdown_batch_system, start_batch_queue_manager

# Import services
from .services import ConfigService, ToolServerService, ToolService, AgentService
from .services.session_manager import SessionManager, SessionPermissionError
from .servers.agent.components.status_forwarding import in_line_with_a_live_run
from .core.session_presence import SessionBusy, forget_stop, presence_for
from .services.background_job_manager import (
    BackgroundJob,
    BackgroundJobManager,
    DuplicateRequestIdError,
    JobStatus,
    get_background_job_manager,
)


# Global registry for the tool endpoints
# (No _app_config next to it: a module global belongs to whichever build_app
# ran last, and a reload writes app.state.config -- _live_config() is the one
# source. The global had exactly one reader left, answering from process start.)
_app_registry: Optional[ToolServerRegistry] = None
_tool_integration = None

# Global services (initialized in build_app)
_config_service: Optional[ConfigService] = None
_tool_server_service: Optional[ToolServerService] = None
_tool_service: Optional[ToolService] = None
_agent_service: Optional[AgentService] = None
_initialization_service: Optional[Any] = None  # InitializationService
_session_manager: Optional[SessionManager] = None
_session_service: Optional[Any] = None  # SessionService, imported at runtime to avoid circular import
_session_archive: Optional[Any] = None  # SessionArchive, see services/session_archive.py


# Security: Track request_id -> user_id mapping for status stream authorization.
# Owner is core.request_context (usable from agent layer without upward import);
# re-exported here under the historical name for existing importers.
from .core.request_context import (  # noqa: E402
    get_request_user,
    register_request_user,
    request_user_map as _request_user_map,  # noqa: F401 - re-export for tests/importers
    release_request_user_tree,
)


#: Longest line /chat/resolve will look at. A chat line is a chat line; the
#: cap keeps a multi-megabyte paste from turning into CPU work on the event
#: loop before anyone has decided it is even a command.
MAX_CHAT_LINE = 100_000

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


async def _parse_json_body(request: Request) -> Any:
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


def _sse_response(stream, **kwargs) -> StreamingResponse:
    """A streaming response whose generator is closed as soon as its client has gone.

    Starlette cancels a response whose client went away but never closes its body
    generator. Caught at a yield -- in the middle of a send, or at a stream's
    farewell line -- the generator waited there for the garbage collector, and its
    ``finally`` with it: the job's reader count, the run's session save, its hold on
    the session, the request's owner entries.
    """
    async def close() -> None:
        # A coroutine function: handed `stream.aclose` itself, Starlette takes it for a plain
        # callable, calls it in a thread -- and the awaitable it returns is never awaited.
        await stream.aclose()

    return StreamingResponse(stream, background=BackgroundTask(close), **kwargs)

# Shutdown event for graceful stream termination
_shutdown_event: Optional[asyncio.Event] = None


async def resolve_agent_for_request(
    request_id: str,
    job_manager: BackgroundJobManager,
    registry: Optional[ToolServerRegistry],
    default_agent: Any,
    agent_name: Optional[str] = None,
) -> Any:
    """Resolve the agent instance that owns an active/recent request.

    Runs started with an agent_name execute on that registered agent; their
    per-request state (mid-run append queue, request→session mapping) lives on
    that instance, not on the global default agent. Falls back to the default
    agent when the job is unknown, names the default placeholder, or its agent
    cannot be resolved. ``agent_name`` names the agent for a run that has no job
    (a /run with files, a sub-agent's session), when the caller knows it.
    """
    try:
        job = await job_manager.get_job(request_id)
    except Exception:
        job = None
    agent_name = getattr(job, "agent_name", None) or agent_name
    if registry is not None and agent_name and agent_name not in ("default", default_agent.name):
        try:
            candidate = registry.get(agent_name)
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not resolve agent '%s' for request %s, using default agent",
                agent_name, request_id,
            )
            return default_agent
        from .servers.agent.server import Agent as _Agent
        if isinstance(candidate, _Agent):
            return candidate
    return default_agent


async def format_answer_fields(payload: dict, selected_agent, request_id: str, session_id: str) -> dict:
    """The event with the answer it carries rendered to HTML: a final's summary, a finished step's content.

    A copy where anything changes, never the event itself: every reader of a job is sent
    the same event object, and rendered in place the next reader would render the HTML
    again -- and the run's own caller (POST /run) holds it too.
    """
    kind = payload.get("type")
    if kind == "final" and payload.get("summary"):
        try:
            formatted_summary, content_format = await selected_agent._hook_manager.execute_format_output_hooks(
                output=payload["summary"],
                request_id=request_id,
                session_id=session_id,
                output_format='html'
            )
            return {**payload, "summary": formatted_summary, "content_format": content_format}
        except Exception as e:
            logging.getLogger(__name__).error(f"[FORMAT_HTML] Failed to format summary to HTML: {e}", exc_info=True)

    # Also format thinking_complete content to HTML (for streaming)
    elif kind == "thinking_complete" and payload.get("assistant", {}).get("content"):
        try:
            formatted_content, content_format = await selected_agent._hook_manager.execute_format_output_hooks(
                output=payload["assistant"]["content"],
                request_id=request_id,
                session_id=session_id,
                output_format='html'
            )
            return {**payload, "assistant": {**payload["assistant"], "content": formatted_content},
                    "content_format": content_format}
        except Exception as e:
            logging.getLogger(__name__).error(f"[FORMAT_HTML] Failed to format thinking_complete to HTML: {e}", exc_info=True)
    return payload


# Lives in llm.capabilities so the command-line entry points share it without
# importing FastAPI; imported here for this module and its tests.
from .llm.capabilities import capability_model_name  # noqa: E402,F401


templates = ui_templates()
static_path = STATIC_DIR

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


async def _validate_client_request_id(client_request_id: str) -> str:
    """Guard a caller-supplied request_id before adopting it for a NEW run.

    Shared by POST /run and GET/POST /events (non-reconnect path). The id
    flows into log lines, ownership maps and cancellation-token keys, so:
      - format whitelist (8-64 url-safe chars) → 400;
      - 409 when the id is already live ANYWHERE (BackgroundJob, any
        registry agent, default agent) — the duplicate-dispatch guard: a
        caller retry that fires while the original run is still grinding
        gets a clean 409 instead of silently starting a second run;
      - 409 when the id and a live run's id are in line, one being the other
        followed by `_…`: that is how the server names a run's sub-runs, and
        every stream above a run takes what starts with its id and `_`. Such
        an id would read as a live run's sub-run and be handed its events, or
        make a live run's sub-runs read as its own. (A job dispatched again
        under its id while a sub-agent of its last attempt still works waits
        for that one, as it waits for a run of its own id.)
    """
    rid = str(client_request_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", rid):
        raise HTTPException(
            status_code=400,
            detail="invalid request_id: expected 8-64 chars [A-Za-z0-9_-]",
        )
    if in_line_with_a_live_run(rid):
        raise HTTPException(
            status_code=409,
            detail=f"request_id {rid} is in line with a run in flight (one extends the other by _)",
        )
    if await get_background_job_manager().is_request_active_anywhere(rid):
        raise HTTPException(
            status_code=409,
            detail=(
                f"request_id {rid} is already active — "
                "the original run is still in flight"
            ),
        )
    # A new run under an id that was stopped before (writer_jobs dispatches a run
    # again under its id): that stop was the earlier run's (core/session_presence.py).
    # Here and nowhere else -- the ids every other caller mints are new, and a stop
    # noted before their run registers is theirs.
    forget_stop(rid)
    return rid


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


def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    from pathlib import Path

    # Initialize ConfigService and load configuration
    if not config_path:
        cfg_path = str(Path(__file__).parents[2] / "config" / "config.yaml")
    else:
        cfg_path = config_path

    # Setup early logging BEFORE config loading so YAML errors are captured
    # This ensures config parsing errors appear in the log file
    early_log_file = Path(__file__).parents[2] / "logs" / "api.log"
    early_log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",  # Match Uvicorn format
        handlers=[
            logging.FileHandler(str(early_log_file), encoding="utf-8"),
            logging.StreamHandler()
        ],
        force=True  # Override any existing config
    )
    early_logger = logging.getLogger(__name__)
    early_logger.debug(f"Early logging initialized, loading config from {cfg_path}")

    # Create ConfigService
    global _config_service
    _config_service = ConfigService()
    config = _config_service.load_config(config_path=cfg_path)

    # Setup full logging via ConfigService (may reconfigure handlers)
    _config_service.setup_logging()
    
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
    global _initialization_service, _session_manager, _session_service, _session_archive
    _initialization_service = InitializationService(config)
    _session_manager = _initialization_service.session_manager
    _session_service = _initialization_service.session_service
    logger.info("InitializationService created (SessionManager and SessionService ready)")

    # Initialize tool integration
    async def _init_mcp_for_app(app: FastAPI):
        global _tool_integration, _tool_server_service, _tool_service, _agent_service
        logger = logging.getLogger(__name__)
        logger.info("Starting tool integration initialization...")
        try:
            # Start the Batch Queue Manager (async operations: register providers, start background tasks)
            # The manager was already created and registered in build_app() sync section
            await start_batch_queue_manager(config, custom_logger=logger)
            
            tool_integration = await initialize_tools(config, app)
            _tool_integration = tool_integration

            # Mark that bootstrap_servers() was already called by initialize_tools
            tool_integration.servers_bootstrapped = True

            # Initialize services
            _tool_server_service = ToolServerService(tool_integration, config)
            _tool_service = ToolService(tool_integration, config)

            # CRITICAL: Inject session_service into ALL agents in plugin_registry
            # This ensures hooks and tools can access session management
            # Must be done AFTER bootstrap_servers() in initialize_tools() created agents
            _initialization_service.initialize_for_api(
                plugin_registry=tool_integration.plugin_registry,
            )

            # Store session manager in app state for dependency injection (after initialization)
            app.state.session_manager = _session_manager
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
            _session_archive = SessionArchive(
                _session_manager,
                archive_path=archive_config.archive_path,
                retention_days=archive_config.retention_days,
                sweep_interval_hours=archive_config.sweep_interval_hours,
                first_sweep_delay_seconds=archive_config.first_sweep_delay_seconds,
                max_trees_per_sweep=archive_config.max_trees_per_sweep,
                presence=presence_for(config),
                busy_sessions=_busy_sessions,
            )
            app.state.session_archive = _session_archive
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
        global _app_start_time, _shutdown_event
        _app_start_time = time.time()
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
        _shutdown_event = _asyncio.Event()

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
            if _shutdown_event:
                logger.info("Signaling SSE streams to terminate...")
                _shutdown_event.set()
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

    def _live_config():
        """The config THIS app currently runs on.

        POST /admin/reload-config replaces app.state.config (admin_endpoints),
        while every handler below closed over the config build_app started
        with. Anything that answers a question ABOUT the configuration --
        profiles, defaults, an override -- has to read the live one, or it
        keeps answering from the state at process start. Per app on purpose:
        a module-level ConfigService is overwritten by the next build_app and
        would make one app answer with another app's config.
        """
        return getattr(app.state, "config", None) or config

    async def _claim_session(target_agent: Any, sid: Optional[str], user_id: str,
                             force: bool) -> tuple[Optional[str], Optional[str]]:
        """Take a stored session for a run of this process, and bring the copy
        in memory up to date. Returns (refusal, held): the refusal goes to the
        client, ``held`` names what _let_go has to release afterwards.

        Session presence (core/session_presence.py) refuses a session another
        process runs -- both would write the conversation and the last save
        would win; ``force`` runs it anyway, for the lock of a process that
        hangs. The session is loaded before this (ownership, metadata), so
        whatever another process wrote in between is read again here.
        """
        held = None
        if not sid:
            return None, None
        # A session deleted in this process takes no run: nothing writes it again, so what the run answers would
        # be lost without a word (force does not change that).
        if _session_service and _session_service.session_manager and _session_service.session_manager.is_deleted(sid):
            return f"Session {sid} has been deleted", None
        presence = presence_for(getattr(target_agent, "system_config", None))
        if presence is not None:
            try:
                if presence.hold(sid, user_id, target_agent.name):
                    held = sid
            except SessionBusy as busy:
                if not force:
                    return f"{busy}. Send force=true if its lock is a leftover.", None
                logging.getLogger(__name__).warning("%s; running it anyway (force=true)", busy)
        try:
            await _bring_the_copy_up_to_date(target_agent, user_id, sid)
        except BaseException:
            # The hold belongs to this function until it hands it back, and
            # nothing else would let it go: in this process it would outlive the
            # request and refuse every later run of that session.
            _let_go(target_agent, held, user_id)
            raise
        return None, held

    async def _bring_the_copy_up_to_date(target_agent: Any, user_id: str, sid: str) -> None:
        """Re-read a session another process continued while this one had it
        loaded but not yet held.

        SessionManager says whether the file moved since this process last wrote
        it or read it into a tracker -- not since the web UI last showed it, a
        load that puts nothing in memory. Where it has no stamp -- it is bounded with its cache -- the longer
        conversation wins: re-reading unasked undoes a run of this process whose
        save is still to come, and that run's answer is nowhere else.
        """
        if not _session_service:
            return
        tracker = getattr(target_agent, "_session_tracker", None)
        if tracker is not None and tracker.check_session_locked(sid)[0]:
            # A run of this agent has it (its session lock): the copy in memory IS
            # that run's, and the run asking here is refused at that lock anyway
            # -- read back, the running turn lost what it had not saved yet, and
            # its metadata named the asker (as open_for_run leaves it, in_use).
            return
        manager = _session_service.session_manager
        changed = manager.changed_on_disk(user_id, sid)
        if changed is None:
            tracker = getattr(target_agent, "_session_tracker", None)
            in_memory = len(tracker.get_session_messages(sid) or []) if tracker else 0
            try:
                stored = await manager.load_session(user_id, sid)
            except Exception:
                # No readable session there; a permission error comes back out
                # of load_and_restore_session below, where it belongs.
                stored = {}
            changed = len(stored.get("messages") or []) >= in_memory
        if changed:
            await _session_service.load_and_restore_session(target_agent, user_id, sid)

    def _hold_fresh_session(target_agent: Any, sid: str, user_id: str) -> Optional[str]:
        """Hold a session the running request just created: nothing to re-read
        (its conversation lives in this process) and no one to refuse."""
        presence = presence_for(getattr(target_agent, "system_config", None))
        if presence is None:
            return None
        try:
            return sid if presence.hold(sid, user_id, target_agent.name) else None
        except SessionBusy as busy:
            logging.getLogger(__name__).warning("%s; this run keeps it unheld", busy)
            return None

    def _carry_title(target_agent: Any, event: dict, title: Optional[str]) -> None:
        """A title the caller gave the session a run starts: handed to the run
        at its start event -- the one that names a new session -- and written
        by its first save (SessionService), as agent-cli writes a /title typed
        before the first message. Only text is a title: a JSON number or a
        file part of that name is not one."""
        if not isinstance(title, str) or not title.strip():
            return
        if event.get("type") == "start" and event.get("session_id"):
            target_agent._session_tracker.carry_title(event["session_id"], title.strip())

    async def _mirror_run_as_job(request_id: str, user_id: str, agent_name: str,
                                 session_id: Optional[str], llm_profile: Optional[str]):
        """A BackgroundJob that shows a run somebody else collects, or None.

        POST /run answers its caller from collect_final_result and made no job, so
        nothing could follow such a run: not the chat (it attaches to a session's
        job), not a second page. The job takes the events as they pass (``put``);
        the caller's answer does not go through it.

        A job running under the id already is refused with a 409, as /events does:
        _validate_client_request_id cannot rule out a request that got past it at
        the same time, and two agents under one id cannot be told apart.
        """
        feed: asyncio.Queue = asyncio.Queue()

        async def relay():
            while (event := await feed.get()) is not None:
                yield event

        try:
            await get_background_job_manager().create_job(
                request_id=request_id, user_id=user_id, agent_name=agent_name,
                session_id=session_id, agent_runner=relay, llm_profile=llm_profile,
                mirror=True)
        except DuplicateRequestIdError:
            logger.warning("[RUN] refused duplicate request_id=%s -- a job is already running under it", request_id)
            raise HTTPException(status_code=409, detail="request_id is already running")

        return types.SimpleNamespace(put=feed.put_nowait, close=lambda: feed.put_nowait(None))

    def _refused_at_the_lock(event: dict) -> bool:
        """Whether a run event says the run was refused at the agent's session lock (Agent.run_events): another
        run of this process has the session, and nothing of this request may be saved to it."""
        from .servers.agent.server import SESSION_LOCKED

        return event.get("type") == "error" and event.get("error_type") == SESSION_LOCKED

    def _let_go(target_agent: Any, sid: Optional[str], user_id: str) -> None:
        """Let go of a held session; input that came in for it wakes it."""
        if not sid:
            return
        presence = presence_for(getattr(target_agent, "system_config", None))
        if presence is not None:
            presence.release(sid, user_id)

    # Initialize security enforcer (always created, respects auth.enabled)
    from .auth.enforcement import EndpointSecurityEnforcer, AnonymousUser
    _security_enforcer = EndpointSecurityEnforcer(config.auth)
    logger.info(f"Security enforcer initialized: auth.enabled={config.auth.enabled}, "
                f"anonymous_access={config.auth.anonymous_access.enabled}")

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
            from .auth.dependencies import get_current_user as get_user_dep
            from .auth.database import get_db
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

    async def _enforce_endpoint_security(request: Request) -> Any:
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
        return await _security_enforcer.enforce_endpoint_security(
            request,
            _get_current_user_optional
        )

    def _validate_llm_access(user: Any, is_llm_request: bool = False) -> None:
        """Validate that user is allowed to make LLM requests.
        
        Args:
            user: User object (User or AnonymousUser)
            is_llm_request: Whether this is an LLM API call
        
        Raises:
            HTTPException: 403 if user not allowed LLM access
        """
        if not is_llm_request:
            return
        
        if not config.auth.enabled:
            return
        
        llm_security = config.auth.llm_security
        
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

    # Bootstrap tool servers and plugin registry using InitializationService
    # This handles bootstrap_servers() and session_service injection
    # Note: Batch queue manager is created lazily by LLMFactory when first needed
    registry = ToolServerRegistry()
    if not _tool_integration or not _tool_integration.servers_bootstrapped:
        # Use InitializationService for consistent bootstrap + injection
        registry = _initialization_service.bootstrap_and_inject(
            registry=registry,
            inject_sessions=True
        )
        # Mark as bootstrapped to prevent duplicate calls
        if _tool_integration:
            _tool_integration.servers_bootstrapped = True
        logging.getLogger(__name__).info("Bootstrapped servers using InitializationService")
    else:
        # Servers already bootstrapped by initialize_tools, just populate local registry
        # by copying from plugin_registry and inject sessions
        for server_name in _tool_integration.plugin_registry.list_servers():
            server_adapter = _tool_integration.plugin_registry.get_server(server_name)
            if server_adapter and hasattr(server_adapter, 'plugin_server'):
                registry.register(server_name, server_adapter.plugin_server)
        logging.getLogger(__name__).debug(f"Populated local registry with {len(registry.list())} servers from plugin_registry")

        # Inject session_service into local registry agents
        from .services.agent_injection import inject_session_service_into_agents
        inject_session_service_into_agents(registry, _session_service)

    # Get entry agent from config
    entry_name = config.default_agent or 'agent'
    logging.getLogger(__name__).debug(f"Using entry agent: '{entry_name}'")

    # The registered agent, rewired to this registry and session service
    # (bootstrap built it without one), or a build. No server overrides are
    # applied on top: the Runtime built it from the MERGED server config,
    # which is where those overrides come from.
    agent = _build_entry_agent(entry_name, config, registry, _session_service)

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
    app.state.runtime = _initialization_service.runtime if _initialization_service else None
    # For the deliberate config reload (POST /admin/reload-config, `agent-cli
    # reload`): the service + path let the endpoint re-parse the on-disk config.
    app.state.config_service = _config_service
    app.state.config_path = cfg_path
    logger.info("Default agent, registry, and config stored in app.state for dependency injection")

    # Store registry globally
    global _app_registry
    _app_registry = registry

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

    # Health check endpoint
    @app.get("/health")
    def health():
        global _app_start_time

        uptime_seconds = time.time() - _app_start_time if _app_start_time else 0

        agent_config = {}
        try:
            agent_config_path = Path(__file__).parents[2] / "config" / "config.yaml"
            with open(agent_config_path, 'r', encoding='utf-8') as f:
                agent_config = yaml_io.safe_load(f) or {}
        except Exception as e:
            logger.debug(f"Failed to load config for health check: {e}")

        # Get Python version
        import sys
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        
        # Get key package versions
        packages = {}
        try:
            import fastapi
            import anthropic
            import openai
            import uvicorn
            import pydantic
            
            packages["fastapi"] = getattr(fastapi, "__version__", "unknown")
            packages["anthropic"] = getattr(anthropic, "__version__", "unknown")
            packages["openai"] = getattr(openai, "__version__", "unknown")
            packages["uvicorn"] = getattr(uvicorn, "__version__", "unknown")
            packages["pydantic"] = getattr(pydantic, "__version__", "unknown")
            
            # Try to get Google Gemini version
            try:
                from google import genai
                packages["google-genai"] = getattr(genai, "__version__", "unknown")
            except ImportError:
                pass
                
            # Try to get httpx version
            try:
                import httpx
                packages["httpx"] = getattr(httpx, "__version__", "unknown")
            except ImportError:
                pass
            
            # Try to get chromadb version
            try:
                import chromadb
                packages["chromadb"] = getattr(chromadb, "__version__", "unknown")
            except ImportError:
                pass
                
        except Exception as e:
            logger.debug(f"Failed to get package versions: {e}")

        return {
            "status": "ok",
            "version": agent_config.get("version", "unknown"),
            "name": agent_config.get("name", "AgentSystem"),
            "uptime_seconds": round(uptime_seconds, 2),
            "timestamp": datetime.now().isoformat(),
            "python_version": python_version,
            "packages": packages
        }

    async def _format_and_yield_event(ev: dict, selected_agent, request_id: str, session_id: str) -> str:
        """An event as an SSE data line, its answer rendered to HTML."""
        payload = ev.to_dict() if hasattr(ev, 'to_dict') else ev

        if selected_agent._hook_manager:
            payload = await format_answer_fields(payload, selected_agent, request_id, session_id)
            # A sub-agent's answer is the same text and is shown the same way.
            if payload.get("type") == "sub_run" and isinstance(payload.get("event"), dict):
                payload = {**payload, "event": await format_answer_fields(
                    payload["event"], selected_agent, request_id, session_id)}

        try:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialize event %s: %s", ev, e)
            error_payload = {"type": "error", "message": f"Serialization error: {str(e)}"}
            return f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

    async def _open_session_for_run(selected_agent, user_id: str, session_id: Optional[str],
                                    llm_profile: Optional[str]) -> bool:
        """SessionService.open_for_run for /run and /events; whether the session
        existed. 403 for another user's session; without an id there is nothing
        to open -- the run creates its session and names it in the start event.

        While a run of this process has the session, the tracker holds that
        run's state -- a session it has not saved yet reads as new, and must
        not be reset under it (in_use). Another user's such run answers 403:
        the metadata this writes would hand them its session.
        """
        if not session_id:
            return False
        running = (await get_background_job_manager().active_sessions()).get(session_id)
        if running is not None and running.get("user_id") not in (None, user_id):
            raise HTTPException(status_code=403,
                                detail=f"Permission denied: session {session_id} belongs to another user")
        try:
            return await _session_service.open_for_run(
                selected_agent, user_id, session_id,
                llm_profile or selected_agent.agent_config.default_llm_profile,
                in_use=running is not None)
        except SessionPermissionError as e:
            raise HTTPException(status_code=403, detail=f"Permission denied: {e}")

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

        # Override agent if specified. When the caller passes an
        # EXPLICIT agent_name we must NOT silently fall back to the
        # default agent — that turns an "agent name typo" or a "plugin
        # not loaded" bug into a chat_agent run that returns HTTP 200,
        # which downstream batch dispatchers (writer-jobs book_generation
        # / fix) treat as success. Strict 404 instead, so the caller's
        # job-row goes 'failed' with a useful error.
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
                logger.warning(
                    "Agent '%s' not found — returning 404 (no silent fallback)",
                    agent_name,
                )
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"agent_not_found:{agent_name}. "
                        "Check the plugin name (registered tool server name, not "
                        "the agent yaml filename) and that the plugin is loaded."
                    ),
                )
            except HTTPException:
                # The deliberate 400 ("'x' is not an agent") must keep its
                # status — the generic handler below turned it into a 500.
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to get agent: {str(e)}")

        # Create LLM override if profile specified. Resolved against the LIVE
        # config -- a profile added by a reload was "not found" here and fell
        # back to the default profile.
        live = _live_config()
        if llm_profile and live.llm_system and live.llm_system.profiles:
            if llm_profile not in live.llm_system.profiles:
                # LLM profile not found - fallback to default profile
                default_profile = live.llm_system.default_profile
                logger.warning(f"LLM profile '{llm_profile}' not found, falling back to default profile '{default_profile}'")
                llm_profile = default_profile

            try:
                from .llm.factory import override_for_profile
                llm_override, llm_profile_info = override_for_profile(
                    live, getattr(selected_agent, "agent_config", None), llm_profile)
            except Exception as e:
                logger.error(f"Failed to create LLM override: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to apply LLM profile: {str(e)}")

        return selected_agent, llm_override, llm_profile_info

    @app.get("/admin/config")
    def get_config():
        """Return the full system configuration (admin only).

        Live, not the start state: the endpoint whose whole job is to show the
        configuration must not report the state before the reload that just
        succeeded.
        """
        return _live_config().model_dump()

    @app.get("/agents")
    def list_agents(response: Response):
        """List registered agent-like servers that are publicly visible (UI dropdown).

        Returns agents with _tool_public=True OR agents without _tool_public attribute (backward compat).
        Agents with visibility='tool' or 'private' (_tool_public=False) are excluded.
        """
        # Without this the browser may serve the list from its HTTP cache on a
        # normal reload — newly registered agents then only appear after a
        # force reload (observed: agent missing from the dropdown until Ctrl+F5).
        response.headers["Cache-Control"] = "no-store"
        agents = []
        try:
            for name in _app_registry.list():  # type: ignore[attr-defined]
                try:
                    # describe() first: it answers "is this an agent, is it
                    # public" from the instance when there is one and from the
                    # declaration otherwise -- no build to read two flags.
                    #
                    # isinstance, not "is not None": a Mock registry answers
                    # describe() with a truthy Mock whose attributes are all
                    # truthy, which would list every server as a public agent.
                    view = _app_registry.describe(name)  # type: ignore[attr-defined]
                    if isinstance(view, ServerView):
                        if view.is_agent and view.tool_public:
                            agents.append(name)
                        elif view.is_agent:
                            logger.debug(f"Skipping agent '{name}' in UI list (_tool_public=False)")
                        continue

                    # Unbound registry, or a declaration that cannot answer for
                    # its instance: the instance is the only source.
                    srv = _app_registry.get(name)  # type: ignore[attr-defined]
                    from .servers.agent.server import Agent as _Agent
                    if isinstance(srv, _Agent):
                        # Filter by _tool_public flag (visibility control)
                        # Default to True if attribute doesn't exist (backward compatibility with plugin agents)
                        if hasattr(srv, '_tool_public'):
                            if not srv._tool_public:
                                logger.debug(f"Skipping agent '{name}' in UI list (_tool_public=False)")
                                continue
                        # else: No _tool_public attribute → show in UI (backward compat)
                        agents.append(name)
                except Exception as e:
                    logger.debug(f"Failed to check agent {name}: {e}")
                    continue
        except Exception as e:
            logger.debug(f"Failed to list agents: {e}")
        # The agent /run actually uses without agent_name is this object --
        # not config.default_agent, which a reload can move without moving
        # the entry agent with it.
        return {"agents": sorted(agents), "details": _agent_details(sorted(agents)), "default": agent.name}

    def _hostname(base_url: Optional[str]) -> Optional[str]:
        """The host a model's requests go to; a malformed URL ("http://[fe80::1") names none rather than ending the list."""
        try:
            return urlparse(base_url).hostname if base_url else None
        except ValueError:
            return None

    def _agent_details(names: list[str]) -> list[dict[str, Any]]:
        """What the agent picker filters and groups by: description, category and tags from the merged config."""
        from .config.settings import get_tool_server_config
        live = _live_config()
        details = []
        for name in names:
            try:
                cfg = get_tool_server_config(name, live)
            except Exception as e:
                logger.debug(f"No config details for agent {name}: {e}")
                cfg = None
            meta = cfg.metadata if cfg else None
            details.append({
                "name": name,
                "description": cfg.description if cfg else None,
                "category": meta.category if meta else None,
                "tags": (meta.tags or []) if meta else [],
            })
        return details

    @app.get("/llm/profiles")
    def list_llm_profiles(response: Response):
        """List available LLM profiles with their descriptions."""
        response.headers["Cache-Control"] = "no-store"  # same staleness class as /agents
        profiles = []
        default_profile = None
        # Live config: /run accepts a profile a reload added, so the list the
        # UI picks from must know it too.
        live = _live_config()
        try:
            if live.llm_system and live.llm_system.profiles:
                for profile_name, profile_config in live.llm_system.profiles.items():
                    model = live.llm_system.models.get(profile_config.model_ref)
                    profiles.append({
                        "name": profile_name,
                        "model_ref": profile_config.model_ref,
                        # the picker groups by route (host, else provider) or model and finds a profile by either
                        "provider": model.provider if model else None,
                        "model": model.model if model else None,
                        "host": _hostname(model.base_url) if model else None,
                        "description": profile_config.description or profile_name,
                        "max_steps": profile_config.max_steps
                    })
                default_profile = live.llm_system.default_profile
        except Exception as e:
            logger.debug(f"Failed to list LLM profiles: {e}")
        return {
            "profiles": sorted(profiles, key=lambda p: p["name"].lower()),
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

    @app.get("/agents/{agent_name}/tools")
    async def get_agent_tools(request: Request, agent_name: str):
        """The tools an agent REALLY has: name, description, and its server.

        The same ground truth the terminal chat's /tools prints -- the filtered
        schema the model is given, not what the model says it has. The
        neighbouring /allowed-tools answers a different question: its
        "available" list holds SERVER names, so the browser had no way to the
        tools themselves and /tools said "only in the terminal".

        User role, like its neighbour, not admin like the debug twin: any
        agent_name is allowed here because /run already is -- an authenticated
        user can RUN any registered agent by name (_get_agent_with_overrides
        checks that it is an agent, not who may see it), so reading the tool
        names of one discloses nothing that running it would not.

        No filter parameter on purpose: the terminal filters the list it
        already holds, and a server that returns only the matches also
        returns a ``total`` that can no longer tell "this agent has no tools"
        from "nothing matched" -- the caller would have to guess which
        sentence to show.
        """
        from .chat_commands import group_tools_by_server
        from .servers.agent.server import Agent as _Agent

        registry = getattr(request.app.state, "tool_registry", None) or _app_registry
        try:
            srv = registry.get(agent_name) if registry is not None else None
        except Exception as e:
            logger.debug("Failed to get agent %s: %s", agent_name, e)
            srv = None
        if srv is None:
            raise HTTPException(status_code=404, detail=f"agent '{agent_name}' not found")
        if not isinstance(srv, _Agent):
            raise HTTPException(status_code=400,
                                detail=f"'{agent_name}' is a tool server, not an agent")

        try:
            tools = await srv._list_usable_tools_with_details({})
        except Exception as e:
            # Not total: 0 -- the browser reads that as an empty allowlist.
            logger.error("Listing the tools of %s failed: %s", agent_name, e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Could not list tools: {e}")
        try:
            servers = list(registry.list())
        except Exception:
            logger.debug("Could not read registry server names", exc_info=True)
            servers = []
        return {
            "agent": agent_name,
            "total": len(tools),
            "groups": [{"server": server, "tools": grouped}
                       for server, grouped in group_tools_by_server(tools, servers)],
        }

    @app.get("/agents/debug/{agent_name}/allowed-tools")
    async def get_agent_allowed_tools_debug(agent_name: str):
        """Return detailed pattern match diagnostics for an agent's allowed tools.

        Provides:
        - Phase 1: Server-level filtering (which servers pass the allow patterns)
        - Phase 2: Tool-level filtering (actual tools after expansion and blocked filtering)
        
        This shows the complete two-phase filtering process.
        
        Note: This endpoint is admin-only (requires admin role).
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
            # Uses the SAME shared matcher as discovery/details listing.
            from .servers.agent.tool_schema_builder import server_matches_patterns
            server_diagnostics = []
            if patterns:
                for tool in available:
                    matched_by = []
                    for pat in patterns:
                        if server_matches_patterns(tool, [pat]):
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
                    tool_integration_manager=srv._tool_integration_manager,
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

    @app.get("/agents/debug/{agent_name}/system-prompt")
    async def get_agent_system_prompt(agent_name: str):
        """Return the currently rendered system & tools prompt for the agent.

        Renders on demand using the same logic as execution, including:
          - allowed tool filtering
          - max_steps (minus one for planning budget inside prompt)
          - datetime context (if enabled)
        
        Note: This endpoint is admin-only (requires admin role).
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
        llm_profile: Optional[str] = Query(default=None),
        session_title: Optional[str] = Query(default=None),
        force: bool = Query(default=False)
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
        - session_title: Optional title for the session, written with its first save
        
        Security:
        - Requires authentication when auth.enabled=true
        - Validates LLM request permissions
        - Enforces session ownership
        """
        logger = logging.getLogger(__name__)
        request_id = short_id()

        # ========================================
        # SECURITY: Enforce endpoint authentication
        # ========================================
        current_user = await _enforce_endpoint_security(request)
        
        # SECURITY: Validate LLM access (this is an LLM-consuming endpoint)
        _validate_llm_access(current_user, is_llm_request=True)
        
        # Determine user_id for session management
        user_id = current_user.username if current_user else "anonymous"

        # Try to parse task and files from the request in a flexible way
        task = None
        upload_files: list[UploadFile] = []
        content_type = request.headers.get('content-type', '')
        logger.debug("/run content-type: %s", content_type)

        # Optional client-supplied request id — collected from body/form/
        # query below, validated + applied after parsing.
        client_request_id: Optional[str] = None

        # JSON body: {"task": "...", "session_id": "...", "agent_name": "...", "llm_profile": "...", "request_id": "..."}
        if content_type.startswith('application/json'):
            body = await _parse_json_body(request)
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
                if not session_title and 'session_title' in body:
                    session_title = body.get('session_title')
                if 'request_id' in body:
                    client_request_id = body.get('request_id')
                # Session presence: run a session another process holds anyway
                force = force or bool(body.get('force'))

        # multipart/form-data: parse form and files
        elif content_type.startswith('multipart/form-data'):
            try:
                form = await request.form()
            except Exception:
                # Malformed multipart body is a client error, not a 500
                raise HTTPException(
                    status_code=400,
                    detail="Invalid multipart form data: could not be parsed"
                )
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
            if not session_title and 'session_title' in form:
                session_title = form.get('session_title')
            if 'request_id' in form:
                client_request_id = form.get('request_id')
            force = force or str(form.get('force') or "").lower() in ("1", "true", "yes")
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
        if not client_request_id:
            client_request_id = request.query_params.get('request_id')

        # Client-supplied request_id (writer-jobs worker et al.): lets the
        # caller key this run under an id IT already persisted, so its
        # later ``/api/requests/{rid}/status`` probes and
        # ``/api/requests/{rid}/cancel`` propagation actually match this
        # run. Without this, /run minted an id the caller never learns
        # (the response body carries no request_id), so writer-side
        # reconcile probes were guaranteed misses — reported
        # ``unknown/no_active_run`` for live runs (→ resume double-run)
        # and cancel propagation no-opped while the agent kept burning
        # tokens.
        #
        # Guards (400 format; 409 already active, or in line with a live run's
        # id): see _validate_client_request_id, shared with /events.
        if client_request_id:
            request_id = await _validate_client_request_id(client_request_id)

        logger.info("/run invoked, task=%s, files=%d, request_id=%s, session_id=%s, agent=%s, llm_profile=%s, user=%s",
                   task, len(upload_files), request_id, session_id, agent_name or "default", 
                   llm_profile or "default", user_id)

        # Get agent with LLM override
        selected_agent, llm_override, llm_profile_info = _get_agent_with_overrides(agent_name, llm_profile)

        # A run without a session creates one, as with files and on /events. The text-only run goes through
        # collect_final_result, which takes a missing id for a stateless call: a throwaway session, never saved.
        # Its caller is headless (the writer's dispatches) and never learns the id: once saved, the session
        # leaves the agent's tracker, which keeps what it holds for the life of the process.
        made_session = False
        if not session_id and not upload_files and task:
            session_id, made_session = short_id(), True

        session_exists = await _open_session_for_run(selected_agent, user_id, session_id, llm_profile)
        if session_exists:
            session_title = None  # it names a session the run creates, not one it continues

        from .servers.agent.result_utils import collect_final_result

        # If no uploaded files, treat as text-only
        if not upload_files:
            if not task:
                raise HTTPException(status_code=400, detail="Missing 'task' in request")

            # Mirrored into a job, so a page can follow the run (see _mirror_run_as_job).
            # First: a second run under a running id is refused before it takes the
            # id's ownership or the session.
            try:
                mirror = await _mirror_run_as_job(
                    request_id, user_id, selected_agent.name, session_id,
                    llm_profile or selected_agent.agent_config.default_llm_profile)
            except HTTPException:
                if made_session:  # opened already, and nothing below lets go of it
                    selected_agent._session_tracker.discard_session(session_id)
                raise
            held = None
            try:
                # Register request ownership for status stream security -- AFTER
                # all validations and inside the try whose finally releases it.
                # Registered earlier, every 4xx above leaked the entry.
                register_request_user(request_id, user_id)
                # Session presence (core/session_presence.py): held through the save
                # after the run, so no woken run has its turn overwritten.
                refusal, held = await _claim_session(selected_agent, session_id, user_id, force)
                if refusal:
                    raise HTTPException(status_code=409, detail=refusal)
                refused = []

                def on_event(event: dict) -> Any:
                    _carry_title(selected_agent, event, session_title)
                    if _refused_at_the_lock(event):
                        refused.append(event)
                    return mirror.put(event)

                # Pass LLM override to collect_final_result
                result = await collect_final_result(
                    selected_agent, task,
                    request_id=request_id,
                    session_id=session_id,
                    llm_override=llm_override,
                    llm_profile_info_override=llm_profile_info,
                    on_event=on_event,
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

                # Save session after execution (if session_id was provided or created) --
                # not one the run was refused, nor one somebody holds after it
                # (after_run): another run of this process, or an append saving it.
                if session_id and _session_service and not refused:
                    effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                    was_new_session = not session_exists
                    await _session_service.save_session(
                        selected_agent,
                        user_id,
                        session_id,
                        selected_agent.name,
                        effective_llm_profile,
                        was_new_session,
                        after_run=True,
                    )

                return result
            finally:
                mirror.close()
                _let_go(selected_agent, held, user_id)
                if made_session:
                    selected_agent._session_tracker.discard_session(session_id)
                # Cleanup: release request + derived sub-request ids (tool
                # suffixes, sub-agents) from the ownership map
                release_request_user_tree(request_id)

        # Process uploaded files for multimodal input
        from .utils.multimodal_processor import (
            AttachmentRejected, detect_file_type, message_with_attachments)
        import tempfile
        from pathlib import Path

        # Categorize uploaded files by type
        image_paths = []
        audio_paths = []
        text_paths = []
        temp_files = []
        temp_dir = None
        # Once the SSE generator is returned, ITS finally owns the cleanup.
        # Until then every error path (400 capability check, write failure,
        # processing error) must clean up here — see the outer finally.
        stream_owns_cleanup = False

        # Ownership registration inside the try/finally pairing (see the
        # text-only branch for the rationale).
        register_request_user(request_id, user_id)

        try:
            temp_dir = Path(tempfile.mkdtemp())

            temp_dir_resolved = temp_dir.resolve()
            for upload_file in upload_files:
                # SECURITY: the client-supplied filename must NOT be trusted.
                # Path's `/` drops the left side if the right is absolute and
                # honors '../' segments, so a raw join allows arbitrary-path
                # writes (RCE / config overwrite). Take only the basename and
                # verify the result stays inside temp_dir.
                safe_name = Path(upload_file.filename or "").name
                if not safe_name:
                    logger.warning("Skipping upload with empty/unsafe filename: %r", upload_file.filename)
                    continue
                temp_path = temp_dir / safe_name
                if not temp_path.resolve().is_relative_to(temp_dir_resolved):
                    logger.warning("Skipping upload that escapes temp dir: %r", upload_file.filename)
                    continue
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

            if not task and not (image_paths or audio_paths or text_paths):
                raise HTTPException(status_code=400, detail="Missing 'task' in request")
            # The check against the model this run uses, then the build: the
            # step every entry point that attaches media shares.
            try:
                multimodal_msg = message_with_attachments(
                    task, {"image": image_paths, "audio": audio_paths, "text": text_paths},
                    llm_override, selected_agent)
            except AttachmentRejected as e:
                # A file that failed to process carries its cause; that one may
                # be a server fault (a codec, a bug) and keeps its traceback.
                logger.warning("Attachments refused: %s", e, exc_info=e.__cause__ is not None)
                raise HTTPException(status_code=400, detail=str(e))

            # Stream events for multimodal message (same as /events endpoint)
            async def event_stream():
                # Initial keep-alive line
                yield ":ok\n\n"

                # Track if this is a new session
                was_new_session = (session_id is None) or (not session_exists)
                actual_session_id = session_id
                refused = False  # the run was refused at the agent's session lock
                # Session presence (core/session_presence.py): held before the
                # run through the save after it; a session this run creates
                # comes with the start event.
                refusal, held = await _claim_session(
                    selected_agent, actual_session_id, user_id, force)
                if refusal:
                    yield "event: error\n"
                    yield f"data: {json.dumps({'type': 'error', 'message': refusal}, ensure_ascii=False)}\n\n"
                    return

                try:
                    async for event in selected_agent.run_events(multimodal_msg, request_id=request_id, session_id=actual_session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info):
                        event_type = event.get("type")
                        refused = refused or _refused_at_the_lock(event)

                        # Capture session_id from start event (created on first call)
                        if event_type == "start" and event.get("session_id"):
                            actual_session_id = event["session_id"]
                            if not held:
                                held = _hold_fresh_session(
                                    selected_agent, actual_session_id, user_id)
                            _carry_title(selected_agent, event, session_title)

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
                    try:
                        # Save session after completion -- not one the run was refused, nor
                        # one somebody holds after it (after_run): another run of this
                        # process, an append saving it, or this run itself, not done (a
                        # stream left at a yield) -- its own end saves it. Shielded: a
                        # client that leaves cancels the stream's whole scope, the run in
                        # it and its last save too, and here every await was cancelled
                        # again -- nothing saved, and the steps below skipped.
                        if _session_service and actual_session_id and not refused:
                            # Use actual agent name and effective llm_profile (respecting overrides)
                            effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                            with anyio.CancelScope(shield=True):
                                await _session_service.save_session(
                                    selected_agent,
                                    user_id,
                                    actual_session_id,
                                    selected_agent.name,
                                    effective_llm_profile,
                                    was_new_session,
                                    after_run=True,
                                )
                    finally:
                        # Whatever became of the save: skipped, the session stayed held
                        # for the life of the process (presence refuses every later run).
                        _let_go(selected_agent, held, user_id)
                        # Cleanup: release request + derived sub-request ids
                        release_request_user_tree(request_id)

                        # Cleanup temp files after streaming completes
                        for temp_file in temp_files:
                            try:
                                temp_file.unlink()
                            except Exception as e:
                                logger.warning("Failed to delete temp file %s: %s", temp_file, e)
                        try:
                            temp_dir.rmdir()
                        except Exception as e:
                            logger.warning("Failed to delete temp dir %s: %s", temp_dir, e)

            stream_owns_cleanup = True
            return _sse_response(event_stream(), media_type="text/event-stream")

        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Unexpected error in /run: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if not stream_owns_cleanup:
                # The generator was never handed to the client — uploads and
                # the ownership entry would leak on this error path.
                release_request_user_tree(request_id)
                for temp_file in temp_files:
                    try:
                        temp_file.unlink()
                    except Exception as e:
                        logger.warning("Failed to delete temp file %s: %s", temp_file, e)
                if temp_dir is not None:
                    try:
                        temp_dir.rmdir()
                    except Exception as e:
                        logger.warning("Failed to delete temp dir %s: %s", temp_dir, e)

    async def _sse_lines(job: BackgroundJob, format_agent: Any, cursor: int,
                         before_send: Optional[Callable[[dict], None]] = None):
        """A job's events from number ``cursor`` on as SSE lines, a keepalive while it is quiet.

        Every stream of every job reads through here: the run's own and each reconnect.
        ``before_send`` sees each event first.
        """
        job_manager = get_background_job_manager()
        await job_manager.increment_sse_client(job.request_id)
        try:
            async for ev in job.follow(cursor, config.status.sse_keepalive_interval):
                if ev is None:
                    yield ":keepalive\n\n"
                    continue
                if before_send is not None:
                    before_send(ev)
                yield await _format_and_yield_event(
                    ev, format_agent, job.request_id, job.actual_session_id or job.session_id or "unknown")
        finally:
            await job_manager.decrement_sse_client(job.request_id)

    async def _handle_events(
        request: Request,
        task: str,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        llm_profile: Optional[str] = None,
        request_id: Optional[str] = None,
        force: bool = False,
        session_title: Optional[str] = None,
    ):
        """Shared implementation for GET/POST /events endpoints.

        Streams agent SSE events for a task.
        """
        logger = logging.getLogger(__name__)

        # ========================================
        # SECURITY: Enforce endpoint authentication
        # ========================================
        current_user = await _enforce_endpoint_security(request)
        
        # SECURITY: Validate LLM access (this is an LLM-consuming endpoint)
        _validate_llm_access(current_user, is_llm_request=True)

        # Determine user_id for session management
        user_id = current_user.username if current_user else "anonymous"

        # Check if reconnecting to an existing job
        job_manager = get_background_job_manager()
        existing_job: Optional[BackgroundJob] = None
        if request_id:
            existing_job = await job_manager.get_job(request_id)
            if existing_job:
                # Verify user owns this job
                if existing_job.user_id != user_id:
                    raise HTTPException(status_code=403, detail="Access denied to this request")
                logger.info(f"Client reconnecting to job {request_id}")
                # Use the agent_name from the original job, not from query params
                agent_name = existing_job.agent_name
        
        # Not reconnecting: adopt a caller-supplied request_id (parity with
        # POST /run) so external dispatchers can track/cancel the run under
        # an ID they know; mint one only when the client sent none. Before
        # 2026-08 a client-supplied ID was silently discarded here, which
        # made every /events-dispatched run uncancellable by its caller.
        # Same guards as /run (_validate_client_request_id): format → 400; id
        # already active elsewhere (non-BackgroundJob, so not reconnectable), or
        # in line with a live run's id → 409.
        if not existing_job:
            # No task, no run. The chat's reconnect URL carries none, and for an id whose
            # job is gone by then (a restart) it would start an empty turn in the session.
            if not (task or "").strip():
                if request_id:
                    raise HTTPException(status_code=404, detail=f"No running job under request_id {request_id}")
                raise HTTPException(status_code=400, detail="Missing 'task'")
            if request_id:
                request_id = await _validate_client_request_id(request_id)
            else:
                request_id = short_id()
            
        # Register request ownership for status stream security
        # (register_request_user, not a raw dict write -- keeps the FIFO cap)
        register_request_user(request_id, user_id)

        logger.info("SSE /events connected, task=%s, request_id=%s, session_id=%s, agent=%s, llm_profile=%s, user_id=%s, reconnect=%s",
                   task, request_id, session_id, agent_name or "default", llm_profile or "default", user_id, existing_job is not None)

        # FAST PATH: For reconnecting clients, skip all setup and go straight to streaming
        if existing_job:
            # Load agent for event formatting (needed for HTML conversion via hooks)
            # Start with the global default agent from build_app()
            reconnect_agent = app.state.agent  # Global agent from build_app
            
            # Try to load the specific agent if it's different from the default
            if existing_job.agent_name and existing_job.agent_name != reconnect_agent.name:
                try:
                    reconnect_agent = _app_registry.get(existing_job.agent_name)  # type: ignore[attr-defined]
                except Exception as e:
                    logger.warning(f"Could not load agent '{existing_job.agent_name}': {e}, using default")
            
            # ``catch_up=skip&seen=N``: the client has just loaded this session, and that
            # load told it the run had sent N events by then -- everything those events
            # say is already in the messages it is showing, so it reads on from event N.
            # Without it the reconnect replays what the buffer still holds.
            cursor = 0
            if request.query_params.get("catch_up") == "skip":
                try:
                    cursor = max(0, int(request.query_params.get("seen", "")))
                except ValueError:
                    cursor = 0

            async def reconnect_event_stream():
                """Simplified event stream for reconnecting clients."""
                # Send immediate :ok to establish connection
                yield ":ok\n\n"

                # Send reconnect event
                reconnect_payload = {
                    "type": "reconnect",
                    "request_id": request_id,
                    "session_id": existing_job.actual_session_id or existing_job.session_id,
                    "agent_name": existing_job.agent_name,
                    "llm_profile": existing_job.llm_profile,
                    "status": existing_job.status.value,
                    "task": existing_job.task_description,
                    "created_at": existing_job.created_at,
                    "message": f"Reconnected to running job (started {int(time.time() - existing_job.created_at)}s ago)"
                }
                if existing_job.last_status_message:
                    reconnect_payload["last_status"] = existing_job.last_status_message
                yield f"data: {json.dumps(reconnect_payload, ensure_ascii=False)}\n\n"

                async with aclosing(_sse_lines(existing_job, reconnect_agent, cursor)) as lines:
                    async for line in lines:
                        yield line

            return _sse_response(
                reconnect_event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # NORMAL PATH: For new requests, do full setup
        try:
            selected_agent, llm_override, llm_profile_info = _get_agent_with_overrides(agent_name, llm_profile)
            session_exists = await _open_session_for_run(selected_agent, user_id, session_id, llm_profile)
        except HTTPException:
            # Registered above for the status stream; no run follows to release it.
            release_request_user_tree(request_id)
            raise
        if session_exists:
            session_title = None  # it names a session the run creates, not one it continues

        async def event_stream():
            # Check if server is already shutting down
            if _shutdown_event and _shutdown_event.is_set():
                yield ":server_shutdown\n\n"
                return
            
            yield ":ok\n\n"

            was_new_session = (session_id is None) or (not session_exists)
            actual_session_id = session_id
            refused = False  # the run was refused at the agent's session lock

            # Session presence (core/session_presence.py): held before the job
            # starts through the save after it; a session the job creates comes
            # with the start event.
            refusal, held = await _claim_session(
                selected_agent, actual_session_id, user_id, force)
            if refusal:
                yield f"data: {json.dumps({'type': 'error', 'request_id': request_id, 'error': refusal}, ensure_ascii=False)}\n\n"
                return

            # Create new background job (reconnects use the fast path above)
            async def agent_runner():
                """Run the agent and yield events"""
                async for ev in selected_agent.run_events(
                    task, request_id, actual_session_id, 
                    llm_override=llm_override, llm_profile_info_override=llm_profile_info
                ):
                    # Here, not where the stream is read: the run waits at this
                    # event until it is passed on, so no save of it comes first.
                    _carry_title(selected_agent, ev, session_title)
                    yield ev
            
            try:
                job = await job_manager.create_job(
                    request_id=request_id,
                    user_id=user_id,
                    agent_name=agent_name or "default",
                    session_id=session_id,
                    agent_runner=agent_runner,
                    llm_profile=llm_profile,
                )
            except DuplicateRequestIdError:
                # A concurrent request won the race for this caller-supplied
                # id (the 409 guard above cannot be atomic with create_job,
                # which only runs once this body is streamed). Refuse instead
                # of starting a second agent under the same id; the caller's
                # retry lands on the reconnect fast path.
                logger.warning(
                    "SSE /events refused duplicate request_id=%s — a job is "
                    "already running under it", request_id,
                )
                yield f"data: {json.dumps({'type': 'error', 'request_id': request_id, 'error': 'request_id is already running — reconnect instead of starting a second run'}, ensure_ascii=False)}\n\n"
                _let_go(selected_agent, held, user_id)  # no job of ours runs it
                return
            except BaseException:
                # Nothing of ours runs the session, and the finally below that
                # would let it go is not entered yet: in this process a hold
                # nobody releases refuses every later run of that session.
                _let_go(selected_agent, held, user_id)
                raise
            # Store task description for reconnect
            job.task_description = task

            def take_the_session(ev: dict) -> None:
                """The run's own stream holds a session the run creates, from its start event."""
                nonlocal actual_session_id, held, refused
                refused = refused or _refused_at_the_lock(ev)
                if ev.get("type") == "start" and ev.get("session_id"):
                    actual_session_id = ev["session_id"]
                    if not held:
                        held = _hold_fresh_session(selected_agent, actual_session_id, user_id)
                if was_new_session or ev.get("type") == "start":
                    selected_agent._session_tracker.set_session_metadata(actual_session_id, {
                        "user_id": user_id,
                        # The real agent name, never the literal "default" —
                        # a later append persists this field to disk and the
                        # session UI resolves it against the registry.
                        "agent_name": selected_agent.name,
                        "llm_profile": llm_profile or selected_agent.agent_config.default_llm_profile,
                    })

            try:
                async with aclosing(_sse_lines(job, selected_agent, 0, take_the_session)) as lines:
                    async for line in lines:
                        yield line
            except asyncio.CancelledError:
                # SSE connection cancelled (client disconnect)
                # Send cancellation event to client (if possible)
                cancelled_payload = {"type": "disconnected", "request_id": request_id, "message": "SSE connection closed, job continues in background"}
                try:
                    yield f"data: {json.dumps(cancelled_payload, ensure_ascii=False)}\n\n"
                except Exception:
                    pass
                raise
            except Exception as e:
                # Other errors - send error event
                logger.error(f"Error in event stream for request {request_id}: {e}", exc_info=True)
                error_payload = {"type": "error", "message": str(e), "request_id": request_id}
                try:
                    yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                except Exception:
                    pass
                raise
            finally:
                # NOTE: We do NOT cancel the job here! The job continues running in background.
                # The job will be cancelled only via explicit /cancel endpoint.
                
                # Persist session if job is completed -- not one its run was refused,
                # nor one somebody holds after it (after_run): another run of this
                # process has it, and the tracker holds that run's live state (a tool
                # call without its result, say), or an append that saves it itself.
                try:
                    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED) and not refused:
                        if actual_session_id and _session_service:
                            effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
                            # Shielded: a client that leaves cancels the stream's scope,
                            # and the save was cancelled again at its first await.
                            with anyio.CancelScope(shield=True):
                                await _session_service.save_session(
                                    selected_agent,
                                    user_id,
                                    actual_session_id,
                                    selected_agent.name,
                                    effective_llm_profile,
                                    was_new_session,
                                    after_run=True,
                                )
                finally:
                    # Whatever became of the save: skipped, the session stayed held.
                    _let_go(selected_agent, held, user_id)

                    # Cleanup: release ownership only if job is done (a running
                    # job's stream may reconnect and must keep its mapping)
                    if job.status != JobStatus.RUNNING:
                        release_request_user_tree(request_id)

        return _sse_response(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/events")
    async def events_get(
        request: Request,
        task: str,
        session_id: Optional[str] = Query(default=None),
        agent: Optional[str] = Query(default=None, alias="agent"),
        agent_name: Optional[str] = Query(default=None),
        llm_profile: Optional[str] = Query(default=None),
        request_id: Optional[str] = Query(default=None),
        force: bool = Query(default=False),
        session_title: Optional[str] = Query(default=None),
    ):
        """Stream agent events for a task (GET).

        Query parameters:
        - task: The task to execute
        - session_id: Optional session ID for conversation continuity
        - agent or agent_name: Optional agent to use
        - llm_profile: Optional LLM profile override
        - session_title: Optional title for the session, written with its first save
        - request_id: Optional request ID — reconnects if it matches a
          running job, otherwise the new run is keyed under it (parity
          with POST /run; the guards of _validate_client_request_id apply)

        Note: For long task texts, prefer POST /events to avoid URL length limits.
        """
        # Prioritize 'agent' parameter over 'agent_name' for backwards compatibility
        effective_agent_name = agent or agent_name
        return await _handle_events(
            request=request,
            task=task,
            session_id=session_id,
            agent_name=effective_agent_name,
            llm_profile=llm_profile,
            request_id=request_id,
            force=force,
            session_title=session_title,
        )

    @app.post("/events")
    async def events_post(request: Request):
        """Stream agent events for a task (POST).

        Accepts JSON body with fields:
        - task: The task to execute (required)
        - session_id: Optional session ID for conversation continuity
        - agent_name: Optional agent to use
        - llm_profile: Optional LLM profile override
        - session_title: Optional title for the session, written with its first save
        - request_id: Optional request ID — reconnects if it matches a
          running job, otherwise the new run is keyed under it (parity
          with POST /run; the guards of _validate_client_request_id apply)

        This endpoint avoids URL length limits that affect GET /events
        when sending long task texts.
        """
        body = await _parse_json_body(request)
        task = body.get("task")
        if not task:
            raise HTTPException(status_code=400, detail="Missing 'task' in request body")
        return await _handle_events(
            request=request,
            task=task,
            session_id=body.get("session_id"),
            agent_name=body.get("agent_name") or body.get("agent"),
            llm_profile=body.get("llm_profile"),
            request_id=body.get("request_id"),
            force=bool(body.get("force")),
            session_title=body.get("session_title"),
        )

    async def _refuse_foreign_request(request_id: str, current_user: Any, *, reaches_below: bool = False) -> None:
        """403 unless every run the request id reaches is the caller's own, or the caller is an admin.

        A request id is all these endpoints are given. Without this, anyone signed in
        who learned one could read another user's run, stop it, or put words into it --
        which the run then acts on with its owner's tools.

        An id may be nobody's and still be somebody's run: a sub-run is named by its
        caller's id and `_…`, and forgotten by the owner map when its caller's turn ends,
        while it may work on -- so the owner is that of the nearest known run at or above
        the id. A cancel (``reaches_below``) stops every run whose id extends the one
        given, so there every known run below it counts too. A request nobody is known to
        own (a restart forgot it) is left alone: nothing of anybody's is reached through it.
        """
        if current_user is None or getattr(current_user, "role", None) == "admin":
            return
        job_manager = get_background_job_manager()
        owners = set()
        parts = request_id.split("_")
        for n in range(len(parts), 0, -1):
            above = "_".join(parts[:n])
            job = await job_manager.get_job(above)
            owner = job.user_id if job is not None else get_request_user(above, default=None)
            if owner is not None:
                owners.add(owner)
                break
        if reaches_below:
            below = f"{request_id}_"
            owners.update(user for rid, user in list(_request_user_map.items()) if rid.startswith(below))
            owners.update(job["user_id"] for job in await job_manager.get_all_jobs(include_completed=True)
                          if job["request_id"].startswith(below))
        if owners - {current_user.username}:
            raise HTTPException(status_code=403, detail="Access denied to this request")

    @app.get("/api/requests/{request_id}/status")
    async def get_request_status(request_id: str, request: Request):
        """Get the status of a request for reconnection purposes.

        Used by the WebUI before it follows a run again after a reload: GET /events
        with an id the job manager no longer holds would start a new run. Returns
        whether the request is still running or completed.
        """
        # First check BackgroundJobManager for more accurate status
        job_manager = get_background_job_manager()
        job = await job_manager.get_job(request_id)
        await _refuse_foreign_request(request_id, await _enforce_endpoint_security(request))
        # A finished MIRROR (POST /run) answers as if there had been no job: its
        # caller had the answer, and "completed" with the job's keys is what the
        # writer reconcile takes as proof that a book run is done.
        if job and job.mirror and job.status != JobStatus.RUNNING:
            job = None
        if job:
            return {
                "request_id": request_id,
                "status": job.status.value,
                "completed": job.status != JobStatus.RUNNING,
                "error": job.error_message,
                "sse_clients": job.sse_client_count,
                "events_buffered": len(job.events),
            }
        
        # Fallback to session tracker — checks the DEFAULT agent first
        # (cheap), then walks the agent registry so requests running on
        # sub-agent servers (linear_book, v5b_story_designer,
        # cover_artist, ...) are seen too. Without the walk this
        # endpoint reported ``unknown/no_active_run`` for a /run that
        # was actively grinding on a non-default agent — the writer
        # reconcile pass consumed that as "run lost" and re-queued the
        # job for resume, double-running multi-hour generations. Same
        # per-agent blind-spot class as the 2026-06-27 cancel
        # regression, fixed the same way (registry walk).
        is_active = await agent._session_tracker.is_request_active(request_id)
        if not is_active:
            is_active = await job_manager.is_request_active_anywhere(request_id)

        if is_active:
            return {
                "request_id": request_id,
                "status": "running",
                "completed": False
            }

        # Request not active in either tracker.
        #
        # 2026-06-27 fix: previously returned ``status='completed',
        # completed=true`` which LIED about the run's outcome — there
        # is no positive evidence the request finished successfully,
        # only that this process doesn't know about it (typical case:
        # agent-api restarted and the BackgroundJob died with it).
        # The writer-side reconcile pass (producer.py
        # _reconcile_agent_api_orphans) consumed that lie as success
        # and marked the queue row 'done' → silent data loss
        # (2026-06-26 incident root cause). Frontend's poll loop
        # also treated it as a clean completion and rendered an
        # empty result.
        #
        # New contract: status='unknown', completed=false +
        # error+reason so the writer-side reconcile leaves the row
        # for the sweep (= Resume) and the WebUI lets the run go
        # instead of following it.
        return {
            "request_id": request_id,
            "status": "unknown",
            "completed": False,
            "reason": "no_active_run",
            "error": "Request not found — the server may have restarted while this run was active",
        }

    @app.post("/api/requests/{request_id}/cancel")
    async def cancel_request(request_id: str, request: Request, force: bool = Query(default=False)):
        """Cancel an active request by its ID.

        Delegates to ``BackgroundJobManager.cancel_job`` which is the
        single source of truth for writer-side cancellation: it sets
        the cancellation token, walks the agent registry to call
        ``cancel_request`` on the server that actually owns the
        request (linear_book, v5b_story_designer, cover_artist, ...),
        falls back to the default agent for chat_agent-style requests,
        and force-cancels the asyncio task after the grace period.

        See ``BackgroundJobManager.cancel_job`` for the layered
        semantics. The 2026-06-27 cancel regression (sub-agent
        requests reported 'cancelled' but kept running) is closed
        there — every caller of this endpoint, the admin endpoint at
        ``/admin/active-sessions/{rid}/cancel``, and the writer-jobs
        propagation helper now use the same path.

        Args:
            request_id: The request ID to cancel
            force: If True, force-cancel after 5s if agent doesn't respond to graceful cancel
        """
        logger = logging.getLogger(__name__)
        logger.info(
            "Cancel request received for request_id=%s (force=%s)",
            request_id, force,
        )
        await _refuse_foreign_request(request_id, await _enforce_endpoint_security(request), reaches_below=True)
        success = await get_background_job_manager().cancel_job(
            request_id, force_timeout=5.0 if force else 0.0,
        )
        if success:
            return {"status": "cancelled", "request_id": request_id}
        return {
            "status": "not_found",
            "request_id": request_id,
            "message": "Request not found or already completed",
        }

    async def _verify_session_owner(sid: str, current_user: Any,
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
        """
        from fastapi import HTTPException
        if current_user is None:
            return
        if tracker is None:
            tracker = getattr(agent, "_session_tracker", None)
        owner = ((await get_background_job_manager().active_sessions()).get(sid) or {}).get("user_id")
        # In-memory session metadata next (covers sessions not yet persisted),
        # then the persisted owner on disk.
        try:
            meta = tracker.get_session_metadata(sid) if owner is None else None
            if meta:
                owner = meta.get("user_id")
        except Exception:
            owner = None
        if owner is None and _session_service and getattr(_session_service, "session_manager", None):
            try:
                owner = await _session_service.session_manager._find_session_owner_async(sid)
            except Exception:
                owner = None
        if owner is not None and owner != current_user.username:
            raise HTTPException(status_code=403, detail="You do not have access to this session")

    @app.post("/events/{request_id}/append")
    async def append_event(
        request_id: str,
        request: Request,
        session_id: Optional[str] = Query(default=None),
        fallback: str = Query(default="session"),
        force: bool = Query(default=False),
    ):
        """Append a user message to an existing active request or session.

        If `session_id` query parameter is provided, append directly to session.
        Body: { "content": "the user message" }

        `fallback` controls what happens when the request is not active anymore:
        - "session" (default): append to the request's persisted session (the
          message is stored but only answered by the next run).
        - "none": return 404 so the caller can start a new request instead.

        Note: When appending to a session (not an active request), the session is
        persisted to disk automatically.
        """
        logger = logging.getLogger(__name__)
        from fastapi import HTTPException
        
        # ========================================
        # SECURITY: Enforce endpoint authentication
        # ========================================
        current_user = await _enforce_endpoint_security(request)
        user_id = current_user.username if current_user else "anonymous"
        
        body = await _parse_json_body(request)

        content = body.get('content')
        if not content:
            raise HTTPException(status_code=400, detail="Missing 'content' in body")

        if session_id:
            owner_agent = await _session_owner_agent(request, session_id, user_id)
            # Ownership check before mutating someone else's session (IDOR) -- against the tracker the append
            # writes, not the entry agent's (a session without an owner on disk passes there for anybody).
            await _verify_session_owner(session_id, current_user, owner_agent._session_tracker)
            # Append directly to persisted session using agent method
            logger.debug("Appending to session %s: %.120s", session_id, content)
            if not await _append_and_persist(owner_agent, session_id, content, user_id, force):
                raise HTTPException(status_code=404, detail="Session not found")
            return {"status": "appended", "session_id": session_id}

        # Mid-run appends must reach the agent instance that owns the run:
        # runs started with agent_name execute on that agent, not on the
        # default agent this endpoint is bound to.
        target_agent = await resolve_agent_for_request(
            request_id, get_background_job_manager(), _app_registry, agent
        )
        await _refuse_foreign_request(request_id, current_user)

        logger.debug("Append request received for request_id=%s (agent=%s): %.120s",
                     request_id, target_agent.name, content)
        try:
            appended = await target_agent.append_user_message(request_id, content)
        except HTTPException:
            # Let agent-level HTTPExceptions bubble up
            raise
        except Exception as e:
            logger.exception("Unexpected error in append_user_message for %s: %s", request_id, e)
            raise HTTPException(status_code=500, detail=str(e))

        if appended:
            # Active request - will be persisted when request completes
            return {"status": "appended", "request_id": request_id}

        if fallback == "none":
            # Caller handles the finished-run case itself (e.g. starts a new
            # request) instead of parking the message in the session unanswered.
            raise HTTPException(status_code=404, detail="Request not active")

        # If request not found/finished, try to append into the persisted session for this request
        sid = target_agent._session_tracker.get_session_for_request(request_id)
        if sid:
            await _verify_session_owner(sid, current_user, target_agent._session_tracker)
            logger.debug("Request %s already finished; appending to session %s", request_id, sid)
            if await _append_and_persist(target_agent, sid, content, user_id, force):
                return {"status": "appended", "session_id": sid}

        raise HTTPException(status_code=404, detail="Request not found or already completed")

    @asynccontextmanager
    async def _beside_the_runs(target_agent: Any, sid: str):
        """The agent's session lock for a write no run makes -- an append, /undo's cut -- from before it reads the
        session until its save is done. Yields None while it holds it, else the request id of the run that has the
        session ("" when it cannot be named): the lock refuses at once while one owns it.

        Beside the runs, such a write raced what a run of this process does with the session after its own end:
        openai_api's AgentTurn puts back a turn its client never got and drops the conversation from the tracker
        -- over the write, or between the write and its save, which then found nothing to write -- and a run that
        took the session meanwhile had it read back from under it. Session presence does not keep them apart
        (holds nest inside a process), and it may be off.

        Taken as a writer: a run, a turn's put back or another write that asks
        for the lock meanwhile waits for it instead of being refused.
        """
        tracker = target_agent._session_tracker
        writer = f"write_{short_id()}"
        if not await tracker.acquire_session_lock(sid, writer, timeout=5.0, writer=True):
            yield tracker.check_session_locked(sid)[1] or ""
            return
        try:
            yield None
        finally:
            await tracker.release_session_lock(sid, writer)

    def _settling_agent(request: Request, sid: str) -> Any:
        """The agent with a turn settling the session (openai_api's AgentTurn watches for appends while it does),
        or None. It comes before the record, which names the agent of the last SAVED run: a turn whose run saved
        nothing -- it failed on its way in -- puts back its own copy over whatever another agent's tracker took."""
        registry = getattr(request.app.state, "tool_registry", None) or _app_registry
        try:
            names = list(registry.list()) if registry is not None else []
        except Exception as e:  # noqa: BLE001 - no registry to ask, the record decides
            logging.getLogger(__name__).debug("No agents to ask about %s: %s", sid, e)
            names = []
        for name in names:
            candidate = _chat_agent(request, name)
            if candidate is not None and candidate._session_tracker.watches_appends(sid):
                return candidate
        return None

    async def _session_owner_agent(request: Request, sid: str, user_id: str) -> Any:
        """The agent whose SessionTracker holds a stored session -- every agent carries its own, and a
        conversation of openai_api runs on the agent its model names. Written through another agent's tracker, a
        message was read back into a copy no run of the session looks at, and put back or saved over by the one
        that does. The agent of a turn settling it (_settling_agent), else the one the record names, else the entry
        agent: a session without a record, or whose agent is gone."""
        settling = _settling_agent(request, sid)
        if settling is not None:
            return settling
        ran_with = await _session_agent_name(sid, user_id)
        return (_chat_agent(request, ran_with) if ran_with else None) or agent

    async def _append_and_persist(owner_agent: Any, sid: str, content: str, user_id: str,
                                  force: bool = False) -> bool:
        """Append a user message to a session no request of this process runs, and save it.

        The session is held for the append (session presence,
        core/session_presence.py), and its copy in memory is re-read when
        another process wrote the file -- a run woken by a direct message
        continues the session from disk, while re-reading unasked would undo
        what a run of this process has not saved yet -- or when there is none:
        a session this process saved and let go of. From reading it to its
        save, the append holds the agent's session lock (_beside_the_runs).

        A session a run of THIS process has is not written beside the run: the
        message goes to the run, which reads it at its next step -- or, when the
        run takes no more (it is finishing), it is refused. Written into the
        session, it answered "appended" and was gone at the run's next save, which
        writes the run's own list; the claim below does not stop it, since holds
        nest inside a process.
        """
        job_manager = get_background_job_manager()
        running = (await job_manager.active_sessions()).get(sid)
        # Another append or /undo holding the session is no run: waited for below (_beside_the_runs).
        if running and running.get("request_id") and not owner_agent._session_tracker.held_by_a_writer(sid):
            run_id = running["request_id"]
            # `agent`, not owner_agent: a job on "default" runs on the app's default agent,
            # and owner_agent is the agent of the request the caller named -- its last run.
            run_agent = await resolve_agent_for_request(run_id, job_manager, _app_registry, agent,
                                                        agent_name=running.get("agent_name"))
            if await run_agent.append_user_message(run_id, content):
                return True
            raise HTTPException(
                status_code=409,
                detail=f"Session {sid} is finishing a run -- what it saves would drop the message. "
                       f"Try again once it is done.")

        refusal, held = await _claim_session(owner_agent, sid, user_id, force)
        if refusal:
            raise HTTPException(status_code=409, detail=refusal)
        try:
            async with _beside_the_runs(owner_agent, sid) as running:
                if running is not None:
                    # A run of this agent took the session since it was asked above: the message is its.
                    if running and await owner_agent.append_user_message(running, content):
                        return True
                    raise HTTPException(
                        status_code=409,
                        detail=f"Session {sid} is running -- what it saves would drop the message. "
                               f"Try again once it is done.")
                tracker = owner_agent._session_tracker
                if not tracker.has_session(sid) and _session_service:
                    # Not in memory, and the file unmoved since this process wrote it, so the claim read nothing: a
                    # session this process saved and let go of (a settled openai_api turn does). Appended to
                    # nothing, the conversation on screen was "not found".
                    await _session_service.load_and_restore_session(owner_agent, user_id, sid)
                if not await owner_agent.append_to_session(sid, content):
                    return False
                appended = tracker.get_session_messages(sid)[-1]
                if _session_service:
                    metadata = tracker.get_session_metadata(sid) or {}
                    saved = await _session_service.save_session(
                        owner_agent,
                        user_id,
                        sid,
                        metadata.get("agent_name", owner_agent.name),
                        metadata.get("llm_profile", owner_agent.agent_config.default_llm_profile),
                        was_new_session=False
                    )
                    if not saved:
                        # Answered "appended", the message was not on disk -- and left in memory, the next run's
                        # save wrote it after all, beside the copy a client that heard the failure sent again.
                        tracker.set_session_messages(
                            sid, [message for message in tracker.get_session_messages(sid) if message is not appended])
                        raise HTTPException(
                            status_code=500, detail=f"Session {sid} could not be saved; the message was not appended.")
                    logging.getLogger(__name__).debug("Session %s persisted to disk after append", sid)
                return True
        finally:
            _let_go(owner_agent, held, user_id)

    async def _session_record(sid: str, user_id: str) -> dict:
        """A stored session as it lies on disk, or {} when there is none.

        One read for everything a handler wants off it -- its agent, the LLM
        profile it runs on, its messages. Two helpers doing their own
        load_session read the same file twice for one command.
        """
        if not _session_service or not _session_service.session_manager:
            return {}
        try:
            record = await _session_service.session_manager.load_session(user_id, sid)
        except Exception as e:
            logging.getLogger(__name__).debug("No record for %s: %s", sid, e)
            return {}
        return record or {}

    async def _session_agent_name(sid: str, user_id: str) -> Optional[str]:
        """The agent a stored session ran with, or None while it has none.

        Read off the record, which is where a session's agent lives
        (cli_utils/session_defaults.py says the same for the terminal) -- and
        without taking it as seen (SessionManager.peek_session): the callers
        claim the session next, and the claim re-reads the copy in memory only
        when the file holds what this process has not seen. Loaded here, what
        another process wrote meanwhile counted as seen: /undo cut the stale
        copy's last exchange, an append was written onto it, and both saved it
        over the other process's turn.
        """
        if not _session_service or not _session_service.session_manager:
            return None
        try:
            record = await _session_service.session_manager.peek_session(user_id, sid)
        except Exception as e:  # noqa: BLE001 - a record that does not read names no agent, as _session_record
            logging.getLogger(__name__).debug("No record for %s: %s", sid, e)
            return None
        return record.get("agent_name") or None

    async def _drop_last_exchange_and_persist(owner_agent: Any, sid: str, user_id: str,
                                              force: bool = False) -> Any:
        """Take the last exchange out of a session, and save what is left.

        The mirror of _append_and_persist, claim and save included -- and the
        cut itself is chat_actions.split_off_last_exchange, the one the
        terminal chat uses, so both surfaces end a turn in the same place.

        A session that is RUNNING is refused before anything is touched.
        _claim_session alone does not do it: holds nest inside a process, so a
        run of THIS process lets the claim through -- and then writes its whole
        message list back when it finishes, putting the dropped exchange
        straight back while the browser shows it gone.
        """
        from .chat_actions import split_off_last_exchange

        presence = presence_for(getattr(owner_agent, "system_config", None))
        state = presence.get(sid, user_id) if presence is not None else None
        if state and state["status"] != "idle" and not force:
            raise HTTPException(
                status_code=409,
                detail=f"Session {sid} is {state['status']} -- what it is writing "
                       f"would put the exchange back. Try again once it is done.")

        refusal, held = await _claim_session(owner_agent, sid, user_id, force)
        if refusal:
            raise HTTPException(status_code=409, detail=refusal)
        try:
            async with _beside_the_runs(owner_agent, sid) as running:
                if running is not None:
                    # A run of this agent has it (session presence off, or it took the session since the check
                    # above): it writes its whole message list back when it finishes.
                    raise HTTPException(
                        status_code=409,
                        detail=f"Session {sid} is running -- what it is writing would put the exchange back. "
                               f"Try again once it is done.")
                tracker = owner_agent._session_tracker
                messages = list(tracker.get_session_messages(sid) or [])
                if not messages and _session_service:
                    # The copy in memory can be empty although the record is not:
                    # _claim_session re-reads only when the FILE moved, and a
                    # session this process wrote and no longer holds looks
                    # unchanged to it. Cutting that would answer "nothing to take
                    # back" about a conversation that is plainly on screen.
                    await _session_service.load_and_restore_session(owner_agent, user_id, sid)
                    messages = list(tracker.get_session_messages(sid) or [])
                kept, dropped = split_off_last_exchange(messages)
                if dropped is None:
                    return None
                tracker.set_session_messages(sid, kept)
                if _session_service:
                    metadata = tracker.get_session_metadata(sid) or {}
                    # The agent's own default only where the record has none, and
                    # read defensively: Agent.agent_config may be None (the agent
                    # guards it itself), and reaching through it eagerly turns a
                    # /undo into a 500.
                    saved = await _session_service.save_session(
                        owner_agent,
                        user_id,
                        sid,
                        metadata.get("agent_name") or owner_agent.name,
                        metadata.get("llm_profile") or getattr(
                            getattr(owner_agent, "agent_config", None),
                            "default_llm_profile", None) or "default",
                        was_new_session=False,
                    )
                    if not saved:
                        # Answered with the exchange gone while the record still has it -- and the cut left in
                        # memory for the next save to write after all.
                        tracker.set_session_messages(sid, messages)
                        raise HTTPException(
                            status_code=500, detail=f"Session {sid} could not be saved; nothing was taken back.")
                return dropped
        finally:
            _let_go(owner_agent, held, user_id)

    @app.post("/sessions")
    async def create_session():
        """Create a new session id for multi-turn conversations."""
        sid = short_id()
        # Pre-create empty session in agent using the component API
        agent._session_tracker.set_session_messages(sid, [])
        return {"session_id": sid}

    @app.post("/sessions/{session_id}/append")
    async def append_to_session_endpoint(session_id: str, request: Request,
                                         force: bool = Query(default=False)):
        """Append a user message directly to a session (no active request required).
        
        This endpoint adds a user message to an existing session and persists it to disk.
        """
        logger = logging.getLogger(__name__)
        
        # ========================================
        # SECURITY: Enforce endpoint authentication
        # ========================================
        current_user = await _enforce_endpoint_security(request)
        user_id = current_user.username if current_user else "anonymous"
        
        body = await _parse_json_body(request)
        try:
            content = body.get('content')
            if not content:
                raise HTTPException(status_code=400, detail="Missing 'content' in body")

            logger.debug("Session append request for session_id=%s: %.120s", session_id, content)

            owner_agent = await _session_owner_agent(request, session_id, user_id)
            # Ownership check before mutating someone else's session (IDOR) -- against the tracker the append
            # writes, not the entry agent's (a session without an owner on disk passes there for anybody).
            await _verify_session_owner(session_id, current_user, owner_agent._session_tracker)

            if not await _append_and_persist(owner_agent, session_id, content, user_id, force):
                raise HTTPException(status_code=404, detail="Session not found")

            return {"status": "appended", "session_id": session_id}
        except HTTPException:
            # Client errors (400 missing content, 403 ownership, 404) must
            # keep their status — the generic handler below turned them
            # into 500s.
            raise
        except Exception as e:
            logger.exception("Failed to append to session %s: %s", session_id, e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/sessions/{session_id}/force_optimize")
    async def force_optimize_session(session_id: str, request: Request):
        """Gone: context optimization runs automatically via hook plugins.

        The manual path died with the hook migration -- no Agent carries a
        ``token_optimizer`` anymore, so this endpoint answered "ok" for a
        long time without doing anything. 410 tells the caller the truth
        instead of pretending success. No shipped consumer (UI or repo
        code) calls it.
        """
        await _enforce_endpoint_security(request)
        raise HTTPException(
            status_code=410,
            detail=(
                "Manual optimization was removed: context optimization and "
                "summarization run via the context_engineer/"
                "context_summarizer hook plugins during LLM calls."
            ),
        )

    @app.post("/sessions/force_optimize")
    async def force_optimize_all_sessions(request: Request):
        """Gone -- see force_optimize_session."""
        await _enforce_endpoint_security(request)
        raise HTTPException(
            status_code=410,
            detail=(
                "Manual optimization was removed: context optimization and "
                "summarization run via the context_engineer/"
                "context_summarizer hook plugins during LLM calls."
            ),
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

    @app.get("/tools/status")
    async def tools_status(force_refresh: bool = False):
        """Get tool server status including plugins and external servers.

        Args:
            force_refresh: If True, invalidates cache before fetching status
        """
        try:
            logger = logging.getLogger(__name__)

            # Use ToolServerService for comprehensive status
            global _tool_server_service, _app_registry, _tool_integration

            if not _tool_server_service:
                return {"error": "tool server service not initialized"}

            if not _app_registry:
                return {"error": "Registry not initialized"}

            # If force_refresh requested, invalidate cache first
            if force_refresh and _tool_integration:
                try:
                    await _tool_integration.invalidate_tools_cache()
                    logger.debug("Cache invalidated due to force_refresh=True")
                except Exception as e:
                    logger.warning(f"Failed to invalidate cache: {e}")

            # Delegate to ToolServerService
            status = await _tool_server_service.get_comprehensive_status(
                registry=_app_registry,
                check_connectivity=True  # Always check connectivity for accurate status
            )

            return status

        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"Tool status error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to get tool status: {str(e)}"}

    @app.get("/tools/cache/statistics")
    async def tools_cache_statistics():
        """Get tool cache statistics for monitoring."""
        try:
            global _tool_integration

            if not _tool_integration:
                return {"error": "tool integration not initialized"}

            # Get cache statistics from tool integration
            stats = await _tool_integration.get_cache_statistics()
            return {
                "success": True,
                "cache": stats
            }

        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"Tool cache statistics error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to get cache statistics: {str(e)}"}

    @app.post("/tools/cache/invalidate")
    async def tools_cache_invalidate():
        """Manually invalidate the tool cache."""
        try:
            global _tool_integration

            if not _tool_integration:
                return {"error": "tool integration not initialized"}

            # Invalidate the cache
            await _tool_integration.invalidate_tools_cache()

            return {
                "success": True,
                "message": "Cache invalidated successfully"
            }

        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"Tool cache invalidation error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to invalidate cache: {str(e)}"}

    # ===========================
    # Chat Command Endpoints
    # ===========================
    #
    # The web UI resolves a typed line through the SAME catalogue and parser as
    # the terminal chat (agent_system.chat_commands). The browser cannot read
    # the skill folders, so expansion happens here -- one implementation, and
    # `/writer x` means the same thing on both surfaces.

    def _skill_registry_for_app():
        """Registry scanned with the roots the CONFIG resolves to."""
        from agent_system.skills import get_skill_registry
        from agent_system.skills.registry import default_skill_dirs

        configured = list(
            getattr(getattr(_live_config(), "skills", None), "skill_dirs", []) or []
        )
        registry = get_skill_registry()
        registry.ensure_discovered(configured or list(default_skill_dirs()))
        return registry

    def _chat_agent(request: Request, agent_name: Optional[str]):
        """The agent a chat surface is talking to, or None.

        Plugin commands are per AGENT: the list holds only what that agent's
        own allowlist lets it dispatch, so every chat endpoint that touches
        them has to know which agent is meant. A name that is not a registered
        agent yields None rather than falling back to the default -- answering
        for a different agent would list commands the named one may not run.
        """
        from .servers.agent.server import Agent as _Agent

        if not agent_name:
            entry = getattr(request.app.state, "agent", None)
            return entry if isinstance(entry, _Agent) else None
        registry = getattr(request.app.state, "tool_registry", None) or _app_registry
        try:
            candidate = registry.get(agent_name) if registry is not None else None
        except Exception as e:
            logging.getLogger(__name__).debug("No agent '%s': %s", agent_name, e)
            return None
        return candidate if isinstance(candidate, _Agent) else None

    def _plugin_commands_for(agent) -> list:
        """What *agent* may run, empty for anything that cannot be asked."""
        if agent is None:
            return []
        from agent_system.plugin_commands import collect_plugin_commands
        try:
            return collect_plugin_commands(agent)
        except Exception as e:  # noqa: BLE001 - a broken plugin must not kill the chat
            logging.getLogger(__name__).warning("Could not collect plugin commands: %s", e)
            return []

    @app.get("/chat/commands")
    async def chat_commands(request: Request, surface: str = "web",
                            agent: Optional[str] = None):
        """Commands and skills this surface offers, for help and autocomplete.

        ``agent`` is not decoration: plugin commands differ per agent, so the
        browser has to say which one it is talking to -- and ask again when
        the selector changes. A caller that names none gets the entry agent's
        list, the same agent /run would have used.
        """
        from agent_system.chat_commands import commands_for, runnable_skill_names
        from agent_system.plugin_commands import spellings

        try:
            # Only what this parser can actually reach: the same filter the
            # terminal uses. A skill named "3d-print" was offered here and
            # then went to the model as a message; one named "tools" was
            # offered and ran the built-in.
            listed = {s.name: s for s in _skill_registry_for_app().list_skills()}
            skills = [
                {"name": s.name, "summary": s.description, "version": s.version,
                 "kind": "skill", "display": f"/{s.name}"}
                for s in (listed[name] for name in runnable_skill_names(listed))
            ]
        except Exception as e:
            logging.getLogger(__name__).warning("Could not list skills: %s", e)
            skills = []

        plugin_commands = _plugin_commands_for(_chat_agent(request, agent))
        return {
            "commands": [
                {"name": c.name, "aliases": list(c.aliases), "summary": c.summary,
                 "display": c.display, "kind": "command"}
                for c in commands_for(surface)
            ],
            "skills": skills,
            # The SPELLING, not just the name: a plugin command whose name a
            # built-in already owns is only reachable as "plugin:name", and
            # offering the bare one would land on the built-in instead.
            "plugin_commands": [
                {"name": c.name, "qualified": c.qualified, "spelling": spelling,
                 "summary": c.summary, "argument_hint": c.argument_hint,
                 "kind": "plugin",
                 "display": f"/{spelling}" + (f" {c.argument_hint}" if c.argument_hint else "")}
                for spelling, c in zip(spellings(plugin_commands), plugin_commands)
            ],
        }

    @app.post("/chat/resolve")
    async def chat_resolve(request: Request):
        """Classify a typed line, expanding a skill invocation into its text.

        Returns ``kind`` (command | skill | message | unknown) plus what the
        caller needs: the command name, or the message to send to the agent.
        """
        from agent_system.chat_commands import (
            resolve as resolve_line,
            runnable_skill_names,
            suggest_command,
        )
        from agent_system.plugin_commands import spellings
        from agent_system.skills import invoke

        body = await _parse_json_body(request)
        if body is not None and not isinstance(body, dict):
            # A JSON array or bare string parses fine but has no .get -- answer
            # "bad request" rather than letting an AttributeError become a 500.
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        line = (body or {}).get("line") or ""
        if not isinstance(line, str):
            raise HTTPException(status_code=400, detail="'line' must be a string")
        if len(line) > MAX_CHAT_LINE:
            raise HTTPException(
                status_code=413,
                detail=f"Line too long ({len(line)} chars, limit {MAX_CHAT_LINE})",
            )

        try:
            registry = _skill_registry_for_app()
            # The same names the listing offers -- a typo hint pointing at a
            # skill nobody can invoke is worse than none.
            skill_names = runnable_skill_names(s.name for s in registry.list_skills())
        except Exception as e:
            logging.getLogger(__name__).warning("Could not list skills: %s", e)
            registry, skill_names = None, []

        # The agent decides which plugin commands exist at all, so an
        # unnamed one leaves "/compact" the unknown command it was before.
        plugin_commands = _plugin_commands_for(
            _chat_agent(request, (body or {}).get("agent_name")))

        result = resolve_line(line, skill_names, plugin_commands)
        payload = {"kind": result.kind, "name": result.name, "payload": result.payload}

        if result.kind == "skill" and registry is not None:
            skill = registry.get(result.name)
            if skill is None:
                # Vanished between listing and reading -- say so instead of
                # sending the raw "/name" to the agent as if it were a message.
                return {"kind": "unknown", "name": None, "payload": f"/{result.name}",
                        "error": f"Skill '{result.name}' is no longer available"}
            try:
                payload["text"] = invoke(skill, result.payload)
            except OSError as e:
                raise HTTPException(
                    status_code=500, detail=f"Could not read skill '{result.name}': {e}"
                ) from e
        elif result.kind == "message":
            payload["text"] = result.payload
        elif result.kind == "unknown":
            # Plugin spellings compete for the typo hint too, or "/compac"
            # would be told about skills only.
            payload["suggestion"] = suggest_command(
                result.payload, list(skill_names) + spellings(plugin_commands))

        return payload

    def _vars_tracker(request: Request, agent_name: Optional[str]):
        """The session tracker of the agent a chat surface is talking to.

        Template variables live on the AGENT's tracker, not in the session
        file: that is what ``server.py`` reads per turn and hands to the prompt
        strategy, and what ``session_service`` later syncs into the persisted
        ``context_vars``. Writing anywhere else would show a changed value in
        the panel while the next turn still rendered the old one.
        """
        agent = _chat_agent(request, agent_name)
        return getattr(agent, "_session_tracker", None) if agent is not None else None

    async def _effective_vars(tracker: Any, user_id: str, session_id: str) -> dict:
        """What the next turn will really see: persisted, then runtime on top.

        The tracker alone is not the answer on this surface. A session opened
        in the browser is not loaded into the tracker until a turn runs, so
        reading only the tracker reported "no variables" for a session whose
        file is full of them -- and, far worse since the write became a
        REPLACE, an `unset` computed from that empty base would have persisted
        an empty set and wiped the rest.

        Same merge order as GET /api/sessions/{id} (see api/session_endpoints
        _live_merge_vars), so the panel and this command cannot disagree. The
        terminal needs no such fallback: its tracker is seeded from the session
        at startup and on /resume.
        """
        merged: dict = {}
        manager = getattr(_session_service, "session_manager", None)
        if manager is not None and user_id:
            try:
                stored = (await manager.load_session(user_id, session_id)) or {}
                if isinstance(stored.get("context_vars"), dict):
                    merged.update(stored["context_vars"])
            except Exception:
                # No file yet, or not readable: the tracker is then the only
                # truth there is, which is the normal case for a new session.
                logging.getLogger(__name__).debug(
                    "No persisted context_vars for %s", session_id, exc_info=True)
        if tracker is not None:
            merged.update(tracker.get_session_template_vars(session_id) or {})
        return merged

    @app.get("/chat/vars")
    async def chat_vars(request: Request):
        """Template variables of one session, as the next turn will see them."""
        current_user = await _enforce_endpoint_security(request)
        session_id = (request.query_params.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="'session_id' is required")

        # Tracker FIRST, then the ownership check against that same tracker:
        # each agent has its own, so verifying against the default one asked
        # the wrong object and found no owner.
        tracker = _vars_tracker(request, request.query_params.get("agent_name"))
        if tracker is None:
            raise HTTPException(status_code=404, detail="No such agent")
        await _verify_session_owner(session_id, current_user, tracker)
        owner = current_user.username if current_user else "anonymous"
        return {"session_id": session_id,
                "vars": await _effective_vars(tracker, owner, session_id)}

    @app.post("/chat/vars")
    async def chat_vars_update(request: Request):
        """Set, unset or clear a session's template variables.

        The BODY carries the raw rest of the ``/vars`` line, not a parsed dict:
        the grammar is then read by the same ``parse_vars`` the terminal uses,
        so ``/vars greeting="hallo welt"`` cannot come to mean two different
        things depending on which surface it was typed into.
        """
        from agent_system.chat_commands import apply_vars, parse_vars, store_vars

        current_user = await _enforce_endpoint_security(request)
        body = await _parse_json_body(request)
        if body is not None and not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        body = body or {}

        session_id = str(body.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="'session_id' is required")
        payload = body.get("payload") or ""
        if not isinstance(payload, str):
            raise HTTPException(status_code=400, detail="'payload' must be a string")
        if len(payload) > MAX_CHAT_LINE:
            raise HTTPException(
                status_code=413,
                detail=f"Payload too long ({len(payload)} chars, limit {MAX_CHAT_LINE})")
        tracker = _vars_tracker(request, body.get("agent_name"))
        if tracker is None:
            raise HTTPException(status_code=404, detail="No such agent")
        await _verify_session_owner(session_id, current_user, tracker)

        parsed = parse_vars(payload)
        owner = current_user.username if current_user else "anonymous"
        # The persisted set has to be in the base, or an unset computed from an
        # empty tracker would persist an empty set over a full file.
        current = await _effective_vars(tracker, owner, session_id)
        if parsed.errors:
            # Refused whole, like the terminal: half-applying a line leaves the
            # person guessing which half took.
            return {"session_id": session_id, "vars": current,
                    "errors": list(parsed.errors), "changed": False}
        persisted = False
        if not parsed.is_query:
            current = apply_vars(current, parsed)
            manager = getattr(_session_service, "session_manager", None)
            persisted = await store_vars(tracker, manager, owner, session_id, current)
        return {"session_id": session_id, "vars": current, "errors": [],
                "changed": not parsed.is_query, "persisted": persisted}

    @app.post("/chat/command")
    async def chat_command(request: Request):
        """Run a plugin command, the way the terminal runs it.

        The caller names a COMMAND, never a tool. It is looked up in the list
        this agent may run -- collect_plugin_commands filters by the agent's
        own dispatch predicate -- and execution goes through
        Agent.dispatch_tool_call, the same path with the same authorization,
        runtime params and status channel the terminal uses. A command the
        agent may not run does not exist here, so this is no second entry
        point beside the tools: exactly as powerful as the allowlist permits.
        """
        from agent_system.chat_commands import match_plugin_command
        from agent_system.plugin_commands import run_plugin_command

        current_user = await _enforce_endpoint_security(request)
        body = await _parse_json_body(request)
        if body is not None and not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        body = body or {}

        name = body.get("name") or ""
        command_payload = body.get("payload") or ""
        if not isinstance(name, str) or not isinstance(command_payload, str):
            raise HTTPException(status_code=400,
                                detail="'name' and 'payload' must be strings")
        if len(command_payload) > MAX_CHAT_LINE:
            raise HTTPException(
                status_code=413,
                detail=f"Payload too long ({len(command_payload)} chars, "
                       f"limit {MAX_CHAT_LINE})")

        # The command runs ON a session -- compaction rewrites it -- so the
        # same ownership rule the other session-taking endpoints apply holds
        # here: without it any authenticated user could hand in a foreign
        # session id and have a tool act on it.
        session_id = body.get("session_id")
        if session_id:
            await _verify_session_owner(str(session_id), current_user)

        agent = _chat_agent(request, body.get("agent_name"))
        if agent is None:
            raise HTTPException(status_code=404, detail="no such agent")
        match = match_plugin_command(name.lstrip("/"), _plugin_commands_for(agent))
        if match is None:
            raise HTTPException(
                status_code=404,
                detail=f"'{name}' is not a command this agent can run")

        text = await run_plugin_command(
            agent, match, command_payload,
            session_id=session_id,
            user_id=getattr(current_user, "username", None))
        return {"name": match.qualified, "text": text}

    @app.post("/chat/undo")
    async def chat_undo(request: Request, force: bool = Query(default=False)):
        """Drop the last question and everything that answered it.

        `/undo` and the cut half of `/retry`. The browser reloads a session
        from disk on every message, so the record is what has to shrink --
        and the agent's copy in memory with it, or the next save would put
        the dropped turn straight back.

        The text comes back so the caller can offer it again (`/retry` puts
        it in the input). Whether it carried a file is said, not sent: the
        browser attaches from the viewer's disk, and a data URL handed back
        would be a second, silent upload.
        """
        from .chat_actions import message_text, message_role

        current_user = await _enforce_endpoint_security(request)
        user_id = current_user.username if current_user else "anonymous"
        body = await _parse_json_body(request)
        if body is not None and not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        body = body or {}

        session_id = body.get("session_id")
        if not session_id or not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="'session_id' is required")

        # The agent the SESSION ran with, before the one the caller names:
        # every agent carries its own SessionTracker, so cutting the wrong
        # one leaves the exchange standing in the right one -- and its next
        # save writes it back. The caller's name is the fallback for a session
        # that has no record yet.
        # A turn settling the session first, though: its agent holds the copy that turn puts back (_settling_agent).
        target_agent = _settling_agent(request, session_id)
        if target_agent is None:
            ran_with = await _session_agent_name(session_id, user_id)
            target_agent = _chat_agent(request, ran_with or body.get("agent_name"))
        if target_agent is None:
            raise HTTPException(status_code=404, detail="no such agent")
        # Against the tracker this endpoint is about to write, not the entry
        # agent's: a session that is not persisted yet has no owner on disk,
        # and the default tracker does not know it either -- so that check
        # passes for anybody (the IDOR _verify_session_owner documents).
        await _verify_session_owner(session_id, current_user,
                                    getattr(target_agent, "_session_tracker", None))

        dropped = await _drop_last_exchange_and_persist(
            target_agent, session_id, user_id, force)
        if dropped is None:
            return {"session_id": session_id, "dropped": None}
        content = getattr(dropped, "content", None)
        return {
            "session_id": session_id,
            "dropped": {
                "role": message_role(dropped),
                "text": message_text(dropped),
                "had_attachments": isinstance(content, list) and len(content) > 1,
            },
        }

    @app.get("/chat/context")
    async def chat_context(request: Request, session_id: str = Query(...),
                           agent_name: Optional[str] = Query(default=None)):
        """What fills the context window of a session -- `/context`.

        Two blocks that are never mixed: what the provider COUNTED on the last
        call (the usage tracker keeps it, with the window it was counted
        against), and what the conversation holds NOW, estimated per kind. A
        category worked out as "measured minus estimated" would look exact and
        carry the error of both.

        The system prompt and the tool schemas are read off the agent that
        RAN the session, because that is whose prompt and whose tools sit in
        that window -- another agent's numbers would describe a chat that
        never happened.
        """
        from .chat_actions import (
            context_breakdown, live_context_window, measured_context,
            profile_context_window)

        current_user = await _enforce_endpoint_security(request)
        user_id = current_user.username if current_user else "anonymous"
        # One read: the agent it ran with, the profile its next call goes out
        # on, and the conversation itself all live in the same record.
        record = await _session_record(session_id, user_id)
        ran_with = record.get("agent_name") or None
        ran_profile = record.get("llm_profile") or None
        target_agent = _chat_agent(request, ran_with or agent_name)
        if target_agent is None:
            raise HTTPException(status_code=404, detail="no such agent")
        await _verify_session_owner(session_id, current_user,
                                    getattr(target_agent, "_session_tracker", None))

        messages: list = record.get("messages") or []

        prompt, tools = "", []
        try:
            # With the session id: the prompt this SESSION sends, template
            # vars and all. Rendered without them it is short by the whole
            # var payload, on the one line the command exists to show.
            prompt, tools = await target_agent.describe_context_inputs(session_id)
        except Exception as e:  # noqa: BLE001 - a missing line, not a failed request
            logging.getLogger(__name__).warning("No context inputs for %s: %s",
                                                session_id, e)

        return {
            "session_id": session_id,
            "agent_name": getattr(target_agent, "name", ran_with or ""),
            # The window the NEXT call runs against -- of the profile the
            # SESSION is on, which is what a model picked in the panel
            # changes; the agent's own client only answers without one. The
            # measurement below carries the window IT was counted against.
            "window": (profile_context_window(target_agent, ran_profile)
                       or live_context_window(target_agent)),
            # What the provider counted, kept in its own box -- and only for a
            # session that HAS a record: the tracker is keyed by session id
            # alone, and an unpersisted id passes the ownership check.
            "last_call": measured_context(target_agent, session_id) if ran_with else {},
            "estimated": context_breakdown(messages, system_prompt=prompt, tools=tools),
        }

    @app.get("/chat/transcript")
    async def chat_transcript(request: Request, session_id: str = Query(...)):
        """The conversation as markdown -- `/export`, for whoever asks.

        With the other /chat endpoints rather than under /sessions, because
        this is what a chat command produces, and the rendering is the one
        the terminal writes to a file (chat_actions.transcript_markdown).

        Rendered from the RECORD, not from a running agent's memory: the
        browser shows the record, and a transcript that disagrees with what
        is on screen is worse than none. text/markdown with a filename, so a
        browser saves it instead of painting it.
        """
        from .chat_actions import transcript_markdown

        current_user = await _enforce_endpoint_security(request)
        user_id = current_user.username if current_user else "anonymous"
        await _verify_session_owner(session_id, current_user)
        if not _session_service or not _session_service.session_manager:
            raise HTTPException(status_code=503, detail="No session storage")

        # By the kind of failure, not by "anything went wrong": a corrupt
        # record read as "no such session" would send someone looking for a
        # session id that is right there in their list.
        from .services.session_manager import SessionNotFoundError, SessionPermissionError

        try:
            record = await _session_service.session_manager.load_session(user_id, session_id)
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail=f"No session '{session_id}'")
        except SessionPermissionError:
            raise HTTPException(status_code=403, detail=f"Not your session '{session_id}'")

        if not (record.get("messages") or []):
            # A file holding nothing but a heading, reported as written, is
            # what the terminal refuses too ("Nothing to export"). Reachable
            # right after /undo takes the only exchange out.
            raise HTTPException(
                status_code=409,
                detail=f"Session '{session_id}' has no messages yet")
        markdown = transcript_markdown(
            record.get("messages") or [],
            agent_name=record.get("agent_name") or "agent",
            session_id=session_id,
            llm=record.get("llm_profile") or "",
        )
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="chat-{session_id}.md"'},
        )

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
