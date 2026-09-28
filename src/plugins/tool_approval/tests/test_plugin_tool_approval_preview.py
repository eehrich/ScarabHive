"""What the person reads of a call's arguments before they allow it.

The preview is bounded, and it must never lose an argument to the bound: the
path of a file write behind its long content, or a command behind a padding
argument the tool ignores, would be approved unseen.
"""
from __future__ import annotations

from plugins.tool_approval.preview import ARGUMENTS_PREVIEW_CHARS, MAX_LEAVES, arguments_preview


def test_short_arguments_are_shown_whole_in_the_order_sent():
    text, cut = arguments_preview({"path": "/tmp/a.txt", "mode": "w", "count": 3})

    assert text == 'path: "/tmp/a.txt"\nmode: "w"\ncount: 3'
    assert cut is False


def test_a_long_value_does_not_push_a_short_one_out():
    text, cut = arguments_preview({"content": "y" * 20000, "path": "/Users/x/.zshrc"})

    assert 'path: "/Users/x/.zshrc"' in text
    assert cut is True
    assert len(text) < ARGUMENTS_PREVIEW_CHARS + 200


def test_padding_in_front_does_not_hide_the_command():
    text, _ = arguments_preview({"a": "x" * 4000, "command": "rm -rf ~/"})

    assert 'command: "rm -rf ~/"' in text


def test_a_long_value_keeps_its_head_and_its_tail():
    value = "HEAD" + "m" * 10000 + "TAIL"
    text, cut = arguments_preview({"content": value})

    assert text.startswith('content: "HEAD') and text.endswith('TAIL"')
    assert "characters" in text and cut is True


def test_a_name_cannot_push_the_arguments_after_it_out_of_view():
    """A line break in an argument's name would fill the box before the command."""
    text, cut = arguments_preview({"note" + "\n" * 150: 1, "command": "rm -rf ~/"})

    assert text.count("\n") == 1, "the name broke the preview into lines"
    assert text.endswith('command: "rm -rf ~/"')
    assert cut is True, "a shortened name was not reported"


def test_a_field_inside_a_nested_argument_is_named():
    text, cut = arguments_preview({"opts": {"a": "x" * 5000, "cmd": "rm -rf ~/", "b": "y" * 5000}})

    assert 'opts.cmd: "rm -rf ~/"' in text
    assert cut is True


def test_list_items_are_named_by_position():
    text, cut = arguments_preview({0: "key", "files": ["a", {"path": "/etc/x"}], "none": []})

    assert text == '0: "key"\nfiles[0]: "a"\nfiles[1].path: "/etc/x"\nnone: []'
    assert cut is False


def test_line_separators_are_escaped():
    text, _ = arguments_preview({"x": "a\u2028b\u2029c"})

    assert "\u2028" not in text and "\u2029" not in text


def test_past_the_limit_the_rest_is_counted_and_reported():
    text, cut = arguments_preview({f"k{i}": i for i in range(MAX_LEAVES + 7)})

    assert text.endswith("… and 7 more arguments not shown")
    assert cut is True


def test_many_long_values_are_all_named():
    arguments = {f"arg{i}": "z" * 3000 for i in range(30)}
    text, cut = arguments_preview(arguments)

    assert all(f"arg{i}: " in text for i in range(30))
    assert cut is True


def test_no_arguments():
    assert arguments_preview({}) == ("(no arguments)", False)


def test_a_script_reads_as_code_after_the_short_arguments():
    """tool_script's call is asked with its code: one line per line of code,
    each marked, behind the one-line arguments."""
    script = "files = call_tool('fs_list', {})\nfor f in files:\n    call_tool('fs_read', {'path': f})"
    text, cut = arguments_preview({"script": script, "timeout": 60})

    assert text.splitlines() == [
        "timeout: 60",
        "script: (3 lines)",
        "  │ files = call_tool('fs_list', {})",
        "  │ for f in files:",
        "  │     call_tool('fs_read', {'path': f})",
    ]
    assert cut is False


def test_a_long_script_loses_whole_lines_from_its_middle():
    lines = [f"step_{i:04d}()" for i in range(1000)]
    text, cut = arguments_preview({"script": "\n".join(lines)})

    shown = text.splitlines()
    assert shown[0] == "script: (1000 lines)" and shown[1] == "  │ step_0000()" and shown[-1] == "  │ step_0999()"
    assert any("characters left out" in line for line in shown) and cut is True
    assert all(line.startswith("  │ step_") or "left out" in line for line in shown[1:]), "a line was cut in two"


def test_a_block_cannot_pass_for_another_argument():
    text, _ = arguments_preview({"script": "x = 1\ncommand: \"ls\"\r\rrm -rf /"})

    assert "\ncommand:" not in text, "a line of the value reads as an argument"
    assert "\r" not in text


def test_code_reads_as_it_runs():
    """Bidi overrides and zero-width characters make code read otherwise than
    it runs (Trojan Source): shown escaped, in blocks and one-line values."""
    text, _ = arguments_preview({"code": 'x = 1\ny = "\u202e2\u2066"\n\u200bprint(y)\x85', "name": "a\u202eb\ufeff"})

    for char in ("\u202e", "\u2066", "\u200b", "\x85", "\ufeff"):
        assert char not in text, repr(char)
    assert "\\u202e" in text and "\\u200b" in text


def test_a_trailing_line_break_makes_a_name_not_plain():
    text, _ = arguments_preview({"cmd\n": "x", "opts": {"y\n": 1}})

    assert text.splitlines() == ['"cmd\\n": "x"', 'opts."y\\n": 1']
