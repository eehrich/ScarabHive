"""What a "." in allowed_directories means.

Both CLIs enter the project at startup, so the working directory is the
checkout no matter where the person started. A "." in the allow-list is how
they let the agent into the directory they ARE in -- reading it as the
checkout would hand the agent the repository instead and leave the one
directory they meant locked.
"""
import sys
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

    def test_a_relative_path_names_a_file_where_the_person_stands(self, here_and_project):
        """The shell of such an agent starts there too: read against the
        checkout, "README.md" named a file outside every root and was refused."""
        here, _ = here_and_project

        server = _server(["."])

        assert server.validator.validate_path("README.md") == (here / "README.md").resolve()

    def test_data_stays_the_installations(self, here_and_project, monkeypatch):
        """Prompts and skills name data/workspace and its like relatively."""
        here, project = here_and_project
        monkeypatch.setattr(paths, "configured_data_dir", lambda: None)
        (project / "data" / "workspace").mkdir(parents=True)

        server = _server([".", "data/workspace"])

        assert server.validator.validate_path("data/workspace/a.py") == (
            project / "data" / "workspace" / "a.py").resolve()
        assert server.validator.describe_roots() == "., data/workspace"

    def test_a_project_with_a_data_folder_of_its_own_still_gets_the_installations(
            self, here_and_project, monkeypatch):
        """The Godot tools and every other plugin read data/... as the
        installation's; a second meaning here split the file tools from them."""
        here, project = here_and_project
        monkeypatch.setattr(paths, "configured_data_dir", lambda: None)
        (here / "data" / "workspace").mkdir(parents=True)
        (project / "data" / "workspace").mkdir(parents=True)

        server = _server([".", "data/workspace"])

        assert server.validator.validate_path("data/workspace/a.py") == (
            project / "data" / "workspace" / "a.py").resolve()


    def test_without_a_dot_a_relative_path_names_the_project(self, here_and_project):
        here, project = here_and_project
        (project / "src").mkdir()

        server = _server(["src"])

        assert server.validator.validate_path("src/a.py") == (project / "src" / "a.py").resolve()

    @pytest.mark.parametrize("spelling", ["./"] + ([".\\"] if sys.platform == "win32" else []))
    def test_every_spelling_of_the_dot_means_the_same(self, here_and_project, spelling):
        here, _ = here_and_project

        server = _server([spelling])

        assert server.validator.allowed_dirs == [here.resolve()]


def test_started_in_the_project_a_configured_data_directory_still_wins(tmp_path, monkeypatch):
    """Where the person stands IS the project (the web UI, a start in the
    checkout): its own data folder must not shadow the configured one."""
    project, elsewhere = tmp_path / "projekt", tmp_path / "daten"
    (project / "data").mkdir(parents=True)
    (elsewhere / "workspace").mkdir(parents=True)
    monkeypatch.setattr(paths, "_launch_dir", None)
    monkeypatch.chdir(project)
    monkeypatch.setattr(paths, "configured_data_dir", lambda: elsewhere)

    server = _server([".", str(elsewhere / "workspace")])

    assert server.validator.validate_path("data/workspace/a.py") == (elsewhere / "workspace" / "a.py").resolve()


def test_an_empty_list_allows_nothing(tmp_path, monkeypatch):
    """Fail closed: an empty allow-list is not the missing key's defaults."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()

    server = _server([])

    assert server.validator.allowed_dirs == []
    with pytest.raises(SecurityError):
        server.validator.validate_path(str(tmp_path / "src" / "a.py"))


@pytest.mark.parametrize("spelling", [".", "./", "src"] + ([".\\"] if sys.platform == "win32" else []))
def test_project_instructions_reads_a_root_as_file_ops_does(here_and_project, spelling):
    """project_instructions finds AGENTS.md in the folder file_ops allows;
    two readings of the same entry pointed the hook at another folder."""
    from plugins.project_instructions.hooks import resolve_configured_root
    _, project = here_and_project
    (project / "src").mkdir()

    assert resolve_configured_root(spelling) == _server([spelling]).validator.allowed_dirs[0]


def test_an_empty_entry_allows_nothing(here_and_project):
    """pathlib reads "" as "." -- the entry must not open the launch folder."""
    assert _server([""]).validator.allowed_dirs == []
