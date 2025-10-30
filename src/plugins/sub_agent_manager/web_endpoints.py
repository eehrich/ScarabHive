"""Web UI endpoints for Sub-Agent Manager plugin."""

import logging
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


def get_session_service():
    """Get global session_service from app.py."""
    from agent_system.app import _session_service
    if not _session_service:
        raise RuntimeError("SessionService not initialized - app not started?")
    return _session_service


def get_registry():
    """Get global MCP registry from app.py."""
    from agent_system.app import _app_registry
    if not _app_registry:
        raise RuntimeError("MCPRegistry not initialized - app not started?")
    return _app_registry


class SubAgentManagerWebFactory:
    """Web UI factory for Sub-Agent Manager."""
    
    def __init__(self, server):
        """
        Initialize web factory.
        
        Args:
            server: SubAgentManagerServer instance
        """
        self.server = server
        self.plugin_dir = Path(__file__).parent
        self.templates_dir = self.plugin_dir / "templates"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))
    
    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        router = APIRouter(prefix=f"/plugins/{self.server.name}")
        
        @router.get("/panel", response_class=HTMLResponse)
        async def get_panel(request: Request):
            """Render the Sub-Agent Manager dashboard."""
            return self.render_panel(request)
        
        @router.get("/sub-agents")
        async def get_sub_agents(
            session_id: str = Query(..., description="Parent session ID"),
            include_completed: bool = Query(False, description="Include archived sub-agents")
        ):
            """
            Get all sub-agents for a session (JSON).
            
            Args:
                session_id: Parent session ID (required)
                include_completed: Include archived sub-agents (default: false)
            """
            try:
                # Get session_service from app
                session_service = get_session_service()
                
                # Build params as server expects (with injected session_service)
                params = {
                    "_session_id": session_id,
                    "_session_service": session_service,
                    "include_completed": include_completed
                }
                
                result = await self.server._handle_list(params)
                
                if result.get("status") == "error":
                    raise HTTPException(status_code=500, detail=result.get("error"))
                
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error fetching sub-agents: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/sub-agents/{instance_id}")
        async def get_sub_agent_info(
            instance_id: str,
            session_id: str = Query(..., description="Parent session ID")
        ):
            """
            Get detailed info about specific sub-agent (JSON).
            
            Args:
                instance_id: Sub-agent instance ID
                session_id: Parent session ID
            """
            try:
                # Get session_service from app
                session_service = get_session_service()
                
                params = {
                    "_session_id": session_id,
                    "_session_service": session_service,
                    "instance_id": instance_id
                }
                
                result = await self.server._handle_info(params)
                
                if result.get("status") == "error":
                    status_code = 404 if "not found" in result.get("error", "").lower() else 500
                    raise HTTPException(status_code=status_code, detail=result.get("error"))
                
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error fetching sub-agent {instance_id}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/sub-agents/{instance_id}/continue")
        async def continue_sub_agent(
            instance_id: str,
            request: Request,
            session_id: str = Query(..., description="Parent session ID")
        ):
            """
            Continue existing sub-agent with new message (JSON).
            
            Args:
                instance_id: Sub-agent instance ID
                session_id: Parent session ID
                
            Request body:
                {
                    "message": "Follow-up question or task"
                }
            """
            try:
                # Get session_service and registry from app
                session_service = get_session_service()
                registry = get_registry()
                
                body = await request.json()
                
                params = {
                    "_session_id": session_id,
                    "_session_service": session_service,
                    "_registry": registry,
                    "instance_id": instance_id,
                    "message": body.get("message")
                }
                
                if not params["message"]:
                    raise HTTPException(status_code=400, detail="Missing 'message'")
                
                result = await self.server._handle_continue(params)
                
                if result.get("status") == "error":
                    raise HTTPException(status_code=400, detail=result.get("error"))
                
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error continuing sub-agent {instance_id}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.delete("/sub-agents/{instance_id}")
        async def delete_sub_agent(
            instance_id: str,
            session_id: str = Query(..., description="Parent session ID")
        ):
            """
            Archive sub-agent (JSON).
            
            Args:
                instance_id: Sub-agent instance ID
                session_id: Parent session ID
            """
            try:
                # Get session_service from app
                session_service = get_session_service()
                
                params = {
                    "_session_id": session_id,
                    "_session_service": session_service,
                    "instance_id": instance_id
                }
                
                result = await self.server._handle_delete(params)
                
                if result.get("status") == "error":
                    status_code = 404 if "not found" in result.get("error", "").lower() else 500
                    raise HTTPException(status_code=status_code, detail=result.get("error"))
                
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error deleting sub-agent {instance_id}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/stats")
        async def get_stats(
            session_id: str = Query(..., description="Parent session ID")
        ):
            """
            Get statistics about sub-agents (JSON).
            
            Args:
                session_id: Parent session ID
            """
            try:
                # Get session_service from app
                session_service = get_session_service()
                
                params = {
                    "_session_id": session_id,
                    "_session_service": session_service,
                    "include_completed": True  # Get all for stats
                }
                
                result = await self.server._handle_list(params)
                
                if result.get("status") == "error":
                    raise HTTPException(status_code=500, detail=result.get("error"))
                
                instances = result.get("instances", [])
                
                # Calculate stats
                stats = {
                    "total": len(instances),
                    "active": len([i for i in instances if i.get("status") == "active"]),
                    "archived": len([i for i in instances if i.get("status") == "archived"]),
                    "by_agent_type": {}
                }
                
                # Count by agent type
                for instance in instances:
                    agent_type = instance.get("agent_type", "unknown")
                    stats["by_agent_type"][agent_type] = stats["by_agent_type"].get(agent_type, 0) + 1
                
                return JSONResponse(stats)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error fetching stats: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
        
        return router
    
    def render_panel(self, request: Request) -> HTMLResponse:
        """
        Render the main dashboard panel.
        
        Args:
            request: FastAPI request object
            
        Returns:
            HTML response with rendered template
        """
        return self.templates.TemplateResponse(
            "panel.html",
            {
                "request": request,
                "plugin_name": self.server.name,
                "plugin_title": "Sub-Agent Manager"
            }
        )
