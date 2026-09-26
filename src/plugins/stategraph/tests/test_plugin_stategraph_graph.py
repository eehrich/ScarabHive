"""graph_view: the canvas's view of a machine, from sound and from broken files."""

from __future__ import annotations

from plugins.stategraph.model.graph import graph_view
from plugins.stategraph.model.loader import SnapshotSources, load_tree

MACHINE = """\
stategraph: 1
id: review_loop
title: Review loop
description: Write, review, decide.
imports:
  critique: ./critique.yaml
params:
  premise: {type: string, required: true}
  max_rounds: {type: integer, default: 3}
context:
  draft: null
  round: 0
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
        do: {machine: critique, params: {text: "{{ ctx.draft }}"}}
        transitions:
          - target: verdict
      verdict:
        type: final
    transitions:
      - target: route
  route:
    type: choice
    transitions:
      - target: done
        guard: ctx.round >= params.max_rounds
      - target: write
        guard: else
  done:
    type: final
    output: {draft: "{{ ctx.draft }}"}
  failed:
    type: final
    status: failed
"""

CRITIQUE = """\
stategraph: 1
id: critique
params:
  text: {type: string}
initial: done
states:
  done: {type: final, output: {notes: "{{ params.text }}"}}
"""


def view(text: str, extra: dict[str, str] | None = None) -> dict:
    files = {"m/review_loop.yaml": text, "m/critique.yaml": CRITIQUE, **(extra or {})}
    return graph_view(load_tree("m/review_loop.yaml", SnapshotSources(files)))


def by_name(graph: dict) -> dict[str, dict]:
    return {state["name"]: state for state in graph["states"]}


def test_states_come_parent_first_with_their_hierarchy_and_lines():
    graph = view(MACHINE)

    assert [s["name"] for s in graph["states"]] == ["write", "review", "read", "verdict", "route", "done", "failed"]
    states = by_name(graph)
    assert states["review"]["composite"] is True and states["review"]["initial"] == "read"
    assert states["read"]["parent"] == "review" and states["verdict"]["parent"] == "review"
    assert states["write"]["parent"] is None and states["write"]["initial"] is None
    assert states["read"]["path"] == "states.review.states.read"
    assert states["write"]["line"] == MACHINE.splitlines().index("  write:") + 1
    assert states["read"]["line"] == MACHINE.splitlines().index("      read:") + 1
    assert states["write"]["max_visits"] == 5
    assert (states["route"]["type"], states["done"]["type"]) == ("choice", "final")
    assert states["failed"]["status"] == "failed"


def test_an_activity_gives_kind_label_and_icon():
    states = by_name(view(MACHINE))

    assert (states["write"]["kind"], states["write"]["label"], states["write"]["icon"]) == ("agent", "scene_writer", "brain")
    assert (states["read"]["kind"], states["read"]["label"]) == ("machine", "critique")
    assert states["route"]["kind"] is None and states["route"]["icon"] is None


def test_transitions_keep_their_list_index_and_default_trigger():
    graph = view(MACHINE)
    transitions = {t["id"]: t for t in graph["transitions"]}

    assert list(transitions) == ["write#0", "write#1", "review#0", "read#0", "route#0", "route#1"]
    assert transitions["write#0"]["trigger"] == "done" and transitions["write#0"]["effect"] == "ctx.draft = out"
    assert transitions["write#1"]["trigger"] == "error" and transitions["write#1"]["target"] == "failed"
    assert transitions["route#1"]["guard"] == "else" and transitions["route#1"]["index"] == 1
    assert transitions["read#0"]["path"] == "states.review.states.read.transitions[0]"
    assert transitions["write#1"]["line"] == MACHINE.splitlines().index("      - trigger: error") + 1


def test_machine_level_fields_are_there_for_the_run_form():
    graph = view(MACHINE)

    assert (graph["id"], graph["title"], graph["initial"]) == ("review_loop", "Review loop", "write")
    assert graph["imports"] == {"critique": "./critique.yaml"}
    assert graph["params"]["max_rounds"] == {"type": "integer", "default": 3}
    assert graph["context"] == {"draft": None, "round": 0}


def test_a_schema_error_still_shows_the_graph():
    broken = MACHINE.replace("    max_visits: 5\n", "    max_visits: 5\n    colour: red\n")
    tree = load_tree("m/review_loop.yaml", SnapshotSources({"m/review_loop.yaml": broken, "m/critique.yaml": CRITIQUE}))

    graph = graph_view(tree)

    assert not tree.ok, "fixture: the unknown key must be a problem"
    assert len(graph["states"]) == 7 and len(graph["transitions"]) == 6


def test_an_invalid_activity_still_names_its_kind():
    broken = MACHINE.replace('      task: "{{ params.premise }}"\n', "")

    write = by_name(view(broken))["write"]

    assert (write["kind"], write["label"], write["icon"]) == ("agent", "scene_writer", "brain")


def test_a_wait_state_is_one_without_activity_that_only_takes_events():
    text = MACHINE.replace("  done:\n    type: final", "  approve:\n    transitions:\n      - trigger: approved\n"
                                                         "        target: done\n  relay:\n    transitions:\n"
                                                         "      - trigger: approved\n        target: done\n"
                                                         "      - target: done\n  done:\n    type: final")

    states = by_name(view(text))

    assert states["approve"]["wait"] is True
    assert states["relay"]["wait"] is False, "a completion transition makes it complete, not wait"
    assert states["write"]["wait"] is False and states["route"]["wait"] is False


def test_a_transition_that_is_no_mapping_keeps_the_list_index_of_the_others():
    text = MACHINE.replace("      - target: review\n        effect: ctx.draft = out\n",
                           "      - just text\n      - target: review\n")

    transitions = {t["id"]: t for t in view(text)["transitions"]}

    assert "write#0" not in transitions
    assert transitions["write#1"]["target"] == "review" and transitions["write#2"]["trigger"] == "error"


def test_unparseable_yaml_gives_the_empty_graph_and_leaves_the_problem_to_the_caller():
    tree = load_tree("m/review_loop.yaml", SnapshotSources({"m/review_loop.yaml": "states: [unclosed\n"}))

    graph = graph_view(tree)

    assert graph["states"] == [] and graph["transitions"] == [] and graph["id"] is None
    assert any(p.code == "SG001" for p in tree.problems)


def test_a_file_that_is_no_mapping_gives_the_empty_graph():
    graph = graph_view(load_tree("m/list.yaml", SnapshotSources({"m/list.yaml": "- write\n- review\n"})))

    assert graph["states"] == [] and graph["id"] is None


def test_a_missing_root_file_gives_the_empty_graph():
    graph = graph_view(load_tree("m/nothing.yaml", SnapshotSources({})))

    assert graph["states"] == [] and graph["initial"] is None
