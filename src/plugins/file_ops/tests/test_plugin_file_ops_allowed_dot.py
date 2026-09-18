"""What a "." in allowed_directories means.

Both CLIs enter the project at startup, so the working directory is the
checkout no matter where the person started. A "." in the allow-list is how
they let the agent into the directory they ARE in -- reading it as the
checkout would hand the agent the repository instead and leave the one
directory they meant locked.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_system import paths
from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.file_ops.security import SecurityError
from plugins.file_ops.server import FileOpsServer


def _server(allowed: list[str]) -> FileOpsServer:
    system_config = Mock(spec=AgentSystemConfig)
    server_config = ToolServerConfig(type="file_ops", enabled=True)
    server_config.allowed_directories = allowed
    server_config.search = {"enable_indexing": False,
                            "enable_semantic_search": False,
                            "index_on_startup": False}
    return FileOpsServer("file_ops", system_config, server_config)


@pytest.fixture
def here_and_project(tmp_path, monkeypatch):
    """The two directories the change is about, told apart."""
    here = tmp_path / "wo der mensch steht"
    project = tmp_path / "projekt"
    here.mkdir()
    project.mkdir()
    monkeypatch.setattr(paths, "_launch_dir", here)
    monkeypatch.chdir(project)  # what enter_project() leaves behind
    return here, project


class TestADotIsWhereThePersonStands:
    def test_it_allows_the_launch_directory(self, here_and_project):
        here, project = here_and_project
        assert Path.cwd() == project.resolve(), "fixture: the two are the same"

        server = _server(["."])

        assert server.validator.allowed_dirs == [here.resolve()]

    def test_a_file_there_can_be_written_and_one_in_the_project_cannot(
            self, here_and_project):
        here, project = here_and_project
        server = _server(["."])

        assert server.validator.validate_path(
            str(here / "neu.txt")) == (here / "neu.txt").resolve()
        with pytest.raises(SecurityError):
            server.validator.validate_path(str(project / "neu.txt"))

    def test_a_named_directory_still_means_the_project(self, here_and_project):
        """Only "." moves. "src" and its like are written in plugins.yaml and
        name the project's own directories -- reading them as the person's
        would point the allow-list at whatever happens to be beside them.
        """
        here, project = here_and_project
        (project / "src").mkdir()
        (here / "src").mkdir()

        server = _server(["src"])

        assert server.validator.allowed_dirs == [(project / "src").resolve()]
