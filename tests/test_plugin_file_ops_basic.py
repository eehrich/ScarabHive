"""Basic tests for file_ops plugin."""

from __future__ import annotations

import gc
import pytest
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, MCPConfig
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
def tmp_allowed_dir(tmp_path):
    """Create a temporary allowed directory."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    return allowed_dir


@pytest.fixture
async def file_ops_server(tmp_allowed_dir):
    """Create file operations server with temp directory."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_allowed_dir.parent)
    
    mcp_config = MCPConfig(type="file_ops", enabled=True)
    mcp_config.allowed_directories = [str(tmp_allowed_dir)]
    mcp_config.search = {
        "enable_indexing": False  # Disable for faster tests
    }
    
    server = FileOpsServer("file_ops", system_config, mcp_config)
    yield server
    
    # Cleanup: stop background indexing and close resources
    await server.search_engine.stop()
    # Force garbage collection to close any file handles
    gc.collect()


@pytest.mark.asyncio
async def test_create_and_read_file(file_ops_server, tmp_allowed_dir):
    """Test creating and reading a file."""
    # Create file
    result = await file_ops_server.create_file({
        "file_path": str(tmp_allowed_dir / "test.txt"),
        "content": "Hello World\nLine 2\nLine 3"
    })
    
    assert result["status"] == "success"
    assert "file_path" in result
    
    # Read file
    result = await file_ops_server.read_file({
        "file_path": str(tmp_allowed_dir / "test.txt")
    })
    
    assert result["status"] == "success"
    assert result["content"] == "Hello World\nLine 2\nLine 3"
    assert result["total_lines"] == 3


@pytest.mark.asyncio
async def test_read_file_with_pagination(file_ops_server, tmp_allowed_dir):
    """Test reading file with offset and limit."""
    # Create test file
    test_file = tmp_allowed_dir / "paginated.txt"
    test_file.write_text("\n".join([f"Line {i}" for i in range(100)]))
    
    # Read with offset and limit
    result = await file_ops_server.read_file({
        "file_path": str(test_file),
        "offset": 10,
        "limit": 5
    })
    
    assert result["status"] == "success"
    assert result["total_lines"] == 100
    assert result["lines_read"] == 5
    assert result["offset"] == 10
    assert "Line 10" in result["content"]
    assert "Line 14" in result["content"]


@pytest.mark.asyncio
async def test_edit_file_append(file_ops_server, tmp_allowed_dir):
    """Test appending to a file."""
    # Create test file
    test_file = tmp_allowed_dir / "append.txt"
    test_file.write_text("Line 1")
    
    # Append content
    result = await file_ops_server.edit_file({
        "file_path": str(test_file),
        "mode": "append",
        "content": "\nLine 2"
    })
    
    assert result["status"] == "success"
    assert result["mode"] == "append"
    
    # Verify content
    assert test_file.read_text() == "Line 1\nLine 2"


@pytest.mark.asyncio
async def test_edit_file_replace(file_ops_server, tmp_allowed_dir):
    """Test replacing string in file."""
    # Create test file
    test_file = tmp_allowed_dir / "replace.txt"
    test_file.write_text("Hello World\nHello Python")
    
    # Replace content
    result = await file_ops_server.edit_file({
        "file_path": str(test_file),
        "mode": "replace",
        "old_string": "Hello",
        "new_string": "Hi"
    })
    
    assert result["status"] == "success"
    assert result["changes"]["replacements"] == 2
    
    # Verify content
    assert test_file.read_text() == "Hi World\nHi Python"


@pytest.mark.asyncio
async def test_edit_file_insert(file_ops_server, tmp_allowed_dir):
    """Test inserting line into file."""
    # Create test file
    test_file = tmp_allowed_dir / "insert.txt"
    test_file.write_text("Line 1\nLine 3")
    
    # Insert line
    result = await file_ops_server.edit_file({
        "file_path": str(test_file),
        "mode": "insert",
        "line_number": 1,
        "content": "Line 2"
    })
    
    assert result["status"] == "success"
    assert result["changes"]["inserted_at_line"] == 1
    
    # Verify content
    content = test_file.read_text()
    lines = content.splitlines()
    assert len(lines) == 3
    assert lines[1] == "Line 2"


@pytest.mark.asyncio
async def test_delete_file(file_ops_server, tmp_allowed_dir):
    """Test deleting a file."""
    # Create test file
    test_file = tmp_allowed_dir / "delete.txt"
    test_file.write_text("Delete me")
    
    # Delete file
    result = await file_ops_server.delete_file({
        "file_path": str(test_file),
        "confirm": True
    })
    
    assert result["status"] == "success"
    assert not test_file.exists()


@pytest.mark.asyncio
async def test_delete_file_without_confirmation(file_ops_server, tmp_allowed_dir):
    """Test that deletion requires confirmation."""
    test_file = tmp_allowed_dir / "protected.txt"
    test_file.write_text("Protected")
    
    result = await file_ops_server.delete_file({
        "file_path": str(test_file),
        "confirm": False
    })
    
    assert result["status"] == "error"
    assert "confirm" in result["error"].lower()
    assert test_file.exists()


@pytest.mark.asyncio
async def test_list_directory(file_ops_server, tmp_allowed_dir):
    """Test listing directory contents."""
    # Create test files
    (tmp_allowed_dir / "file1.txt").write_text("1")
    (tmp_allowed_dir / "file2.py").write_text("2")
    (tmp_allowed_dir / "subdir").mkdir()
    
    # List directory
    result = await file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir)
    })
    
    assert result["status"] == "success"
    assert result["total_files"] == 2
    assert result["total_directories"] == 1


@pytest.mark.asyncio
async def test_list_directory_with_pattern(file_ops_server, tmp_allowed_dir):
    """Test listing directory with pattern filter."""
    # Create test files
    (tmp_allowed_dir / "test1.py").write_text("1")
    (tmp_allowed_dir / "test2.py").write_text("2")
    (tmp_allowed_dir / "other.txt").write_text("3")
    
    # List with pattern
    result = await file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir),
        "pattern": "*.py"
    })
    
    assert result["status"] == "success"
    assert result["total_files"] == 2
    assert all(f.endswith(".py") for f in result["files"])


@pytest.mark.asyncio
async def test_path_traversal_blocked(file_ops_server, tmp_allowed_dir):
    """Test that path traversal attempts are blocked."""
    # Try to access parent directory
    result = await file_ops_server.read_file({
        "file_path": str(tmp_allowed_dir / ".." / ".." / "etc" / "passwd")
    })
    
    assert result["status"] == "error"
    assert result["error_type"] == "SecurityError"


@pytest.mark.asyncio
async def test_outside_allowed_dir_blocked(file_ops_server, tmp_path):
    """Test that access outside allowed directories is blocked."""
    # Try to access file outside allowed dir
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("Outside")
    
    result = await file_ops_server.read_file({
        "file_path": str(outside_file)
    })
    
    assert result["status"] == "error"
    assert result["error_type"] == "SecurityError"
    assert "outside allowed directories" in result["error"].lower()


@pytest.mark.asyncio
async def test_create_file_with_overwrite(file_ops_server, tmp_allowed_dir):
    """Test creating file with overwrite flag."""
    test_file = tmp_allowed_dir / "overwrite.txt"
    test_file.write_text("Original")
    
    # Try without overwrite (should fail)
    result = await file_ops_server.create_file({
        "file_path": str(test_file),
        "content": "New content",
        "overwrite": False
    })
    
    assert result["status"] == "error"
    assert "already exists" in result["error"].lower()
    
    # Try with overwrite (should succeed)
    result = await file_ops_server.create_file({
        "file_path": str(test_file),
        "content": "New content",
        "overwrite": True
    })
    
    assert result["status"] == "success"
    assert test_file.read_text() == "New content"


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_search_files_basic(file_ops_server, tmp_allowed_dir):
    """Test basic file search."""
    # Create test files
    (tmp_allowed_dir / "test1.py").write_text("1")
    (tmp_allowed_dir / "test2.py").write_text("2")
    (tmp_allowed_dir / "other.txt").write_text("3")
    
    # Enable indexing and build index
    file_ops_server.search_engine.config["enable_indexing"] = True
    await file_ops_server.search_engine.rebuild_index()
    
    # Search for Python files
    result = await file_ops_server.search_files({
        "pattern": "*.py"
    })
    
    assert result["status"] == "success"
    assert result["total_found"] >= 2


@pytest.mark.asyncio
async def test_grep_search_basic(file_ops_server, tmp_allowed_dir):
    """Test basic text search."""
    # Create test files with searchable content
    (tmp_allowed_dir / "file1.txt").write_text("Hello World\nPython rocks")
    (tmp_allowed_dir / "file2.txt").write_text("Goodbye World\nJava rules")
    
    # Enable indexing and build index
    file_ops_server.search_engine.config["enable_indexing"] = True
    await file_ops_server.search_engine.rebuild_index()
    
    # Search for "World"
    result = await file_ops_server.grep_search({
        "query": "World",
        "case_sensitive": False
    })
    
    assert result["status"] == "success"
    assert result["total_matches"] >= 2
    assert any("Hello World" in m["line_content"] for m in result["matches"])


@pytest.mark.asyncio
async def test_edit_file_replace_not_found(file_ops_server, tmp_allowed_dir):
    """Test replace mode with non-existent string."""
    test_file = tmp_allowed_dir / "not_found.txt"
    test_file.write_text("Hello World")
    
    result = await file_ops_server.edit_file({
        "file_path": str(test_file),
        "mode": "replace",
        "old_string": "Python",
        "new_string": "Java"
    })
    
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_semantic_search_basic(file_ops_server, tmp_allowed_dir):
    """Test semantic search with ChromaDB."""
    # Create test files with semantic content
    (tmp_allowed_dir / "auth.py").write_text("""
def verify_credentials(username, password):
    \"\"\"Check if user credentials are valid.\"\"\"
    return authenticate_user(username, password)

def login(username, password):
    \"\"\"User login function.\"\"\"
    if verify_credentials(username, password):
        create_session(username)
        return True
    return False
""")
    
    (tmp_allowed_dir / "database.py").write_text("""
def connect_database():
    \"\"\"Establish database connection.\"\"\"
    return DatabasePool.get_connection()

def execute_query(sql):
    \"\"\"Run SQL query on database.\"\"\"
    conn = connect_database()
    return conn.execute(sql)
""")
    
    (tmp_allowed_dir / "utils.py").write_text("""
def format_string(text):
    \"\"\"Format text string.\"\"\"
    return text.strip().lower()
""")
    
    # Enable semantic search and build index
    file_ops_server.search_engine.config["enable_semantic_search"] = True
    file_ops_server.search_engine.config["enable_indexing"] = True
    
    try:
        await file_ops_server.search_engine.rebuild_index()
    except Exception as e:
        # If ChromaDB not available, skip test
        pytest.skip(f"ChromaDB not available: {e}")
    
    # Semantic search for authentication logic
    result = await file_ops_server.semantic_search({
        "query": "authentication and login logic",
        "max_results": 5
    })
    
    assert result["status"] == "success"
    assert result["count"] >= 1
    
    # Should find auth.py (most relevant)
    file_paths = [r["file_path"] for r in result["results"]]
    assert any("auth.py" in path for path in file_paths)
    
    # Check similarity scores are reasonable
    for match in result["results"]:
        assert 0.0 <= match["similarity_score"] <= 1.0
        assert "file_path" in match
        assert "filename" in match
    
    # Semantic search for database code
    result = await file_ops_server.semantic_search({
        "query": "database connection and queries",
        "max_results": 5
    })
    
    assert result["status"] == "success"
    file_paths = [r["file_path"] for r in result["results"]]
    assert any("database.py" in path for path in file_paths)
    
    # Test with filter pattern
    result = await file_ops_server.semantic_search({
        "query": "authentication",
        "max_results": 5,
        "filter_pattern": "*.py"
    })
    
    assert result["status"] == "success"
    for match in result["results"]:
        assert match["file_path"].endswith(".py")

