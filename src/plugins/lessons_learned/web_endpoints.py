"""Web endpoints of the Lessons Learned plugin: the panel and the calls it makes."""

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

if TYPE_CHECKING:
    from .server import LessonsLearnedServer

logger = logging.getLogger(__name__)


class LessonForm(BaseModel):
    """A lesson as the panel's editor sends it, whole: checked here, as the store would fail on it only in SQLite.

    No length limit and a priority not yet whole: a lesson stored before the limits may exceed the form, and has to be
    saved back unchanged (the store rounds the priority). Without tags, the stored ones stay."""

    model_config = ConfigDict(str_strip_whitespace=True)
    agent_name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    category: str = Field(min_length=1)
    priority: float = Field(ge=1, le=10)
    status: Literal["draft", "active", "inactive", "archived"]
    source_type: Literal["manual", "cross_agent", "reflection", "auto"]
    confidence: float = Field(ge=0.0, le=1.0)
    tags: Optional[list[str]] = None


class SearchForm(BaseModel):
    query: str = Field(min_length=1)
    agent_name: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    limit: int = Field(50, ge=1, le=100)


class ConsolidateForm(BaseModel):
    agent_name: Optional[str] = None
    similarity_threshold: Optional[float] = Field(None, ge=0.5, le=1.0)
    dry_run: bool


class CleanupForm(BaseModel):
    agent_name: Optional[str] = None
    status: Optional[str] = None
    older_than_days: Optional[int] = Field(None, ge=1)
    max_evidence_count: Optional[int] = Field(None, ge=0)
    max_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    dry_run: bool
    lesson_ids: Optional[list[str]] = None


def answered(result: dict, refused: int) -> dict:
    """The server answers a refusal like a result; here it becomes an error, which the panel shows rather than takes for done."""
    if "error" in result:
        raise HTTPException(status_code=refused, detail=result["error"])
    return result


class LessonsWebFactory:
    """Web UI factory for Lessons Learned management."""

    def __init__(self, server: "LessonsLearnedServer"):
        self.server = server
        self.runs: set[asyncio.Task] = set()  # consolidations going on after their client left
        self.merging = False
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def get_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def get_lessons(
        self,
        request: Request,
        agent_name: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        sort_by: Literal["priority", "confidence", "evidence", "created", "updated"] = "priority",
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        return await self.server.list_lessons(agent_name=agent_name, status=status, category=category,
                                              sort_by=sort_by, limit=limit, offset=offset)

    async def get_lesson_detail(self, request: Request, lesson_id: str) -> dict:
        lesson = await self.server.get_lesson(lesson_id)
        if not lesson:
            raise HTTPException(status_code=404, detail=f"Lesson '{lesson_id}' not found.")
        return lesson

    async def create_lesson(self, request: Request, form: LessonForm) -> dict:
        """400 for what the store refuses: the lesson limit of the agent reached."""
        return answered(await self.server.store_lesson(**form.model_dump()), 400)

    async def update_lesson(self, request: Request, lesson_id: str, form: LessonForm) -> dict:
        """The panel sends every field, so the only refusal left is a lesson gone meanwhile."""
        return answered(await self.server.update_lesson(lesson_id, **form.model_dump()), 404)

    async def delete_lesson(self, request: Request, lesson_id: str) -> dict:
        return answered(await self.server.delete_lesson(lesson_id), 404)

    async def activate_lesson(self, request: Request, lesson_id: str) -> dict:
        """Only a draft: a row out of date must not revive a lesson an agent's evidence turned inactive meanwhile."""
        lesson = await self.server.get_lesson(lesson_id)
        if not lesson:
            raise HTTPException(status_code=404, detail=f"Lesson '{lesson_id}' not found.")
        if lesson["status"] != "draft":
            raise HTTPException(status_code=409, detail=f"Lesson '{lesson_id}' is {lesson['status']} now.")
        return answered(await self.server.update_lesson(lesson_id, status="active"), 404)

    async def search_lessons(self, request: Request, form: SearchForm) -> dict:
        """Every status unless one is asked for (the store's own default is active only)."""
        return answered(await self.server.search_lessons(query=form.query, agent_name=form.agent_name,
                                                         status=form.status, category=form.category,
                                                         limit=form.limit), 503)

    async def get_stats(self, request: Request, agent_name: Optional[str] = None) -> dict:
        return await self.server.get_stats(agent_name=agent_name)

    async def get_categories(self, request: Request) -> dict:
        return {"categories": await self.server.list_categories()}

    async def consolidate(self, request: Request, form: ConsolidateForm) -> StreamingResponse:
        """Merge similar lessons by LLM evaluation; progress, then the result or the error, one JSON object per line.

        One merge at a time (409): two would merge copies of the same lessons, one of them already gone."""
        if not form.dry_run:
            if self.merging:
                raise HTTPException(status_code=409, detail="A consolidation is merging already.")
            self.merging = True
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            try:
                result = await self.server.consolidate_lessons(agent_name=form.agent_name,
                                                               similarity_threshold=form.similarity_threshold,
                                                               dry_run=form.dry_run, progress_callback=queue.put)
                await queue.put({"type": "result", "data": result})
            except Exception as error:
                logger.exception("Consolidation failed")
                await queue.put({"type": "error", "message": str(error)})
            finally:
                if not form.dry_run:
                    self.merging = False

        task = asyncio.create_task(run())
        self.runs.add(task)
        task.add_done_callback(self.runs.discard)

        async def lines():
            try:
                while True:
                    message = await queue.get()
                    yield json.dumps(message) + "\n"
                    if message["type"] in ("result", "error"):
                        break
            finally:
                # a client gone ends a dry run; a merge runs on, as cut short it could leave a lesson half merged
                if form.dry_run and not task.done():
                    task.cancel()

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    async def cleanup(self, request: Request, form: CleanupForm) -> dict:
        """Without a filter it would take every lesson of every agent: refused. `lesson_ids` narrows a delete to a preview's."""
        criteria = form.model_dump(exclude={"dry_run", "lesson_ids"})
        if all(value in (None, "") for value in criteria.values()):
            raise HTTPException(status_code=400, detail="Set at least one filter.")
        return await self.server.cleanup_lessons(**criteria, dry_run=form.dry_run, lesson_ids=form.lesson_ids)
