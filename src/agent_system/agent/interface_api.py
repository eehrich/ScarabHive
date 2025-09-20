from __future__ import annotations

import json
import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Callable

import uvicorn
from fastapi import FastAPI, Request, Query, Header
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
# Response is not needed here; FastAPI/Starlette response classes are imported where required

from ..servers.agent.server import Agent
from ..config.loader import load_config
from ..config.models import AgentConfig
from api.endpoints import router as api_router
from ..mcp.base import MCPRegistry
from ..servers.bootstrap import bootstrap_servers
from ..utils.logging import setup_logging
from ..llm.clients import ChatMessage
from ..mcp.status import status_bus, StatusEvent, get_status_metrics, publish_status
from ..mcp.integration import initialize_mcp, shutdown_mcp


# Global registry for MCP endpoints access
_app_registry: Optional[MCPRegistry] = None
_app_config: Optional[AgentConfig] = None
_mcp_integration = None  # Global reference to the initialized MCP integration


# Note: we set cache-control for static files via a small middleware in build_app()


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


def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Build and configure the FastAPI application."""

    cfg_path = config_path or str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)

    # Initialize MCP integration helper function
    async def _init_mcp_for_app(app: FastAPI):
        global _mcp_integration
        logger = logging.getLogger(__name__)
        logger.info("Starting MCP integration initialization...")
        try:
            # Use only the MCP configuration from the loaded agent.yaml config.
            # Do NOT read separate mcp.yaml files; all configuration should be
            # included via agent.yaml.
            try:
                mcp_block = config.mcp.model_dump() if hasattr(config.mcp, "model_dump") else getattr(config.mcp, "__dict__", {})

                # Add global network settings to MCP config
                if not mcp_block.get('connection'):
                    mcp_block['connection'] = {}

                # Use global ssl_verify setting if not specifically set in MCP config
                if 'ssl_verify' not in mcp_block['connection']:
                    mcp_block['connection']['ssl_verify'] = config.network.ssl_verify

                logger.info(f"MCP config loaded: external_servers={len(mcp_block.get('external_servers', {}))}")
                logger.debug(f"MCP config block: {mcp_block}")
            except Exception:
                mcp_block = getattr(config.mcp, "__dict__", {})
                logger.warning("Failed to get MCP config with model_dump, using __dict__")

            mcp_integration = await initialize_mcp({"mcp": mcp_block}, app)
            _mcp_integration = mcp_integration  # Store the initialized instance globally
            logger.info("MCP integration initialized for API")
        except Exception as e:
            logger.exception("Failed to initialize MCP integration for API: %s", e)

    # Create a custom lifespan for this app instance
    @asynccontextmanager
    async def custom_lifespan(app: FastAPI):
        # Startup
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

    # Create a new FastAPI app instance for this build
    app = FastAPI(title="Agent System (MCP)", lifespan=custom_lifespan)

    # Configure static files with cache control based on configuration
    if static_path.exists():
        static_files = StaticFiles(directory=str(static_path))
        app.mount("/static", static_files, name="static")

        # middleware to add no-cache headers for static files when configured
        if config.network.disable_cache:
            @app.middleware("http")
            async def _no_cache_static_middleware(request: Request, call_next: Callable):
                # only intercept static paths
                if request.url.path.startswith("/static"):
                    response = await call_next(request)
                    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    response.headers["Pragma"] = "no-cache"
                    response.headers["Expires"] = "0"
                    return response

                return await call_next(request)

    # Initialize logging. Use a role-specific logfile so the API server does
    # not write into the same file as the CLI (e.g., create `logs/agent-api.log`).
    def _role_logfile(base: str, role: str) -> str:
        try:
            p = Path(base)
            stem = p.stem or "agent"
            suffix = "".join(p.suffixes) or ".log"
            return str(p.with_name(f"{stem}-{role}{suffix}"))
        except Exception:
            # If path construction fails, fall back to default
            return str(Path("logs") / f"agent-{role}.log")

    # Determine logfile for API: prefer explicit per-role setting if provided.
    log_path = config.logging.file_api or _role_logfile(config.logging.file or "logs/agent.log", "api")

    # Allow overriding the configured log level via environment variable
    # (useful for temporary runs or CI). If AGENT_LOG_LEVEL is set, prefer it
    # over the value in config.logging.level. We still honor config.logging.enabled.
    env_level = os.getenv("AGENT_LOG_LEVEL")
    level_to_use = env_level if env_level else config.logging.level
    # If possible, mutate the config object so other code sees the override
    try:
        if env_level and hasattr(config, "logging") and hasattr(config.logging, "level"):
            config.logging.level = env_level
    except Exception:
        # Non-fatal if we can't assign back into the config model
        pass

    log_file = setup_logging(config.logging.enabled, level_to_use, log_path)
    if log_file:
        logging.getLogger(__name__).info("Logging initialized, file=%s", log_file)

    # Configure SSL verification
    if not config.network.ssl_verify:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ.setdefault("SSL_CERT_FILE", "")
        os.environ.setdefault("CURL_CA_BUNDLE", "")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "")

    # Initialize agent and registry
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    agent = Agent("api_agent", config, registry)
    registry.register("agent", agent)

    # Store registry and config globally for MCP endpoint access
    global _app_registry, _app_config
    _app_registry = registry
    _app_config = config

    # Include API router for debug endpoints
    app.include_router(api_router)

    # Define route handlers
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/config")
    def get_config():
        return config.model_dump()

    @app.post("/run")
    async def run(task: str, traceparent: Optional[str] = Header(default=None)):
        logging.getLogger(__name__).info("/run invoked, task=%s", task)
        return await agent.run(task)

    @app.get("/events")
    async def events(task: str, session_id: Optional[str] = Query(default=None)):
        logger = logging.getLogger(__name__)
        request_id = str(uuid.uuid4())
        logger.info("SSE /events connected, task=%s, request_id=%s, session_id=%s", task, request_id, session_id)

        async def event_stream():
            # Initial keep-alive line
            yield ":ok\n\n"
            async for ev in agent.run_events(task, request_id, session_id):
                logger.debug("SSE event: %s", ev.get("type"))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

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
        sid = str(uuid.uuid4())
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

            # Run optimizer and summarizer synchronously within event loop context
            # Use agent.context_manager and agent.token_optimizer if available
            # Return a summary of actions taken
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
                    new_msgs = await agent.context_manager.manage_context(msgs)
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
                        msgs = list(agent._sessions.get(sid, []))
                        new_msgs = await agent.context_manager.manage_context(msgs)
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

        # Trigger immediate status update for newly connected subscriber
        try:
            from ..mcp.status import publish_status, PHASE_PROGRESS
            # Only publish server summary, not connection noise
            mcp_integration = getattr(app.state, 'mcp_integration', None)
            if mcp_integration and hasattr(mcp_integration, 'client_manager'):
                connected_servers = [name for name, client in mcp_integration.client_manager.clients.items()]
                await publish_status(
                    server="AgentSystem",
                    message=f"Ready - Connected to {len(connected_servers)} MCP servers: {', '.join(connected_servers) if connected_servers else 'none'}",
                    phase=PHASE_PROGRESS,
                    meta={"connected_servers": connected_servers, "total_servers": len(connected_servers)}
                )
            else:
                await publish_status(
                    server="AgentSystem",
                    message="Ready - Status monitoring active",
                    phase=PHASE_PROGRESS,
                    meta={"status": "ready"}
                )
        except Exception as e:
            logger.debug("Failed to publish initial status for new subscriber: %s", e)

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
        # Updated to new Starlette signature: TemplateResponse(request, name)
        response = templates.TemplateResponse(request, "index.html")

        # Add cache control headers if caching is disabled
        if config.network.disable_cache:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        # Redirect to main page since status is now integrated
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)

    @app.get("/status/meta")
    async def status_meta(request: Request):  # pragma: no cover - simple diagnostics
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
                agent_servers = [s for s in _app_registry.list() if s == "agent"]
                if agent_servers:
                    agent = _app_registry.get("agent")

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

                    estimated_tokens = agent.context_manager.estimate_token_count([msg]) if agent.context_manager else None
                    messages.append({
                        "role": getattr(msg, 'role', 'unknown'),
                        "content": getattr(msg, 'content', ''),
                        "estimated_tokens": estimated_tokens,
                        "has_tool_calls": bool(getattr(msg, 'tool_calls', None))
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
                    # reset session_start and last_activity to now
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
                # For plugins, check if they're in the active registry and functioning
                server_name = server_info.get("id", "")
                if _app_registry and hasattr(_app_registry, "_servers"):
                    server_obj = _app_registry._servers.get(server_name)
                    if server_obj:
                        try:
                            # Test if we can call a basic method
                            server_obj.get_default_action()
                            return True
                        except Exception as e:
                            logger.debug(f"Local server {server_name} basic method test failed: {e}")
                            return False
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

            # Use the registry approach for plugins
            if not _app_registry:
                return {"error": "Registry not initialized"}

            servers = []

            # Add plugin servers from registry
            for server_id, server_obj in _app_registry._servers.items():
                try:
                    # Skip servers that explicitly mark themselves as internal/private
                    if getattr(server_obj, '_mcp_public', True) is False:
                        continue
                    # Get tools using both get_tools() and get_schema() methods
                    tools = []
                    detailed_tools = []

                    # Try get_tools() first (for multi-tool plugins like IBKR)
                    if hasattr(server_obj, 'get_tools'):
                        try:
                            tool_definitions = server_obj.get_tools()
                            if tool_definitions:
                                for tool_def in tool_definitions:
                                    # Handle both direct tool definition and function-wrapped definition
                                    if 'function' in tool_def:
                                        func_def = tool_def['function']
                                        tool_name = func_def.get('name', f'{server_id}_tool')
                                        description = func_def.get('description', f'Tool for {server_id}')
                                        parameters = func_def.get('parameters', {})
                                    else:
                                        tool_name = tool_def.get('name', f'{server_id}_tool')
                                        description = tool_def.get('description', f'Tool for {server_id}')
                                        parameters = tool_def.get('input_schema', {})

                                    tools.append(tool_name)
                                    detailed_tools.append({
                                        'name': tool_name,
                                        'description': description,
                                        'parameters': parameters
                                    })
                        except Exception as e:
                            # If get_tools() fails, fall back to get_schema()
                            logger.debug(f"get_tools() failed for server {server_id}, falling back to get_schema(): {e}")

                    # Fall back to get_schema() if get_tools() didn't work or doesn't exist
                    if not tools and hasattr(server_obj, 'get_schema'):
                        try:
                            schema = server_obj.get_schema()
                            if schema:
                                # Each plugin typically provides one function/tool
                                if 'function' in schema and 'name' in schema['function']:
                                    tool_name = schema['function']['name']
                                    tools = [tool_name]
                                    # Get description from schema if available
                                    description = schema['function'].get('description', f'Tool for {server_id}')
                                    detailed_tools = [{
                                        'name': tool_name,
                                        'description': description,
                                        'parameters': schema['function'].get('parameters', {})
                                    }]
                        except Exception as e:
                            # If schema loading fails, treat as no tools
                            logger.debug(f"get_schema() failed for server {server_id}: {e}")

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

                    # Get originally configured external servers (including failed connections)
                    configured_servers = getattr(_mcp_integration, 'configured_external_servers', {})

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
                        # Filter out blocked tools for tool_names, but keep blocked info for detailed_tools
                        tool_names = [tool["name"] for tool in tools if not tool.get("blocked", False)]
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
                            # Server is in client list and has tools - likely connected
                            connected = True
                        else:
                            # Either not reachable or no tools available - mark disconnected
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
                pass

            # Separate plugins and external servers for expected response format
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
    cfg_path = str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)

    # Build the application
    app_obj = build_app(cfg_path)

    # Get server configuration
    host = os.getenv("HOST") or config.network.host or "127.0.0.1"
    port_env = os.getenv("PORT")
    port = int(port_env) if port_env else int(getattr(config.network, "port", 8000))

    # Configure log level. Allow AGENT_LOG_LEVEL to override for the running
    # uvicorn process as well so console logging can be forced without editing
    # `agent.yaml`.
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
