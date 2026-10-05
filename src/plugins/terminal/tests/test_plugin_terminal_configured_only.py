"""A whitelisted terminal runs its commands in its configured directory and environment.

A whitelist checks the command string and nothing else. The working directory
decides which files the one allowed command reads and writes, the environment
how it runs (PYTHONPATH, PYTHONSTARTUP, PATH, LD_PRELOAD: code of the caller's
choosing inside the allowed command). Taken from the model they undo the
whitelist, so a whitelisted instance refuses both -- at the one place every
spawn passes (CommandExecutor.refusal), so no entry into the server skips it --
and does not offer them in its schema. An instance without a whitelist keeps
both.

The spawn itself is replaced by a recorder: a process that would start is a
call to it, and the call raises instead of starting anything.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.terminal import executor as executor_module
from plugins.terminal.server import TerminalServer

WHITELIST = [r"^echo hi$"]


class _Status:
    async def progress(self, message): pass
    async def end(self, message, meta=None): pass
    async def error(self, message): pass


class _Spawned(Exception):
    """Raised by the recorder where a process would have started."""


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Builds a terminal (its plugin cache under tmp_path)."""
    from agent_system.plugins import cache as cache_module

    monkeypatch.setattr(cache_module, "default_cache_root", lambda: tmp_path / "cache")

    def build(whitelist):
        security = {"whitelist": whitelist} if whitelist else {}
        return TerminalServer("tt", AgentSystemConfig(),
                              ToolServerConfig(type="terminal", enabled=True, security=security))
    return build


@pytest.fixture
def spawned(monkeypatch):
    """Every process the executor would start, with the cwd and environment it would get."""
    calls = []

    async def record(*argv, **kwargs):
        calls.append({"cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        raise _Spawned("recorded, not started")

    monkeypatch.setattr(executor_module.asyncio, "create_subprocess_exec", record)
    return calls


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("given", [{"cwd": "/tmp"}, {"env_vars": {"PYTHONPATH": "/tmp/elsewhere"}}])
async def test_a_whitelisted_terminal_refuses_a_directory_or_environment_and_spawns_nothing(
        server, spawned, background, given):
    terminal = server(WHITELIST)

    answer = await terminal.execute({"command": "echo hi", "background": background, "_status": _Status(), **given})

    assert answer["status"] == "error" and answer["error_type"] == "ConfiguredOnly", answer
    assert next(iter(given)) in answer["error"], answer
    assert spawned == [], "something was spawned"


def _entries(terminal):
    """Every way into a spawn, each given a working directory."""
    params = {"command": "echo hi", "cwd": "/tmp", "_status": _Status()}
    return {
        "execute_command": lambda: terminal.execute_command(dict(params)),
        "execute_background": lambda: terminal.execute_background(dict(params)),
        "_start_background": lambda: terminal._start_background(dict(params)),
        "executor.execute": lambda: terminal.executor.execute(command="echo hi", cwd="/tmp"),
        "executor.execute_background": lambda: terminal.executor.execute_background(command="echo hi", cwd="/tmp"),
    }


@pytest.mark.parametrize("entry", ["execute_command", "execute_background", "_start_background",
                                   "executor.execute", "executor.execute_background"])
async def test_no_entry_skips_the_refusal(server, spawned, entry):
    answer = await _entries(server(WHITELIST))[entry]()

    assert answer["error_type"] == "ConfiguredOnly", answer
    assert spawned == []


#: How a process gets started: (module, function). Counted as calls in the parsed source, so a comment that
#: names one does not count, and a spawn written in any of these forms does.
SPAWN_CALLS = frozenset({
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("subprocess", "Popen"), ("subprocess", "run"), ("subprocess", "call"),
    ("subprocess", "check_output"), ("subprocess", "check_call"),
    ("os", "system"), ("os", "popen"), ("pty", "spawn"),
})
#: Imported bare, these names say what they do; ``run``, ``call`` and ``system`` would not.
BARE_SPAWN_NAMES = frozenset({"create_subprocess_exec", "create_subprocess_shell", "Popen", "check_output",
                              "check_call"})


def _calls(module, match):
    """How many calls in *module*'s source have a callee *match* accepts."""
    import ast
    import inspect

    return sum(1 for node in ast.walk(ast.parse(inspect.getsource(module)))
               if isinstance(node, ast.Call) and match(node.func))


def _is_spawn(func):
    import ast

    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr) in SPAWN_CALLS
    return isinstance(func, ast.Name) and func.id in BARE_SPAWN_NAMES


def _spawn_sites(module):
    return _calls(module, _is_spawn)


def test_every_spawn_site_asks_the_refusal_first():
    """Anti-drift: a spawn site added later, in whatever form, has to ask CommandExecutor.refusal as well."""
    import ast

    spawns = _spawn_sites(executor_module)
    asks = _calls(executor_module, lambda func: isinstance(func, ast.Attribute) and func.attr == "refusal"
                  and isinstance(func.value, ast.Name) and func.value.id == "self")
    assert spawns == asks == 2, f"{spawns} spawn sites, {asks} of them ask the refusal"


async def test_a_refused_background_command_leaves_a_finished_process_record_alone(server, spawned, monkeypatch):
    """Taking over a finished process's id drops its recorded outcome; a command that will not run must not
    cost that."""
    terminal = server(WHITELIST)
    finished = {"process": SimpleNamespace(returncode=0), "owner_session": "S"}
    terminal.process_manager.processes["build"] = finished
    dropped = []

    async def delete(process_id):
        dropped.append(process_id)

    monkeypatch.setattr(terminal._recorded, "delete", delete)

    answer = await terminal.execute({"command": "echo hi", "background": True, "process_id": "build",
                                     "cwd": "/tmp", "_session_id": "S", "_status": _Status()})

    assert answer["error_type"] == "ConfiguredOnly", answer
    assert dropped == [] and "read_after_finish" not in finished, "the finished process's record was dropped"
    assert spawned == []


async def test_a_whitelisted_terminal_runs_the_command_as_configured(server, spawned):
    import os

    terminal = server(WHITELIST)

    await terminal.execute({"command": "echo hi", "_status": _Status()})

    assert [call["cwd"] for call in spawned] == [terminal.executor.initial_cwd]
    # The server's environment, less what came from the secrets files.
    assert spawned[0]["env"] == terminal.executor._environment(None)
    assert set(spawned[0]["env"]) <= set(os.environ)


@pytest.mark.parametrize("background", [False, True])
async def test_a_terminal_without_a_whitelist_keeps_both(server, spawned, background):
    terminal = server(None)

    elsewhere = os.path.abspath(os.sep + "tmp")  # absolute on every platform
    await terminal.execute({"command": "echo hi", "background": background, "cwd": elsewhere,
                            "env_vars": {"A": "1"}, "_status": _Status()})

    [call] = spawned
    assert (call["cwd"], call["env"].get("A")) == (elsewhere, "1"), call


@pytest.mark.parametrize("whitelist, offered", [(WHITELIST, False), (None, True)])
async def test_the_schema_offers_them_only_without_a_whitelist(server, whitelist, offered):
    tools = await server(whitelist).list_tools()
    [execute] = [tool for tool in tools if tool.name == "tt_execute"]

    properties = execute.input_schema["properties"]
    assert ("cwd" in properties, "env_vars" in properties) == (offered, offered), sorted(properties)
    assert "command" in properties and "background" in properties
