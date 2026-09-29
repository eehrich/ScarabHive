"""Host key checking, agent authentication, the audit trail and the reasons of failed transfers.

No real host is ever contacted: ``asyncssh.connect`` or ``SSHAuthenticator.create_connection`` is replaced.
"""
import asyncio
import logging
from types import SimpleNamespace

import asyncssh
import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.ssh_control.auth import SSHAuthenticator
from plugins.ssh_control.connection_manager import SSHConnectionManager
from plugins.ssh_control.models import FileTransferResult, MachineConfig


@pytest.fixture
def connect_kwargs(monkeypatch):
    """What reaches asyncssh.connect."""
    seen = {}

    async def fake_connect(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("known_hosts_file", [None, ""])
async def test_strict_checking_without_a_known_hosts_file_refuses_to_connect(connect_kwargs, known_hosts_file):
    machine = MachineConfig(name="m", host="build-host.example", username="u", auth_method="password", password="p")
    with pytest.raises(ValueError, match="known_hosts_file"):
        await SSHAuthenticator.create_connection(machine, known_hosts_file, True)
    assert connect_kwargs == {}  # never reached asyncssh with verification off


@pytest.mark.asyncio
async def test_agent_authentication_leaves_the_agent_to_asyncssh(connect_kwargs):
    machine = MachineConfig(name="m", host="build-host.example", username="u", auth_method="agent")
    await SSHAuthenticator.create_connection(machine, None, False)
    assert connect_kwargs["host"] == "build-host.example"
    assert "agent_path" not in connect_kwargs and "client_keys" not in connect_kwargs


def test_a_bare_security_or_defaults_line_is_no_crash():
    manager = SSHConnectionManager({"security": None, "defaults": None,
                                    "machines": [{"name": "m", "host": "h.test", "username": "u"}]})
    assert manager.strict_host_key_checking is True and list(manager.machines) == ["m"]


class Connection:
    async def run(self, command, check=False):
        if command == "hang":
            await asyncio.sleep(10)
        return SimpleNamespace(stdout="", stderr="", exit_status=0)

    async def create_process(self, command):
        async def eof():
            return ""
        stream = SimpleNamespace(readline=eof)

        async def wait():
            process.exit_status = 0
        process = SimpleNamespace(stdout=stream, stderr=stream, exit_status=None, wait=wait, close=lambda: None)
        return process

    def close(self):
        pass


def server(monkeypatch, audit=True):
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    async def connect(*args, **kwargs):
        return Connection()

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    config = ToolServerConfig()
    config.machines = [{"name": "m", "host": "m.test", "username": "root", "command_timeout": 1}]
    config.security = {"audit_log": audit}
    return PLUGIN_FACTORY("ssh_control_test", AgentSystemConfig(), config).tool_server


def audited(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("AUDIT:")]


@pytest.mark.asyncio
async def test_a_command_that_times_out_is_audited(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="plugins.ssh_control")
    answer = await server(monkeypatch).execute({"machine": "m", "command": "hang"})
    assert answer["failed"] == 1
    assert any('"command": "hang"' in line and "Timed out" in line for line in audited(caplog))


@pytest.mark.asyncio
async def test_a_background_command_is_audited_and_lands_in_the_history(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="plugins.ssh_control")
    tool_server = server(monkeypatch)
    started = await tool_server.execute({"machine": "m", "command": "make all", "background": True})
    assert started["status"] == "success"
    assert any('"command": "make all"' in line and "start_background" in line for line in audited(caplog))
    for _ in range(200):
        if any(e["command"] == "make all" for e in tool_server.command_history):
            break
        await asyncio.sleep(0.02)
    entry = next(e for e in tool_server.command_history if e["command"] == "make all")
    assert entry["machine"] == "m" and entry["exit_code"] == 0


@pytest.mark.asyncio
async def test_no_audit_line_when_audit_is_off(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="plugins.ssh_control")
    await server(monkeypatch, audit=False).execute({"machine": "m", "command": "hang"})
    assert audited(caplog) == []


def failed(machine, local_path, remote_path, *rest):
    return FileTransferResult(machine=machine, local_path=local_path, remote_path=remote_path,
                              bytes_transferred=0, duration=0.0, success=False, error="Permission denied")


@pytest.mark.asyncio
async def test_a_failed_upload_says_why(monkeypatch):
    tool_server = server(monkeypatch)

    async def upload(machine, local_path, remote_path, mode=None):
        return failed(machine, local_path, remote_path)

    monkeypatch.setattr(tool_server.connection_manager, "upload_file", upload)
    answer = await tool_server.upload_file({"machine": "m", "local_path": "a.txt", "remote_path": "/srv/a.txt"})
    assert answer["failed"] == 1
    assert answer["results"][0]["error"] == "Permission denied"


@pytest.mark.asyncio
async def test_a_failed_download_says_why_and_ends_as_an_error(monkeypatch):
    tool_server = server(monkeypatch)

    async def download(machine, remote_path, local_path):
        return failed(machine, local_path, remote_path)

    monkeypatch.setattr(tool_server.connection_manager, "download_file", download)
    lines = []
    status = SimpleNamespace(
        progress=lambda *a, **k: asyncio.sleep(0),
        end=lambda text, **k: asyncio.sleep(0, lines.append(("end", text))),
        error=lambda text, **k: asyncio.sleep(0, lines.append(("error", text))))
    answer = await tool_server.download_file({"machine": "m", "remote_path": "/srv/a.txt", "local_path": "a.txt",
                                              "_status": status})
    assert answer["success"] is False and answer["error"] == "Permission denied"
    assert [kind for kind, _ in lines] == ["error"]


# --- review round: empty values, cancelled and early-stopped commands, bad entries, add_machine


@pytest.mark.asyncio
async def test_an_empty_strict_or_audit_value_keeps_them_on(connect_kwargs):
    manager = SSHConnectionManager({"security": {"strict_host_key_checking": None, "audit_log": None}})
    assert manager.strict_host_key_checking is True and manager.audit_log_enabled is True
    machine = MachineConfig(name="m", host="build-host.example", username="u", auth_method="password", password="p")
    with pytest.raises(FileNotFoundError):  # checking stays on: the missing file refuses
        await SSHAuthenticator.create_connection(machine, "~/no_such_known_hosts_file", None)
    assert connect_kwargs == {}
    await SSHAuthenticator.create_connection(machine, None, False)  # only an explicit false turns it off
    assert connect_kwargs["known_hosts"] is None


@pytest.mark.asyncio
async def test_a_cancelled_command_is_audited_and_recorded(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="plugins.ssh_control")
    tool_server = server(monkeypatch)
    task = asyncio.create_task(tool_server.connection_manager.execute_command("m", "hang"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any('"command": "hang"' in line and "Cancelled" in line for line in audited(caplog))
    assert [e["error"] for e in tool_server.command_history if e["command"] == "hang"] == ["Cancelled"]


@pytest.mark.asyncio
async def test_a_background_command_stopped_while_its_channel_opens_is_audited(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="plugins.ssh_control")
    tool_server = server(monkeypatch)
    opened = Connection.create_process

    async def slow_create_process(self, command):
        await asyncio.sleep(0.2)  # the channel opens; the command already runs remotely
        return await opened(self, command)

    monkeypatch.setattr(Connection, "create_process", slow_create_process)
    start = asyncio.create_task(tool_server.processes.start("m", "make all", process_id="p1", owner_session="s"))
    await asyncio.sleep(0.05)
    assert (await tool_server.processes.kill_process("p1", requester_session="s"))["status"] == "success"
    assert (await start)["error_type"] == "StoppedBeforeStart"
    assert any('"command": "make all"' in line and "start_background" in line for line in audited(caplog))


def test_a_bad_machine_entry_is_skipped_not_fatal():
    manager = SSHConnectionManager({"machines": [
        "stray", None, {"name": "zero", "host": "h.test", "username": "u", "max_connections": 0},
        {"name": "ok", "host": "h.test", "username": "u"}]})
    assert list(manager.machines) == ["ok"]


def recording_status(lines):
    return SimpleNamespace(
        progress=lambda *a, **k: asyncio.sleep(0),
        end=lambda text, **k: asyncio.sleep(0, lines.append(("end", text))),
        error=lambda text, **k: asyncio.sleep(0, lines.append(("error", text))))


@pytest.mark.asyncio
async def test_failed_transfers_say_so_in_the_status_line(monkeypatch):
    tool_server = server(monkeypatch)

    async def upload(machine, local_path, remote_path, mode=None):
        return failed(machine, local_path, remote_path)

    async def download(machine, remote_path, local_path):
        return failed(machine, local_path, remote_path)

    monkeypatch.setattr(tool_server.connection_manager, "upload_file", upload)
    monkeypatch.setattr(tool_server.connection_manager, "download_file", download)
    lines = []
    await tool_server.upload_file({"machine": "m", "local_path": "a.txt", "remote_path": "/srv/a.txt",
                                   "_status": recording_status(lines)})
    await tool_server.download_file({"machine": "m", "remote_path": "/srv/a.txt", "local_path": "a.txt",
                                     "_status": recording_status(lines)})
    (up_kind, up), (down_kind, down) = lines
    assert up_kind == down_kind == "error"
    assert up.startswith("Upload failed:") and "Permission denied" in up
    assert down.startswith("Download failed:") and "Permission denied" in down


@pytest.mark.asyncio
async def test_add_machine_closes_the_connection_when_the_test_command_times_out(monkeypatch):
    tool_server = server(monkeypatch)
    closed = []

    class Hanging:
        async def run(self, command, check=False):
            raise asyncio.TimeoutError()  # what wait_for raises after the 5 s

        def close(self):
            closed.append(True)

    async def connect(*args, **kwargs):
        return Hanging()

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    answer = await tool_server.add_machine({"name": "new", "host": "build-host.example", "username": "u"})
    assert answer["success"] is False and "5 for the test command" in answer["error"]
    assert closed == [True]
    assert "new" not in tool_server.connection_manager.machines


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, "3"])
async def test_add_machine_refuses_a_max_connections_below_one(monkeypatch, value):
    tool_server = server(monkeypatch)
    tried = []

    async def connect(*args, **kwargs):
        tried.append(True)
        return Connection()

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    answer = await tool_server.add_machine({"name": "new", "host": "build-host.example", "username": "u",
                                            "max_connections": value})
    assert answer["success"] is False and "max_connections must be" in answer["error"]
    assert tried == []
