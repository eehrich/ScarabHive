"""Paths a model should not be able to use against the machine.

Three holes, each measured before it was closed:

* A UNC path (``\\\\host\\share\\x``) was resolved before containment refused
  it, and resolving opens it: on Windows that connects to the host and signs
  in with the user's NTLM credentials. Four file system calls reached the host
  per refused path. The refusal must now come before any.
* An allowed directory could be deleted, moved or renamed itself --
  ``delete . recursive`` removed the whole folder the instance was given.
* A link stood for its target: deleting ``link.txt`` deleted the file it
  points to, a recursive delete of a directory link emptied that directory.
"""
from __future__ import annotations

import errno
import gc
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops.server import FileOpsServer


@pytest.fixture
async def root(tmp_path):
    folder = tmp_path / "root"
    folder.mkdir()
    (folder / "real.txt").write_text("real")
    return folder


@pytest.fixture
async def server(root):
    config = ToolServerConfig(type="file_ops", enabled=True)
    config.allowed_directories = [str(root)]
    config.search = {"enable_indexing": False, "enable_semantic_search": False}
    server = FileOpsServer("file_ops", Mock(spec=AgentSystemConfig), config)
    yield server
    await server.search_engine.stop()
    gc.collect()


EVIL = [r"\\evil.example\share\x.txt", "//evil.example/share/x.txt",
        r"\\?\UNC\evil.example\share\x.txt", "/\\evil.example\\share\\x.txt",
        r"\??\UNC\evil.example\share\x.txt", "/??/UNC/evil.example/share/x.txt"]


@pytest.fixture
def touched(monkeypatch):
    """Every Path call on a path naming evil.example. The spy raises too;
    code that swallows the raise still leaves the call recorded."""
    calls = []
    for name in ("exists", "is_file", "is_dir", "is_symlink", "is_junction", "resolve", "stat",
                 "open", "mkdir", "unlink", "rename", "replace", "read_text", "write_text"):
        original = getattr(Path, name)

        def spy(self, *args, _original=original, _name=name, **kwargs):
            if "evil.example" in str(self):
                calls.append((_name, str(self)))
                raise AssertionError(f"{_name} on {self}")
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(Path, name, spy)
    return calls


WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="UNC paths exist on Windows only; on POSIX they are file names")


@WINDOWS_ONLY
@pytest.mark.parametrize("evil", EVIL)
async def test_a_unc_path_is_refused_before_the_file_system_is_asked(server, root, touched, evil):
    real = str(root / "real.txt")
    calls = [
        (server.read_file, {"filePath": evil}),
        (server.replace_string_in_file, {"filePath": evil, "oldString": "a", "newString": "b"}),
        (server.list_directory, {"dir_path": evil}),
        (server.manage, {"operation": "create", "path": evil, "content": "x"}),
        (server.manage, {"operation": "delete", "path": evil}),
        (server.manage, {"operation": "move", "path": evil, "destination": str(root / "y.txt")}),
        (server.manage, {"operation": "move", "path": real, "destination": evil}),
        (server.manage, {"operation": "rename", "path": real, "new_name": evil}),
    ]
    for tool, params in calls:
        result = await tool(params)
        expected = "ValidationError" if "new_name" in params else "SecurityError"
        assert result["status"] == "error" and result["error_type"] == expected, (params, result)
    assert touched == []
    assert (root / "real.txt").read_text() == "real"


@pytest.mark.parametrize("params", [
    {"operation": "delete", "recursive": True},
    {"operation": "move", "destination": "elsewhere"},
    {"operation": "rename", "new_name": "elsewhere"},
])
async def test_an_allowed_directory_itself_is_not_deleted_moved_or_renamed(server, root, params):
    if params.get("destination"):
        params = {**params, "destination": str(root / "inner" / "elsewhere")}
    result = await server.manage({**params, "path": str(root)})
    assert result["status"] == "error" and "allowed directories itself" in result["error"], result
    assert (root / "real.txt").read_text() == "real"


def _link(link: Path, target: Path, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlinks not permitted here -- this test measures nothing")


async def test_deleting_a_file_link_leaves_its_target(server, root):
    _link(root / "link.txt", root / "real.txt")
    result = await server.manage({"operation": "delete", "path": str(root / "link.txt")})
    assert result["status"] == "success" and result["type"] == "link", result
    assert not (root / "link.txt").is_symlink()
    assert (root / "real.txt").read_text() == "real"


async def test_a_recursive_delete_of_a_directory_link_leaves_the_directory(server, root):
    (root / "lib").mkdir()
    (root / "lib" / "keep.txt").write_text("keep")
    _link(root / "dlink", root / "lib", directory=True)
    result = await server.manage({"operation": "delete", "path": str(root / "dlink"), "recursive": True})
    assert result["status"] == "success" and result["type"] == "link", result
    assert (root / "lib" / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
async def test_a_recursive_delete_of_a_junction_leaves_the_directory(server, root):
    (root / "lib").mkdir()
    (root / "lib" / "keep.txt").write_text("keep")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "jn"), str(root / "lib")],
                   capture_output=True, check=False)
    if not (root / "jn").is_junction():
        pytest.skip("could not create a junction -- this test measures nothing")
    result = await server.manage({"operation": "delete", "path": str(root / "jn"), "recursive": True})
    assert result["status"] == "success", result
    assert (root / "lib" / "keep.txt").read_text() == "keep"


async def test_a_dangling_link_can_be_deleted(server, root):
    _link(root / "dangling.txt", root / "gone.txt")
    result = await server.manage({"operation": "delete", "path": str(root / "dangling.txt")})
    assert result["status"] == "success", result
    assert not (root / "dangling.txt").is_symlink()


async def test_renaming_or_moving_a_link_moves_the_link(server, root):
    _link(root / "link.txt", root / "real.txt")
    result = await server.manage({"operation": "rename", "path": str(root / "link.txt"), "new_name": "l2.txt"})
    assert result["status"] == "success", result
    assert (root / "l2.txt").is_symlink() and (root / "real.txt").read_text() == "real"
    (root / "sub").mkdir()
    result = await server.manage({"operation": "move", "path": str(root / "l2.txt"),
                                  "destination": str(root / "sub" / "l3.txt")})
    assert result["status"] == "success", result
    assert (root / "sub" / "l3.txt").is_symlink() and (root / "real.txt").read_text() == "real"


async def test_a_dangling_link_can_be_renamed_and_moved(server, root):
    _link(root / "dangling.txt", root / "gone.txt")
    result = await server.manage({"operation": "rename", "path": str(root / "dangling.txt"), "new_name": "d2.txt"})
    assert result["status"] == "success", result
    result = await server.manage({"operation": "move", "path": str(root / "d2.txt"),
                                  "destination": str(root / "d3.txt")})
    assert result["status"] == "success", result
    assert (root / "d3.txt").is_symlink()


async def test_a_missing_path_is_not_reported_as_an_unexpected_error(server, root):
    status = MagicMock(progress=AsyncMock(), end=AsyncMock(), error=AsyncMock())
    result = await server.manage({"operation": "delete", "path": str(root / "nope.txt"), "_status": status})
    assert result["error_type"] == "FileNotFoundError", result
    message = status.error.call_args.args[0]
    assert not message.startswith("Unexpected"), message


def _junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   capture_output=True, check=False)
    if not link.is_junction():
        pytest.skip("could not create a junction -- this test measures nothing")


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("SECRET")
    return folder


@pytest.fixture
def across_drives(monkeypatch):
    """os.rename fails as it does between two drives; shutil.move then copies."""
    def rename(src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
    monkeypatch.setattr(os, "rename", rename)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
@pytest.mark.parametrize("inside_a_folder", [False, True])
async def test_a_junction_moved_across_drives_copies_nothing_in(
        server, root, outside, across_drives, inside_a_folder):
    source = root / "box" if inside_a_folder else root / "jn"
    if inside_a_folder:
        source.mkdir()
        _junction(source / "jn", outside)
    else:
        _junction(source, outside)
    result = await server.manage({"operation": "move", "path": str(source),
                                  "destination": str(root / "copied")})
    assert result["status"] == "error", result
    assert not (root / "copied").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
async def test_a_junction_moves_and_renames_as_a_link(server, root, outside):
    _junction(root / "jn", outside)
    result = await server.manage({"operation": "move", "path": str(root / "jn"),
                                  "destination": str(root / "jn2")})
    assert result["status"] == "success" and result["type"] == "link", result
    result = await server.manage({"operation": "rename", "path": str(root / "jn2"), "new_name": "jn3"})
    assert result["status"] == "success" and result["type"] == "link", result
    assert (root / "jn3").is_junction() and (outside / "secret.txt").read_text() == "SECRET"


async def test_a_file_still_moves_across_drives(server, root, across_drives):
    result = await server.manage({"operation": "move", "path": str(root / "real.txt"),
                                  "destination": str(root / "sub" / "real.txt")})
    assert result["status"] == "success" and result["type"] == "file", result
    assert (root / "sub" / "real.txt").read_text() == "real"
