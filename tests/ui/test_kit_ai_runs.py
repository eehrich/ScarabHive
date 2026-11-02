"""ai-tasks.js in a real browser: a started agent is followed in the panel's side sheet -- its words, its tool calls,
its result or why it failed -- without a switch to the chat. The plugin is a stand-in (kit_ai_runs_tests.html)."""
from __future__ import annotations

import pytest

from tests.ui.browser import find_browser, run_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")


@pytest.fixture(scope="module")
def results():
    return run_test_page(BROWSER, "tests/ui/kit_ai_runs_tests.html")


EXPECTED = [
    "before any run the runs button stays hidden",
    "the AI choices open visibly and leave the panel usable, even with their host in a hidden tab",
    "a started agent is followed in a side sheet beside the panel, not in the chat",
    "what the agent says and calls appears while it runs, its words as text",
    "the panel stays at hand while the sheet is open",
    "a run at work shows its step in plain words, in the page language, and how long it has been at it",
    "the running time goes on without redrawing what a person reads",
    "an open run shows its steps as a timeline, the one at work now marked",
    "a run that went quiet says so, and a sign of life takes it back",
    "when the run ends the sheet shows its result, a toast says so and the panel redraws",
    "a run that fails says why",
    "closed, the sheet comes back with the runs button, which counts the running ones",
    "a run is opened and closed by its toggle, one at a time",
    "a run cut off by a restart says so, and can be answered to go on",
    "an answer being typed survives the redraws while another run goes on, and goes on in the same session",
    "a run at work can be stopped, after a question",
    "a result to check shows what was changed, what could go wrong and how it was checked, and lets a person ask first",
    "a result to check says so, where to look, and is settled with \"Geprüft\"",
    "a run handed over says so and is settled with \"Gesehen\"",
    "a question of the agent is shown with its options, counted as needing you, and kept through heartbeats",
    "the answer goes to the route of the question, and the run is at work again",
    "an empty answer is not sent, and one that comes too late is told the question no longer waits",
    "several options may be picked where the question allows it",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_ai_runs(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    assert sorted(results) == sorted(EXPECTED)
