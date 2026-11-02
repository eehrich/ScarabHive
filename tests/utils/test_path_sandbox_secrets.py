"""The installation's private files -- config/*.env, data/users.db, data/sessions and its
archive, the message debugger's store -- are refused to every plugin path.

A root may lie above the repository (a whole workbench), so containment alone
would hand them out. The files below REALLY exist; a green result must come
from the refusal, not from "file not found".
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.utils import path_sandbox
from agent_system.utils.path_sandbox import PathSandbox, PathSandboxDenied, holds_secrets, is_secret


@pytest.fixture
def install(tmp_path: Path, monkeypatch) -> Path:
    """A repository with config/local.env beside an ordinary config file, the user store and a
    session in data/, inside a workbench."""
    repo = tmp_path / "workbench" / "repo"
    config = repo / "config"
    config.mkdir(parents=True)
    (config / "local.env").write_text("TOKEN=secret")
    (config / "plugins.yaml").write_text("plugins: {}")
    data = repo / path_sandbox.data_path() if not path_sandbox.data_path().is_absolute() else None
    if data is None:
        pytest.skip("a data directory is configured outside the repository")
    (data / "sessions" / "alice").mkdir(parents=True)
    (data / "sessions" / "alice" / "s1.json").write_text("{}")
    (data / "users.db").write_text("hashes")
    (data / "session_archive").mkdir()
    (data / "session_archive" / "alice-2026-10.zip").write_text("archived")
    (data / "message_debugger").mkdir()
    (data / "message_debugger" / "debugger.db").write_text("every request")
    (data / "workspace").mkdir()
    (data / "workspace" / "notes.md").write_text("open")
    monkeypatch.setattr(path_sandbox, "PROJECT_ROOT", repo)
    monkeypatch.setattr(path_sandbox, "SECRETS_DIR", config)
    # the places as this installation keeps them, not as the test process happens to
    from agent_system.auth import database
    monkeypatch.setattr(database, "_db", None)
    monkeypatch.delenv("AGENT_SESSION_STORAGE_PATH", raising=False)
    return config


@pytest.fixture
def sandbox(install: Path) -> PathSandbox:
    workbench = install.parents[1]
    return PathSandbox.from_config([str(workbench)], base=workbench, read_only=True)


@pytest.mark.parametrize("spelling", [
    "repo/config/local.env",
    "repo/config/../config/local.env",
    "repo/CONFIG/LOCAL.ENV" if os.name == "nt" else "repo/config/local.env",
    "repo/config/local.env." if os.name == "nt" else "repo/config/local.env",
    "repo/config/local.env::$DATA" if os.name == "nt" else "repo/config/local.env",
])
def test_a_secret_is_refused_however_it_is_spelled(sandbox, install, spelling):
    with pytest.raises(PathSandboxDenied, match="private files"):
        sandbox.resolve(spelling)


@pytest.mark.parametrize("spelling", [
    "repo/data/users.db", "repo/data/users.db-wal", "repo/data/users.db-journal",
    "repo/data/sessions", "repo/data/sessions/alice", "repo/data/sessions/alice/s1.json",
    "repo/data/workspace/../sessions/alice/s1.json",
    "repo/data/session_archive", "repo/data/session_archive/alice-2026-10.zip",
    "repo/data/message_debugger/debugger.db",
])
def test_the_user_store_and_the_sessions_are_refused(sandbox, spelling):
    with pytest.raises(PathSandboxDenied, match="private files"):
        sandbox.resolve(spelling)


def test_the_rest_of_data_stays_readable(sandbox, install):
    assert sandbox.resolve("repo/data/workspace/notes.md").name == "notes.md"


def test_a_read_behind_an_imagined_root_is_refused_a_private_file(install):
    """A model writes /workspace/data/... for data/...: the read lands in the real data folder and is still judged."""
    repo = install.parent
    data = PathSandbox.from_config([str(repo / "data")], base=repo, read_only=True)
    imagined = "/no-such-sandbox-root/data"
    assert data.resolve(f"{imagined}/workspace/notes.md") == (repo / "data" / "workspace" / "notes.md").resolve()
    # (not the debugger store: the root conftest keeps it in a temporary folder, outside this data folder)
    for private in ("sessions", "sessions/alice", "session_archive", "users.db"):
        with pytest.raises(PathSandboxDenied, match="private files"):
            data.resolve(f"{imagined}/{private}")


def test_a_link_to_a_secret_is_refused(sandbox, install):
    link = install.parents[1] / "innocent.txt"
    try:
        link.symlink_to(install / "local.env")
    except OSError:
        pytest.skip("no symlink right (WinError 1314)")
    with pytest.raises(PathSandboxDenied, match="private files"):
        sandbox.resolve("innocent.txt")


def test_the_rest_of_the_config_stays_readable(sandbox, install):
    assert sandbox.resolve("repo/config/plugins.yaml") == (install / "plugins.yaml").resolve()


def test_an_env_file_of_another_project_is_not_the_installations(sandbox, install):
    other = install.parents[1] / "project" / "config"
    other.mkdir(parents=True)
    (other / "dev.env").write_text("X=1")
    assert sandbox.resolve("project/config/dev.env") == (other / "dev.env").resolve()


def test_the_secrets_directory_and_its_ancestors_hold_secrets(install):
    assert holds_secrets(install)
    assert holds_secrets(install.parent)
    assert holds_secrets(install.parents[1])
    assert not holds_secrets(install / "plugins.yaml")
    assert not holds_secrets(install.parents[1] / "elsewhere")
    assert is_secret(install / "local.env")
    assert not is_secret(install / "plugins.yaml")


def test_the_real_installation_refuses_its_own_env_files():
    real = Path(__file__).parents[2] / "config"
    assert path_sandbox.SECRETS_DIR.resolve() == real.resolve()
    assert is_secret(real / "local.env")
    assert is_secret(real / "secrets.env")


def test_the_text_search_walk_skips_a_secret(install):
    from plugins.file_ops.textsearch import walk_files

    found = {p.name for p in walk_files(install.parents[1], [], include_hidden=True)}
    assert "plugins.yaml" in found
    assert "local.env" not in found and "users.db" not in found and "s1.json" not in found
    assert "notes.md" in found


def test_is_link_names_a_symlink_and_a_junction_and_nothing_else(tmp_path):
    from agent_system.utils.path_sandbox import is_link

    folder = tmp_path / "folder"
    folder.mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert not is_link(folder) and not is_link(tmp_path / "file.txt")
    assert not is_link(tmp_path / "not_there") and not is_link("nul\0byte")
    try:
        (tmp_path / "symlink").symlink_to(folder, target_is_directory=True)
    except OSError:
        pass  # Windows without the right to make one
    else:
        assert is_link(tmp_path / "symlink") and not is_link(tmp_path / "symlink" / "inside")
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(folder), str(tmp_path / "junction"))
        assert is_link(tmp_path / "junction")


def test_the_code_asks_for_junctions_in_a_way_python_3_11_has():
    """Path.is_junction exists from 3.12: file_ops deleted, moved and renamed nothing on 3.11."""
    src = Path(__file__).resolve().parents[2] / "src"
    used = [str(py) for root in (src / "agent_system", src / "plugins") for py in root.rglob("*.py")
            if "tests" not in py.parts and ".is_junction()" in py.read_text(encoding="utf-8", errors="replace")]
    assert not used, used


@pytest.mark.parametrize("call", ["delete_file", "delete_dir", "delete_root", "move", "move_in", "rename"])
def test_file_ops_will_not_delete_move_or_rename_a_secret(install, call):
    from plugins.file_ops.operations import FileOperations

    ops = FileOperations.__new__(FileOperations)
    workbench = install.parents[1]
    run = {
        "delete_file": lambda: ops.delete_path_safe(install / "local.env"),
        "delete_dir": lambda: ops.delete_path_safe(install, recursive=True),
        "delete_root": lambda: ops.delete_path_safe(workbench, recursive=True),
        "move": lambda: ops.move_path_safe(install / "local.env", workbench / "taken.txt"),
        "move_in": lambda: ops.move_path_safe(install / "plugins.yaml", install / "new.env"),
        "rename": lambda: ops.rename_path_safe(install.parent, "renamed"),
    }[call]
    result = asyncio.run(run())
    assert result["status"] == "error" and "private files" in result["error"]
    assert (install / "local.env").read_text() == "TOKEN=secret"


def test_coding_cli_denies_the_secrets_to_claude_code(install):
    from plugins.coding_cli.run import build_command

    cmd = build_command(["claude"], mode="edit", mcp_config=Path("mcp.json"))
    denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--tools")]
    def spelled(path):
        text = path.resolve().as_posix()
        return f"/{text[0].lower()}{text[2:]}" if text[1:3] == ":/" else text

    data = install.parent / "data"
    assert denied == [f"{tool}(/{pattern})" for pattern in (
        f"{spelled(install)}/*.env", f"{spelled(data / 'users.db')}*", f"{spelled(data / 'sessions')}/**",
        f"{spelled(data / 'session_archive')}/**", f"{spelled(data / 'message_debugger')}/**")
        for tool in ("Read", "Edit")]


def test_a_session_store_and_a_user_store_kept_elsewhere_are_refused(install, tmp_path, monkeypatch):
    """AGENT_SESSION_STORAGE_PATH moves the sessions (their archive lies beside them), auth's db_path the user
    store: both stay private where they are."""
    from agent_system.auth import database

    store = tmp_path / "elsewhere" / "sessions"
    (store / "bob").mkdir(parents=True)
    (store / "bob" / "s2.json").write_text("{}")
    (store.parent / "session_archive").mkdir()
    users = tmp_path / "auth" / "accounts.db"
    users.parent.mkdir()
    users.write_text("hashes")
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(store))
    monkeypatch.setattr(database, "_db", type("Db", (), {"db_path": users})())
    assert is_secret(store / "bob" / "s2.json") and is_secret(store.parent / "session_archive")
    assert is_secret(users) and is_secret(str(users) + "-wal")
    assert path_sandbox.might_be_private("accounts.db")
    assert holds_secrets(tmp_path / "elsewhere") and not holds_secrets(tmp_path / "auth" / "nothing")
