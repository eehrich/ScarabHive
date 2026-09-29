"""update_state and update_machine: the inspector's forms write single keys, the file's comments and layout kept.

The machine and the helpers are the ones of test_plugin_stategraph_yamledit (a comment on every kind of line).
"""

from __future__ import annotations

import pytest

from plugins.stategraph.model.graph import graph_view
from plugins.stategraph.model.loader import SnapshotSources, load_tree
from plugins.stategraph.model.yamledit import EditError, apply_op
from plugins.stategraph.tests.test_plugin_stategraph_yamledit import MACHINE, data, kept, loads_cleanly


def state(text: str, *path: str) -> dict:
    node = data(text)
    for name in path:
        node = node["states"][name]
    return node


# ---------------------------------------------------------------- update_state: the state's own keys

def test_state_keys_are_set_in_place_and_new_ones_go_before_the_transitions():
    out = apply_op(MACHINE, {"op": "update_state", "name": "write",
                             "fields": {"max_visits": 7, "description": "drafts the scene", "entry": "ctx.round += 1"}})

    body = state(out, "write")
    assert (body["max_visits"], body["description"], body["entry"]) == (7, "drafts the scene", "ctx.round += 1")
    assert list(body).index("description") < list(body).index("transitions")
    assert "max_visits: 7    # loop guard" in out, "the value changed, its comment stays"
    assert kept(out) == [] and loads_cleanly(out)


def test_null_or_empty_removes_a_key_and_several_lines_become_a_block():
    out = apply_op(MACHINE, {"op": "update_state", "name": "write",
                             "fields": {"max_visits": None, "exit": "ctx.a = 1\nctx.b = 2\n"}})

    body = state(out, "write")
    assert "max_visits" not in body and body["exit"] == "ctx.a = 1\nctx.b = 2\n"
    assert "exit: |" in out
    assert kept(out, "# loop guard") == []
    assert state(apply_op(out, {"op": "update_state", "name": "write", "fields": {"exit": ""}}), "write").get("exit") is None


def test_the_type_is_set_and_a_simple_state_loses_the_key():
    out = apply_op(MACHINE, {"op": "update_state", "name": "failed", "fields": {"type": "state"}})
    assert "type" not in state(out, "failed")
    out = apply_op(out, {"op": "update_state", "name": "failed", "fields": {"type": "final"}})
    assert state(out, "failed")["type"] == "final"
    with pytest.raises(EditError, match="type"):
        apply_op(MACHINE, {"op": "update_state", "name": "failed", "fields": {"type": "fork"}})


def test_a_final_s_output_and_a_finally_activity_come_as_yaml_text():
    out = apply_op(MACHINE, {"op": "update_state", "name": "done", "fields": {
        "output": {"$yaml": "draft: '{{ ctx.draft }}'  # what the caller gets\nrounds: 2"},
        "finally": {"$yaml": "tool: stategraph_json_manage_json\nargs: {operation: list}"}}})

    body = state(out, "done")
    assert body["output"] == {"draft": "{{ ctx.draft }}", "rounds": 2}
    assert body["finally"] == {"tool": "stategraph_json_manage_json", "args": {"operation": "list"}}
    assert "# what the caller gets" in out, "a comment typed into the YAML field is kept"
    assert kept(out) == [] and loads_cleanly(out)


# ---------------------------------------------------------------- update_state: the activity

def test_activity_keys_are_set_one_by_one_and_the_others_keep_their_text():
    out = apply_op(MACHINE, {"op": "update_state", "name": "write",
                             "do": {"timeout": "5m", "vars": {"$yaml": "tone: dry"}}})

    assert state(out, "write")["do"] == {"agent": "scene_writer", "task": "{{ params.premise }}", "timeout": "5m",
                                         "vars": {"tone": "dry"}}
    assert '      task: "{{ params.premise }}"' in out, "an untouched key keeps its quoting"
    assert kept(out) == []


def test_another_kind_replaces_the_old_kind_key_and_stands_first():
    timed = apply_op(MACHINE, {"op": "update_state", "name": "write", "do": {"timeout": "5m"}})
    out = apply_op(timed, {"op": "update_state", "name": "write", "do": {
        "agent": None, "task": None, "decide": "noul", "input": "{{ ctx.draft }}", "question": "Is it done?"}})

    do = state(out, "write")["do"]
    assert do == {"decide": "noul", "timeout": "5m", "input": "{{ ctx.draft }}", "question": "Is it done?"}
    assert list(do)[0] == "decide", "the kind goes before the timeout the change keeps"
    assert loads_cleanly(out)


def test_a_state_without_an_activity_gets_one_and_can_lose_it_again():
    out = apply_op(MACHINE, {"op": "update_state", "name": "verdict",
                             "fields": {"type": "state"}, "do": {"agent": "critic", "task": "judge"}})
    assert state(out, "review", "verdict") == {"do": {"agent": "critic", "task": "judge"}}

    gone = apply_op(out, {"op": "update_state", "name": "verdict", "fields": {"do": None}})
    assert "do" not in (state(gone, "review", "verdict") or {})


def test_a_flow_style_activity_is_edited_too():
    out = apply_op(MACHINE, {"op": "update_state", "name": "read", "do": {"params": {"$yaml": "text: x"}}})

    assert state(out, "review", "read")["do"] == {"machine": "critique", "params": {"text": "x"}}
    assert loads_cleanly(out)


@pytest.mark.parametrize("op, message", [
    ({"fields": {"color": "red"}}, "unknown state keys color"),
    ({"fields": {}}, "fields"),
    ({"do": "agent: x"}, "do"),
    ({"fields": {"output": {"$yaml": "a: [1, 2"}}}, "does not parse"),
    ({"fields": {"output": {"$yaml": "a: " + "[" * 150 + "]" * 150}}}, "deep"),
    ({"fields": {"transitions": None}}, "unknown state keys transitions"),
], ids=["unknown", "nothing", "do_not_a_mapping", "bad_yaml", "too_deep", "not_through_this_op"])
def test_what_update_state_refuses(op, message):
    with pytest.raises(EditError, match=message):
        apply_op(MACHINE, {"op": "update_state", "name": "write", **op})


SHARED = """\
stategraph: 1
id: shared
initial: a
states:
  a: &base
    description: one
    do: &job {agent: w, task: t}
    transitions: [{target: b}]
  b:
    <<: *base
    transitions: [{target: c}]
  c:
    do: *job
    type: state
  d:
    type: final
"""


@pytest.mark.parametrize("name, op", [
    ("a", {"fields": {"description": "two"}}),   # b merges a: b would change too
    ("b", {"fields": {"description": "two"}}),   # b's description comes through its merge
    ("c", {"do": {"task": "u"}}),               # c's activity is a's too
], ids=["merged_into_another", "inherited", "aliased_activity"])
def test_a_key_another_state_sees_is_refused(name, op):
    with pytest.raises(EditError, match="shared"):
        apply_op(SHARED, {"op": "update_state", "name": name, **op})


ALIASED_OUTPUT = """\
stategraph: 1
id: m
initial: a
states:
  a:
    transitions: [{target: b}]
  b:
    type: final
    output: &o {x: 1}
  c:
    type: final
    output: *o
"""


def test_a_value_other_places_share_is_refused_and_shown_locked():
    with pytest.raises(EditError, match="shared"):
        apply_op(ALIASED_OUTPUT, {"op": "update_state", "name": "b", "fields": {"output": {"$yaml": "x: 2"}}})

    states = {s["name"]: s for s in graph_view(load_tree("m.yaml", SnapshotSources({"m.yaml": ALIASED_OUTPUT})))["states"]}
    assert states["b"]["locked"] == ["output"] and "output" not in states["b"]["yaml"]


def test_removing_every_key_of_the_activity_removes_it():
    out = apply_op(MACHINE, {"op": "update_state", "name": "write", "do": {"agent": None, "task": None}})

    assert "do" not in state(out, "write") and loads_cleanly(out)


def graph_of(text: str) -> dict:
    return graph_view(load_tree("m.yaml", SnapshotSources({"m.yaml": text})))


def test_the_yaml_text_keeps_the_quotes_it_is_written_with():
    text = MACHINE.replace("do: {machine: critique}", 'do: {machine: critique, params: {text: "{{ ctx.draft }}", n: 2}}')

    read = {s["name"]: s for s in graph_of(text)["states"]}["read"]

    assert read["yaml"]["do.params"] == '{text: "{{ ctx.draft }}", n: 2}\n'


def test_what_another_state_shares_is_locked_in_the_forms():
    states = {s["name"]: s for s in graph_of(SHARED)["states"]}

    assert {"description", "do"} <= set(states["a"]["locked"]), "b merges a: a's description and activity are b's too"
    assert {"description", "do"} <= set(states["b"]["locked"]), "b's description and activity come through its merge"
    assert "transitions" not in states["a"]["locked"] + states["b"]["locked"], "b's transitions are its own"
    assert "do" in states["c"]["locked"], "c's activity is an alias of a's"
    assert states["d"]["locked"] == []


def test_a_folded_text_is_folded_again_at_its_width():
    text = MACHINE.replace("id: review\n", "id: review\ndescription: >-\n  One agent activity and a final output.\n"
                                           "  Mock the state to run it offline.\n")
    long = "One agent activity and a final output. Live, it asks the example agent; mock the state to run it offline."

    out = apply_op(text, {"op": "update_machine", "fields": {"description": long}})

    block = out.split("description: >-\n", 1)[1].split("initial:", 1)[0].splitlines()
    assert len(block) >= 3 and max(len(line.strip()) for line in block) <= len("One agent activity and a final output.")
    assert data(out)["description"] == long


def test_a_key_of_its_own_beside_a_merge_is_the_state_s_business():
    out = apply_op(SHARED, {"op": "update_state", "name": "b", "fields": {"max_visits": 3}})

    assert state(out, "b")["max_visits"] == 3 and "max_visits" not in state(out, "a")


# ---------------------------------------------------------------- update_machine

def test_machine_keys_are_set_before_the_states_and_objects_come_as_yaml():
    out = apply_op(MACHINE, {"op": "update_machine", "fields": {
        "title": "Scene review", "group": "Writer/demo",
        "params": {"$yaml": "premise: {type: string, required: true}  # the scene"},
        "limits": {"$yaml": "max_steps: 200"}}})

    doc = data(out)
    assert (doc["title"], doc["group"], doc["limits"]) == ("Scene review", "Writer/demo", {"max_steps": 200})
    assert doc["params"] == {"premise": {"type": "string", "required": True}}
    assert list(doc).index("params") < list(doc).index("states")
    assert kept(out) == [] and loads_cleanly(out) and "# the scene" in out


@pytest.mark.parametrize("fields, message", [
    ({"states": None}, "unknown machine keys states"),
    ({"id": "other"}, "unknown machine keys id"),
    ({}, "fields"),
], ids=["states", "id", "nothing"])
def test_what_update_machine_refuses(fields, message):
    with pytest.raises(EditError, match=message):
        apply_op(MACHINE, {"op": "update_machine", "fields": fields})


# ---------------------------------------------------------------- what the inspector reads

def test_the_graph_carries_what_the_forms_show():
    text = apply_op(MACHINE, {"op": "update_state", "name": "done", "fields": {"output": {"$yaml": "draft: x"}}})
    text = apply_op(text, {"op": "update_machine", "fields": {"group": "Writer", "limits": {"$yaml": "max_steps: 9"}}})
    tree = load_tree("review.yaml", SnapshotSources({"review.yaml": text}))
    graph = graph_view(tree)
    states = {s["name"]: s for s in graph["states"]}

    assert states["write"]["do"] == {"agent": "scene_writer", "task": "{{ params.premise }}"}
    assert states["read"]["do"] == {"machine": "critique"}
    assert states["done"]["output"] == {"draft": "x"} and states["done"]["yaml"]["output"].strip() == "draft: x"
    assert graph["group"] == "Writer" and graph["yaml"]["limits"].strip() == "max_steps: 9"
