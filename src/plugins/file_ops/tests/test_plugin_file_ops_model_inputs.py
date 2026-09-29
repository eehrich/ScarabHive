"""What models really send, measured in the coder's sessions (25.09.2026).

Three inputs the tools handled badly: numbers as text, paths in Git Bash form,
and an answer that grew with the file instead of with the edit.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops import security
from plugins.file_ops.server import FileOpsServer


def _server(allowed: list[str]) -> FileOpsServer:
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = allowed
    server_config.search = {"enable_indexing": False, "enable_semantic_search": False,
                            "index_on_startup": False}
    return FileOpsServer("file_ops", Mock(spec=AgentSystemConfig), server_config)


# ── the answer of a replacement ──────────────────────────────────────────────

async def test_a_replacement_names_where_it_stands(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")

    result = await _server([str(tmp_path)]).replace_string_in_file(
        {"filePath": str(target), "oldString": "line 3\n", "newString": "new a\nnew b\nnew c\n"})

    assert result["status"] == "success", result
    # 1-indexed, first and last line of the new text.
    assert result["changes"]["lines"] == [3, 5]


async def test_a_replacement_opening_with_a_newline_starts_on_the_next_line(tmp_path):
    target = tmp_path / "b.py"
    target.write_text("line 1\nline 2\nline 3\nline 4\n", encoding="utf-8")

    result = await _server([str(tmp_path)]).replace_string_in_file(
        {"filePath": str(target), "oldString": "\nline 3", "newString": "\nnew 3"})

    assert result["changes"]["lines"] == [3, 3], result


async def test_the_answer_does_not_grow_with_the_file(tmp_path):
    """Compared line by line, every line after an inserted one read as
    modified: one answer carried 53k characters of line numbers."""
    target = tmp_path / "big.py"
    target.write_text("head\n" + "".join(f"x = {i}\n" for i in range(5000)), encoding="utf-8")

    result = await _server([str(tmp_path)]).replace_string_in_file(
        {"filePath": str(target), "oldString": "head\n", "newString": "head\nimport os\n"})

    assert result["changes"]["lines"] == [1, 2]
    assert len(json.dumps(result)) < 500, len(json.dumps(result))


async def test_a_file_with_windows_line_endings_counts_its_lines_alike(tmp_path):
    target = tmp_path / "crlf.py"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = await _server([str(tmp_path)]).replace_string_in_file(
        {"filePath": str(target), "oldString": "three\n", "newString": "3\n"})

    assert result["status"] == "success", result
    assert result["changes"]["lines"] == [3, 3]


# ── numbers as text ──────────────────────────────────────────────────────────

async def test_an_offset_sent_as_text_is_read_as_the_number(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("".join(f"row {i}\n" for i in range(1, 2001)), encoding="utf-8")

    result = await _server([str(tmp_path)]).read_file(
        {"filePath": str(target), "offset": "1480, ", "limit": "2"})

    assert result["status"] == "success", result
    assert "row 1480" in json.dumps(result) and "row 1482" not in json.dumps(result)


async def test_a_number_that_is_none_says_which_parameter(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x\n", encoding="utf-8")

    result = await _server([str(tmp_path)]).read_file({"filePath": str(target), "offset": "the end"})

    assert result["status"] == "error"
    assert "offset" in result["error"] and "whole number" in result["error"], result


async def test_max_results_as_text_works_in_the_searches(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.py").write_text("hit\n", encoding="utf-8")

    result = await _server([str(tmp_path)]).search_files(
        {"pattern": "*.py", "max_results": "2", "path": str(tmp_path)})

    assert result["status"] == "success", result


# ── Git Bash paths ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("given, meant", [
    ("/e/Projects/x.py", "E:/Projects/x.py"),
    ("/c", "C:/"),
    ("/tmp/x", "/tmp/x"),                 # a directory, not a drive letter
    ("src/x.py", "src/x.py"),
    ("E:/already/x.py", "E:/already/x.py"),
])
def test_a_git_bash_path_names_its_drive_on_windows(monkeypatch, given, meant):
    monkeypatch.setattr(security, "os", SimpleNamespace(name="nt"))

    assert security.from_git_bash(given) == meant


def test_elsewhere_a_path_is_what_it_says(monkeypatch):
    monkeypatch.setattr(security, "os", SimpleNamespace(name="posix"))

    assert security.from_git_bash("/e/Projects/x.py") == "/e/Projects/x.py"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Git Bash paths exist on Windows only")
async def test_the_file_tools_read_a_git_bash_path(tmp_path):
    target = tmp_path / "found.txt"
    target.write_text("here\n", encoding="utf-8")
    drive, rest = str(target.resolve()).split(":", 1)
    bash_path = f"/{drive.lower()}{rest.replace(chr(92), '/')}"

    result = await _server([str(tmp_path)]).read_file({"filePath": bash_path})

    assert result["status"] == "success", result



async def test_a_whole_number_sent_as_a_float_is_one(tmp_path):
    target = tmp_path / "rows.txt"
    target.write_text("".join(f"row {i}\n" for i in range(1, 30)), encoding="utf-8")

    for offset in (12.0, "12.0"):
        result = await _server([str(tmp_path)]).read_file({"filePath": str(target), "offset": offset, "limit": 1})
        assert result["status"] == "success" and "row 12" in json.dumps(result), (offset, result)

    result = await _server([str(tmp_path)]).read_file({"filePath": str(target), "offset": 12.5})
    assert result["status"] == "error" and "offset" in result["error"]


# ── what read_file promises ──────────────────────────────────────────────────

def test_read_file_describes_this_instances_limits(tmp_path):
    """The description said "truncates at 2000 lines", a limit nothing applies;
    what applies is the size ceiling and the default page, both configurable."""
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(tmp_path)]
    server_config.search = {"enable_semantic_search": False}
    server_config.max_unpaginated_file_size_kb = 200
    server_config.default_line_limit = 300
    tools = FileOpsServer("fs", Mock(spec=AgentSystemConfig), server_config).get_tools()
    description = next(t["function"]["description"] for t in tools
                       if t["function"]["name"] == "fs_read_file")
    assert "200 KB" in description and "first 300 lines" in description, description
    assert "2000" not in description


# ── yes/no as text ───────────────────────────────────────────────────────────

async def test_false_as_text_does_not_delete_a_tree_or_replace_a_file(tmp_path):
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "keep.txt").write_text("keep")
    server = _server([str(tmp_path)])

    result = await server.manage({"operation": "delete", "path": str(tmp_path / "tree"), "recursive": "false"})
    assert result["error_type"] == "DirectoryNotEmptyError", result
    result = await server.manage({"operation": "create", "path": str(tmp_path / "tree" / "keep.txt"),
                                  "content": "new", "overwrite": "false"})
    assert result["error_type"] == "FileExistsError", result
    assert (tmp_path / "tree" / "keep.txt").read_text() == "keep"

    result = await server.manage({"operation": "delete", "path": str(tmp_path / "tree"), "recursive": "True"})
    assert result["status"] == "success", result


async def test_a_yes_no_value_that_is_neither_is_named(tmp_path):
    result = await _server([str(tmp_path)]).grep_search({"query": "x", "is_regex": "maybe"})
    assert result["status"] == "error" and "is_regex: true or false" in result["error"], result


# ── the schema's bounds, held by the server ──────────────────────────────────

async def test_a_negative_max_results_is_one(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x")
    server = _server([str(tmp_path)])

    listing = await server.list_directory({"dir_path": str(tmp_path), "max_results": -1})
    assert listing["status"] == "success" and listing["total_files"] == 1, listing
    found = await server.search_files({"pattern": "*.txt", "max_results": -1})
    assert found["total_found"] == 1 and len(found["files"]) == 1, found


async def test_context_lines_stop_at_the_schemas_maximum(tmp_path):
    (tmp_path / "a.txt").write_text("".join(f"line {i}\n" for i in range(40)) + "needle\n")

    result = await _server([str(tmp_path)]).grep_search({"query": "needle", "context_lines": 50})

    assert len(result["matches"][0]["context_before"]) == 10, result
