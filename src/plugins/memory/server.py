"""
Memory Plugin - MCP Server Implementation

Provides persistent memory storage and retrieval for agent conversations.
Uses ChromaDB for semantic vector search and JSON for metadata.

Key features:
- Simple design: 1 tool (memory) with multi-mode detection (store/recall/search/list/delete)
- ChromaDB integration for semantic search
- JSON file persistence for metadata (access counts, timestamps, importance)
- System prompt injection hook (auto-inject memory titles before LLM calls)
- Web UI for viewing and managing memories
"""

import asyncio
import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import re

from pydantic import BaseModel, Field, field_serializer

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import PluginHook, HookContext, HookResult
from agent_system.utils.vector_store import VectorStore

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

class Memory(BaseModel):
    """Core memory data structure"""

    # Identity
    memory_id: str = Field(..., description="Unique memory identifier (mem_YYYYMMDD_uuid)")
    title: str = Field(..., min_length=1, max_length=200, description="Memory title")

    # Content
    content: str = Field(..., min_length=1, max_length=5000, description="Memory content")
    keywords: List[str] = Field(default_factory=list, description="Keywords for search")

    # Context
    session_id: str = Field(..., description="Session that created this memory")
    agent_name: Optional[str] = Field(default=None, description="Agent that created memory")

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Access tracking
    access_count: int = Field(default=0, ge=0, description="Number of times recalled")

    # Optional metadata
    importance: int = Field(default=5, ge=1, le=10, description="Importance level 1-10")
    tags: List[str] = Field(default_factory=list, description="Categorization tags")

    @field_serializer('created_at', 'updated_at', 'accessed_at')
    def serialize_datetime(self, dt: datetime, _info) -> str:
        """Serialize datetime to ISO format with 'Z' suffix for UTC."""
        return dt.isoformat().replace('+00:00', 'Z')


class MemoryCollection(BaseModel):
    """Session-scoped memory collection (metadata only, vectors in ChromaDB)"""

    session_id: str
    memories: Dict[str, Memory] = Field(default_factory=dict)  # memory_id -> Memory
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    total_memories: int = Field(default=0, ge=0)

    @field_serializer('created_at', 'updated_at')
    def serialize_datetime(self, dt: datetime, _info) -> str:
        """Serialize datetime to ISO format with 'Z' suffix for UTC."""
        return dt.isoformat().replace('+00:00', 'Z')


# =============================================================================
# Exceptions
# =============================================================================

class MemoryError(Exception):
    """Base exception for Memory plugin errors"""
    pass


class ValidationError(MemoryError):
    """Memory data validation failed"""
    pass


class StorageError(MemoryError):
    """Persistence layer error"""
    pass


class ChromaDBError(MemoryError):
    """ChromaDB operation failed"""
    pass


# =============================================================================
# Memory Management Server
# =============================================================================

class MemoryServer(SchemaBasedMCPServer, PluginHook):
    """
    Memory Management Server with ChromaDB vector search.

    Implements:
    - MCP tool: 'memory' with operations (store/recall/search/list/delete)
    - Hook: inject_memory_context (pre_llm_call)
    - Storage: ChromaDB (vectors) + JSON (metadata)
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig"
    ):
        super().__init__(name, system_config, mcp_config)

        # Storage paths
        self.storage_path = Path("data/memories")
        self.vector_store_path = self.storage_path / "vectors"

        # Create directories
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.vector_store_path.mkdir(parents=True, exist_ok=True)

        # Initialize vector store
        self.vector_store = VectorStore(persist_path=str(self.vector_store_path))

        # In-memory cache for metadata (session_id -> MemoryCollection)
        # Limited to prevent memory leaks
        self._collections_cache: Dict[str, MemoryCollection] = {}
        self._cache_access_times: Dict[str, float] = {}  # session_id -> last_access_time
        self._max_cache_size = 100  # Maximum cached collections
        self._cache_ttl = 1800.0  # 30 minutes TTL

        # Memory ID counters per session (session_id -> int)
        self._memory_counters: Dict[str, int] = {}

        logger.info(f"MemoryServer initialized with storage_path={self.storage_path}")

    def _get_collection_name(self, session_id: str) -> str:
        """Get collection name for session"""
        # Sanitize session_id for collection name
        return f"session_{session_id.replace('-', '_')}"

    async def _generate_memory_id(self, session_id: str) -> str:
        """
        Generate unique memory ID for session using counter.

        Format: mem_001, mem_002, etc.

        Args:
            session_id: Session identifier

        Returns:
            Memory ID (e.g., "mem_042")
        """
        # Initialize counter from existing memories if not set
        if session_id not in self._memory_counters:
            # Check if we have existing memories in cache or storage
            try:
                collection = self._collections_cache.get(session_id)
                if not collection:
                    # Try to load from storage (async)
                    collection = await self._load_collection(session_id)

                if collection and collection.memories:
                    # Find highest memory number
                    max_num = 0
                    for memory_id in collection.memories.keys():
                        if memory_id.startswith("mem_"):
                            try:
                                num = int(memory_id.split("_")[1])
                                max_num = max(max_num, num)
                            except (IndexError, ValueError):
                                pass
                    self._memory_counters[session_id] = max_num
                else:
                    self._memory_counters[session_id] = 0
            except Exception:
                self._memory_counters[session_id] = 0

        # Increment counter
        counter = self._memory_counters.get(session_id, 0)
        counter += 1
        self._memory_counters[session_id] = counter

        return f"mem_{counter:03d}"

    def _extract_keywords(self, text: str, max_keywords: int = 10) -> List[str]:
        """
        Extract keywords from text using language-agnostic statistical approach.

        Strategy:
        1. Extract all words (Unicode-aware for multilingual support)
        2. Calculate TF (term frequency) for each word
        3. Filter very common words using document frequency heuristic
        4. For short texts, use word length as additional signal

        Args:
            text: Text to extract keywords from
            max_keywords: Maximum number of keywords to extract

        Returns:
            List of keywords (lowercased, unique)
        """
        from collections import Counter

        # Split into sentences for document frequency calculation
        sentences = re.split(r'[.!?]+', text)
        sentences = [s.strip() for s in sentences if s.strip()]

        if not sentences:
            return []

        # Extract words (Unicode-aware for any language)
        all_words = re.findall(r'\b\w{3,}\b', text.lower(), re.UNICODE)

        if not all_words:
            return []

        # Calculate word frequency
        word_counts = Counter(all_words)
        total_words = len(all_words)

        # Calculate document frequency (how many sentences contain each word)
        word_doc_freq = {}
        for word in word_counts.keys():
            word_doc_freq[word] = sum(1 for s in sentences if word in s.lower())

        # Score each word based on:
        # - Term frequency (how often it appears)
        # - Document frequency (avoid words in every sentence - likely stopwords)
        # - Word length (longer words are usually more meaningful)
        scored_keywords = []
        for word, count in word_counts.items():
            tf = count / total_words
            df_ratio = word_doc_freq[word] / len(sentences)

            # Skip words that appear in all or most sentences (>= 80% = likely stopwords)
            # For single sentence texts, skip words appearing more than 3 times
            if len(sentences) == 1:
                if count > 3:
                    continue
            else:
                if df_ratio >= 0.8:
                    continue

            # Calculate score: TF * inverse DF * length bonus
            # Longer words get slight preference (max 1.5x bonus for 10+ char words)
            length_bonus = min(1.5, 1.0 + len(word) / 20)
            score = tf * (1 - df_ratio) * length_bonus

            scored_keywords.append((word, score))

        # Sort by score and return top N
        scored_keywords.sort(key=lambda x: x[1], reverse=True)
        return [word for word, _ in scored_keywords[:max_keywords]]

    # Storage methods will be added in next task
    # Tool implementation will be added in next task
    # Hook implementation will be added in next task

    # =========================================================================
    # Storage Layer (ChromaDB + JSON hybrid)
    # =========================================================================

    def _get_metadata_path(self, session_id: str) -> Path:
        """Get path to JSON metadata file for session"""
        return self.storage_path / f"{session_id}.json"

    def _load_collection_sync(self, session_id: str) -> MemoryCollection:
        """Load memory collection from JSON - sync version for thread pool."""
        metadata_path = self._get_metadata_path(session_id)

        if not metadata_path.exists():
            return MemoryCollection(session_id=session_id)

        with open(metadata_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return MemoryCollection(**data)

    async def _load_collection(self, session_id: str) -> MemoryCollection:
        """Load memory collection from JSON metadata file"""
        import time as _time
        
        # Check cache first
        if session_id in self._collections_cache:
            self._cache_access_times[session_id] = _time.time()
            return self._collections_cache[session_id]

        metadata_path = self._get_metadata_path(session_id)

        if not metadata_path.exists():
            # Create new collection
            collection = MemoryCollection(session_id=session_id)
            self._add_to_cache(session_id, collection)
            return collection

        try:
            # Run sync file I/O in thread pool
            collection = await asyncio.to_thread(self._load_collection_sync, session_id)
            self._add_to_cache(session_id, collection)
            return collection

        except Exception as e:
            logger.error(f"Failed to load collection {session_id}: {e}")
            raise StorageError(f"Failed to load memories: {e}")
    
    def _add_to_cache(self, session_id: str, collection: MemoryCollection) -> None:
        """Add collection to cache with LRU eviction."""
        import time as _time
        
        # Evict old entries if at capacity
        self._cleanup_cache()
        
        self._collections_cache[session_id] = collection
        self._cache_access_times[session_id] = _time.time()
    
    def _cleanup_cache(self) -> None:
        """Clean up old cache entries using TTL and LRU."""
        import time as _time
        now = _time.time()
        
        # Remove expired entries (older than TTL)
        expired = [
            sid for sid, ts in self._cache_access_times.items()
            if now - ts > self._cache_ttl
        ]
        for sid in expired:
            self._collections_cache.pop(sid, None)
            self._cache_access_times.pop(sid, None)
            self._memory_counters.pop(sid, None)
        
        if expired:
            logger.debug(f"MemoryServer: Cleaned up {len(expired)} expired cache entries")
        
        # LRU eviction if still over limit
        while len(self._collections_cache) >= self._max_cache_size:
            if not self._cache_access_times:
                break
            # Find least recently used
            oldest = min(self._cache_access_times, key=self._cache_access_times.get)  # type: ignore[arg-type]
            self._collections_cache.pop(oldest, None)
            self._cache_access_times.pop(oldest, None)
            self._memory_counters.pop(oldest, None)
            logger.debug(f"MemoryServer: Evicted cache entry {oldest} (LRU)")

    def _save_collection_sync(self, collection: MemoryCollection) -> None:
        """Save memory collection to JSON - sync version for thread pool."""
        metadata_path = self._get_metadata_path(collection.session_id)
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(collection.model_dump(), f, indent=2, ensure_ascii=False)

    async def _save_collection(self, collection: MemoryCollection):
        """Save memory collection to JSON metadata file"""
        import time as _time
        
        try:
            # Update timestamp and count
            collection.updated_at = datetime.now(UTC)
            collection.total_memories = len(collection.memories)

            # Run sync file I/O in thread pool
            await asyncio.to_thread(self._save_collection_sync, collection)

            # Update cache with access time
            self._collections_cache[collection.session_id] = collection
            self._cache_access_times[collection.session_id] = _time.time()

            logger.debug(f"Saved collection {collection.session_id} with {collection.total_memories} memories")

        except Exception as e:
            logger.error(f"Failed to save collection {collection.session_id}: {e}")
            raise StorageError(f"Failed to save memories: {e}")

    async def _store_memory_in_chroma(
        self,
        session_id: str,
        memory: Memory
    ):
        """Store memory content in vector store for semantic search"""
        try:
            collection_name = self._get_collection_name(session_id)

            # Store in vector store with metadata (run sync call in thread pool)
            await asyncio.to_thread(
                self.vector_store.add,
                collection=collection_name,
                ids=[memory.memory_id],
                documents=[memory.content],
                metadatas=[{
                    "title": memory.title,
                    "keywords": ",".join(memory.keywords),
                    "importance": str(memory.importance),
                    "created_at": memory.created_at.isoformat(),
                    "tags": ",".join(memory.tags)
                }]
            )

            logger.debug(f"Stored memory {memory.memory_id} in vector store")

        except Exception as e:
            logger.error(f"Failed to store memory in vector store: {e}")
            raise ChromaDBError(f"Vector store storage failed: {e}")

    async def _delete_memory_from_chroma(
        self,
        session_id: str,
        memory_id: str
    ):
        """Delete memory from vector store"""
        try:
            collection_name = self._get_collection_name(session_id)
            # Run sync call in thread pool
            await asyncio.to_thread(
                self.vector_store.delete,
                collection=collection_name,
                ids=[memory_id]
            )
            logger.debug(f"Deleted memory {memory_id} from vector store")
        except Exception as e:
            logger.error(f"Failed to delete memory from vector store: {e}")
            # Don't raise - non-critical if vector store delete fails

    # =========================================================================
    # Memory Operations (Tool Implementation)
    # =========================================================================

    async def _operation_store(
        self,
        session_id: str,
        title: str,
        content: str,
        keywords: Optional[List[str]] = None,
        importance: int = 5,
        tags: Optional[List[str]] = None,
        agent_name: Optional[str] = None
    ) -> Dict:
        """Store a new memory"""
        # Auto-extract keywords if not provided
        if keywords is None or len(keywords) == 0:
            keywords = self._extract_keywords(f"{title} {content}")

        # Generate memory ID with session-specific counter (async)
        memory_id = await self._generate_memory_id(session_id)

        # Create memory object
        memory = Memory(
            memory_id=memory_id,
            title=title,
            content=content,
            keywords=keywords or [],
            session_id=session_id,
            agent_name=agent_name,
            importance=importance,
            tags=tags or []
        )

        # Store in ChromaDB
        await self._store_memory_in_chroma(session_id, memory)

        # Store metadata in JSON
        collection = await self._load_collection(session_id)
        collection.memories[memory_id] = memory
        await self._save_collection(collection)

        logger.info(f"Stored memory {memory_id}: {title}")

        return {
            "memory_id": memory_id,
            "title": title,
            "created_at": memory.created_at.isoformat(),
            "keywords": memory.keywords,
            "message": f"Memory stored successfully with ID: {memory_id}"
        }

    async def _operation_recall(
        self,
        session_id: str,
        memory_id: str
    ) -> Dict:
        """Recall a memory by ID (updates access count)"""
        collection = await self._load_collection(session_id)

        if memory_id not in collection.memories:
            logger.info(f"Memory {memory_id} not found in session {session_id}")
            return {
                "error": f"Memory {memory_id} not found",
                "message": f"Memory ID '{memory_id}' does not exist. Use memory(operation='list') to see available memories."
            }

        memory = collection.memories[memory_id]

        # Update access tracking
        memory.access_count += 1
        memory.accessed_at = datetime.now(UTC)
        memory.updated_at = datetime.now(UTC)

        await self._save_collection(collection)

        logger.info(f"Recalled memory {memory_id} (access_count={memory.access_count})")

        return {
            "memory_id": memory.memory_id,
            "title": memory.title,
            "content": memory.content,
            "keywords": memory.keywords,
            "tags": memory.tags,
            "importance": memory.importance,
            "created_at": memory.created_at.isoformat(),
            "accessed_at": memory.accessed_at.isoformat(),
            "access_count": memory.access_count
        }

    async def _operation_search(
        self,
        session_id: str,
        query: str,
        n_results: int = 5
    ) -> Dict:
        """Semantic search using vector embeddings"""
        try:
            collection_name = self._get_collection_name(session_id)

            # Perform semantic search (run sync call in thread pool)
            results = await asyncio.to_thread(
                self.vector_store.query,
                collection=collection_name,
                query_text=query,
                n_results=n_results,
                include=["documents", "metadatas", "distances"]
            )

            if not results["ids"] or len(results["ids"][0]) == 0:
                return {
                    "query": query,
                    "results": [],
                    "count": 0,
                    "message": "No memories found"
                }

            # Load metadata for access count updates
            metadata_collection = await self._load_collection(session_id)

            # Format results
            memories = []
            for i in range(len(results["ids"][0])):
                memory_id = results["ids"][0][i]

                # Get full memory from metadata for access count
                memory_meta = metadata_collection.memories.get(memory_id)

                # Calculate similarity score (1 - distance = easier to understand)
                # ChromaDB distance: 0 = identical, 2 = completely different
                # Similarity: 1.0 = perfect match, 0.0 = no match
                distance = results["distances"][0][i]
                similarity = max(0.0, min(1.0, 1.0 - (distance / 2.0)))

                memories.append({
                    "memory_id": memory_id,
                    "title": results["metadatas"][0][i]["title"],
                    "content": results["documents"][0][i],
                    "similarity": round(similarity, 3),  # 0.0-1.0, higher = better match
                    "importance": int(results["metadatas"][0][i].get("importance", "5")),
                    "keywords": results["metadatas"][0][i].get("keywords", "").split(","),
                    "access_count": memory_meta.access_count if memory_meta else 0
                })

            logger.info(f"Search '{query}' returned {len(memories)} results")

            return {
                "query": query,
                "results": memories,
                "count": len(memories),
                "message": f"Found {len(memories)} memories" if len(memories) > 0 else "No memories found matching your query"
            }

        except Exception as e:
            logger.error(f"Search failed: {e}")
            raise ChromaDBError(f"Semantic search failed: {e}")

    async def _operation_list(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "accessed",
        sort_order: str = "desc"
    ) -> Dict:
        """List all memories for session"""
        collection = await self._load_collection(session_id)

        # Convert to list
        memories_list = list(collection.memories.values())

        # Sort
        sort_key_map = {
            "created": lambda m: m.created_at,
            "updated": lambda m: m.updated_at,
            "accessed": lambda m: m.accessed_at,
            "importance": lambda m: m.importance
        }

        memories_list.sort(
            key=sort_key_map.get(sort_by, sort_key_map["accessed"]),
            reverse=(sort_order == "desc")
        )

        # Paginate
        paginated = memories_list[offset:offset + limit]

        # Format
        results = [
            {
                "memory_id": m.memory_id,
                "title": m.title,
                "content": m.content,  # Include content for web UI
                "keywords": m.keywords,
                "importance": m.importance,
                "created_at": m.created_at.isoformat(),
                "accessed_at": m.accessed_at.isoformat(),
                "access_count": m.access_count
            }
            for m in paginated
        ]

        logger.info(f"Listed {len(results)} memories for session {session_id}")

        return {
            "memories": results,
            "total": len(memories_list),
            "limit": limit,
            "offset": offset,
            "count": len(results)
        }

    async def _operation_delete(
        self,
        session_id: str,
        memory_id: str
    ) -> Dict:
        """Delete a memory"""
        collection = await self._load_collection(session_id)

        if memory_id not in collection.memories:
            logger.info(f"Memory {memory_id} not found for deletion in session {session_id}")
            return {
                "error": f"Memory {memory_id} not found",
                "message": f"Cannot delete: Memory ID '{memory_id}' does not exist."
            }

        # Delete from JSON metadata
        del collection.memories[memory_id]
        await self._save_collection(collection)

        # Delete from ChromaDB
        await self._delete_memory_from_chroma(session_id, memory_id)

        logger.info(f"Deleted memory {memory_id}")

        return {
            "deleted": True,
            "memory_id": memory_id,
            "message": f"Memory {memory_id} deleted successfully"
        }

    async def _operation_update(
        self,
        session_id: str,
        memory_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        importance: Optional[int] = None,
        tags: Optional[List[str]] = None
    ) -> Dict:
        """Update an existing memory (partial update supported)"""
        collection = await self._load_collection(session_id)

        if memory_id not in collection.memories:
            logger.info(f"Memory {memory_id} not found for update in session {session_id}")
            return {
                "error": f"Memory {memory_id} not found",
                "message": f"Cannot update: Memory ID '{memory_id}' does not exist."
            }

        memory = collection.memories[memory_id]

        # Track what changed
        changed_fields = []

        # Update fields if provided
        if title is not None:
            memory.title = title
            changed_fields.append("title")

        if content is not None:
            memory.content = content
            changed_fields.append("content")

            # Re-index in ChromaDB if content changed
            await self._store_memory_in_chroma(session_id, memory)

        if keywords is not None:
            memory.keywords = keywords
            changed_fields.append("keywords")

        if importance is not None:
            memory.importance = importance
            changed_fields.append("importance")

        if tags is not None:
            memory.tags = tags
            changed_fields.append("tags")

        # Update timestamp
        memory.updated_at = datetime.now(UTC)

        # Save metadata
        await self._save_collection(collection)

        logger.info(f"Updated memory {memory_id}: {', '.join(changed_fields)}")

        return {
            "memory_id": memory_id,
            "title": memory.title,
            "updated_fields": changed_fields,
            "updated_at": memory.updated_at.isoformat(),
            "message": f"Memory {memory_id} updated successfully"
        }

    # =========================================================================
    # MCP Tool Interface
    # =========================================================================

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main tool entry point - called when tool name matches server name ({{name}}).

        Unified memory tool handler.

        Supports operations: store, recall, search, list, delete
        """
        arguments = params or {}

        operation = arguments.get("operation")
        if not operation:
            raise ValidationError("Missing 'operation' parameter")

        # Get session ID from context (injected by agent system as _session_id)
        session_id = arguments.get("_session_id") or arguments.get("session_id", "default")
        agent_name = arguments.get("_agent_name") or arguments.get("agent_name")

        # Get status object for progress updates
        status = arguments.get("_status")

        try:
            # Execute operation with informative status updates
            if operation == "store":
                # Validate required parameters for store operation
                if "title" not in arguments:
                    error_msg = "Missing required parameter 'title' for store operation"
                    if status:
                        await status.error(error_msg)
                    logger.error(f"Memory store failed: {error_msg}")
                    return {"error": error_msg}
                
                if "content" not in arguments:
                    error_msg = "Missing required parameter 'content' for store operation"
                    if status:
                        await status.error(error_msg)
                    logger.error(f"Memory store failed: {error_msg}")
                    return {"error": error_msg}
                
                title = arguments["title"]
                if status:
                    await status.progress(f"Storing: {title[:50]}...")

                result = await self._operation_store(
                    session_id=session_id,
                    title=title,
                    content=arguments["content"],
                    keywords=arguments.get("keywords"),
                    importance=arguments.get("importance", 5),
                    tags=arguments.get("tags"),
                    agent_name=agent_name
                )

                if status:
                    await status.end(f"Stored: {result['memory_id']}")
                return result

            elif operation == "recall":
                # Validate required parameters for recall operation
                if "memory_id" not in arguments:
                    error_msg = "Missing required parameter 'memory_id' for recall operation"
                    if status:
                        await status.error(error_msg)
                    logger.error(f"Memory recall failed: {error_msg}")
                    return {"error": error_msg}
                
                memory_id = arguments["memory_id"]
                if status:
                    await status.progress(f"Recalling: {memory_id}")

                result = await self._operation_recall(
                    session_id=session_id,
                    memory_id=memory_id
                )

                if status:
                    if "error" in result:
                        await status.error(result.get("message", result["error"]))
                    else:
                        await status.end(f"Recalled: {result['title']}")
                return result

            elif operation == "search":
                # Validate required parameters for search operation
                if "query" not in arguments:
                    error_msg = "Missing required parameter 'query' for search operation"
                    if status:
                        await status.error(error_msg)
                    logger.error(f"Memory search failed: {error_msg}")
                    return {"error": error_msg}
                
                query = arguments["query"]
                n_results = arguments.get("n_results", 5)
                if status:
                    await status.progress(f"Searching: '{query[:50]}...'")

                result = await self._operation_search(
                    session_id=session_id,
                    query=query,
                    n_results=n_results
                )

                if status:
                    await status.end(f"Found {len(result.get('results', []))} memories")
                return result

            elif operation == "list":
                limit = arguments.get("limit", 50)
                if status:
                    await status.progress(f"Listing (limit: {limit})")

                result = await self._operation_list(
                    session_id=session_id,
                    limit=limit,
                    offset=arguments.get("offset", 0),
                    sort_by=arguments.get("sort_by", "accessed"),
                    sort_order=arguments.get("sort_order", "desc")
                )

                if status:
                    await status.end(f"Listed {len(result.get('memories', []))} memories")
                return result

            elif operation == "delete":
                # Validate required parameters for delete operation
                if "memory_id" not in arguments:
                    error_msg = "Missing required parameter 'memory_id' for delete operation"
                    if status:
                        await status.error(error_msg)
                    logger.error(f"Memory delete failed: {error_msg}")
                    return {"error": error_msg}
                
                memory_id = arguments["memory_id"]
                if status:
                    await status.progress(f"Deleting: {memory_id}")

                result = await self._operation_delete(
                    session_id=session_id,
                    memory_id=memory_id
                )

                if status:
                    if "error" in result:
                        await status.error(result.get("message", result["error"]))
                    else:
                        await status.end(f"Deleted: {memory_id}")
                return result

            elif operation == "update":
                # Validate required parameters for update operation
                if "memory_id" not in arguments:
                    error_msg = "Missing required parameter 'memory_id' for update operation"
                    if status:
                        await status.error(error_msg)
                    logger.error(f"Memory update failed: {error_msg}")
                    return {"error": error_msg}
                
                memory_id = arguments["memory_id"]
                if status:
                    await status.progress(f"Updating: {memory_id}")

                result = await self._operation_update(
                    session_id=session_id,
                    memory_id=memory_id,
                    title=arguments.get("title"),
                    content=arguments.get("content"),
                    keywords=arguments.get("keywords"),
                    importance=arguments.get("importance"),
                    tags=arguments.get("tags")
                )

                if status:
                    if "error" in result:
                        await status.error(result.get("message", result["error"]))
                    else:
                        await status.end(f"Updated: {memory_id}")
                return result

            else:
                raise ValidationError(f"Unknown operation: {operation}")

        except (ValidationError, StorageError, ChromaDBError) as e:
            error_msg = str(e)
            logger.error(f"Memory operation failed: {error_msg}")
            if status:
                await status.error(error_msg)
            return {"error": error_msg}

    # =========================================================================
    # Hook Implementation
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Inject memory context into system prompt.

        Adds relevant memories to system prompt before LLM call.
        Can use semantic search (if config.use_semantic_injection=true)
        or just recent memories.
        """
        try:
            # Get session ID from context
            session_id = getattr(context, 'session_id', 'default')

            # Load memories
            collection = await self._load_collection(session_id)

            if not collection.memories or len(collection.memories) == 0:
                return HookResult(success=True, modified=False)

            # Get config
            max_memories = 10  # Default, should come from config
            use_semantic = True  # Default

            # Get user's current message for semantic search
            user_message = None
            if context.messages and len(context.messages) > 0:
                last_msg = context.messages[-1]
                if hasattr(last_msg, 'content'):
                    user_message = last_msg.content

            # Select memories to inject
            if use_semantic and user_message:
                # Semantic search based on current user message
                search_result = await self._operation_search(
                    session_id=session_id,
                    query=user_message,
                    n_results=max_memories
                )
                relevant_memories = [
                    collection.memories[r["memory_id"]]
                    for r in search_result.get("results", [])
                    if r["memory_id"] in collection.memories
                ]
            else:
                # Just use most recently accessed
                memories_list = sorted(
                    collection.memories.values(),
                    key=lambda m: (m.importance, m.accessed_at),
                    reverse=True
                )
                relevant_memories = memories_list[:max_memories]

            if not relevant_memories:
                return HookResult(success=True, modified=False)

            # Build injection text with unique marker
            injection_marker = "AVAILABLE MEMORIES"
            lines = [f"\n## {injection_marker}"]
            lines.append("\nUse `memory(operation='recall', memory_id='...')` to access full content:\n")
            for mem in relevant_memories:
                # Limit title length to 80 chars with ellipsis
                title = mem.title if len(mem.title) <= 80 else mem.title[:77] + "..."
                lines.append(f"- `{mem.memory_id}`: {title}")

            injection = "\n".join(lines)

            # Check if already injected and REMOVE old injection
            from agent_system.llm.models import ChatMessage
            for i, msg in enumerate(context.messages):
                msg_content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
                if msg_content and injection_marker in msg_content:
                    # Remove old injection
                    context.messages.pop(i)
                    break

            # Insert after first system message
            insert_pos = self._find_system_message_position(context.messages)
            context.messages.insert(insert_pos, ChatMessage(
                role="system",
                content=injection
            ))

            logger.info(f"Injected {len(relevant_memories)} memories into system prompt")

            return HookResult(
                success=True,
                modified=True,
                context=context,
                metadata={"injected_memories": len(relevant_memories)}
            )

        except Exception as e:
            logger.error(f"Hook execution failed: {e}", exc_info=True)
            return HookResult(success=False, modified=False, metadata={"error": str(e)})

    def _find_system_message_position(self, messages: list) -> int:
        """Find position to insert system message (after first system message)."""
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, 'role') else msg.get('role')
            if role == 'system':
                return i + 1
        return 0  # No system message found, insert at start
