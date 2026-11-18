"""The runs as a terminal reads them (agent_system.run_tree): what works now as a tree with the step each run is
on, what waits for its person, what just ended -- the list the web shell shows above its sessions, from the same
rows. And the two places that print it: ``agent-cli runs`` and the chat's ``/runs``."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_system import run_events
from agent_system.chat_commands import commands_for, parse_chat_command
from agent_system.cli_utils.cli_parser import main_parser, main_parser_args, preliminary_parser
from agent_system.cli_utils.commands import runs as runs_command
from agent_system.run_events import RunEventBus, RunEventStore, RunReporter
from agent_system.run_tree import ended, render, says, since, waiting, working

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def run(run_id, state="working", **fields):
    return {"run_id": run_id, "agent": "coder", "title": f"About {run_id}", "source": "chat", "state": state,
            "started": "2026-11-18T11:48:00Z", "updated": "2026-11-18T11:59:00Z", "ended": None, "step_label": "",
            "step_labels": {}, "silent": False, "needs_you": False, "parent_session": "", **fields}


def test_the_runs_at_work_read_as_a_tree_the_oldest_first():
    runs = [run("c2", started="2026-11-18T11:55:00Z"),
            run("s2", agent="explorer", source="sub_agent", parent_session="s1", started="2026-11-18T11:50:00Z"),
            run("s1", agent="researcher", source="sub_agent", parent_session="c1", started="2026-11-18T11:49:00Z"),
            run("c1"),
            run("b1", source="background", parent_session="long-gone", started="2026-11-18T11:58:00Z"),
            run("d1", state="done", ended="2026-11-18T11:40:00Z"),
            run("loop", parent_session="loop", started="2026-11-18T11:59:00Z")]
    assert [(r["run_id"], depth) for r, depth in working(runs)] == [
        ("c1", 0), ("s1", 1), ("s2", 2), ("c2", 0), ("b1", 0), ("loop", 0)]


def test_two_runs_that_name_each_other_draw_nothing_twice():
    runs = [run("a", parent_session="b"), run("b", parent_session="a")]
    assert working(runs) == []      # neither stands on its own: nothing to hang them on, and no endless walk


def test_the_chats_runs_command_prints_them(store, capsys):
    from agent_system.cli_utils.chat import repl

    repl._COMMANDS["runs"](None, "")
    assert capsys.readouterr().out == "Nothing at work.\n"


def test_what_waits_and_what_ended_are_told_the_newest_first():
    runs = [run("old-question", state="needs_input", needs_you=True, asked="2026-11-18T11:00:00Z",
                updated="2026-11-18T11:59:55Z"),      # it goes on saying that it lives: that is not what it waits since
            run("failure", state="failed", needs_you=True, updated="2026-11-18T11:30:00Z", ended="2026-11-18T11:30:00Z"),
            run("to-check", state="review", needs_you=True, updated="2026-11-18T11:45:00Z"),
            run("seen", state="failed", ended="2026-11-18T11:10:00Z"),
            run("sub", state="done", source="sub_agent", ended="2026-11-18T11:59:00Z"),
            run("yesterday", state="done", ended="2026-11-17T11:59:00Z"),
            run("an-hour-ago", state="done", ended="2026-11-18T10:59:59Z"),
            *(run(f"d{n}", state="done", ended=f"2026-11-18T11:2{n}:00Z") for n in range(8))]
    assert [r["run_id"] for r in waiting(runs)] == ["to-check", "failure", "old-question"]
    assert [r["run_id"] for r in ended(runs, now=NOW)] == ["d7", "d6", "d5", "d4", "d3", "d2"]
    # "just": what ended within the hour, and nothing whose end is no time at all
    assert [r["run_id"] for r in ended(runs, 20, now=NOW)][-1] == "seen"
    assert ended([run("no-time", state="done", ended=None)], now=NOW) == []


def test_how_long_is_told_by_the_minute():
    assert since("2026-11-18T11:59:20Z", NOW) == "now"
    assert since("2026-11-18T11:48:00Z", NOW) == "12 min"
    assert since("2026-11-18T09:57:00Z", NOW) == "2 h 03"
    assert since("2026-11-18T11:48:00", NOW) == "12 min"      # without a zone: UTC, as the store writes
    assert since("", NOW) == "" and since(None, NOW) == "" and since("soon", NOW) == ""
    assert since("2026-11-18T12:00:09Z", NOW) == "now"


def test_a_run_says_its_step_what_it_asks_or_how_it_ended():
    assert says(run("a", step_label="file_ops: read_file · a.py")) == "file_ops: read_file · a.py"
    assert says(run("a", step_label="Thinking", step_labels={"de": "Denkt nach"}), "de") == "Denkt nach"
    assert says(run("a")) == "At work" and says(run("a", state="queued")) == "Waiting to start"
    assert says(run("a", step_label="Thinking", silent=True)) == "Thinking -- no sign of life for 30 s"
    assert says(run("a", state="needs_input", question={"question": "Which branch?"})) == "Asks: Which branch?"
    assert says(run("a", state="review")) == "Done -- please check the result"
    # its mark says how it ended; a failure says why where the run events know
    assert [says(run("a", state=state)) for state in ("done", "cancelled", "handed_over", "failed")] == [
        "", "", "Handed over to you", "Failed"]
    assert says(run("a", state="failed", reason="the model is down")) == "Failed: the model is down"
    assert says(run("a", state="handed_over", reason="no access")) == "Handed over to you: no access"


def test_the_runs_as_lines_of_a_terminal():
    runs = [run("c1", step_label="Thinking"),
            run("s1", agent="explorer", title="Read the parser code", source="sub_agent", parent_session="c1",
                started="2026-11-18T11:59:30Z", step_label="file_ops: read_file · parser.c", silent=True),
            run("q1", agent="code_analyst", title="Assess the issues", state="needs_input", needs_you=True,
                question={"question": "Which branch?"}, started="2026-11-18T11:57:00Z"),
            run("d1", agent="reviewer", state="done", ended="2026-11-18T11:55:00Z"),
            run("x1", state="failed", ended="2026-11-18T11:50:00Z", reason="the model is down"),
            run("k1", state="cancelled", ended="2026-11-18T11:45:00Z")]
    assert render(runs, now=NOW, width=60) == [
        "At work (2)",
        "  ● coder · About c1                                  12 min",
        "      Thinking",
        "    ◌ explorer · Read the parser code                    now",
        "        file_ops: read_file · parser.c -- no sign of life f…",
        "Needs you (1)",
        "  ? code_analyst · Assess the issues                   3 min",
        "      Asks: Which branch?",
        "Just ended (3)",
        "  ✓ reviewer · About d1                                5 min",
        "  ✕ coder · About x1                                  10 min",
        "      Failed: the model is down",
        "  ■ coder · About k1                                  15 min",
    ]
    assert max(len(line) for line in render(runs, now=NOW, width=60)) == 60
    assert render([], now=NOW) == ["Nothing at work."]


def test_a_terminal_that_cannot_show_the_marks_gets_plain_ones():
    lines = render([run("c1", title="A title far too long for a terminal as narrow as this one is", step_label="Thinking"),
                    run("d1", state="done", ended="2026-11-18T11:55:00Z")], now=NOW, width=40, plain=True)
    assert lines == ["At work (1)", "  * coder - A title far too l...  12 min", "      Thinking",
                     "Just ended (1)", "  + coder - About d1               5 min"]
    "\n".join(lines).encode("ascii")


@pytest.fixture
def store(tmp_path):
    store = RunEventStore(tmp_path / "run_events.db")
    run_events.use_run_events(store, RunEventBus())
    yield store
    run_events.use_run_events(None)


async def test_agent_cli_runs_prints_every_users_runs_as_they_stand(store, capsys, monkeypatch):
    mine = RunReporter("s1", "coder", who="alice", title="Fix the parser", source="chat", task="turn")
    await mine.started()
    await mine.step("Thinking")
    theirs = RunReporter("s2", "reviewer", who="bob", title="A report", source="chat", task="turn")
    await theirs.started()
    await theirs.done("ok")
    monkeypatch.setattr(runs_command.shutil, "get_terminal_size", lambda fallback=None: SimpleNamespace(columns=61))

    # as agent-cli reads its command line: `runs` is the command, not a task for the default agent
    typed, rest = preliminary_parser().parse_known_args(["runs"])
    assert main_parser_args(typed, rest) == ["runs"]
    args = main_parser()[0].parse_args(["runs"])
    assert args.subcommand == "runs" and args.watch is False
    runs_command.run_runs_command(args, None)
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "At work (1)" and printed[1].startswith("  ● coder · Fix the parser") and printed[2] == "      Thinking"
    assert printed[3] == "Just ended (1)" and printed[4].startswith("  ✓ reviewer · A report")
    assert max(len(line) for line in printed) <= 60
    await mine.done("ok")


def test_a_console_on_a_legacy_code_page_gets_the_plain_marks(store, monkeypatch):
    out = SimpleNamespace(encoding="cp437")
    assert runs_command._plain(out) is True and runs_command._plain(io.StringIO()) is False
    assert runs_command.runs_text(out) == "Nothing at work."


async def test_watch_draws_again_until_it_is_left(store, capsys, monkeypatch):
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(runs_command.time, "sleep", nap)
    monkeypatch.setattr(runs_command, "ansi_capable_stdout", lambda: True)
    runs_command.run_runs_command(main_parser()[0].parse_args(["runs", "--watch"]), None)
    printed = capsys.readouterr().out
    assert printed.count("\x1b[H\x1b[2JNothing at work.") == 2 and naps == [runs_command.WATCH_S] * 2

    # into a file, or on a console that would print the codes: a line between, no codes
    naps.clear()
    monkeypatch.setattr(runs_command, "ansi_capable_stdout", lambda: False)
    runs_command.run_runs_command(main_parser()[0].parse_args(["runs", "--watch"]), None)
    printed = capsys.readouterr().out
    assert "\x1b" not in printed and printed.count("-" * 40 + "\nNothing at work.") == 2


def test_looking_at_the_runs_ends_none_of_them(tmp_path, monkeypatch):
    """A process that opens the run events ends the runs whose process is gone -- as it sees processes. One that
    only looks must not: from outside a container none of the server's processes is one, and a glance at the runs
    ended every one of them."""
    path = tmp_path / "run_events" / "run_events.db"
    with monkeypatch.context() as gone:
        gone.setattr(run_events, "OWNER", "999999:1.000:gone")
        RunEventStore(path).append("held", "run.started", agent="coder", who="alice")
    run_events.use_run_events(None)
    monkeypatch.setattr(run_events, "data_path", lambda *parts: tmp_path.joinpath(*parts))
    try:
        assert "At work (1)" in runs_command.runs_text()
        assert run_events.run_event_reader().run("held")["state"] == "working"
        assert RunEventStore(path).run("held")["state"] == "failed", "fixture: a store that sweeps ends it"
    finally:
        run_events.use_run_events(None)


def test_runs_is_a_command_of_the_terminal_chat():
    for line in ("/runs", "/ps", "/tasks"):
        assert parse_chat_command(line)[0] == "runs", line
    assert "runs" in {command.name for command in commands_for("cli")}
    assert "runs" in {command.name for command in commands_for("web")}, "a command is built for both or not at all"
