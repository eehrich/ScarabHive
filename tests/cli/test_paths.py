"""Where the project is, and where the person was when they typed the command.

Started outside the repository, agent-cli found no config at all: the path is
relative, so `config/config.yaml` was looked for next to the person. Worse than
the error was what it did NOT report -- the plugin configs name their databases
as repository-relative strings, so a run in a foreign directory created
`data/writer/books.db` there, empty, and every query against it read zero rows
without failing.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_system import paths


@pytest.fixture
def unentered(monkeypatch):
    """paths with its module state restored -- enter_project writes globals."""
    monkeypatch.setattr(paths, "_launch_dir", None)
    return paths


class TestProjectRoot:
    def test_it_is_the_repository_and_not_the_working_directory(self, tmp_path,
                                                                monkeypatch):
        """Anchored to the module, so it survives any chdir -- that is the
        whole point of it."""
        monkeypatch.chdir(tmp_path)
        assert (paths.PROJECT_ROOT / "config" / "config.yaml").is_file()


class TestEnterProject:
    def test_it_runs_from_the_repository_and_reports_where_it_began(
            self, tmp_path, unentered, monkeypatch):
        monkeypatch.chdir(tmp_path)
        try:
            began = unentered.enter_project()
            assert Path.cwd() == paths.PROJECT_ROOT
            # tmp_path via cwd(): macOS hands out /private/var for /var.
            assert began == Path(tmp_path).resolve()
            assert unentered.launch_dir() == began
        finally:
            os.chdir(paths.PROJECT_ROOT)

    def test_a_second_call_keeps_the_directory_the_person_came_from(
            self, tmp_path, unentered, monkeypatch):
        """Otherwise the second call records the REPOSITORY as the place the
        person started, and every path they typed resolves against it."""
        monkeypatch.chdir(tmp_path)
        try:
            began = unentered.enter_project()
            unentered.enter_project()  # already inside the project now
            assert unentered.launch_dir() == began
        finally:
            os.chdir(paths.PROJECT_ROOT)

    def test_a_later_entry_point_elsewhere_gets_its_own_directory(
            self, tmp_path, unentered, monkeypatch):
        """One process runs one entry point -- except in this suite, which
        calls agent_cli.main() and agent_run.main() one after another from
        different directories. A flag would have frozen the first one's
        directory and resolved every later typed path against it.
        """
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        try:
            monkeypatch.chdir(first)
            assert unentered.enter_project() == first.resolve()
            monkeypatch.chdir(second)
            assert unentered.enter_project() == second.resolve()
        finally:
            os.chdir(paths.PROJECT_ROOT)

    def test_without_a_project_it_stays_where_it_is(self, tmp_path, unentered,
                                                    monkeypatch):
        """Installed instead of checked out, PROJECT_ROOT points into the
        environment. Moving there would recreate the shadow tree inside
        site-packages -- and chdir to a missing directory would raise out of
        the first line of main(), before argparse can say anything.
        """
        monkeypatch.setattr(unentered, "PROJECT_ROOT", tmp_path / "nothing here")
        monkeypatch.chdir(tmp_path)
        began = unentered.enter_project()
        assert Path.cwd() == Path(tmp_path).resolve(), "it moved anyway"
        assert began == Path(tmp_path).resolve()

    def test_a_relative_config_variable_is_resolved_before_the_move(
            self, tmp_path, unentered, monkeypatch):
        """AGENT_CONFIG_PATH is agent-run's only way to name a config, and it
        is named where the person stands."""
        monkeypatch.setenv("AGENT_CONFIG_PATH", "mine.yaml")
        monkeypatch.chdir(tmp_path)
        try:
            unentered.enter_project()
            assert os.environ["AGENT_CONFIG_PATH"] == str(
                Path(tmp_path).resolve() / "mine.yaml")
        finally:
            os.chdir(paths.PROJECT_ROOT)

    def test_an_absolute_config_variable_is_left_alone(self, tmp_path, unentered,
                                                       monkeypatch):
        # Written with forward slashes on purpose: what the person set stays
        # byte for byte. Sent through the resolution it would come back in the
        # platform's own spelling -- a value they never typed, in a variable
        # every sub-process inherits.
        given = tmp_path.as_posix() + "/mine.yaml"
        monkeypatch.setenv("AGENT_CONFIG_PATH", given)
        monkeypatch.chdir(tmp_path)
        try:
            unentered.enter_project()
            assert os.environ["AGENT_CONFIG_PATH"] == given
        finally:
            os.chdir(paths.PROJECT_ROOT)


class TestUserPath:
    def test_a_relative_path_follows_the_person_not_the_process(
            self, tmp_path, unentered, monkeypatch):
        monkeypatch.chdir(tmp_path)
        try:
            unentered.enter_project()
            assert Path.cwd() == paths.PROJECT_ROOT, "fixture: never entered"
            assert unentered.user_path("notes.md") == Path(tmp_path).resolve() / "notes.md"
        finally:
            os.chdir(paths.PROJECT_ROOT)

    def test_an_absolute_path_is_left_alone(self, tmp_path, unentered):
        given = tmp_path / "somewhere" / "notes.md"
        assert unentered.user_path(str(given)) == given

    def test_a_word_lock_file_does_not_raise(self, unentered, monkeypatch):
        """`~$notes.md` is what Word leaves beside a document. Path.expanduser
        RAISES RuntimeError on it whenever USERNAME differs from the profile
        directory -- a file name must not end the process handed it.

        The setenv is what makes this measure anything on a machine where the
        two DO match: without it the pathlib expanduser resolves the name
        quietly and the test passes against the broken implementation.
        """
        monkeypatch.setenv("USERNAME", "jemand_ganz_anderes")
        assert unentered.user_path("~$notes.md").name == "~$notes.md"


class TestTheCliWiring:
    def test_the_config_a_person_types_reaches_the_users_branch_resolved(
            self, tmp_path, unentered, monkeypatch):
        """`users` is split off BEFORE the run path parses its arguments and
        returns from there, so it never saw the resolution the run path does.
        The same --config named two different files depending on the
        subcommand behind it.
        """
        import sys

        import agent_system.agent_cli as cli

        seen: list[str | None] = []
        monkeypatch.setattr(cli, "_run_users_cli",
                            lambda args, config: seen.append(config))
        monkeypatch.setattr(sys, "argv",
                            ["agent-cli", "--config", "mine.yaml", "users", "list"])
        monkeypatch.chdir(tmp_path)
        try:
            cli.main()
        finally:
            os.chdir(paths.PROJECT_ROOT)

        assert seen == [str(Path(tmp_path).resolve() / "mine.yaml")]

    def test_agent_run_enters_the_project_as_well(self, monkeypatch):
        """agent-run has no --config at all, so AGENT_CONFIG_PATH is its only
        way to name one -- and entering the project is what resolves it. Both
        entry points or neither: a fix in one of two is a trap in the other.
        """
        import sys

        import agent_system.agent_run as runner

        entered: list[bool] = []
        monkeypatch.setattr(runner, "enter_project", lambda: entered.append(True))
        # No request: argparse stops right after the entry, which is all this
        # needs to see.
        monkeypatch.setattr(sys, "argv", ["agent-run"])
        with pytest.raises(SystemExit):
            runner.main()

        assert entered == [True]


class TestAttachmentsFollowThePerson:
    def test_a_file_named_where_the_person_stands_is_found(
            self, tmp_path, unentered, monkeypatch):
        """The production path: both CLIs and the chat's /attach sort their
        files through this one function."""
        from agent_system.cli_utils.attachments import sort_attachments

        attached = tmp_path / "note.txt"
        attached.write_text("hallo", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        try:
            unentered.enter_project()
            kinds, problems = sort_attachments(["note.txt"])
            assert problems == [], "the file was looked for in the repository"
            assert kinds["text"] == [str(Path(tmp_path).resolve() / "note.txt")]
        finally:
            os.chdir(paths.PROJECT_ROOT)
