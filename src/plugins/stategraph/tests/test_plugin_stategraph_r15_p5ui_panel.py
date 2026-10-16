"""The panel's every-machine run list without an open machine (FEAT-7) and the run's own cost against its own
limit (FEAT-6), review of 2026-10-15: tests/js/panel_cases.js, its p5 cases, against the kit's timing."""

from __future__ import annotations

import pytest

from plugins.stategraph.tests.test_plugin_stategraph_js import JSC, NODE, prepare_panel, run_case

needs_js = pytest.mark.skipif(JSC is None and NODE is None, reason="neither JavaScriptCore (jsc) nor node is installed")

P5_CASES = [
    "p5_without_a_machine_the_runs_of_every_machine_are_listed_and_open_with_their_machine",
    "p5_the_every_machine_list_filters_by_tags_pages_back_and_a_tick_neither_aborts_nor_wipes_it",
    "p5_the_result_shows_the_runs_own_cost_against_its_own_limit",
]


@needs_js
def test_every_p5_panel_case_is_run(tmp_path):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    listed = next(line for line in run_case(tmp_path, "p5:*") if line.startswith("CASES ")).split()[1:]

    assert sorted(listed) == sorted(P5_CASES)


@needs_js
@pytest.mark.parametrize("case", P5_CASES)
def test_p5_panel_case(tmp_path, case):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    lines = run_case(tmp_path, case)

    assert f"PASS {case}" in lines and "ERRORS []" in lines, "\n".join(lines)
