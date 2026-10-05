"""An agent's answer on stdout (cli_utils.common.show_answer), as --color asks.

agent-cli, agent-run and the terminal chat print every answer through it: its Markdown drawn with colours
where they show, HTML for --color html, the text as the model wrote it otherwise -- into a pipe, a file,
under NO_COLOR or TERM=dumb. JSON always as it is. Rich draws for real here; only the mode is set.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_system.cli_utils import common

ANSWER = "# Found it\n\nLine one\nline two\n\n- **bold** item"


def _plain(out: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", out)


def _shown(monkeypatch, capsys, mode: str, text: str, terminal: bool = False, term: str = "xterm-256color") -> str:
    monkeypatch.setattr(common, "color_mode", mode)
    monkeypatch.setattr(common, "ansi_capable_stdout", lambda: terminal)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", term)
    common.show_answer(text)
    return capsys.readouterr().out


@pytest.mark.parametrize("mode, terminal, term", [("auto", False, "xterm"), ("never", True, "xterm"),
                                                  ("text", True, "xterm"), ("auto", True, "dumb")])
def test_the_text_as_the_model_wrote_it_where_nothing_is_drawn(monkeypatch, capsys, mode, terminal, term):
    assert _shown(monkeypatch, capsys, mode, ANSWER, terminal, term) == ANSWER + "\n"


@pytest.mark.parametrize("mode, terminal", [("auto", True), ("ansi", False), ("always", False)])
def test_markdown_is_drawn_on_a_terminal_and_a_line_break_stays_one(monkeypatch, capsys, mode, terminal):
    out = _shown(monkeypatch, capsys, mode, ANSWER, terminal)
    assert "\x1b[" in out, "no colours: " + out   # --color asked for them, into a pipe too
    out = _plain(out)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert "Found it" in out and "#" not in out and "**" not in out, out   # drawn, not the marks
    assert lines.index("line two") == lines.index("Line one") + 1, "the line break was joined: " + out


def test_html_in_an_answer_stays_text_on_a_terminal(monkeypatch, capsys):
    """As in the chat: Rich on its own drops it, and with it a placeholder like <name>."""
    out = _plain(_shown(monkeypatch, capsys, "ansi", "Run agent-cli --agent <name> now.\n\n<div>block</div>"))
    assert "--agent <name> now." in out and "<div>block</div>" in out, out


def test_a_line_break_tag_in_a_table_cell_breaks_the_line(monkeypatch, capsys):
    out = _plain(_shown(monkeypatch, capsys, "ansi", "| day | open |\n|---|---|\n| Mo<br>Sa | yes |"))
    lines = [line.strip("│ ") for line in out.splitlines()]
    assert "<br>" not in out and any(line.startswith("Mo") for line in lines) \
        and any(line.startswith("Sa") for line in lines), out


def test_a_line_break_tag_on_a_line_of_its_own_is_one_too(monkeypatch, capsys):
    """Markdown would make it a block of HTML, drawn as code; the chat has no HTML blocks. One that ends a
    paragraph draws no line, in the chat neither."""
    def gap(text: str) -> int:
        lines = [line.strip() for line in _plain(_shown(monkeypatch, capsys, "ansi", text)).splitlines()]
        return lines.index("Line two") - lines.index("Line one")

    plain = gap("Line one\n\nLine two")
    assert gap("Line one\n\n<br>\n\nLine two") == plain + 2  # a paragraph of one empty line between
    assert gap("Line one<br>\n\nLine two") == plain and gap("**Line one<br>**\n\nLine two") == plain
    out = _plain(_shown(monkeypatch, capsys, "ansi", "- a\n- <br>\n- c\n\n## Heading\n<br>\nSome **bold** text"))
    assert out.count("•") == 3 and "Some bold text" in out and "<br>" not in out, out


@pytest.mark.parametrize("silenced", [{"NO_COLOR": "1", "TERM": "xterm-256color"}, {"NO_COLOR": "1", "TERM": "dumb"}],
                         ids=["NO_COLOR", "NO_COLOR+TERM=dumb"])
def test_colours_asked_for_reach_a_real_pipe_under_no_color_or_a_dumb_term(silenced):
    """--color always into a pipe: a real one, with a file descriptor (pytest's capture has none, and Rich
    takes another path without) -- on Windows Rich would use the console API, which a pipe ignores. NO_COLOR
    and TERM=dumb only silence the automatic choice; a dumb terminal gets a link as text, not as an OSC 8
    escape, an image's too (no image, as in the chat), an autolink once -- if its text is its address. Links
    are the chat's: another scheme stays text. The child imports the agent_system this test imported, not
    whatever the venv points at."""
    script = ("from agent_system.cli_utils import common\n"
              "common.color_mode = 'always'\n"
              "common.show_answer('**bold** and `code` [docs](https://example.com/docs)"
              " ![chart](https://example.com/c.png) <https://example.com/auto> <https://example.com/a%20b>"
              " [ftp](ftp://example.com/f) <ftp://example.com/g>')\n")
    src = str(Path(common.__file__).resolve().parents[2])
    env = {**{k: v for k, v in os.environ.items() if k != "NO_COLOR"}, **silenced,
           "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    # a foreground colour, not just bold: NO_COLOR alone leaves "\x1b[1m" for `code`, colour gives "\x1b[1;36;40m"
    assert re.search(rb"\x1b\[(?:\d+;)*(?:3[0-8]|9[0-7])(?:;\d+)*m", out.stdout), out.stdout
    dumb = silenced["TERM"] == "dumb"
    assert (b"\x1b]8;" in out.stdout) != dumb and b"https://example.com/docs" in out.stdout \
        and b"https://example.com/c.png" in out.stdout, out.stdout
    assert not dumb or (out.stdout.count(b"https://example.com/auto") == 1
                        and out.stdout.count(b"https://example.com/a%20b") == 1), out.stdout
    # ftp: markdown-it's own check lets it pass, the chat's does not
    assert b"[ftp](ftp://example.com/f) <ftp://example.com/g>" in out.stdout, out.stdout


def test_a_fence_around_the_whole_answer_is_the_answer(monkeypatch, capsys):
    out = _shown(monkeypatch, capsys, "ansi", "```markdown\n" + ANSWER + "\n```")
    assert "Found it" in out and "#" not in out and "```" not in out, out


@pytest.mark.parametrize("mode", ["ansi", "html", "text"])
def test_json_is_printed_as_it_is(monkeypatch, capsys, mode):
    answer = json.dumps({"city": "Oslo", "days": 3}, indent=2)
    assert _shown(monkeypatch, capsys, mode, answer) == answer + "\n"


def test_html_is_the_servers_rendering(monkeypatch, capsys):
    out = _shown(monkeypatch, capsys, "html", ANSWER + "\n\n<script>x()</script>")
    assert "<h1>Found it</h1>" in out and "<strong>bold</strong>" in out, out
    assert "<script>" not in out, out


def test_a_drawing_that_fails_still_prints_the_answer(monkeypatch, capsys):
    def broken(_text, **_kwargs):
        raise RuntimeError("no terminal after all")

    monkeypatch.setattr(common, "render_with_rich", broken)
    assert _shown(monkeypatch, capsys, "ansi", ANSWER) == ANSWER + "\n"
