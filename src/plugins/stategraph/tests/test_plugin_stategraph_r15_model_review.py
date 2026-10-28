"""Review 2026-10-15, model cluster: the validator, the loader, the editor and machine code (docs/review_2026-10-15.md).

Machines are YAML text through the real loader and ``validate_tree`` (``stategraph_testkit.validate``); edits go
through ``apply_op``; machine code through ``Namespace``/``load_module``. Each test names its review item.
"""

from __future__ import annotations

import datetime
import gc
import linecache
import textwrap
import traceback

import jsonschema
import pytest

from plugins.stategraph.model.code import CodeError, Namespace, Scope, load_module
from plugins.stategraph.model.graph import graph_view
from plugins.stategraph.model.loader import load_tree, parse_yaml, to_plain
from plugins.stategraph.model.schema_gen import machine_schema
from plugins.stategraph.model.validate import agent_params_problems
from plugins.stategraph.model.yamledit import apply_op, companion_mentions
from plugins.stategraph.model.spec import ParamSpec
from plugins.stategraph.store import FileSources, MachineStore
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, errors, found, load, validate

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


def machine(states: str, *, initial: str = "a", head: str = "", python: str | None = None) -> dict[str, str]:
    text = ("stategraph: 1\nid: m\n" + textwrap.dedent(head) + f"initial: {initial}\nstates:\n"
            + textwrap.indent(textwrap.dedent(states), "  "))
    files = {"m.yaml": text}
    if python is not None:
        files["m.yaml"] = text.replace("id: m\n", "id: m\npython: m.py\n", 1)
        files["m.py"] = textwrap.dedent(python)
    return files


def messages(tree, code: str) -> list[str]:
    return [p.message for p in found(tree, code)]


# ------------------------------------------------------------------ MOD-1: nested attributes of every namespace

@pytest.mark.parametrize("code", ["error.cause.type == 'timeout'", "event.data.reason == 'x'",
                                  "ending.error.type == 'x'", "run.visits.work > 1"])
def test_mod1_attribute_below_any_namespace_field_is_refused(code):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          finally: {{call: tidy, args: {{x: "{{{{ {code} }}}}"}}}}
          transitions:
            - target: b
        b: {{type: final}}
        """, python="def tidy(x):\n    return x\n"))

    assert any("plain data" in m for m in messages(tree, "SG004")), tree.problems


def test_mod1_calls_and_item_access_below_a_namespace_stay_clean():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - trigger: error
              target: b
              guard: error.data.get('x') == 1 and error.cause['type'] == 't' and run.visits.get('a', 0) < 3
            - target: b
        b: {type: final}
        """))

    assert not errors(tree), tree.problems


# ------------------------------------------------------------------ MOD-2: state names in code

@pytest.mark.parametrize("code", ["run.visits['nope'] < 2", "run.visits.get('nope', 0) < 2", "'nope' in run.visits",
                                  "run.state == 'nope'", "run.state in ('a', 'nope')", "'nope' != run.state",
                                  "run.entered['nope'] > 2", "run.entered.get('nope') is None",
                                  "'nope' in run.entered"])
def test_mod2_unknown_state_name_in_code_is_an_error(code):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          transitions:
            - target: b
              guard: "{code}"
        b: {{type: final}}
        """))

    assert any("'nope' is not a state" in m for m in messages(tree, "SG002")), tree.problems


def test_mod2_known_state_names_in_code_and_templates_are_clean():
    tree = validate(machine("""\
        a:
          entry: "ctx.n = run.visits.get('a', 0)"
          do: {agent: w, task: "round {{ run.visits['a'] }} of {{ run.state == 'a' }}"}
          transitions:
            - target: b
        b: {type: final}
        """, head="context: {n: 0}\n"))

    assert not errors(tree), tree.problems


RENAMED = """\
stategraph: 1
id: m
initial: produce
states:
  produce:
    entry: |
      if run.visits.get("produce", 0) > 1:
          ctx.again = True
    do: {agent: w, task: "try {{ run.visits['produce'] }}: produce"}
    transitions:
      - trigger: error
        target: produce
        guard: run.visits.get("produce", 0) < 2 and run.state == 'produce'
      - target: done
  done: {type: final}
machines:
  sub:
    initial: produce
    states:
      produce: {type: final, output: "{{ run.visits['produce'] }}"}
"""


def test_mod2_rename_state_rewrites_the_names_code_compares_with():
    out = apply_op(RENAMED, {"op": "rename_state", "old": "produce", "new": "make"})

    data = to_plain(parse_yaml(out))
    make = data["states"]["make"]
    assert 'run.visits.get("make", 0) > 1' in make["entry"]
    assert make["do"]["task"] == "try {{ run.visits['make'] }}: produce"  # the text outside {{ }} is text
    assert make["transitions"][0]['guard'] == "run.visits.get(\"make\", 0) < 2 and run.state == 'make'"
    assert data["machines"]["sub"]["states"]["produce"]["output"] == "{{ run.visits['produce'] }}"  # its own states
    tree = validate({"m.yaml": out})
    assert not errors(tree), tree.problems


def test_mod2_companion_mentions_lists_the_lines_a_rename_leaves():
    source = 'def again(visits):\n    return visits.get("produce", 0)\n\nNAME = "produce"\nOTHER = "x"\n'

    assert companion_mentions(source, "produce") == [2, 4]


# ------------------------------------------------------------------ MOD-3: events on states that never wait

def test_mod3_event_transition_on_a_state_with_do_warns():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - target: b
            - trigger: cancel
              target: b
        b: {type: final}
        """, head="events: {cancel: {}}\n"))

    assert [p.path for p in found(tree, "SG112")] == ["states.a.transitions[1].trigger"], tree.problems


def test_mod3_event_transitions_on_wait_timer_and_composite_states_do_not_warn():
    tree = validate(machine("""\
        a:
          transitions:
            - trigger: go
              target: t
        t:
          after: 1s
          transitions:
            - target: c
            - trigger: go
              target: b
        c:
          initial: w
          states:
            w:
              transitions:
                - trigger: go
                  target: b
          transitions:
            - trigger: stop
              target: b
        b: {type: final}
        """, head="events: {go: {}, stop: {}}\n"))

    assert not found(tree, "SG112"), tree.problems


# ------------------------------------------------------------------ MOD-4: move_state of an initial

def test_mod4_the_initial_follows_into_the_composite_it_moved_into():
    text = ("stategraph: 1\nid: m\ninitial: start\nstates:\n  start:\n    transitions:\n      - target: work\n"
            "  other:\n    transitions:\n      - target: done\n"  # the first state left: not the answer
            "  done:\n    type: final\n  work:\n    transitions:\n      - target: done\n")

    out = apply_op(text, {"op": "move_state", "name": "start", "into": "work"})

    data = to_plain(parse_yaml(out))
    assert data["initial"] == "work" and data["states"]["work"]["initial"] == "start", out


def test_mod4_a_final_never_becomes_the_initial():
    text = ("stategraph: 1\nid: m\ninitial: c\nstates:\n  c:\n    initial: b\n    states:\n      a:\n"
            "        type: final\n      b:\n        transitions:\n          - target: a\n      d:\n"
            "        transitions:\n          - target: a\n  e:\n    type: final\n")

    picked = to_plain(parse_yaml(apply_op(text, {"op": "move_state", "name": "b", "into": None})))
    only_finals = to_plain(parse_yaml(apply_op(text.replace("      d:\n        transitions:\n          - target: a\n",
                                                            "      d:\n        type: final\n"),
                                               {"op": "move_state", "name": "b", "into": None})))

    assert picked["states"]["c"]["initial"] == "d"
    assert only_finals["states"]["c"]["initial"] == "b"  # left to SG002: the author chooses


# ------------------------------------------------------------------ MOD-5: comments typed as $yaml

VARS = """\
stategraph: 1
id: m
initial: write
states:
  write:
    do:
      agent: a
      task: t
      vars:
        # the tone of the call
        tone: plain   # plain or lively
        genre: x
    transitions:
      - target: done
  done: {type: final}
"""


def vars_block(out: str) -> list[str]:
    return [line.strip() for line in out[out.index("vars:"):out.index("transitions")].splitlines()[1:]
            if line.strip()]


@pytest.mark.parametrize("typed, expected", [
    ("# the TONE\ntone: plain   # plain or lively\ngenre: x\n", ["# the TONE", "tone:", "genre: x"]),
    ("# one\n# two\ntone: plain\ngenre: x\n", ["# one", "# two", "tone:", "genre: x"]),
    ("# the tone of the call\ntone: dry\ngenre: x\n", ["# the tone of the call", "tone:", "genre: x"]),
    ("tone: plain\ngenre: x\n", ["tone:", "genre: x"]),
], ids=["changed", "added", "first_key_changed", "removed"])
def test_mod5_typed_comments_stand_where_they_were_typed(typed, expected):
    out = apply_op(VARS, {"op": "update_state", "name": "write", "do": {"vars": {"$yaml": typed}}})

    got = vars_block(out)
    assert [line if line.startswith("#") or line == "genre: x" else line.split(" ")[0] for line in got] == expected, out
    assert all(line.startswith("        ") for line in out.splitlines() if line.strip().startswith("# t")), out


KEY_LINE = """\
stategraph: 1
id: m
params:        # bound by whoever starts it
  # the first one
  p: {type: string, default: x}
initial: a
states:
  a:
    do:
      agent: w
      task: t
      retry: {attempts: 2, backoff: 30s}   # once more, fresh
    transitions:
      - target: b
  b: {type: final}
"""


def test_mod5_a_value_retyped_as_shown_keeps_its_key_line_comments():
    graph = graph_view(validate({"m.yaml": KEY_LINE}))
    retry = next(state for state in graph["states"] if state["name"] == "a")["yaml"]["do.retry"]

    assert apply_op(KEY_LINE, {"op": "update_machine", "fields": {"params": {"$yaml": graph["yaml"]["params"]}}}) \
        == KEY_LINE
    assert apply_op(KEY_LINE, {"op": "update_state", "name": "a", "do": {"retry": {"$yaml": retry}}}) == KEY_LINE


# ------------------------------------------------------------------ MOD-6: kind values from optional params

def test_mod6_an_optional_param_without_default_cannot_name_the_agent():
    head = "params:\n  who: {type: string, enum: [w, v]}\n  how: {type: string, enum: [w, v], default: w}\n"
    tree = validate(machine("""\
        a:
          do: {agent: "{{ params.who }}", task: t}
          transitions:
            - target: b
        b:
          do: {agent: "{{ params.how }}", task: t}
          transitions:
            - target: c
        c: {type: final}
        """, head=head))

    assert [p.path for p in found(tree, "SG005")] == ["states.a.do.agent"], tree.problems


# ------------------------------------------------------------------ MOD-7: map over what is no list

@pytest.mark.parametrize("source", ['"{\'a\': 1}"', '"\'abc\'"', '"{k: 1 for k in ctx.xs}"', '"f\'{ctx.xs}\'"'])
def test_mod7_map_over_a_dict_or_text_literal_is_refused(source):
    tree = validate(machine(f"""\
        a:
          do: {{map: {source}, each: {{agent: w, task: t}}}}
          transitions:
            - target: b
        b: {{type: final}}
        """, head="context: {xs: []}\n"))

    assert [p.path for p in found(tree, "SG005")] == ["states.a.do.map"], tree.problems


def test_mod7_map_over_a_list_expression_is_clean():
    tree = validate(machine("""\
        a:
          do: {map: "list(ctx.d.items())", each: {agent: w, task: t}}
          transitions:
            - target: b
        b: {type: final}
        """, head="context: {d: {}}\n"))

    assert not errors(tree), tree.problems


# ------------------------------------------------------------------ MOD-9: agent: block params

def test_mod9_agent_params_required_with_default_unknown_and_mistyped():
    declared = {"task": ParamSpec(type="string", required=True), "mode": ParamSpec(required=True, default="fast"),
                "n": ParamSpec(type="integer", enum=[1, 2])}

    assert agent_params_problems("m", declared, {"input": "text"}) == []
    problems = agent_params_problems("m", declared, {"input": "text", "params": {"modee": "x", "n": 3}})
    assert any("no param 'modee'" in p for p in problems) and any(p.startswith("params.n: 3 is not one of") for p
                                                                   in problems), problems
    assert any("params.n: 'x' is not a integer" in p
               for p in agent_params_problems("m", declared, {"input": "json", "params": {"n": "x"}}))
    declared["who"] = ParamSpec(type="string", required=True)
    assert any("requires who" in p for p in agent_params_problems("m", declared, {"params": {"who": None}}))


# ------------------------------------------------------------------ MOD-10: machine vars that read ctx

def test_mod10_machine_vars_reading_ctx_warn_params_do_not():
    tree = validate(machine("""\
        a: {type: final}
        """, head="params: {p: {type: string, default: x}}\ncontext: {tone: plain}\n"
                  "vars: {tone: '{{ ctx.tone }}', other: '{{ params.p }}'}\n"))

    assert [p.path for p in found(tree, "SG113")] == ["vars.tone"], tree.problems


# ------------------------------------------------------------------ MOD-11: unreadable files say why

def test_mod11_a_file_that_is_not_utf8_says_so(tmp_path):
    root = tmp_path / "machines"
    root.mkdir()
    (root / "m.yaml").write_text("stategraph: 1\nid: m\npython: m.py\ninitial: a\nstates:\n  a: {type: final}\n",
                                 encoding="utf-8")
    (root / "m.py").write_bytes("# Grüße\n".encode("cp1252"))
    (root / "n.yaml").write_bytes("stategraph: 1\nid: n\ninitial: a\nstates:\n  a: {type: final}  # ü\n"
                                  .encode("cp1252"))
    store = MachineStore([str(root)], [str(root)])

    assert any("m.py is not UTF-8 (byte 0xfc" in m for m in messages(store.load("m"), "SG004"))
    tree = load_tree(str(root / "n.yaml"), FileSources(store))
    assert any("is not UTF-8 (byte 0xfc" in m for m in messages(tree, "SG006")), tree.problems


def test_mod11_a_missing_file_still_reads_not_found():
    tree = load({"m.yaml": "stategraph: 1\nid: m\npython: gone.py\ninitial: a\nstates:\n  a: {type: final}\n"})

    assert messages(tree, "SG004") == ["companion module not found: gone.py"]


# ------------------------------------------------------------------ MOD-12: match statements

def test_mod12_match_captures_are_names_in_effects_and_modules():
    tree = validate(machine("""\
        a:
          do: {call: kind_of}
          transitions:
            - target: b
              effect: |
                match out:
                    case {"kind": k, **rest}:
                        ctx.kind = k
                    case [first, *others]:
                        ctx.kind = first
                    case _:
                        ctx.kind = limit
        b: {type: final}
        """, head="context: {kind: null}\n", python="""\
        match 1:
            case int() as limit:
                pass

        def kind_of():
            return limit
        """))

    assert not errors(tree), tree.problems


# ------------------------------------------------------------------ MOD-13: durations past what can be stamped

@pytest.mark.parametrize("value", [".inf", ".nan", "1e12", "99999999h", "1" + "0" * 400],
                         ids=["inf", "nan", "huge_float", "huge_hours", "int_past_float"])
def test_mod13_infinite_or_absurd_durations_are_refused(value):
    tree = validate(machine(f"""\
        a:
          timeout: {value}
          transitions:
            - trigger: go
              target: b
        b: {{type: final}}
        """, head="events: {go: {}}\n"))

    assert any("longer than ten years" in m for m in messages(tree, "SG001")), tree.problems


# ------------------------------------------------------------------ MOD-14: a scalar no JSON says

def test_mod14_a_final_output_date_keeps_the_graph():
    tree = validate(machine("""\
        a: {type: final, output: 2024-01-01}
        """))
    assert not errors(tree), tree.problems

    graph = graph_view(tree)

    state = graph["states"][0]
    assert state["name"] == "a" and "output" in state["locked"] and "output" not in state["yaml"]
    assert state["output"] == datetime.date(2024, 1, 1)


# ------------------------------------------------------------------ MOD-15: line separators inside a text

def test_mod15_a_unicode_line_separator_does_not_shift_the_planned_lines():
    text = ("stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    description: \"pasted\u2028text\"\n"
            "    transitions:\n      - target: b\n  b:\n    transitions:\n      - target: c\n  c:\n    type: final\n")

    out = apply_op(text, {"op": "move_state", "name": "c", "into": "b"})

    assert "pasted\u2028text" in out and "\\L" not in out, out
    assert to_plain(parse_yaml(out))["states"]["b"]["states"] == {"c": {"type": "final"}}


# ------------------------------------------------------------------ FEAT-5: call args against the function

COMPANION = """\
from textwrap import dedent

def build_text(text, *, mode="plain"):
    return text

def with_sg(sg, text):
    return text

def anything(**kwargs):
    return kwargs

def parse_two(text, extra):
    return text

def check_it(sg, out):
    return None
"""


@pytest.mark.parametrize("activity, where, says", [
    ("{call: build_text, args: {txt: hello}}", "states.a.do.args.txt", "it takes no argument 'txt'"),
    ("{call: build_text, args: {mode: x}}", "states.a.do.call", "argument 'text' is required"),
    ("{call: with_sg, args: {}}", "states.a.do.call", "argument 'text' is required"),
    ("{call: dedent, args: {txt: x}}", "states.a.do.args.txt", "it takes no argument 'txt'"),
    ("{call: 'textwrap:dedent', args: {txt: x}}", "states.a.do.args.txt", "it takes no argument 'txt'"),
    ("{agent: w, task: t, parse: parse_two}", "states.a.do.parse", "missing a required argument: 'extra'"),
])
def test_feat5_a_call_the_function_cannot_take_is_an_error(activity, where, says):
    tree = validate(machine(f"""\
        a:
          do: {activity}
          transitions:
            - target: b
        b: {{type: final}}
        """, python=COMPANION))

    hits = [p for p in found(tree, "SG004") if says in p.message]
    assert [p.path for p in hits] == [where], tree.problems


@pytest.mark.parametrize("activity", [
    "{call: build_text, args: {text: hello, mode: x}}", "{call: with_sg, args: {text: x}}",
    "{call: anything, args: {whatever: 1}}", "{call: 'textwrap:dedent', args: {text: x}}",
    "{agent: w, task: t, check: check_it}",
])
def test_feat5_a_call_that_fits_is_clean(activity):
    tree = validate(machine(f"""\
        a:
          do: {activity}
          transitions:
            - target: b
        b: {{type: final}}
        """, python=COMPANION))

    assert not errors(tree), tree.problems


def test_feat5_a_decorated_function_may_take_more_than_its_def_names():
    # a flow's _additive wrapper takes the args of a newer machine: a warning, not a refusal
    tree = validate(machine("""\
        a:
          do: {call: tolerant, args: {text: x, argument_of_tomorrow: 1}}
          transitions:
            - target: b
        b: {type: final}
        c:
          do: {call: tolerant, args: {}}
          transitions:
            - target: b
        """, python=COMPANION + "\n@decorate\ndef tolerant(text):\n    return text\n"))

    unsure = [p for p in found(tree, "SG004") if "argument_of_tomorrow" in p.message]
    assert [(p.level, p.path) for p in unsure] == [("warning", "states.a.do.args.argument_of_tomorrow")], tree.problems
    assert [p.path for p in errors(tree) if "'text' is required" in p.message] == ["states.c.do.call"], tree.problems


# ------------------------------------------------------------------ DBG-1, DBG-10: where machine code raised

def test_dbg1_a_code_field_error_carries_its_line_and_code():
    ctx = Scope({"a": {}})
    with pytest.raises(CodeError) as caught:
        Namespace().execute("x = 1\nctx.b = ctx.a['nope']\n", {"ctx": ctx}, "one.transitions[1].effect")

    assert caught.value.message == "KeyError: 'nope'"  # unchanged: the journal hashes it
    assert caught.value.location == {"where": "one.transitions[1].effect", "file": "one.transitions[1].effect",
                                     "line": 2, "code": "ctx.b = ctx.a['nope']"}


def test_dbg1_dbg10_an_error_inside_a_companion_function_names_its_file_line_and_code():
    namespace = load_module("def helper(table):\n    return table['nope']\n", "m.py")

    with pytest.raises(CodeError) as caught:
        namespace.evaluate("helper({})", {"ctx": Scope({})}, "s.do.args.x")

    assert (caught.value.file, caught.value.line, caught.value.code) == ("m.py", 2, "return table['nope']")
    text = "".join(traceback.format_exception(caught.value.cause))
    assert "m.py\", line 2, in helper\n    return table['nope']" in text, text


def test_dbg10_a_finished_namespace_leaves_no_source_behind():
    namespace = load_module("def f():\n    return 1\n", "gone.py")
    shown = namespace.public["f"].__code__.co_filename
    assert linecache.getline(shown, 2).strip() == "return 1", "fixture: the source is registered"

    del namespace
    gc.collect()

    assert shown not in linecache.cache


# ------------------------------------------------------------------ MOD-17: activities typed everywhere in the schema

def test_mod17_the_schema_types_finally_resources_and_nested_activities():
    validator = jsonschema.Draft202012Validator(machine_schema())
    base = {"stategraph": 1, "id": "m", "initial": "a", "states": {"a": {"type": "final"}}}
    good = {"agent": "w", "task": "t"}
    bad = {"agent": "w", "taks": "t"}
    places = [
        ("finally",), ("resources", "r", "open"), ("states", "a", "finally"),
        ("states", "a", "do", "each"), ("states", "a", "do", "parallel", "one"),
    ]
    for place in places:
        for activity, valid in ((good, True), (bad, False)):
            doc = {**base, "states": {"a": {"type": "final"}}}
            if place[:3] == ("states", "a", "do"):
                outer = {"map": "ctx.xs", "each": activity} if place[3] == "each" else {"parallel": {"one": activity}}
                doc["states"] = {"a": {"do": outer}}
            elif place[0] == "states":
                doc["states"] = {"a": {"finally": activity}}
            elif place[0] == "resources":
                doc["resources"] = {"r": {"open": activity}}
            else:
                doc["finally"] = activity
            assert validator.is_valid(doc) is valid, (place, activity, list(validator.iter_errors(doc))[:1])
