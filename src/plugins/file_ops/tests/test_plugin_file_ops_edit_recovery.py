"""Edits that miss, reads that would flood, paths that are wrong -- through the server's own tool methods.

A missed edit says why and where to look instead of only "not found"; an edit that only matches with its line
endings read loosely changes that span and nothing else; a read refuses binary content and stops a page at the
size an unpaginated read may have; a wrong path names the files it may have meant.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops import server as server_module
from plugins.file_ops.server import FileOpsServer

PAGE_KB = 4


@pytest.fixture
async def ops(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    system_config = Mock(spec=AgentSystemConfig)
    system_config.project_root = str(tmp_path)
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = [str(root)]
    server_config.max_unpaginated_file_size_kb = PAGE_KB
    server_config.search = {"enable_indexing": False, "chroma_db_path": str(tmp_path / "vector_store")}
    server = FileOpsServer("file_ops", system_config, server_config)
    yield server, root
    await server.search_engine.stop()


def put(path: Path, text: str) -> None:
    """Exactly these characters on disk: write_text would turn every \\n into CRLF on Windows."""
    path.write_bytes(text.encode("utf-8"))


def text_of(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


async def edit(server, path: Path, old: str, new: str) -> dict:
    return await server.replace_string_in_file({"filePath": str(path), "oldString": old, "newString": new})


@pytest.mark.parametrize("old, new, expected", [
    # the span's own CRLF for the new text, the LF line after it untouched
    ("two\nthree", "TWO\nTHREE", b"one\r\nTWO\r\nTHREE\nfour\n"),
    # a span that ends on a CRLF takes it along, and a span that starts on one too
    ("one\ntwo\n", "ONE\nTWO\n", b"ONE\r\nTWO\r\nthree\nfour\n"),
    ("\ntwo\nthree", "\n2\n3", b"one\r\n2\r\n3\nfour\n"),   # not exact: "\ntwo\r\nthree" is
    # a span that ends right before a CRLF leaves it in place
    ("one\ntwo", "ONE\nTWO", b"ONE\r\nTWO\r\nthree\nfour\n"),
    # an exact match: the model's \n becomes the CRLF of the line it lands in
    ("two", "two\nextra", b"one\r\ntwo\r\nextra\r\nthree\nfour\n"),
    # an exact match that starts on the \n of a CRLF takes its \r along: no \r\r\n, no lone \r
    ("\ntwo", "\nTWO\nextra", b"one\r\nTWO\r\nextra\r\nthree\nfour\n"),
    ("\ntwo", " two", b"one two\r\nthree\nfour\n"),
])
async def test_an_edit_matched_across_line_endings_changes_only_its_span(ops, old, new, expected):
    server, root = ops
    path = root / "mixed.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\nfour\n")

    result = await edit(server, path, old, new)

    assert result["status"] == "success", result
    assert path.read_bytes() == expected


async def test_an_edit_whose_result_is_already_there_says_so_beside_not_found(ops):
    server, root = ops
    path = root / "done.py"
    put(path, "retries = 5  # default\nb = 3\n")

    result = await edit(server, path, "retries = 3  # default", "retries = 5  # default")

    assert result["error_type"] == "StringNotFoundError" and result["new_string_present"] is True, result
    assert "made already" in result["hint"]
    assert text_of(path) == "retries = 5  # default\nb = 3\n"


async def test_a_short_new_string_found_inside_another_word_is_no_sign_of_a_done_edit(ops):
    server, root = ops
    path = root / "auth.py"
    put(path, "password = read()\n")

    result = await edit(server, path, "    raise", "    pass")

    assert result["error_type"] == "StringNotFoundError" and "new_string_present" not in result, result


async def test_typographic_characters_are_named_even_when_the_new_text_stands_elsewhere(ops):
    server, root = ops
    path = root / "story.md"
    put(path, "Sie lachte – leise.\nSie lachte.\n")

    result = await edit(server, path, "Sie lachte - leise.", "Sie lachte.")

    assert result["error_type"] == "CharacterMatchError", result


async def test_overlapping_matches_are_ambiguous(ops):
    server, root = ops
    path = root / "q.txt"
    put(path, "q\nq\nq\n")

    result = await edit(server, path, "\nq\n", "\nX\n")

    assert result["error_type"] == "AmbiguousMatchError" and result["occurrences"] == 2, result
    assert text_of(path) == "q\nq\nq\n"


async def test_plain_quotes_against_typographic_ones_get_the_exact_text_and_change_nothing(ops):
    server, root = ops
    path = root / "prose.md"
    line = "Er sagte: „Hallo“ – und ging."
    put(path, f"Anfang.\n{line}\nEnde.\n")

    result = await edit(server, path, 'Er sagte: "Hallo" - und ging.', 'Er sagte: "Tschuess" - und ging.')

    assert result["error_type"] == "CharacterMatchError", result
    assert result["corrected_old_string"] == line
    assert text_of(path) == f"Anfang.\n{line}\nEnde.\n"


async def test_a_missed_edit_names_the_closest_lines(ops):
    server, root = ops
    path = root / "calc.py"
    put(path, "import math\n\n\ndef compute_total(items):\n    return sum(items)\n")

    result = await edit(server, path, "def compute_totals(items):\n    return sum(items)", "x")

    assert result["error_type"] == "StringNotFoundError", result
    assert result["closest_lines"][0] == "line 4: def compute_total(items):"


async def test_closest_lines_count_lines_as_read_file_does(ops):
    """splitlines() also split at a form feed: the number was one too high."""
    server, root = ops
    path = root / "paged.c"
    put(path, "a\nb\x0cc\nint x = 1;\n")

    result = await edit(server, path, "int x = 2;", "int x = 3;")

    assert result["closest_lines"][0] == "line 3: int x = 1;", result


async def test_a_binary_file_is_refused_and_an_empty_one_reads(ops):
    server, root = ops
    (root / "blob.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" * 20)
    (root / "empty.txt").write_bytes(b"")

    binary = await server.read_file({"filePath": str(root / "blob.bin")})
    empty = await server.read_file({"filePath": str(root / "empty.txt")})

    assert binary["error_type"] == "BinaryFileError", binary
    assert empty["status"] == "success" and empty["content"] == ""


async def test_utf16_and_coloured_logs_are_text(ops):
    server, root = ops
    (root / "out.txt").write_bytes("Grüße aus PowerShell\r\n".encode("utf-16"))   # with its BOM
    put(root / "pytest.log", "\x1b[32m.\x1b[0m" * 300 + "\n")

    utf16 = await server.read_file({"filePath": str(root / "out.txt")})
    log = await server.read_file({"filePath": str(root / "pytest.log")})

    assert utf16["status"] == "success" and utf16["content"] == "Grüße aus PowerShell\r\n", utf16
    assert log["status"] == "success" and log["content"].startswith("\x1b[32m."), log


async def test_a_file_that_cannot_be_sniffed_is_not_called_binary(ops, monkeypatch):
    """os.access says yes for every existing file on Windows: a locked one read as binary."""
    from plugins.file_ops import operations
    server, root = ops
    put(root / "locked.txt", "inside\n")

    def refusing_open(path, mode="r", *args, **kwargs):
        raise PermissionError(13, "locked", str(path))

    monkeypatch.setattr(operations, "open", refusing_open, raising=False)
    result = await server.read_file({"filePath": str(root / "locked.txt")})

    assert result.get("error_type") != "BinaryFileError", result


async def test_a_page_stops_at_the_unpaginated_size_and_says_where_to_go_on(ops):
    server, root = ops
    lines = [f"{i:04d} " + "x" * 94 + "\n" for i in range(200)]   # 100 characters a line, 20 KB in all
    path = root / "wide.txt"
    put(path, "".join(lines))

    first = await server.read_file({"filePath": str(path)})
    assert 0 < first["lines_read"] < 200 and len(first["content"]) <= PAGE_KB * 1024, first["lines_read"]
    next_offset = first["lines_read"] + 1
    assert f"offset={next_offset}" in first["note"] and first["line_cut"] is False

    second = await server.read_file({"filePath": str(path), "offset": next_offset})
    assert second["content"].startswith(lines[first["lines_read"]])


async def test_a_page_of_umlauts_stops_at_the_size_in_bytes(ops):
    server, root = ops
    put(root / "umlaut.txt", ("ä" * 49 + "\n") * 200)   # 50 characters, 99 bytes a line

    page = await server.read_file({"filePath": str(root / "umlaut.txt")})

    assert len(page["content"].encode("utf-8")) <= PAGE_KB * 1024 and page["lines_read"] == PAGE_KB * 1024 // 99


async def test_a_line_longer_than_a_page_is_cut_and_said_so(ops):
    server, root = ops
    path = root / "minified.js"
    put(path, "a" * (3 * PAGE_KB * 1024) + "\ntail\n")

    first = await server.read_file({"filePath": str(path)})
    rest = await server.read_file({"filePath": str(path), "offset": 2})

    assert first["line_cut"] is True and len(first["content"]) == PAGE_KB * 1024 and "offset=2" in first["note"]
    assert rest["content"] == "tail\n"


async def test_a_wrong_path_names_the_files_meant_but_never_a_secret(ops, monkeypatch):
    server, root = ops
    put(root / "report.txt", "r")
    put(root / "secrets.env", "KEY=1")
    monkeypatch.setattr(server_module, "is_secret", lambda path: Path(path).name == "secrets.env")

    typo = await server.read_file({"filePath": str(root / "reprot.txt")})
    near_secret = await server.read_file({"filePath": str(root / "secret.env")})

    assert typo["error_type"] == "FileNotFoundError" and str(root / "report.txt") in typo["error"], typo
    assert near_secret["error_type"] == "FileNotFoundError" and "secrets.env" not in near_secret["error"]


@pytest.mark.skipif(os.name != "nt", reason="Git Bash paths are a Windows form")
async def test_a_wrong_git_bash_path_gets_suggestions_too(ops):
    server, root = ops
    put(root / "report.txt", "r")
    drive, rest = str(root).split(":", 1)
    bash_path = f"/{drive.lower()}{rest.replace(chr(92), '/')}/reprot.txt"

    result = await server.read_file({"filePath": bash_path})

    assert result["error_type"] == "FileNotFoundError" and "report.txt" in result["error"], result
    suggested = result["error"].split("Did you mean: ", 1)[1].rstrip("?").split(", ")[0]
    again = await server.read_file({"filePath": suggested})
    assert again["status"] == "success" and again["content"] == "r", (suggested, again)


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
async def test_a_utf16_file_is_edited_in_its_own_byte_order(ops, codec):
    server, root = ops
    path = root / "settings.reg"
    path.write_bytes(("﻿Grüße\r\nZeile\r\n").encode(codec))

    result = await edit(server, path, "Zeile", "Linie")

    assert result["status"] == "success", result
    assert path.read_bytes() == ("﻿Grüße\r\nLinie\r\n").encode(codec)


@pytest.mark.parametrize("tool, params", [
    ("list_directory", {}),
    ("read_file", {}),
    ("replace_string_in_file", {"filePath": "x.txt", "oldString": "a"}),
    ("grep_search", {}),
])
async def test_a_missing_required_argument_is_named(ops, tool, params):
    """The framework does not check required: the model got "error: 'dir_path'" from a bare KeyError."""
    server, root = ops

    result = await getattr(server, tool)(params)

    assert result["status"] == "error" and "Missing required argument '" in result["error"], result
