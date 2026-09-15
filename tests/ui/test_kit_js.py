"""panel-kit.js in a real browser: escaping, dialogs, tabs, api errors.

The escaping is the security-relevant part: panels build markup with html``
instead of concatenating strings into innerHTML.
"""
from __future__ import annotations

import pytest

from tests.ui.browser import find_browser, run_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")


@pytest.fixture(scope="module")
def results():
    return run_test_page(BROWSER, "tests/ui/kit_js_tests.html")


EXPECTED = [
    "html escapes interpolated markup",
    "html keeps nested html and joins arrays",
    "trusted markup passes unchanged",
    "jsonView escapes keys and strings",
    "jsonView shows every level, strings as text with their line breaks, arrays as lists",
    "native confirm is refused, not blocking",
    "a dialog resolves with the pressed action",
    "a prompt resolves with the typed text",
    "dismissing a dialog resolves null",
    "tabs switch their panels",
    "autoRefresh starts and stops",
    "api turns an HTTP error into ApiError",
    "overlays cast a shadow",
    "an empty tab list does not break the kit",
    "tabs are wired once however often initTabs runs",
    "pk-refresh falls back to a sane interval",
    "icon escapes its size",
    "api reports a broken body like any other failure",
    "a frame that is not the shell answers dialogs itself",
    "tabs rendered later still switch",
    "the handshake delivers early calls and visibility",
    "a menu opens at its button",
    "a menu stays inside the window however wide or tall it is",
    "a theme change reaches its listeners once",
    "a destructive dialog starts on the safe button",
    "pk-refresh with auto refreshes from the start, and its button stops it",
    "a refresh says whether the viewer or the timer asked",
    "hidden hides whatever display a component sets",
    "a panel pushed narrow scrolls sideways with its scrollbar in view, and prose tables still wrap",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_panel_kit(results, name):
    assert results.get(name) == "ok", results
