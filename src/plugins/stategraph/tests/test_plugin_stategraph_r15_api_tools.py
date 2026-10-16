"""The api cluster of the review of 2026-10-15 (docs/review_2026-10-15.md): tool answers within one budget, the
read-only tools for non-admin readers, saving a tree, forks of callback runs, tags, run_to_end's exception.

Every test goes through the real StateGraphServer and its tools (run_tool, as an agent's call), or the real
service; each was checked with a mutation of the production line it is about (the report lists them).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph import server as server_module
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config
from plugins.stategraph.tests.test_plugin_stategraph_server import (CALLBACK, HELLO, HELLO_PY, _enable_auth,
                                                                    run_tool)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

BIG = """\
stategraph: 1
id: big
python: big.py
context: {big: null, small: null}
initial: make
states:
  make:
    do: {call: make}
    transitions:
      - target: done
        effect: |
          ctx.big = out
          ctx.small = "kept"
  done: {type: final, output: {n: "{{ ctx.small }}"}}
"""
#: 5,000 short texts: no text is cut at 2,000 characters, the whole is some 260,000
BIG_PY = "def make():\n    return ['x%05d-' % i + 'y' * 40 for i in range(5000)]\n"

FAILS = """\
stategraph: 1
id: fails
python: fails.py
initial: go
states:
  go:
    do: {call: boom}
    transitions:
      - target: done
  done: {type: final}
"""
FAILS_PY = "class Rejected(Exception):\n    pass\n\ndef boom():\n    raise Rejected('the gate said no')\n"


@pytest.fixture
async def server(tmp_path):
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    srv = StateGraphServer("stategraph", AgentSystemConfig(),
                           tool_config(tmp_path, machine_dirs=[str(tmp_path / "machines"), str(shipped)]))
    machines = tmp_path / "machines"
    for name, text in {"hello.yaml": HELLO, "hello.py": HELLO_PY, "big.yaml": BIG, "big.py": BIG_PY,
                       "fails.yaml": FAILS, "fails.py": FAILS_PY, "ask.yaml": CALLBACK}.items():
        (machines / name).write_text(text, encoding="utf-8")
    yield srv
    await srv.stop_plugin()


def size(value) -> int:
    return len(json.dumps(value, default=str))


# ------------------------------------------------------------------ one budget (API-1, API-8, DBG-2)

async def test_get_run_of_a_huge_ctx_keeps_its_journal_and_small_keys(server):
    """API-1: the frames alone over the budget dropped every journal row, then hid the whole ctx."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "big", "mock_only": False})
    assert started["run_status"] == "succeeded", started

    got, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"]})

    assert size(got) <= server_module.ANSWER_CHARS, size(got)
    ctx = got["frames"][0]["ctx"]
    assert ctx["small"] == "kept" and set(ctx["big"]) == {"$too_large"}, ctx
    assert any(row["kind"] == "activity" for row in got["journal"]), got.get("journal_cut")
    assert "cut" in got, sorted(got)


def test_the_journal_fills_what_the_frames_leave_newest_first():
    frames = [{"prefix": "", "ctx": {f"k{i}": "y" * 1999 for i in range(150)}}]
    journal = [{"seq": i, "data": {"out": "z" * 1999}} for i in range(1, 101)]

    answer = server_module._bounded({"run_id": "r", "frames": frames, "journal": journal})

    assert size(answer) <= server_module.ANSWER_CHARS, size(answer)
    assert answer["journal"] and answer["journal"][-1]["seq"] == 100, "the newest rows go first"
    assert "after=0" in answer["journal_cut"], answer["journal_cut"]
    forward = server_module._bounded({"run_id": "r", "frames": frames, "journal": journal}, forward=True)
    assert forward["journal"][0]["seq"] == 1 and f"after={forward['journal'][-1]['seq']}" in forward["journal_cut"]


async def test_run_machine_and_control_run_keep_the_bound(server):
    """API-8: an output of many short texts went out whole in run_machine; control_run had no bound at all."""
    (server.machines.find("big").path.parent / "big.yaml").write_text(
        BIG.replace('output: {n: "{{ ctx.small }}"}', "output: {items: \"{{ ctx.big }}\"}"), encoding="utf-8")
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "big"})
    assert started["status"] == "success", started
    assert size(started) <= server_module.ANSWER_CHARS, size(started)
    assert "$too_large" in json.dumps(started["output"]) and "full_output" in json.dumps(started["cut"]), started["cut"]

    whole, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "full_output": True})
    assert len(whole["output"]["items"]) == 5000, "full_output is the output whole"


async def test_a_paused_out_is_cut_and_said_once(server):
    """DBG-2: a paused activity's out went into every answer uncapped, twice into get_run's."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "big", "breakpoints": ["make@exit"]})
    assert started["run_status"] == "paused", started
    try:
        assert size(started) <= server_module.ANSWER_CHARS, size(started)
        assert started["paused"]["out"] == {"$too_large": size(json.loads(json.dumps(
            ['x%05d-' % i + 'y' * 40 for i in range(5000)])))}, str(started["paused"])[:300]
        got, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"]})
        assert "breakpoints" in got["debug"] and "paused" not in got["debug"], str(got["debug"])[:300]
        assert got["paused"]["state"] == "make", got["paused"]
        assert size(got) <= server_module.ANSWER_CHARS
    finally:
        await run_tool(server, "stategraph_control_run", {"run_id": started["run_id"], "action": "terminate"})


# ------------------------------------------------------------------ identity, bookkeeping (DBG-7)

async def test_get_run_names_the_run_and_leaves_bookkeeping_rows_out(server):
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "run_key": "k1",
                                                                   "tags": {"book": 95}})
    got, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"]})

    assert (got["machine_id"], got["run_key"], got["tags"]) == ("hello", "k1", {"book": 95}), got
    assert got["created_at"] and got["finished_at"], got
    assert not [row for row in got["journal"] if row.get("status") == "step"], "step rows by default"
    traced, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "kinds": ["trace"]})
    assert [row for row in traced["journal"] if row.get("status") == "step"], "asked for: the step rows"


async def test_the_runs_error_is_said_once(server):
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "fails"})
    assert started["run_status"] == "failed", started

    got, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"]})

    repeated = [row for row in got["journal"] if row["kind"] == "trace"
                and (row.get("data") or {}).get("error") == got["error"]]
    assert not repeated, repeated
    assert [row for row in got["journal"] if (row.get("data") or {}).get("error") == "(the run's error, above)"]


# ------------------------------------------------------------------ readers (API-3)

async def test_a_user_sees_no_run_of_nobody_no_path_and_no_traceback(server, monkeypatch):
    nobodys, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello"})
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})
    alices = await server.service.start_run("fails", user_id="alice")
    await server.run_manager.wait(alices["run_id"], timeout=5.0)

    listed, _ = await run_tool(server, "stategraph_list_runs", {"_user_id": "alice"})
    assert [run["id"] for run in listed["runs"]] == [alices["run_id"]], listed
    refused, _ = await run_tool(server, "stategraph_get_run", {"run_id": nobodys["run_id"], "_user_id": "alice"})
    assert refused["error_type"] == "http_404", refused
    by_admin, _ = await run_tool(server, "stategraph_get_run", {"run_id": nobodys["run_id"], "_user_id": "root"})
    assert by_admin["status"] == "success", by_admin
    by_system = server.service.get_run(nobodys["run_id"])  # the process's own code (run_to_end) has no user
    assert by_system["id"] == nobodys["run_id"]

    own, _ = await run_tool(server, "stategraph_get_run", {"run_id": alices["run_id"], "_user_id": "alice"})
    assert own["status"] == "success" and "traceback" not in json.dumps(own), str(own)[:500]
    admins, _ = await run_tool(server, "stategraph_get_run", {"run_id": alices["run_id"], "_user_id": "root"})
    assert "traceback" in json.dumps(admins), "the admin reads the traceback"

    machine, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello", "_user_id": "alice"})
    assert machine["file"] == "hello.yaml", machine["file"]
    listed_machines, _ = await run_tool(server, "stategraph_list_machines", {"_user_id": "alice"})
    assert {entry["file"] for entry in listed_machines["machines"]} >= {"hello.yaml"}, listed_machines
    admin_machine, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello", "_user_id": "root"})
    assert admin_machine["file"] == str(server.machines.find("hello").path)


# ------------------------------------------------------------------ machines (API-5, API-7, API-9)

SUB = """\
stategraph: 1
id: sub
initial: a
states:
  a: {type: final}
"""
MINE = """\
stategraph: 1
id: mine
imports: {sub: sub}
initial: run
states:
  run:
    do: {machine: sub}
    transitions:
      - target: done
  done: {type: final}
"""


async def test_the_whole_tree_saves_back_with_a_read_only_import_unchanged(server, tmp_path):
    (tmp_path / "shipped" / "sub.yaml").write_text(SUB, encoding="utf-8")
    (tmp_path / "machines" / "mine.yaml").write_text(MINE, encoding="utf-8")
    got, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "mine"})
    assert "../shipped/sub.yaml" in got["files"], got["files"].keys()

    files = {**got["files"], "mine.yaml": got["files"]["mine.yaml"] + "# changed\n"}
    saved, _ = await run_tool(server, "stategraph_save_machine", {"machine_id": "mine", "files": files,
                                                                  "expected_versions": got["versions"]})

    assert saved["status"] == "success", saved
    assert saved["versions"]["../shipped/sub.yaml"] == got["versions"]["../shipped/sub.yaml"]
    changed = {**files, "../shipped/sub.yaml": SUB + "# mine\n"}
    refused, _ = await run_tool(server, "stategraph_save_machine", {"machine_id": "mine", "files": changed,
                                                                    "expected_versions": saved["versions"]})
    assert refused["error_type"] == "http_403", "a changed read-only file is still refused"


async def test_a_save_refuses_files_outside_the_tree_and_an_id_that_is_no_id(server):
    version = server.service.get_machine("hello")["versions"]
    extra, _ = await run_tool(server, "stategraph_save_machine", {
        "files": {"hello.yaml": HELLO + "# x\n", "notes/readme.txt": "hi", "other.yaml": "not: [valid"},
        "expected_versions": version})
    assert extra["error_type"] == "http_422" and "notes/readme.txt" in extra["error"], extra
    assert not (server.machines.find("hello").path.parent / "other.yaml").exists()

    nested, _ = await run_tool(server, "stategraph_save_machine", {
        "machine_id": "sub/x", "files": {"sub/x.yaml": SUB.replace("id: sub", "id: x")}})
    assert nested["error_type"] == "http_422", nested


async def test_get_machine_leaves_a_large_file_out_and_reads_it_by_name(server):
    big = "# padding\n" * 4000 + HELLO_PY
    (server.machines.find("hello").path.parent / "hello.py").write_text(big, encoding="utf-8")

    got, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello"})
    assert "hello.py" not in got["files"] and got["left_out"] == {"hello.py": len(big)}, got.get("left_out")
    assert "hello.py" in got["versions"]
    one, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello", "file": "hello.py"})
    assert one["files"] == {"hello.py": big}


# ------------------------------------------------------------------ forks of callback runs (API-4)

async def _fork(server, run_id: str) -> dict:
    forked, _ = await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "fork"})
    assert forked["status"] == "success", forked
    return forked


async def test_a_fork_of_a_waiting_callback_run_gets_its_own_url(server):
    source, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "ask", "mock_only": True})
    assert source["run_status"] == "waiting", source
    forked = await _fork(server, source["run_id"])
    try:
        [made] = forked["callbacks"]
        token = made["url"].split("token=")[1]
        await server.service.use_callback(token, None)
        await server.run_manager.wait(forked["run_id"], timeout=5.0)
        assert server.run_store.get_run(forked["run_id"])["status"] == "succeeded"
        assert server.run_store.get_run(source["run_id"])["status"] == "waiting", "the source keeps its own URL"
    finally:
        await run_tool(server, "stategraph_control_run", {"run_id": source["run_id"], "action": "terminate"})


async def test_a_fork_of_an_ended_callback_run_takes_its_url(server):
    source, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "ask", "mock_only": True})
    await run_tool(server, "stategraph_control_run", {"run_id": source["run_id"], "action": "terminate"})
    forked = await _fork(server, source["run_id"])
    await server.run_manager.wait(forked["run_id"], timeout=5.0)  # replayed into its wait
    link = server.run_store.get_run(forked["run_id"])["view"]["frames"][0]["ctx"]["link"]

    await server.service.use_callback(link.split("token=")[1], None)
    await server.run_manager.wait(forked["run_id"], timeout=5.0)

    assert server.run_store.get_run(forked["run_id"])["status"] == "succeeded"


# ------------------------------------------------------------------ tags (FEAT-7)

async def test_tags_find_runs_across_machines(server):
    await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "tags": {"book": 95, "path": "main"}})
    await run_tool(server, "stategraph_run_machine", {"machine_id": "fails", "tags": {"book": 95}})
    await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "tags": {"book": 96}})

    listed, _ = await run_tool(server, "stategraph_list_runs", {"tags": {"book": 95}})
    assert sorted(run["machine_id"] for run in listed["runs"]) == ["fails", "hello"], listed
    narrow, _ = await run_tool(server, "stategraph_list_runs", {"tags": {"book": "95", "path": "main"}})
    assert [run["tags"] for run in narrow["runs"]] == [{"book": 95, "path": "main"}], narrow
    refused, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "tags": {"a": [1]}})
    assert refused["error_type"] == "http_422", refused


# ------------------------------------------------------------------ run_to_end (FEAT-12), start off the loop (API-2)

async def test_run_to_end_raises_the_calls_own_exception(server):
    with pytest.raises(Exception) as raised:
        await server.service.run_to_end("fails", raise_error=True)
    assert type(raised.value).__name__ == "Rejected" and "gate said no" in str(raised.value)

    from plugins.stategraph.service import RunFailed

    with pytest.raises(RunFailed) as failed:  # a mocked failure has no exception behind it
        await server.service.run_to_end("fails", mocks={"go": {"$error": {"type": "x", "message": "m"}}},
                                        raise_error=True)
    assert failed.value.error["type"] == "x"
    row = await server.service.run_to_end("fails")
    assert row["status"] == "failed", "without raise_error: the row, as before"


async def test_start_validates_off_the_loop_and_one_key_starts_one_run(server, monkeypatch):
    validate = server.service._validate

    def off_loop(tree):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return validate(tree)
        raise AssertionError("validated on the event loop")

    monkeypatch.setattr(server.service, "_validate", off_loop)
    first, second = await asyncio.gather(server.service.start_run("hello", run_key="same"),
                                         server.service.start_run("hello", run_key="same"))

    assert first["run_id"] == second["run_id"], (first, second)
    assert server.run_store.count_by_key("same") == 1


# ------------------------------------------------------------------ fix round of the review

async def test_a_run_key_reaches_no_run_a_user_may_not_see(server, monkeypatch):
    """R-API-1: run_key answered a non-admin with the output of a run of nobody (a schedule's), which get_run hides."""
    nobodys = await server.service.run_to_end("hello", {"name": "secret"}, run_key="schedule:nightly:1")
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})
    server.allowed_users = ["alice"]

    refused, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "params": {"name": "secret"},
                                                                   "run_key": "schedule:nightly:1", "_user_id": "alice"})
    assert refused["error_type"] == "http_409" and "secret" not in json.dumps(refused), refused
    by_admin, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "params": {"name": "secret"},
                                                                    "run_key": "schedule:nightly:1", "_user_id": "root"})
    assert by_admin["run_id"] == nobodys["id"] and by_admin["ended"] == "succeeded", by_admin
    again = await server.service.start_run("hello", params={"name": "secret"}, run_key="schedule:nightly:1")
    assert again["run_id"] == nobodys["id"], "the process's own code (no user) still finds its run"


FILEOUT = """\
stategraph: 1
id: fileout
initial: done
states:
  done: {type: final, output: {file: "reports/2026/book_95.md", traceback: "kept by the machine"}}
"""


async def test_a_reader_gets_the_machines_own_file_and_traceback_keys(server, monkeypatch, tmp_path):
    """R-API-2: the reader filter shortened every `file` and dropped every `traceback` key, the machine's own too."""
    (tmp_path / "machines" / "fileout.yaml").write_text(FILEOUT, encoding="utf-8")
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})
    row = await server.service.run_to_end("fileout", user_id="alice")

    got, _ = await run_tool(server, "stategraph_get_run", {"run_id": row["id"], "_user_id": "alice"})

    assert got["output"] == {"file": "reports/2026/book_95.md", "traceback": "kept by the machine"}, got["output"]


async def test_an_unchanged_file_the_tree_no_longer_imports_is_left_alone(server, tmp_path):
    """R-API-3: the whole tree sent back after an import was removed: the unchanged import was refused as an extra."""
    (tmp_path / "shipped" / "sub.yaml").write_text(SUB, encoding="utf-8")
    (tmp_path / "machines" / "mine.yaml").write_text(MINE, encoding="utf-8")
    got, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "mine"})
    alone = MINE.replace("imports: {sub: sub}\n", "").replace("    do: {machine: sub}\n", "")

    saved, _ = await run_tool(server, "stategraph_save_machine", {
        "machine_id": "mine", "files": {**got["files"], "mine.yaml": alone}, "expected_versions": got["versions"]})

    assert saved["status"] == "success", saved
    assert (tmp_path / "machines" / "mine.yaml").read_text(encoding="utf-8") == alone
    stray, _ = await run_tool(server, "stategraph_save_machine", {
        "machine_id": "mine", "files": {"mine.yaml": alone, "../shipped/sub.yaml": SUB + "# changed\n"},
        "expected_versions": saved["versions"]})
    assert stray["error_type"] == "http_422" and "not part of machine" in stray["error"], stray
