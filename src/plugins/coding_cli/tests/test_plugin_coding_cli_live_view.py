"""A coding_cli run is shown the way a sub-agent is: a box under the call.

The chat builds a sub-agent's box from the `sub_run` envelopes relayed into the
parent run's stream, and both the chat and the terminal show its tool calls as
status lines under the run's id. Claude Code is no Agent: live.py translates its
stream into those events. What must hold is what the viewer relies on -- the ids
that hang the box and its lines where they belong, the order, the end -- and
that the box goes on after the call has answered with a run id.
"""
import asyncio
import contextlib
import json
import os
import time

import pytest

from agent_system.core.session_presence import alive
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from plugins.coding_cli import live as live_module
from plugins.coding_cli import run as cli
from plugins.coding_cli.live import CAP_PAYLOAD, LiveRun

from test_plugin_coding_cli_server import call, ended, make_server  # noqa: F401 - fixtures below
from test_plugin_coding_cli_server import data_root, plain_git, repo  # noqa: F401


def _assistant(message_id, *blocks):
    return {"type": "assistant", "message": {"id": message_id, "content": list(blocks)}}


def _tool_result(tool_use_id, text, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": text, "is_error": is_error}]}}


@pytest.fixture
async def parent():
    """The run the viewer watches, streaming: what it collects is what the chat gets."""
    forwarder = StatusEventForwarder()
    await forwarder.start_forwarding("run1")
    yield forwarder
    await forwarder.stop_forwarding()


def _envelopes(forwarder):
    return [e for e in forwarder.status_events_to_forward if e["type"] == "sub_run"]


def _status(forwarder):
    return [e for e in forwarder.status_events_to_forward if e["type"] == "status"]


async def test_the_stream_arrives_as_a_sub_agent_would(parent, tmp_path):
    view = await LiveRun.open("run1_001", "fix the parser", tmp_path)
    await view.feed([
        # One model turn in two events, as Claude Code sends it: one step.
        _assistant("m1", {"type": "text", "text": "Reading the file first."}),
        _assistant("m1", {"type": "tool_use", "id": "t1", "name": "Write",
                          "input": {"file_path": str(tmp_path / "a.py"), "content": "x" * 5000}}),
        _tool_result("t1", "File created successfully"),
        _assistant("m2", {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}),
        _tool_result("t2", "2 failed\nmore output", is_error=True),
    ])
    await view.close({"state": "done", "result": "Fixed the parser."})

    rid = view.request_id
    assert rid.startswith("run1_001_sub_") and len(rid) == len("run1_001_sub_") + 6
    envelopes = _envelopes(parent)
    assert {(e["run_id"], e["spawned_by"], e["agent"]) for e in envelopes} == {(rid, "run1_001", "claude_code")}
    events = [e["event"] for e in envelopes]
    assert [(e["type"], e.get("step")) for e in events] == [
        ("start", None), ("thinking", 1), ("reasoning_delta", 1), ("tool_call", 1), ("thinking", 1),
        ("tool_result", None), ("thinking", 2), ("tool_call", 2), ("thinking", 2), ("tool_result", None),
        ("final", None), ("end", None)]
    assert events[0]["task"] == "fix the parser"
    calls = [e for e in events if e["type"] == "tool_call"]
    assert [c["request_id"] for c in calls] == [f"{rid}_001", f"{rid}_002"]
    # The arguments keep their fields -- the chat pretty-prints them -- and a
    # whole file does not travel whole.
    assert calls[0]["params"]["file_path"].endswith("a.py")
    assert len(calls[0]["params"]["content"]) < CAP_PAYLOAD + 60
    results = [e["result"] for e in events if e["type"] == "tool_result"]
    assert results == [{"content": "File created successfully"},
                       {"is_error": True, "content": "2 failed\nmore output"}]
    named = [e for e in events if e["type"] == "thinking" and "assistant" in e]
    assert [[c["function"]["name"] for c in e["assistant"]["tool_calls"]] for e in named] == [["Write"], ["Bash"]]
    assert events[-2]["summary"] == "Fixed the parser."

    lines = [(s["request_id"], s["phase"], s["message"]) for s in _status(parent)]
    assert lines == [
        (f"{rid}_001", "start", "Write a.py"),
        (f"{rid}_001", "end", "Write a.py: File created successfully"),
        (f"{rid}_002", "start", "Bash pytest -q"),
        (f"{rid}_002", "error", "Bash pytest -q: 2 failed"),
    ]
    # Their depth hangs them under the box, the box under the call.
    assert {s["tree"]["parent_id"] for s in _status(parent)} == {rid}


@pytest.mark.parametrize("record, last", [
    ({"state": "failed", "note": "Claude Code ended without a result"}, "error"),
    ({"state": "cancelled", "note": "stopped"}, "cancelled"),
])
async def test_a_run_that_did_not_finish_says_so(parent, tmp_path, record, last):
    view = await LiveRun.open("run1_002", "task", tmp_path)
    await view.feed([_assistant("m1", {"type": "tool_use", "id": "t1", "name": "Edit",
                                       "input": {"file_path": str(tmp_path / "b.py")}})])
    await view.close(record)

    events = [e["event"] for e in _envelopes(parent)]
    assert [e["type"] for e in events[-2:]] == [last, "end"]
    if last == "error":
        assert "without a result" in events[-2]["message"]
    # The call it never got an answer to does not spin for good.
    assert _status(parent)[-1]["phase"] == "error" and "no result" in _status(parent)[-1]["message"]


async def test_nothing_is_relayed_after_the_end(parent, tmp_path):
    view = await LiveRun.open("run1_003", "task", tmp_path)
    await view.close({"state": "done", "result": "ok"})
    count = len(parent.status_events_to_forward)

    await view.feed([_assistant("m9", {"type": "text", "text": "late"})])
    await view.close({"state": "done", "result": "again"})

    assert len(parent.status_events_to_forward) == count


async def test_a_call_without_a_request_id_has_no_view(tmp_path):
    assert await LiveRun.open(None, "task", tmp_path) is None


async def test_a_broken_relay_costs_the_view_not_the_run(parent, tmp_path, monkeypatch):
    view = await LiveRun.open("run1_004", "task", tmp_path)

    def broken(*args):
        raise RuntimeError("relay down")

    monkeypatch.setattr(live_module, "relay_run_event", broken)
    await view.feed([_assistant("m1", {"type": "text", "text": "hi"},
                                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}})])
    await view.close({"state": "done", "result": "ok"})   # neither raises
    # The terminal's only line for the call: a broken box must not take it along.
    assert [(e["message"], e["phase"]) for e in _status(parent)] == [
        ("Bash ls", "start"), ("Bash ls: no result, the run ended", "error")], _status(parent)


async def test_the_box_goes_on_after_the_call_answered(repo, parent):  # noqa: F811
    """wait_s is short: the call answers with a run id while Claude Code is
    still at work. What it does afterwards still reaches the parent's stream,
    the way a background sub-agent's steps do."""
    server = make_server(repo, wait_s=0.3)
    status_lines = []

    class Status:
        async def progress(self, message, meta=None):
            status_lines.append(message)

        async def end(self, message, meta=None):
            status_lines.append(message)

        async def error(self, message, meta=None):
            status_lines.append(message)

    answer = await server.run_task({"task": "SLEEP 1\nWRITE late.txt written after the answer\nSLEEP 4",
                                    "_status": Status(), "_user_id": "admin", "_session_id": "s1",
                                    "_request_id": "run1_005"})
    assert answer["state"] == "running", answer
    monitor = server._monitors[answer["run_id"]]

    def the_write():
        return [e["event"] for e in _envelopes(parent)
                if e["event"]["type"] == "tool_call" and e["event"]["action"] == "Write"]

    # While the run is still at work -- not collected at its end.
    for _ in range(60):
        if the_write() or monitor.done():
            break
        await asyncio.sleep(0.05)
    assert the_write() and not monitor.done(), "the box stood still until the run ended"
    assert "late.txt" in json.dumps(the_write()[0]["params"])

    await ended(server, answer["run_id"])
    kinds = [e["event"]["type"] for e in _envelopes(parent)]
    assert kinds[0] == "start" and kinds[-2:] == ["final", "end"], kinds
    assert server._live == {}, "the view outlived its run"


async def test_what_the_process_wrote_just_before_it_exited_arrives(tmp_path, monkeypatch):
    """The monitor reads after each pause and asks whether the process still
    runs before the next: lines written between that read and the exit are
    only reached by one more read after the loop."""
    from plugins.coding_cli import server as server_module

    monkeypatch.setattr(server_module, "POLL_S", 0.01)
    server = make_server(tmp_path)
    record = {"run_id": "tail01", "worktree": str(tmp_path), "started_at": 1e12}
    server._file("tail01", "jsonl").parent.mkdir(parents=True, exist_ok=True)
    server._save(record)
    stream = server._file("tail01", "jsonl")
    stream.write_text(json.dumps(_assistant("m1", {"type": "text", "text": "head"})) + "\n", encoding="utf-8")

    class Exiting:
        """Running until asked the second time -- and it writes its tail then."""
        polls = 0

        def poll(self):
            self.polls += 1
            if self.polls < 3:
                return None
            with open(stream, "a", encoding="utf-8") as out:
                out.write(json.dumps(_assistant("m2", {"type": "text", "text": "tail"})) + "\n")
            return 0

    seen = []

    class Recording:
        background = None   # relayed live only: nothing of it is kept

        async def feed(self, events):
            seen.extend(e["message"]["content"][0]["text"] for e in events)

        async def close(self, record):
            seen.append("closed")

        def detach(self):
            seen.append("detached")

    server._live["tail01"] = Recording()
    await server._monitor(record, Exiting())

    assert seen == ["head", "tail", "closed", "detached"], seen


@pytest.mark.parametrize("with_view", [True, False])
async def test_a_tool_call_the_view_shows_is_not_repeated_as_progress(tmp_path, monkeypatch, with_view):
    """The terminal prints every status line. With a live view each tool call
    already has its own line, with its result; the call's progress then only
    carries what Claude Code says. Without a view the progress is all there is."""
    from plugins.coding_cli import server as server_module

    monkeypatch.setattr(server_module, "POLL_S", 0.01)
    server = make_server(tmp_path)
    record = {"run_id": "prog01", "worktree": str(tmp_path), "started_at": 1e12}
    server._file("prog01", "jsonl").parent.mkdir(parents=True, exist_ok=True)
    server._save(record)
    server._file("prog01", "jsonl").write_text(json.dumps(_assistant(
        "m1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": str(tmp_path / "a.py")}},
        {"type": "text", "text": "Checking the parser."})) + "\n", encoding="utf-8")

    class Exiting:
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls < 3 else 0

    class Silent:
        background = None

        async def feed(self, events):
            pass

        async def close(self, record):
            pass

        def detach(self):
            pass

    progress = []

    class Listener:
        async def progress(self, message, meta=None):
            progress.append(message)

    server._listeners["prog01"] = Listener()
    if with_view:
        server._live["prog01"] = Silent()
    await server._monitor(record, Exiting())

    if with_view:
        assert progress == ["Checking the parser."], progress
    else:
        assert progress == ["Checking the parser. (+1)"], progress


async def test_an_event_whose_message_is_text_costs_nothing(parent, tmp_path, caplog):
    """Claude Code's system events carry text in "message" (permission_denied: a tool call refused):
    the view stopped at the first, and the rest of its batch was lost."""
    view = await LiveRun.open("run1_001", "text", tmp_path)
    await view.feed([
        {"type": "assistant", "message": "API Error: overloaded"},
        {"type": "user", "message": {"content": "a plain prompt"}},
        {"type": "system", "subtype": "permission_denied", "message": "Bash wurde verweigert"},
        {"type": "assistant", "message": {"id": "m0", "content": 5}},
        _assistant("m1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}),
        _tool_result("t1", "ok"),
    ])
    await view.close({"state": "done", "result": "done"})
    events = [e["event"] for e in _envelopes(parent)]
    assert [e["action"] for e in events if e["type"] == "tool_call"] == ["Bash"]
    assert [e["result"] for e in events if e["type"] == "tool_result"] == [{"content": "ok"}]
    assert "relaying a run's stream failed" not in caplog.text
    assert cli.actions({"type": "assistant", "message": "API Error"}, tmp_path) == []
    assert cli.actions({"type": "assistant", "message": {"content": 5}}, tmp_path) == [], "the run's watch reads it too"


async def test_an_event_that_breaks_the_view_costs_only_itself(parent, tmp_path, caplog, monkeypatch):
    real = LiveRun._assistant

    async def flaky(self, message, content):
        if message.get("id") == "bad":
            raise RuntimeError("a shape nobody foresaw")
        await real(self, message, content)
    monkeypatch.setattr(LiveRun, "_assistant", flaky)
    view = await LiveRun.open("run1_001", "flaky", tmp_path)
    await view.feed([_assistant("bad", {"type": "text", "text": "x"}),
                     _assistant("m1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}})])
    await view.close({"state": "done", "result": "done"})
    assert [e["event"]["action"] for e in _envelopes(parent) if e["event"]["type"] == "tool_call"] == ["Bash"]
    assert caplog.text.count("relaying a run's stream failed") == 1


async def test_arguments_nested_deeper_than_the_view_walks_cost_nothing_else(parent, tmp_path):
    """1,500 levels parse, but the view's capping recursed out: the rest of the batch was lost, the tool's
    row never ended."""
    view = await LiveRun.open("run1_001", "deep", tmp_path)
    deep = json.loads("[" * 1_500 + "]" * 1_500)
    await view.feed([
        _assistant("m1", {"type": "tool_use", "id": "t1", "name": "mcp__s__x", "input": {"a": deep}}),
        _tool_result("t1", "ok"),
    ])
    await view.close({"state": "done", "result": "done"})
    events = [e["event"] for e in _envelopes(parent)]
    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert "nested deeper" in tool_call["params"]
    assert [e["result"] for e in events if e["type"] == "tool_result"] == [{"content": "ok"}]


@pytest.fixture
async def kept(tmp_path, monkeypatch):
    """Sessions and run events of their own: where a run's box is kept once its turn has ended."""
    from agent_system import app_state, run_events
    from agent_system.run_events import RunEventBus, RunEventStore
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService

    store = RunEventStore(tmp_path / "run_events.db")
    run_events.use_run_events(store, RunEventBus())
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    monkeypatch.setattr(app_state, "session_service", SessionService(session_manager=manager))
    parent = (await manager.create_session(user_id="admin", title="the chat", agent_name="coder"))["session_id"]
    yield manager, store, parent
    run_events.use_run_events(None)


async def test_the_box_is_kept_where_the_chat_finds_it_once_the_turn_has_ended(repo, kept):  # noqa: F811
    """After the call answered, nothing streams the box any more: what the run does is kept in a sub-session of the
    chat's session and as a run below it, and the chat's Stop reaches it."""
    from agent_system.core import background_runs

    manager, store, parent = kept
    server = make_server(repo, wait_s=0.3)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE late.txt after\nSLEEP 30",
                           _request_id="run1_006")
    assert answer["state"] == "running", answer
    run_id = answer["run_id"]
    view = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))
    assert view["run"].startswith("run1_006_sub_") and view["parent"] == parent
    sub = view["session"]
    for _ in range(100):
        messages = (await manager.load_session("admin", sub))["messages"]
        if any(m.get("role") == "tool" for m in messages):
            break
        await asyncio.sleep(0.1)
    assert messages[0]["request_id"] == view["run"], "the box would not find its run"
    assert any("late.txt" in json.dumps(m.get("tool_calls") or []) for m in messages), messages
    who = background_runs.run_who(server.system_config, "admin")
    assert [r["run_id"] for r in store.background([parent], who)[parent]] == [sub]

    assert await background_runs.cancel(sub), "the chat's Stop did not reach the run"
    await ended(server, run_id)
    assert server._load(run_id)["state"] == "cancelled"
    assert (await manager.load_session("admin", sub))["metadata"]["background"]["state"] == "cancelled"
    assert store.run(sub)["state"] == "cancelled"
    assert store.background([parent], who) == {}


async def test_a_run_taken_over_keeps_its_box(repo, kept):  # noqa: F811
    """Another process takes the run over (a restart): its box goes on in the same sub-session, and a Stop reaches
    the watch that has it now."""
    from agent_system.core import background_runs

    manager, store, parent = kept
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 30", _request_id="run1_007")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    await other._sweep()
    assert run_id in other._monitors, "fixture: the other instance did not take the run over"
    assert other._live[run_id].background.session_id == sub
    assert store.run(sub)["state"] == "working"
    assert await background_runs.cancel(sub)
    await ended(other, run_id)
    assert other._load(run_id)["state"] == "cancelled"
    assert store.run(sub)["state"] == "cancelled"


async def test_a_run_that_ended_while_nobody_watched_ends_its_box_once(repo, kept):  # noqa: F811
    """The plugin stopped while the run went on, and the run ended before anyone took it over: whoever settles it
    then ends its box too -- not "at work" for good -- and only once."""
    manager, store, parent = kept
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE done.txt x", _request_id="run1_008")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    for _ in range(100):
        if not alive(server._load(run_id).get("pid")):
            break
        await asyncio.sleep(0.1)
    assert store.run(sub)["state"] == "working", "fixture: the stopped watch ended the run itself"
    await other._sweep()
    assert other._load(run_id)["state"] in ("done", "failed"), other._load(run_id)
    assert store.run(sub)["state"] in ("done", "failed"), "the run ended and its box stayed at work"
    once = len((await manager.load_session("admin", sub))["messages"])
    await call(other, "get_run", run_id=run_id)
    assert len((await manager.load_session("admin", sub))["messages"]) == once, "the end was kept twice"


async def test_a_run_that_ends_before_its_view_is_open_ends_its_box(repo, kept, monkeypatch):  # noqa: F811
    """Refused at once: the watch saw the end while the call was still opening the view, and ended nothing."""
    manager, store, parent = kept
    opening = LiveRun.open.__func__

    async def slow_open(cls, *args, **kwargs):
        await asyncio.sleep(3)   # the run has ended and been finalized by then
        return await opening(cls, *args, **kwargs)

    monkeypatch.setattr(LiveRun, "open", classmethod(slow_open))
    server = make_server(repo, wait_s=30)
    answer, _ = await call(server, "run_task", session=parent, task="WRITE quick.txt x", _request_id="run1_009")
    (sub,) = (await manager.load_session("admin", parent))["metadata"]["background_runs"]
    assert answer["state"] == "done", answer
    assert store.run(sub)["state"] == "done", "the run ended before its view opened, and its box stays at work"
    assert answer["run_id"] not in server._live


async def test_an_end_cut_short_by_a_stop_is_still_kept(repo, kept, monkeypatch):  # noqa: F811
    """Claimed, the end goes through whatever stops the one that claimed it -- or the claim holds every later end
    off and the run stays at work."""
    from agent_system.core import background_runs

    manager, store, parent = kept
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE b.txt x", _request_id="run1_010")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    for _ in range(100):
        if not alive(server._load(run_id).get("pid")):
            break
        await asyncio.sleep(0.1)
    held, ending = asyncio.Event(), background_runs.BackgroundRun.ended.__func__

    async def held_up(cls, **kwargs):
        await held.wait()
        return await ending(cls, **kwargs)

    monkeypatch.setattr(background_runs.BackgroundRun, "ended", classmethod(held_up))
    settling = asyncio.create_task(other._sweep())
    for _ in range(100):
        if other._file(run_id, "view-ended").exists():
            break
        await asyncio.sleep(0.05)
    settling.cancel()
    held.set()
    with contextlib.suppress(asyncio.CancelledError):
        await settling
    for _ in range(50):
        if store.run(sub)["state"] != "working":
            break
        await asyncio.sleep(0.1)
    assert store.run(sub)["state"] in ("done", "failed"), "a stop at the end left the run at work for good"


async def test_another_process_leaves_the_end_to_the_owner_that_holds_the_view(repo, kept):  # noqa: F811
    manager, store, parent = kept
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 30", _request_id="run1_011")
    run_id = answer["run_id"]
    try:
        record = server._load(run_id)
        server._save({**record, "state": "done"})   # as a reader in another process sees it, just before the owner
        await other._end_view(run_id)
        assert not other._file(run_id, "view-ended").exists(), "another process ended the owner's view"
    finally:
        server._save(record)
        await call(server, "cancel_run", run_id=run_id)
        await ended(server, run_id)


async def _process_gone(server, run_id):
    for _ in range(100):
        if not alive(server._load(run_id).get("pid")):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("fixture: the run's process never ended")


async def test_a_process_that_keeps_no_sessions_leaves_the_end_to_one_that_does(repo, kept, monkeypatch):  # noqa: F811
    """A CLI took the run over while the API was away. It can say that the run works, not write its end into the
    sub-session the chat reads: ended by it, the box stayed unfinished for good. The next sweep of a process that
    keeps sessions ends it."""
    from agent_system import app_state

    manager, store, parent = kept
    service = app_state.session_service
    server, cli_side = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 2\nWRITE c.txt x", _request_id="run1_012")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    monkeypatch.setattr(app_state, "session_service", None)
    await cli_side._sweep()
    assert run_id in cli_side._monitors, "fixture: the process without sessions did not take the run over"
    await ended(cli_side, run_id)
    for _ in range(50):
        if run_id not in cli_side._monitors:
            break
        await asyncio.sleep(0.1)
    assert cli_side._file(run_id, "view").exists(), "a process that cannot write the run's end took it"
    assert store.run(sub)["state"] == "working"
    monkeypatch.setattr(app_state, "session_service", service)
    await server._sweep()
    assert (await manager.load_session("admin", sub))["metadata"]["background"]["state"] in ("done", "failed")
    assert store.run(sub)["state"] in ("done", "failed")
    assert not server._file(run_id, "view").exists(), "an ended view is asked about at every sweep"


@pytest.mark.parametrize("left", ['{"pid": 99999999, "started": 0, "instance": "gone"}', ""])
async def test_an_end_a_process_died_over_is_taken_over(repo, kept, left):  # noqa: F811
    """Its process took the end and died before it had written it (or left the lease empty, dying while it wrote
    it): the lease held every later end off, and the box stayed at work for good."""
    manager, store, parent = kept
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE d.txt x", _request_id="run1_013")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    await _process_gone(server, run_id)
    lease = other._file(run_id, "view-ended")
    lease.write_text(left, encoding="utf-8")
    os.utime(lease, (time.time() - 60, time.time() - 60))
    await other._sweep()
    assert store.run(sub)["state"] in ("done", "failed"), "an end its taker died over is never written"
    assert (await manager.load_session("admin", sub))["metadata"]["background"]["state"] in ("done", "failed")
    assert not lease.exists() and not other._file(run_id, "view").exists()


async def test_a_process_that_keeps_no_sessions_does_not_end_a_view_it_finds(repo, kept, monkeypatch):  # noqa: F811
    """The run ended while nobody watched, and a CLI's sweep finds it first: settling it (its ring, its record) is
    that sweep's, the end of its box is not -- taken there, the box never got its answer."""
    from agent_system import app_state

    manager, store, parent = kept
    service = app_state.session_service
    server, cli_side = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE e.txt x", _request_id="run1_014")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    await _process_gone(server, run_id)
    monkeypatch.setattr(app_state, "session_service", None)
    await cli_side._sweep()
    assert cli_side._load(run_id)["state"] in ("done", "failed"), "fixture: the sweep did not settle the run"
    assert cli_side._file(run_id, "view").exists(), "a process that cannot write the run's end took it"
    assert store.run(sub)["state"] == "working"
    monkeypatch.setattr(app_state, "session_service", service)
    await server._sweep()
    assert (await manager.load_session("admin", sub))["metadata"]["background"]["state"] in ("done", "failed")
    assert store.run(sub)["state"] in ("done", "failed")


async def test_a_box_that_could_not_be_opened_again_still_gets_its_end(repo, kept, monkeypatch):  # noqa: F811
    """Taken over while its sub-session could not be read (a file held): the watch goes on without a background
    run -- and at the end removed the view that named the sub-session, which stayed at work for good."""
    from agent_system.core import background_runs

    manager, store, parent = kept
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 30", _request_id="run1_015")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    opening = background_runs.BackgroundRun.open

    async def refused(cls, **kwargs):
        return None

    monkeypatch.setattr(background_runs.BackgroundRun, "open", classmethod(refused))
    await other._sweep()
    monkeypatch.setattr(background_runs.BackgroundRun, "open", opening)
    assert other._live[run_id].background is None, "fixture: the box was opened again"
    await call(other, "cancel_run", run_id=run_id)
    await ended(other, run_id)
    assert (await manager.load_session("admin", sub))["metadata"]["background"]["state"] == "cancelled"
    assert store.run(sub)["state"] == "cancelled"
    assert not other._file(run_id, "view").exists()


async def test_an_end_the_run_events_refused_is_said_again_at_the_next_sweep(repo, kept, monkeypatch):  # noqa: F811
    """The view file is what asks for the end: removed although the run events had not taken it, the run stayed at
    work there until its process was gone."""
    from agent_system import run_events

    manager, store, parent = kept
    monkeypatch.setattr(run_events, "END_RETRY_S", 0.01)
    server, other = make_server(repo, wait_s=0.2), make_server(repo)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE f.txt x", _request_id="run1_016")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    await server.stop_plugin()
    await _process_gone(server, run_id)
    appending, busy = store.append, [True]

    def append(target, type_, **kwargs):
        if busy and type_ != "run.heartbeat":
            raise OSError("database is locked")
        return appending(target, type_, **kwargs)

    monkeypatch.setattr(store, "append", append)
    await other._sweep()
    assert other._load(run_id)["state"] in ("done", "failed"), "fixture: the sweep did not settle the run"
    assert other._file(run_id, "view").exists(), "an end the run events did not take is never said again"
    assert not other._file(run_id, "view-ended").exists(), "its lease holds the next sweep off"
    busy.clear()
    await other._sweep()
    assert store.run(sub)["state"] in ("done", "failed")
    assert not other._file(run_id, "view").exists()


def test_a_lease_a_live_process_holds_or_is_writing_is_left_alone(repo):  # noqa: F811
    server = make_server(repo)
    lease = server._file("abc123abc123", "view-ended")
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(json.dumps(server._me), encoding="utf-8")
    assert not server._take(lease), "an end somebody is writing was taken from them"
    lease.write_text("", encoding="utf-8")
    assert not server._take(lease), "a lease being written this moment was taken over"


async def test_an_end_refused_while_its_watch_held_the_run_is_said_again(repo, kept, monkeypatch):  # noqa: F811
    """The same where the watch that held the run ends it: its view stays until the run events have the end."""
    from agent_system import run_events

    manager, store, parent = kept
    monkeypatch.setattr(run_events, "END_RETRY_S", 0.01)
    server = make_server(repo, wait_s=0.2)
    answer, _ = await call(server, "run_task", session=parent, task="SLEEP 1\nWRITE g.txt x", _request_id="run1_017")
    run_id = answer["run_id"]
    sub = json.loads(server._file(run_id, "view").read_text(encoding="utf-8"))["session"]
    appending, busy = store.append, [True]

    def append(target, type_, **kwargs):
        if busy and type_ in ("run.done", "run.failed"):
            raise OSError("database is locked")
        return appending(target, type_, **kwargs)

    monkeypatch.setattr(store, "append", append)
    await ended(server, run_id)
    assert server._load(run_id)["state"] in ("done", "failed"), "fixture: the run did not end"
    assert server._file(run_id, "view").exists() and store.run(sub)["state"] == "working"
    busy.clear()
    await server._sweep()
    assert store.run(sub)["state"] in ("done", "failed")
    assert not server._file(run_id, "view").exists()
