"""Three things the background commit left open, all found by review.

A command that is killed went on ringing for five minutes; a recorded result
was only ever dropped by the one read that does not happen in the process that
ran the command; and a woken run that asked for one stream got both.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.models import (AgentSystemConfig, SessionPresenceConfig,
                                        ToolServerConfig)
from plugins.ssh_control.auth import SSHAuthenticator


class Reader:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else ""


class Process:
    def __init__(self, stdout=(), stderr=(), exit_status=0, hold=None):
        self.stdout = Reader(stdout)
        self.stderr = Reader(stderr)
        self.exit_status = None
        self._final_status = exit_status
        self._hold = hold
        self.signals = []

    async def wait(self):
        if self._hold is not None:
            await self._hold.wait()
        self.exit_status = self._final_status

    def terminate(self):
        self.signals.append("SIGTERM")
        if self._hold is not None:
            self._hold.set()

    def kill(self):
        self.signals.append("SIGKILL")
        if self._hold is not None:
            self._hold.set()

    def close(self):
        self.signals.append("close")
        if self._hold is not None:
            self._hold.set()


class Connection:
    def __init__(self, process_factory):
        self._process_factory = process_factory
        self.started = []

    async def run(self, command, check=False):
        return SimpleNamespace(stdout="", stderr="", exit_status=0)

    async def create_process(self, command):
        self.started.append(command)
        return self._process_factory(command)

    def close(self):
        pass


def build(monkeypatch, process_factory):
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    async def connect(*args, **kwargs):
        return Connection(process_factory)

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    server_config = ToolServerConfig()
    server_config.machines = [{"name": "m", "host": "m.test", "username": "root"}]
    server_config.security = {"audit_log": False}
    system_config = AgentSystemConfig()
    system_config.session_presence = SessionPresenceConfig(enabled=True)
    return PLUGIN_FACTORY("ssh_control_test", system_config, server_config)


@pytest.fixture
def woken(monkeypatch):
    calls = []

    async def fake_wake(system_config, session_id, user_id, what="",
                        still_needed=None):
        calls.append({"what": what, "still_needed": still_needed})
        return "woke_session"

    monkeypatch.setattr("plugins.ssh_control.tool_server.wake_session", fake_wake)
    return calls


async def _until(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _call(**extra):
    params = {"machine": "m", "command": "make all", "background": True,
              "_session_id": "sess-1", "_user_id": "someone"}
    params.update(extra)
    return params


class TestKillingItIsDealingWithIt:

    async def test_killing_a_running_command_stops_the_ring(self, monkeypatch, woken):
        hold = asyncio.Event()
        server = build(monkeypatch, lambda cmd: Process(hold=hold)).tool_server
        try:
            started = await server.execute(_call(wake=True))
            assert started["status"] == "success", started
            pid = started["process_id"]

            killed = await server.kill_process({"process_id": pid,
                                                "_session_id": "sess-1"})
            assert killed["status"] == "success", killed
            assert server.processes.processes[pid]["read_after_finish"] is True
        finally:
            await server.close()

    async def test_killing_one_that_already_ended_stops_it_too(self, monkeypatch, woken):
        """Asking for it to stop is dealing with it, whatever it was doing."""
        server = build(monkeypatch, lambda cmd: Process(stdout=["done\n"])).tool_server
        try:
            started = await server.execute(_call(wake=True))
            pid = started["process_id"]
            assert await _until(
                lambda: server.processes.processes[pid]["finished_at"] is not None)
            server.processes.processes[pid]["read_after_finish"] = False

            answer = await server.kill_process({"process_id": pid,
                                                "_session_id": "sess-1"})
            assert answer["note"] == "already finished", answer
            assert server.processes.processes[pid]["read_after_finish"] is True
        finally:
            await server.close()


class TestTheRecordIsHandedOverOrDropped:

    async def test_reading_a_finished_command_live_drops_its_record(
            self, monkeypatch, woken):
        server = build(monkeypatch, lambda cmd: Process(stdout=["done\n"])).tool_server
        try:
            started = await server.execute(_call(wake=True))
            pid = started["process_id"]
            assert await _until(lambda: server.processes.processes[pid]["finished_at"])
            # The record is written by on_finish, after the capture task ends.
            for _ in range(200):
                if await server._recorded.get(pid) is not None:
                    break
                await asyncio.sleep(0.02)
            assert await server._recorded.get(pid) is not None, "nothing was recorded"

            answer = await server.get_output({"process_id": pid,
                                              "_session_id": "sess-1"})
            assert answer["status"] == "success", answer
            assert answer.get("source") != "recorded", "this must be the LIVE answer"
            assert await server._recorded.get(pid) is None
        finally:
            await server.close()

    async def test_a_running_command_keeps_its_record(self, monkeypatch, woken):
        hold = asyncio.Event()
        server = build(monkeypatch, lambda cmd: Process(hold=hold)).tool_server
        try:
            started = await server.execute(_call(wake=True))
            pid = started["process_id"]
            await server._recorded.set(pid, {"process_id": pid, "exit_code": None})

            answer = await server.get_output({"process_id": pid,
                                              "_session_id": "sess-1"})
            assert answer["is_running"] is True, answer
            assert await server._recorded.get(pid) is not None
        finally:
            hold.set()
            await server.close()


class TestARecalledResultAnswersTheQuestionItWasAsked:

    async def test_stream_stderr_does_not_carry_stdout_back(self, monkeypatch):
        """A woken run asking for one stream is avoiding the other one's 30 000
        characters; answering with both spends exactly what it saved."""
        server = build(monkeypatch, lambda cmd: Process()).tool_server
        try:
            await server._recorded.set("ssh_proc_x", {
                "process_id": "ssh_proc_x", "machine": "m", "command": "make",
                "stdout": "a lot of stdout", "stderr": "the error",
                "exit_code": 1, "owner_session": "sess-1"})

            answer = await server.get_output({"process_id": "ssh_proc_x",
                                              "stream": "stderr",
                                              "_session_id": "sess-1"})
            assert answer["source"] == "recorded", answer
            assert answer["stderr"] == "the error"
            assert answer["stdout"] == ""
        finally:
            await server.close()

    async def test_both_is_still_both(self, monkeypatch):
        server = build(monkeypatch, lambda cmd: Process()).tool_server
        try:
            await server._recorded.set("ssh_proc_y", {
                "process_id": "ssh_proc_y", "machine": "m", "command": "make",
                "stdout": "out", "stderr": "err", "exit_code": 0,
                "owner_session": "sess-1"})

            answer = await server.get_output({"process_id": "ssh_proc_y",
                                              "_session_id": "sess-1"})
            assert answer["stdout"] == "out" and answer["stderr"] == "err", answer
        finally:
            await server.close()


async def test_stop_plugin_is_the_name_the_framework_calls(monkeypatch):
    """plugins/capabilities.stop_plugin is the only shutdown hook the adapter
    knows. Without this name, close() was unreachable and remote commands
    outlived the run that started them."""
    plugin = build(monkeypatch, lambda cmd: Process())
    called = []

    async def fake_close():
        called.append(True)

    plugin.close = fake_close
    await plugin.stop_plugin()
    assert called == [True]
    await plugin.tool_server.close()


class TestSomethingAlreadyDealtWithIsNotReportedAgain:
    """Called directly: which of the kill and the capture task gets there
    first is a race, and this is about the branch, not about the winner."""

    async def test_no_record_and_no_ring_for_a_command_already_read(
            self, monkeypatch, woken):
        server = build(monkeypatch, lambda cmd: Process()).tool_server
        try:
            on_finish, note = server._wake_callback(
                {"_session_id": "sess-1", "_user_id": "someone"})
            assert on_finish is not None, note

            server.processes.processes["done"] = {
                "machine": "m", "command": "make all", "exit_code": 0,
                "error": None, "read_after_finish": True,
                "stdout_buffer": ["hi"], "stderr_buffer": [],
                "started_at": "t0", "finished_at": "t1", "owner_session": "sess-1",
            }
            await on_finish("done")

            assert woken == [], woken
            assert await server._recorded.get("done") is None
        finally:
            await server.close()


class TestAnIdIsFreeAgainOnceItsCommandIsOver:
    """_forget_old_finished reclaims an id only after fifty further commands
    have ended -- for a recurring id that is never."""

    async def test_a_finished_id_can_be_used_again(self, monkeypatch):
        server = build(monkeypatch, lambda cmd: Process(stdout=["out\n"])).tool_server
        try:
            first = await server.execute(_call(command="one", process_id="build"))
            assert first["status"] == "success", first
            assert await _until(
                lambda: server.processes.processes["build"]["finished_at"])

            second = await server.execute(_call(command="two", process_id="build"))
            assert second["status"] == "success", second
            assert server.processes.processes["build"]["command"] == "two"
        finally:
            await server.close()

    async def test_a_running_id_is_still_refused(self, monkeypatch):
        hold = asyncio.Event()
        server = build(monkeypatch, lambda cmd: Process(hold=hold)).tool_server
        try:
            first = await server.execute(_call(command="one", process_id="build"))
            assert first["status"] == "success", first

            second = await server.execute(_call(command="two", process_id="build"))
            assert second["status"] == "error", second
            assert second["error_type"] == "ProcessIdInUse", second
            assert server.processes.processes["build"]["command"] == "one"
        finally:
            hold.set()
            await server.close()

    async def test_reusing_an_id_drops_what_was_recorded_under_it(self, monkeypatch):
        """A record left behind would answer a woken run with the PREVIOUS
        command's output -- worse than the refusal this branch lifted."""
        server = build(monkeypatch, lambda cmd: Process(stdout=["out\n"])).tool_server
        try:
            await server._recorded.set("build", {
                "process_id": "build", "machine": "m", "command": "one",
                "exit_code": 0, "stdout": "the old output", "stderr": "",
                "started_at": "t0", "finished_at": "t1", "owner_session": "sess-1",
            })
            second = await server.execute(_call(command="two", process_id="build"))
            assert second["status"] == "success", second
            assert await server._recorded.get("build") is None
        finally:
            await server.close()

    async def test_a_ring_follows_the_entry_not_the_id(self, monkeypatch, woken):
        """A lookup BY ID would let the second command, which starts with the
        flag cleared, decide whether the FIRST command's ring is still needed."""
        server = build(monkeypatch, lambda cmd: Process(stdout=["out\n"])).tool_server
        try:
            first = await server.execute(_call(command="one", process_id="build",
                                               wake=True))
            assert first["status"] == "success", first
            assert await _until(lambda: bool(woken)), "the end was never reported"
            ring = woken[0]["still_needed"]
            assert ring() is True, "nobody has read it yet"

            second = await server.execute(_call(command="two", process_id="build",
                                                wake=True))
            assert second["status"] == "success", second
            assert server.processes.processes["build"]["command"] == "two"
            assert ring() is False
        finally:
            await server.close()
