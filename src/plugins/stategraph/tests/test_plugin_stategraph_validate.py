"""The validator (docs/stategraph_design.md §4): one test per check it implements.

Machines are YAML text through the real loader and ``validate_tree``. SG007 is
driven twice: with a fake ``config_check`` for the plumbing (which names reach
the configuration, enum values one by one) and with the production
``make_config_check`` over a real ``AgentSystemConfig`` for the answers
(own-tool refusal, allowlists, agents).

Mutation checks run (each turned the named tests red, then was restored from a copy):
- validate._check_transitions: the undeclared-trigger check removed          -> test_sg002_undeclared_event_trigger
- validate._collect_states: the duplicate-name branch removed                  -> test_sg002_duplicate_state_name
- validate._check_state: the choice-else check removed                         -> test_sg003_choice_without_else
- validate._check_state: ``is_wait and not _accepts_somewhere`` -> ``False``   -> test_sg003_wait_state_that_accepts_no_event
- validate._check_state: ``_accepts_somewhere`` walks only the state itself    -> test_a_wait_state_may_rely_on_an_enclosing_state
- validate._check_graph: the pseudo-cycle loop removed                         -> test_sg003_pseudostate_cycle
- validate._check_code: the braced() check removed                             -> test_sg004_code_field_in_braces
- validate._check_code: ``unbound`` computed as empty                          -> test_sg004_name_not_bound_here
- validate._check_activity: the parse/call companion check removed              -> test_sg004_missing_companion_function
- validate._check_references: enum values not expanded                        -> test_sg007_fake_check_sees_every_enum_value
- backend.make_config_check: own-instance clause removed                      -> test_sg007_production_check_refuses_stategraphs_own_tools
- backend.make_config_check: the runner / facade / is_agent / own-allowlist clause removed, one at a
  time; blocked patterns ignored; only own_instance searched; the raw type instead of the resolved one;
  the SAM walk removed; the SAM walk one level only                             -> test_sg007_refuses_agents_that_reach_machines[its case]
- backend.make_config_check: the walk back from the refused agents stops after one step
                                                                               -> test_sg007_follows_sams_through_a_cycle,
                                                                                  test_sg007_refuses_agents_that_reach_machines[chain_spawner]
- validate._check_graph: SG101/SG102/SG103 blocks removed one at a time        -> the matching test_sg10x
- validate._check_transitions: SG104 branch removed                            -> test_sg104_transition_after_an_unguarded_one
- validate._validate_file: SG105 loop removed                                  -> test_sg105_undeclared_context_read
- code._IMPURE_NAMES emptied                                                    -> test_sg106_impure_code[set/hash/open]
- code._IMPURE_CALLS without random/time/uuid                                   -> test_sg106_impure_code[random/time/uuid]
- validate._lint_bare_references removed                                       -> test_sg107_bare_reference
- validate._validate_file: SG108 block removed                                 -> test_sg108_submachine_with_run_timeout
"""

from __future__ import annotations

import textwrap

import pytest

from plugins.stategraph.engine.backend import make_config_check
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, errors, found, validate

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


def machine(states: str, *, initial: str = "a", head: str = "", python: str | None = None,
            **others: str) -> dict[str, str]:
    """A machine ``m`` with the given states block (and optional top-level keys, companion, other files)."""
    text = ("stategraph: 1\nid: m\n" + textwrap.dedent(head) + f"initial: {initial}\nstates:\n"
            + textwrap.indent(textwrap.dedent(states), "  "))
    files = {"m.yaml": text}
    if python is not None:
        files["m.yaml"] = text.replace("id: m\n", "id: m\npython: m.py\n", 1)
        files["m.py"] = textwrap.dedent(python)
    files.update(others)
    return files


def pairs(tree) -> set[tuple[str, str]]:
    return {(p.code, p.path) for p in tree.problems}


def messages(tree, code: str) -> str:
    return " | ".join(p.message for p in found(tree, code))


def test_a_sound_machine_has_no_problems():
    tree = validate(machine("""\
        a:
          do: {agent: writer, task: "write about {{ params.topic }}"}
          transitions:
            - target: done
              effect: ctx.draft = out
            - trigger: error
              target: failed
        done: {type: final, output: "{{ ctx.draft }}"}
        failed: {type: final, status: failed}
        """, head="params: {topic: {type: string, required: true}}\ncontext: {draft: null}\n"))
    assert tree.problems == []


# ------------------------------------------------------------------ SG001 (validator part)

def test_sg001_context_value_that_is_not_json():
    tree = validate(machine("a: {type: final}\n", head="context: {x: .nan}\n"))
    assert ("SG001", "context.x") in pairs(tree)


# ------------------------------------------------------------------ SG002

def test_sg002_unknown_target_and_initial():
    tree = validate(machine("""\
        a:
          transitions:
            - target: nowhere
        """, initial="missing"))
    assert ("SG002", "states.a.transitions[0].target") in pairs(tree)
    assert ("SG002", "initial") in pairs(tree)


def test_sg002_initial_must_be_top_level():
    tree = validate(machine("""\
        c:
          initial: inner
          states:
            inner: {type: final}
          transitions:
            - target: done
        done: {type: final}
        """, initial="inner"))
    assert "is a nested state" in messages(tree, "SG002")


def test_sg002_composite_initial_must_be_a_direct_child():
    tree = validate(machine("""\
        c:
          initial: deep
          states:
            mid:
              initial: deep
              states:
                deep: {type: final}
              transitions:
                - target: fin
            fin: {type: final}
          transitions:
            - target: done
        done: {type: final}
        """, initial="c"))
    assert ("SG002", "states.c.initial") in pairs(tree)


def test_sg002_duplicate_state_name():
    tree = validate(machine("""\
        a:
          initial: b
          states:
            b: {type: final}
          transitions:
            - target: b
        b: {type: final}
        """))
    assert "used twice" in messages(tree, "SG002")


def test_sg002_undeclared_event_trigger():
    tree = validate(machine("""\
        a:
          transitions:
            - trigger: approve
              target: done
        done: {type: final}
        """))
    assert ("SG002", "states.a.transitions[0].trigger") in pairs(tree)
    declared = validate(machine("""\
        a:
          transitions:
            - trigger: approve
              target: done
        done: {type: final}
        """, head="events: {approve: {}}\n"))
    assert errors(declared) == []


# ------------------------------------------------------------------ SG003

def test_sg003_choice_without_else():
    tree = validate(machine("""\
        a:
          transitions: [{target: pick}]
        pick:
          type: choice
          transitions:
            - target: done
              guard: ctx.x > 1
        done: {type: final}
        """, head="context: {x: 0}\n"))
    assert ("SG003", "states.pick.transitions") in pairs(tree)


def test_sg003_else_not_last():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - target: done
              guard: else
            - target: done
              guard: out == 1
        done: {type: final}
        """))
    assert ("SG003", "states.a.transitions[0].guard") in pairs(tree)


def test_sg003_trigger_on_a_pseudostate():
    tree = validate(machine("""\
        a:
          transitions: [{target: j}]
        j:
          type: junction
          transitions:
            - trigger: error
              target: done
        done: {type: final}
        """))
    assert ("SG003", "states.j.transitions[0].trigger") in pairs(tree)


def test_sg003_pseudostate_with_state_keys_and_without_transitions():
    tree = validate(machine("""\
        a:
          transitions: [{target: pick}]
        pick:
          type: choice
          entry: ctx.x = 1
        done: {type: final}
        """, head="context: {x: 0}\n"))
    assert ("SG003", "states.pick.entry") in pairs(tree)
    assert "needs outgoing transitions" in messages(tree, "SG003")


@pytest.mark.parametrize("key,value", [
    ("do", "{agent: w, task: t}"), ("entry", "ctx.x = 1"), ("exit", "ctx.x = 1"),
    ("max_visits", "2"), ("timeout", "5s"),
])
def test_sg003_final_with_state_keys(key, value):
    tree = validate(machine(f"""\
        a:
          transitions: [{{target: done}}]
        done:
          type: final
          {key}: {value}
        """, head="context: {x: 0}\n"))
    assert ("SG003", f"states.done.{key}") in pairs(tree)


def test_sg003_final_with_transitions_and_nested_final_with_status():
    tree = validate(machine("""\
        c:
          initial: inner
          states:
            inner: {type: final, status: failed}
          transitions: [{target: done}]
        done:
          type: final
          transitions: [{target: c}]
        """, initial="c"))
    assert ("SG003", "states.c.states.inner.status") in pairs(tree)
    assert ("SG003", "states.done.transitions") in pairs(tree)


def test_sg003_status_or_output_on_a_state():
    tree = validate(machine("""\
        a:
          output: 1
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert ("SG003", "states.a") in pairs(tree)


def test_sg003_composite_with_do_or_without_initial():
    tree = validate(machine("""\
        c:
          do: {agent: w, task: t}
          states:
            inner: {type: final}
          transitions: [{target: d}]
        d:
          states:
            inner2: {type: final}
          transitions: [{target: done}]
        done: {type: final}
        """, initial="c"))
    assert ("SG003", "states.c.do") in pairs(tree)
    assert ("SG003", "states.d") in pairs(tree)


def test_sg003_initial_on_a_simple_state():
    tree = validate(machine("""\
        a:
          initial: done
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert ("SG003", "states.a.initial") in pairs(tree)


def test_sg003_wait_state_that_accepts_no_event():
    tree = validate(machine("""\
        a:
          transitions:
            - trigger: error
              target: done
        done: {type: final}
        """))
    [problem] = found(tree, "SG003")
    assert problem.path == "states.a"
    assert "wait forever" in problem.message


def test_a_wait_state_may_rely_on_an_enclosing_state():
    tree = validate(machine("""\
        c:
          initial: w
          states:
            w:
              transitions:
                - trigger: error
                  target: done
          transitions:
            - trigger: cancel
              target: done
        done: {type: final}
        """, initial="c", head="events: {cancel: {}}\n"))
    assert errors(tree) == []


def test_sg003_timeout_on_a_state_that_is_not_a_wait_state():
    tree = validate(machine("""\
        a:
          timeout: 5s
          do: {agent: w, task: t}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert ("SG003", "states.a.timeout") in pairs(tree)


def test_sg003_pseudostate_cycle():
    tree = validate(machine("""\
        a:
          transitions: [{target: pick}]
        pick:
          type: choice
          transitions:
            - target: hop
              guard: ctx.x > 0
            - target: done
              guard: else
        hop:
          type: junction
          transitions:
            - target: pick
        done: {type: final}
        """, head="context: {x: 0}\n"))
    assert "form a cycle" in messages(tree, "SG003")


@pytest.mark.parametrize("trigger", ["done", "error"])
def test_sg003_internal_completion_or_error_transition(trigger):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          transitions:
            - trigger: {trigger}
              effect: ctx.x = 1
            - target: done
        done: {{type: final}}
        """, head="context: {x: 0}\n"))
    assert errors(tree), "the internal completion/error transition is not reported at all"
    assert [p.code for p in errors(tree)] == ["SG003"]


# ------------------------------------------------------------------ SG004

def test_sg004_code_that_does_not_compile():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - target: done
              guard: out >
        done: {type: final}
        """))
    assert ("SG004", "states.a.transitions[0].guard") in pairs(tree)


def test_sg004_unknown_name():
    tree = validate(machine("""\
        a:
          entry: ctx.x = helper(1)
          transitions: [{target: done}]
        done: {type: final}
        """, head="context: {x: 0}\n"))
    assert "unknown name 'helper'" in messages(tree, "SG004")


@pytest.mark.parametrize("transition", [
    '{trigger: error, guard: "out == 1", target: done}',
    '{target: done, effect: "ctx.x = error.message"}',
    '{target: done, guard: "event.name == 1"}',
], ids=["out_in_error_transition", "error_in_completion", "event_in_completion"])
def test_sg004_name_not_bound_here(transition):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          transitions:
            - {transition}
            - {{target: done}}
        done: {{type: final}}
        """, head="context: {x: 0}\n"))
    assert [p.code for p in errors(tree)] == ["SG004"], tree.problems
    assert "is not bound here" in messages(tree, "SG004")


def test_sg004_out_is_not_bound_in_an_activity_template():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: "{{ out }}"}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert ("SG004", "states.a.do.task") in pairs(tree)


def test_sg004_code_field_in_braces():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - target: done
              guard: "{{ out == 1 }}"
            - target: done
              guard: else
        done: {type: final}
        """))
    assert ("SG004", "states.a.transitions[0].guard") in pairs(tree)


def test_sg004_missing_companion_function():
    tree = validate(machine("""\
        a:
          do: {call: not_there}
          transitions: [{target: b}]
        b:
          do: {agent: w, task: t, parse: nope}
          transitions: [{target: done}]
        done: {type: final}
        """, python="def present():\n    return 1\n"))
    assert ("SG004", "states.a.do.call") in pairs(tree)
    assert ("SG004", "states.b.do.parse") in pairs(tree)


def test_companion_names_are_in_scope():
    tree = validate(machine("""\
        a:
          do: {call: present, args: {n: "{{ LIMIT }}"}}
          transitions: [{target: done, guard: "out < LIMIT"}, {target: done, guard: else}]
        done: {type: final}
        """, python="LIMIT = 3\ndef present(n):\n    return n\n"))
    assert errors(tree) == []


# ------------------------------------------------------------------ SG005

@pytest.mark.parametrize("do,needle", [
    ("{bogus: 1}", "names no activity kind"),
    ("{agent: w, tool: t, task: x}", "several kinds"),
    ("{agent: w, task: x, bogus: 1}", "unknown key 'bogus'"),
    ("{agent: w}", "Field required"),
    ("{decide: choice, question: q, input: x, criteria: {only: one}}", "at least two options"),
    ("{decide: score, question: q, input: x, criteria: [low]}", "at least two levels"),
    ("{decide: noul, question: q, input: x, criteria: [a, b]}", "a noul's criteria"),
    ("{decide: questions, input: x}", "needs questions"),
    ("{map: '[1]', each: {agent: w, task: t}, concurrency: 0}", "greater than or equal to 1"),
])
def test_sg005_invalid_activity(do, needle):
    tree = validate(machine(f"""\
        a:
          do: {do}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """))
    assert needle in messages(tree, "SG005"), tree.problems


def test_sg005_parametrised_kind_needs_an_enum():
    tree = validate(machine("""\
        a:
          do: {agent: "{{ params.who }}", task: t}
          transitions: [{target: done}]
        done: {type: final}
        """, head="params: {who: {type: string, default: writer}}\n"))
    assert ("SG005", "states.a.do.agent") in pairs(tree)


# ------------------------------------------------------------------ SG006

SUB = """\
stategraph: 1
id: sub
params:
  text: {type: string, required: true}
  mode: {type: string, required: true, default: fast}
initial: a
states:
  a: {type: final, output: "{{ params.text }}"}
"""


def test_sg006_unknown_alias():
    tree = validate(machine("""\
        a:
          do: {machine: not_imported}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert ("SG006", "states.a.do.machine") in pairs(tree)


def test_sg006_missing_required_and_unknown_parameter():
    tree = validate(machine("""\
        a:
          do: {machine: sub, params: {txt: typo}}
          transitions: [{target: done}]
        done: {type: final}
        """, head="imports: {sub: ./sub.yaml}\n", **{"sub.yaml": SUB}))
    assert ("SG006", "states.a.do.params.txt") in pairs(tree)
    required = messages(tree, "SG006")
    assert "requires parameter 'text'" in required
    assert "'mode'" not in required, "a required parameter with a default is not missing"


# ------------------------------------------------------------------ SG007

SG007_MACHINE = """\
    a:
      do: {agent: writer, task: t}
      transitions: [{target: b}]
    b:
      do: {agent: forbidden, task: t}
      transitions: [{target: c}]
    c:
      do: {agent: "{{ params.who }}", task: t}
      transitions: [{target: d}]
    d:
      do: {tool: "{{ params.store }}", args: {k: v}}
      transitions: [{target: e}]
    e:
      do: {decide: noul, question: q, input: x, profile: strict}
      transitions: [{target: done}]
    done: {type: final}
    """
SG007_HEAD = """\
    params:
      who: {type: string, enum: [writer, forbidden], default: writer}
      store: {type: string, enum: [json_store_read, stategraph_run_machine], default: json_store_read}
    """


def test_sg007_fake_check_sees_every_enum_value():
    seen: list[tuple[str, str, dict]] = []
    refused = {("agent", "forbidden"), ("tool", "stategraph_run_machine"), ("profile", "strict")}

    def check(what, name, extra):
        seen.append((what, name, dict(extra)))
        return f"{what} {name} refused" if (what, name) in refused else None

    tree = validate(machine(SG007_MACHINE, head=SG007_HEAD), config_check=check)

    assert ("agent", "writer", {}) in seen
    enum_checks = [(what, name) for what, name, _ in seen if what in ("agent", "tool")]
    assert enum_checks.count(("agent", "forbidden")) == 2, "the enum value must be checked like a literal"
    assert ("tool", "json_store_read") in enum_checks and ("tool", "stategraph_run_machine") in enum_checks
    assert ("profile", "strict", {}) in seen
    sg007 = pairs(tree)
    assert ("SG007", "states.b.do.agent") in sg007
    assert ("SG007", "states.c.do.agent") in sg007
    assert ("SG007", "states.d.do.tool") in sg007
    assert ("SG007", "states.e.do.decide") in sg007
    assert ("SG007", "states.a.do.agent") not in sg007


def _system_config(runner_allows=("stategraph", "sg_copy", "json_store", "some_sam")):
    from agent_system.config.models import AgentSystemConfig

    return AgentSystemConfig.model_validate({"plugins": {"servers": {
        "stategraph": {"type": "stategraph", "enabled": True},
        "sg_copy": {"type": "stategraph", "enabled": True},
        "stategraph_runner": {"type": "basic_agent", "enabled": True,
                              "agent_config": {"tools": {"allowed": list(runner_allows)}}},
        "some_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["writer"]},
        "writer": {"type": "basic_agent", "enabled": True},
        "critic": {"type": "basic_agent", "enabled": True},
        "json_store": {"type": "json_store", "enabled": True},
        "web_search": {"type": "tavily_search", "enabled": True},
        "author": {"type": "basic_agent", "enabled": True,
                   "agent_config": {"tools": {"allowed": ["stategraph/stategraph_run_machine"]}}},
        "blocked_author": {"type": "basic_agent", "enabled": True,
                           "agent_config": {"tools": {"allowed": ["stategraph/*"],
                                                      "blocked": ["stategraph/*"]}}},
        "reader": {"type": "basic_agent", "enabled": True,
                   "agent_config": {"tools": {"allowed": ["stategraph/stategraph_catalog", "sg_copy/sg_copy_get_run"]}}},
        "copy_author": {"type": "basic_agent", "enabled": True,
                        "agent_config": {"tools": {"allowed": ["sg_copy/*"]}}},
        "story_machine": {"type": "stategraph_machine", "enabled": True, "machine": "m"},
        # inherited types: the resolved type counts
        "sg_base": {"type": "stategraph", "enabled": True},
        "sg_child": {"type": "sg_base", "enabled": True},
        "child_author": {"type": "basic_agent", "enabled": True,
                         "agent_config": {"tools": {"allowed": ["sg_child/*"]}}},
        "fac_child": {"type": "story_machine", "enabled": True},
        # reach through a SAM the agent may call, all the way down
        "author_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["author"]},
        "star_sam": {"type": "sub_agent_manager", "enabled": True},
        "chain_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["spawner"]},
        "spawner": {"type": "basic_agent", "enabled": True,
                    "agent_config": {"tools": {"allowed": ["author_sam/*"]}}},
        "star_spawner": {"type": "basic_agent", "enabled": True,
                         "agent_config": {"tools": {"allowed": ["star_sam/*"]}}},
        "chain_spawner": {"type": "basic_agent", "enabled": True,
                          "agent_config": {"tools": {"allowed": ["chain_sam/*"]}}},
        "writer_spawner": {"type": "basic_agent", "enabled": True,
                           "agent_config": {"tools": {"allowed": ["some_sam/*"]}}},
        # a cycle: loop_a and loop_b can start each other; loop_a can also start the author
        "aaa_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["loop_b"]},
        "zzz_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["loop_a"]},
        "loop_a": {"type": "basic_agent", "enabled": True,
                   "agent_config": {"tools": {"allowed": ["aaa_sam/*", "author_sam/*"]}}},
        "loop_b": {"type": "basic_agent", "enabled": True, "agent_config": {"tools": {"allowed": ["zzz_sam/*"]}}},
    }}})


def test_sg007_production_check_refuses_stategraphs_own_tools():
    """Even when the runner's allowlist names stategraph, a machine may not call its tools (§8.3)."""
    check = make_config_check(_system_config(), runner="stategraph_runner", own_instance="stategraph")
    tree = validate(machine("""\
        a:
          do: {tool: stategraph_run_machine, args: {machine_id: x}}
          transitions: [{target: b}]
        b:
          do: {tool: sg_copy_save_machine}
          transitions: [{target: c}]
        c:
          do: {tool: json_store_read, args: {key: k}}
          transitions: [{target: done}]
        done: {type: final}
        """), config_check=check)
    by_path = {p.path: p.message for p in found(tree, "SG007")}
    assert "belongs to stategraph itself" in by_path["states.a.do.tool"]
    assert "belongs to stategraph itself" in by_path["states.b.do.tool"], "another stategraph instance counts too"
    assert "states.c.do.tool" not in by_path


def test_sg007_production_check_runner_allowlist_agents_and_profile():
    """Any configured agent may be named -- no SAM list (critic is in none); an unknown one is refused."""
    check = make_config_check(_system_config(), runner="stategraph_runner", own_instance="stategraph")
    tree = validate(machine("""\
        a:
          do: {tool: web_search_search, args: {q: x}}
          transitions: [{target: b}]
        b:
          do: {agent: "{{ params.who }}", task: t}
          transitions: [{target: c}]
        c:
          do: {decide: noul, question: q, input: x, profile: jev}
          transitions: [{target: done}]
        done: {type: final}
        """, head="params: {who: {type: string, enum: [writer, critic, ghost], default: writer}}\n"),
        config_check=check)
    text = messages(tree, "SG007")
    assert "not in stategraph_runner's tool allowlist" in text
    assert "agent 'ghost' is not configured or not enabled" in text
    assert "'writer'" not in text and "'critic'" not in text
    assert "decision profile 'jev' is not configured" in text


@pytest.mark.parametrize("agent,refused", [
    ("author", "may call stategraph_run_machine: a machine may not save, run or control machines"),
    ("copy_author", "may call sg_copy_"),
    ("stategraph_runner", "is the runner"),
    ("story_machine", "use it as a submachine"),
    ("json_store", "is not an agent"),
    ("reader", None),
    ("blocked_author", None),
    ("writer", None),
    ("child_author", "may call sg_child_"),
    ("fac_child", "use it as a submachine"),
    ("spawner", "can start 'author' through author_sam"),
    ("star_spawner", "through star_sam"),
    ("chain_spawner", "can start 'spawner' through chain_sam -- agent 'spawner' can start 'author'"),
    ("writer_spawner", None),
])
def test_sg007_refuses_agents_that_reach_machines(agent, refused):
    """A machine may not save, start or control machines -- through an agent neither (§8.3): not the runner,
    not a machine facade, not an agent whose allowlist reaches stategraph's non-read-only tools, not an agent
    that can start one of those through a SAM (followed all the way down). Types count as resolved."""
    check = make_config_check(_system_config(), runner="stategraph_runner", own_instance="stategraph",
                              is_agent=lambda name: name != "json_store")
    tree = validate(machine(f"""\
        a:
          do: {{agent: {agent}, task: t}}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """), config_check=check)

    if refused is None:
        assert found(tree, "SG007") == [], messages(tree, "SG007")
    else:
        assert refused in messages(tree, "SG007")


def test_sg007_follows_sams_through_a_cycle():
    """loop_b can start loop_a, which can start the author: both are refused, whichever is asked first."""
    for order in (["loop_a", "loop_b"], ["loop_b", "loop_a"]):
        check = make_config_check(_system_config(), runner="stategraph_runner", own_instance="stategraph")
        answers = {name: check("agent", name, {}) for name in order}
        assert "can start 'author' through author_sam" in (answers["loop_a"] or ""), order
        assert "can start 'loop_a' through zzz_sam" in (answers["loop_b"] or ""), order


def test_sg007_vars_from_may_name_any_configured_agent():
    """vars_from only reads template vars: the runner may be named there, a missing agent may not."""
    check = make_config_check(_system_config(), runner="stategraph_runner", own_instance="stategraph")
    body = """\
        a:
          do: {agent: writer, task: t}
          transitions: [{target: done}]
        done: {type: final}
        """
    assert found(validate(machine(body, head="vars_from: stategraph_runner\n"), config_check=check), "SG007") == []
    assert found(validate(machine(body, head="vars_from: ghost\n"), config_check=check), "SG007")


def test_sg007_agent_activities_need_nothing_in_the_runner_s_allowlist():
    """The backend runs the agent itself: the runner's tool allowlist bounds tool activities only."""
    check = make_config_check(_system_config(runner_allows=("json_store",)), runner="stategraph_runner",
                              own_instance="stategraph")
    tree = validate(machine("""\
        a:
          do: {agent: writer, task: t}
          transitions: [{target: done}]
        done: {type: final}
        """), config_check=check)

    assert found(tree, "SG007") == [], messages(tree, "SG007")


def test_sg007_a_tool_activity_may_not_call_the_sam_s_tool():
    """Even when the runner's allowlist names a SAM, a tool activity (or sg.tool(), checked by the same
    function at run time) may not call it: its sub-agents would not be journaled and would outlive the run."""
    check = make_config_check(_system_config(), runner="stategraph_runner", own_instance="stategraph")
    tree = validate(machine("""\
        a:
          do: {tool: some_sam_manage_sub_agent, args: {action: create}}
          transitions: [{target: done}]
        done: {type: final}
        """), config_check=check)

    assert "tool 'some_sam_manage_sub_agent' belongs to the SAM some_sam" in messages(tree, "SG007")


# ------------------------------------------------------------------ warnings

def test_warnings_do_not_block():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions: [{target: done}]
        orphan:
          do: {agent: w, task: t}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert tree.ok
    assert [p.code for p in tree.problems] == ["SG101"]


def test_sg101_unreachable_state():
    tree = validate(machine("""\
        a:
          transitions: [{target: done}]
        orphan:
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert ("SG101", "states.orphan") in pairs(tree)
    assert ("SG101", "states.a") not in pairs(tree)


def test_sg101_composite_transitions_count_for_nested_states():
    """An active composite's transitions leave from its nested states: 'after' is reachable."""
    tree = validate(machine("""\
        c:
          initial: w
          states:
            w:
              transitions:
                - trigger: go
                  target: fin
            fin: {type: final}
          transitions:
            - trigger: error
              target: after
            - target: done
        after:
          transitions: [{target: done}]
        done: {type: final}
        """, initial="c", head="events: {go: {}}\n"))
    assert found(tree, "SG101") == []


def test_sg102_no_path_to_a_root_final():
    tree = validate(machine("""\
        a:
          max_visits: 3
          do: {agent: w, task: t}
          transitions: [{target: b}]
        b:
          do: {agent: w, task: t}
          transitions: [{target: a}]
        done: {type: final}
        """))
    assert ("SG102", "states.a") in pairs(tree)
    assert ("SG102", "states.b") in pairs(tree)


def test_sg103_loop_without_max_visits():
    loop = """\
        a:
          {limit}do: {{agent: w, task: t}}
          transitions:
            - target: b
        b:
          do: {{agent: w, task: t}}
          transitions:
            - target: a
              guard: out == 'again'
            - target: done
              guard: else
        done: {{type: final}}
        """
    unbounded = validate(machine(loop.format(limit="")))
    assert "a, b" in messages(unbounded, "SG103")
    bounded = validate(machine(loop.format(limit="max_visits: 3\n  ")))
    assert found(bounded, "SG103") == []


def test_sg104_transition_after_an_unguarded_one():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - target: done
            - target: other
              guard: out == 1
        other: {type: final}
        done: {type: final}
        """))
    assert ("SG104", "states.a.transitions[1]") in pairs(tree)


def test_sg105_undeclared_context_read():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t}
          transitions:
            - target: done
              guard: ctx.missing > 1 and ctx.assigned
              effect: ctx.assigned = out
            - target: done
              guard: else
        done: {type: final}
        """))
    assert "ctx.missing" in messages(tree, "SG105")
    assert "ctx.assigned" not in messages(tree, "SG105")


@pytest.mark.parametrize("code", [
    "random.random() > 0.5",
    "time.time() > 0",
    "datetime.now().year > 2000",
    "uuid.uuid4() is not None",
    "set([1, 2]) is not None",
    "hash('x') > 0",
    "open('f') is not None",
    "os.getenv('HOME') is not None",
    "os.environ['HOME'] != ''",
])
def test_sg106_impure_code(code):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          transitions:
            - target: done
              guard: "{code}"
            - target: done
              guard: else
        done: {{type: final}}
        """, python="import random\nimport time\nimport uuid\nimport os\nfrom datetime import datetime\n"))
    assert errors(tree) == [], tree.problems
    assert ("SG106", "states.a.transitions[0].guard") in pairs(tree)


def test_sg107_bare_reference():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: ctx.draft}
          transitions: [{target: done}]
        done: {type: final, output: "{{ ctx.draft }}"}
        """, head="context: {draft: null}\n"))
    assert ("SG107", "states.a.do.task") in pairs(tree)
    assert ("SG107", "states.done.output") not in pairs(tree)


def test_sg108_submachine_with_run_timeout():
    sub = SUB.replace("initial: a\n", "limits: {timeout: 10m}\ninitial: a\n")
    tree = validate(machine("""\
        a:
          do: {machine: sub, params: {text: x}}
          transitions: [{target: done}]
        done: {type: final}
        """, head="imports: {sub: ./sub.yaml}\n", **{"sub.yaml": sub}))
    [problem] = found(tree, "SG108")
    assert problem.file == "sub.yaml"
    root_alone = validate({"m.yaml": sub.replace("id: sub", "id: m")})
    assert found(root_alone, "SG108") == [], "a root machine's run timeout is honoured, no warning"



@pytest.mark.parametrize("notes, says", [("  Why: x\n", "note name"), ("  why: [x]\n", "notes")])
def test_notes_are_names_to_texts(notes, says):
    text = "stategraph: 1\nid: m\nnotes:\n" + notes + "initial: a\nstates:\n  a: {type: final}\n"
    assert any(says in p.message or says in (p.path or "") for p in errors(validate({"m.yaml": text}))), \
        [p.as_dict() for p in validate({"m.yaml": text}).problems]
    assert not errors(validate({"m.yaml": text.replace(notes, "  why: free text\n")}))
