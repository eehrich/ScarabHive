"""Web UI endpoints for TODO management plugin."""

import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)


class TodoWebFactory:
    """Web UI factory for TODO management."""
    
    def __init__(self, server):
        """
        Initialize web factory.
        
        Args:
            server: TodoServer instance
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
            """Render the TODO management dashboard."""
            return self.render_panel(request)
        
        @router.get("/tasks")
        async def get_tasks(
            session_id: Optional[str] = Query(None),
            status: Optional[str] = Query(None),
            priority: Optional[str] = Query(None),
            tags: Optional[str] = Query(None)
        ):
            """
            Get tasks with filters (JSON).
            
            Args:
                session_id: Filter by session ID
                status: Filter by status (comma-separated)
                priority: Filter by priority (comma-separated)
                tags: Filter by tags (comma-separated)
            """
            try:
                # Build context
                context = {"session_id": session_id} if session_id else {}
                
                # Parse filters
                filter_status = status.split(",") if status else None
                filter_priority = priority.split(",") if priority else None
                filter_tags = tags.split(",") if tags else None
                
                # Call server's list_todos
                result = await self.server.list_todos(
                    filter_status=filter_status,
                    filter_priority=filter_priority,
                    filter_tags=filter_tags,
                    context=context
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error fetching tasks: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e), "tasks": {}},
                    status_code=500
                )
        
        @router.get("/stats")
        async def get_stats(session_id: Optional[str] = Query(None)):
            """
            Get summary statistics (JSON).
            
            Args:
                session_id: Filter by session ID
            """
            try:
                context = {"session_id": session_id} if session_id else {}
                
                # Call server's get_progress_summary
                result = await self.server.get_progress_summary(context=context)
                
                # Transform for WebUI: extract numeric completion rate
                # Server returns: "X/Y (Z.Z%)" -> extract Z.Z as number
                completion_rate_str = result.get("completion_rate", "0/0 (0.0%)")
                if isinstance(completion_rate_str, str) and "(" in completion_rate_str:
                    # Extract percentage: "5/10 (50.0%)" -> 50.0
                    percentage_part = completion_rate_str.split("(")[1].split("%")[0]
                    completion_rate_num = float(percentage_part)
                else:
                    completion_rate_num = 0.0
                
                # Add individual status counts for easier access
                by_status = result.get("by_status", {})
                
                return JSONResponse({
                    "total_tasks": result.get("total_tasks", 0),
                    "completion_rate": completion_rate_num,  # Numeric 0-100
                    "not_started": by_status.get("not-started", 0),
                    "in_progress": by_status.get("in-progress", 0),
                    "completed": by_status.get("completed", 0),
                    "blocked": by_status.get("blocked", 0),
                    "cancelled": by_status.get("cancelled", 0),
                    "overall_progress": result.get("overall_progress", 0.0),
                    "by_status": by_status,
                    "by_priority": result.get("by_priority", {}),
                })
                
            except Exception as e:
                logger.error(f"Error fetching stats: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=500
                )
        
        @router.post("/tasks/{task_id}/start")
        async def start_task(
            task_id: str,
            session_id: Optional[str] = Query(None)
        ):
            """
            Quick action: Start task (set status to in-progress).
            
            Args:
                task_id: Task ID to start
                session_id: Session ID context
            """
            try:
                context = {"session_id": session_id} if session_id else {}
                
                result = await self.server.update_todo(
                    task_id=task_id,
                    new_status="in-progress",
                    context=context
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error starting task {task_id}: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=500
                )
        
        @router.post("/tasks/{task_id}/complete")
        async def complete_task(
            task_id: str,
            session_id: Optional[str] = Query(None)
        ):
            """
            Quick action: Complete task (set status to completed).
            
            Args:
                task_id: Task ID to complete
                session_id: Session ID context
            """
            try:
                context = {"session_id": session_id} if session_id else {}
                
                result = await self.server.update_todo(
                    task_id=task_id,
                    new_status="completed",
                    context=context
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error completing task {task_id}: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=500
                )
        
        @router.delete("/tasks/{task_id}")
        async def delete_task(
            task_id: str,
            cascade: bool = Query(False),
            session_id: Optional[str] = Query(None)
        ):
            """
            Quick action: Delete task.
            
            Args:
                task_id: Task ID to delete
                cascade: Also delete dependent tasks
                session_id: Session ID context
            """
            try:
                context = {"session_id": session_id} if session_id else {}
                
                result = await self.server.delete_todo(
                    task_id=task_id,
                    cascade=cascade,
                    context=context
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error deleting task {task_id}: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=500
                )
        
        return router
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Get panel definitions for this plugin."""
        return [
            {
                "id": "todo",
                "title": "TODO Manager",
                "icon": "📝",
                "endpoint": "/plugins/todo/panel",
                "type": "iframe",
                "default_height": 700,
            }
        ]
    
    def get_static_assets(self) -> Dict[str, Path]:
        """Get static assets for this plugin."""
        return {}
    
    def render_panel(self, request: Request) -> HTMLResponse:
        """Render the TODO management dashboard."""
        return self.templates.TemplateResponse(
            "panel.html",
            {
                "request": request,
                "name": self.server.name
            }
        )
