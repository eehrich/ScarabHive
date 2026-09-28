"""`/undo files`, `/retry files` and `/rewind` in agent-cli's chat.

The REPL is the real run_chat_loop, dispatching the typed lines; the agent is
a real one whose turns wrote files through a real file_ops server with the
file_checkpoints plugin recording them (file_rewind_rig). Only the prompt, the
turn runner and the session save are stand-ins -- no LLM turn is taken here.
"""
from __future__ import annotations

import asyncio

import pytest

from file_rewind_rig import SESSION, USER, Rig, create, replace, tree


class _Lines:
    """The typed lines, then Ctrl-D."""

    def __init__(self, lines):
        self._lines = iter(lines)

    def read(self, prompt):
        try:
            return next(self._lines)
        except StopIteration:
            raise EOFError

    def read_continuation(self, prompt):
        return self.read(prompt)

    def reseed(self, seed):
        pass

    def remember(self, text):
        pass

    def remember_command(self, text):
        pass


def _drive(monkeypatch, agent, lines):
    """Run the REPL over ``lines`` on the rig's agent and session; the turns it would start."""
    import agent_system.cli_utils.chat as chat

    editor = _Lines(lines)
    started = []
    monkeypatch.setattr(chat, "_build_prompt_editor", lambda seed, suggest=None, loop=None: editor)
    monkeypatch.setattr(chat, "collect_plugin_commands", lambda agent_: [])
    monkeypatch.setattr(chat, "_available_skills", lambda ctx: [])
    monkeypatch.setattr(chat, "_execute_turn",
                        lambda loop, ctx, task, renderer, editor=None: started.append(task) or {})

    async def _saved(ctx):
        return True

    monkeypatch.setattr(chat, "_save_session", _saved)
    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True, raising=False)
    loop = asyncio.new_event_loop()
    try:
        chat.run_chat_loop(agent=agent, entry_name=agent.name, session_service=None,
                           session_user=USER, session_id=SESSION, was_new_session=False,
                           llm_profile="normal", show_status=False, loop=loop)
    finally:
        loop.close()
    return started


@pytest.fixture
def rig(tmp_path):
    async def build():
        built = await Rig(tmp_path).start()
        work = built.work
        (work / "a.txt").write_bytes(b"original\n")
        await built.turn("first", [replace(work / "a.txt", "original", "first")])
        await built.turn("second", [create(work / "b.txt", "second"), replace(work / "a.txt", "first", "second")])
        return built

    built = asyncio.run(build())
    yield built
    asyncio.run(built.close())


def _questions(rig):
    return [m.content for m in rig.messages() if m.role == "user"]


class TestUndoFiles:

    def test_undo_files_puts_the_files_back_and_drops_the_exchange(self, rig, monkeypatch, capsys):
        started = _drive(monkeypatch, rig.agent(), ["/undo files"])

        out = capsys.readouterr().out
        assert tree(rig.work) == {"a.txt": b"first\n"}
        assert _questions(rig) == ["first"]
        assert "1 put back, 1 removed" in out
        assert started == []

    def test_a_refused_rewind_keeps_the_exchange(self, rig, monkeypatch, capsys):
        (rig.work / "b.txt").write_text("the person's")

        _drive(monkeypatch, rig.agent(), ["/undo files"])

        out = capsys.readouterr().out
        assert _questions(rig) == ["first", "second"]
        assert (rig.work / "b.txt").read_text() == "the person's"
        assert "overwrite" in out and "the exchange stays" in out

    def test_undo_files_overwrite_puts_it_back_anyway(self, rig, monkeypatch):
        (rig.work / "b.txt").write_text("the person's")

        _drive(monkeypatch, rig.agent(), ["/undo files overwrite"])

        assert tree(rig.work) == {"a.txt": b"first\n"}
        assert _questions(rig) == ["first"]

    def test_retry_files_asks_again_after_putting_the_files_back(self, rig, monkeypatch):
        started = _drive(monkeypatch, rig.agent(), ["/retry --files"])

        assert tree(rig.work) == {"a.txt": b"first\n"}
        assert [getattr(task, "content", task) for task in started] == ["second"]

    def test_a_word_it_does_not_know_drops_nothing(self, rig, monkeypatch, capsys):
        _drive(monkeypatch, rig.agent(), ["/undo fils"])

        assert _questions(rig) == ["first", "second"]
        assert "/undo files" in capsys.readouterr().out

    def test_a_plain_undo_leaves_the_files(self, rig, monkeypatch):
        _drive(monkeypatch, rig.agent(), ["/undo"])

        assert _questions(rig) == ["first"]
        assert (rig.work / "b.txt").read_text() == "second"


class TestRewind:

    def test_bare_rewind_lists_and_a_number_rewinds_the_files_only(self, rig, monkeypatch, capsys):
        _drive(monkeypatch, rig.agent(), ["/rewind", "/rewind 1"])

        out = capsys.readouterr().out
        assert "1  turn 1" in out and "2  turn 2" in out
        assert tree(rig.work) == {"a.txt": b"original\n"}
        assert _questions(rig) == ["first", "second"], "/rewind changed the conversation"

    def test_a_number_that_names_no_checkpoint(self, rig, monkeypatch, capsys):
        _drive(monkeypatch, rig.agent(), ["/rewind 9"])

        assert "no checkpoint 9" in capsys.readouterr().out
        assert (rig.work / "b.txt").read_text() == "second"

    def test_without_the_plugin_it_says_so(self, rig, monkeypatch, capsys):
        from agent_system import file_rewind

        monkeypatch.setattr(file_rewind, "_rewinder", None)
        _drive(monkeypatch, rig.agent(), ["/rewind", "/undo files"])

        out = capsys.readouterr().out
        assert out.count("File checkpoints are off") == 2
        assert _questions(rig) == ["first", "second"], "/undo files dropped the exchange without its files"
