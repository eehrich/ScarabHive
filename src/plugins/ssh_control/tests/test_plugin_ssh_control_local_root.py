"""upload_file and download_file touch local files only inside local_root.

No real host is contacted: the connection manager's transfer methods are
replaced by fakes that record the local path they would have opened.
"""
import os
from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.ssh_control.models import FileTransferResult


def build(local_root=None):
    """A tool server whose transfers only record the local path they were given."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    config = ToolServerConfig()
    config.machines = [{"name": "m", "host": "m.test", "username": "u"}]
    if local_root is not None:
        config.local_root = str(local_root)
    tool_server = PLUGIN_FACTORY("ssh_control_test", AgentSystemConfig(), config).tool_server
    opened = []

    async def upload(machine, local_path, remote_path, mode=None):
        opened.append(("upload", local_path))
        return FileTransferResult(machine=machine, local_path=local_path, remote_path=remote_path,
                                  bytes_transferred=1, duration=0.0, success=True)

    async def download(machine, remote_path, local_path):
        opened.append(("download", local_path))
        return FileTransferResult(machine=machine, local_path=local_path, remote_path=remote_path,
                                  bytes_transferred=1, duration=0.0, success=True)

    tool_server.connection_manager.upload_file = upload
    tool_server.connection_manager.download_file = download
    return tool_server, opened


async def upload(tool_server, local_path):
    return await tool_server.upload_file({"machine": "m", "local_path": local_path, "remote_path": "/srv/x"})


async def download(tool_server, local_path):
    return await tool_server.download_file({"machine": "m", "remote_path": "/srv/x", "local_path": local_path})


def link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or a junction where Windows refuses the symlink."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "root"
    folder.mkdir()
    return folder


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("secret")
    return folder


@pytest.mark.asyncio
async def test_without_local_root_both_tools_refuse_and_name_the_setting():
    tool_server, opened = build()
    for call in (upload, download):
        with pytest.raises(PermissionError, match="local_root is not set"):
            await call(tool_server, "a.txt")
    assert opened == []


@pytest.mark.asyncio
async def test_a_path_inside_the_root_is_used_resolved(root):
    (root / "a.txt").write_text("a")
    tool_server, opened = build(root)
    await upload(tool_server, "a.txt")
    await upload(tool_server, str(root / "a.txt"))
    await download(tool_server, "logs/deep/b.log")
    assert opened == [("upload", str(root / "a.txt")), ("upload", str(root / "a.txt")),
                      ("download", str(root / "logs" / "deep" / "b.log"))]
    assert (root / "logs" / "deep").is_dir()  # the download's folder is created inside the root


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["../outside/secret.txt", "sub/../../outside/secret.txt"])
async def test_dot_dot_out_of_the_root_is_refused(root, outside, escape):
    tool_server, opened = build(root)
    for call in (upload, download):
        with pytest.raises(PermissionError, match="outside the allowed directories"):
            await call(tool_server, escape)
    assert opened == [] and (outside / "secret.txt").read_text() == "secret"


@pytest.mark.asyncio
async def test_an_absolute_path_outside_the_root_is_refused(root, outside):
    tool_server, opened = build(root)
    for call in (upload, download):
        with pytest.raises(PermissionError, match="outside the allowed directories"):
            await call(tool_server, str(outside / "secret.txt"))
    assert opened == [] and not (root.parent / "outside" / "sub").exists()


class FileSystemTouched(BaseException):
    """BaseException, so no except-clause on the way can swallow it."""


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [r"\\192.0.2.1\share\x.txt", "//192.0.2.1/share/x.txt",
                                  r"\\?\UNC\192.0.2.1\share\x.txt"])
async def test_a_host_path_is_refused_before_the_file_system_is_asked(root, monkeypatch, path):
    """Resolving \\\\host\\share makes Windows connect to the host and sign in
    with the user's NTLM credentials -- the refusal has to come first."""
    tool_server, opened = build(root)
    asked = []

    def touched(name):
        def call(*args, **kwargs):
            asked.append((name, args))
            raise FileSystemTouched(name)
        return call

    for name in ("stat", "lstat", "mkdir", "readlink"):
        monkeypatch.setattr(os, name, touched(name))
    monkeypatch.setattr(os.path, "realpath", touched("realpath"))
    if hasattr(os, "_getfinalpathname"):
        monkeypatch.setattr(os, "_getfinalpathname", touched("_getfinalpathname"))
    refusals = []
    try:
        for call in (upload, download):
            try:
                await call(tool_server, path)
            except PermissionError as exc:
                refusals.append(str(exc))
    except FileSystemTouched:
        pass
    finally:
        monkeypatch.undo()  # pytest itself needs os.stat to report
    assert asked == [] and opened == []
    assert len(refusals) == 2 and all("outside the allowed directories" in r for r in refusals)


@pytest.mark.asyncio
async def test_a_link_out_of_the_root_is_refused(root, outside):
    link_dir(root / "link", outside)
    tool_server, opened = build(root)
    for call in (upload, download):
        with pytest.raises(PermissionError, match="outside the allowed directories"):
            await call(tool_server, "link/secret.txt")
    assert opened == [] and (outside / "secret.txt").read_text() == "secret"


@pytest.mark.asyncio
async def test_a_download_onto_a_file_symlink_does_not_write_outside(root, outside):
    try:
        os.symlink(outside / "secret.txt", root / "target.txt")
    except OSError as exc:
        pytest.skip(f"file symlinks not permitted here: {exc}")
    tool_server, opened = build(root)
    with pytest.raises(PermissionError, match="outside the allowed directories"):
        await download(tool_server, "target.txt")
    assert opened == [] and (outside / "secret.txt").read_text() == "secret"


@pytest.mark.asyncio
async def test_a_directory_is_refused_as_local_path(root):
    tool_server, opened = build(root)
    for call in (upload, download):
        with pytest.raises(IsADirectoryError):
            await call(tool_server, ".")
    assert opened == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="device names exist on Windows only")
async def test_a_windows_device_name_is_refused(root):
    tool_server, opened = build(root)
    for call in (upload, download):
        with pytest.raises(PermissionError, match="device name"):
            await call(tool_server, "COM1")
    assert opened == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_local_root_is_unset(blank):
    """"  " resolved to the project root on Windows: config/ and src/ open to both tools."""
    tool_server, opened = build(blank)
    with pytest.raises(PermissionError, match="local_root is not set"):
        await upload(tool_server, "config/secrets.env")
    assert opened == []


@pytest.mark.asyncio
async def test_a_relative_data_path_stays_in_the_root(root, tmp_path, monkeypatch):
    """The sandbox sends a relative data/... to the data directory: refused, or
    elsewhere, depending on the installation."""
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "elsewhere"))
    tool_server, opened = build(root)
    await download(tool_server, "data/x.txt")
    assert opened == [("download", str(root / "data" / "x.txt"))]
