"""The chat renders tool traffic twice -- in Python and in JavaScript.

The terminal builds those lines in ``cli_utils/chat.py``; the browser cannot
run Python, so ``static/js/chat_module.js`` builds the same lines again. The
architecture asks for that (``chat_commands`` holds the knowledge, each
surface keeps its own execution) -- but nothing holds the two halves together,
and measured on the day they were written they already disagreed seven times:
a missing "[image_url]" placeholder turned an image-only turn into no turn at
all, CRLF left a stray carriage return per line, an empty result printed one
blank line instead of none, a missing status lost a space, int() and round()
disagreed, and the /history count accepted "0x1f".

So this test runs the REAL JavaScript -- sliced out of the shipped file, not a
copy -- through node and compares it, case by case, against the Python that
produced the same screen in the terminal.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_system.cli_utils.chat import (  # noqa: E402
    _is_real_turn,
    _message_text,
    _render_tool_call,
    _render_tool_result,
)

REPO = Path(__file__).resolve().parents[2]
CHAT_JS = REPO / "static" / "js" / "chat_module.js"

#: The helper block in chat_module.js, between these two markers. Both are
#: real lines of that file; if either moves, this test fails loudly rather
#: than silently comparing nothing.
BLOCK_START = "  function asText(value)"
BLOCK_END = "  // The commands themselves"

#: Cases where the two renderers must agree character for character. Each is a
#: shape that really occurs: a shell result with status+stdout, a result that
#: is a bare JSON array, a non-JSON string, an empty one, CRLF from a Windows
#: shell, a call whose arguments are unparsable, a call with nothing at all.
TOOL_RESULTS = {
    "dict": json.dumps({"status": "success", "stdout": "a\nb"}),
    "dict_without_status": json.dumps({"content": "ok"}),
    "crlf": json.dumps({"status": "success", "stdout": "a\r\nb"}),
    "trailing_newline": json.dumps({"status": "success", "stdout": "a\n"}),
    "array": json.dumps(["eins", "zwei"]),
    "plain_text": "not json at all",
    "empty": "",
}

TOOL_CALLS = {
    "normal": {"function": {"name": "x", "arguments": json.dumps({"command": "ls", "cwd": "/tmp"})}},
    "empty_arguments": {"function": {"name": "x", "arguments": ""},
                        "arguments": json.dumps({"fallback": 1})},
    "flat_shape": {"name": "y", "arguments": "not json"},
    "nothing": {},
    "multiline_value": {"function": {"name": "z", "arguments": json.dumps({"script": "a\nb"})}},
}

MESSAGES = {
    "plain_text": "hallo",
    "image_with_empty_text": [{"type": "text", "text": ""},
                              {"type": "image_url", "image_url": {"url": "x"}}],
    "image_only": [{"type": "image_url", "image_url": {"url": "x"}}],
    "text_parts": [{"type": "text", "text": "eins"}, {"type": "text", "text": "zwei"}],
    # Sent as "//help me read this": stored with one slash, and a turn.
    "escaped_command_word": "/help me read this",
    "blank": "   ",
}

#: The same predicate asked about the ROLE rather than the text. The cases
#: above only ever vary the content, all of them as `role: "user"` -- so the
#: one input the two halves came to disagree on (a wake) was never handed to
#: either of them.
TURN_SHAPES = {
    "typed": {"role": "user", "content": "hallo"},
    "woken": {"role": "developer",
              "content": "You were woken because input is waiting for this session."},
    "note_from_the_run": {"role": "developer", "content": "2 steps left",
                          "injected_by": "agent.step_budget"},
    "scripted_followup": {"role": "user", "content": "Check your work.",
                          "injected_by": "agent_continuation.followup"},
    "an_answer": {"role": "assistant", "content": "fertig"},
}

#: The harness. It slices the helpers out of the shipped file and applies them
#: to the cases this test hands it, so what runs here is the code that ships.
NODE_HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf(process.argv[3]);
const end = source.indexOf(process.argv[4]);
if (start < 0 || end < 0 || end <= start) {
  console.error('MARKER_NOT_FOUND');
  process.exit(2);
}
const block = source.slice(start, end);
const window = {};
const h = new Function('window', block +
  '\nreturn {messageText, isRealTurn, toolCallLines, toolResultLines};')(window);

const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = { messages: {}, results: {}, calls: {}, turns: {}, shapes: {} };
for (const [name, content] of Object.entries(cases.messages)) {
  out.messages[name] = h.messageText({ content: content });
  out.turns[name] = h.isRealTurn({ role: 'user', content: content });
}
for (const [name, msg] of Object.entries(cases.shapes || {})) {
  out.shapes[name] = h.isRealTurn(msg);
}
for (const [name, content] of Object.entries(cases.results)) {
  out.results[name] = {
    full: h.toolResultLines({ role: 'tool', content: content }, true),
    compact: h.toolResultLines({ role: 'tool', content: content }, false),
  };
}
for (const [name, call] of Object.entries(cases.calls)) {
  out.calls[name] = {
    full: h.toolCallLines(call, true),
    compact: h.toolCallLines(call, false),
  };
}
process.stdout.write(JSON.stringify(out));
"""


class _Recorder:
    """Stands in for ChatRenderer: keeps the lines, drops the colour."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def println(self, text: str = "", color: str | None = None) -> None:
        self.lines.append(text)

    def commit(self) -> None:
        pass


class _Message:
    def __init__(self, content, role: str = "tool", injected_by=None) -> None:
        self.content = content
        self.role = role
        self.injected_by = injected_by


def _python_side() -> dict:
    """The same four answers, from the terminal's own helpers."""
    def result(content: str, full: bool) -> list[str]:
        recorder = _Recorder()
        _render_tool_result(recorder, _Message(content), full=full)
        return recorder.lines

    def call(payload: dict, full: bool) -> list[str]:
        recorder = _Recorder()
        _render_tool_call(recorder, payload, full=full)
        return recorder.lines

    return {
        "messages": {name: _message_text(_Message(content, role="user"))
                     for name, content in MESSAGES.items()},
        "turns": {name: _is_real_turn(_Message(content, role="user"))
                  for name, content in MESSAGES.items()},
        "shapes": {name: _is_real_turn(_Message(shape["content"], role=shape["role"],
                                                injected_by=shape.get("injected_by")))
                   for name, shape in TURN_SHAPES.items()},
        "results": {name: {"full": result(content, True), "compact": result(content, False)}
                    for name, content in TOOL_RESULTS.items()},
        "calls": {name: {"full": call(payload, True), "compact": call(payload, False)}
                  for name, payload in TOOL_CALLS.items()},
    }


def _run_harness(cases: dict, harness: Path) -> subprocess.CompletedProcess:
    """Apply the shipped JS helpers to *cases* and return node's answer."""
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    return subprocess.run(
        [shutil.which("node"), str(harness), str(CHAT_JS), BLOCK_START, BLOCK_END],
        input=json.dumps(cases), capture_output=True, text=True, timeout=120,
    )


@pytest.fixture(scope="module")
def javascript_side(tmp_path_factory) -> dict:
    if not shutil.which("node"):
        pytest.skip("node is not on PATH -- the browser half cannot be executed here")

    finished = _run_harness(
        {"messages": MESSAGES, "results": TOOL_RESULTS, "calls": TOOL_CALLS,
         "shapes": TURN_SHAPES},
        tmp_path_factory.mktemp("parity") / "harness.js")

    assert finished.returncode == 0, (
        f"the harness could not run the shipped helpers: {finished.stderr.strip()}\n"
        f"(MARKER_NOT_FOUND means the block markers in {CHAT_JS.name} moved -- "
        f"update BLOCK_START/BLOCK_END, do not delete this test)")
    return json.loads(finished.stdout)


class TestBothSurfacesRenderTheSame:
    def test_the_text_of_a_message(self, javascript_side):
        """A part without text becomes "[<type>]" on both sides -- returning ''
        for it is what made an image-only turn invisible to /last."""
        assert javascript_side["messages"] == _python_side()["messages"]

    def test_what_counts_as_a_turn(self, javascript_side):
        """The predicate /history and /last cut on. It has to be the same
        boolean, or the two surfaces disagree about where a turn begins."""
        expected = _python_side()["turns"]
        assert expected["image_with_empty_text"] is True, \
            "fixture: this case must be a real turn, or it proves nothing"
        assert expected["escaped_command_word"] is True, \
            "an escaped message is a turn -- hiding it orphaned its answer"
        assert expected["blank"] is False, "fixture: one case must not be a turn"
        assert javascript_side["turns"] == expected

    def test_what_counts_as_a_turn_by_its_role(self, javascript_side):
        """The other half of the same predicate. The cases above vary the text
        and are all `user`; this one varies role and marker, which is where the
        two halves drifted apart: a wake opens a turn and the browser's copy
        did not know it, so /last sliced from the exchange BEFORE it and
        /history counted one where the terminal counted two."""
        expected = _python_side()["shapes"]
        assert expected == {"typed": True, "woken": True, "note_from_the_run": False,
                            "scripted_followup": False, "an_answer": False}, \
            "fixture: the Python side itself must answer these five this way"
        assert javascript_side["shapes"] == expected

    def test_a_tool_result(self, javascript_side):
        assert javascript_side["results"] == _python_side()["results"]

    def test_a_tool_call(self, javascript_side):
        assert javascript_side["calls"] == _python_side()["calls"]


def test_the_one_accepted_difference_is_still_only_that_one(tmp_path):
    """JSON.stringify writes {"a":1}, json.dumps writes {"a": 1}.

    Matching it would need a serializer of our own for whitespace inside a
    truncated preview, so the difference is accepted and written down in
    chat_module.js. This pins it: if it ever grows beyond spacing, or if
    someone closes it, this test says so.
    """
    if not shutil.which("node"):
        pytest.skip("node is not on PATH")

    call = {"function": {"name": "z", "arguments": json.dumps({"a": 1, "b": [1, 2]})}}
    recorder = _Recorder()
    _render_tool_call(recorder, call, full=True)
    python_lines = recorder.lines

    finished = _run_harness({"messages": {}, "results": {}, "calls": {"spacing": call}},
                            tmp_path / "harness.js")
    assert finished.returncode == 0, finished.stderr
    js_lines = json.loads(finished.stdout)["calls"]["spacing"]["full"]

    assert js_lines != python_lines, \
        "the spacing difference is gone -- delete this test and the note in chat_module.js"
    normalised = [line.replace(",", ", ").replace(",  ", ", ") for line in js_lines]
    assert normalised == python_lines, (
        "the two renderers differ by more than JSON spacing now:\n"
        f"  js: {js_lines}\n  py: {python_lines}")
