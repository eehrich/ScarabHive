"""The panel items of the review of 2026-10-15 (docs/review_2026-10-15.md, UI-2 ... UI-17): the panel against the
kit's timing (tests/js/panel_cases.js, its R15 cases), the canvas against a fake DOM and the vendored ELK
(tests/js/canvas_cases_r15.js), ELK in a real worker thread (tests/js/elk_worker_r15.mjs), the callback page's script
and the panel template.

The helpers come from test_plugin_stategraph_js through the package: a run with another ``pythonpath`` (a mutated copy
of src) prepares the panel from that copy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.stategraph.tests.test_plugin_stategraph_js import JSC, NODE, PLUGIN, module_ref, prepare_panel, run_case, run_js
from plugins.stategraph.web_endpoints import StateGraphWebEndpoints

needs_js = pytest.mark.skipif(JSC is None and NODE is None, reason="neither JavaScriptCore (jsc) nor node is installed")

R15_CASES = [
    "r15_a_double_click_on_move_down_sends_one_edit",
    "r15_a_run_whose_first_read_fails_is_asked_again_and_the_bar_says_so",
    "r15_a_failed_refresh_keeps_the_machine_list_and_says_so",
    "r15_kinds_that_failed_at_start_are_asked_again",
    "r15_mock_only_and_pause_at_start_stay_with_their_machine",
    "r15_writer_run_ids_keep_their_label_and_hash_and_the_list_is_not_sorted_here",
    "r15_fork_with_the_saved_machine_is_a_named_action",
    "r15_resume_says_which_machine_it_runs",
    "r15_a_large_machine_opens_readable_and_find_zooms_to_the_state",
    "r15_a_run_id_alone_opens_its_machine_with_the_run",
    "r15_a_failed_runs_state_leads_to_the_graph",
    "r15_breakpoint_and_watch_conditions_are_set_and_points_switched_off",
    "r15_a_breakpoint_switched_on_again_in_the_inspector_keeps_its_condition",
    "r15_a_refresh_read_before_an_edit_does_not_put_the_old_machine_back",
]

CANVAS_CASES = [
    "an_answer_whose_elk_input_is_unchanged_is_not_laid_out_again",
    "a_worker_answers_the_layouts_and_one_that_fails_hands_them_to_the_page",
    "a_large_machine_opens_readable_on_a_state_and_find_zooms_while_a_click_does_not",
    "transitions_take_the_keyboard_a_focused_state_moves_by_the_arrows_and_the_marks_have_names",
    "a_run_id_keeps_its_label_and_hash",
]


@needs_js
def test_every_r15_panel_case_is_run(tmp_path):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    listed = next(line for line in run_case(tmp_path, "r15:*") if line.startswith("CASES ")).split()[1:]

    assert sorted(listed) == sorted(R15_CASES)


@needs_js
@pytest.mark.parametrize("case", R15_CASES)
def test_r15_panel_case(tmp_path, case):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    lines = run_case(tmp_path, case)

    assert f"PASS {case}" in lines and "ERRORS []" in lines, "\n".join(lines)


@needs_js
def test_the_canvas_keeps_its_layout_views_large_machines_readably_and_takes_the_keyboard(tmp_path):
    js = PLUGIN / "tests" / "js"
    for name in ("fake_dom.js", "canvas_cases_r15.js"):
        shutil.copy(js / name, tmp_path / name)
    elk = json.dumps(str(PLUGIN / "static" / "vendor" / "elkjs" / "elk.bundled.js"))
    (tmp_path / "canvas_paths.js").write_text(
        f"export const GRAPH_JS = {module_ref(PLUGIN / 'static' / 'graph.js')};\nexport const ELK_PATH = {elk};\n",
        encoding="utf-8")

    done = run_js("canvas_cases_r15.js", tmp_path, timeout=120)

    lines = done.stdout.splitlines()
    listed = next((line for line in lines if line.startswith("CASES ")), "CASES").split()[1:]
    assert done.returncode == 0 and "ERRORS []" in lines, done.stdout + done.stderr
    assert sorted(listed) == sorted(CANVAS_CASES)
    assert [f"PASS {case}" for case in CANVAS_CASES if f"PASS {case}" not in lines] == [], done.stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed: the worker check runs in node's worker threads")
def test_elk_lays_out_in_a_worker_of_its_own():
    """The canvas's worker source, run where a page's worker would run it: the vendored ELK answers from there."""
    script = PLUGIN / "tests" / "js" / "elk_worker_r15.mjs"
    elk = PLUGIN / "static" / "vendor" / "elkjs" / "elk.bundled.js"

    done = subprocess.run([NODE, str(script), (PLUGIN / "static" / "graph.js").as_uri(), str(elk)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120, start_new_session=True)

    assert done.stdout.strip().splitlines()[-1:] == ["LAID OUT 6"], done.stdout + done.stderr


# ------------------------------------------------------------------ UI-10: the callback page

def callback_script() -> str:
    """The script of the page a callback link opens, from the real handler."""
    import asyncio

    async def found(method, token):
        return {"event": "approve", "machine_id": "review"}

    page = asyncio.run(StateGraphWebEndpoints.callback_page(SimpleNamespace(_call=found), None, "t0k3n"))
    return re.search(r"<script>(.*)</script>", page.body.decode("utf-8"), re.S).group(1)


CALLBACK_HARNESS = """
const said = { textContent: '' };
const button = { disabled: false };
let clicked;
globalThis.location = { href: 'https://hive.test/plugins/stategraph/callback?token=t0k3n' };
globalThis.document = { getElementById: (id) => (id === 'said' ? said : { set onclick(fn) { clicked = fn; } }) };
const answers = {
  network: () => Promise.reject(new TypeError('Failed to fetch')),
  crash: async () => ({ ok: false, status: 500, statusText: 'Internal Server Error', json: async () => { throw new SyntaxError('not JSON'); } }),
  used: async () => ({ ok: false, status: 410, statusText: 'Gone', json: async () => ({ detail: 'the link was used' }) }),
  sent: async () => ({ ok: true, status: 200, statusText: 'OK', json: async () => ({}) }),
};
const shown = {};
for (const [name, answer] of Object.entries(answers)) {
  globalThis.fetch = answer;
  button.disabled = false;
  said.textContent = '';
  await clicked({ target: button });
  shown[name] = { said: said.textContent, disabled: button.disabled };
}
console.log(JSON.stringify(shown));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_callback_page_says_why_a_send_failed_and_lets_it_be_tried_again(tmp_path):
    (tmp_path / "callback.mjs").write_text(f"{CALLBACK_HARNESS.split('const shown')[0]}{callback_script()}\n"
                                           f"const shown{CALLBACK_HARNESS.split('const shown')[1]}", encoding="utf-8")

    done = subprocess.run([NODE, str(tmp_path / "callback.mjs")], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, start_new_session=True)

    assert done.returncode == 0, done.stdout + done.stderr
    shown = json.loads(done.stdout.strip().splitlines()[-1])
    assert shown["network"] == {"said": "Not sent: the server could not be reached. Try again.", "disabled": False}
    assert shown["crash"] == {"said": "Not sent: 500 Internal Server Error", "disabled": False}
    assert shown["used"] == {"said": "Not sent: the link was used", "disabled": False}
    assert shown["sent"] == {"said": "Sent.", "disabled": True}


# ------------------------------------------------------------------ UI-5: the refresh ticks from the start

def test_the_panel_refreshes_by_itself_from_the_start():
    template = (PLUGIN / "templates" / "panel.html").read_text(encoding="utf-8")

    refresh = re.search(r"<pk-refresh\b[^>]*>", template)

    assert refresh and re.search(r"\bauto\b", refresh.group(0)) and 'interval="10"' in refresh.group(0), refresh
