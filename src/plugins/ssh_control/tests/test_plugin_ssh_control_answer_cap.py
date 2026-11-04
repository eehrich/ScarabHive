"""A foreground ssh answer is bounded like terminal's."""


def test_a_foreground_answer_is_bounded():
    """`cat big.log` returned megabytes into the model's context."""
    from plugins.ssh_control import tool_server

    big = "x" * (tool_server._ANSWER_STREAM_CAP * 3)
    cut = tool_server._capped(big)
    assert len(cut) < tool_server._ANSWER_STREAM_CAP + 200 and cut.endswith("limit output]")
    assert tool_server._capped("short") == "short"


async def test_the_answer_carries_the_cut_streams_and_says_so():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.ssh_control import tool_server
    from plugins.ssh_control.connection_manager import MachineConfig
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    config = ToolServerConfig()
    config.machines = []
    plugin = PLUGIN_FACTORY("ssh_control_test", AgentSystemConfig(), config)
    manager = plugin.tool_server.connection_manager
    manager.machines["m"] = MachineConfig(name="m", host="localhost", port=22, username="u", auth_method="key")
    big = "x" * (tool_server._ANSWER_STREAM_CAP * 3)
    ran = SimpleNamespace(machine="m", command="cat", stdout=big, stderr=big, exit_code=0, duration=0.1)

    with patch.object(manager, "execute_command", AsyncMock(return_value=ran)):
        answer = (await plugin.tool_server.execute({"machine": "m", "command": "cat"}))["results"][0]

    assert len(answer["stdout"]) < len(big) and len(answer["stderr"]) < len(big)
    assert answer["truncated"] is True
