"""A coding_cli run is shown the way a sub-agent is: a box under the call.

The chat builds a sub-agent's box from the `sub_run` envelopes relayed into the
parent run's stream, and both the chat and the terminal show its tool calls as
status lines under the run's id. Claude Code is no Agent: live.py translates its
stream into those events. What must hold is what the viewer relies on -- the ids
that hang the box and its lines where they belong, the order, the end -- and
that the box goes on after the call has answered with a run id.
"""
import asyncio
import json

import pytest

from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from plugins.coding_cli import live as live_module
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
        async def feed(self, events):
            seen.extend(e["message"]["content"][0]["text"] for e in events)

        async def close(self, record):
            seen.append("closed")

    server._live["tail01"] = Recording()
    await server._monitor(record, Exiting())

    assert seen == ["head", "tail", "closed"], seen


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
        async def feed(self, events):
            pass

        async def close(self, record):
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
