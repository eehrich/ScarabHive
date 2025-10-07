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
from typing import Optional, Callable

import uvicorn
from fastapi import FastAPI, Request, Query, Header
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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
from ..context.agent_tracker import record_agent_summarization


# Global registry for MCP endpoints access
_app_registry: Optional[MCPRegistry] = None
_app_config: Optional[AgentConfig] = None
_mcp_integration = None


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

    # Load configuration from config.yaml (includes llm.yaml and mcp.yaml)
    if not config_path:
        cfg_path = str(Path(__file__).parents[3] / "config" / "config.yaml")
    else:
        cfg_path = config_path
        
    config = load_settings(cfg_path)
    
    # Log configuration status
    logger = logging.getLogger(__name__)
    logger.info(f"Loading configuration from: {cfg_path}")
    if config.llm_system:
        logger.debug(f"LLM system loaded with {len(config.llm_system.profiles)} profiles")
    else:
        logger.warning("No llm_system configuration loaded")
    if config.mcp_system:
        logger.debug(f"MCP system loaded with {len(config.mcp_system.servers or {})} servers")
    else:
        logger.warning("No mcp_system configuration loaded")

    # Initialize MCP integration
    async def _init_mcp_for_app(app: FastAPI):
        global _mcp_integration
        logger = logging.getLogger(__name__)
        logger.info("Starting MCP integration initialization...")
        try:
            mcp_integration = await initialize_mcp(config, app)
            _mcp_integration = mcp_integration
            
            # Make integration accessible to mcp module
            from ..mcp import integration as _mcp_mod
            _mcp_mod.mcp_integration = mcp_integration
            
            logger.info("MCP integration initialized for API")

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
                    if not config.servers:
                        ValueError("No servers config to apply overrides from")

                    server_cfg = config.servers.get(entry_name, {})
                    overrides = server_cfg.get('agent_config', {}) if isinstance(server_cfg, dict) else {}
                    if isinstance(overrides, dict) and overrides:
                        needs_copy = any(k in overrides for k in ('allowed_tools', 'blocked_tools')) or 'max_steps' in server_cfg
                        if needs_copy:
                            updates = {}
                            if overrides.get('allowed_tools') is not None and getattr(selected_agent.agent_config, 'allowed_tools', None) is None:
                                try:
                                    updates['allowed_tools'] = list(overrides.get('allowed_tools') or [])
                                except Exception:
                                    pass
                            if overrides.get('blocked_tools') is not None and getattr(selected_agent.agent_config, 'blocked_tools', None) is None:
                                try:
                                    updates['blocked_tools'] = list(overrides.get('blocked_tools') or [])
                                except Exception:
                                    pass
                            if 'max_steps' in server_cfg and isinstance(server_cfg.get('max_steps'), int):
                                try:
                                    updates['max_steps'] = int(server_cfg.get('max_steps'))
                                except Exception:
                                    pass
                            if updates:
                                selected_agent.agent_config = selected_agent.agent_config.model_copy(update=updates)
                            logging.getLogger(__name__).debug("Applied entry agent server overrides for %s", entry_name)
                except Exception:
                    logging.getLogger(__name__).debug("Failed to apply entry agent overrides for %s", entry_name)
    except Exception:
        selected_agent = None

    # Create new agent if not found in registry
    if selected_agent is None:
        from ..servers.agent.server import Agent as CoreAgent
        try:
            if not config.servers:
                logging.getLogger(__name__).warning(
                    "No 'servers' configuration found, using empty server config for agent '%s'", entry_name
                )
                server_cfg = {}
            else:
                server_cfg = config.servers.get(entry_name, {})
                
            # Apply agent-specific overrides from server config
            server_agent_cfg = server_cfg.get('agent_config', {}) if isinstance(server_cfg, dict) else {}
            if isinstance(server_agent_cfg, dict):
                updates = {}
                if server_agent_cfg.get('allowed_tools') and not config.allowed_tools:
                    try:
                        updates['allowed_tools'] = list(server_agent_cfg.get('allowed_tools'))
                    except Exception:
                        pass
                if server_agent_cfg.get('blocked_tools') and not config.blocked_tools:
                    try:
                        updates['blocked_tools'] = list(server_agent_cfg.get('blocked_tools'))
                    except Exception:
                        pass
                if updates:
                    config = config.model_copy(update=updates)
        except Exception:
            pass
        
        # Build MCPConfig for agent
        from agent_system.config.models import MCPConfig, AgentConfig, ToolConfig
        if config.mcp_system and config.mcp_system.default_config:
            mcp_cfg = config.mcp_system.default_config
        else:
            # Create default MCPConfig if not found
            logging.getLogger(__name__).warning(
                "No mcp_system.default_config found in configuration, creating default MCPConfig with llm_profile='normal'"
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
        except Exception:
            pass

    # Register 'agent' alias for backward compatibility
    if entry_name != 'agent':
        try:
            registry.register('agent', selected_agent)
        except Exception:
            pass
    agent = selected_agent

    # Store registry and config globally
    global _app_registry, _app_config
    _app_registry = registry
    _app_config = config

    # Include API router
    app.include_router(api_router)

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
        except Exception:
            pass  # Continue with empty config if loading fails

        return {
            "status": "ok",
            "version": agent_config.get("version", "unknown"),
            "name": agent_config.get("name", "AgentSystem"),
            "uptime_seconds": round(uptime_seconds, 2),
            "timestamp": datetime.now().isoformat()
        }

    @app.get("/config")
    def get_config():
        return config.model_dump()

    @app.get("/agents")
    def list_agents():
        """List registered agent-like servers (those extending Agent)."""
        agents = []
        try:
            for name in _app_registry.list():  # type: ignore[attr-defined]
                try:
                    srv = _app_registry.get(name)  # type: ignore[attr-defined]
                    from ..servers.agent.server import Agent as _Agent
                    if isinstance(srv, _Agent):
                        agents.append(name)
                except Exception:
                    continue
        except Exception:
            pass
        return {"agents": agents}

    @app.get("/agents/{agent_name}/allowed-tools")
    async def get_agent_allowed_tools(agent_name: str):
        """Return the effective allowed tools list for an agent after pattern filtering."""
        try:
            srv = _app_registry.get(agent_name)  # type: ignore[attr-defined]
        except Exception:
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
        except Exception:
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
        except Exception:
            return {"error": "agent not found", "agent": agent_name}
        from ..servers.agent.server import Agent as _Agent
        if not isinstance(srv, _Agent):
            return {"error": "not an agent", "agent": agent_name}
        try:
            return await srv.get_current_system_prompt()
        except Exception as e:  # pragma: no cover - defensive
            return {"error": str(e), "agent": agent_name}

    @app.post("/run")
    async def run(task: str, traceparent: Optional[str] = Header(default=None)):
        logger = logging.getLogger(__name__)
        request_id = short_id()
        logger.info("/run invoked, task=%s, request_id=%s", task, request_id)
        from agent_system.servers.agent.result_utils import collect_final_result
        return await collect_final_result(agent, task, request_id=request_id)

    @app.get("/events")
    async def events(task: str, session_id: Optional[str] = Query(default=None)):
        logger = logging.getLogger(__name__)
        request_id = short_id()
        logger.info("SSE /events connected, task=%s, request_id=%s, session_id=%s", task, request_id, session_id)

        async def event_stream():
            # Initial keep-alive line
            yield ":ok\n\n"
            async for ev in agent.run_events(task, request_id, session_id):
                logger.debug("SSE event: %s", ev.get("type"))
                try:
                    # Ensure proper JSON serialization of any potential enum values
                    if hasattr(ev, 'to_dict'):
                        payload = ev.to_dict()
                    else:
                        payload = ev
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except (TypeError, ValueError) as e:
                    logger.error("Failed to serialize event %s: %s", ev, e)
                    # Send an error event instead
                    error_payload = {"type": "error", "message": f"Serialization error: {str(e)}"}
                    yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"

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

            if getattr(agent, 'context_manager', None):
                try:
                    async with agent._request_lock:
                        msgs = list(agent._sessions.get(session_id, []))
                    cm = agent.context_manager
                    # If a dedicated summarizer is available, run a forced summarization
                    if getattr(cm, '_summarizer', None):
                        new_msgs = await cm._summarize_conversation(msgs)
                        # Record summarization in agent tracking
                        try:
                            agent_name = getattr(agent, 'name', 'unknown_agent')
                            record_agent_summarization(agent_name)
                        except Exception as e:
                            logger.debug("Failed to record summarization: %s", e)
                    else:
                        # No dedicated summarizer; fall back to normal management which may or may not summarize
                        new_msgs = await cm.manage_context(msgs)

                    async with agent._request_lock:
                        agent._sessions[session_id] = new_msgs
                    actions['summarizer'] = True
                except Exception as e:
                    logger.exception("Failed to run summarizer for session %s: %s", session_id, e)

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
                    if getattr(agent, 'context_manager', None):
                        cm = agent.context_manager
                        msgs = list(agent._sessions.get(sid, []))
                        if getattr(cm, '_summarizer', None):
                            new_msgs = await cm._summarize_conversation(msgs)
                            # Track summarization for UI display
                            try:
                                agent_name = getattr(agent, 'name', 'unknown_agent')
                                record_agent_summarization(agent_name)
                            except Exception as track_e:
                                logger.debug("Failed to track summarization for %s: %s", agent_name, track_e)
                        else:
                            new_msgs = await cm.manage_context(msgs)

                        async with agent._request_lock:
                            agent._sessions[sid] = new_msgs
                        actions['summarizer'] = True
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
                        except Exception:
                            logger.exception("Failed to serialize status event")

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
                        except Exception:
                            break
                        try:
                            payload = ev.to_dict()
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        except Exception:
                            logger.exception("Failed to serialize status event during drain")
                except Exception:
                    # Ignore issues while draining to ensure cleanup continues
                    pass
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
            except Exception:
                logging.getLogger(__name__).debug("Failed to set root logger level during debug toggle")

            return {"debug": bool(app.state.debug_enabled)}
        except Exception as e:
            logging.getLogger(__name__).exception("Debug toggle failed: %s", e)
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/debug/context")
    async def debug_context():
        """Diagnostic endpoint to get current context management state and conversation messages."""
        try:
            # Get the agent from registry if available
            agent = None
            if _app_registry:
                # Use the configured entry agent name instead of hardcoded "agent"
                entry_name = config.default_agent or 'agent'
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

            # Get context manager usage stats
            usage_stats = agent.context_manager.get_usage_stats() if hasattr(agent.context_manager, 'get_usage_stats') else {}

            # Get current conversation messages if available
            messages = []
            if hasattr(agent, '_current_messages') and agent._current_messages:
                # Estimate tokens for each message and prepare for display
                for i, msg in enumerate(agent._current_messages):
                    # Make sure msg is a ChatMessage object before estimating tokens
                    if not isinstance(msg, ChatMessage):
                        # Attempt to convert dict to ChatMessage if possible
                        try:
                            msg = ChatMessage(**msg)
                        except (TypeError, ValueError):
                            # Skip if conversion fails
                            continue

                    # Use context manager's estimate_token_count method
                    estimated_tokens = agent.context_manager.estimate_token_count([msg])
                    messages.append({
                        "role": msg.role,
                        "content": msg.content,
                        "estimated_tokens": estimated_tokens,
                        "has_tool_calls": bool(msg.tool_calls)
                    })

            return {
                "context_window": usage_stats.get("context_window", "N/A"),
                "prediction_threshold": usage_stats.get("prediction_threshold", 0),
                "summarization_threshold": usage_stats.get("summarization_threshold", "N/A"),
                "actual_usage": usage_stats.get("actual_usage", {"total_tokens": 0, "last_call_tokens": 0}),
                "warning_levels": usage_stats.get("warning_levels", {}),
                "messages": messages,
                "message_count": len(messages)
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

    @app.get("/debug/context/usage")
    async def debug_context_usage():
        """Get context usage tracking data for monitoring and debugging."""
        try:
            from ..context.tracker import get_tracker
            from agent_system.context.agent_tracker import get_all_agent_stats
            tracker = get_tracker()

            # Get latest snapshot and recent history
            latest = tracker.get_latest()
            recent_history = tracker.get_history(last_n=100)  # Last 100 data points



            # Get statistics for different time windows
            stats_1h = tracker.get_statistics(time_window_seconds=3600)  # Last hour
            stats_24h = tracker.get_statistics(time_window_seconds=86400)  # Last 24 hours

            # Get per-agent statistics
            agent_stats = get_all_agent_stats()

            # If tracker has no snapshots yet, synthesize a 'latest' view from per-agent stats
            if not latest:
                try:
                    total_tokens = 0
                    total_messages = 0
                    context_window = 0
                    warning_level = None

                    # agent_stats is expected to be a dict of agent_id -> stats dict
                    for aid, a in (agent_stats or {}).items():
                        try:
                            total_tokens += int(a.get('current_tokens', 0) or 0)
                            total_messages += int(a.get('message_count', 0) or 0)
                            context_window = max(context_window, int(a.get('context_window', 0) or 0))
                        except Exception:
                            continue

                    usage_percentage = (total_tokens / context_window * 100) if context_window > 0 else 0
                    latest = {
                        'timestamp': __import__('time').time(),
                        'total_tokens': total_tokens,
                        'user_tokens': 0,
                        'assistant_tokens': 0,
                        'tool_call_tokens': 0,
                        'tool_result_tokens': 0,
                        'system_tokens': 0,
                        'message_count': total_messages,
                        'context_window': context_window,
                        'usage_percentage': usage_percentage,
                        'warning_level': warning_level,
                        'session_id': None
                    }
                except Exception:
                    latest = None

            return {
                "latest": latest,
                "recent_history": recent_history,
                "statistics": {
                    "last_hour": stats_1h,
                    "last_24_hours": stats_24h,
                    "all_time": tracker.get_statistics()
                },
                "agents": {
                    "count": len(agent_stats),
                    "details": agent_stats
                }
            }
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Context usage endpoint failed: %s", e)
            return {"error": f"Failed to get context usage data: {str(e)}"}

    @app.get("/debug/context/usage/history")
    async def debug_context_usage_history(last_n: int = 50):
        """Get context usage history for graphing."""
        try:
            from ..context.tracker import get_tracker
            tracker = get_tracker()

            history = tracker.get_history(last_n=last_n)
            return {"history": history, "count": len(history)}
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Context usage history endpoint failed: %s", e)
            return {"error": f"Failed to get context usage history: {str(e)}"}

    @app.post("/debug/context/usage/clear")
    async def debug_context_usage_clear():
        """Clear context usage history (for testing/debugging)."""
        try:
            import logging
            logger = logging.getLogger(__name__)
            from ..context.tracker import get_tracker
            from ..context.accumulator import get_token_accumulator
            from ..context.agent_tracker import get_agent_tracker

            tracker = get_tracker()
            tracker.clear_history()

            # Reset persistent accumulated stats
            try:
                acc = get_token_accumulator()
                acc.reset_all_stats()
            except Exception as e:
                # If accumulator reset fails, continue clearing in-memory history
                logger.warning("Failed to reset accumulator stats during context clear: %s", e)

            # Reset per-agent in-memory counters (keep registrations)
            try:
                agent_tracker = get_agent_tracker()
                all_agents = agent_tracker.get_all_agents()
                for aid, stats in all_agents.items():
                    # Clear all relevant in-memory counters for the agent
                    stats.current_tokens = 0
                    stats.predicted_tokens = 0
                    stats.actual_tokens = 0
                    stats.message_count = 0
                    stats.summarization_count = 0
                    stats.peak_tokens = 0
                    stats.total_llm_calls = 0
                    stats.total_tokens_processed = 0
                    # Reset timestamps
                    now = __import__('time').time()
                    stats.session_start = now
                    stats.last_activity = now
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.warning("Failed to reset per-agent in-memory counters during context clear: %s", e)

            return {"result": "Context usage history and accumulated stats cleared"}
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception("Context usage clear endpoint failed: %s", e)
            return {"error": f"Failed to clear context usage history: {str(e)}"}

    def _check_server_connection(server_info):
        """Check if a server is actually responding with real-time connectivity test"""
        import socket
        import logging
        from urllib.parse import urlparse

        logger = logging.getLogger(__name__)

        try:
            if server_info.get("type") == "plugin":
                # For plugins, if they're in the registry, they're connected
                server_name = server_info.get("id", "")
                if _app_registry and hasattr(_app_registry, "_servers"):
                    server_obj = _app_registry._servers.get(server_name)
                    # If server exists in registry, it's connected (loaded and available)
                    return server_obj is not None
                return False

            elif server_info.get("type") == "external":
                # For external servers, do actual connectivity check
                url = server_info.get("url", "")
                if not url:
                    return False

                # Parse URL to get host and port
                parsed = urlparse(url)
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port

                if not port:
                    # Default ports based on scheme
                    if parsed.scheme == "https":
                        port = 443
                    else:
                        port = 80

                # Try socket connection with short timeout
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)  # 2 second timeout
                try:
                    result = sock.connect_ex((host, port))
                    return result == 0
                finally:
                    sock.close()

        except Exception as e:
            # Use logging if available, otherwise ignore
            try:
                logger = logging.getLogger(__name__)
                logger.debug(f"Connection check failed for {server_info.get('name', 'unknown')}: {e}")
            except Exception:
                # If even logging fails, truly ignore (e.g., during shutdown)
                pass
            return False

        return False

    @app.get("/mcp/status")
    async def mcp_status():
        """Get MCP server status including plugins and external servers."""
        try:
            import logging
            logger = logging.getLogger(__name__)
            # Use the global MCP integration instance that was initialized during startup
            global _mcp_integration

            # Use both the registry approach and MCP integration for plugins
            if not _app_registry:
                return {"error": "Registry not initialized"}

            servers = []

            # First, add plugin servers from the MCP HTTP server registry (where MCP plugins are registered)
            if _mcp_integration and hasattr(_mcp_integration, 'http_server'):
                try:
                    mcp_servers = _mcp_integration.http_server.servers
                    for server_id, server_obj in mcp_servers.items():
                        try:
                            # Skip servers that explicitly mark themselves as internal/private
                            if getattr(server_obj, '_mcp_public', True) is False:
                                continue

                            tools = []
                            detailed_tools = []

                            # Use unified list_tools() method for all servers
                            if hasattr(server_obj, 'list_tools'):
                                try:
                                    # Call async method to get tools
                                    mcp_tools = await server_obj.list_tools()
                                    if mcp_tools:
                                        for mcp_tool in mcp_tools:
                                            tool_name = mcp_tool.name
                                            description = mcp_tool.description
                                            parameters = mcp_tool.input_schema

                                            tools.append(tool_name)
                                            detailed_tools.append({
                                                'name': tool_name,
                                                'description': description,
                                                'parameters': parameters
                                            })
                                except Exception as e:
                                    logger.debug(f"list_tools() failed for server {server_id}: {e}")
                                    # Mark server as having no tools if list_tools() fails
                                    pass

                            # Check connection using real-time verification
                            server_info = {"id": server_id, "type": "plugin"}
                            connected = _check_server_connection(server_info)

                            servers.append({
                                "id": server_id,
                                "name": server_id.replace('_', ' ').title(),
                                "type": "plugin",
                                "connected": connected,
                                "tools": tools,
                                "detailed_tools": detailed_tools,
                                "tool_count": len(tools)
                            })
                        except Exception as e:
                            # If we can't get info about a server, mark it as disconnected
                            servers.append({
                                "id": server_id,
                                "name": server_id.replace('_', ' ').title(),
                                "type": "plugin",
                                "connected": False,
                                "tools": [],
                                "tool_count": 0,
                                "error": str(e)
                            })
                except Exception as e:
                    logger.debug(f"Failed to get MCP servers: {e}")

            # Track processed servers to avoid duplicates
            processed_server_ids = {server["id"] for server in servers}

            # Add plugin servers from bootstrap registry
            for server_id, server_obj in _app_registry._servers.items():
                try:
                    # Skip if already processed
                    if server_id in processed_server_ids:
                        continue
                    # Skip internal/private servers
                    if getattr(server_obj, '_mcp_public', True) is False:
                        continue

                    tools = []
                    detailed_tools = []

                    # Use unified list_tools() method for all servers
                    if hasattr(server_obj, 'list_tools'):
                        try:
                            # Call async method to get tools
                            mcp_tools = await server_obj.list_tools()
                            if mcp_tools:
                                for mcp_tool in mcp_tools:
                                    tool_name = mcp_tool.name
                                    description = mcp_tool.description
                                    parameters = mcp_tool.input_schema

                                    tools.append(tool_name)
                                    detailed_tools.append({
                                        'name': tool_name,
                                        'description': description,
                                        'parameters': parameters
                                    })
                        except Exception as e:
                            logger.debug(f"list_tools() failed for server {server_id}: {e}")

                    # Check connection using real-time verification
                    server_info = {
                        "id": server_id,
                        "type": "plugin"
                    }
                    connected = _check_server_connection(server_info)

                    servers.append({
                        "id": server_id,
                        "name": server_id.replace('_', ' ').title(),
                        "type": "plugin",
                        "connected": connected,
                        "tools": tools,
                        "detailed_tools": detailed_tools,
                        "tool_count": len(tools)
                    })
                except Exception as e:
                    # If we can't get info about a server, mark it as disconnected
                    servers.append({
                        "id": server_id,
                        "name": server_id.replace('_', ' ').title(),
                        "type": "plugin",
                        "connected": False,
                        "tools": [],
                        "tool_count": 0,
                        "error": str(e)
                    })

            # Try to get external servers from the global MCP integration instance
            try:
                if _mcp_integration and _mcp_integration.initialized:
                    # Get external servers from client manager (these are connected ones)
                    connected_servers = _mcp_integration.client_manager.list_clients()

                    # Get configured external servers (including failed connections)
                    configured_servers = getattr(_mcp_integration, 'configured_external_servers', {})

                    # Filter out disabled servers to prevent them from appearing
                    # as "Disconnected" in the web UI
                    try:
                        configured_servers = {
                            name: cfg
                            for name, cfg in (configured_servers or {}).items()
                            if cfg.get('enabled', True)
                        }
                    except Exception:
                        # If anything goes wrong while filtering, fall back to the
                        # original mapping so we don't hide potentially important
                        # entries in unexpected failure modes.
                        pass

                    logger = logging.getLogger(__name__)
                    logger.info(f"Connected external servers: {connected_servers}")
                    logger.info(f"Configured external servers: {list(configured_servers.keys())}")

                    # Also get any that have tools (for servers that might be configured elsewhere)
                    all_tools = await _mcp_integration.list_all_tools()
                    servers_with_tools = all_tools.get("external_servers", {})

                    # Combine connected servers with configured servers
                    all_external_servers = set(connected_servers) | set(configured_servers.keys())

                    logger.info(f"All external servers to process: {all_external_servers}")

                    for server_name in all_external_servers:
                        # Get server config for info from stored configuration
                        description = server_name.replace('_', ' ').title()
                        url = ""

                        # Try to get server config from stored configuration
                        try:
                            server_config = configured_servers.get(server_name, {})
                            if server_config:
                                if server_config.get('description'):
                                    description = server_config['description']
                                if server_config.get('url'):
                                    url = server_config['url']
                        except Exception as e:
                            logger.debug(f"Failed to get server config for {server_name}: {e}")

                        # Get tools (may be empty if server is down)
                        tools = servers_with_tools.get(server_name, [])
                        # Include all tools in tool_names - blocked status is in detailed_tools
                        tool_names = [tool["name"] for tool in tools]
                        detailed_tools = [{
                            'name': tool.get("name", "unknown"),
                            'description': tool.get("description", f"Tool from {description}"),
                            'parameters': tool.get("parameters", {}),
                            'blocked': tool.get("blocked", False)
                        } for tool in tools]

                        # Do real-time connection check
                        server_info = {
                            "id": server_name,
                            "name": description,
                            "type": "external",
                            "url": url
                        }
                        # Do real-time connection check first, then fall back to client manager list
                        reachable = _check_server_connection(server_info)
                        logger.debug(f"MCP status: external server '{server_name}' reachable={reachable} listed_in_clients={server_name in connected_servers} tool_count={len(tool_names)} url={url}")
                        if reachable:
                            connected = True
                        elif server_name in connected_servers and len(tool_names) > 0:
                            # Server has active client connection with available tools
                            connected = True
                        else:
                            # Not reachable or no tools available
                            connected = False

                        servers.append({
                            "id": server_name,
                            "name": description,
                            "type": "external",
                            "connected": connected,
                            "tools": tool_names,
                            "detailed_tools": detailed_tools,
                            "tool_count": len(tool_names),
                            "url": url
                        })
            except Exception as e:
                # If MCP integration fails, just continue with plugins only
                logger = logging.getLogger(__name__)
                logger.error(f"MCP status: Exception getting external servers: {e}")
                import traceback
                logger.error(f"MCP status traceback: {traceback.format_exc()}")

            # Separate plugins and external servers
            plugins = {}
            external_servers = {}

            for server in servers:
                server_data = {
                    "id": server["id"],
                    "name": server["name"],
                    "connected": server["connected"],
                    "tools": server["tools"],
                    "detailed_tools": server["detailed_tools"],
                    "tool_count": server["tool_count"]
                }
                if "url" in server:
                    server_data["url"] = server["url"]
                if "error" in server:
                    server_data["error"] = server["error"]

                if server["type"] == "plugin":
                    plugins[server["id"]] = server_data
                else:
                    external_servers[server["id"]] = server_data

            return {
                "plugins": plugins,
                "external_servers": external_servers,
                "servers": servers,  # Keep original for backward compatibility
                "total_servers": len(servers),
                "total_tools": sum(s["tool_count"] for s in servers)
            }

        except Exception as e:
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"MCP status error: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {"error": f"Failed to get MCP status: {str(e)}"}

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
