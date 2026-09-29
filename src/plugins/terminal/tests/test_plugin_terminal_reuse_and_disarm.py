"""What happens to an id, a record and an armed wake once the work is over.

Three things the wake commit left open, all found by review: a process that is
killed went on ringing, a process_id was taken for the life of the server once
it had been used, and the copy written for a woken run was only ever dropped by
the ONE read that does not happen in the process that ran the command.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.models import SessionPresenceConfig
from plugins.terminal.server import TerminalServer


@pytest.fixture
def presence_on():
    return SimpleNamespace(session_presence=SessionPresenceConfig(enabled=True))


class Quiet:
    """A status sink that keeps only what a test asserts on."""

    def __init__(self):
        self.errors = []

    async def update(self, msg, meta=None):
        pass

    async def progress(self, msg, meta=None):
        pass

    async def end(self, msg, meta=None):
        pass

    async def error(self, msg, meta=None):
        self.errors.append(msg)


@pytest.fixture
def no_real_wake(monkeypatch):
    """The ring is core and would start an agent-cli process. Replaced by a
    recorder that also keeps the still_needed callback, because that callback
    IS what two of these tests are about."""
    calls = []

    async def fake_wake(system_config, session_id, user_id, what="",
                        still_needed=None):
        calls.append({"what": what, "still_needed": still_needed})
        return "woke_session"

    monkeypatch.setattr("plugins.terminal.server.wake_session", fake_wake)
    return calls


async def _start(server, status, **extra):
    params = {"command": "echo hello", "background": True, "_status": status,
              "_session_id": "sess-1", "_user_id": "someone"}
    params.update(extra)
    return await server.execute(params)


async def _wait_until_finished(server, process_id, timeout=20.0):
    """The capture task reaps the child; finished_at is set only after that."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        info = server.process_manager.processes.get(process_id)
        if info is not None and info["finished_at"] is not None:
            return info
        await asyncio.sleep(0.05)
    raise AssertionError(f"{process_id} never finished")


class TestKillingItIsDealingWithIt:
    """`still_needed` was driven by get_output alone, and nobody reads the
    output of a command they just ended."""

    @pytest.mark.asyncio
    async def test_killing_a_process_stops_the_ring(self, presence_on, no_real_wake):
        server = TerminalServer("test", presence_on, {})
        try:
            status = Quiet()
            started = await _start(server, status, command="sleep 30", wake=True)
            assert started["status"] == "success", started
            pid = started["process_id"]

            killed = await server.kill_process(
                {"process_id": pid, "_status": Quiet(), "_session_id": "sess-1"})
            assert killed["status"] == "success", killed

            # The ring asks this on every retry; True would keep it going for
            # five minutes and then start a run to report an ordered death.
            info = server.process_manager.processes[pid]
            assert info["read_after_finish"] is True
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_killing_one_that_just_ended_stops_the_ring_too(self, presence_on, no_real_wake):
        """The race a wake invites: the process ends, the session sends its
        kill a moment later. The answer is ProcessAlreadyTerminated -- and the
        session has dealt with it all the same."""
        server = TerminalServer("test", presence_on, {})
        try:
            started = await _start(server, Quiet(), command="echo done", wake=True)
            pid = started["process_id"]
            info = await _wait_until_finished(server, pid)
            info["read_after_finish"] = False   # nobody has read it yet

            killed = await server.kill_process(
                {"process_id": pid, "_status": Quiet(), "_session_id": "sess-1"})

            assert killed.get("error_type") == "ProcessAlreadyTerminated", killed
            assert info["read_after_finish"] is True
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_the_callback_the_ring_holds_agrees(self, presence_on, no_real_wake):
        """Not the flag but the callback that reads it -- that is what core calls."""
        server = TerminalServer("test", presence_on, {})
        try:
            started = await _start(server, Quiet(), command="sleep 30", wake=True)
            pid = started["process_id"]

            # The wake fires only when the process ENDS, so kill it and let the
            # capture task report; then the recorded callback must say "done".
            await server.kill_process(
                {"process_id": pid, "_status": Quiet(), "_session_id": "sess-1"})
            info = await _wait_until_finished(server, pid)

            # Two orderings are possible and both are right: the kill can win
            # the race, and the capture task then skips the ring entirely; or
            # the capture task reports first, and the ring it left must say it
            # is no longer needed. Asserting only one of them would be flaky.
            assert info["read_after_finish"] is True
            assert all(call["still_needed"]() is False for call in no_real_wake), \
                [call["what"] for call in no_real_wake]
        finally:
            await server.cleanup()


class TestAnIdIsFreeAgainOnceItsProcessIsOver:
    """The tool invites a stable id; nothing ever removed an entry."""

    @pytest.mark.asyncio
    async def test_a_finished_id_can_be_used_again(self, presence_on):
        server = TerminalServer("test", presence_on, {})
        try:
            first = await _start(server, Quiet(), process_id="build")
            assert first["status"] == "success", first
            await _wait_until_finished(server, "build")

            second = await _start(server, Quiet(), process_id="build")
            assert second["status"] == "success", second
            assert second["process_id"] == "build"
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_a_running_id_is_still_refused(self, presence_on):
        """The reason the guard exists: replacing a running entry would leave
        the process alive with nobody able to read or kill it."""
        server = TerminalServer("test", presence_on, {})
        try:
            first = await _start(server, Quiet(), command="sleep 30", process_id="dev")
            assert first["status"] == "success", first

            status = Quiet()
            second = await _start(server, status, command="sleep 30", process_id="dev")
            assert second["status"] == "error", second
            assert second["error_type"] == "ProcessIdInUse"
            assert status.errors, "the refusal has to reach the status row"
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_reusing_an_id_drops_what_was_recorded_under_it(self, presence_on):
        """Otherwise get_output answers for the NEW process with the OLD
        result -- worse than the refusal this lifted."""
        server = TerminalServer("test", presence_on, {})
        try:
            await server._recorded.set("build", {"process_id": "build",
                                                 "exit_code": 99, "stdout": "stale"})
            first = await _start(server, Quiet(), process_id="build")
            assert first["status"] == "success"
            await _wait_until_finished(server, "build")

            second = await _start(server, Quiet(), process_id="build")
            assert second["status"] == "success", second
            assert await server._recorded.get("build") is None
        finally:
            await server.cleanup()


class TestTheRecordIsHandedOverOrDropped:

    @pytest.mark.asyncio
    async def test_reading_a_finished_process_live_drops_its_record(
            self, presence_on, no_real_wake):
        """_recall deletes it, but _recall runs only when the live registry
        MISSES -- never in the process that ran the command."""
        server = TerminalServer("test", presence_on, {})
        try:
            started = await _start(server, Quiet(), wake=True)
            pid = started["process_id"]
            await _wait_until_finished(server, pid)
            # on_finish writes the record; give the callback its turn.
            for _ in range(200):
                if await server._recorded.get(pid) is not None:
                    break
                await asyncio.sleep(0.05)
            assert await server._recorded.get(pid) is not None, "nothing was recorded"

            answer = await server.get_output(
                {"process_id": pid, "_status": Quiet(), "_session_id": "sess-1"})
            assert answer["status"] == "success", answer
            assert answer.get("source") != "recorded", "this must be the LIVE answer"
            assert await server._recorded.get(pid) is None
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_a_running_process_keeps_its_record(self, presence_on, no_real_wake):
        """The drop is bound to "it is over", not to "somebody looked": a poll
        while it runs must not throw away what a woken run would need."""
        server = TerminalServer("test", presence_on, {})
        try:
            started = await _start(server, Quiet(), command="sleep 30", wake=True)
            pid = started["process_id"]
            await server._recorded.set(pid, {"process_id": pid, "exit_code": None})

            answer = await server.get_output(
                {"process_id": pid, "_status": Quiet(), "_session_id": "sess-1"})
            assert answer["is_running"] is True, answer
            assert await server._recorded.get(pid) is not None
        finally:
            await server.cleanup()


@pytest.mark.asyncio
async def test_stop_plugin_is_the_name_the_framework_calls(presence_on):
    """plugins/capabilities.stop_plugin is the only shutdown hook the adapter
    knows. Without this name, cleanup() was unreachable and every background
    process outlived its run."""
    server = TerminalServer("test", presence_on, {})
    called = []
    server.cleanup = lambda: called.append(True) or asyncio.sleep(0)
    await server.stop_plugin()
    assert called == [True]


class TestSomethingAlreadyDealtWithIsNotReportedAgain:
    """The kill sets the flag; the capture task reaches the callback after it.
    Called directly, because which of the two gets there first is a race and
    this is about the branch, not about the winner."""

    @pytest.mark.asyncio
    async def test_no_record_and_no_ring_for_a_process_already_read(
            self, presence_on, no_real_wake):
        server = TerminalServer("test", presence_on, {})
        try:
            on_finish, note = server._wake_callback(
                {"_session_id": "sess-1", "_user_id": "someone"})
            assert on_finish is not None, note

            server.process_manager.processes["done"] = {
                # A finished child, as register_process would leave it: cleanup
                # walks every entry and asks this object for its returncode.
                "process": SimpleNamespace(returncode=0),
                "command": "echo hi", "exit_code": 0, "read_after_finish": True,
                "stdout_buffer": ["hi"], "stderr_buffer": [],
                "started_at": "t0", "finished_at": "t1", "owner_session": "sess-1",
            }
            await on_finish("done", server.process_manager.processes["done"])

            assert no_real_wake == [], no_real_wake
            assert await server._recorded.get("done") is None
        finally:
            await server.cleanup()

    @pytest.mark.asyncio
    async def test_a_ring_follows_the_entry_not_the_id(self, presence_on,
                                                       no_real_wake):
        """An id is free again once its process is over, so a lookup BY ID
        would let a later run decide whether an older ring is still needed --
        and that later run starts with the flag cleared."""
        server = TerminalServer("test", presence_on, {})
        try:
            first = await _start(server, Quiet(), process_id="build", wake=True)
            assert first["status"] == "success", first
            await _wait_until_finished(server, "build")
            for _ in range(200):
                if no_real_wake:
                    break
                await asyncio.sleep(0.05)
            assert no_real_wake, "the end of the first run was never reported"
            ring = no_real_wake[0]["still_needed"]
            assert ring() is True, "nobody has read it yet"

            second = await _start(server, Quiet(), process_id="build", wake=True)
            assert second["status"] == "success", second

            # The second run is a fresh entry with the flag cleared. The first
            # run's ring must not read it: its own outcome is gone from the id.
            assert (server.process_manager.processes["build"]["read_after_finish"]
                    is False)
            assert ring() is False
        finally:
            await server.cleanup()


class TestARecalledResultHonoursStream:

    @pytest.mark.asyncio
    async def test_stream_stderr_does_not_carry_stdout_back(self, presence_on):
        """A woken run asking for stderr alone is avoiding 60 000 characters
        of stdout; handing both over spends exactly what it saved."""
        server = TerminalServer("test", presence_on, {})
        try:
            await server._recorded.set("gone", {
                "process_id": "gone", "command": "make", "exit_code": 1,
                "stdout": "a mountain of stdout", "stderr": "the one line",
                "started_at": "t0", "finished_at": "t1", "owner_session": "sess-1",
            })
            out = await server.get_output({"process_id": "gone", "stream": "stderr",
                                           "_status": Quiet(),
                                           "_session_id": "sess-1"})
            assert out["status"] == "success", out
            assert out["source"] == "recorded", out
            assert out["stderr"] == "the one line", out
            assert out["stdout"] == "", out
        finally:
            await server.cleanup()


def _finished_entry(owner="sess-1", read=False):
    return {
        "process": SimpleNamespace(returncode=0),
        "command": "echo hi", "exit_code": 0, "read_after_finish": read,
        "stdout_buffer": ["hi"], "stderr_buffer": [],
        "started_at": "t0", "finished_at": "t1", "owner_session": owner,
    }


class TestTheRingAnswersForItsOwnEntry:
    @pytest.mark.asyncio
    async def test_a_later_run_under_the_same_id_does_not_answer_for_it(
            self, presence_on, no_real_wake):
        """The id was reused while the capture task was still draining: the
        old entry is disarmed, the new one is armed. A lookup by id rang for
        the NEW run's entry."""
        server = TerminalServer("test", presence_on, {})
        try:
            on_finish, note = server._wake_callback(
                {"_session_id": "sess-1", "_user_id": "someone"})
            assert on_finish is not None, note
            old = _finished_entry(read=True)
            server.process_manager.processes["build"] = _finished_entry(read=False)

            await on_finish("build", old)

            assert no_real_wake == [], no_real_wake
            assert await server._recorded.get("build") is None
        finally:
            await server.cleanup()


class TestAnotherSessionsIdStaysTheirs:
    @pytest.mark.asyncio
    async def test_reusing_it_is_refused_and_leaves_their_wake_and_record(
            self, presence_on):
        server = TerminalServer("test", presence_on, {})
        try:
            theirs = _finished_entry(owner="sess-1", read=False)
            server.process_manager.processes["build"] = theirs
            await server._recorded.set("build", {"process_id": "build", "stdout": "theirs"})
            status = Quiet()

            result = await _start(server, status, process_id="build",
                                  _session_id="sess-2")

            assert result["error_type"] == "ProcessIdInUse", result
            assert server.process_manager.processes["build"] is theirs
            assert theirs["read_after_finish"] is False
            assert (await server._recorded.get("build"))["stdout"] == "theirs"
        finally:
            await server.cleanup()


class TestTheirRecordCountsAsMuchAsTheirEntry:
    @pytest.mark.asyncio
    async def test_a_record_with_no_live_entry_still_belongs_to_them(self, presence_on):
        """The entry lives in another process (or is gone after a restart);
        the record is what their woken run reads, and a new run here would
        overwrite it."""
        server = TerminalServer("test", presence_on, {})
        try:
            await server._recorded.set("build", {"process_id": "build", "stdout": "theirs",
                                                 "owner_session": "sess-1"})

            result = await _start(server, Quiet(), process_id="build", _session_id="sess-2")

            assert result.get("error_type") == "ProcessIdInUse", result
            assert "build" not in server.process_manager.processes
            assert (await server._recorded.get("build"))["stdout"] == "theirs"
        finally:
            await server.cleanup()


class _Pipe:
    async def readline(self):
        return b""


class _HeldChild:
    def __init__(self):
        self.stdout, self.stderr = _Pipe(), _Pipe()
        self.returncode = None
        self.release = asyncio.Event()

    async def wait(self):
        await self.release.wait()
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_the_capture_task_reports_the_entry_it_was_started_for():
    """The id is handed to a later run while the first one is still being
    captured: its end must be told with ITS entry, not the later one's."""
    from plugins.terminal.process_manager import ProcessManager

    manager = ProcessManager()
    told = []

    async def on_finish(process_id, info):
        told.append(info)

    child = _HeldChild()
    await manager.register_process(child, "echo one", process_id="build",
                                   on_finish=on_finish)
    first = manager.processes["build"]
    manager.processes["build"] = {"command": "echo two", "process": _HeldChild()}
    child.release.set()
    for _ in range(100):
        if told:
            break
        await asyncio.sleep(0.01)

    assert told and told[0] is first
    assert first["exit_code"] == 0


@pytest.mark.asyncio
async def test_two_calls_with_one_id_start_one_process(presence_on):
    """Both used to pass the check -- awaits lie between it and the
    registration -- and the second registration replaced the entry of the
    first process, which ran on with nobody able to read or kill it."""
    server = TerminalServer("test", presence_on, {})
    try:
        results = await asyncio.gather(
            _start(server, Quiet(), process_id="build"),
            _start(server, Quiet(), process_id="build"))

        assert sorted(r["status"] for r in results) == ["error", "success"], results
        assert [r.get("error_type") for r in results if r["status"] == "error"] == ["ProcessIdInUse"]
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_not_taken_over(presence_on, monkeypatch):
    server = TerminalServer("test", presence_on, {})
    try:
        async def broken(key):
            raise OSError("disk says no")

        monkeypatch.setattr(server._recorded, "get", broken)

        result = await _start(server, Quiet(), process_id="build")

        assert result.get("error_type") == "ProcessIdInUse", result
        assert "leave process_id out" in result["error"], result
        assert "build" not in server.process_manager.processes
    finally:
        await server.cleanup()
