"""Status lines must stay readable at a glance.

They are emitted into the same stream that carries the model's tokens, so a
command that wraps over several lines pushes everything around it out of view.
"""
from plugins.terminal.server import (
    _CMD_DISPLAY_LIMIT,
    _output_size,
    _short_cmd,
)

# The kind of one-liner that made the status output unreadable.
PIPELINE = (
    "cd data/workspace && awk 'NR>=255 && NR<=305 {printf \"%d: %s\\n\", NR, $0}' "
    "tunnel.asm | grep -n 'lsl.b\\|or.b\\|move.b  d2,d3'"
)


class TestShortCmd:
    def test_short_command_is_passed_through(self):
        assert _short_cmd("ls -la") == "ls -la"

    def test_long_command_is_capped(self):
        out = _short_cmd(PIPELINE)
        assert len(out) == _CMD_DISPLAY_LIMIT
        assert out.endswith("...")
        assert out.startswith("cd data/workspace && awk")

    def test_never_returns_more_than_the_limit(self):
        """A shortener that lengthens its input is a bug: with the ellipsis
        appended after a full-limit slice, inputs just over the limit came back
        longer than they went in."""
        for length in range(_CMD_DISPLAY_LIMIT - 2, _CMD_DISPLAY_LIMIT + 6):
            assert len(_short_cmd("x" * length)) <= _CMD_DISPLAY_LIMIT

    def test_newlines_and_runs_of_space_collapse(self):
        """A multi-line heredoc must not turn one status line into five."""
        assert _short_cmd("foo \n\t  bar\n\nbaz") == "foo bar baz"

    def test_no_line_break_survives_even_when_capped(self):
        assert "\n" not in _short_cmd("x\n" * 200)

    def test_empty_command_is_safe(self):
        assert _short_cmd("") == ""


class TestOutputSize:
    def test_reports_line_count(self):
        assert _output_size({"stdout": "a\nb\nc\n"}) == "3 lines"

    def test_unterminated_last_line_still_counts(self):
        assert _output_size({"stdout": "a\nb"}) == "2 lines"

    def test_singular_for_one_line(self):
        assert _output_size({"stdout": "only\n"}) == "1 line"

    def test_truncated_output_is_marked_not_reported_as_the_total(self):
        """The executor caps stdout at max_output_kb. Reporting the surviving
        line count as if it were the whole output is wrong by orders of
        magnitude for something like `ls -R /`."""
        assert _output_size({"stdout": "a\nb\n", "truncated": True}) == "2+ lines (truncated)"

    def test_empty_output_is_stated_not_zero(self):
        """'no output' answers the actual question -- e.g. a grep that matched
        nothing exits 0 and looks like a success otherwise."""
        assert _output_size({"stdout": ""}) == "no output"

    def test_missing_and_none_stdout_are_safe(self):
        assert _output_size({}) == "no output"
        assert _output_size({"stdout": None}) == "no output"


class TestConsoleModeRestore:
    """Every bash -c spawn resets the inherited console's VT flag on Windows;
    the executor puts it back so status lines after the command still render
    ANSI instead of literal escapes."""

    async def test_execute_restores_console_mode(self, monkeypatch):
        import plugins.terminal.executor as executor_mod
        calls = []
        monkeypatch.setattr(executor_mod, "_restore_console_mode",
                            lambda: calls.append(1))
        from plugins.terminal.platform_detect import PlatformDetector
        from plugins.terminal.security import CommandSecurityValidator
        bash_path, _shell = PlatformDetector().detect_bash()
        ex = executor_mod.CommandExecutor(
            bash_path=bash_path,
            security_validator=CommandSecurityValidator(),
        )
        result = await ex.execute("echo hi")
        assert result["status"] == "success"
        assert calls, "execute() must restore the console mode afterwards"

    async def test_execute_restores_even_on_timeout(self, monkeypatch):
        import plugins.terminal.executor as executor_mod
        calls = []
        monkeypatch.setattr(executor_mod, "_restore_console_mode",
                            lambda: calls.append(1))
        from plugins.terminal.platform_detect import PlatformDetector
        from plugins.terminal.security import CommandSecurityValidator
        bash_path, _shell = PlatformDetector().detect_bash()
        ex = executor_mod.CommandExecutor(
            bash_path=bash_path,
            security_validator=CommandSecurityValidator(),
        )
        result = await ex.execute("sleep 30", timeout=1)
        assert result["status"] == "error"
        assert calls, "the finally must run on the timeout path too"
