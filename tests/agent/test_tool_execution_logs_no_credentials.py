"""A tool call's credentials reach the tool, not the log.

Every tool call is logged at INFO with its parameters: an ssh_control add_machine
password went into api.log in clear. The tool still gets it.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from tool_execution_test_helpers import execute_tools_collect

PASSWORD = "Pr0d-R00t-Passw0rd!"


class CapturingServer:
    def __init__(self):
        self.received = None

    async def call_with_status(self, tool_name, params):
        self.received = params
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_the_password_reaches_the_tool_but_not_the_log(caplog):
    server = CapturingServer()
    agent = MagicMock()
    agent.name = "test_agent"
    agent._get_server_from_any_registry.return_value = server
    agent.agent_config = SimpleNamespace(timeouts=None)
    registry = MagicMock()
    registry.list.return_value = []
    manager = ToolExecutionManager(registry, agent=agent)
    arguments = {"name": "db1", "host": "192.0.2.10", "username": "root", "auth_method": "password",
                 "password": PASSWORD}
    call = {"id": "call-1", "type": "function",
            "function": {"name": "ssh_control_add_machine", "arguments": json.dumps(arguments)}}

    with caplog.at_level(logging.INFO, logger="agent_system.servers.agent.components.tool_execution"):
        await execute_tools_collect(manager, [call],
                                    tool_name_mapping={"ssh_control_add_machine": "ssh_control_add_machine"},
                                    available_tools=["ssh_control_add_machine"], step=0)

    assert server.received is not None and server.received["password"] == PASSWORD
    assert "Invoking tool ssh_control_add_machine" in caplog.text, caplog.text
    assert PASSWORD not in caplog.text
