"""A workdir whose path holds a *: every git repository it matches, found at each call -- a project made after the
start is a workdir without a line of configuration and without a restart."""

from plugins.coding_cli.tests.test_plugin_coding_cli_server import (call, data_root, fresh_repo, git,  # noqa: F401
                                                                    make_server, plain_git)


def projects(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    fresh_repo(root / "First")
    (root / "notes").mkdir()                       # a folder that is no repository is no workdir
    return root


async def test_every_repository_a_pattern_matches_is_a_workdir_even_one_made_later(tmp_path):
    root = projects(tmp_path)
    server = make_server(root / "First", workdirs={"projects": {"path": str(root / "*")}})
    assert sorted(server._live_workdirs()) == ["first"], "named by the folder, in lower case"
    fresh_repo(root / "Later")
    assert sorted(server._live_workdirs()) == ["first", "later"], "a repository made after the start counts at once"
    result, _ = await call(server, "run_task", task="WRITE b.txt hello", workdir="later")
    assert result["state"] == "done", result
    assert git(root / "Later", "show", f"{result['branch']}:b.txt") == "hello", "the run worked in that repository"
    refused, _ = await call(server, "run_task", task="x", workdir="notes")
    assert refused["status"] == "error" and "first, later" in refused["error"], refused


def test_a_pattern_leaves_the_schemas_list_of_workdirs_open(tmp_path):
    """The schema is rendered once: a fixed list would name only what was there at the start."""
    root = projects(tmp_path)
    server = make_server(root / "First", workdirs={"projects": {"path": str(root / "*")}})
    workdir = server.get_schema_data()["tools"][0]["function"]["parameters"]["properties"]["workdir"]
    assert "enum" not in workdir and "first" in workdir["description"], workdir
    fixed = make_server(root / "First")
    assert fixed.get_schema_data()["tools"][0]["function"]["parameters"]["properties"]["workdir"]["enum"] == ["repo"]


def test_a_pattern_carries_its_settings_and_a_configured_name_wins(tmp_path):
    root = projects(tmp_path)
    other = fresh_repo(tmp_path / "other")
    server = make_server(root / "First", workdirs={
        "projects": {"path": str(root / "*"), "exclude": ["secret.txt"]},
        "first": {"path": str(other)}})
    assert server._live_workdirs()["first"]["path"] == other.resolve(), "the configured workdir of that name"
    fresh_repo(root / "Second")
    assert server._live_workdirs()["second"]["exclude"] == ["secret.txt"], server._live_workdirs()["second"]


async def test_a_workdir_a_caller_adds_at_run_time_stays_beside_a_pattern(tmp_path):
    """A caller registers a clone of its own in `workdirs` while the server runs: a pattern beside it must not
    hide it (a copy of the dict on every read lost the caller's entry)."""
    root = projects(tmp_path)
    server = make_server(root / "First", workdirs={"projects": {"path": str(root / "*")}})
    clone = fresh_repo(tmp_path / "clone")
    server.workdirs["clone"] = {**server._live_workdirs()["first"], "path": clone.resolve()}
    result, _ = await call(server, "run_task", task="WRITE b.txt hello", workdir="clone")
    assert result["state"] == "done" and git(clone, "show", f"{result['branch']}:b.txt") == "hello", result


GATEWAY = {"tools": ["probe_*"], "pin": {"probe_*": ["scope_id"]}}


def test_a_pattern_no_repository_matches_yet_still_offers_its_settings(tmp_path, caplog):
    """The schema is rendered at the start: a pattern over an empty folder must already offer pin, and the start
    must already warn that allowed commands inherit the gateway's token -- the repository comes later."""
    root = tmp_path / "workspace"
    root.mkdir()
    server = make_server(root, workdirs={"projects": {"path": str(root / "*"), "gateway": GATEWAY}},
                         allowed_commands=["git status"])
    tool = server.get_schema_data()["tools"][0]["function"]["parameters"]["properties"]
    assert "pin" in tool and "workdir" in tool, sorted(tool)
    assert any("inherits the environment" in r.getMessage() for r in caplog.records), caplog.text
    assert not any("no usable workdir" in r.getMessage() for r in caplog.records), caplog.text


async def test_a_pattern_without_a_match_says_so(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    server = make_server(root, workdirs={"projects": {"path": str(root / "*")}})
    refused, _ = await call(server, "run_task", task="x")
    assert "no git repository matches" in refused["error"], refused


def test_only_the_star_is_a_wildcard(tmp_path):
    """An installation under a folder with [ in its name: glob read it as a character class and matched nothing."""
    root = tmp_path / "my [x] projects"
    root.mkdir()
    fresh_repo(root / "One")
    server = make_server(root / "One", workdirs={"projects": {"path": str(root / "*")}})
    assert sorted(server._live_workdirs()) == ["one"]


def test_a_name_two_repositories_give_opens_neither(tmp_path, caplog):
    """Two patterns, each with a project of one name: which one a run would open would be a guess."""
    for side in ("a", "b"):
        (tmp_path / side).mkdir()
        fresh_repo(tmp_path / side / "proj")
    fresh_repo(tmp_path / "a" / "only")
    server = make_server(tmp_path / "a" / "only", workdirs={
        "left": {"path": str(tmp_path / "a" / "*")}, "right": {"path": str(tmp_path / "b" / "*")}})
    assert sorted(server._live_workdirs()) == ["only"], server._live_workdirs()
    assert any("'proj' left out -- 2 repositories" in r.getMessage() for r in caplog.records), caplog.text


async def test_a_workdir_is_found_by_its_folders_own_spelling(tmp_path):
    """A model names the project as its folder is spelled (Later), the workdir is named later."""
    root = projects(tmp_path)
    fresh_repo(root / "Later")
    server = make_server(root / "First", workdirs={"projects": {"path": str(root / "*")}})
    result, _ = await call(server, "run_task", task="WRITE b.txt hello", workdir="Later")
    assert result["state"] == "done" and git(root / "Later", "show", f"{result['branch']}:b.txt") == "hello", result


def test_a_configured_repository_is_not_opened_again_by_a_pattern(tmp_path):
    """A configured workdir hides a secret file; the same repository matched by a pattern would open it again
    without -- under its folder's name, or under a configured name in another case."""
    root = projects(tmp_path)
    fresh_repo(root / "Billing")
    server = make_server(root / "First", workdirs={
        "secretrepo": {"path": str(root / "Billing"), "exclude": ["a.txt"]},
        "First": {"path": str(root / "First")},
        "projects": {"path": str(root / "*")}})
    assert sorted(server._live_workdirs()) == ["First", "secretrepo"], server._live_workdirs()


def test_every_part_of_a_pattern_but_the_stars_is_literal(tmp_path):
    """[x] after the first * was a character class: it matched a folder x, not [x]."""
    fresh_repo((tmp_path / "group" / "[x]").mkdir(parents=True) or tmp_path / "group" / "[x]" / "proj")
    fresh_repo((tmp_path / "group" / "x").mkdir(parents=True) or tmp_path / "group" / "x" / "other")
    server = make_server(tmp_path, workdirs={"projects": {"path": str(tmp_path / "*" / "[x]" / "*")}})
    assert sorted(server._live_workdirs()) == ["proj"], server._live_workdirs()


def test_the_model_reads_the_pattern_as_configured(tmp_path):
    root = tmp_path / "my [x] projects"
    root.mkdir()
    fresh_repo(root / "One")
    server = make_server(root / "One", workdirs={"projects": {"path": str(root / "*")}})
    workdir = server.get_schema_data()["tools"][0]["function"]["parameters"]["properties"]["workdir"]
    assert "[[]" not in workdir["description"] and "my [x] projects/*" in workdir["description"], workdir


async def test_a_folder_name_no_workdir_can_have_is_named_in_the_answer(tmp_path, caplog):
    root = projects(tmp_path)
    fresh_repo(root / "my.game")
    server = make_server(root / "First", workdirs={"projects": {"path": str(root / "*")}})
    assert sorted(server._live_workdirs()) == ["first"]
    assert any("'my.game' left out" in r.getMessage() for r in caplog.records), caplog.text
    refused, _ = await call(server, "run_task", task="x", workdir="my.game")
    assert "no plain name" in refused["error"], refused


async def test_a_repository_from_a_pattern_runs_none_of_its_hooks_in_the_server(tmp_path):
    """Anyone who puts a repository into the pattern's folder chooses its hooks: git ran its post-checkout hook
    in this process, with its keys, when the worktree was made."""
    root = projects(tmp_path)
    marker = tmp_path / "hooked"
    hook = root / "First" / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    git(root / "First", "checkout", "-q", "-b", "probe")       # the hook runs in a plain checkout: it is live
    assert marker.exists(), "fixture: the hook never runs, the test would prove nothing"
    marker.unlink()
    server = make_server(root / "First", workdirs={"projects": {"path": str(root / "*")}})
    result, _ = await call(server, "run_task", task="WRITE b.txt hello", workdir="first")
    assert result["state"] == "done" and not marker.exists(), result


async def test_a_resume_whose_name_now_opens_another_repository_is_refused(tmp_path):
    """The run's project left one pattern's folder and a project of that name under another pattern took the
    name: the resume would bring that workdir's settings to the old worktree."""
    for side in ("a", "b"):
        (tmp_path / side).mkdir()
    fresh_repo(tmp_path / "a" / "proj")
    server = make_server(tmp_path, workdirs={
        "left": {"path": str(tmp_path / "a" / "*")}, "right": {"path": str(tmp_path / "b" / "*")}})
    first, _ = await call(server, "run_task", task="WRITE b.txt hello", workdir="proj")
    assert first["state"] == "done", first
    (tmp_path / "a" / "proj").rename(tmp_path / "moved")
    fresh_repo(tmp_path / "b" / "proj")
    again, _ = await call(server, "run_task", task="x", resume=first["run_id"])
    assert "another repository" in again["error"], again


def test_a_configured_name_in_another_case_wins_and_says_what_it_hides(tmp_path, caplog):
    """First is configured on another repository: the pattern's folder first is left out -- with a word in the
    log, since a run asking for first now gets First."""
    root = projects(tmp_path)
    other = fresh_repo(tmp_path / "other")
    server = make_server(root / "First", workdirs={"First": {"path": str(other)}, "projects": {"path": str(root / "*")}})
    assert sorted(server._live_workdirs()) == ["First"] and server._live_workdirs()["First"]["path"] == other.resolve()
    assert any("'first'" in r.getMessage() and "configured workdir has that name" in r.getMessage()
               for r in caplog.records), caplog.text


def test_a_configured_repository_is_known_by_the_folder_not_by_its_spelling(tmp_path):
    """The configured path spelled another way (another case on macOS, a bind mount, a detour): the strings
    differ, the folder is the same."""
    root = projects(tmp_path)
    server = make_server(root / "First", workdirs={
        "secret": {"path": str(root / "First"), "exclude": ["a.txt"]}, "projects": {"path": str(root / "*")}})
    server.workdirs["secret"]["path"] = root / "notes" / ".." / "First"
    assert sorted(server._live_workdirs()) == ["secret"], server._live_workdirs()
