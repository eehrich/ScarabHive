"""activity.js in a real browser: above the sessions the shell shows what works now -- every agent's turn with the
sub-agents below it and the step each is on --, what waits for the person and what just ended; a click opens a run,
Stop ends one. The stream is a stand-in (shell_activity_tests.html)."""
from __future__ import annotations

import pytest

from tests.ui.browser import find_browser, run_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")


@pytest.fixture(scope="module")
def results():
    return run_test_page(BROWSER, "tests/ui/shell_activity_tests.html")


EXPECTED = [
    "the stream it follows is the run events of the person",
    "while nothing works, waits or just ended the tree stays out of the way",
    "a turn at work shows with its agent, what it is about and the step it is on",
    "a sub-agent stands below the turn that started it, and its own below it",
    "work below a session whose turn has ended stands on its own",
    "a click opens the run",
    "the session open in the chat is marked",
    "Stop asks the server to end the run, once, and the run leaves when it has ended",
    "a run another process holds says so, and offers no Stop that does nothing",
    "a run gone quiet is marked",
    "a run that needs its person is listed so, with what it wants",
    "what just ended is listed, the newest first, without the sub-agents, and only so many",
    "a turn begun again is at work again",
    "an event from before what is known of a run changes nothing",
    "a run that begins anew under an id the page knew counts its events from its own start",
    "of the ended runs only the newest are remembered",
    "a snapshot is what there is: what it does not name is gone",
    "with nothing left to say the tree is out of the way again",
    "how long is told by the minute",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_activity_tree(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    assert sorted(results) == sorted(EXPECTED)
