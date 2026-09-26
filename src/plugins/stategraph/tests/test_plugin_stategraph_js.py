"""The panel's scripts in JavaScriptCore: module syntax, the canvas's pure functions (tests/js/graph_tests.js), the
inspector's state fragments sent back through set_state, the whole panel against a fake kit and DOM
(tests/js/panel_smoke.js), and single cases of it against the kit's timing (tests/js/panel_cases.js).

No browser on the machine this was built on (docs/stategraph_design.md §11): jsc checks what can be checked without
one. Skipped where jsc is not installed (it ships with macOS).
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

pytestmark = pytest.mark.skipif(JSC is None, reason="JavaScriptCore (jsc) is not installed")


@pytest.mark.parametrize("script", ["static/panel.js", "static/graph.js"])
def test_the_panel_scripts_are_valid_modules(script):
    source = json.dumps(str(PLUGIN / script))

    done = subprocess.run([JSC, "-e", f"checkModuleSyntax(readFile({source}))"], capture_output=True, text=True,
                          timeout=60, start_new_session=True)

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_canvas_functions_pass_their_tests():
    done = subprocess.run([JSC, "-m", "graph_tests.js"], cwd=PLUGIN / "tests" / "js", capture_output=True, text=True,
                          timeout=120, start_new_session=True)

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
    graph_js = json.dumps(str(PLUGIN / "static" / "graph.js"))
    (tmp_path / "fragments.js").write_text(
        f"import {{ fragmentLock, stateFragment }} from {graph_js};\n"
        "const { files, cases } = JSON.parse(readFile('./cases.json'));\n"
        "print(JSON.stringify(cases.map((c) => ({ ...c, yaml: stateFragment(files[c.file], c.line),"
        " lock: fragmentLock(files[c.file], c.line) }))));\n", encoding="utf-8")

    done = subprocess.run([JSC, "-m", "fragments.js"], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                          start_new_session=True)
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
                                       ("HOOKS", machine_answer("hooks", HOOKS_MACHINE))))


#: A state of each type the inspector offers breakpoints for differently, and one it cannot apply (the anchor).
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
    panel, graph = re.subn(r"'\./graph\.js'", json.dumps(str(PLUGIN / "static" / "graph.js")), panel)
    assert (kits, graph) == (1, 1), "panel.js imports changed: point the copy at the fakes again"
    (directory / "panel_copy.js").write_text(panel, encoding="utf-8")
    (directory / "fixtures.js").write_text(smoke_fixtures(), encoding="utf-8")
    elk = json.dumps(str(PLUGIN / "static" / "vendor" / "elkjs" / "elk.bundled.js"))
    (directory / "paths.js").write_text(f"export const ELK_PATH = {elk};\n", encoding="utf-8")


def test_the_panel_runs_every_main_path_against_a_fake_kit_and_dom(tmp_path):
    prepare_panel(tmp_path, "fake_kit.js", ("panel_smoke.js",))

    done = subprocess.run([JSC, "-m", "panel_smoke.js"], cwd=tmp_path, capture_output=True, text=True, timeout=180,
                          start_new_session=True)

    lines = done.stdout.splitlines()
    failed = [line for line in lines if line.startswith("FAIL")]
    errors = next((line for line in lines if line.startswith("ERRORS")), "ERRORS ?")
    summary = next((line for line in lines if line.startswith("SUMMARY")), "")
    assert done.returncode == 0 and not failed and errors == "ERRORS []", done.stdout + done.stderr
    passed, total = summary.removeprefix("SUMMARY ").split("/")
    assert passed == total and int(total) >= 15, summary


PANEL_CASES = [
    "a_click_on_the_open_machine_asks_before_it_drops_the_drafts",
    "a_reload_after_an_edit_conflict_asks_about_the_drafts_once",
    "breakpoints_and_watches_of_the_next_run_carry_the_open_machine",
    "a_live_run_shows_and_changes_only_the_open_machines_breakpoints",
    "the_inspector_offers_only_hooks_that_can_stop",
    "a_state_the_inspector_cannot_stand_in_for_is_shown_not_applied",
    "a_poll_tick_waits_for_the_answer_that_is_out",
    "a_refresh_neither_drops_a_machine_click_nor_draws_the_machine_left",
    "opening_a_machine_abandons_the_refresh_in_flight",
]


def run_case(directory: Path, case: str) -> list[str]:
    (directory / "case.js").write_text(f"export const CASE = {json.dumps(case)};\n", encoding="utf-8")
    done = subprocess.run([JSC, "-m", "panel_cases.js"], cwd=directory, capture_output=True, text=True, timeout=120,
                          start_new_session=True)
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
