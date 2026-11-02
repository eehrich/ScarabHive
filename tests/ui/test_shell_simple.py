"""simple.js in a real browser: the simple view of the shell -- the panels the admins chose, topics and Quick or
Thorough in the composer, the view switch in the account menu and the start page (shell_simple_tests.html)."""
from __future__ import annotations

import pytest

from tests.ui.browser import find_browser, run_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")


@pytest.fixture(scope="module")
def results():
    return run_test_page(BROWSER, "tests/ui/shell_simple_tests.html")


EXPECTED = [
    "without the view route the shell shows everything, as before there were views",
    "when the server cannot say, an admin sees everything and anyone else the simple view",
    "the simple view lists its panels in the order chosen, leaving out what the catalogue lacks",
    "in the composer, topics and Quick or Thorough take the place of agent, model and thinking",
    "a topic picks its agent and profile quietly: the old choice is cleared before the agent changes",
    "Thorough takes the thorough profile and asks for more thinking; Quick goes back",
    "the topic and depth chosen are there again on the next visit",
    "a loaded session is followed: the bar shows its agent and sets nothing, the next new chat the topic picked",
    "a new chat /agent asks for stays on that agent; the next one is the topic picked again",
    "what /think, /model and a rewind change, the depth buttons show",
    "Thorough holds with the storage full: the new chat goes on what was pressed",
    "an admin switches the view in the account menu; a user is told who chose theirs",
    "switching tells the server the view chosen, then loads the page again",
    "the start page shows the areas as tiles and the recent conversations",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_simple_view(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    assert sorted(results) == sorted(EXPECTED)
