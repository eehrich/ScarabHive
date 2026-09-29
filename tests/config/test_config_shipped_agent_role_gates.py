"""Every shipped agent granted a shell, code execution or file access to what holds secrets is admin-only.

A shell (terminal, coding CLI, SSH), a tool that runs arbitrary code in a
program (blender_execute: bpy Python; godot_script: GDScript with OS.execute),
or file access to the checkout, to config/, to data/ itself (the user store and
every user's sessions) or write access to src/ (the code that runs) gives
whoever runs the agent the logs, config/secrets.env, data/users.db, other
users' sessions or commands on the configured hosts. Its role
gate (metadata.min_role, auth/agent_access.py) decides who that is. This loads
the shipped configuration (secrets stubbed out: no key enters the process),
resolves which tool server instances each enabled agent's ``tools.allowed``
grants -- with the framework's own discovery matcher -- and what type each
instance is, and holds the gates against it. A new agent with such a tool and
no gate turns this red; so does a changed set, which then has to be looked at
rather than absorbed.

Part of the shipped agents' gates (the agent YAMLs), not of the gate itself:
nothing else depends on this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIPPED_CONFIG = REPO / "config" / "config.yaml"

#: Tool server types whose grant runs whatever the caller asks for: a shell (terminal, coding_cli), commands
#: on remote hosts (ssh_control), arbitrary code inside a program (blender: bpy Python with os and
#: subprocess, src/plugins/blender/schema.yaml; godot: GDScript, which has OS.execute).
CODE_TYPES = frozenset({"terminal", "coding_cli", "ssh_control", "blender", "godot"})
#: The file access type; dangerous by the directories it may reach (_file_reach).
FILE_TYPE = "file_ops"

DATA = REPO / "data"
CONFIG = REPO / "config"
SRC = REPO / "src"

#: The agents the shipped configuration grants such a tool, all gated at admin.
EXPECTED_ADMIN = frozenset({
    "amiga_coder", "blender_agent", "claude_code_agent", "coder", "coder_explorer", "coder_reviewer",
    "coder_tester", "file_ops_test_agent", "gamedev", "gamedev_tester", "godot_agent", "skills_agent",
    "skills_agent_multimodal", "sysadmin_agent",
})

#: Granted a terminal, and still below admin: theirs (state_graph_terminal) runs one analysis script and
#: nothing else -- its whitelist is anchored and closed to the end of the line, chaining is off -- and the
#: writer's book runs spawn them (v4_sam) for ordinary accounts. Set in their YAML, not inherited: their
#: base type skills_agent is gated at admin (a full terminal), metadata is merged deeply, so without a
#: value of their own they would take admin and every ordinary account's book run would be refused;
#: ``min_role: null`` does not lift an inherited gate. ``user`` is what POST /run and /events ask anyway.
LOWER = {"state_graph_agent": "user", "state_graph_agent_ui": "user"}


@pytest.fixture(scope="module")
def shipped():
    """{name: merged ToolServerConfig} of every enabled server in the shipped configuration."""
    from agent_system.config import settings

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "_load_secrets_file", lambda path: None)  # keys stay out of this process
        config = settings.load_settings(str(SHIPPED_CONFIG))
        merged = {}
        for name, raw in (config.plugins.servers or {}).items():
            if not raw.enabled:
                continue
            resolved = settings.get_tool_server_config(name, config)
            if resolved is not None and resolved.enabled:
                merged[name] = resolved
    assert len(merged) > 100, f"fixture: the shipped configuration resolved only {len(merged)} servers"
    return merged


@pytest.fixture(scope="module")
def default_file_dirs():
    """The directories a file_ops instance without allowed_directories gets -- read from the plugin, not
    copied: it builds one and asks its path validator (construction starts no indexing)."""
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.file_ops.server import FileOpsServer

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(REPO)  # the plugin resolves its default against the working directory
        server = FileOpsServer("role_gate_guard_probe", AgentSystemConfig(),
                               ToolServerConfig(type="file_ops", enabled=True))
    directories = [str(path) for path in server.validator.allowed_dirs]
    assert directories, "fixture: file_ops gave an instance without allowed_directories no directory at all"
    return directories


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _file_reach(directory: object, writable: bool) -> str | None:
    """What a file_ops directory entry reaches that holds secrets or runs, or None.

    Relative entries resolve against the project root, as file_ops resolves them (and "." is the
    directory the CLI was started in, which is the checkout: both CLIs enter it at start).
    """
    path = Path(str(directory)).expanduser()
    resolved = (path if path.is_absolute() else REPO / path).resolve()
    if resolved == REPO or resolved in REPO.parents:
        return "the checkout"
    if resolved == DATA or _within(DATA / "users.db", resolved) or _within(DATA / "sessions", resolved):
        return "data/ (the user store, every user's sessions)"
    if _within(resolved, CONFIG):
        return "config/"
    if writable and _within(resolved, SRC):
        return "src/, writable"
    return None


def _powerful(merged: dict, default_file_dirs: list[str]) -> dict[str, str]:
    """{instance: why} of every enabled tool server instance that runs code or reaches what holds secrets."""
    found = {}
    for name, server in merged.items():
        if server.type in CODE_TYPES:
            found[name] = server.type
        elif server.type == FILE_TYPE:
            extra = server.model_extra or {}
            directories = extra.get("allowed_directories") or default_file_dirs
            writable = not extra.get("read_only", False)
            reach = next((reached for reached in (_file_reach(d, writable) for d in directories) if reached), None)
            if reach:
                found[name] = f"file_ops on {reach}"
    return found


def _granted(merged: dict, powerful: dict) -> dict[str, set[str]]:
    """{server: powerful instances its tools.allowed grants}, matched as tool discovery matches them."""
    from agent_system.servers.agent.tool_schema_builder import server_matches_patterns

    granted = {}
    for name, server in merged.items():
        tools = server.agent_config.tools if server.agent_config else None
        allowed = list(tools.allowed or []) if tools else []
        hits = {instance for instance in powerful if allowed and server_matches_patterns(instance, allowed)}
        if hits:
            granted[name] = hits
    return granted


def test_the_detector_sees_the_shipped_shells(shipped, default_file_dirs):
    """Without this a detector that sees nothing would make every assertion below vacuous."""
    powerful = _powerful(shipped, default_file_dirs)
    assert {"terminal", "coder_shell", "coding_cli", "ssh_control", "blender", "godot"} <= set(powerful), powerful
    assert "coder_fs" in powerful and "coder_fs_ro" in powerful, powerful


@pytest.mark.parametrize("server, flagged", [
    ({"allowed_directories": ["data"]}, True),                           # the user store and all sessions
    ({"allowed_directories": ["data/sessions"]}, True),
    ({"allowed_directories": ["data/workspace"]}, False),                # a folder of its own under data/
    ({"allowed_directories": ["config"]}, True),
    ({"allowed_directories": ["config/agents"]}, True),
    ({"allowed_directories": ["src/plugins/x/skills"]}, True),           # writable code
    ({"allowed_directories": ["src/plugins/x/skills"], "read_only": True}, False),
    ({}, True),                                                          # the plugin's default: writable src/
    ({"allowed_directories": ["."], "read_only": True}, True),           # the checkout, read-only or not
])
def test_the_file_detector_by_directory(default_file_dirs, server, flagged):
    from agent_system.config.models import ToolServerConfig

    config = ToolServerConfig(type="file_ops", enabled=True, **server)
    assert ("probe" in _powerful({"probe": config}, default_file_dirs)) is flagged, server


def test_every_agent_granted_a_shell_or_the_checkout_is_admin_only(shipped, default_file_dirs):
    granted = _granted(shipped, _powerful(shipped, default_file_dirs))

    assert set(granted) - set(LOWER) == EXPECTED_ADMIN, (
        "the agents granted a shell, code execution or file access to what holds secrets changed -- look at the "
        f"difference, then gate it or update this list: {sorted(set(granted) - set(LOWER) ^ EXPECTED_ADMIN)}")
    gates = {name: shipped[name].metadata.min_role if shipped[name].metadata else None for name in granted}
    wrong = {name: gate for name, gate in gates.items() if gate != LOWER.get(name, "admin")}
    assert wrong == {}, f"granted such a tool without the gate it needs: {wrong}"


def test_the_exceptions_hold_only_a_whitelisted_terminal(shipped, default_file_dirs):
    """The reason for LOWER, measured: nothing else powerful, and the terminal starts the one script only --
    every whitelist entry anchored at both ends (``^`` ... ``\\Z``) and naming it, chaining off, and no
    entry that matches a line break on its own, inside the command or at its end: ``bash -c`` runs every
    line, and ``\\s``, or ``$`` (it also matches before a final line break), let a second one through.
    The terminal refuses control characters before it asks a pattern; the entries do not lean on that."""
    import re

    granted = _granted(shipped, _powerful(shipped, default_file_dirs))

    for name in LOWER:
        assert granted.get(name) == {"state_graph_terminal"}, (name, granted.get(name))
    security = (shipped["state_graph_terminal"].model_extra or {}).get("security") or {}
    whitelist = security.get("whitelist") or []
    assert whitelist, f"state_graph_terminal has no whitelist: {security}"
    loose = [entry for entry in whitelist
             if not (entry.startswith("^") and entry.endswith(r"\Z") and r"verdachtsliste\.py" in entry)
             or re.fullmatch(entry, "bash -c 'id'")]
    assert loose == [], f"whitelist entries that are not anchored to the analysis script: {loose}"
    spanning = [entry for entry in whitelist
                if any(re.match(entry, command) for command in (
                    f"python {SCRIPT}\n4711", f"python {SCRIPT} 4711\n4712", f"python {SCRIPT} 4711\n"))]
    assert spanning == [], f"whitelist entries that match across a line break: {spanning}"
    assert security.get("allow_command_chains") is False, security


SCRIPT = "skills/writer/zustandsgraph/scripts/verdachtsliste.py"


def _build_state_graph_terminal(shipped, tmp_path, monkeypatch):
    from agent_system.config.models import AgentSystemConfig
    from agent_system.plugins import cache as cache_module
    from plugins.terminal.server import TerminalServer

    monkeypatch.setattr(cache_module, "default_cache_root", lambda: tmp_path / "cache")
    return TerminalServer("state_graph_terminal", AgentSystemConfig(), shipped["state_graph_terminal"])


@pytest.fixture
def state_graph_terminal(shipped, tmp_path, monkeypatch):
    """The shipped state_graph_terminal, built from its resolved config (its plugin cache under tmp_path)."""
    return _build_state_graph_terminal(shipped, tmp_path, monkeypatch)


@pytest.mark.parametrize("command, allowed", [
    (f"python {SCRIPT} 4711", True),
    (f"python3 {SCRIPT} 4711 --db /tmp/copy.db", True),
    (f"python {SCRIPT} 4711 --db C:/tmp/copy.db", True),
    (rf"python {SCRIPT} 4711 --db C:\tmp\copy.db", False),   # bash drops unquoted backslashes
    (f"py.exe {SCRIPT} 4711", True),
    (f".venv/bin/python {SCRIPT} 4711", True),
    (f".venv/bin/python3 {SCRIPT} 4711", True),
    (f".venv/Scripts/python.exe {SCRIPT} 4711", True),
    (rf".venv\Scripts\python.exe {SCRIPT} 4711", False),   # bash drops unquoted backslashes: forward only
    (rf"C:\work\hive\.venv\Scripts\python.exe {SCRIPT} 4711", False),   # the venv of the checkout only
    (f"../x/.venv/bin/python {SCRIPT} 4711", False),
    (f"/abs/.venv/bin/python {SCRIPT} 4711", False),
    (f"/tmp/x/evilpy {SCRIPT} 4711", False),       # any program whose name ends in "py"
    (f"/tmp/x/python {SCRIPT} 4711", False),
    (f"python {SCRIPT}\n4711", False),               # a line break is a second command to bash -c
    (f"python {SCRIPT} 4711\n", False),
    (f"python {SCRIPT} 4711\rid", False),
    (f"python\t{SCRIPT} 4711", False),
    (f"python -c 'import os' {SCRIPT} 4711", False),
    (f"python {SCRIPT} 4711; id", False),
])
def test_the_exception_terminal_starts_a_named_interpreter_on_the_script_only(state_graph_terminal, command,
                                                                                allowed):
    """The interpreter is named: ``python``, ``python3`` or ``py`` (optionally ``.exe``), or the checkout's
    own venv interpreter by its relative path with forward slashes (``bash`` drops unquoted backslashes,
    so a backslash form could only fail) -- the terminal starts in the directory the server runs from (its
    initial_cwd). A free prefix before ``python``/``py`` let any program whose name ends in those letters
    through, and a free directory before ``.venv`` any interpreter planted elsewhere. Behind it: this
    script, a book id, optionally ``--db``, separated by spaces only, and the end of the command."""
    assert state_graph_terminal.security.validate_command(command)[0] is allowed, command


def test_the_command_the_prompt_names_passes_the_whitelist(shipped, state_graph_terminal):
    """Evaluated, not quoted: every command the agent's prompt spells out for the script must run."""
    import re

    prompt = shipped["state_graph_agent"].agent_config.system_prompt or ""
    commands = [command.replace("<book_id>", "4711") for command in re.findall(r"`([^`]*verdachtsliste\.py[^`]*)`", prompt)]
    assert commands, "fixture: the prompt names no command for the analysis script"
    refused = [command for command in commands if not state_graph_terminal.security.validate_command(command)[0]]
    assert refused == [], refused


class _Status:
    async def progress(self, message): pass
    async def end(self, message, meta=None): pass
    async def error(self, message): pass


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("given", [{"cwd": "/tmp"}, {"env_vars": {"PYTHONPATH": "/tmp/x"}}])
async def test_the_exception_terminal_refuses_a_directory_or_environment(state_graph_terminal, monkeypatch,
                                                                          background, given):
    """The whitelist checks the command string only, while the working directory decides which files the
    script reads and writes and the environment how the interpreter runs; the model sets neither here. The
    spawn is a recorder: nothing of the script runs, refused or not."""
    from plugins.terminal import executor as executor_module

    spawned = []

    async def record(*argv, **kwargs):
        spawned.append(kwargs)
        raise RuntimeError("recorded, not started")

    monkeypatch.setattr(executor_module.asyncio, "create_subprocess_exec", record)

    answer = await state_graph_terminal.execute({"command": f"python {SCRIPT} 4711", "background": background,
                                                 "_status": _Status(), **given})

    assert answer.get("error_type") == "ConfiguredOnly", answer
    assert spawned == [], "the script was started"


def test_the_exception_terminal_starts_in_the_checkout_wherever_the_cli_was_started(shipped, tmp_path,
                                                                                      monkeypatch):
    """Its command names the script relative to the checkout, and the model sets no directory. Without
    one configured the terminal starts where the person stood (paths.launch_dir) -- started from anywhere
    else, the script was not found. The configured ``.`` is the directory the server runs from, the
    checkout, as every relative path of the configuration assumes: agent-cli and agent-run enter the
    project first (paths.enter_project) and resolve it after that; the API has to be started there, and
    started elsewhere the script is not found (it fails closed)."""
    from agent_system import paths

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)                 # where the person stands
    monkeypatch.setattr(paths, "_launch_dir", None)
    paths.enter_project()                        # what agent-cli and agent-run do first

    terminal = _build_state_graph_terminal(shipped, tmp_path, monkeypatch)

    assert Path(terminal.executor.initial_cwd) == REPO, terminal.executor.initial_cwd
    assert (REPO / SCRIPT).is_file(), "fixture: the analysis script is not where the command names it"
