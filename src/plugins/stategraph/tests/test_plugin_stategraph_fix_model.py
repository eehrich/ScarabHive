"""Review fixes in the model layer and the machine store (loader, code, validate, spec, graph, yamledit, store).

Machines are YAML text through the real loader and validator; the store tests use a ``MachineStore`` on
``tmp_path``, the run tests the testkit's ``Harness``.

Mutation checks run (each in a copy of src, the named tests red; the unmutated copy green):
- loader.MachineTree.snapshot: returns the host paths (no ``portable_snapshot``)  -> test_a_snapshot_*_portable
- loader.load_snapshot: no ``portable_snapshot`` on read                        -> test_a_snapshot_stored_with_*
- code.load_module: no ``sys.modules[name] = module``                           -> test_a_companion_module_*_real_module
- code.load_module: no ``weakref.finalize``                                    -> test_a_companion_module_leaves_*
- code._bind: no recursion into nested statements                              -> test_the_scan_finds_*
- code._bind: an imported name is no function                                  -> test_the_scan_finds_*
- code._bind: an assigned callable is no function                              -> test_the_scan_finds_*
- code._bind: a data literal counts as a function                              -> test_the_scan_finds_*
- store._lf: returns the text unchanged                                        -> test_a_save_with_crlf_*
- store._put_back: restores nothing                                            -> test_a_save_that_fails_*
- validate._check_schema: never called                                         -> test_an_invalid_json_schema_*
- spec.TransitionSpec._guard: keeps a blank guard                              -> test_a_blank_guard_*
- code.analyse: no read-only misuse                                            -> test_assigning_a_read_only_*
- loader.yaml_path: the whole location                                         -> test_a_union_field_*
- validate._check_template: checks the field as one value                      -> test_a_template_problem_*
- code.compile_expression/scan_template/scan_module: no ``_TOO_DEEP`` branch   -> test_code_nested_too_deeply_*
- spec.check_name: ``match`` instead of ``fullmatch``                          -> test_a_name_with_a_trailing_*
- yamledit._transition / _set_state: no ``untied`` check                       -> test_an_edit_of_yaml_shared_*
- store.FileSources.read: catches only FileNotFoundError, IsADirectoryError    -> test_a_directory_as_companion_*
- graph._walk: the old ``wait`` expression                                     -> test_the_graph_s_wait_flag_*
- loader._load: ``version != FORMAT_VERSION`` alone                            -> test_a_format_version_*
- loader._reference: keeps ``\\`` / accepts absolute paths                     -> test_references_with_backslashes_*
- yamledit._set_state: refuses any tie / renders a tie                         -> test_set_state_beside_an_anchor_*,
                                                                                  test_an_edit_of_yaml_shared_*
- code._stored: recursive                                                      -> test_a_long_expression_*
- code._OPERATORS / _is_data: arithmetic no data, any operator data, recursive -> test_arithmetic_is_data_*,
                                                                                  test_unpacking_*, test_a_long_*
- loader._load / yamledit._Edit: no ``RecursionError`` branch                  -> test_yaml_nested_too_deeply_*
- code._bind/_assigned: unpacking judged as a whole, unknown unpacking data    -> test_unpacking_*
- loader.yaml_bounds: not called / no memo (hangs); yamledit: file, fragment   -> test_yaml_that_aliases_expand_*
- yamledit: merging body tied, ``_reached`` stops at a shared node, heirs       -> test_a_merging_state_s_own_*
"""

from __future__ import annotations

import gc
import os
import sys
import textwrap
from pathlib import Path

import pytest

from plugins.stategraph import store as store_module
from plugins.stategraph.engine.interpreter import Frame
from plugins.stategraph.engine.machine import compile_tree
from plugins.stategraph.model.code import load_module
from plugins.stategraph.model.graph import graph_view
from plugins.stategraph.model.loader import load_snapshot, parse_yaml, to_plain
from plugins.stategraph.model.spec import check_name
from plugins.stategraph.model.yamledit import EditError, apply_op
from plugins.stategraph.store import MachineStore, version_of
from plugins.stategraph.tests.stategraph_testkit import FakeBackend, Harness, errors, found, load, validate


def machine_store(tmp_path: Path, files: dict[str, str]) -> tuple[MachineStore, Path]:
    root = tmp_path / "machines"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text), encoding="utf-8")
    root.mkdir(exist_ok=True)
    return MachineStore([str(root)], [str(root)], base=tmp_path), root


def problems(tree) -> list[tuple[str, str, int | None]]:
    return [(p.code, p.path, p.line) for p in tree.problems]


# ------------------------------------------------------------------ H1: portable snapshots

TREE = {
    "m.yaml": """\
        stategraph: 1
        id: m
        python: m.py
        imports: {sub: ./parts/s.yaml, byid: t}
        initial: a
        states:
          a:
            do: {machine: sub}
            transitions: [{target: b}]
          b:
            do: {machine: byid}
            transitions: [{target: done, guard: f() == 1}]
          done: {type: final}
        """,
    "m.py": "def f():\n    return 1\n",
    "parts/s.yaml": "stategraph: 1\nid: s\npython: s.py\ninitial: x\nstates:\n  x: {type: final}\n",
    "parts/s.py": "def g():\n    return 2\n",
    "t.yaml": "stategraph: 1\nid: t\ninitial: x\nstates:\n  x: {type: final}\n",
}


def test_a_snapshot_holds_paths_relative_to_the_root_file_with_slashes_and_is_portable(tmp_path):
    store, _ = machine_store(tmp_path, TREE)
    snapshot = store.load("m", execute_python=True).snapshot()

    assert snapshot["root"] == "m.yaml"
    assert sorted(snapshot["files"]) == ["m.py", "m.yaml", "parts/s.py", "parts/s.yaml", "t.yaml"]
    assert snapshot["ids"] == {"m": "m.yaml", "s": "parts/s.yaml", "t": "t.yaml"}
    again = load_snapshot(snapshot)
    assert again.problems == []
    assert again.files["parts/s.yaml"].namespace.function("g", "test")() == 2


def test_a_snapshot_stored_with_windows_paths_still_loads():
    """A run started before snapshots were portable: absolute Windows keys, one file on another drive."""
    files = {rel: textwrap.dedent(text) for rel, text in TREE.items()}
    legacy = {
        "root": "C:\\sg\\machines\\m.yaml",
        "files": {"C:\\sg\\machines\\m.yaml": files["m.yaml"], "C:\\sg\\machines\\m.py": files["m.py"],
                  "C:\\sg\\machines\\parts\\s.yaml": files["parts/s.yaml"],
                  "C:\\sg\\machines\\parts\\s.py": files["parts/s.py"], "D:\\shipped\\t.yaml": files["t.yaml"]},
        "ids": {"m": "C:\\sg\\machines\\m.yaml", "s": "C:\\sg\\machines\\parts\\s.yaml", "t": "D:\\shipped\\t.yaml"},
    }

    tree = load_snapshot(legacy)

    assert tree.problems == []
    assert tree.root_file.namespace.function("f", "test")() == 1
    assert tree.root_file.imports == {"sub": "parts/s.yaml", "byid": "D:/shipped/t.yaml"}


# ------------------------------------------------------------------ H6: companion modules are modules

MODELS = """\
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel


class Note(BaseModel):
    text: str


class Review(BaseModel):
    notes: list[Note]
    parent: Optional[Review] = None


@dataclass
class Item:
    name: str
    tags: list[str] = field(default_factory=list)


def review(raw):
    return Review.model_validate(raw).model_dump()


def item(name):
    return Item(name).tags
"""


def test_a_companion_module_with_dataclasses_and_pydantic_models_loads_as_a_real_module():
    tree = load({"m.yaml": "stategraph: 1\nid: m\npython: m.py\ninitial: a\nstates:\n  a: {type: final}\n",
                  "m.py": MODELS}, execute=True)

    assert tree.problems == []
    namespace = tree.root_file.namespace
    assert namespace.function("review", "test")({"notes": [{"text": "a"}], "parent": {"notes": []}}) == {
        "notes": [{"text": "a"}], "parent": {"notes": [], "parent": None}}
    assert namespace.function("item", "test")("x") == []


def test_a_companion_module_leaves_sys_modules_with_its_namespace_and_versions_never_share_one():
    first = load_module("def f():\n    return 1\n", "m.py")
    second = load_module("def f():\n    return 2\n", "m.py")
    one = first.function("f", "test").__module__
    two = second.function("f", "test").__module__

    assert one != two and one in sys.modules and two in sys.modules
    assert (first.function("f", "test")(), second.function("f", "test")()) == (1, 2)
    del first
    gc.collect()
    assert one not in sys.modules and two in sys.modules


# ------------------------------------------------------------------ H7: the scan sees what the run sees

SCANNED = """\
from json import dumps
import functools
try:
    import rapidfuzz as fast
except ImportError:
    fast = None
if True:
    def helper(value):
        return True
build = functools.partial(dict, a=1)
shout = lambda text: text.upper()
LIMITS = {"max": 3}
declared: int
"""


def scanned_machine(states: str) -> dict[str, str]:
    return {"m.yaml": "stategraph: 1\nid: m\npython: m.py\ninitial: a\nstates:\n" + textwrap.indent(
        textwrap.dedent(states), "  "), "m.py": SCANNED}


def test_the_scan_finds_names_in_blocks_imported_functions_and_assigned_callables():
    tree = validate(scanned_machine("""\
        a:
          do: {call: dumps, args: {obj: 1}}
          transitions: [{target: b, guard: "(fast is None or helper(out)) and LIMITS['max'] > 1"}]
        b:
          do: {call: build}
          transitions: [{target: c}]
        c:
          do: {call: shout, args: {text: hi}}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert errors(tree) == []

    data = validate(scanned_machine("""\
        a:
          do: {call: LIMITS}
          transitions: [{target: done, guard: declared > 0}]
        done: {type: final}
        """))
    assert sorted(p.message for p in errors(data)) == [
        "call: the companion module defines no function 'LIMITS'",
        "unknown name 'declared' (in scope: ctx, params, out, error, event, run, activity, the companion module, "
        "builtins)"]


# ------------------------------------------------------------------ H8 and atomic saves

CRLF = ("stategraph: 1\r\nid: x\r\ninitial: a\r\nstates:\r\n  a:\r\n    type: final\r\n    output: |\r\n"
        "      line1\r\n      line2\r\n")


def test_a_save_with_crlf_writes_what_it_reads_back_and_its_version_holds(tmp_path):
    store, root = machine_store(tmp_path, {})

    versions = store.write_files("x", {"x.yaml": CRLF})

    assert b"\r" not in (root / "x.yaml").read_bytes()
    _, back = store.read("x")
    assert back == CRLF.replace("\r\n", "\n")
    assert versions == {"x.yaml": version_of(back)}
    store.write_files("x", {"x.yaml": CRLF}, versions)  # the version it returned is the file's: no conflict


def test_a_save_that_fails_on_a_later_file_leaves_the_tree_as_it_was(tmp_path, monkeypatch):
    store, root = machine_store(tmp_path, {"x.yaml": "OLD yaml\n", "x.py": "OLD py\n"})
    expected = {"x.yaml": version_of("OLD yaml\n"), "x.py": version_of("OLD py\n")}
    replace = os.replace

    def failing(source, target):
        if Path(target).name == "x.py":
            raise PermissionError(13, "locked")
        return replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", failing)
    with pytest.raises(PermissionError, match=r"^x\.py: locked; nothing was saved$"):
        store.write_files("x", {"x.yaml": "NEW yaml\n", "x.layout.json": "{}", "x.py": "NEW py\n"}, expected)

    assert (root / "x.yaml").read_text(encoding="utf-8") == "OLD yaml\n"
    assert (root / "x.py").read_text(encoding="utf-8") == "OLD py\n"
    assert sorted(p.name for p in root.iterdir()) == ["x.py", "x.yaml"], "the new file is gone, no temp file left"


# ------------------------------------------------------------------ H15: JSON schemas

def test_an_invalid_json_schema_is_a_problem_at_its_bad_leaf():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        events:
          go:
            data: {type: objekt}
        initial: a
        states:
          a:
            do:
              agent: w
              task: t
              schema:
                type: object
                properties:
                  n: {type: integer, minimum: low}
            transitions: [{target: b}]
          b:
            transitions: [{trigger: go, target: done}]
          done: {type: final}
        """})

    assert [(p.code, p.path, p.line) for p in errors(tree)] == [
        ("SG001", "events.go.data.type", 5), ("SG005", "states.a.do.schema.properties.n.minimum", 15)]
    assert all(p.message.startswith("not a valid JSON schema: ") for p in errors(tree))


# ------------------------------------------------------------------ a blank guard

@pytest.mark.parametrize("guard", ["''", "'   '"])
async def test_a_blank_guard_is_no_guard_for_the_validator_and_the_engine(tmp_path, guard):
    files = {"m.yaml": f"""\
        stategraph: 1
        id: m
        initial: a
        states:
          a:
            transitions:
              - target: done
                guard: {guard}
          done: {{type: final}}
        """}
    assert validate(files).problems == []

    harness = Harness(tmp_path)
    try:
        row = await harness.run(files, backend=FakeBackend())
    finally:
        await harness.close()
    assert (row["status"], row["error"]) == ("succeeded", None)
    assert load(files).root_file.spec.states["a"].transitions[0].guard is None


# ------------------------------------------------------------------ read-only scopes

def test_assigning_a_read_only_scope_is_sg004_but_a_local_of_that_name_is_not():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        params: {n: {type: integer, default: 1}}
        initial: a
        states:
          a:
            do: {agent: w, task: t}
            transitions:
              - target: b
                effect: params.n = 2
              - trigger: error
                target: b
                effect: error["message"] = "seen"
          b:
            do: {agent: w, task: t}
            transitions:
              - target: done
                effect: |
                  for event in out:
                      event["seen"] = True
                  ctx.n = params.n
          done: {type: final}
        """})

    assert [(p.path, p.message) for p in found(tree, "SG004")] == [
        ("states.a.transitions[0].effect", "params is read-only: only ctx takes assignments"),
        ("states.a.transitions[1].effect", "error is read-only: only ctx takes assignments")]


# ------------------------------------------------------------------ problem paths

def test_a_union_field_s_problem_is_one_problem_at_its_yaml_path():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        vars: 5
        events: {go: {}}
        initial: a
        states:
          a:
            timeout: [1]
            transitions: [{trigger: go, target: done}]
          done: {type: final}
        """})
    assert [(p.path, p.line, p.message) for p in tree.problems] == [
        ("vars", 3, "Input should be a valid dictionary or a valid string"),
        ("states.a.timeout", 8, "Input should be a valid integer or a valid number or a valid string")]

    activity = validate({"m.yaml": """\
        stategraph: 1
        id: m
        initial: a
        states:
          a:
            do:
              agent: w
              task: t
              retry: {attempts: 2, backoff: [1]}
            transitions: [{target: done}]
          done: {type: final}
        """})
    assert [(p.code, p.path, p.line) for p in activity.problems] == [("SG005", "states.a.do.retry.backoff", 9)]


def test_a_template_problem_points_at_the_leaf_that_holds_the_expression():
    tree = validate({"m.yaml": """\
        stategraph: 1
        id: m
        context: {a: 1}
        initial: a
        states:
          a:
            type: final
            output:
              one: "{{ ctx.a }}"
              two:
                deep: "{{ nope }}"
        """})
    assert [(p.code, p.path, p.line) for p in tree.problems] == [("SG004", "states.a.output.two.deep", 11)]


# ------------------------------------------------------------------ pathological code

def test_code_nested_too_deeply_for_the_parser_is_a_problem_not_a_crash():
    deep = "+".join(["1"] * 50000)
    tree = validate({"m.yaml": f"""\
        stategraph: 1
        id: m
        python: m.py
        initial: a
        states:
          a:
            transitions: [{{target: b, guard: '{deep}'}}]
          b:
            type: final
            output: '{{{{ {deep} }}}}'
        """, "m.py": f"x = {deep}\n"})

    assert [(p.code, p.file, p.path) for p in errors(tree)] == [
        ("SG004", "m.py", ""), ("SG004", "m.yaml", "states.a.transitions[0].guard"),
        ("SG004", "m.yaml", "states.b.output")]
    assert all("nested too deeply" in p.message for p in errors(tree))


# ------------------------------------------------------------------ names

def test_a_name_with_a_trailing_newline_is_no_name():
    with pytest.raises(ValueError, match="must match"):
        check_name("m\n", "machine id")
    tree = validate({"m.yaml": 'stategraph: 1\nid: "m\\n"\ninitial: a\nstates:\n  a: {type: final}\n'})
    assert [(p.path, "must match" in p.message) for p in tree.problems] == [("id", True)]
    with pytest.raises(EditError, match="must match"):
        apply_op("stategraph: 1\nid: m\ninitial: a\nstates:\n  a: {type: final}\n",
                 {"op": "add_state", "name": "b\n"})


# ------------------------------------------------------------------ yamledit and shared YAML

MERGED = """\
stategraph: 1
id: m
initial: a
states:
  a: &base
    do: {agent: writer, task: hi}
    transitions:
      - target: c
  b:
    <<: *base
    max_visits: 2
  c:
    type: final
"""

ANCHORED = """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: &w
      agent: writer
      task: hi
    transitions:
      - target: b
  b:
    do: *w
    transitions:
      - target: c
  c:
    type: final
"""


def test_an_edit_of_yaml_shared_through_a_merge_or_an_alias_is_refused_with_a_message():
    with pytest.raises(EditError, match="shared with another place"):
        apply_op(MERGED, {"op": "remove_transition", "source": "a", "index": 0})
    with pytest.raises(EditError, match="shared with another place"):
        apply_op(ANCHORED, {"op": "set_state", "name": "a",
                            "yaml": "do:\n  agent: editor\n  task: hi\ntransitions:\n  - target: b\n"})

    out = apply_op(ANCHORED, {"op": "update_transition", "source": "a", "index": 0, "fields": {"description": "d"}})
    assert "do: &w" in out and "do: *w" in out, "an edit beside the anchor keeps it and its alias"


# ------------------------------------------------------------------ store

def test_a_directory_as_companion_module_is_a_load_problem(tmp_path):
    store, _ = machine_store(tmp_path, {"x.yaml": "stategraph: 1\nid: x\npython: .\ninitial: a\nstates:\n"
                                                  "  a: {type: final}\n"})

    tree = store.load("x")

    assert problems(tree) == [("SG004", "python", 3)]


# ------------------------------------------------------------------ graph

def test_the_graph_s_wait_flag_is_the_engine_s_wait_state():
    tree = load({"m.yaml": """\
        stategraph: 1
        id: m
        events: {go: {}}
        initial: work
        states:
          work:
            do: {agent: w, task: t}
            transitions: [{target: idle}]
          idle:
            transitions: [{trigger: error, target: listen}]
          listen:
            transitions: [{trigger: go, target: still}]
          still: {}
          done: {type: final}
        """})
    machine = compile_tree(tree)

    waits = {state["name"]: state["wait"] for state in graph_view(tree)["states"]}
    assert waits == {name: Frame.is_wait_state(node) for name, node in machine.nodes.items()}
    assert waits == {"work": False, "idle": True, "listen": True, "still": True, "done": False}


# ------------------------------------------------------------------ format version

@pytest.mark.parametrize("version", ["true", "1.0"])
def test_a_format_version_other_than_the_integer_1_is_refused(version):
    tree = validate({"m.yaml": f"stategraph: {version}\nid: m\ninitial: a\nstates:\n  a: {{type: final}}\n"})
    assert [(p.code, p.path) for p in tree.problems] == [("SG001", "stategraph")]


# ------------------------------------------------------------------ second review round

def test_references_with_backslashes_mean_the_same_on_disk_and_in_the_run(tmp_path):
    store, root = machine_store(tmp_path, {
        "m.yaml": "stategraph: 1\nid: m\npython: sub\\m.py\nimports: {x: .\\sub\\x.yaml}\ninitial: a\nstates:\n"
                  "  a:\n    do: {machine: x}\n    transitions: [{target: b, guard: f() == 1}]\n  b: {type: final}\n",
        "sub/m.py": "def f():\n    return 1\n",
        "sub/x.yaml": "stategraph: 1\nid: x\ninitial: a\nstates:\n  a: {type: final}\n"})
    checked = store.load("m")
    assert checked.problems == []
    assert load_snapshot(store.load("m", execute_python=True).snapshot()).problems == []

    sub = (root / "sub").as_posix()  # inside the machine roots: it loads from disk, and would be missing in a run
    (root / "m.yaml").write_text(f"stategraph: 1\nid: m\npython: {sub}/m.py\nimports: {{x: {sub}/x.yaml}}\n"
                                 "initial: a\nstates:\n  a: {type: final}\n", encoding="utf-8")
    assert [(p.code, p.path, "is absolute" in p.message) for p in store.load("m").problems] == [
        ("SG004", "python", True), ("SG006", "imports.x", True)]


def test_set_state_beside_an_anchor_keeps_it_and_its_alias():
    out = apply_op(ANCHORED, {"op": "set_state", "name": "a", "yaml": "description: writes\ndo: &w\n  agent: writer\n"
                                                                        "  task: hi\ntransitions:\n  - target: b\n"})

    assert "description: writes\n    do: &w\n" in out and "do: *w" in out
    assert out.replace("    description: writes\n", "") == ANCHORED


def test_a_long_expression_in_a_companion_module_is_scanned():
    module = "PROMPT = " + " + ".join(['"a"'] * 1000) + "\n"
    tree = validate({"m.yaml": "stategraph: 1\nid: m\npython: m.py\ninitial: a\nstates:\n  a:\n    type: final\n"
                               "    output: '{{ PROMPT }}'\n", "m.py": module})
    assert tree.problems == []


def test_arithmetic_is_data_and_an_alternative_of_callables_a_function():
    module = ("import json\nfast = None\nLIMIT = -1\nHOUR = 60 * 60\nBOTH = 1 and 2\n"
              "pick = fast or json.dumps\n")
    tree = validate({"m.yaml": textwrap.dedent("""\
        stategraph: 1
        id: m
        python: m.py
        initial: a
        states:
          a:
            do: {call: LIMIT}
            transitions: [{target: b}]
          b:
            do: {call: HOUR}
            transitions: [{target: c}]
          c:
            do: {call: BOTH}
            transitions: [{target: d}]
          d:
            do: {call: pick}
            transitions: [{target: done}]
          done: {type: final}
        """), "m.py": module})
    assert [p.path for p in errors(tree)] == ["states.a.do.call", "states.b.do.call", "states.c.do.call"]


def test_yaml_nested_too_deeply_is_a_problem_not_a_crash():
    text = ("stategraph: 1\nid: m\ncontext:\n  x: " + "[" * 400 + "]" * 400
            + "\ninitial: a\nstates:\n  a: {type: final}\n")

    assert [(p.code, p.message) for p in validate({"m.yaml": text}).problems] == [
        ("SG001", "YAML: nested too deeply to read")]
    with pytest.raises(EditError, match="nested too deeply"):
        apply_op(text, {"op": "add_state", "name": "b"})


# ------------------------------------------------------------------ third review round

def test_unpacking_pairs_targets_with_values_and_operators_over_names_may_be_functions():
    module = ("import json\nimport functools\nload, dump = json.loads, json.dumps\nfirst, second = functools.partial"
              "(dict), 2\nA, B = 1, 2\nleft, right = json.dumps\np = dump | load\nHOUR = 60 * 60\n")
    states = "".join(f"  {state}:\n    do: {{call: {name}}}\n    transitions: [{{target: {following}}}]\n"
                     for state, name, following in [("a", "dump", "b"), ("b", "first", "c"), ("c", "second", "d"),
                                                    ("d", "A", "e"), ("e", "right", "f"), ("f", "p", "g"),
                                                    ("g", "HOUR", "done")])
    tree = validate({"m.yaml": "stategraph: 1\nid: m\npython: m.py\ninitial: a\nstates:\n" + states
                               + "  done: {type: final}\n", "m.py": module})

    assert [p.path for p in errors(tree)] == ["states.c.do.call", "states.d.do.call", "states.g.do.call"]


def test_yaml_that_aliases_expand_past_the_bounds_is_a_problem_not_a_crash_or_a_hang():
    chain = "  l0: &l0 [1]\n" + "".join(f"  l{i}: &l{i} [*l{i - 1}]\n" for i in range(1, 1500))
    laughs = "  v0: &v0 [1, 1]\n" + "".join(f"  v{i}: &v{i} [*v{i - 1}, *v{i - 1}]\n" for i in range(1, 40))
    for context, message in ((chain, "YAML: nested 1502 levels deep (at most 100)"),
                             (laughs, "YAML: its aliases expand it to 4398046511115 values (at most 100000)")):
        text = f"stategraph: 1\nid: m\ninitial: a\ncontext:\n{context}states:\n  a: {{type: final}}\n"
        assert [(p.code, p.message) for p in validate({"m.yaml": text}).problems] == [("SG001", message)]
        with pytest.raises(EditError, match=message.replace("(", r"\(").replace(")", r"\)")):
            apply_op(text, {"op": "add_state", "name": "b"})

    deep = "do:\n  agent: w\n  task: t\n  args:\n    k: " + "[" * 300 + "]" * 300 + "\n"
    with pytest.raises(EditError, match="the state's YAML is nested too deeply to read"):
        apply_op(ANCHORED, {"op": "set_state", "name": "c", "yaml": deep})
    wide = "do:\n  agent: w\n  task: t\n  args:\n    k: " + "[" * 120 + "]" * 120 + "\n"
    with pytest.raises(EditError, match=r"the state's YAML: nested \d+ levels deep \(at most 100\)"):
        apply_op(ANCHORED, {"op": "set_state", "name": "c", "yaml": wide})


OWN_BESIDE_MERGE = """\
stategraph: 1
id: m
initial: a
states:
  a: &base
    do: {agent: writer, task: hi}
    transitions:
      - target: b
  b:
    <<: *base
    transitions:
      - target: c
  c:
    type: final
"""


def test_a_merging_state_s_own_transitions_are_edited_and_inherited_ones_refused():
    added = parse_yaml(apply_op(OWN_BESIDE_MERGE, {"op": "add_transition", "source": "b", "target": "a"}))
    assert [to_plain(added["states"][name]["transitions"]) for name in "ab"] == [
        [{"target": "b"}], [{"target": "c"}, {"target": "a"}]]
    updated = parse_yaml(apply_op(OWN_BESIDE_MERGE, {"op": "update_transition", "source": "b", "index": 0,
                                                     "fields": {"description": "d"}}))
    assert [to_plain(updated["states"][name]["transitions"]) for name in "ab"] == [
        [{"target": "b"}], [{"target": "c", "description": "d"}]]

    with pytest.raises(EditError, match="shared with another place"):  # b of MERGED inherits a's transitions
        apply_op(MERGED, {"op": "add_transition", "source": "a", "target": "c"})
    heir = OWN_BESIDE_MERGE.replace("  c:\n    type: final\n", "  c:\n    type: final\n  d:\n    <<: *base\n")
    heir = heir.replace("    transitions:\n      - target: b\n  b:", "  b:")
    with pytest.raises(EditError, match="shared with another place"):  # d would inherit a's first transition
        apply_op(heir, {"op": "add_transition", "source": "a", "target": "c"})
    shared_region = ("stategraph: 1\nid: m\ninitial: p\nstates:\n  p:\n    initial: x\n    states: &r\n      x:\n"
                     "        transitions:\n          - target: y\n      y: {type: final}\n  q:\n    initial: x\n"
                     "    states: *r\n")
    with pytest.raises(EditError, match="shared with another place"):  # x lies in a region q aliases
        apply_op(shared_region, {"op": "update_transition", "source": "x", "index": 0, "fields": {"description": "d"}})


# ------------------------------------------------------------------ second review of the model round

def test_a_long_text_its_aliases_copy_is_bounded_by_its_characters():
    """Ten thousand characters, aliased ten times on four levels: few values, a hundred million characters."""
    lines = ["  s: &l1 |", "    " + "x" * 10_000]
    lines += [f"  l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 10)}]" for i in range(2, 6)]
    text = "stategraph: 1\nid: m\ninitial: a\ncontext:\n" + "\n".join(lines) + "\nstates:\n  a: {type: final}\n"

    tree = validate({"m.yaml": text})

    assert [p.message for p in found(tree, "SG001") if "characters of text" in p.message], [p.message for p in tree.problems]


SELF_MERGE = "stategraph: 1\nid: m\ninitial: a\ncontext:\n  a: &a {x: 1, <<: *a}\nstates:\n  a: {type: final}\n"


def test_a_merge_key_naming_its_own_anchor_is_a_problem_not_a_crash():
    tree = validate({"m.yaml": SELF_MERGE})

    assert [p.message for p in found(tree, "SG001")], [p.message for p in tree.problems]
    with pytest.raises(EditError, match="does not read as YAML"):
        apply_op(SELF_MERGE, {"op": "add_state", "name": "b"})
    with pytest.raises(EditError, match="does not read as YAML"):
        apply_op(ANCHORED, {"op": "set_state", "name": "c", "yaml": "type: final\ndo: &d {<<: *d}\n"})


def test_a_state_using_an_anchor_of_another_is_applied_in_its_place_alias_kept():
    shown = "do: *w\ntransitions:\n  - target: c\n"

    unchanged = apply_op(ANCHORED, {"op": "set_state", "name": "b", "yaml": shown})
    changed = apply_op(ANCHORED, {"op": "set_state", "name": "b", "yaml": "max_visits: 2\n" + shown})

    assert unchanged == ANCHORED
    assert "    max_visits: 2\n    do: *w\n" in changed and "do: &w" in changed
    assert to_plain(parse_yaml(changed))["states"]["b"]["do"] == {"agent": "writer", "task": "hi"}


@pytest.mark.parametrize("module, use", [
    ("from os.path import *\n", "call: basename"),
    ("from os.path import *\n", "effect"),
    ("for handler in (print,):\n    pass\n", "call: handler"),
    ("with open(__file__) as handle:\n    pass\n", "call: handle"),
], ids=["star_call", "star_name", "for_name", "with_name"])
def test_what_the_run_may_find_in_the_module_the_scan_does_not_refuse(module, use):
    lines = ["stategraph: 1", "id: m", "python: m.py", "context: {base: null}", "initial: a", "states:", "  a:"]
    if use.startswith("call"):
        lines.append(f"    do: {{call: {use.split(': ', 1)[1]}, args: {{p: a/b}}}}")
    lines += ["    transitions:", "      - target: b"]
    if use == "effect":
        lines.append("        effect: ctx.base = basename('a/b')")
    lines.append("  b: {type: final}")

    tree = validate({"m.yaml": "\n".join(lines) + "\n", "m.py": module})

    assert errors(tree) == [], [p.message for p in tree.problems]


MERGING_ITEM = """\
stategraph: 1
id: m
initial: a
states:
  a:
    transitions:
      - &t {guard: ctx.go, effect: ctx.n = 1, target: b}
  b:
    transitions:
      - {<<: *t, target: b}
"""


def test_a_transition_that_merges_another_edits_its_own_keys_only():
    out = apply_op(MERGING_ITEM, {"op": "update_transition", "source": "b", "index": 0, "fields": {"target": "a"}})

    doc = to_plain(parse_yaml(out))
    assert doc["states"]["b"]["transitions"][0]["target"] == "a"
    assert doc["states"]["a"]["transitions"][0] == {"guard": "ctx.go", "effect": "ctx.n = 1", "target": "b"}
    with pytest.raises(EditError, match="shared with another place"):  # the guard is a's
        apply_op(MERGING_ITEM, {"op": "update_transition", "source": "b", "index": 0, "fields": {"guard": "True"}})
    with pytest.raises(EditError, match="shared with another place"):  # b's item merges it: b would change too
        apply_op(MERGING_ITEM, {"op": "update_transition", "source": "a", "index": 0, "fields": {"guard": "True"}})


# ------------------------------------------------------------------ third review: set_state stays in its state

REBOUND = """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: &act {agent: x, task: t}
    transitions:
      - target: b
  b:
    description: "one"
    transitions:
      - target: c
  c:
    do: *act
    description: "the end"
"""


@pytest.mark.parametrize("text, name, typed, message", [
    (ANCHORED, "a", "do: &w\n  agent: editor\n  task: hi\ntransitions:\n  - target: b\n", "shared with another place"),
    (REBOUND, "b", "do: &act {agent: y, task: t}\ntransitions:\n  - target: c\n", "names an anchor the file has already"),
    # the open quote closes at the end of c's comment: c becomes part of b's description
    (REBOUND.replace('    description: "the end"\n', '    type: final  # the end"\n'), "b",
     'description: "one\ntransitions:\n  - target: c\n', "changes more than this state"),
], ids=["value_under_its_anchor", "anchor_name_reused", "quote_runs_past"])
def test_set_state_refuses_text_that_changes_another_state(text, name, typed, message):
    with pytest.raises(EditError, match=message):
        apply_op(text, {"op": "set_state", "name": name, "yaml": typed})


def test_set_state_writes_comments_alone_as_an_empty_state_and_refuses_a_null():
    out = apply_op(REBOUND, {"op": "set_state", "name": "b", "yaml": "# to do\n"})

    assert to_plain(parse_yaml(out))["states"]["b"] == {} and "  b: {}\n    # to do\n" in out
    assert [p for p in load({"m.yaml": out}).problems if p.code == "SG001"] == []
    with pytest.raises(EditError, match="must be a mapping"):
        apply_op(REBOUND, {"op": "set_state", "name": "b", "yaml": "null"})


def test_a_quoted_state_key_applied_as_shown_leaves_the_file_byte_exact():
    text = REBOUND.replace("  b:\n", '  "b":\n').replace('description: "one"', 'description: one  # note')

    assert apply_op(text, {"op": "set_state", "name": "b",
                           "yaml": "description: one  # note\ntransitions:\n  - target: c"}) == text


MERGED_LIST = """\
stategraph: 1
id: m
initial: a
states:
  a:
    transitions:
      - &base {guard: ctx.go, target: b}
      - {<<: *base, target: a}
  b: {type: final}
"""


def test_a_merging_item_does_not_move_above_its_anchor_and_does_not_drop_what_it_merges():
    with pytest.raises(EditError, match="shared with another place"):
        apply_op(MERGED_LIST, {"op": "move_transition", "source": "a", "index": 1, "to": 0})
    with pytest.raises(EditError, match="takes the one it merges"):
        apply_op(MERGED_LIST, {"op": "update_transition", "source": "a", "index": 1, "fields": {"target": None}})


def test_a_counter_increased_in_the_module_is_data_not_a_function():
    machine = ("stategraph: 1\nid: m\npython: m.py\ninitial: a\nstates:\n  a:\n    do: {call: COUNT}\n"
               "    transitions:\n      - target: b\n  b: {type: final}\n")

    tree = validate({"m.yaml": machine, "m.py": "COUNT = 0\nCOUNT += 1\n"})

    assert [p for p in found(tree, "SG004") if "COUNT" in p.message], [p.message for p in tree.problems]
