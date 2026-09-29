"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 1: model and editor.

The loader, validator, graph view and comment-keeping edits run on real texts; runs go through the real RunManager
with a FakeBackend.
"""

from __future__ import annotations

import pytest

from plugins.stategraph.model import graph as graph_module
from plugins.stategraph.model.loader import SnapshotSources, load_tree
from plugins.stategraph.model.yamledit import EditError, apply_op
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, found, validate
from plugins.stategraph.tests.test_plugin_stategraph_validate import machine

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

# ------------------------------------------------------------------ M6: a document the loader dropped is not expanded


def test_the_graph_of_an_alias_bomb_is_empty_not_expanded():
    lines = ["stategraph: 1", "id: m", "initial: a", "context:", "  a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"  a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]" for i in range(1, 6)]  # 10^6 values
    lines += ["states:", "  a:", "    type: final"]
    tree = load_tree("m.yaml", SnapshotSources({"m.yaml": "\n".join(lines) + "\n"}))
    assert tree.files["m.yaml"].doc is None, "fixture: the loader drops the document"

    assert graph_module.graph_view(tree)["states"] == []


# ------------------------------------------------------------------ M7: a state that completes needs somewhere to go

@pytest.mark.parametrize("states,where", [
    ("""\
     a:
       do: {agent: w, task: t}
       transitions: [{trigger: approve, target: done}, {trigger: error, target: done}]
     done: {type: final}
     """, "states.a.transitions"),
    ("""\
     a:
       initial: work
       states:
         work: {do: {agent: w, task: t}}
       transitions: [{target: done}]
     done: {type: final}
     """, "states.a.states.work"),
    ("""\
     a:
       initial: inner
       states:
         inner: {type: final}
       transitions: [{trigger: approve, target: done}]
     done: {type: final}
     """, "states.a.transitions"),
])
def test_a_state_that_completes_without_a_completion_transition_is_warned_about(states, where):
    tree = validate(machine(states, head="events: {approve: {}}\n"))

    assert [(p.path, p.level, "no completion transition" in p.message) for p in found(tree, "SG109")] == [
        (where, "warning", True)], (
        [(p.code, p.path, p.message) for p in tree.problems])


def test_a_wait_state_and_a_composite_without_a_final_need_no_completion_transition():
    tree = validate(machine("""\
        a:
          transitions: [{trigger: approve, target: b}]
        b:
          initial: loop
          states:
            loop:
              transitions: [{trigger: approve, target: loop}]
          transitions: [{trigger: stop, target: done}]
        done: {type: final}
        """, head="events: {approve: {}, stop: {}}\n"))

    assert found(tree, "SG003") + found(tree, "SG109") == [], [(p.code, p.path, p.message) for p in tree.problems]


# ------------------------------------------------------------------ M8: reserved names are no state names

SMALL = """\
stategraph: 1
id: m
initial: a
states:
  a:
    transitions: [{target: b}]
  b: {type: final}
"""


@pytest.mark.parametrize("op", [
    {"op": "add_state", "name": "finally"},
    {"op": "rename_state", "old": "b", "new": "resources"},
])
def test_a_reserved_name_is_refused_as_a_state_name(op):
    with pytest.raises(EditError) as refused:
        apply_op(SMALL, op)

    assert "is reserved" in refused.value.message


# ------------------------------------------------------------------ M9: params hold JSON data

@pytest.mark.parametrize("param,key", [
    ("{default: 2024-01-01}", "default"),  # type any: no type check catches it
    ("{type: string, enum: [2024-01-01, later]}", "enum"),
])
def test_a_param_value_that_is_no_json_data_is_an_error(param, key):
    tree = validate(machine("""\
        a: {type: final}
        """, head=f"params: {{day: {param}}}\n"))

    assert [(p.path, "not JSON data" in p.message) for p in found(tree, "SG001")] == [(f"params.day.{key}", True)]


# ------------------------------------------------------------------ M10: file keys use / on every platform

def test_the_files_of_a_machine_with_a_subfolder_are_keyed_with_slashes(tmp_path):
    from plugins.stategraph.store import MachineStore

    (tmp_path / "m.yaml").write_text(SMALL, encoding="utf-8")
    store = MachineStore([str(tmp_path)], [str(tmp_path)])

    assert store.relative("m", str(tmp_path / "sub" / "x.yaml")) == "sub/x.yaml"


# ------------------------------------------------------------------ D3: saving a shipped machine says what to do

def test_saving_a_shipped_machine_says_to_save_a_copy(tmp_path):
    from plugins.stategraph.store import MachineStore

    shipped, own = tmp_path / "shipped", tmp_path / "own"
    shipped.mkdir()
    own.mkdir()
    (shipped / "m.yaml").write_text(SMALL, encoding="utf-8")
    store = MachineStore([str(own), str(shipped)], [str(own)])

    with pytest.raises(PermissionError) as refused:
        store.write_files("m", {"m.yaml": SMALL + "# changed\n"})  # refused before any version check

    assert "save a copy under a new id" in str(refused.value)
