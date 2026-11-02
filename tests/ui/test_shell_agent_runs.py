"""agent_runs.js in a real browser: the shell's head shows the agents at work -- how many, what the newest does in
plain words, which went quiet -- and opens the panel that started one. The stream is a stand-in
(shell_agent_runs_tests.html)."""
from __future__ import annotations

import pytest

from tests.ui.browser import find_browser, run_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")


@pytest.fixture(scope="module")
def results():
    return run_test_page(BROWSER, "tests/ui/shell_agent_runs_tests.html")


EXPECTED = [
    "the stream it follows is the run events of the person",
    "while no agent works the pill stays out of the way",
    "an agent at work shows with what it does now, in plain words",
    "the step is told in the reader's language where the agent names it so",
    "a second agent counts, the one heard of last leads",
    "a click opens the panel that started the leading run",
    "an agent that went quiet is marked, and a sign of life takes the mark back",
    "the bell asks once to allow notifications, and goes",
    "a run done leaves the pill; one that failed stays as needing the person until seen",
    "a result to check comes first: a click leads to it, before the runs at work",
    "while the person looks elsewhere, a run that needs them is told as a notification",
    "a run that ends leaves the pill, and with the last one the pill goes",
    "an agent that asks its person is counted as needing them, named so, and told while they look elsewhere",
    "a run silent at a reconnect is marked at once",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_agent_runs_pill(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    assert sorted(results) == sorted(EXPECTED)
