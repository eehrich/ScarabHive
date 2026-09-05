"""The end line of a tool call must say what happened, and fit on one row.

An audit over all 45 plugins with tools (2026-09-02) found the same defect in
most of them: the handler never called ``status.end``, so the scope closed
with its default "completed" -- or it called it with a constant text
("Execution completed", "Completed operations: now") that repeats the scope
name and throws the result away. The WebUI writes start, progress and end into
the SAME row, so the end message REPLACES everything before it: it is the only
line that survives, and it has to carry subject AND result on its own.

This test nails the CLASS rather than each individual message. It drives real
tool calls through the real dispatch (``call_with_status`` -> StatusScope ->
status bus) and asserts, for whatever end line comes out:

* it is not the scope default and not a bare restatement of the tool name,
* it mentions something from the actual result,
* it fits the display budget (the same one ``terminal/_short_cmd`` uses).

Two layers, because driving 49 plugins would need a network, a GPU and an LLM:

1. DRIVEN -- a real call per tool, asserting the line above. Cache-hit
   branches and empty-collection tools get here without a network.
2. READ -- ``test_no_plugin_publishes_a_constant_line_that_says_nothing``
   parses every plugin under ``src/plugins`` and judges each ``status.end``
   /``status.error`` whose argument is a CONSTANT. That is the exact defect
   this audit found, and it needs no runtime, so it covers the plugins layer
   1 cannot reach.

What neither layer reaches -- an interpolated line inside a tool that needs an
LLM or a live server -- is named in UNCOVERED, per TOOL and with its reason.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.mcp.status import StatusPhase, get_status_bus

#: Budget for one status row. terminal/_short_cmd caps the SUBJECT at 70; a
#: whole line is subject plus a short result, so twice that is generous and
#: still catches a line that dumps a payload.
MAX_LINE = 140

#: Words that carry no information in a status line: the fact that a tool call
#: ran, said in various ways, plus the generic nouns for "a tool call". A line
#: made of nothing but these is empty however it is phrased.
#:
#: This replaced a list of whole-line REGEXES, which was the old equality
#: check in a new costume: measured against it, "Operation completed
#: successfully", "Task finished successfully", "All done" and "operation ok"
#: all passed, while "Completed task_a1b2" and "Finished chapter_03.md" were
#: wrongly rejected -- one of ten empty lines caught, and useful lines lost.
#: A subtractive rule has neither failure mode: what remains after removing
#: filler is the information, and an id, a count, a path or a title always
#: remains.
FILLER_WORDS = frozenset("""
    a an the it its this that all and or of for to in on at from with
    was were is are be been has have had did done doing
    completed complete completing finished finish success successful
    successfully ok okay started starting start ran run running executed
    execute execution executing performed perform processed process
    processing handled handle retrieved retrieve fetched fetch received
    receive returned return got operation operations request requests
    task tasks job jobs call calls command commands search searches
    query queries sandbox context reset cleared clear cache cached
    result results response responses
""".split())
# NB: "no", "none", "nothing" and "empty" are deliberately NOT filler. They
# were, until the static guard below flagged `cognitive_stack.peek` -> "Stack
# is empty" and `todo.get_progress_summary` -> "No tasks": a negative is an
# ANSWER, and a guard that calls it empty pushes plugins to invent a number
# where "none" is the truth.

#: Anything that is not a filler word and not pure punctuation counts as
#: information -- including digits, ids, paths and quoted subjects.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

#: Plugins whose CHANGED line this file does not drive, with the reason.
#:
#: Per TOOL, not per plugin: "runs an LLM" is true of `basic_agent.execute_task`
#: and false of `basic_agent.list_available_tools`, and hiding behind the plugin
#: name is how five entries stayed in this list while being drivable in three
#: lines each (audio_ops, duckduckgo_search, web_scraper, tavily_search,
#: tool_script -- all now driven below). A reason that says "network" where a
#: cache-hit branch returns before the network is not a reason.
#:
#: What is left needs an LLM, a GPU, a live server or a vector store. The
#: static guard at the bottom of this file still covers every one of them
#: against a CONSTANT empty line, and the two with their own status tests are
#: named with the path, so the claim can be checked.
UNCOVERED = {
    "comfyui": "the changed line is inside execute(): a live ComfyUI on :8188",
    "ssh_control": "the changed line is inside execute(): an asyncssh session",
    "basic_agent": "the changed line is in execute_task(), which runs an LLM",
    "llm_router": "the changed line is in chat(), which builds a client and calls it",
    "memory": "store/recall/update all reach the chroma vector store",
    "okf": "the changed line regenerates an index over a real bundle on disk",
    "sub_agent_manager": "the changed lines spawn or wait on an agent -- driven by "
                         "src/plugins/sub_agent_manager/tests (status tests)",
    "file_ops": "the changed lines are the search tools over a chroma index -- "
                "driven by src/plugins/file_ops/tests (status tests)",
}


async def run_tool(server, action, params):
    """One real tool call. Returns the event that closed the scope."""
    bus = get_status_bus()
    method = action[len(server.name) + 1:] if action.startswith(server.name + "_") else action
    queue = await bus.subscribe(server=f"{server.name}.{method}()")
    try:
        result = await server.call_with_status(action, params)
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, \
        f"{action}: expected exactly one closing event, got " \
        f"{[(e.phase, e.message) for e in events]}"
    return result, closing[0]


def says_nothing(message: str, known_words: tuple[str, ...] = ()) -> bool:
    """True for a line that carries no result -- default OR hand-written.

    Subtractive: strike out the filler and the words the reader ALREADY has
    (the plugin and method, which the row shows on its own, and the argument
    that named the operation). Whatever is left is the information. If nothing
    is left, the line restated the question instead of answering it.
    """
    def known(word: str) -> bool:
        if word in FILLER_WORDS:
            return True
        # Exact always; prefix both ways only from four characters, so a
        # method name matches its inflections (`context_engineer` covers
        # "engineering") without a short token like "log" matching "logic".
        return any(k == word
                   or (len(k) >= 4 and (word.startswith(k) or k.startswith(word)))
                   for k in known_words)

    return not [w for w in _WORD.findall(message.lower()) if not known(w)]


def assert_line_is_useful(action, message, must_mention):
    """The three properties every end line has to have."""
    assert message, f"{action}: empty status line"
    # The action's own words count as known: a line that only repeats the
    # tool name tells the reader what the row already says.
    assert not says_nothing(message, tuple(_WORD.findall(action.lower()))), \
        f"{action}: {message!r} carries no result -- says nothing"
    assert len(message) <= MAX_LINE, \
        f"{action}: status line is {len(message)} chars, budget is {MAX_LINE}: {message!r}"
    for needle in must_mention:
        assert str(needle) in message, \
            f"{action}: line {message!r} does not mention {needle!r} from the result"


@pytest.mark.parametrize("line,known", [
    # Verbatim the lines this audit replaced, each with the words its reader
    # already had. Two earlier versions of this guard were green on code they
    # were written to condemn: first an equality check against "completed",
    # then a list of whole-line regexes that still let "Operation completed
    # successfully" and "Task finished successfully" through. Those two now
    # sit in the list as well, so a third such regression cannot hide.
    ("completed", ()),
    ("Completed operations: current", ("datetime", "operations", "current")),
    ("Completed log_viewer_tail operation", ("log", "viewer", "tail")),
    ("Execution completed", ()),
    ("Retrieved from cache", ()),
    ("Sandbox reset completed", ()),
    ("Context engineering completed", ("context", "engineer")),
    ("Operation completed successfully", ()),
    ("Task finished successfully", ()),
    ("All done", ()),
    ("Done!", ()),
    ("operation ok", ()),
    ("Search successful", ()),
    ("Request handled", ()),
    ("-", ()),
])
def test_the_guard_would_have_caught_the_old_lines(line, known):
    assert says_nothing(line, known), f"{line!r} passes the guard -- it should not"


@pytest.mark.parametrize("line", [
    "current: 2026-09-03T10:40:37+00:00",
    "42 audio file(s), 2 unreadable",
    "Created task_a1b2: Rewire the status lines",
    "12 results (cached) -- kontextkompaktierung",
    "3 server(s) configured, 1 connected",
    "Deleted task_a1b2 + 7 dependent task(s)",
    # The regex version rejected these three: it swallowed
    # "Completed <one token>", which is an id, a filename or a count.
    "Completed task_a1b2",
    "Finished chapter_03.md",
    "Completed 3",
])
def test_the_guard_lets_real_lines_through(line):
    """Counter-check: a pattern that swallows real lines is worse than none."""
    assert not says_nothing(line), f"{line!r} is a real line and was rejected"


# ── datetime ──────────────────────────────────────────────────────────────

async def test_datetime_names_operation_and_answer():
    server_cfg = MagicMock()
    from plugins.datetime.server import DateTimeServer
    server = DateTimeServer("datetime", AgentSystemConfig(), server_cfg)

    result, closing = await run_tool(
        server, "datetime_operations", {"operation": "current"})

    assert result.get("status") != "error", result
    assert closing.phase is StatusPhase.END
    # The needle is the ANSWER, not the operation name: "current" alone also
    # sits inside the old "Completed operations: current", so it could not
    # tell the fixed line from the broken one.
    assert_line_is_useful("datetime", closing.message,
                          ["current", result["current_time"][:19]])


async def test_datetime_failure_does_not_say_completed():
    from plugins.datetime.server import DateTimeServer
    server = DateTimeServer("datetime", AgentSystemConfig(), MagicMock())

    _, closing = await run_tool(
        server, "datetime_operations", {"operation": "no_such_operation"})

    assert closing.phase is StatusPhase.ERROR, closing.message
    assert "completed" not in closing.message.lower()


# ── basic_operations ──────────────────────────────────────────────────────

async def test_ping_names_the_server():
    from plugins.basic_operations.server import BasicOperationsServer
    server = BasicOperationsServer("basic_operations", AgentSystemConfig(), MagicMock())

    result, closing = await run_tool(server, "basic_operations_ping", {})

    assert closing.phase is StatusPhase.END
    assert_line_is_useful("ping", closing.message, ["basic_operations"])
    assert result["timestamp"] in closing.message


# ── todo ──────────────────────────────────────────────────────────────────

@pytest.fixture
def todo_server(tmp_path: Path):
    from plugins.todo.server import TodoServer
    cfg = MagicMock()
    cfg.storage_path = str(tmp_path / "todo")
    cfg.max_tasks_per_session = 100
    cfg.enable_dependencies = True
    cfg.auto_save = True
    return TodoServer(name="todo", system_config=MagicMock(), mcp_config=cfg)


async def test_todo_create_names_the_title_not_only_the_id(todo_server):
    result, closing = await run_tool(todo_server, "todo", {
        "operation": "create",
        "title": "Rewire the status lines",
        "_session_id": "s1",
    })

    assert closing.phase is StatusPhase.END
    assert_line_is_useful("todo.create", closing.message,
                          [result["task_id"], "Rewire the status lines"])


async def test_todo_cascade_delete_reports_the_outermost_task(todo_server):
    """All frames share one scope; the deepest child used to close it, so the
    line named a dependent task instead of the one that was asked for."""
    parent = await todo_server.execute({
        "operation": "create", "title": "parent", "_session_id": "s2"})
    child = await todo_server.execute({
        "operation": "create", "title": "child", "_session_id": "s2",
        "depends_on": [parent["task_id"]]})

    result, closing = await run_tool(todo_server, "todo", {
        "operation": "delete",
        "task_id": parent["task_id"],
        "cascade": True,
        "_session_id": "s2",
    })

    # Without a real cascade the recursion never runs and this test would
    # pass on the broken code too.
    assert child["task_id"] in result["cascade_deleted"], \
        f"nothing cascaded ({result['cascade_deleted']}) -- the test would be vacuous"
    assert closing.phase is StatusPhase.END
    assert parent["task_id"] in closing.message, \
        f"the line names {closing.message!r}, not the task that was deleted"
    assert child["task_id"] not in closing.message


# ── debate_forum ──────────────────────────────────────────────────────────

@pytest.fixture
def forum_server(tmp_path: Path):
    from plugins.debate_forum.database import DebateForumDB
    from plugins.debate_forum.server import DebateForumServer
    return DebateForumServer(
        "debate_forum", AgentSystemConfig(), MCPConfig(type="debate_forum"),
        db=DebateForumDB(str(tmp_path / "forum.db")))


async def test_debate_forum_create_group_names_the_group(forum_server):
    _, closing = await run_tool(
        forum_server, "debate_forum_create_group", {"name": "Panel A"})

    assert closing.phase is StatusPhase.END, closing.message
    assert_line_is_useful("debate_forum_create_group", closing.message, ["Panel A"])


async def test_debate_forum_lists_report_the_real_counts(forum_server):
    """Driven NON-empty, and with two different numbers.

    An empty collection is the one case where `len(x)`, a hard-coded 0 and the
    WRONG collection all render identically -- so a count asserted against an
    empty database proves nothing. Three groups and two channels also make the
    two lines distinguishable from each other.
    """
    for name in ("Panel A", "Panel B", "Panel C"):
        await run_tool(forum_server, "debate_forum_create_group", {"name": name})
    for name in ("Round 1", "Round 2"):
        await run_tool(forum_server, "debate_forum_create_channel",
                       {"name": name, "topic": "status lines"})

    result, closing = await run_tool(forum_server, "debate_forum_list_groups", {})
    assert result["count"] == 3, result
    assert_line_is_useful("debate_forum_list_groups", closing.message, ["3 group"])
    assert "2" not in closing.message, \
        f"{closing.message!r} carries the channel count, not the group count"

    result, closing = await run_tool(forum_server, "debate_forum_list_channels", {})
    assert result["count"] == 2, result
    assert_line_is_useful("debate_forum_list_channels", closing.message, ["2 channel"])
    assert "3" not in closing.message, \
        f"{closing.message!r} carries the group count, not the channel count"


# ── the list of what is NOT covered here ──────────────────────────────────

def test_the_uncovered_list_names_real_plugins_with_a_reason():
    """A list of plugins this test cannot reach is only honest if the names
    exist and every one carries its reason -- otherwise it hides a plugin
    behind a typo or behind a shrug."""
    plugins_dir = Path(__file__).resolve().parents[2] / "src" / "plugins"
    missing = [name for name in UNCOVERED if not (plugins_dir / name).is_dir()]
    assert not missing, f"UNCOVERED names non-existent plugins: {missing}"
    unexplained = [name for name, why in UNCOVERED.items() if not why.strip()]
    assert not unexplained, f"UNCOVERED entries without a reason: {unexplained}"


def test_no_uncovered_entry_is_stale():
    """An excuse for a plugin this file DOES drive is worse than no list.

    The previous version asserted only that the names were directories and the
    reasons non-empty -- both properties of a literal three lines above it, so
    the only thing it could catch was a typo. Five entries were drivable in
    three lines each and sat in the list anyway. This reads the file's own
    source: if a driven test imports the plugin, its excuse has expired.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    # Everything after the UNCOVERED literal, so the dict's own keys and the
    # reason strings that name test paths do not count as "driven".
    body = source.split("async def run_tool", 1)[1]
    stale = [name for name in UNCOVERED if f"plugins.{name}." in body]
    assert not stale, (
        "UNCOVERED still excuses plugins this file drives: " + ", ".join(stale))


# ── sequential_thinking (pure in-memory) ──────────────────────────────────

async def test_sequential_thinking_names_session_and_count():
    from agent_system.config.models import MCPConfig as _MCPConfig
    from plugins.sequential_thinking.server import SequentialThinkingServer
    cfg = _MCPConfig(type="sequential_thinking", enabled=True)
    cfg.max_history_size = 20
    server = SequentialThinkingServer("seq", AgentSystemConfig(), cfg)

    result, closing = await run_tool(server, "seq_execute", {
        "thought": "first step", "thought_number": 1, "total_thoughts": 2,
        "next_thought_needed": True, "session_id": "s-line",
    })

    assert result["status"] == "success", result
    assert closing.phase is StatusPhase.END
    # Named needles, not []: with an empty list this asserted only "not
    # empty and short enough", so any constant would have passed the test
    # whose name promises session AND count.
    assert_line_is_useful("seq.execute", closing.message, ["1/2"])


async def test_sequential_thinking_clear_names_the_session():
    from agent_system.config.models import MCPConfig as _MCPConfig
    from plugins.sequential_thinking.server import SequentialThinkingServer
    cfg = _MCPConfig(type="sequential_thinking", enabled=True)
    cfg.max_history_size = 20
    server = SequentialThinkingServer("seq", AgentSystemConfig(), cfg)
    await server.execute({"thought": "t", "thought_number": 1, "total_thoughts": 1,
                          "next_thought_needed": False, "session_id": "s-clear"})

    _, closing = await run_tool(server, "seq_clear_history", {"session_id": "s-clear"})

    assert closing.phase is StatusPhase.END
    # The line used to be the constant "Cleared 1 session".
    assert_line_is_useful("seq.clear_history", closing.message, ["s-clear", "1"])


# ── script_interpreter (local sandbox) ────────────────────────────────────

async def test_script_interpreter_reports_output_and_variables():
    from plugins.script_interpreter.server import ScriptInterpreterServer
    server = ScriptInterpreterServer("script_interpreter", AgentSystemConfig(),
                                     MagicMock(script_interpreter={}))

    _, closing = await run_tool(server, "script_interpreter_execute", {
        "code": "x = 21 * 2" + chr(10) + "print(x)", "_session_id": "s1"})

    assert closing.phase is StatusPhase.END
    # Used to be the constant "Execution completed"; the numbers were in meta,
    # which the WebUI does not render.
    # "1 variable(s)", not "variable": the unit is an f-string constant and
    # is present however wrong the number is.
    assert_line_is_useful("script_interpreter", closing.message,
                          ["1 variable(s)", "chars output"])


async def test_script_interpreter_reset_names_the_session():
    from plugins.script_interpreter.server import ScriptInterpreterServer
    server = ScriptInterpreterServer("script_interpreter", AgentSystemConfig(),
                                     MagicMock(script_interpreter={}))

    _, closing = await run_tool(server, "script_interpreter_reset",
                                {"_session_id": "sess-42"})

    assert closing.phase is StatusPhase.END
    assert_line_is_useful("script_interpreter.reset", closing.message, ["sess-42"])


# ── log_viewer (allowlisted files under tmp_path) ─────────────────────────

@pytest.fixture
def log_server(tmp_path: Path):
    from plugins.log_viewer.mcp_server import LogViewerMCPServer
    log = tmp_path / "agent.log"
    log.write_text("\n".join(["first line", "ERROR boom", "third line", ""]),
                   encoding="utf-8")
    # A SECOND allowlisted file, so the three tools report three DIFFERENT
    # numbers (2 files, 1 match in 2 files, 2 of 3 lines). With one file every
    # count was 1 and an assertion could be satisfied by the wrong number.
    other = tmp_path / "api.log"
    other.write_text("nothing to see here" + chr(10), encoding="utf-8")
    cfg = MagicMock()
    cfg.log_files = [str(log), str(other)]
    return LogViewerMCPServer("log_viewer", AgentSystemConfig(), cfg), str(log)


async def test_log_viewer_list_counts_the_files(log_server):
    server, _ = log_server
    _, closing = await run_tool(server, "log_viewer_list", {})

    assert closing.phase is StatusPhase.END
    # Two files, so the count is distinguishable from the one-match count
    # of the search test -- and "2 log file(s)" cannot be satisfied by a
    # digit that happens to sit inside a bigger number.
    assert_line_is_useful("log_viewer.list", closing.message, ["2 log file(s)"])


async def test_log_viewer_search_reports_matches_and_pattern(log_server):
    server, _ = log_server
    result, closing = await run_tool(server, "log_viewer_search", {"pattern": "ERROR"})

    assert result["total_matches"] == 1, result
    assert closing.phase is StatusPhase.END
    # Reads total_matches / files_searched / pattern -- the field names are
    # exactly what a .get() default would hide.
    assert_line_is_useful("log_viewer.search", closing.message,
                          ["1 matches in 2 file(s)", "ERROR"])


async def test_log_viewer_tail_reports_the_line_counts(log_server):
    server, log_path = log_server
    _, closing = await run_tool(server, "log_viewer_tail",
                                {"log_file": log_path, "lines": 2})

    assert closing.phase is StatusPhase.END
    assert_line_is_useful("log_viewer.tail", closing.message, ["2/3 lines"])


# ── mcp_client (configured pool, nothing connected) ───────────────────────

async def test_mcp_client_list_servers_counts_them():
    from types import SimpleNamespace

    from plugins.mcp_client.server import MCPClientServer
    server = MCPClientServer("mcp_client", AgentSystemConfig(), MCPConfig(type="mcp_client"))
    # Three CONFIGURED, none connected -- via the pool's own configure(), not
    # a mock. With an empty pool both counts were 0, and then `len(configured)`,
    # `len(connected)` and a hard-coded 0 all render the same: the test could
    # not tell the two numbers apart, let alone notice them swapped.
    server.pool.configure({
        name: SimpleNamespace(enabled=True, url=f"http://localhost/{name}")
        for name in ("alpha", "beta", "gamma")
    })

    result, closing = await run_tool(server, "mcp_client_list_servers", {})

    assert result["total"] == 3 and result["connected"] == [], result
    assert closing.phase is StatusPhase.END
    assert_line_is_useful("mcp_client.list_servers", closing.message,
                          ["3 server(s) configured", "0 connected"])


# ── the plugins this file does NOT drive ──────────────────────────────────

def _literal(node) -> str | None:
    """The text of a status argument that is a CONSTANT, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # An f-string with no interpolation is a constant with extra steps.
    if isinstance(node, ast.JoinedStr):
        parts = [p for p in node.values]
        if all(isinstance(p, ast.Constant) for p in parts):
            return "".join(p.value for p in parts)
    return None


def _status_calls_with_constant_lines():
    """Every `<something>.end("literal")` / `.error("literal")` under src/plugins."""
    root = Path(__file__).resolve().parents[2] / "src" / "plugins"
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not this test's business
            continue
        enclosing = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing[id(child)] = node.name
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("end", "error")
                    and node.args):
                continue
            text = _literal(node.args[0])
            if text is None:
                continue
            plugin = path.relative_to(root).parts[0]
            yield path, node.lineno, plugin, enclosing.get(id(node), ""), text


def test_no_plugin_publishes_a_constant_line_that_says_nothing():
    """The class guard, for every plugin -- including the ones not driven here.

    Driving 49 plugins needs a network, a GPU and an LLM. Reading them does
    not: a status line built from a CONSTANT can be judged without running it,
    and that is the whole failure mode this audit found -- `status.end` with a
    fixed text that repeats the scope name. A line built from an f-string with
    real interpolation is out of this test's reach and is covered, for the
    plugins that can be driven, by the tests above.
    """
    offenders = []
    for path, line, plugin, func, text in _status_calls_with_constant_lines():
        known = tuple(_WORD.findall(f"{plugin} {func}".lower()))
        if says_nothing(text, known):
            offenders.append(f"{path.name}:{line} ({plugin}.{func}) -> {text!r}")

    assert not offenders, (
        "status lines that carry no result:" + chr(10) + chr(10).join(offenders))


# ── the cache-hit lines: no network needed, and they were the wrong ones ──

async def test_audio_ops_list_counts_the_files(tmp_path: Path):
    from plugins.audio_ops.server import AudioOpsServer
    cfg = MagicMock()
    cfg.storage_path = str(tmp_path)
    server = AudioOpsServer("audio_ops", AgentSystemConfig(), cfg)

    result, closing = await run_tool(server, "audio_ops_list", {})

    assert result["status"] == "success", result
    assert closing.phase is StatusPhase.END
    assert_line_is_useful("audio_ops.list", closing.message,
                          ["0 audio file(s)", tmp_path.name])


async def test_duckduckgo_cache_hit_names_query_and_count():
    from plugins.duckduckgo_search.server import DuckDuckGoSearchServer
    server = DuckDuckGoSearchServer("duckduckgo_search", AgentSystemConfig(),
                                    MCPConfig(type="duckduckgo_search"))
    cached = {"query": "kontextkompaktierung", "results": [1, 2, 3]}

    with patch.object(server.cache, "get", return_value=cached):
        _, closing = await run_tool(server, "duckduckgo_search_web_search",
                                    {"query": "kontextkompaktierung"})

    assert closing.phase is StatusPhase.END
    assert_line_is_useful("duckduckgo.web_search", closing.message,
                          ["3 results", "kontextkompaktierung"])


@pytest.mark.parametrize("operation,expected", [
    # ONE cached page serves both operations (the scraper parses text and
    # anchors in a single pass), so the line has to be keyed on the
    # operation asked for, not on which keys the cached dict happens to have.
    ("content", "120 chars"),
    ("links", "3 link(s)"),
])
async def test_web_scraper_cache_hit_reports_the_operations_own_result(
        operation, expected):
    from plugins.web_scraper.server import WebScraperServer
    server = WebScraperServer("web_scraper", AgentSystemConfig(),
                              MCPConfig(type="web_scraper"))
    link = {"href": "/a", "abs_url": "https://example.org/a", "text": "a", "rel": []}
    cached = {"text": "x" * 120, "links": [link, link, link], "status_code": 200,
              "final_url": "https://example.org/a", "title": "A"}

    with patch.object(server.cache, "get", return_value=cached):
        _, closing = await run_tool(server, "web_scraper_page", {
            "url": "https://example.org/a", "operation": operation})

    assert closing.phase is StatusPhase.END
    assert_line_is_useful(f"web_scraper.{operation}", closing.message,
                          [expected, "example.org"])


async def test_tavily_extract_cache_hit_counts_what_was_extracted():
    from plugins.tavily_search.server import TavilySearchServer
    with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
        server = TavilySearchServer("tavily_search", AgentSystemConfig(),
                                    MCPConfig(type="tavily_search"))
    # A partial extraction, which is cached as-is: one page of three URLs.
    cached = {"results": [{"url": "a"}], "failed_results": [{"url": "b"}, {"url": "c"}],
              "success_count": 1, "failed_count": 2}

    with patch.object(server.cache, "get", return_value=cached):
        _, closing = await run_tool(server, "tavily_search_extract", {
            "urls": ["a", "b", "c"]})

    assert closing.phase is StatusPhase.END
    # 1 page, not the 3 URLs that were asked for.
    assert_line_is_useful("tavily.extract", closing.message,
                          ["1 page(s)", "2 failed"])


async def test_tool_script_reports_how_many_calls_it_made():
    """A script with ZERO tool calls needs no registry -- the excuse that this
    plugin needs a live one was true of the sandbox, not of the end line."""
    from plugins.tool_script.server import ToolScriptServer
    server = ToolScriptServer("tool_script", AgentSystemConfig(),
                              MCPConfig(type="tool_script"))

    result, closing = await run_tool(server, "tool_script_run_script", {
        "script": "result = 1 + 1", "_agent": MagicMock()})

    assert result["status"] == "ok", result
    assert closing.phase is StatusPhase.END
    assert_line_is_useful("tool_script.run_script", closing.message, ["0/0"])
