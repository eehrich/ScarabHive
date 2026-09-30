"""apply_op: the panel's graph edits on machine files, with the file's comments and layout kept."""

from __future__ import annotations

import pytest

from plugins.stategraph.model.loader import SnapshotSources, load_tree, parse_yaml, to_plain
from plugins.stategraph.model.yamledit import EditError, apply_op

MACHINE = """\
# review.yaml -- the header stays
stategraph: 1
id: review
initial: write   # start here

states:
  # the writer
  write:
    max_visits: 5    # loop guard
    do:
      agent: scene_writer
      task: "{{ params.premise }}"
    transitions:
      # the happy path
      - target: review
        effect: ctx.draft = out
      - trigger: error
        target: failed

  # the review phase
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
      - target: done   # when ready

  done:
    type: final
  failed:
    type: final
    status: failed   # a failed run
# the end
"""

COMMENTS = ["# review.yaml -- the header stays", "# start here", "# the writer", "# loop guard", "# the happy path",
            "# the review phase", "# when ready", "# a failed run", "# the end"]


def data(text: str) -> dict:
    return to_plain(parse_yaml(text))


def loads_cleanly(text: str) -> bool:
    import re

    machine_id = re.search(r"^id: (\w+)", text, re.M).group(1)  # a machine lives in <id>.yaml
    tree = load_tree(f"{machine_id}.yaml", SnapshotSources({f"{machine_id}.yaml": text}))
    return not [p for p in tree.problems if p.code == "SG001"]


def kept(text: str, *comments: str) -> list[str]:
    """The comments of MACHINE that are missing from text (expected: none), except those named."""
    return [c for c in COMMENTS if c not in comments and c not in text]


# ---------------------------------------------------------------- states

def test_add_state_appends_to_the_top_level_and_keeps_every_comment():
    out = apply_op(MACHINE, {"op": "add_state", "name": "judge", "do": {"agent": "jev", "task": "judge it"}})

    assert data(out)["states"]["judge"] == {"do": {"agent": "jev", "task": "judge it"}}
    assert list(data(out)["states"])[-1] == "judge"
    assert kept(out) == [] and loads_cleanly(out)
    assert out.startswith(MACHINE.split("  done:")[0]), "lines before the edit must stay byte-exact"


def test_add_state_into_a_simple_state_makes_it_a_composite():
    text = MACHINE.replace("  done:\n    type: final\n", "  done:\n    type: final\n  wait:\n    transitions:\n"
                                                        "      - trigger: go\n        target: done\n")

    out = apply_op(text, {"op": "add_state", "name": "inner", "parent": "wait", "type": "final"})

    wait = data(out)["states"]["wait"]
    assert wait["initial"] == "inner" and wait["states"] == {"inner": {"type": "final"}}
    assert list(wait) == ["initial", "states", "transitions"], "the new keys go before the transitions"


def test_add_state_into_an_emptied_region_or_an_empty_state():
    emptied = "stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    initial: gone\n    states: {}\n" \
              "    transitions:\n      - target: b\n  b:\n    type: final\n"
    bare = "stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n  b:\n    type: final\n"

    refilled = data(apply_op(emptied, {"op": "add_state", "name": "c", "parent": "a"}))["states"]["a"]
    grown = data(apply_op(bare, {"op": "add_state", "name": "c", "parent": "a"}))["states"]["a"]

    assert list(refilled) == ["initial", "states", "transitions"] and refilled["initial"] == "c"
    assert grown == {"initial": "c", "states": {"c": {}}}


def test_a_batch_applies_its_edits_one_after_another_as_one_edit():
    out = apply_op(MACHINE, {"op": "batch", "ops": [{"op": "add_state", "name": "group"},
                                                    {"op": "add_state", "name": "start", "parent": "group"},
                                                    {"op": "remove_state", "name": "failed"}]})

    states = data(out)["states"]
    assert states["group"] == {"initial": "start", "states": {"start": {}}}, "the second edit sees the first"
    assert "failed" not in states and not any(t.get("target") == "failed" for t in states["write"]["transitions"])
    assert kept(out, "# a failed run") == [] and loads_cleanly(out)  # the removed state's comment goes with it


@pytest.mark.parametrize("ops,says", [
    ([{"op": "add_state", "name": "group"}, {"op": "remove_state", "name": "nowhere"}], "edit 2 of 2: "),
    ([{"op": "batch", "ops": [{"op": "add_state", "name": "x"}]}], "a batch holds no batch"),
    ([], "ops: a list of edits"),
    ([{"op": "add_state", "name": f"s{n}"} for n in range(101)], "at most 100 edits"),
])
def test_a_batch_that_cannot_be_applied_whole_changes_nothing(ops, says):
    with pytest.raises(EditError) as refused:
        apply_op(MACHINE, {"op": "batch", "ops": ops})

    assert says in refused.value.message, refused.value.message


def test_group_states_puts_them_into_a_new_composite_at_the_first_ones_place():
    out = apply_op(MACHINE, {"op": "group_states", "names": ["review", "write"], "name": "draft"})

    doc = data(out)
    assert list(doc["states"]) == ["draft", "done", "failed"]
    assert doc["states"]["draft"]["initial"] == "write" and list(doc["states"]["draft"]["states"]) == ["write", "review"]
    assert doc["initial"] == "draft", "the machine's initial named one of them: it names the composite"
    assert "  draft:\n    initial: write\n    states:\n      # the writer\n      write:\n" in out
    assert "            target: failed\n\n      # the review phase\n      review:\n" in out, "the blank line between them"
    assert out.endswith(MACHINE[MACHINE.index("\n  done:"):]) and kept(out) == []
    tree = load_tree("review.yaml", SnapshotSources({"review.yaml": out}))
    assert not [p for p in tree.problems if p.code in ("SG001", "SG002")], "the transitions reach their states"


def test_group_states_starts_the_composite_where_the_machine_started():
    text = MACHINE.replace("initial: write   # start here", "initial: review   # start here")

    doc = data(apply_op(text, {"op": "group_states", "names": ["write", "review"], "name": "draft"}))

    assert doc["initial"] == "draft" and doc["states"]["draft"]["initial"] == "review"


def test_group_states_inside_a_composite_leaves_the_machines_initial():
    out = apply_op(MACHINE, {"op": "group_states", "names": ["verdict"], "name": "ends"})

    doc = data(out)
    assert doc["initial"] == "write" and doc["states"]["review"]["initial"] == "read"
    assert doc["states"]["review"]["states"]["ends"] == {"initial": "verdict", "states": {"verdict": {"type": "final"}}}
    assert kept(out) == [] and loads_cleanly(out)


def test_group_states_written_in_flow_style_is_rendered():
    text = "stategraph: 1\nid: m\ninitial: a\nstates: {a: {transitions: [{target: b}]}, b: {type: final}}\n"

    out = apply_op(text, {"op": "group_states", "names": ["a", "b"], "name": "all"})

    assert data(out)["initial"] == "all" and data(out)["states"] == {
        "all": {"initial": "a", "states": {"a": {"transitions": [{"target": "b"}]}, "b": {"type": "final"}}}}


@pytest.mark.parametrize("op,says", [
    ({"names": ["write", "read"], "name": "x"}, "side by side"),
    ({"names": ["write"], "name": "done"}, "exists already"),
    ({"names": ["write", "nowhere"], "name": "x"}, "no state 'nowhere'"),
    ({"names": ["write", "write"], "name": "x"}, "each once"),
    ({"names": [], "name": "x"}, "each once"),
    ({"names": ["write"], "name": "Bad"}, "must match"),
])
def test_group_states_that_cannot_be_grouped_changes_nothing(op, says):
    with pytest.raises(EditError) as refused:
        apply_op(MACHINE, {"op": "group_states", **op})

    assert says in refused.value.message, refused.value.message


def test_group_states_refuses_a_state_an_alias_shares():
    text = ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a: &base\n    transitions:\n      - target: b\n"
            "  b:\n    type: final\n  c: *base\n")

    with pytest.raises(EditError) as refused:
        apply_op(text, {"op": "group_states", "names": ["a"], "name": "x"})

    assert "shared" in refused.value.message


def test_group_states_refuses_an_initial_that_follows_into_a_merged_composite_under_its_own_name():
    text = ("stategraph: 1\nid: m\ninitial: a\nbase: &base\n  description: shared\nstates:\n  a:\n    <<: *base\n"
            "    initial: x\n    states:\n      x:\n        transitions:\n          - target: y\n      y:\n        type: final\n")

    with pytest.raises(EditError) as refused:
        apply_op(text, {"op": "group_states", "names": ["x"], "name": "g"})
    grouped = data(apply_op(text, {"op": "group_states", "names": ["y"], "name": "g"}))  # the initial stays: no change

    assert refused.value.message.startswith("group into 'g': ") and "shared" in refused.value.message
    assert grouped["states"]["a"]["initial"] == "x" and list(grouped["states"]["a"]["states"]) == ["x", "g"]


def test_add_state_into_a_composite_lands_in_its_region():
    out = apply_op(MACHINE, {"op": "add_state", "name": "second", "parent": "review"})

    assert list(data(out)["states"]["review"]["states"]) == ["read", "verdict", "second"]
    assert kept(out) == []


def test_remove_state_takes_its_lines_its_comment_and_the_transitions_into_it():
    out = apply_op(MACHINE, {"op": "remove_state", "name": "review"})

    states = data(out)["states"]
    assert "review" not in states
    assert states["write"]["transitions"] == [{"trigger": "error", "target": "failed"}]
    assert kept(out, "# the review phase", "# when ready", "# the happy path") == []
    assert "# the review phase" not in out and "\n\n\n" not in out
    assert loads_cleanly(out)


def test_remove_state_removes_the_transitions_into_its_nested_states_too():
    text = MACHINE.replace("        target: failed\n", "        target: failed\n      - trigger: skip\n"
                                                      "        target: verdict\n")

    out = apply_op(text, {"op": "remove_state", "name": "review"})

    assert [t.get("target") for t in data(out)["states"]["write"]["transitions"]] == ["failed"]


def test_removing_every_transition_of_a_state_drops_its_transitions_key():
    out = apply_op(MACHINE, {"op": "remove_state", "name": "verdict"})

    read = data(out)["states"]["review"]["states"]["read"]
    assert "transitions" not in read and loads_cleanly(out)


def test_the_last_state_of_a_composite_stays_and_says_what_to_do():
    """Removed, it would turn the composite into a simple state -- one that waits, where a composite was drawn."""
    text = apply_op(MACHINE, {"op": "remove_state", "name": "verdict"})

    with pytest.raises(EditError, match="'read' is the last state in 'review', and a composite keeps one"):
        apply_op(text, {"op": "remove_state", "name": "read"})
    with pytest.raises(EditError, match="last state in 'review'"):
        apply_op(text, {"op": "move_state", "name": "read", "into": None})


def test_move_state_puts_a_state_last_into_a_composite_and_keeps_the_file_as_it_was():
    out = apply_op(MACHINE, {"op": "move_state", "name": "done", "into": "review"})

    expected = (MACHINE.replace("      verdict:\n        type: final\n",
                                "      verdict:\n        type: final\n      done:\n        type: final\n")
                .replace("\n  done:\n    type: final\n  failed:", "\n  failed:"))
    assert out == expected, out
    assert loads_cleanly(out)


def test_move_state_out_of_a_composite_gives_its_region_the_first_state_left_as_initial():
    text = MACHINE.replace("    initial: read\n", "    initial: read   # first\n")

    out = apply_op(text, {"op": "move_state", "name": "read", "into": None})

    moved = data(out)
    assert moved["states"]["review"]["initial"] == "verdict" and "    initial: verdict   # first\n" in out
    assert list(moved["states"])[-1] == "read" and moved["states"]["read"]["transitions"] == [{"target": "verdict"}]
    assert all(comment in out for comment in COMMENTS) and loads_cleanly(out)
    three = ("stategraph: 1\nid: m\ninitial: c\nstates:\n  c:\n    initial: b\n    states:\n      a:\n        type: final\n"
             "      b:\n        type: final\n      d:\n        type: final\n  e:\n    type: final\n")
    assert data(apply_op(three, {"op": "move_state", "name": "b", "into": None}))["states"]["c"]["initial"] == "a"


def test_move_state_parts_the_state_by_a_blank_line_where_its_new_region_does():
    text = ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    transitions:\n      - target: b\n\n"
            "  b:\n    type: final\n\n  c:\n    initial: x\n    states:\n      x:\n        type: final\n"
            "      y:\n        type: final\n")

    out = apply_op(text, {"op": "move_state", "name": "y", "into": None})

    assert out == ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    transitions:\n      - target: b\n\n"
                   "  b:\n    type: final\n\n  c:\n    initial: x\n    states:\n      x:\n        type: final\n"
                   "\n  y:\n    type: final\n"), out


def test_move_state_into_a_simple_state_makes_it_a_composite_and_every_comment_stays_once():
    text = ("stategraph: 1\nid: m\ninitial: a\nstates:\n  # a waits\n  a:\n    description: waits  # here\n"
            "    # its way on\n    transitions:\n      - target: c\n\n  # b handles errors\n  b:\n    type: final\n\n"
            "  # c is the good end\n  c:\n    type: final\n")

    out = apply_op(text, {"op": "move_state", "name": "b", "into": "a"})

    assert out == ("stategraph: 1\nid: m\ninitial: a\nstates:\n  # a waits\n  a:\n    description: waits  # here\n"
                   "    initial: b\n    states:\n      # b handles errors\n      b:\n        type: final\n"
                   "    # its way on\n    transitions:\n      - target: c\n\n  # c is the good end\n  c:\n    type: final\n"), out


def test_move_state_leaves_no_second_blank_line_where_the_state_ended_the_entry_it_now_follows():
    text = ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    transitions:\n      - target: c\n\n  c:\n"
            "    initial: x\n    states:\n      x:\n        type: final\n\n      y:\n        type: final\n\n"
            "limits: {max_steps: 5}\n")

    out = apply_op(text, {"op": "move_state", "name": "y", "into": None})

    assert out == ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    transitions:\n      - target: c\n\n  c:\n"
                   "    initial: x\n    states:\n      x:\n        type: final\n\n  y:\n    type: final\n\n"
                   "limits: {max_steps: 5}\n"), out


@pytest.mark.parametrize("text, name, into", [
    # a scalar anchor would follow its alias: the planned text does not read
    ("stategraph: 1\nid: m\ninitial: a\nstates:\n  # the writer\n  a:\n    description: &d shared  # anchored\n"
     "    transitions:\n      - target: b\n  # the reviewer\n  b:\n    description: *d\n    transitions:\n"
     "      - target: c\n  c:\n    initial: x\n    states:\n      x:\n        type: final\n", "a", "c"),
    # a flow mapping as the region: no lines to plan
    ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    initial: x\n    states: {x: {type: final}}\n"
     "  # about b\n  b:\n    type: final\n", "b", "a"),
])
def test_move_state_refuses_a_layout_it_cannot_plan_rather_than_move_comments_wrongly(text, name, into):
    with pytest.raises(EditError, match="change it in the YAML tab"):
        apply_op(text, {"op": "move_state", "name": name, "into": into})


@pytest.mark.parametrize("name, into, refusal", [
    ("review", "review", "cannot go into itself or a state inside it"),
    ("review", "read", "cannot go into itself or a state inside it"),
    ("done", "write", "'write' cannot hold states"),
    ("done", "failed", "'failed' cannot hold states"),
    ("read", "review", "'read' is in 'review' already"),
    ("done", None, "'done' is in the top level already"),
    ("done", "nowhere", "no state 'nowhere'"),
])
def test_move_state_refuses_what_it_cannot_do(name, into, refusal):
    with pytest.raises(EditError, match=refusal):
        apply_op(MACHINE, {"op": "move_state", "name": name, "into": into})


def test_the_last_state_of_a_machine_cannot_be_removed():
    with pytest.raises(EditError, match="at least one state"):
        apply_op("stategraph: 1\nid: m\ninitial: a\nstates:\n  a: {type: final}\n", {"op": "remove_state", "name": "a"})


def test_rename_state_updates_every_target_and_initial_and_keeps_the_position():
    renamed = apply_op(MACHINE, {"op": "rename_state", "old": "write", "new": "draft"})
    nested = apply_op(MACHINE, {"op": "rename_state", "old": "read", "new": "study"})
    targeted = apply_op(MACHINE, {"op": "rename_state", "old": "verdict", "new": "outcome"})

    assert data(targeted)["states"]["review"]["states"]["read"]["transitions"] == [{"target": "outcome"}]
    assert list(data(targeted)["states"]["review"]["states"]) == ["read", "outcome"]
    assert list(data(renamed)["states"])[0] == "draft" and data(renamed)["initial"] == "draft"
    assert "# start here" in renamed and "# loop guard" in renamed
    assert data(nested)["states"]["review"]["initial"] == "study"
    assert list(data(nested)["states"]["review"]["states"]) == ["study", "verdict"]
    assert kept(renamed) == [] and kept(nested) == []


def test_rename_refuses_a_name_in_use_or_out_of_pattern():
    with pytest.raises(EditError, match="exists already"):
        apply_op(MACHINE, {"op": "rename_state", "old": "write", "new": "verdict"})
    with pytest.raises(EditError, match="must match"):
        apply_op(MACHINE, {"op": "rename_state", "old": "write", "new": "Write"})


def test_set_state_writes_the_fragment_as_typed_under_the_key():
    fragment = "type: final\n# ends the run well\noutput: {draft: '{{ ctx.draft }}'}\n"

    out = apply_op(MACHINE, {"op": "set_state", "name": "done", "yaml": fragment})

    assert data(out)["states"]["done"] == {"type": "final", "output": {"draft": "{{ ctx.draft }}"}}
    assert "    # ends the run well\n    output: {draft: '{{ ctx.draft }}'}\n  failed:" in out
    assert kept(out) == []


def test_set_state_replaces_the_whole_body_but_not_the_neighbours():
    out = apply_op(MACHINE, {"op": "set_state", "name": "write", "yaml": "do:\n  agent: other\n  task: hi\n"})

    assert data(out)["states"]["write"] == {"do": {"agent": "other", "task": "hi"}}
    assert kept(out, "# loop guard", "# the happy path") == []
    assert "# loop guard" not in out


BLOCK_AT_THE_END = """\
stategraph: 1
id: m
initial: a
states:
  a:
    description: one
    transitions:
      - target: done
        effect: |
          ctx.a = 1
          ctx.b = 2
  done:
    type: final
"""
A_AS_SHOWN = "description: one\ntransitions:\n  - target: done\n    effect: |\n      ctx.a = 1\n      ctx.b = 2"


def test_set_state_keeps_the_final_newline_of_a_block_that_ends_the_fragment():
    out = apply_op(BLOCK_AT_THE_END, {"op": "set_state", "name": "a", "yaml": A_AS_SHOWN.replace(": one", ": two")})

    assert data(out)["states"]["a"]["transitions"][0]["effect"] == "ctx.a = 1\nctx.b = 2\n"
    assert out == BLOCK_AT_THE_END.replace("description: one", "description: two"), "only the changed line differs"


def test_set_state_as_shown_leaves_the_file_byte_exact_inline_comment_included():
    inline = "stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    transitions:\n      - target: done\n" \
             "  done: {type: final}   # the end\n"

    for text, name, shown in ((BLOCK_AT_THE_END, "a", A_AS_SHOWN), (inline, "done", "{type: final}   # the end"),
                              (inline, "done", "{type: final}"), (MACHINE, "write", "\n  " + "\n  ".join(
                                  MACHINE.split("  write:\n")[1].split("\n  # the review phase")[0].split("\n")))):
        assert apply_op(text, {"op": "set_state", "name": name, "yaml": shown}) == text, (name, shown)


def test_set_state_writes_a_change_of_comments_only():
    out = apply_op(BLOCK_AT_THE_END, {"op": "set_state", "name": "a", "yaml": "# a note\n" + A_AS_SHOWN})

    assert "  a:\n    # a note\n    description: one\n" in out, "the same data, but the author's comment is written"


@pytest.mark.parametrize("state, message", [
    ("  a: &base\n    description: first\n    transitions:\n      - target: done\n", "anchor, a tag or an alias"),
    ("  a: !special\n    transitions:\n      - target: done\n", "anchor, a tag or an alias"),
    ("  a: {transitions: [{target: done}],\n      description: two lines}\n", "goes on below it"),
])
def test_set_state_refuses_a_key_line_the_inspector_cannot_stand_in_for(state, message):
    text = f"stategraph: 1\nid: m\ninitial: a\nstates:\n{state}  done:\n    type: final\n"

    with pytest.raises(EditError, match=f"{message}.*YAML tab"):
        apply_op(text, {"op": "set_state", "name": "a", "yaml": "transitions:\n  - target: done\n"})


def test_set_state_refuses_what_is_not_a_mapping():
    with pytest.raises(EditError, match="must be a mapping"):
        apply_op(MACHINE, {"op": "set_state", "name": "done", "yaml": "- one\n- two\n"})
    with pytest.raises(EditError, match="does not parse"):
        apply_op(MACHINE, {"op": "set_state", "name": "done", "yaml": "type: [final\n"})


# ---------------------------------------------------------------- transitions

def test_add_transition_appends_with_trigger_first_and_code_as_a_block():
    out = apply_op(MACHINE, {"op": "add_transition", "source": "write", "target": "done", "trigger": "error",
                             "guard": "error.type == 'timeout'", "effect": "ctx.a = 1\nctx.b = 2"})

    added = data(out)["states"]["write"]["transitions"][-1]
    assert added == {"trigger": "error", "target": "done", "guard": "error.type == 'timeout'",
                     "effect": "ctx.a = 1\nctx.b = 2"}
    assert "effect: |-\n          ctx.a = 1\n          ctx.b = 2\n\n  # the review phase" in out
    assert kept(out) == []


def test_add_transition_creates_the_list_and_refuses_an_unknown_target():
    out = apply_op(MACHINE, {"op": "add_transition", "source": "failed", "target": "write", "trigger": "retry"})

    assert data(out)["states"]["failed"]["transitions"] == [{"trigger": "retry", "target": "write"}]
    assert out.rstrip().endswith("# the end") and kept(out) == []
    with pytest.raises(EditError, match="no state"):
        apply_op(MACHINE, {"op": "add_transition", "source": "write", "target": "nowhere"})


def test_update_transition_sets_and_removes_keys_in_place():
    out = apply_op(MACHINE, {"op": "update_transition", "source": "write", "index": 0,
                             "fields": {"guard": "ctx.ok", "effect": None}})

    assert data(out)["states"]["write"]["transitions"][0] == {"target": "review", "guard": "ctx.ok"}
    assert kept(out) == []


def test_update_transition_refuses_unknown_keys_and_indexes():
    with pytest.raises(EditError, match="unknown transition keys"):
        apply_op(MACHINE, {"op": "update_transition", "source": "write", "index": 0, "fields": {"colour": "red"}})
    with pytest.raises(EditError, match="no transition 7"):
        apply_op(MACHINE, {"op": "update_transition", "source": "write", "index": 7, "fields": {"guard": "x"}})


def test_remove_transition_takes_its_comment_along():
    out = apply_op(MACHINE, {"op": "remove_transition", "source": "write", "index": 0})

    assert data(out)["states"]["write"]["transitions"] == [{"trigger": "error", "target": "failed"}]
    assert kept(out, "# the happy path") == [] and "# the happy path" not in out


def test_move_transition_moves_the_lines_with_their_comment():
    out = apply_op(MACHINE, {"op": "move_transition", "source": "write", "index": 1, "to": 0})

    assert [t.get("target") for t in data(out)["states"]["write"]["transitions"]] == ["failed", "review"]
    assert "      - trigger: error\n        target: failed\n      # the happy path\n      - target: review" in out
    assert kept(out) == []


# ---------------------------------------------------------------- initial

def test_set_initial_of_the_machine_and_of_a_composite():
    top = apply_op(MACHINE, {"op": "set_initial", "name": "review"})
    nested = apply_op(MACHINE, {"op": "set_initial", "name": "verdict", "parent": "review"})

    assert data(top)["initial"] == "review" and "# start here" in top
    assert data(nested)["states"]["review"]["initial"] == "verdict"
    with pytest.raises(EditError, match="direct child"):
        apply_op(MACHINE, {"op": "set_initial", "name": "verdict", "parent": "write"})


# ---------------------------------------------------------------- layouts and refusals

def test_a_compact_list_layout_stays_compact():
    text = ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    transitions:\n    - target: b   # first\n"
            "    - target: a\n      guard: else\n  b:\n    type: final\n")

    out = apply_op(text, {"op": "add_transition", "source": "b", "target": "a", "trigger": "again"})

    assert out.startswith(text.split("  b:")[0]) and "    transitions:\n    - trigger: again\n      target: a\n" in out


def test_flow_style_is_rewritten_by_ruamel_and_still_reads_right():
    text = "stategraph: 1\nid: m\ninitial: a\nstates:\n  a: {transitions: [{target: b}, {target: a, guard: else}]}\n" \
           "  b: {type: final}  # the end\n"

    out = apply_op(text, {"op": "remove_transition", "source": "a", "index": 0})

    assert data(out)["states"]["a"]["transitions"] == [{"target": "a", "guard": "else"}]
    assert "# the end" in out


@pytest.mark.parametrize("op, message", [
    ({"op": "frobnicate"}, "unknown edit"),
    ("remove_state", "an edit is an object"),
    ({"op": "remove_state", "name": "ghost"}, "no state 'ghost'"),
    ({"op": "add_state", "name": "write"}, "exists already"),
    ({"op": "add_state", "name": "x", "parent": "done"}, "cannot hold states"),
    ({"op": "add_state", "name": "x", "type": "fork"}, "type 'fork'"),
    ({"op": "move_transition", "source": "write", "index": 0, "to": 5}, "to: a position"),
])
def test_invalid_edits_are_refused_with_a_reason(op, message):
    with pytest.raises(EditError, match=message):
        apply_op(MACHINE, op)


def test_an_unparseable_file_is_refused_not_rewritten():
    with pytest.raises(EditError, match="does not parse"):
        apply_op("states: [unclosed\n", {"op": "add_state", "name": "a"})


NOTED = "stategraph: 1\nid: m\ntitle: T  # the title\ninitial: a\nstates:\n  a:\n    type: final\n"


def test_set_note_writes_notes_and_the_last_one_removed_takes_the_key_along():
    one = apply_op(NOTED, {"op": "set_note", "name": "why", "text": "Because the review\nneeds a second look.  \n"})
    assert one == ("stategraph: 1\nid: m\ntitle: T  # the title\nnotes:\n  why: |-\n    Because the review\n"
                   "    needs a second look.\ninitial: a\nstates:\n  a:\n    type: final\n"), one
    two = apply_op(one, {"op": "set_note", "name": "todo", "text": "retry"})
    assert data(two)["notes"] == {"why": "Because the review\nneeds a second look.", "todo": "retry"}
    changed = apply_op(two, {"op": "set_note", "name": "todo", "text": "retry twice"})
    assert data(changed)["notes"]["todo"] == "retry twice" and "Because the review\n" in changed
    gone = apply_op(apply_op(changed, {"op": "set_note", "name": "todo", "text": ""}), {"op": "set_note", "name": "why", "text": None})
    assert gone == NOTED, gone


@pytest.mark.parametrize("op, refusal", [
    ({"name": "Why"}, "a note name"),
    ({"name": "why", "text": 3}, "the note's text"),
    ({"name": "gone", "text": ""}, "there is no note 'gone'"),
])
@pytest.mark.parametrize("text", [NOTED, NOTED.replace("initial: a", "notes:\n  why: kept\ninitial: a")])
def test_set_note_refuses_what_is_no_note(op, refusal, text):
    with pytest.raises(EditError, match=refusal):
        apply_op(text, {"op": "set_note", **op})


def test_set_note_leaves_notes_it_cannot_read_to_the_yaml_tab():
    with pytest.raises(EditError, match="change it in the YAML tab"):
        apply_op(NOTED.replace("initial: a", "notes: [a list]\ninitial: a"), {"op": "set_note", "name": "why", "text": "x"})
