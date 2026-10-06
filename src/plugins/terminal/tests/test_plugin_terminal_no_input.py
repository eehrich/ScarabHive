"""A command gets nothing to read: what it would ask for -- PowerShell's mandatory parameter, Read-Host, `read` --
ends with its error at once, instead of prompting in the terminal agent-cli runs in and taking the person's keys.

The test process's stdin holds a line, as the terminal holds what the person types: a command that inherited it
would read that line.
"""
import asyncio
import os

import pytest

from plugins.terminal.server import TerminalServer

READS = 'read -r line; echo "read:[$line] rc=$?"'


class _Status:
    async def update(self, msg): pass
    async def progress(self, msg): pass
    async def end(self, msg, meta=None): pass
    async def error(self, msg): pass


@pytest.fixture
def typed_on_stdin():
    """This process's stdin -- what a command would inherit -- holds a line the person typed."""
    read, write = os.pipe()
    os.write(write, b"typed by the person\n")
    os.close(write)
    saved = os.dup(0)
    os.dup2(read, 0)
    os.close(read)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)


async def test_a_command_reads_nothing_from_the_terminal(typed_on_stdin):
    server = TerminalServer("test", {}, {})
    try:
        result = await server.execute({"command": READS, "timeout": 20, "_status": _Status()})
    finally:
        await server.cleanup()

    assert "read:[] rc=1" in result["stdout"], result


async def test_a_background_command_gets_an_input_that_stays_open_and_brings_nothing(typed_on_stdin):
    """A watcher that stops once its input closes (esbuild, tailwind --watch) keeps running in the background;
    the end of its input comes when its shell is released."""
    from plugins.terminal.executor import release_job

    server = TerminalServer("test", {}, {})
    try:
        started = await server.executor.execute_background(READS)
        process = started["process"]
        await asyncio.sleep(1.0)
        assert process.returncode is None, "the background command's input ended at once"
        release_job(process)
        out, _ = await asyncio.wait_for(process.communicate(), 20)
    finally:
        await server.cleanup()

    assert "read:[] rc=1" in out.decode("utf-8", "replace"), out
