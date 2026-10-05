import sys
from agent_system.cli_utils import common


def test_supports_color_modes(monkeypatch):
    # Ensure NO_COLOR env cleared, and a terminal that is not dumb
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    # A Windows console renders ANSI only once VT processing is enabled; that
    # probe needs a real console handle, which pytest's captured stdout is not.
    monkeypatch.setattr(common, "_enable_windows_vt", lambda: True)

    # auto + stdout isatty -> True
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    common.set_color_mode("auto")
    assert common.supports_color() is True

    # auto + not a TTY -> False
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    common.set_color_mode("auto")
    assert common.supports_color() is False

    # always -> True regardless of isatty
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    common.set_color_mode("always")
    assert common.supports_color() is True

    # never -> False regardless
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    common.set_color_mode("never")
    assert common.supports_color() is False


def test_colorize_wraps_when_supported(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    common.set_color_mode("always")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    txt = "YES"
    wrapped = common.colorize(txt, "32")
    assert "YES" in wrapped
    assert "\x1b[32m" in wrapped and wrapped.endswith("\x1b[0m")

    # when not supported, returns original
    common.set_color_mode("never")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert common.colorize(txt, "32") == txt


def test_auto_yields_text_when_the_console_cannot_render_ansi(monkeypatch):
    """A Windows console is a TTY but prints ESC[90m literally until VT
    processing is on. Deciding on isatty() alone put raw escapes in the output.
    """
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(common, "_IS_WINDOWS", True)
    monkeypatch.setattr(common, "_enable_windows_vt", lambda: False)

    common.set_color_mode("auto")
    assert common.get_output_format() == "text"
    assert common.colorize("x", "90") == "x"


def test_always_overrides_the_capability_check(monkeypatch):
    """'always' is an explicit user override -- e.g. piping into something that
    does render ANSI -- and must not be second-guessed."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(common, "_enable_windows_vt", lambda: False)

    common.set_color_mode("always")
    assert common.supports_color() is True


def test_non_windows_needs_no_console_probe(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(common, "_IS_WINDOWS", False)

    def _boom():
        raise AssertionError("the Windows console probe must not run on POSIX")

    monkeypatch.setattr(common, "_enable_windows_vt", _boom)
    assert common.ansi_capable_stdout() is True


def test_input_mode_snapshot_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(common, "_IS_WINDOWS", False)
    assert common.snapshot_console_input_mode() is None


def test_input_mode_restore_accepts_none(monkeypatch):
    """None = no console at snapshot time (piped stdin); restore must be a
    silent no-op, never a crash at the prompt."""
    monkeypatch.setattr(common, "_IS_WINDOWS", True)
    common.restore_console_input_mode(None)  # must not raise


def test_input_mode_roundtrip_is_safe_without_console():
    """Under pytest stdin is piped: snapshot yields None (no console handle),
    and restoring that is a no-op end to end."""
    common.restore_console_input_mode(common.snapshot_console_input_mode())
