"""The shell's help buttons in a real browser: the manual from the header, a plugin's guide from its panel.

The real shell against the stub server of test_shell_browser.py; its catalogue gives the
Memory panel a guide (the ``help`` a catalogue entry carries for a documented plugin), and
the Help panel reads the real manual and a Memory README -- so it moves on its own, as it does
for a reader, and tells the shell each page it shows.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.ui import test_shell_browser as shell
from tests.ui.browser import run_app_test_page

PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(shell.BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

EXPECTED = [
    "the header help button opens the manual",
    "a docked panel with a guide shows the dock help button, which opens its guide",
    "a panel's help button leaves the reader on the page of its guide they are on",
    "a panel without a guide leaves the dock help button idle, its place kept",
    "the header help button brings the manual back after the reader moved on",
    "the header help button leaves the reader on the manual page they are on",
    "a window's help button opens the guide as a window above it, one without a guide has none",
    "the header keeps every button in view on a narrow screen",
]

PLUGIN_PANELS = shell.plugin_panels


def panels_with_a_guide():
    panels = PLUGIN_PANELS()
    for panel in panels:
        if panel.id == "memory":
            panel.help = "memory"
    return panels


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    memory = tmp_path_factory.mktemp("plugins") / "memory"
    memory.mkdir()
    (memory / "plugin.toml").write_text('[plugin]\nname = "memory"\ndescription = "Stored memories"\n', encoding="utf-8")
    (memory / "README.md").write_text("# Memory plugin\n\nStored memories. See [the notes](docs/notes.md).\n",
                                      encoding="utf-8")
    (memory / "docs").mkdir()
    (memory / "docs" / "notes.md").write_text("# Notes\n\nMemory notes page.\n", encoding="utf-8")
    app = shell.stub_app()
    app.state.config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[str(memory.parent)], servers={}))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shell, "plugin_panels", panels_with_a_guide)
        return run_app_test_page(shell.BROWSER, app, "tests/ui/shell_help_tests.html", timeout=PAGE_TIMEOUT)


@pytest.mark.parametrize("name", EXPECTED)
def test_shell_help(results, name):
    assert results.get(name) == "ok", results
