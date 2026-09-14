"""SSH Control Web Endpoints

Provides web UI endpoints for SSH machine monitoring and control.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import asyncio

from agent_system.plugins.web_adapter import PluginWebInterface

from . import machine_store

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ExecuteCommandRequest(BaseModel):
    """Request model for command execution"""
    machine: str
    command: str
    timeout: Optional[int] = None


class AddMachineRequest(BaseModel):
    """Request model for adding a new SSH machine"""
    name: str
    host: str
    port: int = 22
    username: str
    auth_method: str = "key"  # "key", "password", or "agent"
    password: Optional[str] = None
    key_path: str = "~/.ssh/id_rsa"
    tags: List[str] = []
    persistent: bool = False
    max_connections: int = 3


class SSHControlWebEndpoints(PluginWebInterface):
    """Web endpoints component for SSH control plugin"""

    def __init__(
        self,
        name: str,
        system_config,
        mcp_config,
        connection_manager,
        command_history,
        plugin=None
    ):
        """Initialize SSH control web endpoints.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
            connection_manager: SSH connection manager instance (shared with MCP server)
            command_history: Command history deque (shared with MCP server)
        """
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.connection_manager = connection_manager
        self.command_history = command_history
        self.plugin = plugin
        self._schema_cache: Optional[Dict[str, Any]] = None

        # Initialize templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))

    def _load_schema(self) -> Dict[str, Any]:
        """Load schema data either from plugin or directly from disk."""
        if self._schema_cache is not None:
            return self._schema_cache
        schema: Dict[str, Any] = {}
        if self.plugin and hasattr(self.plugin, "get_schema_data"):
            try:
                schema = self.plugin.get_schema_data() or {}
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning("Failed to load schema from plugin %s: %s", self.name, exc)
        if not schema:
            try:
                from agent_system.plugins.schema_loader import load_schema_from_dir

                schema = load_schema_from_dir(
                    Path(__file__).parent,
                    template_vars={"name": self.name}
                ) or {}
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning("Failed to load schema from disk for %s: %s", self.name, exc)
                schema = {}
        self._schema_cache = schema
        return self._schema_cache

    def get_web_router(self) -> APIRouter:
        """Return FastAPI router generated from schema endpoints."""
        from agent_system.plugins.schema_router import create_schema_router

        schema = self._load_schema()
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self
        )

    async def panel(self, request: Request) -> HTMLResponse:
        """Serve the SSH control panel HTML."""
        return self.templates.TemplateResponse(
            request,
            "panel.html",
            {
                "plugin_name": self.name,
                "timestamp": int(time.time())
            }
        )

    async def list_machines(self):
        """List all configured SSH machines with connection status."""
        if not self.connection_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Connection manager not available"}
            )

        try:
            machines = []
            for machine_name, machine_config in self.connection_manager.machines.items():
                pool_info = {}
                if machine_name in self.connection_manager.pools:
                    pool = self.connection_manager.pools[machine_name]
                    pool_info = {
                        'total_created': pool.total_created,
                        'available': pool.available.qsize(),
                        'in_use': len(pool.in_use),
                        'total_commands': pool.total_commands,
                        'last_used': pool.last_used
                    }

                machines.append({
                    'name': machine_name,
                    'host': machine_config.host,
                    'port': machine_config.port,
                    'username': machine_config.username,
                    'tags': machine_config.tags,
                    'max_connections': machine_config.max_connections,
                    'pool': pool_info
                })

            return {
                'machines': machines,
                'count': len(machines)
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to list machines: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": str(exc)}
            )

    async def get_machine_status(
        self,
        machine_name: str,
        active: bool = False
    ):
        """Get detailed status for a specific machine."""
        if not self.connection_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Connection manager not available"}
            )

        try:
            if machine_name not in self.connection_manager.machines:
                raise HTTPException(status_code=404, detail=f"Machine '{machine_name}' not found")

            result = await self.connection_manager.check_connection(machine_name, lazy=not active)

            pool_info = {}
            if machine_name in self.connection_manager.pools:
                pool = self.connection_manager.pools[machine_name]
                pool_info = {
                    'total_created': pool.total_created,
                    'available': pool.available.qsize(),
                    'in_use': len(pool.in_use),
                    'total_commands': pool.total_commands,
                    'last_used': pool.last_used,
                    'last_used_ago': time.time() - pool.last_used
                }

            return {
                'machine': machine_name,
                'connected': result.get('connected', False),
                'latency_ms': result.get('latency_ms'),
                'error': result.get('error'),
                'not_yet_connected': result.get('not_yet_connected', False),
                'pool': pool_info
            }
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to get machine status: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": str(exc)}
            )

    async def add_machine(self, request: AddMachineRequest):
        """Dynamically add a new SSH machine (calls ssh_control_add_machine tool)."""
        if not self.connection_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Connection manager not available"}
            )

        try:
            from .models import MachineConfig
            from .auth import SSHAuthenticator
            import asyncssh

            if not request.name or not request.host or not request.username:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "Missing required parameters: name, host, username"}
                )

            if request.name in self.connection_manager.machines:
                return JSONResponse(
                    status_code=409,
                    content={"success": False, "error": f"Machine '{request.name}' already exists"}
                )

            machine_config = MachineConfig(
                name=request.name,
                host=request.host,
                port=request.port,
                username=request.username,
                auth_method=request.auth_method,
                password=request.password,
                key_path=request.key_path,
                tags=request.tags,
                max_connections=request.max_connections,
                timeout=30
            )

            logger.info("Testing connection to %s (%s:%s)...", request.name, request.host, request.port)
            try:
                conn = await asyncio.wait_for(
                    SSHAuthenticator.create_connection(
                        machine_config,
                        self.connection_manager.known_hosts_file,
                        self.connection_manager.strict_host_key_checking
                    ),
                    timeout=10.0
                )

                result = await asyncio.wait_for(
                    conn.run('echo "Connection test"', check=False),
                    timeout=5.0
                )

                if result.exit_status != 0:
                    conn.close()
                    await conn.wait_closed()
                    return JSONResponse(
                        status_code=400,
                        content={
                            "success": False,
                            "error": f"Connection test failed with exit code {result.exit_status}"
                        }
                    )

                conn.close()
                await conn.wait_closed()

            except asyncio.TimeoutError:
                logger.error("Connection test timeout for %s", request.name)
                return JSONResponse(
                    status_code=408,
                    content={"success": False, "error": "Connection test timed out after 10 seconds"}
                )
            except asyncssh.Error as exc:  # pragma: no cover - depends on asyncssh
                logger.error("SSH connection test failed for %s: %s", request.name, exc)
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": f"SSH connection failed: {str(exc)}"}
                )

            self.connection_manager.machines[request.name] = machine_config
            logger.info("Added machine '%s' to runtime connection manager", request.name)

            if request.persistent:
                # One store, one shape -- see machine_store. This endpoint
                # used to carry its own copy of the read-modify-write, which
                # is how it and the remove endpoint came to disagree.
                stored = {
                    'name': request.name,
                    'host': request.host,
                    'port': request.port,
                    'username': request.username,
                    'auth_method': request.auth_method,
                    'max_connections': request.max_connections,
                }
                if request.auth_method == 'key':
                    stored['key_path'] = request.key_path
                if request.tags:
                    stored['tags'] = request.tags

                # Same refusal the tool applies: a machine that cannot be
                # restored is not stored -- see machine_store.
                refusal = machine_store.unrestorable_reason(stored)
                if refusal:
                    logger.info("Not storing '%s': %s", request.name, refusal)
                    return {
                        "success": True,
                        "message": f"Machine '{request.name}' added to runtime, but not stored: {refusal}",
                        "machine": request.name,
                        "persisted": False,
                        "persistence_error": refusal,
                    }

                try:
                    path = machine_store.add(self.name, stored)
                    logger.info("Stored machine '%s' in %s", request.name, path)
                    return {
                        "success": True,
                        "message": f"Machine '{request.name}' added and stored in {path}",
                        "machine": request.name,
                        "persisted": True,
                    }
                except Exception as exc:  # pragma: no cover - file IO errors
                    logger.error("Failed to store machine '%s': %s",
                                 request.name, exc, exc_info=True)
                    return {
                        "success": True,
                        "message": f"Machine '{request.name}' added to runtime, but not stored: {exc}",
                        "machine": request.name,
                        "persisted": False,
                        "persistence_error": str(exc),
                    }

            return {
                "success": True,
                "message": f"Machine '{request.name}' added successfully to runtime",
                "machine": request.name,
                "persisted": False
            }

        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to add machine: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": str(exc)}
            )

    async def remove_machine(
        self,
        name: str,
        remove_from_config: bool = False
    ):
        """Remove an SSH machine."""
        if not self.connection_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Connection manager not available"}
            )

        try:
            if name not in self.connection_manager.machines:
                raise HTTPException(status_code=404, detail=f"Machine '{name}' not found")

            if name in self.connection_manager.pools:
                logger.info("Closing connection pool for machine '%s'...", name)
                await self.connection_manager.pools[name].close_all()
                del self.connection_manager.pools[name]
                logger.info("Connection pool for '%s' closed", name)

            del self.connection_manager.machines[name]
            logger.info("Removed machine '%s' from runtime connection manager", name)

            config_removed = False
            if remove_from_config:
                try:
                    # Through the same store the ADD endpoint writes.
                    config_removed = machine_store.remove(self.name, name)
                    if config_removed:
                        logger.info("Removed machine '%s' from %s", name,
                                    machine_store.store_path(self.name))
                    else:
                        logger.info("Machine '%s' was not in %s (configured, not stored)",
                                    name, machine_store.store_path(self.name))
                except Exception as exc:  # pragma: no cover - file IO errors
                    logger.error("Failed to remove machine '%s' from the store: %s",
                                 name, exc, exc_info=True)
                    return {
                        "success": True,
                        "message": f"Machine '{name}' removed from runtime, but not from the store: {exc}",
                        "machine": name,
                        "config_removed": False,
                        "config_error": str(exc),
                    }

            return {
                "success": True,
                "message": f"Machine '{name}' removed successfully",
                "machine": name,
                "config_removed": config_removed
            }

        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to remove machine: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": str(exc)}
            )

    async def get_command_history(self, limit: int = 50):
        """Get recent command execution history."""
        if not self.command_history:
            return {"history": [], "count": 0}

        try:
            history_list = list(self.command_history)
            history_list.reverse()

            if limit > 0:
                history_list = history_list[:limit]

            for entry in history_list:
                entry['timestamp_formatted'] = time.strftime(
                    '%Y-%m-%d %H:%M:%S',
                    time.localtime(entry['timestamp'])
                )
                entry['duration_formatted'] = f"{entry['duration']:.2f}s"

            return {
                'history': history_list,
                'count': len(history_list),
                'total_count': len(self.command_history)
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to get command history: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": str(exc)}
            )

    async def get_machine_history(
        self,
        machine_name: str,
        limit: int = 100
    ):
        """Get command execution history for a specific machine."""
        if not self.command_history:
            return {"history": [], "count": 0}

        try:
            machine_history = [
                entry for entry in self.command_history
                if entry.get('machine') == machine_name
            ]

            machine_history.reverse()

            if limit > 0:
                machine_history = machine_history[:limit]

            for entry in machine_history:
                entry['timestamp_formatted'] = time.strftime(
                    '%Y-%m-%d %H:%M:%S',
                    time.localtime(entry.get('timestamp', time.time()))
                )
                entry['duration_formatted'] = f"{entry.get('duration', 0):.2f}s"

            return {
                'machine': machine_name,
                'history': machine_history,
                'count': len(machine_history),
                'total_count': sum(1 for entry in self.command_history if entry.get('machine') == machine_name)
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to get machine history: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": str(exc)}
            )

    async def execute_command(self, request: ExecuteCommandRequest):
        """Execute command on remote machine."""
        if not self.connection_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Connection manager not available"}
            )

        try:
            result = await self.connection_manager.execute_command(
                request.machine,
                request.command,
                timeout=request.timeout
            )

            return {
                'machine': result.machine,
                'command': result.command,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'exit_code': result.exit_code,
                'duration': result.duration,
                'success': result.exit_code == 0
            }
        except FileNotFoundError as exc:
            error_msg = str(exc)
            logger.warning("SSH authentication failed for %s: %s", request.machine, error_msg)
            return JSONResponse(
                status_code=401,
                content={"error": f"Authentication failed: {error_msg}"}
            )
        except ConnectionRefusedError as exc:
            logger.warning("Connection refused for %s: %s", request.machine, exc)
            return JSONResponse(
                status_code=503,
                content={"error": f"Connection refused: {exc}"}
            )
        except TimeoutError as exc:
            logger.warning("Connection timeout for %s: %s", request.machine, exc)
            return JSONResponse(
                status_code=504,
                content={"error": f"Connection timeout: {exc}"}
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Failed to execute command on %s: %s", request.machine, exc, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": str(exc)}
            )

    async def execute_command_stream(
        self,
        machine: str,
        command: str,
        timeout: Optional[int] = None
    ):
        """Execute command with SSE streaming output."""
        if not self.connection_manager:
            return JSONResponse(
                status_code=503,
                content={"error": "Connection manager not available"}
            )

        async def event_generator():
            """Generate SSE events from command output."""
            try:
                async for event in self.connection_manager.execute_command_stream(
                    machine,
                    command,
                    timeout=timeout
                ):
                    event_type = event.get('type', 'message')
                    data = event.get('data', '')

                    import json  # Local import to avoid eager dependency
                    data_json = json.dumps(data)

                    yield f"event: {event_type}\ndata: {data_json}\n\n"

                yield "event: done\ndata: {}\n\n"

            except Exception as exc:  # pragma: no cover - streaming errors
                logger.error("SSE streaming error: %s", exc, exc_info=True)
                import json

                error_data = json.dumps({'error': str(exc)})
                yield f"event: error\ndata: {error_data}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets"""
        static_path = Path(__file__).parent / "static"
        if static_path.exists():
            return static_path
        return None

    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration"""
        return {
            "require_auth": False,  # TODO: Add authentication
            "cors_origins": [],
            "rate_limit": None
        }
