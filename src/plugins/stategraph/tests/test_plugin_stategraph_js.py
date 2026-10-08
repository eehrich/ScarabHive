"""The panel's scripts in JavaScriptCore: module syntax, the canvas's pure functions (tests/js/graph_tests.js), the
inspector's state fragments sent back through set_state, the whole panel against a fake kit and DOM
(tests/js/panel_smoke.js), and single cases of it against the kit's timing (tests/js/panel_cases.js).

No browser on the machine this was built on (docs/stategraph_design.md §11): jsc checks what can be checked without
one. Where jsc is not installed (it ships with macOS), node runs the same tests with jsc's globals
(tests/js/node_jsc.mjs); skipped where neither is.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from plugins.stategraph.kinds import describe_kinds
from plugins.stategraph.model.graph import graph_view
from plugins.stategraph.model.loader import SnapshotSources, load_tree

PLUGIN = Path(__file__).resolve().parents[1]
# jsc runs in a session of its own: run from a script that ran pytest, the script died with exit code 144 (seen
# 2026-09-26), and a session of its own avoided that
JSC = next((str(path) for path in (
    Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"),
    Path(shutil.which("jsc") or "/nonexistent"),
) if path.is_file()), None)

NODE = shutil.which("node")
NODE_JSC = Path(__file__).resolve().parent / "js" / "node_jsc.mjs"

pytestmark = pytest.mark.skipif(JSC is None and NODE is None, reason="neither JavaScriptCore (jsc) nor node is installed")


def run_js(script: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """``jsc -m <script>`` in cwd; node with jsc's globals where jsc is missing."""
    command = [JSC, "-m", script] if JSC else [NODE, str(NODE_JSC), script]
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                          start_new_session=True)


def module_ref(path: Path) -> str:
    """A path as a module specifier in an import, quoted: node takes an absolute path only as a file URL."""
    return json.dumps(str(path) if JSC else path.as_uri())


@pytest.mark.parametrize("script", ["static/panel.js", "static/graph.js"])
def test_the_panel_scripts_are_valid_modules(script):
    source = json.dumps(str(PLUGIN / script))
    command = [JSC, "-e", f"checkModuleSyntax(readFile({source}))"] if JSC else [NODE, "--check", str(PLUGIN / script)]

    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60, start_new_session=True)

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_canvas_functions_pass_their_tests():
    done = run_js("graph_tests.js", PLUGIN / "tests" / "js", timeout=120)

    lines = done.stdout.splitlines()
    failed = [line for line in lines if line.startswith("FAIL")]
    summary = next((line for line in lines if line.startswith("SUMMARY")), "")
    assert done.returncode == 0 and not failed, done.stdout + done.stderr
    passed, total = summary.removeprefix("SUMMARY ").split("/")
    assert passed == total and int(total) >= 10, summary


# ------------------------------------------------------------------ the inspector's fragments against set_state

ODD_LAYOUTS = {
    "inline_comment.yaml": "stategraph: 1\nid: inline_comment\ninitial: a\nstates:\n  a:\n    transitions:\n"
                           "      - target: done\n  done: {type: final}   # the end\n",
    "anchor.yaml": "stategraph: 1\nid: anchor\ninitial: a\nstates:\n  a: &base\n    description: first\n"
                   "    transitions:\n      - target: done\n  done:\n    type: final\n",
    "tag.yaml": "stategraph: 1\nid: tag\ninitial: a\nstates:\n  a: !special\n    transitions:\n      - target: done\n"
                "  done:\n    type: final\n",
    "multiline_flow.yaml": "stategraph: 1\nid: multiline_flow\ninitial: a\nstates:\n  a:\n    transitions:\n"
                           "      - target: done\n  done: {type: final,\n         description: end}\n",
    "comment_tail.yaml": "stategraph: 1\nid: comment_tail\ninitial: a\nstates:\n  a:\n    transitions:\n"
                         "      - target: done\n    # trailing note inside a\n\n  # about done\n  done:\n    type: final\n",
    "alias_use.yaml": "stategraph: 1\nid: alias_use\ninitial: a\nstates:\n  a:\n    description: &d shared\n"
                      "    transitions:\n      - target: b\n  b:\n    description: *d\n    transitions:\n"
                      "      - target: done\n  done:\n    type: final\n",
}


def test_every_state_applied_as_the_inspector_shows_it_leaves_the_file_byte_exact(tmp_path):
    """The panel's stateFragment (graph.js, in jsc) for every state of every shipped machine, sent back unchanged
    through set_state: the file must not change. Where the inspector locks a state, set_state must refuse it."""
    from plugins.stategraph.model.yamledit import EditError, apply_op

    files = {path.name: path.read_text(encoding="utf-8") for path in sorted((PLUGIN / "machines").glob("*.yaml"))}
    assert len(files) >= 5, "the shipped machines moved: point the test at them again"
    files.update(ODD_LAYOUTS)
    cases = []
    for name, text in files.items():
        graph = graph_view(load_tree(f"/m/{name}", SnapshotSources({f"/m/{name}": text})))
        assert graph["states"], f"{name}: no states, this case would check nothing"
        cases += [{"file": name, "state": state["name"], "line": state["line"]} for state in graph["states"]]
    (tmp_path / "cases.json").write_text(json.dumps({"files": files, "cases": cases}), encoding="utf-8")
    graph_js = module_ref(PLUGIN / "static" / "graph.js")
    (tmp_path / "fragments.js").write_text(
        f"import {{ fragmentLock, stateFragment }} from {graph_js};\n"
        "const { files, cases } = JSON.parse(readFile('./cases.json'));\n"
        "print(JSON.stringify(cases.map((c) => ({ ...c, yaml: stateFragment(files[c.file], c.line),"
        " lock: fragmentLock(files[c.file], c.line) }))));\n", encoding="utf-8")

    done = run_js("fragments.js", tmp_path, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    shown = json.loads(done.stdout)

    changed, unrefused = [], []
    for case in shown:
        text = files[case["file"]]
        try:
            out = apply_op(text, {"op": "set_state", "name": case["state"], "yaml": case["yaml"]})
        except EditError as exc:
            assert case["lock"], f"{case['file']}:{case['state']} refused but not locked: {exc.message}"
            continue
        if case["lock"]:
            unrefused.append(f"{case['file']}:{case['state']}")
        elif out != text:
            changed.append(f"{case['file']}:{case['state']}")
    assert len(shown) >= 30 and sum(bool(c["lock"]) for c in shown) == 3, "fixture: 3 locked states expected"
    assert changed == [] and unrefused == []


# ------------------------------------------------------------------ the panel, end to end in jsc

SMOKE_MACHINE = """\
stategraph: 1
id: review
params:
  premise: {type: string, required: true}
  rounds: {type: integer, default: 3}
events:
  approve: {description: a human says yes}
initial: write
states:
  write:
    max_visits: 5
    do:
      agent: scene_writer
      task: "{{ params.premise }}"
    transitions:
      - target: review
        effect: ctx.draft = out
      - trigger: error
        target: failed
  review:
    initial: read
    states:
      read:
        do: {machine: critique}
        transitions:
          - target: verdict
      verdict:
        type: final
    transitions:
      - target: done
  done:
    type: final
  failed:
    type: final
    status: failed
"""


def smoke_fixtures() -> str:
    """A get_machine answer built by the real graph_view, the real kinds, and a paused run in the service's shape."""
    tree = load_tree("/m/review.yaml", SnapshotSources({"/m/review.yaml": SMOKE_MACHINE}))
    problems = [p.as_dict() for p in tree.problems] + [
        {"level": "error", "code": "SG004", "message": "unknown name x", "path": "states.write.transitions[0].effect",
         "file": "/m/review.yaml", "line": 16}]
    machine = {"id": "review", "file": "/m/review.yaml", "writable": True, "root_file": "review.yaml",
               "files": {"review.yaml": SMOKE_MACHINE, "review.py": "def f():\n    return 1\n"},
               "versions": {"review.yaml": "v1", "review.py": "p1"}, "problems": problems, "graph": graph_view(tree),
               "layout": {"version": 1, "positions": {"done": {"x": 900, "y": 40}}}}
    frames = [{"machine": "review", "prefix": "", "path": "", "step": 3, "state": "read", "config": ["review", "read"],
               "visits": {"write": 2, "review": 1, "read": 1}, "ctx": {"draft": "d", "round": 2}, "params": {}, "accepts": []},
              {"machine": "critique", "prefix": "s3/m/", "path": "read", "step": 1, "state": "done", "config": ["done"],
               "visits": {}, "ctx": {}, "params": {"text": "d"}, "accepts": ["approve"]}]
    journal = [
        {"seq": 1, "kind": "trace", "key": "s0:enter:write", "state": "write", "status": "enter", "data": {"frame": ""}},
        {"seq": 2, "kind": "activity", "key": "s0", "state": "write", "status": "done",
         "data": {"kind": "agent", "out": "draft text", "meta": {"duration_s": 1.5, "mocked": True}}},
        {"seq": 3, "kind": "trace", "key": "s1:transition:write", "state": "write", "status": "transition",
         "data": {"frame": "", "from": "write", "to": "review", "event": "done", "index": 0}},
        {"seq": 4, "kind": "activity", "key": "s2", "state": "read", "status": "error",
         "data": {"kind": "machine", "error": {"type": "timeout", "message": "slow"}, "meta": {"duration_s": 30.0}}},
        {"seq": 5, "kind": "edit", "key": "s3:enter:1", "state": "read", "status": "set", "data": {"path": "ctx.round", "value": 2}},
        {"seq": 6, "kind": "event", "key": "pending:1", "state": None, "status": "pending", "data": {"name": "approve", "data": {}}}]
    run = {"id": "r1", "machine_id": "review", "status": "paused", "params": {"premise": "x"}, "output": None, "error": None,
           "final_state": None, "active": True, "terminal": False,
           "view": {"frames": frames, "live": True, "inbox": [{"name": "approve", "frame": None}]},
           "debug": {"breakpoints": [{"state": "read", "at": "enter", "machine": None, "condition": None, "enabled": True, "id": "b1"}],
                     "watchpoints": [{"expr": "ctx.round", "machine": None, "condition": None, "enabled": True, "id": "w1"}],
                     "watch": {"w1": {"value": 2, "frame": "top", "step": 3}},
                     "paused": {"frame": "", "machine": "review", "state": "read", "hook": "enter", "step": 3,
                                "reason": "breakpoint b1 at enter of read", "out": None, "error": None},
                     "mode": "run", "run_to": None},
           "accepts": [{"frame": "s3/m/", "state": "done", "events": ["approve"]}], "journal": journal}
    runs = [{"id": "r1", "machine_id": "review", "status": "paused", "created_at": "2026-09-26 01:00:00",
             "finished_at": None, "final_state": None, "error": None, "user_id": "ada", "parent_run": None, "fork_step": None}]
    return "".join(f"export const {name} = {json.dumps(value)};\n"
                   for name, value in (("MACHINE", machine), ("RUN", run), ("RUNS", runs), ("KINDS", describe_kinds()),
                                       ("HOOKS", machine_answer("hooks", HOOKS_MACHINE)),
                                       ("FIELDS", machine_answer("fields", FIELDS_MACHINE))))


#: A state of each type the inspector offers breakpoints for differently, and one it cannot apply (the anchor).
FIELDS_MACHINE = """\
stategraph: 1
id: fields
title: Fields
group: Writer/demo
params:
  text: {type: string, required: true}
context: {verdict: null}
initial: judge
states:
  judge:
    do:
      decide: choice
      input: "{{ params.text }}"
      question: Is it done?
      criteria: {done: finished, open: needs more}
      timeout: 5m
    transitions:
      - target: done
        effect: ctx.verdict = out
  idle:
    description: |
      line one
      line two
    transitions:
      - target: done
        guard: |
          (ctx.verdict is None
           and True)
  share_a:
    do: &job {agent: w, task: t}
    transitions:
      - target: share_b
  share_b:
    do: *job
    transitions:
      - target: done
  done:
    type: final
    output: {verdict: "{{ ctx.verdict }}"}
"""

HOOKS_MACHINE = """\
stategraph: 1
id: hooks
initial: work
states:
  work:
    transitions:
      - target: route
  route:
    type: choice
    transitions:
      - target: box
        guard: ctx.go
      - target: loop
        guard: else
  box:
    initial: inner
    states:
      inner:
        transitions:
          - target: inner_end
      inner_end:
        type: final
    transitions:
      - target: done
  loop:
    max_visits: 2
    initial: step
    states:
      step:
        transitions:
          - target: step_end
      step_end:
        type: final
    transitions:
      - target: shared
  shared: &base
    description: anchored
    transitions:
      - target: done
  done:
    type: final
"""


def machine_answer(machine_id: str, text: str) -> dict:
    """get_machine's answer for a one-file machine, its graph from the real graph_view."""
    path = f"/m/{machine_id}.yaml"
    tree = load_tree(path, SnapshotSources({path: text}))
    return {"id": machine_id, "file": path, "writable": True, "root_file": f"{machine_id}.yaml",
            "files": {f"{machine_id}.yaml": text}, "versions": {f"{machine_id}.yaml": "h1"},
            "problems": [p.as_dict() for p in tree.problems], "graph": graph_view(tree),
            "layout": {"version": 1, "positions": {}}}


def prepare_panel(directory: Path, kit: str, scripts: tuple[str, ...]) -> None:
    """The directory a panel script runs in: the fakes, panel_copy.js (panel.js importing ``kit`` and the real
    graph.js), fixtures.js and paths.js."""
    js = PLUGIN / "tests" / "js"
    for name in ("fake_dom.js", "fake_kit.js", *scripts):
        shutil.copy(js / name, directory / name)
    panel = (PLUGIN / "static" / "panel.js").read_text(encoding="utf-8")
    panel, kits = re.subn(r"'/static/kit/panel-kit\.js'", f"'./{kit}'", panel)
    panel, graph = re.subn(r"'\./graph\.js'", module_ref(PLUGIN / "static" / "graph.js"), panel)
    assert (kits, graph) == (1, 1), "panel.js imports changed: point the copy at the fakes again"
    (directory / "panel_copy.js").write_text(panel, encoding="utf-8")
    (directory / "fixtures.js").write_text(smoke_fixtures(), encoding="utf-8")
    elk = json.dumps(str(PLUGIN / "static" / "vendor" / "elkjs" / "elk.bundled.js"))
    (directory / "paths.js").write_text(f"export const ELK_PATH = {elk};\n", encoding="utf-8")


def test_the_panel_runs_every_main_path_against_a_fake_kit_and_dom(tmp_path):
    prepare_panel(tmp_path, "fake_kit.js", ("panel_smoke.js",))

    done = run_js("panel_smoke.js", tmp_path, timeout=180)

    lines = done.stdout.splitlines()
    failed = [line for line in lines if line.startswith("FAIL")]
    errors = next((line for line in lines if line.startswith("ERRORS")), "ERRORS ?")
    summary = next((line for line in lines if line.startswith("SUMMARY")), "")
    assert done.returncode == 0 and not failed and errors == "ERRORS []", done.stdout + done.stderr
    passed, total = summary.removeprefix("SUMMARY ").split("/")
    assert passed == total and int(total) >= 15, summary


PANEL_CASES = [
    "without_auto_save_an_edit_and_a_move_wait_for_save",
    "without_auto_save_undo_and_redo_move_the_draft_and_write_nothing",
    "turning_auto_save_on_saves_the_drafts_first_and_is_kept",
    "with_auto_save_an_undo_writes_back_the_saved_text_not_discarded_yaml",
    "without_auto_save_text_the_graph_does_not_show_is_asked_about_and_validate_draws_it",
    "without_auto_save_undo_steps_go_when_the_file_changes_under_them",
    "an_edit_answered_after_another_machine_opened_is_dropped",
    "without_auto_save_an_edit_answered_after_a_revert_is_dropped",
    "without_auto_save_text_typed_while_an_edit_is_on_its_way_is_kept",
    "auto_save_is_not_switched_under_an_edit_on_its_way",
    "a_click_on_the_open_machine_asks_before_it_drops_the_drafts",
    "a_reload_after_an_edit_conflict_asks_about_the_drafts_once",
    "breakpoints_and_watches_of_the_next_run_carry_the_open_machine",
    "a_live_run_shows_and_changes_only_the_open_machines_breakpoints",
    "the_inspector_offers_only_hooks_that_can_stop",
    "a_state_the_inspector_cannot_stand_in_for_is_shown_not_applied",
    "a_poll_tick_waits_for_the_answer_that_is_out",
    "runs_live_at_once_are_all_offered_in_the_bar_and_picked_there",
    "a_selected_line_is_bent_by_its_handles_and_kept_in_the_layout_for_its_way",
    "a_line_in_a_lane_is_bent_where_it_is_drawn_and_a_states_forms_show_its_tools",
    "a_line_whose_transition_changed_under_the_pointer_is_neither_bent_nor_kept_as_the_pointer_had_it",
    "a_bump_in_a_lane_dropped_in_line_with_its_far_side_goes",
    "a_run_shown_the_list_does_not_hold_is_offered_beside_the_live_ones_by_their_ends",
    "a_refresh_neither_drops_a_machine_click_nor_draws_the_machine_left",
    "opening_a_machine_abandons_the_refresh_in_flight",
    "a_control_answer_for_a_run_left_does_not_take_the_view",
    "the_run_list_follows_a_terminate",
    "a_failed_poll_goes_on_polling_and_says_so",
    "text_typed_into_the_inspector_is_not_dropped_unasked",
    "tab_in_a_read_only_file_types_nothing",
    "a_run_that_ended_leaves_the_debug_lists_to_the_next_run",
    "an_interrupted_run_can_be_terminated",
    "a_run_of_another_process_can_be_paused_continued_and_terminated_but_not_run_to_a_state",
    "a_click_on_a_states_handle_connects_nothing",
    "a_double_click_renames_the_state_under_the_pointer",
    "a_click_zoomed_out_selects_and_moves_nothing",
    "a_composite_is_drawn_under_the_transitions_inside_it",
    "a_composite_from_the_palette_is_one_edit_with_its_first_state",
    "ctrl_and_shift_click_select_several_states_and_delete_removes_them_in_one_edit",
    "a_selected_state_gone_after_a_reload_leaves_the_selection",
    "a_drag_of_a_selected_state_moves_every_selected_one",
    "the_first_drag_keeps_a_machine_laid_out_flow",
    "a_shift_drag_on_the_empty_canvas_selects_the_states_inside_the_band",
    "a_band_from_any_corner_selects_only_what_lies_wholly_inside_it",
    "a_selected_composite_moves_with_its_states_by_the_pointers_distance",
    "a_state_dropped_on_a_composite_goes_into_it_and_keeps_its_place",
    "a_refused_drop_keeps_the_state_where_it_was_dropped",
    "a_drop_on_a_read_only_machine_is_a_plain_move",
    "the_inspector_moves_a_state_to_another_composite_or_the_top_level",
    "a_selection_dragged_by_a_state_inside_its_composite_moves_the_composite_only",
    "a_cancelled_drag_puts_nothing_into_a_composite",
    "a_drop_answered_after_another_machine_opened_leaves_that_ones_layout_alone",
    "a_composites_last_state_is_not_removed_after_asking",
    "a_note_from_the_bar_goes_into_the_file_and_the_middle_of_the_view",
    "a_note_s_text_is_applied_and_an_emptied_note_is_removed_after_asking",
    "a_note_dragged_keeps_its_place_and_delete_removes_it_with_its_place",
    "a_state_s_description_is_free_text_at_the_top_and_applies_alone",
    "the_overview_names_the_machine_s_runner_and_the_catalog_is_its",
    "a_submachine_lists_the_runs_it_ran_in_and_its_canvas_shows_the_frame_picked",
    "a_run_from_before_the_frame_record_finds_its_frames_in_the_journal_and_shows_the_last",
    "a_catalog_that_fails_leaves_no_other_machine_s_tools_and_is_asked_again",
    "a_state_that_ran_a_submachine_lists_its_runs_in_the_inspector",
    "the_frames_of_an_interrupted_run_run_no_more",
    "the_run_s_own_graph_counts_the_frames_only_its_journal_names",
    "a_note_is_added_once_and_asked_about_only_as_it_is",
    "a_shared_notes_block_is_read_only_in_the_inspector",
    "a_composite_named_like_its_first_state_gets_another_one",
    "the_remove_button_and_its_question_name_what_goes",
    "several_states_of_a_read_only_machine_are_not_asked_about_and_the_last_ones_are_kept",
    "a_double_click_with_ctrl_renames_nothing",
    "ctrl_click_on_transitions_selects_them_with_states_and_delete_removes_them_in_one_edit",
    "transitions_of_one_state_are_removed_from_its_last_one_on",
    "group_puts_the_selected_states_into_a_composite_where_they_are_and_an_undo_puts_them_back",
    "states_of_two_levels_are_not_grouped_a_composite_takes_its_own_along_and_unplaced_ones_get_no_position",
    "a_renamed_states_position_goes_back_with_an_undo",
    "a_transition_back_between_two_states_is_drawn_beside_the_other",
    "a_moved_states_transition_stays_right_angled_and_its_line_is_set_at_once_in_its_inspector",
    "the_start_line_stays_right_angled_when_its_composite_grows",
    "a_machine_without_positions_is_drawn_top_down",
    "a_machine_dragged_before_flow_came_stays_left_to_right",
    "a_stored_line_style_no_longer_offered_is_named_right_angled_as_drawn",
    "the_machines_line_and_a_selections_lines_are_set_from_the_inspector",
    "a_renamed_state_takes_its_line_styles_along_and_an_undo_puts_them_back",
    "a_duplicate_takes_the_line_styles_along_without_positions",
    "an_internal_transition_offers_no_line",
    "an_empty_machine_asks_for_a_first_state",
    "the_result_shows_every_activitys_answer_and_the_end_states",
    "a_live_runs_result_reads_on_from_where_it_stopped",
    "machines_sit_in_their_folders_and_a_closed_folder_stays_closed",
    "a_machine_the_author_made_can_be_deleted_and_a_shipped_one_offers_no_delete",
    "a_running_composite_does_not_make_every_poll_read_again",
    "a_run_that_ends_while_its_result_is_read_is_read_to_its_end",
    "a_conflict_says_that_a_reload_drops_the_inspectors_text",
    "a_run_that_ended_reads_the_activities_it_still_had_running",
    "a_companion_module_is_added_as_two_drafts_and_saved_with_them",
    "a_python_file_is_coloured_as_python_and_indented_by_four",
    "a_machine_that_names_its_module_or_cannot_be_written_offers_none",
    "a_module_line_typed_into_the_yaml_or_a_missing_id_line_adds_nothing",
    "a_module_whose_name_a_file_has_already_is_used_as_it_is",
    "a_decision_state_shows_its_fields_from_the_kinds_schema",
    "an_activity_apply_sends_only_what_changed",
    "another_kind_keeps_what_survives_and_drops_the_rest",
    "no_kind_removes_the_activity",
    "a_final_s_settings_send_status_and_output_as_yaml",
    "the_machine_settings_are_an_update_machine",
    "an_apply_without_a_change_sends_nothing",
    "typing_offers_apply_and_an_unchanged_apply_takes_it_back",
    "a_run_again_that_fails_unfolds_the_form_and_the_test_options_say_what_they_hold",
    "a_status_filter_on_another_machine_does_not_unfold_new_run",
    "a_start_error_before_the_first_run_list_keeps_new_run_open",
    "another_machines_start_error_is_gone_with_it",
    "an_apply_on_its_way_stays_disabled_while_another_form_is_typed_into",
    "a_read_only_machine_shows_its_fields_disabled",
    "a_new_decision_takes_its_criteria_as_yaml",
    "a_number_field_is_a_number_input_and_a_typo_is_refused",
    "a_number_the_browser_cannot_read_is_refused_not_removed",
    "a_transition_sends_only_what_changed_and_a_guard_over_lines_keeps_them",
    "a_renamed_state_takes_the_next_runs_breakpoint_along",
    "a_breakpoint_the_machine_cannot_stop_at_any_more_is_dropped_at_the_start",
    "an_activitys_error_shows_its_traceback_input_failed_attempts_and_a_copyable_request",
    "a_run_that_took_no_transition_shows_the_guards_it_evaluated",
    "the_history_shows_the_time_and_one_kind_of_row_on_request",
    "a_fork_can_be_held_at_its_fork_point",
    "a_waiting_frame_says_what_it_waits_for_and_since_when",
    "a_machine_without_an_agent_offers_the_entry_that_makes_one",
    "a_machine_with_one_param_takes_the_message_as_it_and_lists_its_agents",
    "a_machine_whose_one_param_is_no_text_takes_a_json_message",
    "a_bad_state_name_is_asked_again_with_what_was_typed_and_an_unchanged_one_is_no_error",
    "the_problem_badge_opens_the_overview_that_lists_every_problem",
    "an_agent_field_offers_the_catalog_s_agents_and_says_what_it_is",
    "a_new_event_from_the_trigger_select_is_declared_and_becomes_the_trigger",
    "the_machine_overview_puts_its_settings_first_without_the_tables_they_repeat",
    "a_waiting_run_offers_its_events_in_the_bar_and_the_form_chooses_and_explains_one",
    "an_event_two_frames_wait_for_goes_to_the_form_to_pick_one",
    "the_runs_list_pages_back_and_filters_by_status",
    "run_again_starts_with_the_run_s_inputs_and_the_form_keeps_them_for_the_machine",
    "an_apply_keeps_what_another_field_form_holds_without_asking",
    "an_edit_is_undone_with_the_file_as_it_was_and_auto_layout_asks_first",
    "the_wheel_scrolls_the_graph_ctrl_wheel_zooms_and_the_search_finds_a_state",
    "a_machine_is_duplicated_under_a_new_id_with_its_own_module",
    "a_narrow_panel_folds_the_machine_list_once_a_machine_is_open_and_offers_the_palette_as_a_menu",
    "a_renamed_state_stays_shown_with_what_its_forms_hold",
    "a_timer_state_says_so_and_offers_its_after",
    "a_machine_whose_block_is_not_declared_yet_says_a_restart_offers_it",
    "a_machine_that_offers_itself_says_under_which_name",
    "a_shared_activity_is_locked_with_a_hint",
    "another_kind_without_its_key_is_refused",
    "a_text_over_lines_is_a_text_area",
    "an_activity_read_while_it_ran_is_read_again_when_it_ends",
    "a_poll_leaves_a_result_read_that_is_out_alone",
    "a_run_switched_to_takes_the_result_of_the_one_left_away_at_once",
    "text_typed_into_one_inspector_form_is_asked_about_by_another",
    "a_deleted_machine_leaves_nothing_of_itself_behind",
    "the_sessions_of_another_users_run_are_not_offered",
]


def run_case(directory: Path, case: str) -> list[str]:
    (directory / "case.js").write_text(f"export const CASE = {json.dumps(case)};\n", encoding="utf-8")
    done = run_js("panel_cases.js", directory, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.splitlines()


def test_every_panel_case_is_run(tmp_path):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    listed = next(line for line in run_case(tmp_path, "*") if line.startswith("CASES ")).split()[1:]

    assert sorted(listed) == sorted(PANEL_CASES)


@pytest.mark.parametrize("case", PANEL_CASES)
def test_panel_case(tmp_path, case):
    """One case of tests/js/panel_cases.js: the panel against the kit's timing (answers later, latest aborts)."""
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    lines = run_case(tmp_path, case)

    assert f"PASS {case}" in lines and "ERRORS []" in lines, "\n".join(lines)
