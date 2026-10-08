"""
Tests for sqlite_query plugin - simple SQL execution for debugging.
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from pathlib import Path
import tempfile
import sqlite3
from src.plugins.sqlite_query.server import SqliteQueryServer
from unittest.mock import AsyncMock, MagicMock
from agent_system.config import AgentSystemConfig, ToolServerConfig


@pytest.fixture
def plugin(test_db):
    """Create plugin instance with test database."""
    # Mock configs
    system_config = MagicMock(spec=AgentSystemConfig)
    server_config = MagicMock(spec=ToolServerConfig)
    server_config.database = test_db
    server_config.query_timeout = 30
    
    return SqliteQueryServer(name="test_sqlite", system_config=system_config, server_config=server_config)


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
    server_config = MagicMock(spec=ToolServerConfig)
    server_config.database = "/nonexistent/database.db"
    server_config.query_timeout = 30
    
    plugin = SqliteQueryServer(name="test_sqlite", system_config=system_config, server_config=server_config)
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


class TestNameMissRecovery:
    """A name miss answers the follow-up the agent would type anyway.

    From a real session: `no such table: archived_messages` cost a second turn
    for the sqlite_master query. The error now carries that query's answer.
    """

    @pytest.mark.asyncio
    async def test_no_such_table_lists_tables_and_suggests(self, plugin, mock_status):
        res = await plugin.execute_sql(
            {"sql": "SELECT * FROM bookss", "_status": mock_status})
        assert res["status"] == "error"
        assert "books" in res["tables"]
        assert res["did_you_mean"] == "books"

    @pytest.mark.asyncio
    async def test_no_such_column_scopes_to_referenced_table(self, plugin, mock_status):
        res = await plugin.execute_sql(
            {"sql": "SELECT titel FROM books", "_status": mock_status})
        assert res["status"] == "error"
        assert res["did_you_mean"] == "title"
        # only the table the query names, not the whole schema
        assert list(res["columns"].keys()) == ["books"]
        assert "title" in res["columns"]["books"]

    @pytest.mark.asyncio
    async def test_syntax_error_gets_no_fake_recovery(self, plugin, mock_status):
        """Errors without a mechanical fix must not sprout guesses."""
        res = await plugin.execute_sql(
            {"sql": "SELEC * FROM books", "_status": mock_status})
        assert res["status"] == "error"
        assert "did_you_mean" not in res
        assert "tables" not in res


class TestCommandLine:
    """``mcp-sqlite-query`` imported the MCP SDK and called methods the server
    never had, so the installed command crashed on start. It now runs the
    tool through the plugin like the other plugin CLIs."""

    def _run(self, capsys, *argv):
        from plugins.sqlite_query.__main__ import cli_main

        with pytest.raises(SystemExit) as exit_info:
            cli_main(list(argv))
        return exit_info.value.code, capsys.readouterr()

    def test_a_query_prints_its_rows(self, capsys, test_db):
        code, out = self._run(capsys, "--database", test_db,
                              "--sql", "SELECT title FROM books ORDER BY id")

        assert code == 0, out.err
        assert "Found 3 rows" in out.out
        assert "Book 2" in out.out

    def test_a_failing_statement_exits_non_zero(self, capsys, test_db):
        code, out = self._run(capsys, "-d", test_db, "-s", "SELECT nope FROM books")

        assert code == 1
        assert "no such column" in out.err

    def test_json_output_carries_the_error_and_the_exit_code(self, capsys, test_db):
        code, out = self._run(capsys, "-d", test_db, "-s", "SELEC 1", "--json")

        assert code == 1
        assert json.loads(out.out)["status"] == "error"

    def test_the_installed_entry_point_resolves(self):
        import importlib
        import warnings

        from setuptools.config.pyprojecttoml import read_configuration

        # the commands are in console_scripts.cfg (dynamic in pyproject.toml): read as pip's build reads them
        repo = Path(__file__).resolve().parents[4]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scripts = read_configuration(repo / "pyproject.toml", expand=True)["project"]["scripts"]
        module, func = scripts["tool-sqlite-query"].split(":")

        assert callable(getattr(importlib.import_module(module), func))

    def test_a_name_miss_prints_the_suggestion(self, capsys, test_db):
        code, out = self._run(capsys, "-d", test_db, "-s", "SELECT tittle FROM books")

        assert code == 1
        assert "Did you mean: title" in out.err

    def test_blob_and_null_survive_both_outputs(self, capsys, test_db):
        conn = sqlite3.connect(test_db)  # `with` commits but does not close
        conn.execute("CREATE TABLE blobs (b BLOB, n TEXT)")
        conn.execute("INSERT INTO blobs VALUES (x'00ff', NULL)")
        conn.commit()
        conn.close()

        code, out = self._run(capsys, "-d", test_db, "-s", "SELECT b, n FROM blobs", "--json")
        assert code == 0, out.err
        assert json.loads(out.out)["row_count"] == 1

        code, out = self._run(capsys, "-d", test_db, "-s", "SELECT b, n FROM blobs")
        assert code == 0, out.err
        assert "NULL" in out.out


def _server(db, **settings):
    server_config = MagicMock(spec=ToolServerConfig)
    server_config.database = str(db)
    server_config.query_timeout = settings.pop("query_timeout", 30)
    for key, value in settings.items():
        setattr(server_config, key, value)
    return SqliteQueryServer(name="test_sqlite", system_config=MagicMock(spec=AgentSystemConfig),
                             server_config=server_config)


class TestOnlyTheConfiguredFile:
    """ATTACH and VACUUM INTO created files anywhere the process could write,
    and ATTACH read any other SQLite database on the machine."""

    @pytest.mark.asyncio
    async def test_attach_is_refused_and_creates_nothing(self, plugin, mock_status, tmp_path):
        target = tmp_path / "elsewhere.db"
        res = await plugin.execute_sql(
            {"sql": f"ATTACH '{target.as_posix()}' AS other", "_status": mock_status})
        assert res["status"] == "error"
        assert "not authorized" in res["error"]
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_vacuum_into_is_refused_and_creates_nothing(self, plugin, mock_status, tmp_path):
        target = tmp_path / "copy.db"
        res = await plugin.execute_sql(
            {"sql": f"VACUUM INTO '{target.as_posix()}'", "_status": mock_status})
        assert res["status"] == "error"
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_directory_pragmas_are_refused(self, plugin, mock_status, tmp_path):
        res = await plugin.execute_sql(
            {"sql": f"PRAGMA temp_store_directory = '{tmp_path.as_posix()}'", "_status": mock_status})
        assert res["status"] == "error"
        assert "not authorized" in res["error"]


class TestWritesAreReportedAsTheyHappened:

    @pytest.mark.asyncio
    async def test_insert_returning_is_committed(self, plugin, test_db, mock_status):
        """RETURNING gives a result set, which skipped the commit: the answer
        showed the new row, the database rolled it back."""
        res = await plugin.execute_sql(
            {"sql": "INSERT INTO books (title) VALUES ('Kept') RETURNING id", "_status": mock_status})
        assert res["rows"] == [{"id": 4}]
        conn = sqlite3.connect(test_db)
        try:
            assert conn.execute("SELECT count(*) FROM books WHERE title = 'Kept'").fetchone()[0] == 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_a_with_insert_reports_its_rows(self, plugin, mock_status):
        """cursor.rowcount is -1 for DML that begins with WITH."""
        res = await plugin.execute_sql(
            {"sql": "WITH n AS (SELECT 'A' AS t UNION ALL SELECT 'B') "
                    "INSERT INTO books (title) SELECT t FROM n", "_status": mock_status})
        assert res["rows_affected"] == 2


class TestBounds:

    @pytest.mark.asyncio
    async def test_a_query_that_never_ends_stops_at_query_timeout(self, test_db, mock_status):
        plugin = _server(test_db, query_timeout=1)
        started = time.monotonic()
        res = await plugin.execute_sql(
            {"sql": "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM x) "
                    "SELECT count(*) FROM x", "_status": mock_status})
        assert res["status"] == "error"
        assert "query_timeout" in res["error"]
        assert time.monotonic() - started < 10

    @pytest.mark.asyncio
    async def test_a_cancelled_run_stops_the_statement(self, plugin, mock_status):
        token = SimpleNamespace(is_cancelled=False, is_forced=False)
        threading.Timer(0.3, lambda: setattr(token, "is_cancelled", True)).start()
        started = time.monotonic()
        res = await plugin.execute_sql(
            {"sql": "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM x) "
                    "SELECT count(*) FROM x", "_status": mock_status,
             "_cancellation_token": token})
        assert res["cancelled"] is True
        assert time.monotonic() - started < 10  # not rescued by the 30 s deadline

    @pytest.mark.asyncio
    async def test_rows_are_capped_at_max_rows(self, test_db, mock_status):
        plugin = _server(test_db, max_rows=2)
        res = await plugin.execute_sql({"sql": "SELECT * FROM books", "_status": mock_status})
        assert res["row_count"] == 2
        assert res["truncated"] is True
        full = await plugin.execute_sql({"sql": "SELECT * FROM books LIMIT 2", "_status": mock_status})
        assert "truncated" not in full


@pytest.mark.asyncio
async def test_recovery_reads_a_database_whose_path_has_uri_characters(tmp_path, mock_status):
    """The read-only URI was built by string formatting: '#' cut the path
    and the recovery listed the tables of nothing."""
    folder = tmp_path / "a#b"
    folder.mkdir()
    db = folder / "t.db"
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE "my books" (id INTEGER, title TEXT)')
    conn.commit()
    conn.close()
    plugin = _server(db)

    res = await plugin.execute_sql({"sql": "SELECT * FROM my_bookz", "_status": mock_status})
    assert res["tables"] == ["my books"]

    # a quoted name broke the PRAGMA table_info() the recovery built from it
    res = await plugin.execute_sql({"sql": 'SELECT titel FROM "my books"', "_status": mock_status})
    assert res["did_you_mean"] == "title"


def test_the_help_names_the_installed_command(capsys):
    from plugins.sqlite_query.__main__ import cli_main

    with pytest.raises(SystemExit):
        cli_main(["--help"])
    out = capsys.readouterr().out
    assert "tool-sqlite-query" in out
    assert "mcp-sqlite-query" not in out


@pytest.mark.asyncio
async def test_columns_with_the_same_name_all_reach_the_rows(plugin, mock_status):
    """dict(row) kept only the last of two equal names: `columns` listed
    id twice, each row carried one id."""
    res = await plugin.execute_sql(
        {"sql": "SELECT a.id, b.id FROM books a JOIN books b ON b.id = a.id + 1 ORDER BY a.id",
         "_status": mock_status})
    assert res["columns"] == ["id", "id_2"]
    assert res["rows"][0] == {"id": 1, "id_2": 2}


@pytest.mark.asyncio
async def test_setting_a_process_wide_heap_limit_is_refused(plugin, mock_status):
    """hard/soft_heap_limit apply to every connection in the process and can
    only be lowered; reading them stays allowed."""
    for pragma in ("hard_heap_limit", "soft_heap_limit"):
        res = await plugin.execute_sql({"sql": f"PRAGMA {pragma} = 100000", "_status": mock_status})
        assert res["status"] == "error", pragma
        assert "not authorized" in res["error"]
        read = await plugin.execute_sql({"sql": f"PRAGMA {pragma}", "_status": mock_status})
        assert read["status"] == "success", pragma


class TestValueSize:
    """One value is built in one opcode: zeroblob(900000000) peaked at 13.5 GB
    before any other bound could see it."""

    @pytest.mark.asyncio
    async def test_a_value_above_max_value_bytes_is_refused(self, test_db, mock_status):
        plugin = _server(test_db, max_value_bytes=1000)
        res = await plugin.execute_sql({"sql": "SELECT zeroblob(5000)", "_status": mock_status})
        assert res["status"] == "error"
        assert "too big" in res["error"]

    @pytest.mark.asyncio
    async def test_a_long_text_is_cut_with_its_length(self, test_db, mock_status):
        plugin = _server(test_db, max_value_chars=10)
        res = await plugin.execute_sql(
            {"sql": "SELECT printf('%.50c', 'a') AS t, x'00ff' AS b, zeroblob(40) AS z",
             "_status": mock_status})
        row = res["rows"][0]
        assert row["t"] == "a" * 10 + "…[50 chars]"
        assert row["b"] == "x'00ff'"
        assert row["z"] == "x'0000000000'…[40 bytes]"
