"""A foreground ssh answer is bounded like terminal's, and keeps the end."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh

from agent_system.config.models import AgentSystemConfig, ToolServerConfig


def test_a_foreground_answer_is_bounded_and_keeps_the_end():
    """`cat big.log` returned megabytes into the model's context; the error stands last."""
    from plugins.ssh_control import tool_server

    big = "x" * (tool_server._ANSWER_STREAM_CAP * 3) + "ERROR: the reason"
    cut = tool_server._capped(big)
    assert len(cut) < tool_server._ANSWER_STREAM_CAP + 200
    assert cut.startswith("[OUTPUT TRUNCATED") and cut.endswith("ERROR: the reason")
    assert tool_server._capped("short") == "short"


def _plugin():
    from plugins.ssh_control.connection_manager import MachineConfig
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    config = ToolServerConfig()
    config.machines = []
    plugin = PLUGIN_FACTORY("ssh_control_test", AgentSystemConfig(), config)
    manager = plugin.tool_server.connection_manager
    manager.machines["m"] = MachineConfig(name="m", host="localhost", port=22, username="u", auth_method="key")
    return plugin, manager


async def test_the_answer_carries_the_cut_streams_and_says_so():
    from plugins.ssh_control import tool_server

    plugin, manager = _plugin()
    big = "x" * (tool_server._ANSWER_STREAM_CAP * 3)
    ran = SimpleNamespace(machine="m", command="cat", stdout=big, stderr=big, exit_code=0,
                          duration=0.1, ended=None)

    with patch.object(manager, "execute_command", AsyncMock(return_value=ran)):
        answer = (await plugin.tool_server.execute({"machine": "m", "command": "cat"}))["results"][0]

    assert len(answer["stdout"]) < len(big) and len(answer["stderr"]) < len(big)
    assert answer["truncated"] is True


async def _run_with(manager, plugin, completed, **params):
    """execute() through the real connection manager, the SSH run replaced by *completed*."""
    conn = MagicMock()
    conn.run = AsyncMock(return_value=completed)
    pool = MagicMock(last_latency_ms=1.0, total_commands=0)
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    pool.config.command_timeout = 5
    with patch.object(manager, "_get_pool", AsyncMock(return_value=pool)):
        return (await plugin.tool_server.execute({"machine": "m", "command": "build", **params}))["results"][0]


async def test_a_command_killed_by_a_signal_is_no_success():
    """asyncssh reports a signal as exit status -1: with check_exit_code off that was a success."""
    plugin, manager = _plugin()
    killed = asyncssh.SSHCompletedProcess(exit_status=-1, exit_signal=("KILL", False, "", ""),
                                          stdout="", stderr="")
    for check in (True, False):
        answer = await _run_with(manager, plugin, killed, check_exit_code=check)
        assert answer["success"] is False and answer["exit_code"] == -1
        assert "signal KILL" in answer["error"]


async def test_a_closed_channel_is_no_success_even_with_check_exit_code_off():
    plugin, manager = _plugin()
    dropped = asyncssh.SSHCompletedProcess(exit_status=None, stdout="", stderr="")
    answer = await _run_with(manager, plugin, dropped, check_exit_code=False)
    assert answer["success"] is False
    assert "without an exit status" in answer["error"]
    assert manager.command_history[-1]["error"] == answer["error"]


async def test_a_normal_exit_keeps_its_code():
    plugin, manager = _plugin()
    failed = asyncssh.SSHCompletedProcess(exit_status=2, stdout="", stderr="no")
    answer = await _run_with(manager, plugin, failed)
    assert answer["exit_code"] == 2 and answer["error"] == "Command failed with exit code 2"
    tolerated = await _run_with(manager, plugin, failed, check_exit_code=False)
    assert tolerated["success"] is True and "error" not in tolerated
