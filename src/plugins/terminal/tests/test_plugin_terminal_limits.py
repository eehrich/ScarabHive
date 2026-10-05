"""The limits the terminal applies: timeout, output caps, background count, cancellation.

Each test here once failed against the code before it: a cancelled command ran
on, a null timeout raised, a foreground output was held whole in memory, a
background line over 64 KiB stalled its process for good, the configured
background limit was never read, and an unknown stream answered "no output".
Only harmless commands run (echo, printf, sleep).
"""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config import ToolServerConfig
from plugins.terminal import executor as executor_module
from plugins.terminal.executor import CommandExecutor
from plugins.terminal.server import TerminalServer


def _server(**limits) -> TerminalServer:
    config = MagicMock(spec=ToolServerConfig)
    config.security = {}
    config.limits = limits
    config.platform = {"bash_path": "auto", "initial_cwd": None}
    return TerminalServer("test", {}, config)


def _status():
    status = AsyncMock()
    status.progress = AsyncMock()
    status.error = AsyncMock()
    status.end = AsyncMock()
    return status


@pytest.fixture
def spawned(monkeypatch):
    """Every process the executor spawns, in order."""
    processes = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        process = await real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(executor_module.asyncio, "create_subprocess_exec", spy)
    return processes


@pytest.mark.asyncio
async def test_a_cancelled_command_is_killed(spawned):
    """The framework force-cancels a tool call; the command used to run on,
    held by nobody, out of reach of kill_process. A chain, so that killing the
    shell alone leaves the sleep holding the pipe and the answer waits."""
    server = _server()
    try:
        task = asyncio.create_task(server.execute_command(
            {"command": "echo a; sleep 8; echo b", "timeout": 60, "_status": _status()}))
        for _ in range(100):
            if spawned:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert time.monotonic() - started < 4
        assert spawned[0].returncode is not None
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_graceful_cancel_ends_the_command_at_once():
    """Only the force-cancel, 30 s later, used to reach a running command."""
    from agent_system.core.cancellation import CancellationToken

    server = _server()
    token = CancellationToken("test")
    try:
        task = asyncio.create_task(server.execute_command(
            {"command": "echo a; sleep 8; echo b", "timeout": 60,
             "_cancellation_token": token, "_status": _status()}))
        await asyncio.sleep(0.5)
        started = time.monotonic()
        token.cancel()
        result = await task

        assert result["status"] == "cancelled", result
        assert time.monotonic() - started < 4
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_timeout_ends_what_the_shell_started():
    """Killing the shell alone left the chain's sleep running, and the answer
    waited for it: measured 8 s on Windows and 6 s on Linux for timeout 1."""
    server = _server()
    try:
        started = time.monotonic()
        result = await server.execute_command(
            {"command": "echo a; sleep 8; echo b", "timeout": 1, "_status": _status()})
        assert result["error_type"] == "TimeoutError", result
        assert time.monotonic() - started < 4
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_kill_process_ends_what_the_shell_started():
    server = _server()
    try:
        start = await server.execute({"command": "echo a; sleep 8; echo b",
                                      "background": True, "_status": _status()})
        await asyncio.sleep(0.5)
        started = time.monotonic()
        result = await server.kill_process({"process_id": start["process_id"],
                                            "_status": _status()})
        assert result["status"] == "success", result
        assert time.monotonic() - started < 4
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_relative_cwd_is_taken_from_the_start_directory(tmp_path):
    """It was taken from the server's working directory, which is the
    checkout once the CLIs enter it -- not where the commands run."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "marker.txt").write_text("x")
    server = _server()
    server.executor.initial_cwd = str(tmp_path)
    try:
        result = await server.execute_command(
            {"command": "ls", "cwd": "sub", "timeout": 30, "_status": _status()})
        assert result["status"] == "success", result
        assert "marker.txt" in result["stdout"]
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_timeout_answers_with_the_output_so_far():
    """The output lived in the reading task and died with it."""
    server = _server()
    try:
        result = await server.execute_command(
            {"command": "echo before; sleep 8; echo after", "timeout": 1.5, "_status": _status()})
        assert result["error_type"] == "TimeoutError", result
        assert "before" in result["stdout"] and "after" not in result["stdout"], result
        assert result["truncated"] is False
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_cancel_answers_with_the_output_so_far():
    from agent_system.core.cancellation import CancellationToken

    server = _server()
    token = CancellationToken("test")
    try:
        task = asyncio.create_task(server.execute_command(
            {"command": "echo before; sleep 8; echo after", "timeout": 60,
             "_cancellation_token": token, "_status": _status()}))
        await asyncio.sleep(1.5)
        token.cancel()
        result = await task
        assert result["status"] == "cancelled", result
        assert "before" in result["stdout"], result
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_null_timeout_takes_the_default():
    server = _server()
    try:
        result = await server.execute_command(
            {"command": "echo hi", "timeout": None, "_status": _status()})
        assert result["status"] == "success", result
        assert result["stdout"].strip() == "hi"
    finally:
        await server.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", ["30", 0, -5, True])
async def test_a_timeout_that_is_no_positive_number_is_refused_before_spawning(timeout, spawned):
    server = _server()
    try:
        result = await server.execute_command(
            {"command": "echo hi", "timeout": timeout, "_status": _status()})
        assert result["error_type"] == "InvalidTimeout", result
        assert spawned == []
    finally:
        await server.cleanup()


class _Chunks:
    def __init__(self, count: int, size: int):
        self.left, self.size, self.reads = count, size, 0

    async def read(self, n):
        self.reads += 1
        if not self.left:
            return b""
        self.left -= 1
        return b"a" * min(n, self.size)


@pytest.mark.asyncio
async def test_the_foreground_reader_keeps_no_more_than_the_cap_and_drains_the_rest():
    process = MagicMock()
    process.stdout = _Chunks(count=10, size=65536)
    process.wait = AsyncMock(return_value=0)

    kept, more = await CommandExecutor._read_capped(process, 100_000)

    assert len(kept) == 100_000
    assert more is True
    assert process.stdout.reads == 11  # read to the end: a full pipe blocks the command


@pytest.mark.asyncio
async def test_a_background_line_over_64_kib_does_not_stall_the_process():
    server = _server()
    try:
        start = await server.execute({
            "command": "printf '%*s' 100000 x; echo; echo after-the-long-line",
            "background": True, "_status": _status()})
        assert start["status"] == "success", start
        result = None
        for _ in range(150):
            result = await server.get_output({"process_id": start["process_id"],
                                              "_status": _status()})
            if not result["is_running"]:
                break
            await asyncio.sleep(0.1)

        assert result["is_running"] is False, "the process stalled on a full pipe"
        assert "after-the-long-line" in result["stdout"]
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_the_background_limit_is_applied():
    server = _server(max_concurrent_background=1)
    try:
        first = await server.execute({"command": "sleep 20", "background": True,
                                      "_status": _status()})
        assert first["status"] == "success", first

        second = await server.execute({"command": "echo hi", "background": True,
                                       "_status": _status()})

        assert second["error_type"] == "TooManyProcesses", second
        assert len(server.process_manager.processes) == 1
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_starts_at_the_same_moment_do_not_pass_the_limit_together():
    """Awaits lie between the count and the registration."""
    server = _server(max_concurrent_background=1)
    try:
        results = await asyncio.gather(*(
            server.execute({"command": "sleep 20", "background": True, "_status": _status()})
            for _ in range(3)))

        assert sorted(r["status"] for r in results) == ["error", "error", "success"], results
        assert len(server.process_manager.processes) == 1
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_the_background_limit_counts_per_session_under_a_host_cap():
    """Counted over all sessions, a few leftovers another session cannot see
    or kill locked every session out."""
    server = _server(max_concurrent_background=1)

    async def start(session):
        return await server.execute({"command": "sleep 20", "background": True,
                                     "_session_id": session, "_status": _status()})
    try:
        for session in ("A", "B", "C", "D"):
            assert (await start(session))["status"] == "success", session

        again = await start("A")
        assert again["error_type"] == "TooManyProcesses", again
        assert "of this session" in again["error"] and "kill_process" in again["error"]

        over_cap = await start("E")
        assert over_cap["error_type"] == "TooManyProcesses", over_cap
        assert "none of them is this session's" in over_cap["error"], over_cap
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_null_stream_reads_both():
    server = _server()
    try:
        server.process_manager.processes["log"] = _finished_entry("output")
        result = await server.get_output({"process_id": "log", "stream": None,
                                          "_status": _status()})
        assert result["status"] == "success", result
        assert result["stdout"] == "output"
    finally:
        await server.cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="the job object is Windows only")
async def test_a_finished_background_process_lets_go_of_its_job():
    server = _server()
    try:
        start = await server.execute({"command": "echo hi", "background": True,
                                      "_status": _status()})
        entry = server.process_manager.processes[start["process_id"]]
        assert hasattr(entry["process"], "_terminal_job")
        for _ in range(100):
            if entry["finished_at"]:
                break
            await asyncio.sleep(0.05)
        assert entry["finished_at"]
        assert not hasattr(entry["process"], "_terminal_job")
    finally:
        await server.cleanup()


@pytest.fixture
def a_secret_from_the_file(monkeypatch):
    """TERMINAL_PROBE_SECRET as if the server had read it from secrets.env,
    TERMINAL_PROBE_PLAIN as set by the real environment."""
    from agent_system.config import settings

    monkeypatch.setenv("TERMINAL_PROBE_SECRET", "s3cret")
    monkeypatch.setenv("TERMINAL_PROBE_PLAIN", "plain")
    monkeypatch.setitem(settings._secrets_from_file, settings._env_name("TERMINAL_PROBE_SECRET"),
                        settings._fingerprint("s3cret"))
    # The list a start hands on of the file's names, with value fingerprints.
    monkeypatch.setenv(settings.SECRETS_FROM_FILE_ENV,
                       f"TERMINAL_PROBE_SECRET:{settings._fingerprint('s3cret')}")


@pytest.mark.asyncio
@pytest.mark.parametrize("pass_secrets, expected", [
    (None, "[][plain][]"), (True, "[s3cret][plain][TERMINAL_PROBE_SECRET:")])
async def test_secrets_from_the_files_stay_out_of_the_command(a_secret_from_the_file,
                                                              pass_secrets, expected):
    config = MagicMock(spec=ToolServerConfig)
    config.security = {} if pass_secrets is None else {"pass_secrets_env": pass_secrets}
    config.limits = {}
    config.platform = {"bash_path": "auto", "initial_cwd": None}
    server = TerminalServer("test", {}, config)
    try:
        result = await server.execute_command({
            "command": 'echo "[$TERMINAL_PROBE_SECRET][$TERMINAL_PROBE_PLAIN][$HIVE_SECRETS_FROM_FILE]"',
            "timeout": 30, "_status": _status()})
        assert result["stdout"].strip().startswith(expected), result
    finally:
        await server.cleanup()


def _finished_entry(stdout: str) -> dict:
    process = MagicMock()
    process.returncode = 0
    return {"process": process, "command": "x", "cwd": None, "owner_session": None,
            "started_at": "t0", "finished_at": "t1", "exit_code": 0,
            "stdout_buffer": [stdout], "stderr_buffer": [], "read_after_finish": False,
            "on_finish": None}


@pytest.mark.asyncio
async def test_get_output_returns_the_tail_within_the_cap():
    server = _server(max_output_size_kb=1)
    try:
        server.process_manager.processes["log"] = _finished_entry("head" + "x" * 3000 + "tail")

        result = await server.get_output({"process_id": "log", "_status": _status()})

        assert len(result["stdout"]) == 1024
        assert result["stdout"].endswith("tail")
        assert result["truncated"] is True
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_an_unknown_stream_is_refused_not_answered_empty():
    server = _server()
    try:
        server.process_manager.processes["log"] = _finished_entry("output\n")

        result = await server.get_output({"process_id": "log", "stream": "all",
                                          "_status": _status()})

        assert result["error_type"] == "InvalidStream", result
    finally:
        await server.cleanup()
