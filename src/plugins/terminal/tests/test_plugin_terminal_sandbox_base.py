"""The sandbox root of a shell that starts in a relative directory.

``initial_cwd`` is written relative in the coder harness (``data/workspace``),
and the same value serves twice: as the working directory of every command and
as the base the sandbox resolves its workspace root against. A relative base
puts that segment into the path twice, so the cage would sit at
``<project>/data/workspace/data/workspace`` -- a directory that does not exist,
which is a cage around nothing.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_system import paths
from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.terminal.server import TerminalServer


@pytest.fixture
def server_config_with_relative_cwd():
    config = MagicMock(spec=ToolServerConfig)
    config.security = {'whitelist': None, 'blacklist': [], 'allow_command_chains': True}
    config.limits = {}
    config.platform = {'bash_path': 'auto', 'initial_cwd': 'data/workspace'}
    config.sandbox = {'mode': 'workspace-write'}
    return config


class TestRelativeInitialCwd:
    def test_sandbox_root_is_the_directory_itself_not_its_name_twice(
            self, server_config_with_relative_cwd):
        server = TerminalServer(
            "test", MagicMock(spec=AgentSystemConfig), server_config_with_relative_cwd)

        assert server.sandbox.workspace_root == (Path.cwd() / "data" / "workspace").resolve()

    def test_the_executor_starts_in_an_absolute_directory(
            self, server_config_with_relative_cwd):
        server = TerminalServer(
            "test", MagicMock(spec=AgentSystemConfig), server_config_with_relative_cwd)

        assert Path(server.executor.initial_cwd).is_absolute()


class TestNoInitialCwd:
    """An unset ``initial_cwd`` means where the person started, not the checkout.

    Both CLIs chdir into the project at startup, so ``Path.cwd()`` stopped
    being an answer about the person the moment they could start elsewhere.
    """

    @pytest.fixture
    def server_config_without_cwd(self):
        config = MagicMock(spec=ToolServerConfig)
        config.security = {'whitelist': None, 'blacklist': [], 'allow_command_chains': True}
        config.limits = {}
        config.platform = {'bash_path': 'auto', 'initial_cwd': None}
        config.sandbox = {'mode': 'workspace-write'}
        return config

    def test_shell_and_cage_follow_the_directory_the_command_was_started_in(
            self, server_config_without_cwd, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "_launch_dir", tmp_path)

        server = TerminalServer(
            "test", MagicMock(spec=AgentSystemConfig), server_config_without_cwd)

        assert Path(server.executor.initial_cwd) == tmp_path
        assert server.sandbox.workspace_root == tmp_path.resolve()
