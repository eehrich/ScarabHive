"""
Memory Management Plugin - Comprehensive Test Suite

Tests cover:
- Memory lifecycle (store, recall, search, list, delete)
- ChromaDB integration (semantic search)
- Persistence (JSON metadata + ChromaDB vectors)
- Access tracking (access_count, accessed_at)
- Hook integration (system prompt injection)
- Multi-operation tool dispatch
- Edge cases and error handling

Target: >80% code coverage
"""

import pytest
from pathlib import Path
from typing import Dict, Any
from unittest.mock import MagicMock, Mock
import json

from plugins.memory.server import (
    MemoryServer,
    Memory,
    MemoryCollection,
    MemoryError,
    ValidationError,
    StorageError,
    ChromaDBError,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory for tests"""
    storage = tmp_path / "memories"
    storage.mkdir()
    return storage


@pytest.fixture
def mock_system_config() -> MagicMock:
    """Mock AgentSystemConfig for testing"""
    return MagicMock()


@pytest.fixture
def mock_mcp_config(temp_storage: Path) -> MagicMock:
    """Mock MCPConfig with Memory plugin settings"""
    config = MagicMock()
    config.storage_path = str(temp_storage)
    config.max_memories = 10
    config.max_memories_per_session = 1000
    config.auto_extract_keywords = True
    config.search_n_results = 5
    config.use_semantic_injection = True
    return config


@pytest.fixture
def server(mock_system_config: MagicMock, mock_mcp_config: MagicMock) -> MemoryServer:
    """MemoryServer instance"""
    return MemoryServer(
        name="memory",
        system_config=mock_system_config,
        mcp_config=mock_mcp_config,
    )


@pytest.fixture
def mock_context() -> Dict[str, Any]:
    """Mock MCP tool call context"""
    return {
        "session_id": "test_session_001",
        "agent_name": "test_agent",
    }


# =============================================================================
# Test: Memory Creation
# =============================================================================

@pytest.mark.asyncio
async def test_store_memory_basic(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test basic memory storage"""
    result = await server._operation_store(
        session_id=mock_context["session_id"],
        title="First Memory",
        content="This is the content of my first memory",
        importance=7,
        tags=["test", "important"],
        agent_name=mock_context["agent_name"],
    )
    
    assert "memory_id" in result
    assert result["title"] == "First Memory"
    assert "memory stored successfully" in result["message"].lower()
    assert result["memory_id"].startswith("mem_")


@pytest.mark.asyncio
async def test_store_memory_auto_keywords(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test automatic keyword extraction"""
    result = await server._operation_store(
        session_id=mock_context["session_id"],
        title="Python Programming",
        content="Python is a great programming language for data science and machine learning",
        agent_name=mock_context["agent_name"],
    )
    
    assert "keywords" in result
    assert len(result["keywords"]) > 0
    # Keywords should include high-frequency words
    keywords_str = " ".join(result["keywords"]).lower()
    assert "python" in keywords_str or "programming" in keywords_str


@pytest.mark.asyncio
async def test_store_memory_custom_keywords(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test custom keywords override"""
    result = await server._operation_store(
        session_id=mock_context["session_id"],
        title="Test Memory",
        content="Some content",
        keywords=["custom", "keywords", "here"],
        agent_name=mock_context["agent_name"],
    )
    
    assert result["keywords"] == ["custom", "keywords", "here"]


# =============================================================================
# Test: Memory Recall
# =============================================================================

@pytest.mark.asyncio
async def test_recall_memory(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test memory recall updates access count"""
    # Store memory
    stored = await server._operation_store(
        session_id=mock_context["session_id"],
        title="Test Memory",
        content="Test content",
        agent_name=mock_context["agent_name"],
    )
    memory_id = stored["memory_id"]
    
    # Recall once
    result1 = await server._operation_recall(
        session_id=mock_context["session_id"],
        memory_id=memory_id,
    )
    
    assert result1["memory_id"] == memory_id
    assert result1["title"] == "Test Memory"
    assert result1["content"] == "Test content"
    assert result1["access_count"] == 1
    
    # Recall again
    result2 = await server._operation_recall(
        session_id=mock_context["session_id"],
        memory_id=memory_id,
    )
    
    assert result2["access_count"] == 2


@pytest.mark.asyncio
async def test_recall_nonexistent(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test recall of non-existent memory"""
    with pytest.raises(ValidationError) as exc_info:
        await server._operation_recall(
            session_id=mock_context["session_id"],
            memory_id="nonexistent_memory",
        )
    
    assert "not found" in str(exc_info.value).lower()


# =============================================================================
# Test: Semantic Search
# =============================================================================

@pytest.mark.asyncio
async def test_search_semantic(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test ChromaDB semantic search"""
    # Store related memories
    await server._operation_store(
        session_id=mock_context["session_id"],
        title="Python Tutorial",
        content="Learn Python programming with examples",
        agent_name=mock_context["agent_name"],
    )
    
    await server._operation_store(
        session_id=mock_context["session_id"],
        title="Java Guide",
        content="Java is an object-oriented programming language",
        agent_name=mock_context["agent_name"],
    )
    
    await server._operation_store(
        session_id=mock_context["session_id"],
        title="Cooking Recipe",
        content="How to bake chocolate chip cookies",
        agent_name=mock_context["agent_name"],
    )
    
    # Search for programming-related content
    result = await server._operation_search(
        session_id=mock_context["session_id"],
        query="programming languages",
        n_results=2,
    )
    
    assert result["count"] > 0
    assert len(result["results"]) <= 2
    
    # Results should have similarity scores (distance)
    for mem in result["results"]:
        assert "distance" in mem
        assert "title" in mem
        assert "content" in mem


@pytest.mark.asyncio
async def test_search_no_results(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test search with no matches"""
    result = await server._operation_search(
        session_id=mock_context["session_id"],
        query="nonexistent topic",
        n_results=5,
    )
    
    assert result["count"] == 0
    assert result["results"] == []
    assert "no memories found" in result["message"].lower()


# =============================================================================
# Test: List Memories
# =============================================================================

@pytest.mark.asyncio
async def test_list_memories_all(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test list all memories"""
    # Store multiple memories
    for i in range(5):
        await server._operation_store(
            session_id=mock_context["session_id"],
            title=f"Memory {i}",
            content=f"Content {i}",
            importance=i + 1,
            agent_name=mock_context["agent_name"],
        )
    
    result = await server._operation_list(
        session_id=mock_context["session_id"],
    )
    
    assert result["total"] == 5
    assert result["count"] == 5
    assert len(result["memories"]) == 5


@pytest.mark.asyncio
async def test_list_memories_sorted_by_importance(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test list sorted by importance"""
    # Store with different importance
    await server._operation_store(
        session_id=mock_context["session_id"],
        title="Low Importance",
        content="Content",
        importance=3,
        agent_name=mock_context["agent_name"],
    )
    
    await server._operation_store(
        session_id=mock_context["session_id"],
        title="High Importance",
        content="Content",
        importance=9,
        agent_name=mock_context["agent_name"],
    )
    
    # Sort by importance descending
    result = await server._operation_list(
        session_id=mock_context["session_id"],
        sort_by="importance",
        sort_order="desc",
    )
    
    assert result["memories"][0]["importance"] == 9
    assert result["memories"][1]["importance"] == 3


@pytest.mark.asyncio
async def test_list_memories_pagination(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test pagination"""
    # Create 10 memories
    for i in range(10):
        await server._operation_store(
            session_id=mock_context["session_id"],
            title=f"Memory {i}",
            content="Content",
            agent_name=mock_context["agent_name"],
        )
    
    # Get first page (limit 5)
    result1 = await server._operation_list(
        session_id=mock_context["session_id"],
        limit=5,
        offset=0,
    )
    
    assert result1["total"] == 10
    assert result1["count"] == 5
    assert result1["offset"] == 0
    
    # Get second page
    result2 = await server._operation_list(
        session_id=mock_context["session_id"],
        limit=5,
        offset=5,
    )
    
    assert result2["count"] == 5
    assert result2["offset"] == 5


# =============================================================================
# Test: Delete Memory
# =============================================================================

@pytest.mark.asyncio
async def test_delete_memory(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test memory deletion"""
    # Store memory
    stored = await server._operation_store(
        session_id=mock_context["session_id"],
        title="To Delete",
        content="Content",
        agent_name=mock_context["agent_name"],
    )
    memory_id = stored["memory_id"]
    
    # Delete
    result = await server._operation_delete(
        session_id=mock_context["session_id"],
        memory_id=memory_id,
    )
    
    assert result["deleted"] is True
    assert result["memory_id"] == memory_id
    
    # Verify deleted
    with pytest.raises(ValidationError):
        await server._operation_recall(
            session_id=mock_context["session_id"],
            memory_id=memory_id,
        )


@pytest.mark.asyncio
async def test_delete_nonexistent(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test delete non-existent memory"""
    with pytest.raises(ValidationError) as exc_info:
        await server._operation_delete(
            session_id=mock_context["session_id"],
            memory_id="nonexistent",
        )
    
    assert "not found" in str(exc_info.value).lower()


# =============================================================================
# Test: Multi-Operation Tool
# =============================================================================

@pytest.mark.asyncio
async def test_call_tool_store(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test tool call with store operation"""
    result = await server.call_tool(
        tool_name="memory",
        arguments={
            "operation": "store",
            "session_id": mock_context["session_id"],
            "title": "Tool Test",
            "content": "Content",
            "importance": 5,
        }
    )
    
    assert "memory_id" in result
    assert result["title"] == "Tool Test"


@pytest.mark.asyncio
async def test_call_tool_recall(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test tool call with recall operation"""
    # Store first
    stored = await server.call_tool(
        "memory",
        {
            "operation": "store",
            "session_id": mock_context["session_id"],
            "title": "Test",
            "content": "Content",
        }
    )
    memory_id = stored["memory_id"]
    
    # Recall
    result = await server.call_tool(
        "memory",
        {
            "operation": "recall",
            "session_id": mock_context["session_id"],
            "memory_id": memory_id,
        }
    )
    
    assert result["memory_id"] == memory_id


@pytest.mark.asyncio
async def test_call_tool_search(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test tool call with search operation"""
    await server.call_tool(
        "memory",
        {
            "operation": "store",
            "session_id": mock_context["session_id"],
            "title": "Python",
            "content": "Python programming",
        }
    )
    
    result = await server.call_tool(
        "memory",
        {
            "operation": "search",
            "session_id": mock_context["session_id"],
            "query": "programming",
        }
    )
    
    assert "results" in result


@pytest.mark.asyncio
async def test_call_tool_invalid_operation(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test tool call with invalid operation"""
    result = await server.call_tool(
        "memory",
        {
            "operation": "invalid_op",
            "session_id": mock_context["session_id"],
        }
    )
    
    assert result["error"] is True
    assert "unknown operation" in result["message"].lower()


@pytest.mark.asyncio
async def test_call_tool_missing_operation(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test tool call without operation"""
    result = await server.call_tool(
        "memory",
        {"session_id": mock_context["session_id"]}
    )
    
    assert result["error"] is True
    assert "missing" in result["message"].lower()


# =============================================================================
# Test: Persistence
# =============================================================================

@pytest.mark.asyncio
async def test_persistence_save_load(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test memories persist to JSON file"""
    await server._operation_store(
        session_id=mock_context["session_id"],
        title="Persisted Memory",
        content="Content",
        agent_name=mock_context["agent_name"],
    )
    
    # Check file exists
    session_id = mock_context["session_id"]
    file_path = server._get_metadata_path(session_id)
    assert file_path.exists()
    
    # Load and verify
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    assert data["session_id"] == session_id
    assert len(data["memories"]) == 1


@pytest.mark.asyncio
async def test_persistence_reload(
    temp_storage: Path,
    mock_system_config: MagicMock,
    mock_mcp_config: MagicMock,
    mock_context: Dict[str, Any],
):
    """Test session reloads from disk"""
    # Create memories with first server instance
    server1 = MemoryServer(
        name="memory",
        system_config=mock_system_config,
        mcp_config=mock_mcp_config,
    )
    await server1._operation_store(
        session_id=mock_context["session_id"],
        title="Memory 1",
        content="Content 1",
    )
    await server1._operation_store(
        session_id=mock_context["session_id"],
        title="Memory 2",
        content="Content 2",
    )
    
    # Create new server instance (simulates restart)
    server2 = MemoryServer(
        name="memory",
        system_config=mock_system_config,
        mcp_config=mock_mcp_config,
    )
    
    # Load memories
    result = await server2._operation_list(
        session_id=mock_context["session_id"],
    )
    
    assert result["total"] == 2


# =============================================================================
# Test: Edge Cases
# =============================================================================

@pytest.mark.asyncio
async def test_empty_session(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test operations on empty session"""
    result = await server._operation_list(
        session_id=mock_context["session_id"],
    )
    
    assert result["total"] == 0
    assert result["memories"] == []


@pytest.mark.asyncio
async def test_session_isolation(server: MemoryServer):
    """Test memories are isolated per session"""
    await server._operation_store(
        session_id="session_1",
        title="Session 1 Memory",
        content="Content",
    )
    
    await server._operation_store(
        session_id="session_2",
        title="Session 2 Memory",
        content="Content",
    )
    
    # Verify isolation
    result1 = await server._operation_list(session_id="session_1")
    result2 = await server._operation_list(session_id="session_2")
    
    assert result1["total"] == 1
    assert result2["total"] == 1
    assert result1["memories"][0]["title"] != result2["memories"][0]["title"]


@pytest.mark.asyncio
async def test_unicode_handling(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test Unicode in memory content"""
    result = await server._operation_store(
        session_id=mock_context["session_id"],
        title="Unicode Test: äöü ß 🚀",
        content="Emoji test: 🎉 ✅ 📝\n日本語テスト",
        tags=["日本語", "中文"],
    )
    
    assert "äöü" in result["title"]
    
    # Recall to verify content
    recalled = await server._operation_recall(
        session_id=mock_context["session_id"],
        memory_id=result["memory_id"],
    )
    
    assert "🎉" in recalled["content"]
    assert "日本語" in recalled["tags"]


@pytest.mark.asyncio
async def test_long_content(server: MemoryServer, mock_context: Dict[str, Any]):
    """Test long content storage"""
    long_content = "x" * 4000
    
    result = await server._operation_store(
        session_id=mock_context["session_id"],
        title="Long Content",
        content=long_content,
    )
    
    # Should truncate to max length (5000)
    recalled = await server._operation_recall(
        session_id=mock_context["session_id"],
        memory_id=result["memory_id"],
    )
    
    assert len(recalled["content"]) <= 5000


# =============================================================================
# Test: Data Models
# =============================================================================

def test_memory_model_defaults():
    """Test Memory model default values"""
    memory = Memory(
        memory_id="test_001",
        title="Test",
        content="Content",
        session_id="session",
    )
    
    assert memory.keywords == []
    assert memory.importance == 5
    assert memory.tags == []
    assert memory.access_count == 0


def test_memory_collection_model():
    """Test MemoryCollection model"""
    collection = MemoryCollection(session_id="test")
    
    assert collection.memories == {}
    assert collection.total_memories == 0


def test_exception_hierarchy():
    """Test exception classes"""
    base = MemoryError("base")
    assert isinstance(base, Exception)
    
    val = ValidationError("validation")
    assert isinstance(val, MemoryError)
    
    store = StorageError("storage")
    assert isinstance(store, MemoryError)
    
    chroma = ChromaDBError("chromadb")
    assert isinstance(chroma, MemoryError)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=plugins.memory.server", "--cov-report=html"])
