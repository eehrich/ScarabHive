"""Web UI endpoints for Sub-Agent Manager plugin."""

import logging
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.schema_router import create_schema_router

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
        """Get the FastAPI router for this plugin's web endpoints.

        Routes are automatically generated from schema.yaml endpoint definitions.
        """
        # Get schema from server (already loaded with Jinja2 templates rendered)
        schema = self.server.get_schema_data() if hasattr(self.server, 'get_schema_data') else {}

        # Generate router from schema
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self
        )

    # ========== Handler methods (called by schema router) ==========

    async def get_panel(self, request: Request) -> HTMLResponse:
        """Render the Sub-Agent Manager dashboard (handler for GET /)."""
        return self.render_panel(request)

    async def get_sub_agents_json(
        self,
        request: Request,
        session_id: str = Query(..., description="Parent session ID"),
        include_completed: bool = Query(False, description="Include archived sub-agents")
    ) -> JSONResponse:
        """Get all sub-agents for a session (handler for GET /sub-agents)."""
        try:
            session_service = get_session_service()

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

    async def get_sub_agent_detail(
        self,
        request: Request,
        agent_id: str,
        session_id: str = Query(..., description="Parent session ID")
    ) -> JSONResponse:
        """Get detailed info about specific sub-agent (handler for GET /sub-agents/{agent_id})."""
        try:
            session_service = get_session_service()

            params = {
                "_session_id": session_id,
                "_session_service": session_service,
                "instance_id": agent_id
            }

            result = await self.server._handle_info(params)

            if result.get("status") == "error":
                status_code = 404 if "not found" in result.get("error", "").lower() else 500
                raise HTTPException(status_code=status_code, detail=result.get("error"))

            return JSONResponse(result)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching sub-agent {agent_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    async def delete_sub_agent(
        self,
        request: Request,
        agent_id: str,
        session_id: str = Query(..., description="Parent session ID")
    ) -> JSONResponse:
        """Archive sub-agent (handler for DELETE /sub-agents/{agent_id})."""
        try:
            session_service = get_session_service()

            params = {
                "_session_id": session_id,
                "_session_service": session_service,
                "instance_id": agent_id
            }

            result = await self.server._handle_delete(params)

            if result.get("status") == "error":
                status_code = 404 if "not found" in result.get("error", "").lower() else 500
                raise HTTPException(status_code=status_code, detail=result.get("error"))

            return JSONResponse(result)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting sub-agent {agent_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    async def get_stats(
        self,
        request: Request,
        session_id: str = Query(..., description="Parent session ID")
    ) -> JSONResponse:
        """Get statistics for current session (handler for GET /stats)."""
        try:
            session_service = get_session_service()

            # Get all sub-agents (including archived)
            params_all = {
                "_session_id": session_id,
                "_session_service": session_service,
                "include_completed": True
            }
            result_all = await self.server._handle_list(params_all)

            if result_all.get("status") == "error":
                raise HTTPException(status_code=500, detail=result_all.get("error"))

            instances = result_all.get("instances", [])

            # Calculate statistics
            total = len(instances)
            active = sum(1 for inst in instances if inst.get("status") in ("active", "interrupted"))
            archived = sum(1 for inst in instances if inst.get("status") not in ("active", "interrupted"))

            return JSONResponse({
                "total": total,
                "active": active,
                "archived": archived
            })

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching stats: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    async def get_phase_info(
        self,
        request: Request,
        session_id: str = Query(..., description="Parent session ID")
    ) -> JSONResponse:
        """Get phase filtering info for current session (handler for GET /phase-info).
        
        Returns:
            - enabled: Whether phase filtering is enabled for this manager
            - phase_variable: The template variable name (e.g., "workflow_phase")
            - current_phase: Current phase value from session (or null)
            - phase_agents: Mapping of phase -> allowed agents
            - all_allowed_agents: Full list of allowed agents (ignoring phase)
            - filtered_agents: Agents allowed for current phase (or all if no phase)
        """
        try:
            session_service = get_session_service()
            
            # Get phase filtering config from server
            enabled = self.server.phase_filtering_enabled
            phase_variable = self.server.phase_variable
            phase_agents = self.server.phase_agents
            all_allowed = self.server.allowed_agents
            
            # Try to get current phase from session
            current_phase = None
            filtered_agents = list(all_allowed)  # Default to all
            
            if enabled and session_id and session_service.session_manager:
                try:
                    # First find the session owner (user_id)
                    session_manager = session_service.session_manager
                    user_id = await session_manager._find_session_owner_async(session_id)
                    
                    if user_id:
                        # Load session to get context_vars
                        session_data = await session_manager.load_session(user_id, session_id)
                        if session_data:
                            context_vars = session_data.get("context_vars", {})
                            current_phase = context_vars.get(phase_variable)
                            
                            if current_phase and current_phase in phase_agents:
                                filtered_agents = phase_agents[current_phase]
                            elif "_default" in phase_agents:
                                default_agents = phase_agents["_default"]
                                if default_agents:  # Non-empty default
                                    filtered_agents = default_agents
                                # Empty default = use all allowed
                except Exception as e:
                    logger.debug(f"Could not load session for phase info: {e}")
            
            return JSONResponse({
                "enabled": enabled,
                "phase_variable": phase_variable,
                "current_phase": current_phase,
                "phase_agents": phase_agents,
                "all_allowed_agents": all_allowed,
                "filtered_agents": filtered_agents
            })

        except Exception as e:
            logger.error(f"Error fetching phase info: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

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
