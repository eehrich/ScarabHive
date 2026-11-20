"""/cls -- clear the terminal; the session is not touched.

/clear stays /new (a fresh session), as the other coding CLIs taught it; /cls
is the screen alone.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent_system.chat_commands import resolve
from agent_system.cli_utils.chat import repl as repl_mod


def _repl(*, ansi: bool, interactive: bool) -> SimpleNamespace:
    ctx = SimpleNamespace(entry_name="helper", session_id="sess-1")
    return SimpleNamespace(ctx=ctx, ansi=ansi, interactive=interactive)


def test_cls_is_a_command_of_its_own():
    assert resolve("/cls").kind == "command"
    assert resolve("/cls").name == "cls"
    assert resolve("/clear").name == "new", "/clear no longer starts a fresh session"
    assert repl_mod._COMMANDS["cls"] is repl_mod._on_cls


def test_a_terminal_loses_screen_and_scrollback(capsys):
    repl = _repl(ansi=True, interactive=True)

    repl_mod._on_cls(repl, "")

    out = capsys.readouterr().out
    assert out.startswith("\033[2J\033[3J\033[H"), repr(out[:20])
    assert "session sess-1" in out, "after clearing, nothing says where the chat stands"
    assert repl.ctx.session_id == "sess-1"


def test_output_that_is_no_terminal_gets_no_escape_codes(capsys):
    repl_mod._on_cls(_repl(ansi=True, interactive=False), "")

    out = capsys.readouterr().out
    assert "\033" not in out
    assert "not a terminal" in out
