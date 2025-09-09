import os
import re
import sys

# Ensure the in-repo `src/` directory is importable during tests
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from scripts.backlog_tool.commands import list as list_cmd


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_format_task_line_plain():
    class Task:
        id = "0101"
        title = "A normal title"

    class Epic:
        id = "0001"
        title = "Epic title"

    line = list_cmd._format_task_line(Task, Epic, color=False)
    assert "Task 0101: A normal title" == line


def test_format_task_line_color():
    class Task:
        id = "0101"
        title = "A very long title " + "x" * 200

    class Epic:
        id = "0001"
        title = "Epic title"

    line = list_cmd._format_task_line(Task, Epic, color=True)
    # Should contain ANSI codes but stripped content should be truncated and present
    stripped = strip_ansi(line)
    assert stripped.startswith("Task 0101: ")
    assert len(stripped) <= 1 + 4 + 2 + 100 + 10  # allow some headroom


def test_format_epic_line_color():
    class Epic:
        id = "0004"
        title = "Plugins discovery & metadata\nwith newline"

    line = list_cmd._format_epic_line(Epic, color=True)
    stripped = strip_ansi(line)
    assert "Plugins discovery & metadata" in stripped
    assert "\n" not in stripped


def test_format_epic_inline_color():
    class Epic:
        id = "0004"
        title = "Inline epic title with\nnewline and a very long tail " + ("x" * 200)

    line = list_cmd._format_epic_inline(Epic, color=True)
    stripped = strip_ansi(line)
    assert stripped.startswith("Epic 0004: ")
    assert "\n" not in stripped
    # truncated
    assert len(stripped) <= 120


def test_cmd_list_only_epics_emits_ansi(tmp_path, capsys):
    # Create a tiny backlog file with one epic and run the cmd_list logic
    # Provide a minimal valid backlog structure so the parser recognizes the
    # epic and its task. The parser expects a section header like
    # '## 1. Epics - open' or will treat the first epic line as starting the
    # open epics section; include the explicit header to be robust.
    backlog_content = """
## 1. Epics - open

Epic 0001: Test Epic

- Task 0001: Do something
"""
    file_path = tmp_path / "backlog.md"
    file_path.write_text(backlog_content)

    class Args:
        file = str(file_path)
        state = 'all'
        only = 'epics'
        ids_only = False
        color = True

    # Call cmd_list and capture stdout
    ret = list_cmd.cmd_list(Args)
    captured = capsys.readouterr()
    # Should exit 0
    assert ret == 0
    # Output should include ANSI escape sequences for colors
    assert "\x1b[" in captured.out
