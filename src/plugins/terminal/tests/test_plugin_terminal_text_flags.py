"""Yes/no parameters sent as text are read as what they say.

Nothing checks tool arguments against the schema, and models send booleans as
text. Read raw, ``"false"`` is a true value: ``background: "false"`` started
the command in the background and ``force: "false"`` sent SIGKILL.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.terminal.server import TerminalServer


def _server() -> TerminalServer:
    config = MagicMock(spec=ToolServerConfig)
    config.security = {"whitelist": None, "blacklist": [], "allow_command_chains": True}
    config.limits = {"max_output_size_kb": 60, "default_timeout_seconds": 30,
                     "max_timeout_seconds": 60, "max_concurrent_background": 10}
    config.platform = {"bash_path": "auto", "initial_cwd": None}
    return TerminalServer("test", MagicMock(spec=AgentSystemConfig), config)


@pytest.mark.asyncio
async def test_background_false_as_text_runs_in_the_foreground():
    server = _server()
    try:
        result = await server.execute({"command": "echo hi", "background": "false",
                                       "_status": AsyncMock()})
        assert "process_id" not in result
        assert result["exit_code"] == 0 and "hi" in result["stdout"]
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_force_false_as_text_sends_sigterm():
    server = _server()
    try:
        started = await server.execute({"command": "sleep 30", "background": True,
                                        "process_id": "sleeper", "_status": AsyncMock()})
        assert started["status"] == "success"
        result = await server.kill_process({"process_id": "sleeper", "force": "false",
                                            "_status": AsyncMock()})
        assert result["status"] == "success"
        assert result["signal"] != "SIGKILL"
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_a_flag_that_is_no_yes_or_no_is_refused():
    server = _server()
    try:
        result = await server.execute({"command": "echo hi", "background": "maybe",
                                       "_status": AsyncMock()})
        assert result["error_type"] == "InvalidArgument"
        assert "background" in result["error"]
    finally:
        await server.cleanup()
