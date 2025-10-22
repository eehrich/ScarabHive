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

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    from collections import deque

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
        command_history
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

        # Initialize templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))

    def get_web_router(self) -> APIRouter:
        """Return FastAPI router with SSH control endpoints"""
        router = APIRouter(prefix=f"/plugins/{self.name}")

        @router.get("/panel.html", response_class=HTMLResponse)
        async def panel_html(request: Request):
            """Serve the SSH control panel HTML"""
            return self.templates.TemplateResponse(
                request,
                "panel.html",
                {
                    "plugin_name": self.name,
                    "timestamp": int(time.time())
                }
            )

        @router.get("/panel", response_class=HTMLResponse)
        async def panel(request: Request):
            """Serve the SSH control panel HTML (alternate endpoint)"""
            return self.templates.TemplateResponse(
                request,
                "panel.html",
                {
                    "plugin_name": self.name,
                    "timestamp": int(time.time())
                }
            )

        @router.get("/api/machines")
        async def list_machines():
            """List all configured SSH machines with connection status"""
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )

            try:
                machines = []
                for machine_name, machine_config in self.connection_manager.machines.items():
                    # Get connection pool info if available
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
            except Exception as e:
                logger.error(f"Failed to list machines: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"error": str(e)}
                )

        @router.get("/api/machines/{machine_name}/status")
        async def get_machine_status(machine_name: str, active: bool = False):
            """Get detailed status for a specific machine
            
            Args:
                machine_name: Name of the machine
                active: If True, actively test connection and measure latency (default: False for lazy check)
            """
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )

            try:
                # Check if machine exists
                if machine_name not in self.connection_manager.machines:
                    raise HTTPException(status_code=404, detail=f"Machine '{machine_name}' not found")

                # Check connection (lazy mode by default, active if requested)
                result = await self.connection_manager.check_connection(machine_name, lazy=not active)

                # Get pool info
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
            except Exception as e:
                logger.error(f"Failed to get machine status: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"error": str(e)}
                )

        @router.post("/api/machines/add")
        async def add_machine(request: AddMachineRequest):
            """Dynamically add a new SSH machine (calls ssh_control_add_machine tool)"""
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )

            try:
                # Import MCP server's add_machine tool
                # Note: We need to get the MCP server instance to call its tool
                # For now, we'll directly replicate the logic here
                # TODO: Refactor to share code between MCP tool and Web API

                from .models import MachineConfig
                from .auth import SSHAuthenticator
                import asyncssh

                # Validate required parameters
                if not request.name or not request.host or not request.username:
                    return JSONResponse(
                        status_code=400,
                        content={"success": False, "error": "Missing required parameters: name, host, username"}
                    )

                # Check for duplicate name
                if request.name in self.connection_manager.machines:
                    return JSONResponse(
                        status_code=409,
                        content={"success": False, "error": f"Machine '{request.name}' already exists"}
                    )

                # Create machine config
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

                # Test connection before adding
                logger.info(f"Testing connection to {request.name} ({request.host}:{request.port})...")
                try:
                    conn = await asyncio.wait_for(
                        SSHAuthenticator.create_connection(
                            machine_config,
                            self.connection_manager.known_hosts_file,
                            self.connection_manager.strict_host_key_checking
                        ),
                        timeout=10.0
                    )

                    # Run a simple test command
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

                    # Close test connection
                    conn.close()
                    await conn.wait_closed()

                except asyncio.TimeoutError:
                    logger.error(f"Connection test timeout for {request.name}")
                    return JSONResponse(
                        status_code=408,
                        content={"success": False, "error": "Connection test timed out after 10 seconds"}
                    )
                except asyncssh.Error as e:
                    logger.error(f"SSH connection test failed for {request.name}: {e}")
                    return JSONResponse(
                        status_code=400,
                        content={"success": False, "error": f"SSH connection failed: {str(e)}"}
                    )

                # Add to runtime connection manager
                self.connection_manager.machines[request.name] = machine_config
                logger.info(f"Added machine '{request.name}' to runtime connection manager")

                # Optionally persist to config file
                if request.persistent:
                    try:
                        import yaml
                        config_path = Path('config/mcp.yaml')

                        if not config_path.exists():
                            logger.warning(f"Config file {config_path} does not exist, cannot persist")
                            return {
                                "success": True,
                                "message": f"Machine '{request.name}' added to runtime (config file not found for persistence)",
                                "machine": request.name,
                                "persisted": False
                            }

                        # Load existing config
                        with open(config_path, 'r', encoding='utf-8') as f:
                            config = yaml.safe_load(f) or {}

                        # Ensure mcp_system.servers.ssh_control structure exists
                        if 'plugins' not in config or not isinstance(config['plugins'], dict):
                            config['plugins'] = {}
                        
                        mcp_sys = config['plugins']
                        if 'servers' not in mcp_sys or not isinstance(mcp_sys['servers'], dict):
                            mcp_sys['servers'] = {}
                        
                        if 'ssh_control' not in mcp_sys['servers'] or not isinstance(mcp_sys['servers']['ssh_control'], dict):
                            mcp_sys['servers']['ssh_control'] = {}
                        
                        target = mcp_sys['servers']['ssh_control']

                        if 'machines' not in target or not isinstance(target['machines'], list):
                            target['machines'] = []

                        # Create machine dict for config (without password for security)
                        machine_dict = {
                            'name': request.name,
                            'host': request.host,
                            'port': request.port,
                            'username': request.username,
                            'auth_method': request.auth_method,
                            'tags': request.tags,
                            'max_connections': request.max_connections,
                            'timeout': 30
                        }

                        # Add key_path only if using key auth
                        if request.auth_method == 'key':
                            machine_dict['key_path'] = request.key_path

                        # NOTE: Password is intentionally NOT saved to config for security

                        # Append to machines list
                        target['machines'].append(machine_dict)

                        # Write back to file
                        with open(config_path, 'w', encoding='utf-8') as f:
                            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

                        logger.info(f"Persisted machine '{request.name}' to {config_path}")

                        return {
                            "success": True,
                            "message": f"Machine '{request.name}' added successfully and persisted to config",
                            "machine": request.name,
                            "persisted": True
                        }

                    except Exception as e:
                        logger.error(f"Failed to persist machine to config: {e}", exc_info=True)
                        return {
                            "success": True,
                            "message": f"Machine '{request.name}' added to runtime, but failed to persist: {str(e)}",
                            "machine": request.name,
                            "persisted": False,
                            "persistence_error": str(e)
                        }

                return {
                    "success": True,
                    "message": f"Machine '{request.name}' added successfully to runtime",
                    "machine": request.name,
                    "persisted": False
                }

            except Exception as e:
                logger.error(f"Failed to add machine: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": str(e)}
                )

        @router.delete("/api/machines/{name}")
        async def remove_machine(name: str, remove_from_config: bool = False):
            """Remove an SSH machine (calls ssh_control_remove_machine tool)"""
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )

            try:
                # Check if machine exists
                if name not in self.connection_manager.machines:
                    raise HTTPException(status_code=404, detail=f"Machine '{name}' not found")

                # Close connection pool if exists
                if name in self.connection_manager.pools:
                    logger.info(f"Closing connection pool for machine '{name}'...")
                    await self.connection_manager.pools[name].close_all()
                    del self.connection_manager.pools[name]
                    logger.info(f"Connection pool for '{name}' closed")

                # Remove from runtime connection manager
                del self.connection_manager.machines[name]
                logger.info(f"Removed machine '{name}' from runtime connection manager")

                # Optionally remove from config file
                config_removed = False
                if remove_from_config:
                    try:
                        import yaml
                        config_path = Path('config/mcp.yaml')

                        if not config_path.exists():
                            logger.warning(f"Config file {config_path} does not exist")
                        else:
                            # Load existing config
                            with open(config_path, 'r', encoding='utf-8') as f:
                                config = yaml.safe_load(f) or {}

                            # Remove machine from config
                            if ('servers' in config and
                                'ssh_control' in config['servers'] and
                                'machines' in config['servers']['ssh_control']):

                                original_count = len(config['servers']['ssh_control']['machines'])
                                config['servers']['ssh_control']['machines'] = [
                                    m for m in config['servers']['ssh_control']['machines']
                                    if m.get('name') != name
                                ]
                                new_count = len(config['servers']['ssh_control']['machines'])

                                if original_count > new_count:
                                    # Write back to file
                                    with open(config_path, 'w', encoding='utf-8') as f:
                                        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

                                    logger.info(f"Removed machine '{name}' from {config_path}")
                                    config_removed = True
                                else:
                                    logger.warning(f"Machine '{name}' not found in config file")

                    except Exception as e:
                        logger.error(f"Failed to remove machine from config: {e}", exc_info=True)
                        return {
                            "success": True,
                            "message": f"Machine '{name}' removed from runtime, but failed to remove from config: {str(e)}",
                            "machine": name,
                            "config_removed": False,
                            "config_error": str(e)
                        }

                return {
                    "success": True,
                    "message": f"Machine '{name}' removed successfully",
                    "machine": name,
                    "config_removed": config_removed
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to remove machine: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": str(e)}
                )

        @router.get("/api/history")
        async def get_command_history(limit: int = 50):
            """Get recent command execution history"""
            if not self.command_history:
                return {"history": [], "count": 0}

            try:
                # Get last N items from deque
                history_list = list(self.command_history)
                history_list.reverse()  # Most recent first

                # Apply limit
                if limit > 0:
                    history_list = history_list[:limit]

                # Format timestamps
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
            except Exception as e:
                logger.error(f"Failed to get command history: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"error": str(e)}
                )

        @router.get("/api/machines/{machine_name}/history")
        async def get_machine_history(machine_name: str, limit: int = 100):
            """Get command execution history for a specific machine"""
            if not self.command_history:
                return {"history": [], "count": 0}

            try:
                # Filter history for this machine
                machine_history = [
                    entry for entry in self.command_history
                    if entry.get('machine') == machine_name
                ]

                # Most recent first
                machine_history.reverse()

                # Apply limit
                if limit > 0:
                    machine_history = machine_history[:limit]

                # Format timestamps and add output previews
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
                    'total_count': sum(1 for e in self.command_history if e.get('machine') == machine_name)
                }
            except Exception as e:
                logger.error(f"Failed to get machine history: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"error": str(e)}
                )

        @router.post("/api/execute")
        async def execute_command(request: ExecuteCommandRequest):
            """Execute command on remote machine"""
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )

            try:
                # Execute command
                result = await self.connection_manager.execute_command(
                    request.machine,
                    request.command,
                    timeout=request.timeout
                )

                # History is already logged by connection_manager with full output
                # No need to log again here to avoid duplicates

                return {
                    'machine': result.machine,
                    'command': result.command,
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'exit_code': result.exit_code,
                    'duration': result.duration,
                    'success': result.exit_code == 0
                }
            except FileNotFoundError as e:
                # SSH key or config file not found
                error_msg = str(e)
                logger.warning(f"SSH authentication failed for {request.machine}: {error_msg}")
                return JSONResponse(
                    status_code=401,
                    content={"error": f"Authentication failed: {error_msg}"}
                )
            except ConnectionRefusedError as e:
                logger.warning(f"Connection refused for {request.machine}: {e}")
                return JSONResponse(
                    status_code=503,
                    content={"error": f"Connection refused: {e}"}
                )
            except TimeoutError as e:
                logger.warning(f"Connection timeout for {request.machine}: {e}")
                return JSONResponse(
                    status_code=504,
                    content={"error": f"Connection timeout: {e}"}
                )
            except Exception as e:
                logger.error(f"Failed to execute command on {request.machine}: {e}", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"error": str(e)}
                )

        @router.get("/api/execute/stream")
        async def execute_command_stream(machine: str, command: str, timeout: Optional[int] = None):
            """Execute command with SSE streaming output"""
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )

            async def event_generator():
                """Generate SSE events from command output"""
                try:
                    async for event in self.connection_manager.execute_command_stream(
                        machine,
                        command,
                        timeout=timeout
                    ):
                        # Format as SSE event
                        event_type = event.get('type', 'message')
                        data = event.get('data', '')

                        # Serialize data as JSON
                        import json
                        data_json = json.dumps(data)

                        # SSE format: event: type\ndata: json\n\n
                        yield f"event: {event_type}\ndata: {data_json}\n\n"

                    # Send done event
                    yield "event: done\ndata: {}\n\n"

                except Exception as e:
                    logger.error(f"SSE streaming error: {e}", exc_info=True)
                    error_data = json.dumps({'error': str(e)})
                    yield f"event: error\ndata: {error_data}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"  # Disable nginx buffering
                }
            )

        return router

    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets"""
        static_path = Path(__file__).parent / "static"
        if static_path.exists():
            return static_path
        return None

    def get_panels(self) -> List[Dict[str, Any]]:
        """Return UI panel configuration"""
        return [
            {
                "id": f"{self.name}_panel",
                "title": "SSH Control",
                "icon": "🖥️",
                "url": f"/plugins/{self.name}/panel.html",
                "position": "right",
                "width": "600px",
                "height": "500px"
            }
        ]

    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration"""
        return {
            "require_auth": False,  # TODO: Add authentication
            "cors_origins": [],
            "rate_limit": None
        }
