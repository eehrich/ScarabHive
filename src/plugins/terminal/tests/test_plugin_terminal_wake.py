"""A background process that ends tells the session that started it.

The wake itself is core (``core/session_presence.py``) and is replaced here:
``notify`` on a session nobody holds STARTS AN agent-cli PROCESS, which a test
must never do. What is tested is the plugin's side -- whether the end is
reported at all, with which session, and what the caller is told when there is
nothing to wake.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.models import SessionPresenceConfig
from plugins.terminal.server import TerminalServer


@pytest.fixture
def presence_on():
    """A system config whose session presence is on. ``presence_for`` reads the
    attribute and checks its type, so nothing less than the real model does."""
    return SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))


@pytest.fixture
def presence_off():
    return SimpleNamespace(session_presence=SessionPresenceConfig(enabled=False))


class RecordingStatus:
    """Keeps what the tool reported, so the row budget can be measured."""

    def __init__(self):
        self.ended = []
        self.errors = []

    async def update(self, msg, meta=None):
        pass

    async def progress(self, msg, meta=None):
        pass

    async def end(self, msg, meta=None):
        self.ended.append(msg)

    async def error(self, msg, meta=None):
        self.errors.append(msg)


@pytest.fixture
def woken(monkeypatch):
    """Replaces the core wake and records every call, with an event per call so
    no test has to sleep for a process it can wait on."""
    calls = []
    rang = asyncio.Event()

    async def fake_wake(system_config, session_id, user_id, what="",
                        still_needed=None):
        calls.append({"session_id": session_id, "user_id": user_id, "what": what})
        rang.set()
        return "woke_session"

    monkeypatch.setattr("plugins.terminal.server.wake_session", fake_wake)
    return SimpleNamespace(calls=calls, rang=rang)


async def _run_background(server, status, **extra):
    params = {"command": "echo hello", "background": True, "_status": status,
              "_session_id": "sess-1", "_user_id": "someone"}
    params.update(extra)
    return await server.execute(params)


@pytest.mark.asyncio
async def test_a_finished_background_process_wakes_the_session(presence_on, woken):
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status, wake=True)
        assert result["status"] == "success", result
        assert result["wake"] is True, result

        await asyncio.wait_for(woken.rang.wait(), timeout=20)
        assert len(woken.calls) == 1, woken.calls
        call = woken.calls[0]
        assert call["session_id"] == "sess-1"
        assert call["user_id"] == "someone"
        # The log line has to say WHICH process ended and how; a bare "a
        # process finished" is not traceable in a run with several.
        assert result["process_id"] in call["what"]
        assert "exit 0" in call["what"], call["what"]
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_failed_background_process_wakes_the_session_too(presence_on, woken):
    """A caller waiting on a job waits just as long when it fails."""
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status, command="exit 3", wake=True)
        assert result["wake"] is True, result

        await asyncio.wait_for(woken.rang.wait(), timeout=20)
        assert "exit 3" in woken.calls[0]["what"], woken.calls[0]["what"]
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_without_a_session_the_answer_says_why_there_is_no_wake(presence_on, woken):
    """The dangerous case: a model that asked to be woken and was not would end
    its turn and wait for a message that never comes."""
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status, wake=True,
                                       _session_id=None, _user_id=None)
        assert result["status"] == "success", result
        assert result["wake"] is False, result
        assert "no session" in result["wake_note"], result

        # Give the process every chance to end and ring anyway.
        await asyncio.sleep(2)
        assert woken.calls == []
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_with_presence_off_the_answer_names_the_setting(presence_off, woken):
    server = TerminalServer("test", presence_off, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status, wake=True)
        assert result["wake"] is False, result
        assert "session_presence" in result["wake_note"], result

        await asyncio.sleep(2)
        assert woken.calls == []
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_call_that_did_not_ask_hears_nothing_about_a_wake(presence_on, woken):
    """An answer about a wake nobody wanted is noise in every background call."""
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status)
        assert "wake" not in result, result
        assert "wake_note" not in result, result

        await asyncio.sleep(2)
        assert woken.calls == []
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_the_status_row_stays_within_its_budget_with_the_wake(presence_on, woken):
    """terminal caps the command at 70 so one row stays readable. The wake note
    goes in FRONT of the command, so it must take its space from it."""
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        long_command = "echo " + "a" * 400
        result = await _run_background(server, status, command=long_command, wake=True)
        assert result["wake"] is True, result
        assert len(status.ended) == 1, status.ended
        line = status.ended[0]
        assert "wakes this session" in line, line
        assert len(line) <= 140, f"{len(line)}: {line}"
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_wake_that_throws_does_not_cost_the_output(presence_on, monkeypatch, caplog):
    """Reporting the end must never break the capture it reports on: the buffers
    and the exit code are the caller's only way back to a finished process.

    And the failure has to be SAID: a wake that quietly went missing leaves an
    operator with a session that never woke and no trace of why."""
    rang = asyncio.Event()

    async def exploding_wake(system_config, session_id, user_id, what="",
                             still_needed=None):
        rang.set()
        raise RuntimeError("the wake is broken")

    monkeypatch.setattr("plugins.terminal.server.wake_session", exploding_wake)

    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status, wake=True)
        process_id = result["process_id"]

        await asyncio.wait_for(rang.wait(), timeout=20)
        # The callback runs after the buffers are filled and the exit code is
        # stored, so both are readable the moment it has blown up.
        output = await server.get_output({"process_id": process_id, "_status": status,
                                          "_session_id": "sess-1"})
        assert output["status"] == "success", output
        assert "hello" in output["stdout"], output
        assert output["exit_code"] == 0, output

        assert process_id in caplog.text and "the wake is broken" in caplog.text, caplog.text
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_an_id_that_is_taken_does_not_replace_a_running_process(presence_on, woken):
    """Replacing the entry would leave a real OS process running with nobody
    able to read its output or kill it."""
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        first = await _run_background(server, status, command="sleep 30",
                                      process_id="mine")
        assert first["status"] == "success", first

        second = await _run_background(server, status, command="echo other",
                                       process_id="mine")
        assert second["status"] == "error", second
        assert second["error_type"] == "ProcessIdInUse", second
        assert server.process_manager.processes["mine"]["command"] == "sleep 30"
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_the_status_row_survives_a_process_id_the_caller_chose(presence_on, woken):
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await _run_background(server, status, wake=True,
                                       process_id="p" * 300,
                                       command="echo " + "a" * 400)
        assert result["status"] == "success", result
        assert len(status.ended) == 1, status.ended
        row = status.ended[0]
        assert len(row) <= 140, (len(row), row)
        # The length alone would hold for any blind cut. What has to survive a
        # caller-sized process id is the OUTCOME.
        assert f"PID {result['pid']}" in row, row
        assert "wakes this session" in row, row
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_foreground_call_that_asked_for_a_wake_is_told_there_is_none(presence_on, woken):
    """The one case that used to be answered with nothing at all -- neither
    armed nor refused -- while every other combination says which it is."""
    server = TerminalServer("test", presence_on, {})
    try:
        status = RecordingStatus()
        result = await server.execute({"command": "echo hello", "wake": True,
                                       "_status": status, "_session_id": "sess-1",
                                       "_user_id": "someone"})
        assert result["status"] == "success", result
        assert "hello" in result["stdout"], result
        assert result["wake"] is False, result
        assert "background=true" in result["wake_note"], result
        assert woken.calls == []
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_cleanup_lets_go_of_a_wake_that_is_still_ringing(presence_on, monkeypatch):
    """Same as its ssh twin: the ring loop runs inside the capture task, and a
    server that is closing has nothing left to wake anybody about."""
    ringing = asyncio.Event()

    async def endless_wake(system_config, session_id, user_id, what="", still_needed=None):
        ringing.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("plugins.terminal.server.wake_session", endless_wake)
    server = TerminalServer("test", presence_on, {})
    status = RecordingStatus()
    result = await _run_background(server, status, wake=True)
    assert result["status"] == "success", result

    await asyncio.wait_for(ringing.wait(), timeout=20)
    tasks = set(server.process_manager._tasks)
    assert tasks and not all(task.done() for task in tasks), tasks

    await server.cleanup()
    assert all(task.done() for task in tasks), tasks


@pytest.mark.asyncio
async def test_a_new_process_reads_what_the_old_one_recorded(presence_on, woken):
    """The run that gets woken is a NEW process: fresh tool servers, empty
    registry. Without the record it asks for a process id nobody there has ever
    seen, and the wake reaches a run that cannot read what it was woken for."""
    first = TerminalServer("test", presence_on, {})
    try:
        started = await _run_background(first, RecordingStatus(), wake=True)
        await asyncio.wait_for(woken.rang.wait(), timeout=20)
    finally:
        await first.cleanup()

    woken_run = TerminalServer("test", presence_on, {})
    try:
        assert woken_run.process_manager.processes == {}, "not a fresh registry"
        out = await woken_run.get_output({"process_id": started["process_id"],
                                          "_status": RecordingStatus(),
                                          "_session_id": "sess-1"})
        assert out["status"] == "success", out
        assert out["source"] == "recorded", out
        assert "hello" in out["stdout"], out
        assert out["exit_code"] == 0 and out["is_running"] is False, out

        # Handed over, not kept: a second reader finds nothing.
        again = await woken_run.get_output({"process_id": started["process_id"],
                                            "_status": RecordingStatus(),
                                            "_session_id": "sess-1"})
        assert again["status"] == "error", again
    finally:
        await woken_run.cleanup()


@pytest.mark.asyncio
async def test_another_session_cannot_read_a_recorded_result(presence_on, woken):
    """The live registry hides a foreign process; the record has to hide it too,
    or the isolation ends where the memory does."""
    first = TerminalServer("test", presence_on, {})
    try:
        started = await _run_background(first, RecordingStatus(), wake=True)
        await asyncio.wait_for(woken.rang.wait(), timeout=20)
    finally:
        await first.cleanup()

    other = TerminalServer("test", presence_on, {})
    try:
        out = await other.get_output({"process_id": started["process_id"],
                                      "_status": RecordingStatus(),
                                      "_session_id": "somebody-else"})
        assert out["status"] == "error", out
        assert out["error_type"] == "ProcessNotFound", out
    finally:
        await other.cleanup()


@pytest.mark.asyncio
async def test_without_a_wake_nothing_is_written_to_disk(presence_on, woken,
                                                          isolated_plugin_cache):
    """A caller that polls from this process never needs the record, and a file
    per background command would be a write nobody reads."""
    server = TerminalServer("test", presence_on, {})
    try:
        started = await _run_background(server, RecordingStatus())
        assert await _finished(server, started["process_id"])
        assert woken.calls == []
        files = list(isolated_plugin_cache.rglob("*.json"))
        assert files == [], files
    finally:
        await server.cleanup()


async def _finished(server, process_id, timeout=20.0):
    for _ in range(int(timeout / 0.02)):
        if server.process_manager.processes[process_id]["finished_at"]:
            return True
        await asyncio.sleep(0.02)
    return False
