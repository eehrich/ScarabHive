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

#: The agents the open configuration grants such a tool, all gated at admin. Agents a further
#: src/plugins_<name> root declares are its own tests' business: that root knows its exceptions.
EXPECTED_ADMIN = frozenset({
    "agy_code_agent", "blender_agent", "claude_code_agent", "coder", "coder_explorer", "coder_reviewer",
    "coder_tester", "coder_ultra", "file_ops_test_agent", "gamedev", "gamedev_tester", "godot_agent",
    "skills_agent", "skills_agent_multimodal", "sysadmin_agent",
})

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
    copied: it builds one and asks its path validator (construction starts no indexing). None today: the
    missing key fails closed (it used to open src, docs, tests and tmp, writable)."""
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.file_ops.server import FileOpsServer

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(REPO)  # the plugin resolves its default against the working directory
        server = FileOpsServer("role_gate_guard_probe", AgentSystemConfig(),
                               ToolServerConfig(type="file_ops", enabled=True))
    return [str(path) for path in server.validator.allowed_dirs]


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
    ({}, False),                                                         # the plugin's default: nothing
    ({"allowed_directories": ["."], "read_only": True}, True),           # the checkout, read-only or not
])
def test_the_file_detector_by_directory(default_file_dirs, server, flagged):
    from agent_system.config.models import ToolServerConfig

    config = ToolServerConfig(type="file_ops", enabled=True, **server)
    assert ("probe" in _powerful({"probe": config}, default_file_dirs)) is flagged, server


def _private_agents() -> set[str]:
    """Every server only a plugin root besides src/plugins declares in its agent files; one the public configuration
    declares as well (a further root only overriding it) is not one of them."""
    import yaml

    def declared(paths) -> set[str]:
        names = set()
        for path in paths:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            names.update(((data.get("plugins") or {}).get("servers") or {}).keys())
        return names

    further = declared(p for root in SRC.glob("plugins_*") for p in root.rglob("agents/*.yaml"))
    public = declared([*(SRC / "plugins").rglob("agents/*.yaml"), *CONFIG.rglob("*.yaml")])
    return further - public


def test_every_agent_granted_a_shell_or_the_checkout_is_admin_only(shipped, default_file_dirs):
    granted = _granted(shipped, _powerful(shipped, default_file_dirs))

    public = set(granted) - _private_agents()
    assert public == EXPECTED_ADMIN, (
        "the agents granted a shell, code execution or file access to what holds secrets changed -- look at the "
        f"difference, then gate it or update this list: {sorted(public ^ EXPECTED_ADMIN)}")
    gates = {name: shipped[name].metadata.min_role if shipped[name].metadata else None for name in public}
    wrong = {name: gate for name, gate in gates.items() if gate != "admin"}
    assert wrong == {}, f"granted such a tool without the gate it needs: {wrong}"
