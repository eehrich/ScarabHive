"""The terminal under Seatbelt, end to end: config -> server -> bash -> kernel.

``tests/utils/test_process_sandbox_seatbelt.py`` holds the backend to its
promises; this drives the plugin the way a deployment does -- a real
``ToolServerConfig`` with a ``sandbox:`` block, the server's own executor,
``/bin/bash`` found by the plugin, and the real sandbox-exec deciding.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.terminal.server import TerminalServer
from seatbelt_rig import on_macos, probed_seatbelt, seatbelt_area

pytestmark = on_macos


class Status:
    async def update(self, msg): pass
    async def progress(self, msg): pass
    async def end(self, msg, meta=None): pass
    async def error(self, msg): pass


@pytest.fixture
def area():
    with seatbelt_area() as made, probed_seatbelt():
        yield made


def make_server(area, mode: str) -> TerminalServer:
    config = ToolServerConfig(
        type="terminal", enabled=True,
        security={"whitelist": None, "blacklist": [], "allow_command_chains": True},
        limits={}, platform={"bash_path": "auto", "initial_cwd": str(area.ws)},
        sandbox={"mode": mode})
    return TerminalServer("terminal", AgentSystemConfig(), config)


async def run(server: TerminalServer, command: str) -> dict:
    return await server.execute({"command": command, "_status": Status()})


class TestWorkspaceWrite:
    async def test_the_command_writes_inside_and_is_refused_outside(self, area):
        server = make_server(area, "workspace-write")
        try:
            result = await run(server, f"echo in > inside.txt && echo out > '{area.out}/o.txt'")
        finally:
            await server.cleanup()

        # Not a plugin error: the command ran and failed on its own.
        assert result["status"] == "success"
        assert result["exit_code"] != 0
        assert "Operation not permitted" in result["stdout"]
        assert (area.ws / "inside.txt").read_text() == "in\n"
        assert not (area.out / "o.txt").exists()

    async def test_a_heredoc_from_the_default_directory_works(self, area):
        """The pattern agents write files with. /bin/bash 3.2 falls back to the
        working directory for its temp file, and that is the workspace."""
        server = make_server(area, "workspace-write")
        try:
            result = await run(server, "cat > note.txt <<'EOF'\nfrom a heredoc\nEOF")
        finally:
            await server.cleanup()

        assert result["exit_code"] == 0, result["stdout"]
        assert (area.ws / "note.txt").read_text() == "from a heredoc\n"

    async def test_a_background_process_is_confined_too(self, area):
        """The second spawn site. Its own path through the executor."""
        server = make_server(area, "workspace-write")
        try:
            started = await server.executor.execute_background(
                f"echo in > bg_in.txt; echo out > '{area.out}/bg_out.txt'")
            assert started["status"] == "success", started
            await started["process"].communicate()
        finally:
            await server.cleanup()

        assert (area.ws / "bg_in.txt").exists()
        assert not (area.out / "bg_out.txt").exists()


class TestReadOnly:
    async def test_nothing_is_written_and_reading_still_works(self, area):
        (area.out / "readable.txt").write_text("outside content")
        server = make_server(area, "read-only")
        try:
            refused = await run(server, "echo x > inside.txt")
            read = await run(server, f"cat '{area.out}/readable.txt'")
        finally:
            await server.cleanup()

        assert refused["exit_code"] != 0
        assert not (area.ws / "inside.txt").exists()
        assert read["exit_code"] == 0 and "outside content" in read["stdout"]
