"""Web UI endpoints for Memory management plugin."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.schema_router import create_schema_router

logger = logging.getLogger(__name__)


class MemoryWebFactory:
    """Web UI factory for Memory management."""
    
    def __init__(self, server):
        """
        Initialize web factory.
        
        Args:
            server: MemoryServer instance
        """
        self.server = server
        self.plugin_dir = Path(__file__).parent
        self.templates_dir = self.plugin_dir / "templates"
        self.templates = Jinja2Templates(directory=str(self.templates_dir))
    
    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        # Get schema from server (already loaded with Jinja2 templates rendered)
        schema = self.server.get_schema_data() if hasattr(self.server, 'get_schema_data') else {}
        
        # Generate router from schema
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self
        )
    
    # Handler methods (called by schema router)
    
    async def get_panel(self, request: Request) -> HTMLResponse:
        """Render the Memory management dashboard."""
        return self.templates.TemplateResponse(
            "panel.html",
            {
                "request": request,
                "name": self.server.name
            }
        )
    
    async def get_memories(
        self,
        request: Request,
        session_id: str = Query(..., description="Session ID"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        sort_by: str = Query("accessed"),
        sort_order: str = Query("desc")
    ) -> JSONResponse:
            """
            Get memories with pagination and sorting (JSON).
            
            Args:
                session_id: Session ID (required)
                limit: Max memories to return (1-500, default 50)
                offset: Offset for pagination (default 0)
                sort_by: Sort field (created/updated/accessed/importance, default accessed)
                sort_order: Sort order (asc/desc, default desc)
            """
            try:
                result = await self.server._operation_list(
                    session_id=session_id,
                    limit=limit,
                    offset=offset,
                    sort_by=sort_by,
                    sort_order=sort_order
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error fetching memories: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e), "memories": []},
                    status_code=500
                )
    
    async def get_memory(
        self,
        request: Request,
        memory_id: str,
        session_id: str = Query(..., description="Session ID")
    ) -> JSONResponse:
            """
            Get a single memory by ID (JSON).
            
            Args:
                memory_id: Memory ID
                session_id: Session ID
            """
            try:
                result = await self.server._operation_recall(
                    session_id=session_id,
                    memory_id=memory_id
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error fetching memory {memory_id}: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=404 if "not found" in str(e).lower() else 500
                )
    
    async def search_memories(
        self,
        request: Request,
        session_id: str = Query(..., description="Session ID")
    ) -> JSONResponse:
            """
            Semantic search for memories (JSON).
            
            Body:
                {
                    "query": "search terms",
                    "n_results": 5
                }
            """
            try:
                body = await request.json()
                query = body.get("query")
                
                if not query:
                    raise HTTPException(status_code=400, detail="Missing 'query' parameter")
                
                result = await self.server._operation_search(
                    session_id=session_id,
                    query=query,
                    n_results=body.get("n_results", 5)
                )
                
                return JSONResponse(result)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error searching memories: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e), "results": []},
                    status_code=500
                )
    
    async def create_memory(
        self,
        request: Request,
        session_id: str = Query(..., description="Session ID")
    ) -> JSONResponse:
            """
            Create a new memory (JSON).
            
            Body:
                {
                    "title": "Memory title",
                    "content": "Memory content",
                    "keywords": ["optional", "keywords"],
                    "importance": 5,
                    "tags": ["optional", "tags"]
                }
            """
            try:
                body = await request.json()
                
                title = body.get("title")
                content = body.get("content")
                
                if not title or not content:
                    raise HTTPException(status_code=400, detail="Missing 'title' or 'content'")
                
                result = await self.server._operation_store(
                    session_id=session_id,
                    title=title,
                    content=content,
                    keywords=body.get("keywords"),
                    importance=body.get("importance", 5),
                    tags=body.get("tags"),
                    agent_name=body.get("agent_name")
                )
                
                return JSONResponse(result, status_code=201)
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error creating memory: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=500
                )
    
    async def delete_memory(
        self,
        request: Request,
        memory_id: str,
        session_id: str = Query(..., description="Session ID")
    ) -> JSONResponse:
            """
            Delete a memory (JSON).
            
            Args:
                memory_id: Memory ID to delete
                session_id: Session ID
            """
            try:
                result = await self.server._operation_delete(
                    session_id=session_id,
                    memory_id=memory_id
                )
                
                return JSONResponse(result)
                
            except Exception as e:
                logger.error(f"Error deleting memory {memory_id}: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=404 if "not found" in str(e).lower() else 500
                )
    
    async def get_stats(self, request: Request, session_id: str = Query(..., description="Session ID")) -> JSONResponse:
            """
            Get summary statistics (JSON).
            
            Args:
                session_id: Session ID
            """
            try:
                collection = await self.server._load_collection(session_id)
                
                if not collection.memories:
                    return JSONResponse({
                        "total_memories": 0,
                        "avg_importance": 0.0,
                        "total_accesses": 0,
                        "most_accessed": None,
                        "most_important": None,
                        "top_keywords": []
                    })
                
                memories = list(collection.memories.values())
                
                # Calculate stats
                total_memories = len(memories)
                avg_importance = sum(m.importance for m in memories) / total_memories
                total_accesses = sum(m.access_count for m in memories)
                
                # Most accessed
                most_accessed = max(memories, key=lambda m: m.access_count)
                most_accessed_info = {
                    "memory_id": most_accessed.memory_id,
                    "title": most_accessed.title,
                    "access_count": most_accessed.access_count
                }
                
                # Most important
                most_important = max(memories, key=lambda m: m.importance)
                most_important_info = {
                    "memory_id": most_important.memory_id,
                    "title": most_important.title,
                    "importance": most_important.importance
                }
                
                # Top keywords (frequency)
                keyword_freq = {}
                for mem in memories:
                    for kw in mem.keywords:
                        keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
                
                top_keywords = sorted(
                    keyword_freq.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:10]
                
                return JSONResponse({
                    "total_memories": total_memories,
                    "avg_importance": round(avg_importance, 2),
                    "total_accesses": total_accesses,
                    "most_accessed": most_accessed_info,
                    "most_important": most_important_info,
                    "top_keywords": [{"keyword": kw, "count": cnt} for kw, cnt in top_keywords]
                })
                
            except Exception as e:
                logger.error(f"Error fetching stats: {e}", exc_info=True)
                return JSONResponse(
                    {"error": str(e)},
                    status_code=500
                )
