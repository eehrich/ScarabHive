"""Connection slots and failure reasons, against the real connection manager with a stand-in connection.

A cancelled call must give its slot back, a busy machine must not stall the panel's pings, and an agent must get
the reason a failed run is recorded with -- asyncssh's connect_timeout and wait_for raise a bare TimeoutError.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_system.config.models import AgentSystemConfig, MCPConfig
from plugins.ssh_control.auth import SSHAuthenticator


class Connection:
    def __init__(self, ping_delay: float = 0.0):
        self.ping_delay = ping_delay

    async def run(self, command, check=False):
        if command == "echo 1":
            await asyncio.sleep(self.ping_delay)
        if command == "hang":
            await asyncio.sleep(10)
        return SimpleNamespace(stdout=f"ran {command}\n", stderr="", exit_status=0)

    def close(self):
        pass


def plugin(monkeypatch, connect, **machine):
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    config = MCPConfig()
    config.machines = [{"name": "m", "host": "m.test", "username": "root", **machine}]
    config.security = {"audit_log": False}
    return PLUGIN_FACTORY("ssh_control_test", AgentSystemConfig(), config)


async def test_an_agent_gets_the_reason_of_a_timeout(monkeypatch):
    async def blackholed(*args, **kwargs):
        raise TimeoutError()  # what asyncssh's connect_timeout raises

    server = plugin(monkeypatch, blackholed, command_timeout=1).mcp_server
    connect_timeout = (await server.execute({"machine": "m", "command": "uptime"}))["results"][0]

    async def reachable(*args, **kwargs):
        return Connection()

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(reachable))
    command_timeout = (await server.execute({"machine": "m", "command": "hang"}))["results"][0]

    assert connect_timeout["error"] == "Connection timed out"
    assert command_timeout["error"] == "Timed out after 1s"
    assert [entry["error"] for entry in server.command_history] == ["Connection timed out", "Timed out after 1s"]


async def test_a_call_cancelled_while_connecting_gives_its_slot_back(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    manager = plugin(monkeypatch, slow, max_connections=1).mcp_server.connection_manager
    call = asyncio.create_task(manager.execute_command("m", "uptime"))
    await asyncio.sleep(0.1)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert manager.pools["m"].has_free_slot()


async def test_a_call_cancelled_during_the_first_latency_ping_gives_its_slot_back(monkeypatch):
    async def lagging(*args, **kwargs):
        return Connection(ping_delay=0.5)

    manager = plugin(monkeypatch, lagging, max_connections=1).mcp_server.connection_manager
    call = asyncio.create_task(manager.execute_command("m", "uptime"))
    await asyncio.sleep(0.1)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    pool = manager.pools["m"]
    assert pool.has_free_slot() and not pool.in_use


async def test_a_command_started_just_before_the_pings_does_not_stall_them(monkeypatch):
    async def reachable(*args, **kwargs):
        return Connection()

    web = plugin(monkeypatch, reachable, max_connections=1, command_timeout=2).web_endpoints
    manager = web.server.connection_manager
    await manager.execute_command("m", "uptime")  # connected once: an active look pings it

    # ready before the pings start, it takes the only slot first
    command = asyncio.create_task(manager.execute_command("m", "hang"))
    started = time.monotonic()
    listed = await web.list_machines(None, active=True)
    waited = time.monotonic() - started
    with pytest.raises(asyncio.TimeoutError):
        await command

    assert waited < 1, f"the pings waited {waited:.1f}s for the command's slot"
    assert listed["machines"][0]["error"] is None
