"""Basic tests for file_ops plugin."""

from __future__ import annotations

import pathlib

import gc
import pytest
from unittest.mock import Mock

from agent_system.config import AgentSystemConfig, ToolServerConfig
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

    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tmp_allowed_dir)]
    server_config.search = {
        "enable_indexing": False,  # Disable for faster tests
        # A tmp store, never data/cache: the tree is a fresh tmp directory on
        # every run, so a shared store carries the vectors AND the index state
        # of trees that no longer exist -- and the test that switches semantic
        # search on then searches somebody else's leftovers.
        "chroma_db_path": str(tmp_allowed_dir.parent / "vector_store"),
    }

    server = FileOpsServer("file_ops", system_config, server_config)
    yield server

    # Cleanup: stop background indexing and close resources
    await server.search_engine.stop()
    # Force garbage collection to close any file handles
    gc.collect()


@pytest.mark.asyncio
async def test_create_and_read_file(file_ops_server, tmp_allowed_dir):
    """Test creating and reading a file."""
    # Create file using manage
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(tmp_allowed_dir / "test.txt"),
        "content": "Hello World\nLine 2\nLine 3"
    })

    assert result["status"] == "success"
    assert "file_path" in result

    # Read file
    result = await file_ops_server.read_file({
        "filePath": str(tmp_allowed_dir / "test.txt")
    })

    assert result["status"] == "success"
    # Normalize line endings for platform independence
    content = result["content"].replace('\r\n', '\n')
    assert content == "Hello World\nLine 2\nLine 3"
    assert result["total_lines"] == 3


@pytest.mark.asyncio
async def test_read_file_with_pagination(file_ops_server, tmp_allowed_dir):
    """Test reading file with offset and limit (offset is 1-indexed)."""
    # Create test file with lines 0-99
    test_file = tmp_allowed_dir / "paginated.txt"
    test_file.write_text("\n".join([f"Line {i}" for i in range(100)]))

    # Read with offset=11 and limit=5 (1-indexed, so reads lines 10-14 in 0-indexed terms)
    result = await file_ops_server.read_file({
        "filePath": str(test_file),
        "offset": 11,  # 1-indexed: line 11 = 0-indexed line 10
        "limit": 5
    })

    assert result["status"] == "success"
    assert result["total_lines"] == 100
    assert result["lines_read"] == 5
    assert result["offset"] == 10  # Internal 0-indexed offset
    assert "Line 10" in result["content"]  # Lines 10-14
    assert "Line 14" in result["content"]



@pytest.mark.asyncio
async def test_replace_string_in_file(file_ops_server, tmp_allowed_dir):
    """Test replacing string in file (Copilot-style tool)."""
    # Create test file
    test_file = tmp_allowed_dir / "replace.txt"
    test_file.write_text("Hello World\nHello Python")

    # Replace content using replace_string_in_file
    result = await file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Hello",
        "newString": "Hi"
    })

    # "Hello" occurs twice. The tool promises ONE occurrence, so an
    # ambiguous oldString is refused instead of editing both places.
    assert result["status"] == "error"
    assert result["error_type"] == "AmbiguousMatchError"
    assert result["occurrences"] == 2
    assert test_file.read_text() == "Hello World\nHello Python"

    result = await file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Hello World",
        "newString": "Hi World"
    })
    assert result["status"] == "success"
    assert result["changes"]["replacements"] == 1
    assert test_file.read_text() == "Hi World\nHello Python"


@pytest.mark.asyncio
async def test_replace_keeps_crlf_line_endings(file_ops_server, tmp_allowed_dir):
    """An edit used to rewrite every CRLF of the file as LF."""
    test_file = tmp_allowed_dir / "crlf.txt"
    test_file.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = await file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "two\nthree",
        "newString": "TWO\nTHREE"
    })

    assert result["status"] == "success"
    assert test_file.read_bytes() == b"one\r\nTWO\r\nTHREE\r\n"


@pytest.mark.asyncio
async def test_list_directory_is_capped_and_says_so(file_ops_server, tmp_allowed_dir):
    for i in range(5):
        (tmp_allowed_dir / f"f{i}.txt").write_text("x")

    capped = await file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir), "max_results": 3})
    assert capped["total_files"] == 3
    assert capped["truncated"] is True

    exact = await file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir), "max_results": 5})
    assert exact["total_files"] == 5
    assert exact["truncated"] is False


@pytest.mark.asyncio
async def test_recursive_listing_prunes_excluded_trees(file_ops_server, tmp_allowed_dir):
    (tmp_allowed_dir / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_allowed_dir / "node_modules" / "pkg" / "index.js").write_text("x")
    (tmp_allowed_dir / "src").mkdir()
    (tmp_allowed_dir / "src" / "main.py").write_text("x")

    result = await file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir), "recursive": True})
    names = {pathlib.Path(f).name for f in result["files"]}
    assert names == {"main.py"}
    assert not any("node_modules" in d for d in result["directories"])

    complete = await file_ops_server.list_directory({
        "dir_path": str(tmp_allowed_dir), "recursive": True, "include_ignored": True})
    assert "index.js" in {pathlib.Path(f).name for f in complete["files"]}




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
        "filePath": str(tmp_allowed_dir / ".." / ".." / "etc" / "passwd")
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
        "filePath": str(outside_file)
    })

    assert result["status"] == "error"
    assert result["error_type"] == "SecurityError"
    # Semantic instead of literal: the message must tell the model WHICH path
    # was refused and what is allowed instead, otherwise it guesses. Asserting
    # on the exact wording only turned this test red when the boundary was
    # unified — it never found a defect.
    assert str(outside_file) in result["error"]
    assert any(str(d) in result["error"]
               for d in file_ops_server.validator.allowed_dirs)


@pytest.mark.asyncio
async def test_create_file_fails_if_exists(file_ops_server, tmp_allowed_dir):
    """Test that creating a file fails if it already exists (no overwrite support)."""
    test_file = tmp_allowed_dir / "exists.txt"
    test_file.write_text("Original")

    # Try to create file that already exists (should fail)
    result = await file_ops_server.manage({
        "operation": "create",
        "path": str(test_file),
        "content": "New content"
    })

    assert result["status"] == "error"
    assert "already exists" in result["error"].lower()
    # Original content should be preserved
    assert test_file.read_text() == "Original"


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
async def test_replace_string_not_found(file_ops_server, tmp_allowed_dir):
    """Test replace_string_in_file with non-existent string."""
    test_file = tmp_allowed_dir / "not_found.txt"
    test_file.write_text("Hello World")

    result = await file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Python",
        "newString": "Java"
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



@pytest.mark.asyncio
async def test_replace_string_crlf_detection(file_ops_server, tmp_allowed_dir):
    """Test replace_string_in_file accepts CRLF/LF flexibility."""
    test_file = tmp_allowed_dir / "crlf_test.txt"
    # File uses LF line endings
    test_file.write_text("Line 1\nLine 2\nLine 3\n")

    # Try to replace with CRLF in search string - should now succeed
    result = await file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "Line 1\r\n",  # CRLF
        "newString": "Modified Line 1\n"
    })

    # Should succeed with normalized line endings
    assert result["status"] == "success"
    assert result["changes"]["replacements"] == 1

    # Verify content was updated
    updated_content = test_file.read_text()
    assert "Modified Line 1" in updated_content
@pytest.mark.asyncio
async def test_replace_string_whitespace_detection(file_ops_server, tmp_allowed_dir):
    """Test replace_string_in_file detects spaces/tabs mismatch with detailed error."""
    test_file = tmp_allowed_dir / "whitespace_test.txt"
    # File uses spaces for indentation
    test_file.write_text("    indented line\nno indent\n")

    # Try to replace with tabs instead of spaces
    result = await file_ops_server.replace_string_in_file({
        "filePath": str(test_file),
        "oldString": "\tindented line",  # Tab
        "newString": "    modified line"  # Spaces
    })

    assert result["status"] == "error"
    assert result["error_type"] == "WhitespaceMatchError"
    # Check for detailed whitespace analysis in error message
    assert "leading whitespace" in result["error"] or "tabs" in result["error"]
    assert "spaces" in result["error"]
    assert "hint" in result
    assert "whitespace" in result["hint"].lower()
    # New: Check for detailed breakdown
    assert "file_sample" in result or "search_sample" in result or "File has" in result["error"]


