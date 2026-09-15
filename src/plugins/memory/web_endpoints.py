"""Web endpoints of the Memory plugin: the panel and the calls it makes."""

from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from .server import ChromaDBError, StorageError

# the session ids the session manager hands out: the id names a file and a vector collection, so nothing else gets in
SESSION = Query(..., pattern=r"^[A-Za-z0-9_-]+$", description="The session whose memories")
SHOWN = 100
FOUND = 20


class SearchForm(BaseModel):
    query: str = Field(min_length=1, max_length=500)


@contextmanager
def store_available():
    """A metadata file that cannot be read or written, or a failing vector store: 503 with the reason."""
    try:
        yield
    except (StorageError, ChromaDBError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


class MemoryWebFactory:
    """Web UI factory for Memory management."""

    def __init__(self, server):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def get_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def get_memories(self, request: Request, session_id: str = SESSION) -> dict:
        """The most recently accessed memories of the session, and how many it has."""
        with store_available():
            listed = await self.server._operation_list(session_id=session_id, limit=SHOWN)
        return {"memories": listed["memories"], "total": listed["total"]}

    async def get_stats(self, request: Request, session_id: str = SESSION) -> dict:
        with store_available():
            memories = list((await self.server._load_collection(session_id)).memories.values())
        return {
            "total_memories": len(memories),
            "avg_importance": round(sum(m.importance for m in memories) / len(memories), 2) if memories else None,
            "total_accesses": sum(m.access_count for m in memories),
        }

    async def search_memories(self, request: Request, form: SearchForm, session_id: str = SESSION) -> dict:
        """Read-only like the list: a search from the panel counts as no access by an agent."""
        with store_available():
            return await self.server._operation_search(session_id=session_id, query=form.query, n_results=FOUND)

    async def delete_memory(self, request: Request, memory_id: str, session_id: str = SESSION) -> dict:
        with store_available():
            result = await self.server._operation_delete(session_id=session_id, memory_id=memory_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
