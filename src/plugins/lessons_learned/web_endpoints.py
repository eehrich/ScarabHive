"""Web UI endpoints for Lessons Learned management plugin."""

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.schema_router import create_schema_router

if TYPE_CHECKING:
    from .server import LessonsLearnedServer

logger = logging.getLogger(__name__)


class LessonsWebFactory:
    """Web UI factory for Lessons Learned management."""

    def __init__(self, server: "LessonsLearnedServer"):
        self.server = server
        self.plugin_dir = Path(__file__).parent
        self.templates_dir = self.plugin_dir / "templates"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))

    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        schema = self.server.get_schema_data() if hasattr(self.server, "get_schema_data") else {}
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self,
        )

    # =========================================================================
    # Handler methods (called by schema router)
    # =========================================================================

    async def get_panel(self, request: Request) -> HTMLResponse:
        """Render the Lessons Learned admin dashboard."""
        return self.templates.TemplateResponse(
            "lessons_admin.html",
            {"request": request, "name": self.server.name},
        )

    async def get_lessons(
        self,
        request: Request,
        agent_name: str = Query(None, description="Filter by agent name"),
        status: str = Query(None, description="Filter by status"),
        category: str = Query(None, description="Filter by category"),
        sort_by: str = Query("priority", description="Sort field"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        """Get lessons with filtering, pagination and sorting."""
        try:
            result = await self.server.list_lessons(
                agent_name=agent_name,
                status=status,
                category=category,
                sort_by=sort_by,
                limit=limit,
                offset=offset,
            )
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Error fetching lessons: {e}", exc_info=True)
            return JSONResponse({"error": str(e), "lessons": []}, status_code=500)

    async def get_lesson_detail(
        self,
        request: Request,
        lesson_id: str,
    ) -> JSONResponse:
        """Get a single lesson by ID with evidence summary."""
        try:
            lesson = await self.server.get_lesson(lesson_id)
            if not lesson:
                raise HTTPException(status_code=404, detail=f"Lesson '{lesson_id}' not found")
            return JSONResponse(lesson)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching lesson {lesson_id}: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def create_lesson(self, request: Request) -> JSONResponse:
        """Create a new lesson manually."""
        try:
            body = await request.json()
            agent_name = body.get("agent_name")
            title = body.get("title")
            content = body.get("content")

            if not agent_name or not title or not content:
                raise HTTPException(status_code=400, detail="Missing required fields: agent_name, title, content")

            result = await self.server.store_lesson(
                agent_name=agent_name,
                title=title,
                content=content,
                category=body.get("category", "general"),
                priority=body.get("priority", 5),
                tags=body.get("tags"),
                source_type=body.get("source_type", "manual"),
                status=body.get("status", "active"),
                confidence=body.get("confidence", 0.8),
            )
            return JSONResponse(result, status_code=201)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating lesson: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def update_lesson(self, request: Request, lesson_id: str) -> JSONResponse:
        """Update a lesson."""
        try:
            body = await request.json()
            result = await self.server.update_lesson(lesson_id, **body)
            if "error" in result:
                return JSONResponse(result, status_code=400)
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Error updating lesson {lesson_id}: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def delete_lesson(self, request: Request, lesson_id: str) -> JSONResponse:
        """Delete a lesson."""
        try:
            result = await self.server.delete_lesson(lesson_id)
            if "error" in result:
                return JSONResponse(result, status_code=404)
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Error deleting lesson {lesson_id}: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def activate_lesson(self, request: Request, lesson_id: str) -> JSONResponse:
        """Change lesson status to active."""
        try:
            result = await self.server.update_lesson(lesson_id, status="active")
            if "error" in result:
                return JSONResponse(result, status_code=400)
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Error activating lesson {lesson_id}: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def search_lessons(self, request: Request) -> JSONResponse:
        """Semantic search for lessons."""
        try:
            body = await request.json()
            query = body.get("query")
            if not query:
                raise HTTPException(status_code=400, detail="Missing 'query' parameter")

            result = await self.server.search_lessons(
                query=query,
                agent_name=body.get("agent_name"),
                category=body.get("category"),
                limit=body.get("limit", 10),
            )
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error searching lessons: {e}", exc_info=True)
            return JSONResponse({"error": str(e), "results": []}, status_code=500)

    async def get_stats(
        self,
        request: Request,
        agent_name: str = Query(None, description="Filter by agent name"),
    ) -> JSONResponse:
        """Get dashboard statistics."""
        try:
            result = await self.server.get_stats(agent_name=agent_name)
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Error fetching stats: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def get_categories(self, request: Request) -> JSONResponse:
        """List all lesson categories."""
        try:
            categories = await self.server.list_categories()
            return JSONResponse({"categories": categories})
        except Exception as e:
            logger.error(f"Error fetching categories: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

    async def consolidate(self, request: Request) -> StreamingResponse:
        """Consolidate similar lessons using LLM evaluation, streaming progress."""
        body = await request.json()
        agent_name = body.get("agent_name")
        dry_run = body.get("dry_run", False)
        similarity_threshold = body.get("similarity_threshold")

        queue: asyncio.Queue = asyncio.Queue()

        async def progress_callback(msg: dict) -> None:
            await queue.put(msg)

        async def run_consolidation() -> None:
            try:
                result = await self.server.consolidate_lessons(
                    agent_name=agent_name,
                    similarity_threshold=similarity_threshold,
                    dry_run=dry_run,
                    progress_callback=progress_callback,
                )
                await queue.put({"type": "result", "data": result})
            except Exception as e:
                logger.error(f"Consolidation failed: {e}", exc_info=True)
                await queue.put({"type": "error", "message": str(e)})

        task = asyncio.create_task(run_consolidation())

        async def generate():
            try:
                while True:
                    msg = await queue.get()
                    yield json.dumps(msg) + "\n"
                    if msg.get("type") in ("result", "error"):
                        break
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    async def cleanup(self, request: Request) -> JSONResponse:
        """Bulk-delete lessons matching filter criteria."""
        try:
            body = await request.json()
            result = await self.server.cleanup_lessons(
                older_than_days=body.get("older_than_days"),
                max_evidence_count=body.get("max_evidence_count"),
                max_confidence=body.get("max_confidence"),
                status=body.get("status"),
                agent_name=body.get("agent_name"),
                dry_run=body.get("dry_run", True),
            )
            return JSONResponse(result)
        except Exception as e:
            logger.error(f"Cleanup failed: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)
