"""The SSH Machines panel in a real browser, against the real plugin: its router, its connection manager, its tools,
its machine store, its static files. Only the SSH connection is a stand-in, where the plugin opens one.

Configured: ``alpha`` (deploy@alpha.test:22, tags web and prod, commands time out after 2 s), ``beta``
(root@beta.test:2222), ``gamma`` (down.test, which refuses every connection) and ``stuck`` (stuck.test, whose
connections cannot be closed: removing it fails). alpha has a single connection slot. A connection to any other host
is opened after 0.3 s (to slow.* after 1.5 s); one to a blackholed host times out after 0.3 s.

What the stand-in answers: ``hostname`` the host, ``false`` "failed" on stderr with exit 1, ``sleep`` after 1.5 s,
``hang`` never (in time), ``big`` 12 KB, anything else ``ran <command>``.

Behind the panel's back: POST /__stub/agent-run?machine= runs ``uptime`` there, POST /__stub/agent-add adds the
machine ``rack/epsilon``, POST /__stub/forget?name= drops a machine, POST /__stub/break?host= makes a host refuse
connections, POST /__stub/blackhole?host= makes them time out, POST /__stub/mend?host= undoes both, POST /__stub/clear removes every machine. GET /__stub/asked
counts the machine lists asked for, with and without ``active``, and the machines added; GET
/__stub/runs?machine=&command= counts the runs recorded; GET /__stub/stored names the machines in the store; GET
/__stub/password?name= says whether a machine holds a password. With the cookie ``ssh_machines=fails`` the machine list
fails, with ``ssh_machines=slow`` it takes 1.5 s, with ``ssh_machines=slowfail`` it fails after 1.5 s;
``ssh_slow=<machine>`` delays that machine's history by 1.5 s, ``ssh_history_fails=<machine>`` fails it (after the
delay); ``ssh_execute=gateway`` answers every run 502 before it reaches the plugin.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
MACHINES = [
    {"name": "alpha", "host": "alpha.test", "username": "deploy", "tags": ["web", "prod"], "command_timeout": 2,
     "max_connections": 1},
    {"name": "beta", "host": "beta.test", "port": 2222, "username": "root"},
    {"name": "gamma", "host": "down.test", "username": "root"},
    {"name": "stuck", "host": "stuck.test", "username": "root"},
]


class Connection:
    def __init__(self, host: str, broken: set[str]):
        self.host = host
        self.broken = broken

    async def run(self, command: str, check: bool = False):
        if self.host in self.broken:
            return SimpleNamespace(stdout="", stderr="", exit_status=255)
        if command == "echo 1":
            await asyncio.sleep(0.02)
        if command in ("sleep", "hang"):
            await asyncio.sleep(1.5 if command == "sleep" else 10)
        if command == "false":
            return SimpleNamespace(stdout="", stderr="failed\n", exit_status=1)
        stdout = {"hostname": f"{self.host}\n", "big": "x" * 12000}.get(command, f"ran {command}\n")
        return SimpleNamespace(stdout=stdout, stderr="", exit_status=0)

    def close(self):
        if self.host == "stuck.test":
            raise RuntimeError("The connection to stuck.test will not close")

    async def wait_closed(self):
        pass


def panel_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from plugins.ssh_control import machine_store
    from plugins.ssh_control.auth import SSHAuthenticator
    from plugins.ssh_control.plugin import PLUGIN_FACTORY

    monkeypatch.setattr(machine_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(machine_store, "LEGACY_PATH", tmp_path / "legacy_mcp.yaml")
    broken = {"down.test"}
    blackholed: set[str] = set()

    async def connect(machine_config, known_hosts_file=None, strict_host_key_checking=True):
        if machine_config.host in blackholed:
            await asyncio.sleep(0.3)
            raise TimeoutError()  # what asyncssh's connect_timeout raises
        if machine_config.host in broken:
            raise OSError(f"Connection refused by {machine_config.host}")
        if machine_config.host not in {m["host"] for m in MACHINES}:
            await asyncio.sleep(1.5 if machine_config.host.startswith("slow") else 0.3)
        return Connection(machine_config.host, broken)

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(connect))
    config = MCPConfig()
    config.machines = MACHINES
    config.security = {"audit_log": False}
    plugin = PLUGIN_FACTORY("ssh_control", AgentSystemConfig(), config)
    manager = plugin.mcp_server.connection_manager
    app = FastAPI()
    asked = {"lazy": 0, "active": 0, "added": 0}

    @app.middleware("http")
    async def stand_ins(request: Request, call_next):
        path = request.url.path
        cookies = request.cookies
        if request.method == "GET" and path == "/plugins/ssh_control/api/machines":
            asked["active" if request.query_params.get("active") == "true" else "lazy"] += 1
            if cookies.get("ssh_machines") in ("slow", "slowfail"):
                await asyncio.sleep(1.5)
            if cookies.get("ssh_machines") in ("fails", "slowfail"):
                return JSONResponse({"detail": "The machine list is locked"}, status_code=500)
        if path.startswith("/plugins/ssh_control/api/machines/") and path.endswith("/history"):
            machine = path.split("/")[-2]
            if cookies.get("ssh_slow") == machine:
                await asyncio.sleep(1.5)
            if cookies.get("ssh_history_fails") == machine:
                return JSONResponse({"detail": "The history is locked"}, status_code=500)
        if request.method == "POST" and path == "/plugins/ssh_control/api/machines":
            asked["added"] += 1
        if path == "/plugins/ssh_control/api/execute" and cookies.get("ssh_execute") == "gateway":
            return JSONResponse({"detail": "The gateway is down"}, status_code=502)
        return await call_next(request)

    @app.get("/__stub/asked")
    async def lists_asked():
        return asked

    @app.get("/__stub/runs")
    async def runs(machine: str, command: str):
        return sum(1 for e in plugin.command_history if e["machine"] == machine and e["command"] == command)

    @app.get("/__stub/stored")
    async def stored():
        return [m["name"] for m in machine_store.load("ssh_control")]

    @app.get("/__stub/password")
    async def password(name: str):
        return manager.machines[name].password is not None

    @app.post("/__stub/agent-run")
    async def agent_run(machine: str):
        await manager.execute_command(machine, "uptime")
        return {}

    @app.post("/__stub/forget")
    async def forget(name: str):
        del manager.machines[name]
        return {}

    @app.post("/__stub/mend")
    async def mend(host: str):
        broken.discard(host)
        blackholed.discard(host)
        return {}

    @app.post("/__stub/agent-add")
    async def agent_add():
        await plugin.mcp_server.add_machine({"name": "rack/epsilon", "host": "epsilon.test", "username": "root"})
        return {}

    @app.post("/__stub/break")
    async def break_host(host: str):
        broken.add(host)
        return {}

    @app.post("/__stub/blackhole")
    async def blackhole(host: str):
        blackholed.add(host)
        return {}

    @app.post("/__stub/clear")
    async def clear():
        manager.machines.clear()
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("ssh_control", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ssh_control", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(tmp_path_factory.mktemp("ssh_panel"), monkeypatch)
        yield run_app_test_page(BROWSER, app, "tests/ssh_control/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened, the panel lists the machines with their state and shows the first one, without a credential',
    'a command runs on the machine shown once, its output and the connection drawn',
    'a failed command, an unreachable machine and timeouts are drawn as failures, with their reason',
    'a run the plugin never saw leaves no line behind',
    'commands, output and names are drawn as text, never as markup',
    'switching machines shows each one its history, and a history answered late is dropped; the keyboard stays on the tabs',
    'the output follows new lines only while it is scrolled to the end',
    'adding tests the connection, shows a refusal in the dialog and keeps the machine if asked; a late answer leaves a new dialog alone',
    'removing asks first, removes for good, and says what it could not remove',
    'a failed load shows the error and nothing of the machines shown before',
    'a click on refresh pings the machines connected to before, a tick only looks, and a busy machine is not waited for',
    'an answer overtaken by a later load is dropped',
    'a tick of the auto refresh leaves a load still on its way alone',
    'the auto refresh runs from the start and brings what an agent runs and adds, a name with a slash too',
    'with no machines the panel says so',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_ssh_control_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
