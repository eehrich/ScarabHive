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
