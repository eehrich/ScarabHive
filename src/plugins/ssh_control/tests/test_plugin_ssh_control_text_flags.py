"""Yes/no parameters sent as text are read as what they say.

Nothing checks tool arguments against the schema, and models send booleans as
text. Read raw, ``"false"`` is a true value: ``background: "false"`` started
the command in the background and ``force: "false"`` sent SIGKILL. No machine
is contacted: the parts that would are replaced by recorders.
"""
from unittest.mock import AsyncMock

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.ssh_control.plugin import PLUGIN_FACTORY


def _server():
    config = ToolServerConfig()
    config.machines = []
    return PLUGIN_FACTORY("ssh_flags_test", AgentSystemConfig(), config).tool_server


@pytest.mark.asyncio
async def test_background_false_as_text_runs_in_the_foreground():
    server = _server()
    server._execute_background = AsyncMock(return_value={"status": "success"})
    result = await server.execute({"machine": "nowhere", "command": "true", "background": "false"})
    server._execute_background.assert_not_called()
    assert "results" in result


@pytest.mark.asyncio
async def test_force_false_as_text_is_no_sigkill():
    server = _server()
    server.processes.kill_process = AsyncMock(return_value={"status": "success", "signal": "SIGTERM"})
    await server.kill_process({"process_id": "p", "force": "false"})
    assert server.processes.kill_process.await_args.kwargs["force"] is False


@pytest.mark.asyncio
async def test_clear_buffer_false_as_text_keeps_the_buffer():
    server = _server()
    server.processes.get_output = AsyncMock(return_value={
        "status": "success", "is_running": True, "stdout": "", "stderr": "", "machine": "m"})
    await server.get_output({"process_id": "p", "clear_buffer": "false"})
    assert server.processes.get_output.await_args.kwargs["clear_buffer"] is False


@pytest.mark.asyncio
async def test_a_flag_that_is_no_yes_or_no_is_refused():
    server = _server()
    result = await server.remove_machine({"name": "x", "remove_from_config": "maybe"})
    assert result["error_type"] == "InvalidArgument" and result["success"] is False
    assert "remove_from_config" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [None, "", "  "])
async def test_a_blank_flag_keeps_its_default(blank):
    """check_exit_code defaults to yes; blank text must not turn it into a no."""
    from plugins.ssh_control.tool_server import _flags

    params = {"check_exit_code": blank}
    assert await _flags(params, {"check_exit_code": True}) is None
    assert params["check_exit_code"] is True
