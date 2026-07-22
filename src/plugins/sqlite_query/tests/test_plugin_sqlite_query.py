"""
Tests for sqlite_query plugin - simple SQL execution for debugging.
"""
import pytest
from pathlib import Path
import tempfile
import sqlite3
from src.plugins.sqlite_query.server import SqliteQueryServer
from unittest.mock import AsyncMock, MagicMock
from agent_system.config import AgentSystemConfig, MCPConfig


@pytest.fixture
def plugin(test_db):
    """Create plugin instance with test database."""
    # Mock configs
    system_config = MagicMock(spec=AgentSystemConfig)
    mcp_config = MagicMock(spec=MCPConfig)
    mcp_config.database = test_db
    mcp_config.query_timeout = 30
    
    return SqliteQueryServer(name="test_sqlite", system_config=system_config, mcp_config=mcp_config)


@pytest.fixture
def mock_status():
    """Create mock status object."""
    status = AsyncMock()
    status.progress = AsyncMock()
    status.end = AsyncMock()
    status.error = AsyncMock()
    return status


@pytest.fixture
def test_db():
    """Create a temporary test database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        
        # Create test database with sample data
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'draft'
            )
        """)
        
        cursor.execute("INSERT INTO books (title, status) VALUES ('Book 1', 'draft')")
        cursor.execute("INSERT INTO books (title, status) VALUES ('Book 2', 'completed')")
        cursor.execute("INSERT INTO books (title, status) VALUES ('Book 3', 'draft')")
        
        conn.commit()
        conn.close()
        
        yield str(db_path)


@pytest.mark.asyncio
async def test_select_query(plugin, test_db, mock_status):
    """Test simple SELECT query."""
    result = await plugin.execute_sql({
        "sql": "SELECT * FROM books",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 3
    assert "rows" in result
    assert len(result["rows"]) == 3
    assert result["columns"] == ["id", "title", "status"]
    
    # Verify status calls
    mock_status.progress.assert_called()
    mock_status.end.assert_called_once()


@pytest.mark.asyncio
async def test_select_with_where(plugin, test_db, mock_status):
    """Test SELECT with WHERE clause."""
    result = await plugin.execute_sql({
        "sql": "SELECT * FROM books WHERE status = 'draft'",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 2
    assert all(row["status"] == "draft" for row in result["rows"])


@pytest.mark.asyncio
async def test_parameterized_query(plugin, test_db, mock_status):
    """Test query with embedded values (LLM generates complete SQL)."""
    result = await plugin.execute_sql({
        "sql": "SELECT * FROM books WHERE status = 'completed'",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 1
    assert result["rows"][0]["title"] == "Book 2"


@pytest.mark.asyncio
async def test_update_query(plugin, test_db, mock_status):
    """Test UPDATE statement."""
    result = await plugin.execute_sql({
        "sql": "UPDATE books SET status = 'completed' WHERE id = 1",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["rows_affected"] == 1
    
    # Verify update
    verify_result = await plugin.execute_sql({
        "sql": "SELECT status FROM books WHERE id = 1",
        "_status": mock_status
    })
    assert verify_result["rows"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_insert_query(plugin, test_db, mock_status):
    """Test INSERT statement with embedded values."""
    result = await plugin.execute_sql({
        "sql": "INSERT INTO books (title, status) VALUES ('Book 4', 'draft')",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["rows_affected"] == 1
    assert result["last_row_id"] == 4


@pytest.mark.asyncio
async def test_delete_query(plugin, test_db, mock_status):
    """Test DELETE statement."""
    result = await plugin.execute_sql({
        "sql": "DELETE FROM books WHERE id = 1",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["rows_affected"] == 1


@pytest.mark.asyncio
async def test_pragma_query(plugin, test_db, mock_status):
    """Test PRAGMA statement (schema inspection)."""
    result = await plugin.execute_sql({
        "sql": "PRAGMA table_info(books)",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 3  # id, title, status
    assert any(row["name"] == "title" for row in result["rows"])


@pytest.mark.asyncio
async def test_join_query(plugin, test_db, mock_status):
    """Test complex JOIN query."""
    # Add another table
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, book_id INTEGER)")
    cursor.execute("INSERT INTO authors (name, book_id) VALUES ('Author 1', 1)")
    cursor.execute("INSERT INTO authors (name, book_id) VALUES ('Author 2', 2)")
    conn.commit()
    conn.close()
    
    result = await plugin.execute_sql({
        "sql": """
            SELECT b.title, a.name 
            FROM books b 
            JOIN authors a ON a.book_id = b.id
        """,
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 2


@pytest.mark.asyncio
async def test_database_not_found(mock_status):
    """Test error handling for missing database."""
    system_config = MagicMock(spec=AgentSystemConfig)
    mcp_config = MagicMock(spec=MCPConfig)
    mcp_config.database = "/nonexistent/database.db"
    mcp_config.query_timeout = 30
    
    plugin = SqliteQueryServer(name="test_sqlite", system_config=system_config, mcp_config=mcp_config)
    result = await plugin.execute_sql({
        "sql": "SELECT * FROM books",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_invalid_sql(plugin, test_db, mock_status):
    """Test error handling for invalid SQL."""
    result = await plugin.execute_sql({
        "sql": "SELECT * FROM nonexistent_table",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "no such table" in result["error"].lower()


@pytest.mark.asyncio
async def test_missing_sql_param(plugin, test_db, mock_status):
    """Test error when sql parameter missing."""
    result = await plugin.execute_sql({
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "sql" in result["error"]


@pytest.mark.asyncio
async def test_aggregate_query(plugin, test_db, mock_status):
    """Test aggregate functions."""
    result = await plugin.execute_sql({
        "sql": "SELECT status, COUNT(*) as count FROM books GROUP BY status",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 2  # draft and completed
    assert result["columns"] == ["status", "count"]


@pytest.mark.asyncio
async def test_multiple_parameters(plugin, test_db, mock_status):
    """Test query with multiple conditions (LLM generates complete SQL)."""
    result = await plugin.execute_sql({
        "sql": "SELECT * FROM books WHERE status = 'draft' OR id = 2",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["row_count"] == 3  # 2 drafts + book 2
