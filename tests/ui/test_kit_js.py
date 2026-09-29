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
    "yamlCode keeps the text and colours keys, values, comments, a block over blank lines and a quote over lines",
    "native confirm is refused, not blocking",
    "a dialog resolves with the pressed action",
    "a prompt resolves with the typed text",
    "dismissing a dialog resolves null",
    "tabs switch their panels",
    "tabs are as wide as their text while they fit, give way down to five characters, then the row scrolls; the wheel turns it and a chosen tab comes into sight",
    "a tab row in a column short of room keeps its height",
    "a row of page links scrolls as tabs do: its current page in sight from the start, the wheel turns it",
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
    "a tick missed while the panel was out of sight is caught up once it is back",
    "a page with unsaved input asks before it is left, until it is saved",
    "withBusy guards every control of a NodeList",
    "api refuses latest for a raw response",
    "a row to choose keeps the keyboard focus through a redraw",
    "a pane stuck beside a list fits the scroll area beneath the page head",
    "a menu opens at its button",
    "a menu stays inside the window however wide or tall it is",
    "a theme change reaches its listeners once",
    "a destructive dialog starts on the safe button",
    "the kit speaks the language the page declares",
    "api with latest abandons the older call of that name, quietly",
    "an abandoned call stays abandoned while its body is still being read",
    "pluginBase finds the plugin above its static folder",
    "update draws only a changed answer",
    "localTime reads a stored time as UTC, in the page language",
    "formQuery leaves empty fields out, setQuery writes the URL without a reload",
    "withBusy disables its controls while it runs and ignores a second call",
    "placeMenu can match the width of its anchor, and leaves a menu its own width otherwise",
    "copyText copies and says so, a refusal is an error toast",
    "notice shows a callout of its kind and hides it again",
    "emptyState and skeleton draw kit components",
    "pk-pager steps inside its pages and says where it is",
    "a data-pk-select row is chosen by click and Enter, not through its controls",
    "selectTab shows the named tab without a click",
    "isDark follows the theme choice",
    "pk-refresh with auto refreshes from the start, and its button stops it",
    "pk-refresh remembers the viewer's interval and pause per page",
    "a side pane gets the width the viewer last dragged it to, keeps a new one per pane, and resizes at its corner",
    "a side pane folds away and back with its toggle, and a panel opened again finds it as the viewer left it",
    "a panel in a tab of its own offers ScarabHive with the page it shows when followed, all sessions included while it shows them",
    "a refresh says whether the viewer or the timer asked",
    "hidden hides whatever display a component sets",
    "a panel pushed narrow scrolls sideways with its scrollbar in view, and prose tables still wrap",
    "a sortable table sorts by the head the viewer clicks: numbers biggest first, text A to Z, a second click reverses",
    "a chosen order holds through a re-render and follows its head when a column comes or goes, empty cells last",
    "the order the markup names holds until the viewer picks one, and ties keep the rendered order",
    "timestamps sort as points in time, with or without a fraction of a second, in any offset, and without one as UTC",
    "a column sorts as numbers only when all its values but the blank ones are numbers, else all as text, whatever order the rows came in",
    "a head holding a control of its own is left as it is",
    "the order a viewer picks outlives a reload of the panel, and only a plain name of the last few is kept",
    "the keyboard stays on the column head it sorted with when the table is drawn anew",
    "following the chat the session control names it, and with no choice to make it keeps out of the way",
    "the scope picked says what a panel asks about, and the scope already shown asks nothing",
    "a link pins a panel to a session: the control names it, and the way back keeps the rest of the link",
    "the session control still reports the chat switching after it was moved in the page",
    "a page the shell opened set to all sessions starts on all of them, a shell docking it hears so, and a scope picked there drops that start from the address",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_panel_kit(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    """The list is what turns a check into a test: one added to the page but not
    here ran and was never looked at. (test_shell_browser.py keeps the same guard,
    after six checks were found in exactly that state.)"""
    unexpected = sorted(set(results) - set(EXPECTED))
    assert not unexpected, f"checks the page ran that EXPECTED does not name: {unexpected}"
