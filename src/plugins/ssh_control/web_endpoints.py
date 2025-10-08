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
import json
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


class SSHControlWebEndpoints(PluginWebInterface):
    """Web endpoints component for SSH control plugin"""
    
    def __init__(
        self, 
        name: str, 
        system_config: "AgentSystemConfig", 
        mcp_config: "MCPConfig",
        connection_manager=None,
        command_history: Optional["deque"] = None
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
        
        logger.info(f"SSHControlWebEndpoints initialized: {name}")
    
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
        async def get_machine_status(machine_name: str):
            """Get detailed status for a specific machine"""
            if not self.connection_manager:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Connection manager not available"}
                )
            
            try:
                # Check if machine exists
                if machine_name not in self.connection_manager.machines:
                    raise HTTPException(status_code=404, detail=f"Machine '{machine_name}' not found")
                
                # Check connection
                result = await self.connection_manager.check_connection(machine_name)
                
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
                
                # Log to history
                self.command_history.append({
                    'timestamp': time.time(),
                    'machine': request.machine,
                    'command': request.command,
                    'exit_code': result.exit_code,
                    'duration': result.duration,
                    'stdout_preview': result.stdout[:200] if result.stdout else '',
                    'stderr_preview': result.stderr[:200] if result.stderr else '',
                    'success': result.exit_code == 0
                })
                
                return {
                    'machine': result.machine,
                    'command': result.command,
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'exit_code': result.exit_code,
                    'duration': result.duration,
                    'success': result.exit_code == 0
                }
            except Exception as e:
                logger.error(f"Failed to execute command: {e}", exc_info=True)
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
                        
                        # Add small delay to prevent overwhelming client
                        await asyncio.sleep(0.01)
                    
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
