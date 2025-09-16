from __future__ import annotations

import json
import asyncio
import logging
import os
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
from ..mcp.base import MCPRegistry
from ..servers.bootstrap import bootstrap_servers
from ..utils.logging import setup_logging
from ..mcp.status import status_bus, StatusEvent, get_status_metrics
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


app = FastAPI(title="Agent System (MCP)", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parents[3] / "templates"))

# Mount static directory for CSS/JS if it exists - will be configured with cache control in build_app()
static_path = Path(__file__).parents[3] / "static"

# Global flag to track if middleware has been added
_middleware_added = False


def build_app(config_path: Optional[str] = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    global _middleware_added

    cfg_path = config_path or str(Path(__file__).parents[3] / "config" / "agent.yaml")
    config = load_config(cfg_path)

    # Configure static files with cache control based on configuration
    if static_path.exists():
            static_files = StaticFiles(directory=str(static_path))
            app.mount("/static", static_files, name="static")

            # middleware to add no-cache headers for static files when configured
            # Only add middleware once to avoid FastAPI runtime errors
            if config.network.disable_cache and not _middleware_added:
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

                _middleware_added = True

    # Initialize logging. Use a role-specific logfile so the API server does
    # not write into the same file as the CLI (e.g., create `logs/agent-api.log`).
    def _role_logfile(base: str, role: str) -> str:
        try:
            p = Path(base)
            stem = p.stem or "agent"
            suffix = "".join(p.suffixes) or ".log"
            return str(p.with_name(f"{stem}-{role}{suffix}"))
        except Exception:
            return str(Path("logs") / f"agent-{role}.log")

    # Determine logfile for API: prefer explicit per-role setting if provided.
    log_path = config.logging.file_api or _role_logfile(config.logging.file or "logs/agent.log", "api")
    log_file = setup_logging(config.logging.enabled, config.logging.level, log_path)
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
    
    # Store registry and config globally for MCP endpoint access
    global _app_registry, _app_config
    _app_registry = registry
    _app_config = config

    # Initialize MCP integration on startup so the API can call external MCP
    # servers and expose MCP-related endpoints. Use FastAPI startup/shutdown
    # events to ensure proper async initialization and cleanup.
    async def _init_mcp():
        global _mcp_integration
        logger = logging.getLogger(__name__)
        logger.info("Starting MCP integration initialization...")
        try:
            # Use only the MCP configuration from the loaded agent.yaml config.
            # Do NOT read separate mcp.yaml files; all configuration should be
            # included via agent.yaml.
            try:
                mcp_block = config.mcp.model_dump() if hasattr(config.mcp, "model_dump") else getattr(config.mcp, "__dict__", {})
                logger.info(f"MCP config loaded: external_servers={len(mcp_block.get('external_servers', {}))}")
            except Exception:
                mcp_block = getattr(config.mcp, "__dict__", {})
                logger.warning("Failed to get MCP config with model_dump, using __dict__")

            mcp_integration = await initialize_mcp({"mcp": mcp_block}, app)
            _mcp_integration = mcp_integration  # Store the initialized instance globally
            logger.info("MCP integration initialized for API")
        except Exception as e:
            logger.exception("Failed to initialize MCP integration for API: %s", e)

    async def _shutdown_mcp_event():
        logger = logging.getLogger(__name__)
        try:
            await shutdown_mcp()
            logger.info("MCP integration shut down for API")
        except Exception as e:
            logger.exception("Error shutting down MCP integration for API: %s", e)

    # Use modern lifespan pattern instead of deprecated on_event
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger = logging.getLogger(__name__)
        logger.info("Lifespan startup: Initializing MCP integration...")
        await _init_mcp()
        logger.info("MCP integration initialized during lifespan startup")
        yield
        # Shutdown
        try:
            await shutdown_mcp()
            logger.info("MCP integration shut down during lifespan")
        except Exception as e:
            logger.exception("Error shutting down MCP integration during lifespan: %s", e)

    # Apply lifespan to existing app
    app.router.lifespan_context = lifespan

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
    async def events(task: str):
        logger = logging.getLogger(__name__)
        logger.info("SSE /events connected, task=%s", task)

        async def event_stream():
            # Initial keep-alive line
            yield ":ok\n\n"
            async for ev in agent.run_events(task):
                logger.debug("SSE event: %s", ev.get("type"))
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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

    def _check_server_connection(server_info):
        """Check if a server is actually responding with real-time connectivity test"""
        import socket
        from urllib.parse import urlparse
        
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
                        except Exception:
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
                pass
            return False
        
        return False

    @app.get("/mcp/status")
    async def mcp_status():
        """Get MCP server status including plugins and external servers."""
        try:
            # Use the global MCP integration instance that was initialized during startup
            global _mcp_integration
            
            # Use the registry approach for plugins
            if not _app_registry:
                return {"error": "Registry not initialized"}
            
            servers = []
            
            # Add plugin servers from registry
            for server_id, server_obj in _app_registry._servers.items():
                try:
                    # Get tools using schema method
                    tools = []
                    detailed_tools = []
                    if hasattr(server_obj, 'get_schema'):
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
                        except Exception:
                            # If schema loading fails, treat as no tools
                            pass
                    
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
                        except Exception:
                            pass
                        
                        # Get tools (may be empty if server is down)
                        tools = servers_with_tools.get(server_name, [])
                        tool_names = [tool["name"] for tool in tools]
                        detailed_tools = [{
                            'name': tool.get("name", "unknown"),
                            'description': tool.get("description", f"Tool from {description}"),
                            'parameters': tool.get("parameters", {})
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
            
            return {
                "servers": servers,
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

    # Configure log level
    uvicorn_log_level = config.logging.level.lower() if config.logging.enabled else "info"

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
