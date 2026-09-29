"""Background SSH commands: the pool slot they hold, and the wake when they end.

Against the real connection manager and the real pool, with a stand-in
connection -- the same shape the other connection tests here use. The wake
itself is replaced: ``notify`` on a session nobody holds STARTS AN agent-cli
PROCESS, which a test must never do.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.models import (AgentSystemConfig, SessionPresenceConfig,
                                        ToolServerConfig)
from plugins.ssh_control.auth import SSHAuthenticator


class Reader:
    """Hands out its lines, then reports EOF -- like a command whose output is
    done while the command itself may still run."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else ""


class Process:
    """A remote process that ends when its event is set, or at once without one."""

    def __init__(self, stdout=(), stderr=(), exit_status=0, hold=None):
        self.stdout = Reader(stdout)
        self.stderr = Reader(stderr)
        # None until the channel closes, which is what asyncssh does -- EOF on
        # the streams says nothing about the exit status. A fake that fills it
        # in at construction hides every caller that forgets to wait.
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
        # asyncssh.SSHClientProcess.close() -> None; the real one is there, so
        # this one is too. A stand-in that is missing what the library has hides
        # every caller that uses it.
        self.signals.append("close")
        if self._hold is not None:
            self._hold.set()


class Connection:
    def __init__(self, process_factory, gate=None):
        self._process_factory = process_factory
        self._gate = gate
        self.started = []

    async def run(self, command, check=False):
        return SimpleNamespace(stdout="", stderr="", exit_status=0)

    async def create_process(self, command):
        if self._gate is not None:
            await self._gate.wait()     # a channel that takes its time to open
        self.started.append(command)
        return self._process_factory(command)

    def close(self):
        pass


def build(monkeypatch, process_factory, *, presence=True, gate=None, **machine):
    """The real plugin over a stand-in connection."""
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    async def connect(*args, **kwargs):
        return Connection(process_factory, gate)

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    server_config = ToolServerConfig()
    server_config.machines = [{"name": "m", "host": "m.test", "username": "root", **machine}]
    server_config.security = {"audit_log": False}
    system_config = AgentSystemConfig()
    system_config.session_presence = SessionPresenceConfig(enabled=presence)
    return PLUGIN_FACTORY("ssh_control_test", system_config, server_config).tool_server


@pytest.fixture
def woken(monkeypatch):
    calls = []
    rang = asyncio.Event()

    async def fake_wake(system_config, session_id, user_id, what="",
                        still_needed=None):
        calls.append({"session_id": session_id, "user_id": user_id, "what": what})
        rang.set()
        return "woke_session"

    monkeypatch.setattr("plugins.ssh_control.tool_server.wake_session", fake_wake)
    return SimpleNamespace(calls=calls, rang=rang)


async def _until(predicate, timeout=10.0):
    """Wait for a state the capture task reaches on its own."""
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


async def test_a_finished_background_command_wakes_the_session(monkeypatch, woken):
    server = build(monkeypatch, lambda cmd: Process(stdout=["building\n", "done\n"]))
    try:
        result = await server.execute(_call(wake=True))
        assert result["status"] == "success", result
        assert result["wake"] is True, result

        await asyncio.wait_for(woken.rang.wait(), timeout=10)
        what = woken.calls[0]["what"]
        assert result["process_id"] in what and "m" in what and "exit 0" in what, what
        assert woken.calls[0]["session_id"] == "sess-1"

        output = await server.get_output({"process_id": result["process_id"],
                                          "_session_id": "sess-1"})
        assert output["stdout"] == "building\ndone\n", output
        assert output["is_running"] is False and output["exit_code"] == 0, output
    finally:
        await server.close()


async def test_the_connection_goes_back_when_the_command_ends(monkeypatch, woken):
    """A background command holds a pool slot for its whole life. If it kept it
    afterwards, the machine would lose a connection per command it ever ran."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]))
    try:
        pool = await server.connection_manager.pool_for("m")
        result = await server.execute(_call(wake=True))
        # Without this the test would pass on a command that never started: an
        # unused pool has no connection in use either.
        assert result["status"] == "success", result
        await asyncio.wait_for(woken.rang.wait(), timeout=10)

        assert await _until(lambda: not pool.in_use), f"still in use: {pool.in_use}"
        assert pool.has_free_slot()
    finally:
        await server.close()


async def test_the_last_connection_is_never_taken_by_a_background_command(monkeypatch):
    """With max_connections=1 there is no slot to spare, and saying so beats a
    background command that starves every ordinary one."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]), max_connections=1)
    try:
        result = await server.execute(_call())
        assert result["status"] == "error", result
        assert result["error_type"] == "BackgroundLimitReached", result
        assert "max_connections=1" in result["error"], result
    finally:
        await server.close()


async def test_two_fit_beside_an_ordinary_command_and_a_third_does_not(monkeypatch):
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()), max_connections=3)
    try:
        first = await server.execute(_call(command="one"))
        second = await server.execute(_call(command="two"))
        third = await server.execute(_call(command="three"))

        assert first["status"] == "success", first
        assert second["status"] == "success", second
        assert third["status"] == "error", third
        assert third["error_type"] == "BackgroundLimitReached", third
        assert "2 running" in third["error"], third
    finally:
        await server.close()


async def test_a_list_of_machines_is_refused_for_a_background_command(monkeypatch):
    """One answer carries one process id, and one command would take a pool slot
    on every machine at once."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]))
    try:
        result = await server.execute(_call(machine=["m", "m"]))
        assert result["status"] == "error", result
        assert result["error_type"] == "InvalidParameter", result
    finally:
        await server.close()


async def test_another_session_can_neither_read_nor_stop_it(monkeypatch):
    """Its existence and its output are not another session's business."""
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()))
    try:
        started = await server.execute(_call())
        process_id = started["process_id"]

        seen = await server.get_output({"process_id": process_id, "_session_id": "other"})
        killed = await server.kill_process({"process_id": process_id, "_session_id": "other"})
        assert seen["error_type"] == "ProcessNotFound", seen
        assert killed["error_type"] == "ProcessNotFound", killed

        mine = await server.get_output({"process_id": process_id, "_session_id": "sess-1"})
        assert mine["status"] == "success", mine
    finally:
        await server.close()


async def test_stopping_a_command_ends_it_and_frees_its_connection(monkeypatch):
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()))
    try:
        pool = await server.connection_manager.pool_for("m")
        started = await server.execute(_call())
        process_id = started["process_id"]

        killed = await server.kill_process({"process_id": process_id, "force": True,
                                            "_session_id": "sess-1"})
        assert killed["status"] == "success" and killed["signal"] == "SIGKILL", killed

        assert await _until(lambda: not pool.in_use), f"still in use: {pool.in_use}"
        output = await server.get_output({"process_id": process_id, "_session_id": "sess-1"})
        assert output["is_running"] is False, output

        # Signalling something that is already over is not an error, and it must
        # not reach the process object a second time.
        again = await server.kill_process({"process_id": process_id, "_session_id": "sess-1"})
        assert again["status"] == "success" and again["signal"] == "none", again
    finally:
        await server.close()


async def test_a_finished_command_gives_its_place_back(monkeypatch):
    """The limit counts what RUNS. Counting what ever ran would stop a machine
    from taking background commands after the second one in its lifetime."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]), max_connections=2)
    try:
        first = await server.execute(_call(command="one"))
        assert first["status"] == "success", first
        assert await _until(
            lambda: server.processes.processes[first["process_id"]]["finished_at"])

        second = await server.execute(_call(command="two"))
        assert second["status"] == "success", second
    finally:
        await server.close()


async def test_without_a_session_the_answer_says_why_there_is_no_wake(monkeypatch, woken):
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]))
    try:
        result = await server.execute(_call(wake=True, _session_id=None, _user_id=None))
        assert result["status"] == "success", result
        assert result["wake"] is False, result
        assert "no session" in result["wake_note"], result

        assert not await _until(lambda: bool(woken.calls), timeout=2), woken.calls
    finally:
        await server.close()


async def test_with_presence_off_the_answer_names_the_setting(monkeypatch, woken):
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]), presence=False)
    try:
        result = await server.execute(_call(wake=True))
        assert result["wake"] is False, result
        assert "session_presence" in result["wake_note"], result

        assert not await _until(lambda: bool(woken.calls), timeout=2), woken.calls
    finally:
        await server.close()


async def test_a_call_that_did_not_ask_hears_nothing_about_a_wake(monkeypatch, woken):
    server = build(monkeypatch, lambda cmd: Process(stdout=["x\n"]))
    try:
        result = await server.execute(_call())
        assert "wake" not in result and "wake_note" not in result, result
    finally:
        await server.close()
async def test_a_command_that_cannot_start_does_not_keep_the_slot(monkeypatch):
    """The connection is acquired before the command exists. If starting it
    fails, the slot has to go back -- otherwise the machine loses one
    connection per failed attempt until nothing can run on it at all."""
    def refuse(command):
        raise OSError("channel refused")

    server = build(monkeypatch, refuse)
    try:
        pool = await server.connection_manager.pool_for("m")
        result = await server.execute(_call())
        assert result["status"] == "error", result
        assert "channel refused" in result["error"], result
        assert not pool.in_use, f"slot kept: {pool.in_use}"
        assert pool.has_free_slot()
        # The place is claimed before the channel is opened, so a failed start
        # has to give it back too -- otherwise the limit counts a command that
        # never ran, for as long as the process lives.
        assert server.processes.processes == {}, server.processes.processes
    finally:
        await server.close()


async def test_an_id_that_is_taken_does_not_replace_a_running_command(monkeypatch):
    """Replacing the entry would leave the command behind it running with
    nobody able to read its output or stop it."""
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()))
    try:
        first = await server.execute(_call(command="one", process_id="mine"))
        assert first["status"] == "success", first

        second = await server.execute(_call(command="two", process_id="mine"))
        assert second["status"] == "error", second
        assert second["error_type"] == "ProcessIdInUse", second

        still_there = await server.get_output({"process_id": "mine",
                                               "_session_id": "sess-1"})
        assert still_there["command"] == "one", still_there
        assert still_there["is_running"] is True, still_there
    finally:
        await server.close()


async def test_finished_commands_do_not_pile_up_forever(monkeypatch):
    """Each entry holds two line buffers. A job starting one every few minutes
    would grow this dict for as long as the process lives."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x" + chr(10)]),
                   max_connections=3)
    try:
        server.processes.keep_finished = 2
        ids = []
        for number in range(4):
            started = await server.execute(_call(command=f"run {number}"))
            assert started["status"] == "success", started
            ids.append(started["process_id"])
            assert await _until(
                lambda: server.processes.processes[ids[-1]]["finished_at"])

        kept = set(server.processes.processes)
        # The last one has not been pruned yet -- pruning happens at the next
        # start -- so three of four survive and the oldest is the one that went.
        assert ids[0] not in kept, kept
        assert ids[-1] in kept and ids[-2] in kept, kept
    finally:
        await server.close()


async def test_the_status_row_survives_a_long_machine_name(monkeypatch, woken):
    """The machine name and the process id come from the caller, so the budget
    is kept on the whole row and not guessed from the command."""
    class Recorder:
        def __init__(self):
            self.ended = []

        async def progress(self, msg, meta=None):
            pass

        async def end(self, msg, meta=None):
            self.ended.append(msg)

        async def error(self, msg, meta=None):
            self.ended.append(msg)

    long_name = "m" * 200
    server = build(monkeypatch, lambda cmd: Process(stdout=["x" + chr(10)]),
                   name=long_name)
    try:
        status = Recorder()
        result = await server.execute(_call(wake=True, _status=status,
                                            machine=long_name,
                                            command="make " + "a" * 400))
        assert result["status"] == "success", result
        assert len(status.ended) == 1, status.ended
        row = status.ended[0]
        assert len(row) <= 140, (len(row), row)
        # The length alone is no test -- it would hold for any blind cut. What
        # has to survive a caller-sized machine name is the OUTCOME.
        assert result["process_id"] in row, row
        assert "wakes this session" in row, row
        assert "make" in row, row
    finally:
        await server.close()
async def test_the_one_that_just_ended_is_never_the_one_pruned(monkeypatch):
    """A long command started first can end last. Pruning in START order would
    drop exactly the entry whose caller has just been woken for it -- which
    reaches that caller as ProcessNotFound."""
    held = asyncio.Event()
    server = build(monkeypatch,
                   lambda cmd: Process(hold=held) if cmd == "long" else Process(),
                   max_connections=3)
    try:
        server.processes.keep_finished = 1
        long_one = await server.execute(_call(command="long"))
        short = await server.execute(_call(command="short"))
        assert long_one["status"] == "success" and short["status"] == "success"
        assert await _until(
            lambda: server.processes.processes[short["process_id"]]["finished_at"])

        held.set()
        assert await _until(
            lambda: server.processes.processes[long_one["process_id"]]["finished_at"])

        await server.execute(_call(command="next"))
        kept = set(server.processes.processes)
        assert long_one["process_id"] in kept, kept
        assert short["process_id"] not in kept, kept
    finally:
        held.set()
        await server.close()


async def test_simultaneous_starts_cannot_all_take_the_last_slot(monkeypatch):
    """Tool calls run in parallel. A check followed by an await and only then by
    the registration is read by every simultaneous call as "nothing runs yet",
    and all of them take a connection -- the one thing the check exists for."""
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()),
                   max_connections=3)
    try:
        results = await asyncio.gather(
            server.execute(_call(command="one")),
            server.execute(_call(command="two")),
            server.execute(_call(command="three")),
        )
        started = [r for r in results if r["status"] == "success"]
        refused = [r for r in results if r["status"] == "error"]
        assert len(started) == 2, results
        assert len(refused) == 1, results
        assert refused[0]["error_type"] == "BackgroundLimitReached", refused[0]

        pool = await server.connection_manager.pool_for("m")
        assert pool.has_free_slot(), "no connection left for an ordinary command"
    finally:
        await server.close()


async def test_a_foreground_call_that_asked_for_a_wake_is_told_there_is_none(monkeypatch, woken):
    """The one case that used to be answered with nothing at all."""
    server = build(monkeypatch, lambda cmd: Process())
    try:
        result = await server.execute({"machine": "m", "command": "uptime", "wake": True,
                                       "_session_id": "sess-1", "_user_id": "someone"})
        assert result["wake"] is False, result
        assert "background=true" in result["wake_note"], result
        assert woken.calls == []
    finally:
        await server.close()


async def test_closing_waits_for_the_capture_tasks(monkeypatch):
    """Signalling is not waiting: close_all() replaces the pool's semaphore, and
    a capture task releasing afterwards frees the new one instead."""
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()))
    started = await server.execute(_call())
    assert started["status"] == "success", started
    assert server.processes._tasks, "no capture task was kept"

    await server.close()
    assert all(task.done() for task in server.processes._tasks), server.processes._tasks


async def test_a_command_stopped_before_its_channel_opens_gives_everything_back(monkeypatch):
    """Between claiming a place and having a channel there is no process to
    signal and no capture task to wait for. Stopping such a command has to take
    its place back, and the start that comes out of the channel has to notice
    and let the connection go -- otherwise a shutdown tears the pool down under
    it."""
    opening = asyncio.Event()
    server = build(monkeypatch, lambda cmd: Process(hold=asyncio.Event()), gate=opening)
    try:
        pool = await server.connection_manager.pool_for("m")
        starting = asyncio.ensure_future(server.execute(_call(process_id="mine")))
        assert await _until(lambda: "mine" in server.processes.processes)
        assert server.processes.processes["mine"]["process"] is None

        stopped = await server.kill_process({"process_id": "mine", "_session_id": "sess-1"})
        assert stopped["status"] == "success", stopped
        assert stopped["signal"] == "none" and "before it started" in stopped["note"], stopped

        opening.set()
        result = await starting
        assert result["status"] == "error", result
        assert result["error_type"] == "StoppedBeforeStart", result
        assert server.processes.processes == {}, server.processes.processes
        assert await _until(lambda: not pool.in_use), f"slot kept: {pool.in_use}"
    finally:
        opening.set()
        await server.close()


async def test_closing_lets_go_of_a_wake_that_is_still_ringing(monkeypatch):
    """The ring loop lives INSIDE the capture task and can take minutes. Waiting
    for it on shutdown stalls the close; letting it run on means a server that
    is already gone can still spawn a wake. It is waited for, then cancelled."""
    ringing = asyncio.Event()

    async def endless_wake(system_config, session_id, user_id, what="", still_needed=None):
        ringing.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("plugins.ssh_control.tool_server.wake_session", endless_wake)
    server = build(monkeypatch, lambda cmd: Process(stdout=["x" + chr(10)]))
    started = await server.execute(_call(wake=True))
    assert started["status"] == "success", started

    await asyncio.wait_for(ringing.wait(), timeout=20)
    tasks = set(server.processes._tasks)
    assert tasks and not all(task.done() for task in tasks), tasks

    await server.close()
    assert all(task.done() for task in tasks), tasks


async def test_a_new_process_reads_what_the_old_one_recorded(monkeypatch, woken):
    """The run that gets woken is a NEW process with an empty registry. Without
    the record it asks for a process id nobody there has ever seen."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["done" + chr(10)]))
    try:
        started = await server.execute(_call(wake=True))
        assert started["status"] == "success", started
        await asyncio.wait_for(woken.rang.wait(), timeout=20)
    finally:
        await server.close()

    woken_run = build(monkeypatch, lambda cmd: Process())
    try:
        assert woken_run.processes.processes == {}, "not a fresh registry"
        out = await woken_run.get_output({"process_id": started["process_id"],
                                          "_session_id": "sess-1"})
        assert out["status"] == "success", out
        assert out["source"] == "recorded", out
        assert out["stdout"] == "done" + chr(10), out
        assert out["machine"] == "m" and out["exit_code"] == 0, out
        assert out["is_running"] is False, out

        again = await woken_run.get_output({"process_id": started["process_id"],
                                            "_session_id": "sess-1"})
        assert again["status"] == "error", again
    finally:
        await woken_run.close()


async def test_another_session_cannot_read_a_recorded_result(monkeypatch, woken):
    """The live registry hides a foreign command; the record has to hide it too."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x" + chr(10)]))
    try:
        started = await server.execute(_call(wake=True))
        await asyncio.wait_for(woken.rang.wait(), timeout=20)
    finally:
        await server.close()

    other = build(monkeypatch, lambda cmd: Process())
    try:
        out = await other.get_output({"process_id": started["process_id"],
                                      "_session_id": "somebody-else"})
        assert out["status"] == "error", out
        assert out["error_type"] == "ProcessNotFound", out
    finally:
        await other.close()


async def test_without_a_wake_nothing_is_written_to_disk(monkeypatch, woken,
                                                         isolated_plugin_cache):
    """A caller polling from this process never needs the record."""
    server = build(monkeypatch, lambda cmd: Process(stdout=["x" + chr(10)]))
    try:
        started = await server.execute(_call())
        assert await _until(
            lambda: server.processes.processes[started["process_id"]]["finished_at"])
        assert woken.calls == []
        assert list(isolated_plugin_cache.rglob("*.json")) == []
    finally:
        await server.close()
