"""/help <topic> in the terminal chat: the AmigaGuide viewer (cli_utils/help_viewer.py).

The renderer runs on the guides that ship with the repo (src/plugins, the public part), through the
real ``Library.page``; the search is the real ``_find`` with ``help_index.find_help`` replaced, so no
embedding model is loaded. The chat tests drive the real ``run_chat_loop``.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from agent_system.cli_utils import common
from agent_system.cli_utils import help_viewer
from agent_system.cli_utils.help_viewer import MORE, Viewer, open_help
from agent_system.ui import help_index
from agent_system.ui.help import Library
from test_cli_chat import _RecordingEditor, drive_chat_repl

REPO = Path(__file__).resolve().parents[2]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(scope="module")
def library():
    return Library([REPO / "src" / "plugins"])


def _no_search(coro):
    coro.close()
    raise AssertionError("this test does not search")


def _run(coro):
    """The chat's runner, without a chat: (finished, result)."""
    return True, asyncio.run(coro)


def _viewer(library, answers=(), *, interactive=True, width=80, height=1000, ansi=False, run=_no_search):
    """A viewer whose read() answers from *answers* and records where it stood when asked."""
    replies = iter(answers)
    asked = []

    def read(prompt):
        shown = viewer.shown or {}
        asked.append((prompt, f"search:{shown['query']}" if "query" in shown else
                      "/".join(shown["place"].values()) if shown else None))
        try:
            return next(replies)
        except StopIteration:
            raise AssertionError(f"asked {prompt!r} with no answer left") from None

    viewer = Viewer(library, read=read, run=run, ansi=ansi, interactive=interactive, width=width, height=height)
    return viewer, asked


def _page(library, capsys, guide, node, **kwargs):
    viewer, _ = _viewer(library, interactive=False, **kwargs)
    assert viewer.open({"guide": guide, "node": node}), f"{guide}/{node} did not open"
    return viewer, capsys.readouterr().out


class TestRenderRealGuides:
    def test_links_are_numbered_in_reading_order_and_open_their_node(self, library, capsys):
        viewer, out = _page(library, capsys, "todo", "main")
        numbered = re.findall(r"\[(\d+)\] ([^\n]+)", out)
        assert numbered[:6] == [("1", "The Todos panel"), ("2", "The todo tool"),
                                ("3", "Duplicates and dependencies"), ("4", "The task-list hook"),
                                ("5", "What the model sees"), ("6", "Setting it up")]
        assert [link["node"] for link in viewer.links[:2]] == ["panel", "tool"]
        assert "[image: The Todos panel with the tasks of a session]" in out
        assert "An agent working on something bigger than one answer keeps a task list" in out

    def test_table_columns_line_up(self, library, capsys):
        _, out = _page(library, capsys, "todo", "tool", width=80)
        lines = out.splitlines()
        header = next(i for i, line in enumerate(lines) if line.split() == ["operation", "Needs", "Does"])
        rows = lines[header + 2:header + 9]
        assert rows[0].split()[:2] == ["create", "title"], "fixture: not the Operations table"
        needs, does = lines[header].index("Needs"), lines[header].index("Does")
        for row in rows:
            if row[2] != " ":  # a row of its own; the other is a cell's wrapped line ("adds a note")
                assert row[needs - 2:needs] == "  " and row[needs] != " ", row
            assert row[does - 2:does] == "  " and row[does] != " ", row

    def test_a_narrow_table_wraps_inside_its_columns(self, library, capsys):
        _, out = _page(library, capsys, "todo", "tool", width=40)
        lines = out.splitlines()
        header = next(i for i, line in enumerate(lines) if line.split() == ["Parameter", "Used", "by", "Meaning"])
        table = lines[header:lines.index("", header)]
        assert len(table) > 20, "fixture: the Parameters table is shorter than expected"
        assert max(map(len, table)) <= 40
        meaning = lines[header].index("Meaning")
        assert all(line[meaning - 2:meaning] == "  " for line in table if len(line) > meaning)
        # wrapped, not cut: the last words of a long cell are still there
        assert "with a timestamp" in " ".join(line[meaning:] for line in table)

    def test_a_code_block_is_verbatim_and_indented(self, library, capsys):
        _, out = _page(library, capsys, "todo", "tool", width=40)
        assert ('\n    {"operation": "create", "title": "Run the migration on staging", '
                '"depends_on": ["task_001"]}\n') in out

    def test_a_console_without_utf8_gets_ascii_for_every_glyph(self, library, capsys, monkeypatch):
        """A cp1252 console turned box lines, quotes and ellipses into "?"."""
        viewer, _ = _viewer(library, interactive=False)
        viewer.unicode = False
        viewer.say("Help for “todo” · …")
        viewer.open({"guide": "todo", "node": "tool"})  # a table: box-drawn rules
        out = capsys.readouterr().out
        assert 'Help for "todo" - ...' in out
        assert not set(out) & set("─│“”·…•"), "a glyph the console cannot show"

    def test_no_ansi_when_colour_is_off(self, library, capsys):
        _, out = _page(library, capsys, "todo", "tool", ansi=False)
        assert "The todo tool" in out
        assert "\x1b" not in out

    def test_colour_on_styles_headings_and_links(self, library, capsys):
        _, out = _page(library, capsys, "todo", "tool", ansi=True)
        assert "\x1b[1;36mOperations\x1b[0m" in out
        assert "\x1b[4;36m[1]" in out
        assert ANSI.sub("", out) == _page(library, capsys, "todo", "tool", ansi=False)[1]

    def test_a_readme_guide_shows_its_markdown_as_text(self, tmp_path, capsys):
        plugin = tmp_path / "readme_only"
        (plugin / "docs").mkdir(parents=True)
        (plugin / "plugin.toml").write_text('[plugin]\nname = "readme_only"\ndescription = "A test plugin"\n',
                                            encoding="utf-8")
        (plugin / "README.md").write_text(
            "# Readme Only\n\nA paragraph with **bold** text.\n\n- first item\n- second item\n\n"
            "```\nraw <code> line\n```\n\nSee [the design](docs/design.md).\n", encoding="utf-8")
        (plugin / "docs" / "design.md").write_text("# Design\n\nHow it is built.\n", encoding="utf-8")
        viewer, out = _page(Library([tmp_path]), capsys, "readme_only", "main")
        assert "Readme Only\n===========" in out
        assert "A paragraph with bold text." in out
        assert "  • first item\n  • second item" in out
        assert "    raw <code> line" in out
        assert "See [1] the design." in out
        assert "<p>" not in out and "<strong>" not in out
        viewer.open(viewer.links[0])
        assert "How it is built." in capsys.readouterr().out


class TestNavigation:
    def test_link_back_next_previous_and_quit(self, library):
        viewer, asked = _viewer(library, ["2", "b", "n", "p", "c", "q"])
        assert viewer.start("todo/main")
        viewer.browse()
        assert [where for _, where in asked] == [
            "todo/main", "todo/tool", "todo/main", "todo/panel", "todo/main", "todo/main"]

    def test_search_hits_are_picked_by_number_and_retraced(self, library, monkeypatch):
        calls = []

        async def find_help(library_, query, *, limit):
            calls.append((query, limit))
            return {"query": query, "exact": None, "hits": [
                {"guide": "todo", "node": "tool", "title": "The todo tool", "database": "todo",
                 "snippet": "one tool", "score": 0.9},
                {"guide": "todo", "node": "rules", "title": "Duplicates and dependencies", "database": "todo",
                 "snippet": "duplicates", "score": 0.8}]}

        monkeypatch.setattr(help_index, "find_help", find_help)
        viewer, asked = _viewer(library, ["2", "b", "1", "q"], run=_run)
        assert viewer.start("keep a task list")
        viewer.browse()
        assert calls == [("keep a task list", help_viewer.SEARCH_LIMIT)]
        assert [where for _, where in asked] == [
            "search:keep a task list", "todo/rules", "search:keep a task list", "todo/tool"]

    def test_an_exact_answer_opens_directly(self, library, monkeypatch):
        async def find_help(library_, query, *, limit):
            return {"query": query, "exact": {"guide": "todo", "node": "setup", "title": "Setting it up"},
                    "hits": [{"guide": "todo", "node": "tool", "title": "The todo tool", "database": "todo",
                              "snippet": "", "score": 0.5}]}

        monkeypatch.setattr(help_index, "find_help", find_help)
        viewer, asked = _viewer(library, ["q"], run=_run)
        assert viewer.start("setting it up")
        viewer.browse()
        assert asked[0][1] == "todo/setup"

    @pytest.mark.parametrize("leave", [KeyboardInterrupt, EOFError])
    def test_ctrl_c_and_ctrl_d_leave_the_viewer(self, leave, capsys):
        def read(prompt):
            raise leave

        open_help("manual", None, read=read, run=_no_search, ansi=False, interactive=True)
        out = capsys.readouterr().out
        assert "ScarabHive" in out, "fixture: the manual was not shown"
        assert "could not be shown" not in out, "leaving the viewer was reported as a failure"

    def test_a_long_node_pages_and_q_stops_it(self, library, capsys):
        viewer, asked = _viewer(library, ["", "q", "q"], height=12)
        assert viewer.start("todo/tool")
        viewer.browse()
        out = capsys.readouterr().out
        assert [prompt for prompt, _ in asked] == [MORE, MORE, "help> "]
        assert "The todo tool" in out
        assert "never one it expects" not in out, "q did not stop the page"

    def test_not_a_terminal_prints_everything_and_asks_nothing(self, library, capsys):
        viewer, asked = _viewer(library, interactive=False, height=12)
        assert viewer.start("todo/tool")
        assert asked == []
        assert "never one it expects" in capsys.readouterr().out


class TestChatWiring:
    @pytest.fixture(autouse=True)
    def _plain_tall_terminal(self, monkeypatch):
        monkeypatch.setattr(common, "color_mode", "never")
        monkeypatch.setenv("LINES", "1000")
        monkeypatch.setenv("COLUMNS", "100")

    @staticmethod
    def _turns():
        sent = []

        def probe(loop, ctx, task, renderer, editor=None):
            sent.append(task)
            return {}
        return sent, probe

    def test_help_alone_lists_the_commands_and_names_the_manual(self, monkeypatch, capsys):
        sent, probe = self._turns()
        drive_chat_repl(monkeypatch, ["/help"], turn_probe=probe)
        out = capsys.readouterr().out
        assert "Commands:" in out and "/exit" in out
        assert "Manual: /help manual -- or /help <question or topic>" in out
        assert sent == []

    def test_help_manual_browses_without_a_turn(self, monkeypatch, capsys):
        sent, probe = self._turns()
        drive_chat_repl(monkeypatch, ["/help manual", "2", "b", "q", "hello"], turn_probe=probe)
        out = capsys.readouterr().out
        assert sent == ["hello"], "a line typed in the viewer reached the agent"
        assert "What is ScarabHive   ScarabHive · scarabhive/intro" in out
        assert out.count("ScarabHive   scarabhive/main") == 2

    def test_ctrl_d_in_the_viewer_goes_back_to_the_chat(self, monkeypatch, capsys):
        class _EofInTheViewer(_RecordingEditor):
            def read_continuation(self, prompt):
                raise EOFError

        sent, probe = self._turns()
        drive_chat_repl(monkeypatch, ["/help manual", "hello"], turn_probe=probe,
                        editor=_EofInTheViewer(["/help manual", "hello"]))
        out = capsys.readouterr().out
        assert "ScarabHive   scarabhive/main" in out
        assert "could not be shown" not in out
        assert sent == ["hello"]
