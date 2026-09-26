"""Regressions for the format, security and panel findings of the second review round.

One test (or one parametrised group) per fixed finding. Validator findings run
YAML text through the real loader and ``validate_tree`` (SG007 with the
production ``make_config_check`` over a real ``AgentSystemConfig``); runtime
findings run the real RunManager, and where the finding sits in the production
backend or server, the real ``ScarabHiveBackend`` / ``StateGraphServer`` with
only the runner agent replaced (``FakeRunner``: a real ``SessionTracker``,
recorded SAM and tool calls).

Mutation checks run (each turned the named tests red, then was restored byte-exactly):
- validate._check_graph: the composite -> initial loop edge removed               -> test_sg103_loop_through_a_composite_initial[unbounded, critique]
- validate._check_graph: max_visits counted inside a re-entered composite         -> test_sg103_loop_through_a_composite_initial[critique]
- backend._set_vars: the session's vars not cleared before the call              -> test_session_vars_hold_exactly_this_calls_vars
- validate._check_references: the computed-kind-value SG005 block removed         -> test_computed_kind_value_is_sg005[all four]
- backend.call_tool: the run-time tool_check removed                             -> test_call_tool_refuses_a_stategraph_tool_at_run_time[stategraph_tool]
- loader._load: the id == file name check removed                                 -> test_the_id_must_match_the_file_name
- validate._validate_file: the vars_from config check removed                    -> test_vars_from_an_unconfigured_agent_is_sg007
- interpreter._init_vars: the ActivityError -> MachineFailed(config) wrap removed  -> test_vars_from_an_unconfigured_agent_fails_as_config_at_run_time
- validate._check_concurrent_vars: the map branch removed                        -> test_sg109_for_concurrent_map_items_with_vars
- validate._sets_vars: submachines never count                                     -> test_sg109_for_parallel_submachines_whose_machine_sets_vars
- validate._check_transitions: pseudostate code bound with BINDINGS["any"] again  -> test_out_in_an_initial_choice_is_sg004
- validate._pseudostate_bindings: the initial pseudostates' "state" way dropped   -> test_out_in_an_initial_choice_is_sg004
- interpreter.ERROR_DEFAULTS: branch/index removed                               -> test_a_guard_on_error_branch_falls_through_for_a_branchless_error
- activity.execute: mock meta without attempts/duration_s                         -> test_mocked_activity_meta_reads_like_a_live_one
- code._misuse: the set-of-a-set rule removed                                     -> test_braces_inside_a_code_field_are_sg004[effect, guard]
- code._misuse: the out rule / dict-method rule / nested-attribute rule removed   -> test_plain_data_attribute_slips_are_sg004[its case]
- code._misuse: the nested-attribute rule without ``id(node) not in called``      -> test_plain_data_attribute_slips_are_sg004[method_on_a_value]
- builtin.ToolSpec.error_if: description back to "type tool_error"                -> test_error_if_description_names_the_type_the_run_raises
- builtin.ToolKind.run: error_if raising another type than the description names -> test_error_if_description_names_the_type_the_run_raises
- builtin.QuestionSpec: based on KindSpec again                                   -> test_per_question_retry_or_timeout_is_sg005[timeout, retry]
- validate._check_template: bare references linted only without any template     -> test_sg107_bare_reference_next_to_a_templated_sibling
- validate._check_activity: the SG110 block removed                               -> test_retry_errors_with_an_unknown_type_is_sg110
- spec.RetrySpec: an ``on`` field accepted again                                  -> test_retry_on_is_refused_errors_is_the_key
- spec.ParamSpec: ``_default_fits`` returns at once                               -> test_param_default_must_fit_type_and_enum[the three refusals]
- runner.mock_for: the use count restarts at 1 (the frame-local visit)            -> test_mock_visits_advance_across_calls_of_a_submachine
- runner._load_journal: mocked rows not counted back into mock_uses              -> test_mock_visits_survive_a_resume
- interpreter._enter_pseudo: no exit before leaving the composite                 -> test_initial_choice_routing_out_exits_the_composite_first
- validate._check_transitions: ``and not through_junction`` removed               -> test_no_sg104_for_a_fallback_after_a_junction_without_else
- builtin.MapSpec._as: the scope-name/index refusal removed                       -> test_map_as_may_not_shadow_a_scope_name[all three]
- server.run_machine: ``attached is False`` no longer skips the wait              -> test_run_machine_on_a_run_another_process_owns_returns_at_once
- runner.RunManager.wait: the 0.5 s pause for a foreign run -> 0                   -> test_wait_on_a_foreign_run_does_not_spin_on_the_store
- backend.call_tool: the redaction right after dispatch removed                  -> test_an_echoed_secret_reaches_no_journal_row
- store.FileSources.read: the inside-roots check removed                          -> test_python_and_imports_outside_the_roots_are_refused
- store.FileSources.resolve: the inside-roots check removed (the read check still keeps the text out, but the
  problem loses its imports.x location and says "not found" only for an outside file that exists)
                                                                                  -> test_python_and_imports_outside_the_roots_are_refused
- runner._execute: current_request_id not set                                     -> test_inside_a_run_the_request_id_is_the_run_id
- server: on_finish no longer releases the cancellation token                     -> test_the_cancellation_token_is_released_when_the_run_ends
- backend.make_config_check: the sub_agent_manager type check removed             -> test_a_sam_that_is_no_sub_agent_manager_is_sg007[machine, activity]
- debugger._step: the non-container raise / the list-range check removed          -> test_set_through_a_non_container_is_a_409_and_leaves_ctx[its case]
- service.create_machine: the raw title formatted in again                        -> test_create_machine_title_reads_back_exactly[Review: ..., #1 loop]
- service.create_machine: the control-character refusal removed                   -> test_create_machine_title_with_a_newline_is_refused
"""

from __future__ import annotations

import asyncio
import json
import re
import textwrap
import time
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from plugins.stategraph.engine.backend import ScarabHiveBackend, make_config_check
from plugins.stategraph.kinds import describe_kinds
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, errors, found, held,
                                                         load, runnable, settle, tool_config, until, utc_at,
                                                         validate)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def machine(states: str, *, initial: str = "a", head: str = "", python: Optional[str] = None,
            **others: str) -> dict[str, str]:
    """A machine ``m`` with the given states block (optional top-level keys, companion module, other files)."""
    text = ("stategraph: 1\nid: m\n" + ("python: m.py\n" if python is not None else "") + textwrap.dedent(head)
            + f"initial: {initial}\nstates:\n" + textwrap.indent(textwrap.dedent(states), "  "))
    files = {"m.yaml": text}
    if python is not None:
        files["m.py"] = textwrap.dedent(python)
    files.update({name.replace("__", "."): textwrap.dedent(text) for name, text in others.items()})
    return files


def codes(tree, code: str) -> list[str]:
    return [p.path for p in found(tree, code)]


def system_config(**extra_servers: Any):
    from agent_system.config.models import AgentSystemConfig

    servers = {
        "stategraph": {"type": "stategraph", "enabled": True},
        "stategraph_runner": {"type": "basic_agent", "enabled": True,
                              "agent_config": {"tools": {"allowed": ["stategraph", "json_store", "stategraph_sam"]}}},
        "stategraph_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["writer"]},
        "writer": {"type": "basic_agent", "enabled": True},
        "json_store": {"type": "json_store", "enabled": True},
    }
    servers.update(extra_servers)
    return AgentSystemConfig.model_validate({"plugins": {"servers": servers}})


def config_check(config=None):
    return make_config_check(config or system_config(), runner="stategraph_runner", default_sam="stategraph_sam",
                             own_instance="stategraph")


class FakeRunner:
    """The runner agent, the one seam replaced: a REAL SessionTracker, SAM and tool calls recorded.

    ``call_tool`` is what AgentCaller calls for ``<sam>_manage_sub_agent``; it
    records the session's template vars at that moment -- what the spawned
    sub-agent would inherit. ``dispatch_tool_call`` answers tool activities.
    """

    def __init__(self, tools: Optional[dict[str, Any]] = None):
        from agent_system.servers.agent.components.session_tracking import SessionTracker

        self._session_tracker = SessionTracker()
        self.tools = dict(tools or {})
        self.sam_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []

    async def call_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        seen = dict(self._session_tracker.get_session_template_vars(params.get("_session_id")))
        self.sam_calls.append({"tool": name, "task": params.get("task"), "vars": seen})
        return {"status": "success", "result": f"answer to {params.get('task')}", "instance_id": "inst-1"}

    async def dispatch_tool_call(self, tool: str, args: dict[str, Any], *, session_id=None, user_id=None,
                                 request_id=None) -> Any:
        self.tool_calls.append({"tool": tool, "args": dict(args)})
        answer = self.tools.get(tool, {"status": "error", "error": f"no such tool {tool}"})
        return answer(args) if callable(answer) else answer


def backend_for(runner: FakeRunner, config=None, **options: Any):
    def make(run_id: str) -> ScarabHiveBackend:
        return ScarabHiveBackend(runner=runner, system_config=config, session_id=f"sg_{run_id}", user_id=None,
                                 default_sam="stategraph_sam", **options)
    return make


def _stop_cancellation_monitor() -> None:
    from agent_system.core.cancellation import get_cancellation_manager

    task = get_cancellation_manager()._monitor_task
    if task is not None:
        task.cancel()


# ================================================================== review:format

# ------------------------------------------------------------------ F2: SG103 through a composite's initial

REVIEW_LOOP = """\
write:
  #write
  do: {agent: writer, task: draft}
  transitions: [{target: review}]
review:
  #review
  initial: critique
  states:
    critique:
      #critique
      do: {agent: critic, task: judge}
      transitions:
        - target: write
          guard: out == "again"
        - target: fin
          guard: else
    fin: {type: final}
  transitions:
    - target: done
done: {type: final}
"""


@pytest.mark.parametrize("bounded,warned", [
    (None, True), ("critique", True), ("review", False), ("write", False),
], ids=["unbounded", "critique", "review", "write"])
def test_sg103_loop_through_a_composite_initial(bounded, warned):
    """F2: write -> review(initial critique) -> write is a loop; critique's max_visits resets on every entry."""
    text = REVIEW_LOOP
    if bounded:
        text = text.replace(f"#{bounded}", "max_visits: 3")
    tree = validate(machine(text, initial="write"))
    assert not errors(tree), [p.as_dict() for p in errors(tree)]
    assert bool(found(tree, "SG103")) is warned, [p.message for p in found(tree, "SG103")]


# ------------------------------------------------------------------ F3: session vars per call

async def test_session_vars_hold_exactly_this_calls_vars(harness):
    """F3: each agent call's session holds its own vars only -- a later call without vars sees none."""
    runner = FakeRunner()
    row = await harness.run(machine("""\
        a:
          do: {agent: writer, task: first, vars: {phase: synopsis, strict: true}}
          transitions: [{target: b}]
        b:
          do: {agent: writer, task: second}
          transitions: [{target: c}]
        c:
          do: {agent: writer, task: third, vars: {genre: thriller}}
          transitions: [{target: done}]
        done: {type: final}
        """), backend_factory=backend_for(runner))
    assert row["status"] == "succeeded", row["error"]
    assert [call["vars"] for call in runner.sam_calls] == [
        {"phase": "synopsis", "strict": True}, {}, {"genre": "thriller"}]


# ------------------------------------------------------------------ F4: computed kind values

@pytest.mark.parametrize("kind,value", [
    ("agent", '"{{ ctx.agent_name }}"'),
    ("agent", '"chat_{{ params.who }}"'),
    ("tool", '"{{ ctx.tool_name }}"'),
    ("tool", "\"{{ 'stategraph_' + 'run_machine' }}\""),
], ids=["agent_ctx", "agent_mixed", "tool_ctx", "tool_expression"])
def test_computed_kind_value_is_sg005(kind, value):
    """F4: a kind value is a literal or {{ params.<name> }} with an enum -- nothing else passes the validator."""
    do = f"{{agent: {value}, task: t}}" if kind == "agent" else f"{{tool: {value}}}"
    tree = validate(machine(f"""\
        a:
          do: {do}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """, head="""\
        params: {who: {type: string, enum: [writer], default: writer}}
        context: {agent_name: writer, tool_name: json_store_read}
        """), config_check=config_check())
    assert f"states.a.do.{kind}" in codes(tree, "SG005"), [p.as_dict() for p in tree.problems]


def test_a_param_kind_value_with_an_enum_is_accepted():
    tree = validate(machine("""\
        a:
          do: {agent: "{{ params.who }}", task: t}
          transitions: [{target: done}]
        done: {type: final}
        """, head="params: {who: {type: string, enum: [writer], default: writer}}\n"), config_check=config_check())
    assert not errors(tree), [p.as_dict() for p in errors(tree)]


COMPUTED_TOOL = """\
stategraph: 1
id: m
context: {name: NAME, err: null}
initial: a
states:
  a:
    do: {tool: "{{ ctx.name }}", args: {key: k}}
    transitions:
      - target: done
      - trigger: error
        target: denied
        effect: ctx.err = error.type
  done: {type: final, output: done}
  denied: {type: final, output: "{{ ctx.err }}"}
"""


@pytest.mark.parametrize("name,calls,output", [
    ("stategraph_run_machine", 0, "tool_denied"),
    ("json_store_read", 1, "done"),
], ids=["stategraph_tool", "allowed_tool"])
async def test_call_tool_refuses_a_stategraph_tool_at_run_time(tmp_path, name, calls, output):
    """F4: a name that never met the validator (a force-saved machine) is checked again by the backend the
    service builds -- stategraph's own tools stay out of reach."""
    server = StateGraphServer("stategraph", system_config(), tool_config(tmp_path))
    runner = FakeRunner(tools={"json_store_read": {"value": 1}, "stategraph_run_machine": {"run_id": "x"}})
    server._registry = SimpleNamespace(get=lambda agent: runner)
    try:
        tree = load({"m.yaml": COMPUTED_TOOL.replace("NAME", name)}, execute=True)  # not validated, as forced
        run_id = await server.run_manager.start(tree, backend_factory=server.service.backend_factory(None))
        row = await settle(server.run_manager, run_id)
    finally:
        await server.stop_plugin()
        _stop_cancellation_monitor()
    assert len(runner.tool_calls) == calls, runner.tool_calls
    assert row["output"] == output, row


# ------------------------------------------------------------------ F5: id and file name

def test_the_id_must_match_the_file_name():
    text = "stategraph: 1\nid: critique_round\ninitial: a\nstates:\n  a: {type: final}\n"
    wrong = load({"critique_strict.yaml": text}, root="critique_strict.yaml")
    assert [(p.code, p.path) for p in errors(wrong)] == [("SG001", "id")]
    right = load({"critique_round.yaml": text}, root="critique_round.yaml")
    assert not errors(right)


# ------------------------------------------------------------------ F6: vars_from

def test_vars_from_an_unconfigured_agent_is_sg007():
    typo = validate(machine("""\
        a: {type: final}
        """, head="vars_from: v6_story_cordinator\n"), config_check=config_check())
    assert codes(typo, "SG007") == ["vars_from"]
    configured = validate(machine("a: {type: final}\n", head="vars_from: writer\n"), config_check=config_check())
    assert not errors(configured)


async def test_vars_from_an_unconfigured_agent_fails_as_config_at_run_time(harness):
    """F6: the live lookup refuses the agent; the run fails with the documented type config, not internal."""
    row = await harness.run(machine("""\
        a:
          do: {agent: writer, task: t}
          transitions: [{target: done}]
        done: {type: final}
        """, head="vars_from: v6_story_cordinator\n"), backend_factory=backend_for(FakeRunner(), system_config()))
    assert row["status"] == "failed"
    assert row["error"]["type"] == "config", row["error"]


# ------------------------------------------------------------------ F7: SG109 beyond direct parallel agents

def test_sg109_for_concurrent_map_items_with_vars():
    concurrent = validate(machine("""\
        a:
          do: {map: "[1, 2, 3]", concurrency: 3, each: {agent: w, task: t, vars: {chapter: "{{ item }}"}}}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert codes(concurrent, "SG109") == ["states.a.do.each"]
    in_order = validate(machine("""\
        a:
          do: {map: "[1, 2, 3]", each: {agent: w, task: t, vars: {chapter: "{{ item }}"}}}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert found(in_order, "SG109") == [], "concurrency 1 runs one item at a time: no race"


RITUAL = """\
stategraph: 1
id: ritual
params: {phase: {type: string, required: true}}
vars: {phase: "{{ params.phase }}"}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: fin}]
  fin: {type: final}
"""


def test_sg109_for_parallel_submachines_whose_machine_sets_vars():
    tree = validate(machine("""\
        a:
          do:
            parallel:
              one: {machine: ritual, params: {phase: synopsis}}
              two: {machine: ritual, params: {phase: outline}}
          transitions: [{target: done}]
        done: {type: final}
        """, head="imports: {ritual: ./ritual.yaml}\n", ritual__yaml=RITUAL))
    assert codes(tree, "SG109") == ["states.a.do.parallel"]


# ------------------------------------------------------------------ F8: pseudostate bindings

def test_out_in_an_initial_choice_is_sg004():
    """F8: route is entered at start (no out) and after a (out bound): only the intersection is bound."""
    tree = validate(machine("""\
        route:
          type: choice
          transitions:
            - target: a
              guard: out["value"] > 0.5
            - target: done
              guard: else
        a:
          max_visits: 3
          do: {agent: w, task: t}
          transitions: [{target: route}]
        done: {type: final}
        """, initial="route"))
    sg004 = [p for p in found(tree, "SG004") if p.path == "states.route.transitions[0].guard"]
    assert sg004 and "'out' is not bound" in sg004[0].message, [p.as_dict() for p in tree.problems]


# ------------------------------------------------------------------ F9: error.branch / error.index

async def test_a_guard_on_error_branch_falls_through_for_a_branchless_error(harness):
    row = await harness.run(machine("""\
        a:
          do: {call: boom}
          transitions:
            - target: done
            - trigger: error
              guard: error.branch == "facts"
              target: facts
            - trigger: error
              target: caught
              effect: ctx.by = [error.branch, error.index]
        done: {type: final}
        facts: {type: final, output: facts}
        caught: {type: final, output: "{{ ctx.by }}"}
        """, head="context: {by: null}\n", python="def boom():\n    raise ValueError('boom')\n"))
    assert row["status"] == "succeeded", row["error"]
    assert (row["final_state"], row["output"]) == ("caught", [None, None])


# ------------------------------------------------------------------ F10: mocked activity meta

async def test_mocked_activity_meta_reads_like_a_live_one(harness):
    row = await harness.run(machine("""\
        a:
          do: {agent: w, task: go}
          transitions:
            - target: done
              effect: ctx.meta = [activity.attempts, activity.duration_s]
        done: {type: final, output: "{{ ctx.meta }}"}
        """, head="context: {meta: null}\n"), mocks={"a": "M"}, mock_only=True)
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == [1, 0.0]


# ------------------------------------------------------------------ F11 / F12: code-field slips

@pytest.mark.parametrize("field,code", [
    ("effect", "ctx.x = {{ out }}"),
    ("guard", "{{ out }} != None"),
], ids=["effect", "guard"])
def test_braces_inside_a_code_field_are_sg004(field, code):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          transitions:
            - target: done
              {field}: "{code}"
        done: {{type: final}}
        """, head="context: {x: null}\n"))
    assert f"states.a.transitions[0].{field}" in codes(tree, "SG004"), [p.as_dict() for p in tree.problems]


@pytest.mark.parametrize("guard,refused", [
    ("out.value >= 0.7", True),
    ("ctx.get('x') == 1", True),
    ("ctx.a.b == 1", True),
    ("ctx.a.upper() == 'X'", False),
], ids=["out_attribute", "ctx_dict_method", "ctx_nested_attribute", "method_on_a_value"])
def test_plain_data_attribute_slips_are_sg004(guard, refused):
    tree = validate(machine(f"""\
        a:
          do: {{agent: w, task: t}}
          transitions:
            - target: done
              guard: "{guard}"
            - target: done
              guard: else
        done: {{type: final}}
        """, head="context: {a: {b: 1}, x: 1}\n"))
    assert ("states.a.transitions[0].guard" in codes(tree, "SG004")) is refused, [p.as_dict() for p in tree.problems]


# ------------------------------------------------------------------ F13: error_if names the type it raises

async def test_error_if_description_names_the_type_the_run_raises(harness):
    """F13: the catalog's description of error_if and the run agree on the error type (no wording pinned)."""
    row = await harness.run(machine("""\
        a:
          do: {tool: json_store_read, error_if: "out['ok'] is False"}
          transitions:
            - target: done
            - trigger: error
              target: caught
              effect: ctx.type = error.type
        done: {type: final}
        caught: {type: final, output: "{{ ctx.type }}"}
        """, head="context: {type: null}\n"), backend=FakeBackend({"a": {"ok": False}}))
    raised = row["output"]
    assert row["final_state"] == "caught" and raised, row

    tool = next(kind for kind in describe_kinds() if kind["key"] == "tool")
    described = tool["schema"]["properties"]["error_if"]["description"]
    assert set(re.findall(r"type (\w+)", described)) == {raised}, described


# ------------------------------------------------------------------ F14: decide questions

@pytest.mark.parametrize("extra", ["timeout: 5s", "retry: {attempts: 3}"], ids=["timeout", "retry"])
def test_per_question_retry_or_timeout_is_sg005(extra):
    key = extra.split(":", 1)[0]
    tree = validate(machine(f"""\
        a:
          do:
            decide: questions
            input: text
            questions:
              romance: {{type: noul, question: "romance?", {extra}}}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """))
    assert f"states.a.do.questions.romance.{key}" in codes(tree, "SG005"), [p.as_dict() for p in tree.problems]


# ------------------------------------------------------------------ F15: SG107 per leaf

def test_sg107_bare_reference_next_to_a_templated_sibling():
    tree = validate(machine("""\
        a:
          do: {tool: json_store_write, args: {doc: "{{ ctx.x }}", data: ctx.y}}
          transitions: [{target: done}]
        done: {type: final}
        """, head="context: {x: 1, y: 2}\n"))
    assert codes(tree, "SG107") == ["states.a.do.args.data"]


# ------------------------------------------------------------------ F16: retry.errors

def test_retry_errors_with_an_unknown_type_is_sg110():
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t, retry: {attempts: 3, errors: [agent_failed, time_out]}}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert not errors(tree)
    warned = found(tree, "SG110")
    assert [p.level for p in warned] == ["warning"] and "time_out" in warned[0].message
    assert "agent_failed" not in warned[0].message.split("(known")[0]


def test_retry_on_is_refused_errors_is_the_key():
    """F16: YAML 1.1 readers turn an ``on`` key into True -- the format has no such key."""
    tree = validate(machine("""\
        a:
          do: {agent: w, task: t, retry: {attempts: 3, "on": [timeout]}}
          transitions: [{target: done}]
        done: {type: final}
        """))
    assert "states.a.do.retry.on" in codes(tree, "SG005"), [p.as_dict() for p in tree.problems]


# ------------------------------------------------------------------ F17: param defaults

@pytest.mark.parametrize("param,refused", [
    ("{type: integer, default: '3'}", True),
    ("{enum: [formal, casual], default: friendly}", True),
    ("{type: integer, default: true}", True),
    ("{type: number, default: 2}", False),
    ("{type: string, enum: [formal, casual], default: casual}", False),
], ids=["string_for_integer", "outside_the_enum", "boolean_for_integer", "int_for_number", "enum_member"])
def test_param_default_must_fit_type_and_enum(param, refused):
    tree = load(machine("a: {type: final}\n", head=f"params: {{p: {param}}}\n"))
    sg001 = [p for p in errors(tree) if p.code == "SG001" and p.path.startswith("params.p")]
    assert bool(sg001) is refused, [p.as_dict() for p in tree.problems]


# ------------------------------------------------------------------ F18: $visits across submachine calls

LOOPED_SUB = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {n: 0, seen: []}
initial: r
states:
  r:
    do: {machine: sub}
    transitions:
      - target: g
        effect: ctx.seen = ctx.seen + [out]
  g:
    max_visits: 5
    do: {agent: judge, task: "round {{ ctx.n }}"}
    transitions:
      - target: r
        guard: ctx.n < 2
        effect: ctx.n += 1
      - target: done
        guard: else
  done: {type: final, output: "{{ ctx.seen }}"}
"""
MOCKED_SUB = """\
stategraph: 1
id: sub
context: {o: null}
initial: w
states:
  w:
    do: {agent: writer, task: t}
    transitions:
      - target: fin
        effect: ctx.o = out
  fin: {type: final, output: "{{ ctx.o }}"}
"""
VISITS = {"r/w": {"$visits": ["first", "second", "third"]}}


async def test_mock_visits_advance_across_calls_of_a_submachine(harness):
    row = await harness.run({"m.yaml": LOOPED_SUB, "sub.yaml": MOCKED_SUB}, mocks=VISITS,
                            backend=FakeBackend({"g": "ok"}))
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == ["first", "second", "third"]


async def test_mock_visits_survive_a_resume(harness):
    files = {"m.yaml": LOOPED_SUB, "sub.yaml": MOCKED_SUB}
    first = FakeBackend({"g": lambda call: "ok" if call["task"] == "round 0" else held(asyncio.Event())(call)})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), mocks=VISITS, backend=first)
    await until(lambda: first.count("g") == 2, what="round 2's judge in flight")
    await manager.shutdown()

    resumed = harness.manager()
    await resumed.resume(run_id, backend=FakeBackend({"g": "ok"}))
    row = await settle(resumed, run_id)
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == ["first", "second", "third"]


# ------------------------------------------------------------------ F19: initial choice leaving its composite

async def test_initial_choice_routing_out_exits_the_composite_first(harness):
    row = await harness.run(machine("""\
        review:
          entry: ctx.log = ctx.log + ["enter review"]
          exit: ctx.log = ctx.log + ["exit review"]
          initial: need_review
          states:
            need_review:
              type: choice
              transitions:
                - target: reviewing
                  guard: ctx.flag
                - target: skip_review
                  guard: else
            reviewing:
              transitions: [{target: fin}]
            fin: {type: final}
          transitions: [{target: done}]
        skip_review:
          entry: ctx.log = ctx.log + ["enter skip"]
          transitions: [{target: done}]
        done: {type: final, output: "{{ ctx.log }}"}
        """, initial="review", head="context: {flag: false, log: []}\n"))
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == ["enter review", "exit review", "enter skip"]


# ------------------------------------------------------------------ F20: SG104 and junctions

JUNCTION = """\
a:
  do: {agent: w, task: t}
  transitions:
    - target: j
    - target: other
j:
  type: junction
  transitions:
    - target: x
      guard: out == "x"
    #else
x: {type: final, output: x}
other: {type: final, output: other}
"""


async def test_no_sg104_for_a_fallback_after_a_junction_without_else(harness):
    without_else = machine(JUNCTION)
    assert found(validate(without_else), "SG104") == [], "the fallback is live when no junction branch holds"
    with_else = validate(machine(JUNCTION.replace("#else", "- target: x\n      guard: else")))
    assert codes(with_else, "SG104") == ["states.a.transitions[1]"]

    row = await harness.run(without_else, backend=FakeBackend({"a": "y"}))
    assert row["final_state"] == "other", "fixture: the fallback fires at run time"


# ------------------------------------------------------------------ F21: map as

@pytest.mark.parametrize("name", ["index", "ctx", "params"])
def test_map_as_may_not_shadow_a_scope_name(name):
    tree = validate(machine(f"""\
        a:
          do: {{map: "[1, 2]", as: {name}, each: {{agent: w, task: t}}}}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """))
    assert "states.a.do.as" in codes(tree, "SG005"), [p.as_dict() for p in tree.problems]


# ================================================================== review:security

WAITING = """\
stategraph: 1
id: m
events: {go: {}}
initial: w
states:
  w:
    transitions: [{trigger: go, target: done}]
  done: {type: final, output: finished}
"""


@pytest.fixture
async def server(tmp_path):
    from agent_system.config.models import AgentSystemConfig

    srv = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    (tmp_path / "machines" / "m.yaml").write_text(WAITING, encoding="utf-8")
    yield srv
    await srv.stop_plugin()
    _stop_cancellation_monitor()


def _count_reads(monkeypatch, store) -> list[str]:
    reads: list[str] = []
    real = store.get_run

    def counting(run_id):
        reads.append(run_id)
        return real(run_id)

    monkeypatch.setattr(store, "get_run", counting)
    return reads


def _foreign_run(server, run_id: str, run_key: Optional[str] = None) -> None:
    snapshot = runnable({"m.yaml": WAITING}).snapshot()
    server.run_store.create_run(run_id, "m", snapshot, params={}, mocks={"mocks": {}, "mock_only": True},
                                owner="host-a:1", lease_until=utc_at(60), status="running", run_key=run_key,
                                session_id=f"sg_{run_id}")


# ------------------------------------------------------------------ S2: waiting on a run another process owns

async def test_run_machine_on_a_run_another_process_owns_returns_at_once(server, monkeypatch):
    _foreign_run(server, "elsewhere", run_key="req-s2")
    reads = _count_reads(monkeypatch, server.run_store)
    started = time.monotonic()
    result = await server.run_machine({"machine_id": "m", "run_key": "req-s2", "mock_only": True, "max_wait": 3})
    elapsed = time.monotonic() - started

    assert result["status"] == "success", result
    assert (result["run_id"], result["attached"], result["run_status"]) == ("elsewhere", False, "running")
    assert elapsed < 1.0, f"run_machine sat {elapsed:.1f} s on a run it cannot wait for"
    assert len(reads) < 10, f"{len(reads)} store reads"


async def test_wait_on_a_foreign_run_does_not_spin_on_the_store(server, monkeypatch):
    _foreign_run(server, "elsewhere")
    reads = _count_reads(monkeypatch, server.run_store)
    row = await server.run_manager.wait("elsewhere", timeout=1.0)
    assert row["status"] == "running"
    assert len(reads) <= 6, f"{len(reads)} store reads in one second: the wait spins"


# ------------------------------------------------------------------ S4: injected secrets

SECRET = "s3cr3t-WRITE-KEY"
ECHOING = """\
a:
  do: {tool: writer_content_story, args: {title: t}}
  transitions:
    - target: b
      effect: ctx.x = out
b:
  do: {tool: writer_content_check, args: {title: t}, error_if: "out['ok'] is False"}
  transitions:
    - target: done
    - trigger: error
      target: c
      effect: ctx.e1 = [error.message, error.data]
c:
  do: {tool: writer_content_save, args: {title: t}}
  transitions:
    - target: done
    - trigger: error
      target: done
      effect: ctx.e2 = [error.type, error.message, error.data]
done: {type: final, output: {x: "{{ ctx.x }}", e1: "{{ ctx.e1 }}", e2: "{{ ctx.e2 }}"}}
"""


async def test_an_echoed_secret_reaches_no_journal_row(harness):
    """S4: a tool that echoes its injected write_key -- in its result, in an error_if result, in its error
    text -- leaves the secret in none of out, error.message, error.data, the journal or the run row."""
    runner = FakeRunner(tools={
        "writer_content_story": lambda args: {"echo": args},
        "writer_content_check": lambda args: {"ok": False, "key": args["write_key"]},
        "writer_content_save": lambda args: {"status": "error", "error": f"write refused for key {args['write_key']}"},
    })
    row = await harness.run(machine(ECHOING, head="context: {x: null, e1: null, e2: null}\n"),
                            backend_factory=backend_for(runner, inject_params={"writer_content_*": {"write_key": SECRET}}))

    assert [call["args"].get("write_key") for call in runner.tool_calls] == [SECRET] * 3, "fixture: injected"
    assert row["output"]["x"] == {"echo": {"title": "t", "write_key": "[redacted]"}}, row["output"]
    assert row["output"]["e2"][0] == "tool_failed", "fixture: the error-text path ran"
    everything = json.dumps([harness.store.get_run(row["id"]), harness.store.rows(row["id"])], default=str)
    assert SECRET not in everything


# ------------------------------------------------------------------ S6: files outside the machine roots

async def test_python_and_imports_outside_the_roots_are_refused(tmp_path, server):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("API_KEY = 'hunter2'\n", encoding="utf-8")
    (outside / "other.yaml").write_text(WAITING.replace("id: m", "id: other"), encoding="utf-8")
    (tmp_path / "machines" / "w.yaml").write_text(
        "stategraph: 1\nid: w\npython: ../outside/secret.py\nimports: {x: ../outside/other.yaml}\n"
        "initial: a\nstates:\n  a: {type: final}\n", encoding="utf-8")

    got = server.service.get_machine("w")

    assert sorted(got["files"]) == ["w.yaml"], sorted(got["files"])
    assert "hunter2" not in json.dumps(got, default=str)
    problems = {(p["code"], p["path"]) for p in got["problems"] if p["level"] == "error"}
    assert ("SG004", "python") in problems and ("SG006", "imports.x") in problems, problems


# ------------------------------------------------------------------ S7: the run's request id

async def test_inside_a_run_the_request_id_is_the_run_id(harness):
    from agent_system.tools.status import current_request_id

    token = current_request_id.set("chat_request_7f3a")  # the tool call that starts the run
    try:
        row = await harness.run(machine("""\
            a:
              do: {call: request_id}
              transitions:
                - target: done
                  effect: ctx.rid = out
            done: {type: final, output: "{{ ctx.rid }}"}
            """, head="context: {rid: null}\n", python="""\
            def request_id():
                from agent_system.tools.status import current_request_id
                return current_request_id.get()
            """))
    finally:
        current_request_id.reset(token)
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == row["id"]


# ------------------------------------------------------------------ S8: the cancellation token

async def test_the_cancellation_token_is_released_when_the_run_ends(server):
    from agent_system.core.cancellation import get_cancellation_manager

    server._registry = SimpleNamespace(get=lambda agent: FakeRunner())  # a runner: the run gets a real backend
    run_id = (await server.service.start_run("m"))["run_id"]
    await until(lambda: server.run_manager.live[run_id].ctx.status == "waiting", what="the wait")
    assert get_cancellation_manager().get_token(run_id) is not None, "fixture: the run registered a token"

    server.service.send_event(run_id, "go")
    row = await settle(server.run_manager, run_id)
    assert row["status"] == "succeeded"
    assert get_cancellation_manager().get_token(run_id) is None, "the finished run's token is still registered"


# ------------------------------------------------------------------ S9: sam must be a sub_agent_manager

@pytest.mark.parametrize("where", ["machine", "activity"])
def test_a_sam_that_is_no_sub_agent_manager_is_sg007(where):
    head = "sam: json_store\n" if where == "machine" else ""
    do = "{agent: writer, task: t}" if where == "machine" else "{agent: writer, task: t, sam: json_store}"
    tree = validate(machine(f"""\
        a:
          do: {do}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """, head=head), config_check=config_check())
    assert codes(tree, "SG007") == ["states.a.do.agent"], [p.as_dict() for p in tree.problems]
    assert "sub_agent_manager" in found(tree, "SG007")[0].message


# ================================================================== review:panel

PAUSING = """\
stategraph: 1
id: p
python: p.py
context: {draft: text, items: [1, 2]}
initial: a
states:
  a:
    do: {call: one}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.draft }}"}
"""


@pytest.mark.parametrize("path,names", [
    ("ctx.draft.title", "ctx.draft"),
    ("ctx.items.9", "ctx.items"),
], ids=["below_a_string", "list_index_out_of_range"])
async def test_set_through_a_non_container_is_a_409_and_leaves_ctx(tmp_path, server, path, names):
    """P8: the Debug tab's Set on an impossible path answers with a reason (409), not a 500."""
    (tmp_path / "machines" / "p.yaml").write_text(PAUSING, encoding="utf-8")
    (tmp_path / "machines" / "p.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    run_id = (await server.service.start_run("p", mock_only=True, breakpoints=["a"]))["run_id"]
    live = server.run_manager.live[run_id]
    await until(lambda: live.ctx.debugger.paused is not None, what="the pause at a")

    with pytest.raises(ServiceError) as refused:
        await server.service.control_run(run_id, "set", path=path, expr="'x'")
    assert refused.value.status == 409 and names in refused.value.message, refused.value.message
    assert live.root.ctx == {"draft": "text", "items": [1, 2]}

    server.run_manager.control(run_id, "continue")
    assert (await settle(server.run_manager, run_id))["output"] == "text"


@pytest.mark.parametrize("title", ["Review: first pass", "#1 loop", 'He said "yes"'])
def test_create_machine_title_reads_back_exactly(server, title):
    got = server.service.create_machine("titled", title=title)
    assert not [p for p in got["problems"] if p["level"] == "error"], got["problems"]
    assert server.machines.load("titled").root_file.spec.title == title


def test_create_machine_title_with_a_newline_is_refused(server):
    with pytest.raises(ServiceError) as refused:
        server.service.create_machine("titled", title="Loop\ninitial: nowhere")
    assert refused.value.status == 422
    assert server.machines.find("titled") is None, "a refused create wrote the file"
