"""The panel fixes of the review before the commit (review of 2026-10-15): a poll with more open rows than one read
lists, the New run fold under a tag filter or every machine's runs, and layout PUTs one at a time --
tests/js/panel_cases.js, its final cases, against the kit's timing."""

from __future__ import annotations

import pytest

from plugins.stategraph.tests.test_plugin_stategraph_js import JSC, NODE, prepare_panel, run_case

needs_js = pytest.mark.skipif(JSC is None and NODE is None, reason="neither JavaScriptCore (jsc) nor node is installed")

FINAL_CASES = [
    "final_more_open_rows_than_one_read_lists_are_read_whole_and_a_rewound_row_never_again",
    "final_a_tag_filter_or_every_machine_leaves_new_run_undecided",
    "final_a_held_arrow_key_sends_one_layout_at_a_time_and_the_newest_last",
]


@needs_js
def test_every_final_panel_case_is_run(tmp_path):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    listed = next(line for line in run_case(tmp_path, "final:*") if line.startswith("CASES ")).split()[1:]

    assert sorted(listed) == sorted(FINAL_CASES)


@needs_js
@pytest.mark.parametrize("case", FINAL_CASES)
def test_final_panel_case(tmp_path, case):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    lines = run_case(tmp_path, case)

    assert f"PASS {case}" in lines and "ERRORS []" in lines, "\n".join(lines)
