import sys
from agent_system.cli_utils import common


def test_supports_color_modes(monkeypatch):
    # Ensure NO_COLOR env cleared
    monkeypatch.delenv("NO_COLOR", raising=False)

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
