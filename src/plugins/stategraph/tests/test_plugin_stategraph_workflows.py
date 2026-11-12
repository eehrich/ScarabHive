"""Dynamic workflows: a machine an agent writes and starts unsaved (service.start_workflow, workflows.py).

What each test holds the code to: the caps come from the instance's settings and are written into the run's own
text; an inline machine is self-contained and names no saved machine; it reaches no further than its author; its
end wakes only the caller that asked, and its result reaches that caller's next call once. Runs without the
``workflow`` column -- every other machine run -- meet none of it.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_system.config.models import AgentSystemConfig
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.stategraph.engine.backend import make_config_check
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.stategraph_testkit import FakeBackend, held, tool_config, until
from plugins.stategraph.workflows import (WaitingResults, WorkflowSettings, agent_places, inline_problems,
                                          settings_of, with_terms)

SETTINGS = {"default_max_output_tokens": 2000, "max_output_tokens_ceiling": 10000, "default_concurrency": 3,
            "max_concurrent_agents": 2}

SERVERS = {
    "stategraph": {"type": "stategraph", "enabled": True},
    "stategraph_runner": {"type": "basic_agent", "enabled": True,
                          "agent_config": {"tools": {"allowed": ["notes/*"]}}},
    "lead": {"type": "basic_agent", "enabled": True, "agent_config": {"tools": {"allowed": [
        "team_sam/*", "notes/notes_read", "stategraph/stategraph_run_workflow"]}}},
    "team_sam": {"type": "sub_agent_manager", "enabled": True, "allowed_agents": ["finder", "skeptic"]},
    "finder": {"type": "basic_agent", "enabled": True},
    "skeptic": {"type": "basic_agent", "enabled": True},
    "root_agent": {"type": "basic_agent", "enabled": True},
    "notes": {"type": "json_store", "enabled": True},
}

REVIEW = """\
stategraph: 1
id: quick_review
title: find, then verify
# the author's own note survives the terms written in
limits: {max_steps: 50}
context: {found: []}
initial: find
states:
  find:
    do: {agent: finder, task: "find"}
    transitions: [{target: verify, effect: 'ctx.found = [out]'}]
  verify:
    do:
      map: ctx.found
      as: item
      each: {agent: skeptic, task: "refute {{ item }}"}
    transitions: [{target: done}]
  done: {type: final, output: {found: "{{ ctx.found }}"}}
"""


def system() -> AgentSystemConfig:
    return AgentSystemConfig(plugins={"servers": SERVERS})


@pytest.fixture
async def server(tmp_path):
    srv = StateGraphServer("stategraph", system(), tool_config(tmp_path, workflows=dict(SETTINGS)))
    yield srv
    await srv.stop_plugin()


def fake_backends(server, handlers):
    """Every run of the server on a FakeBackend: the boundary behind the engine, nothing in front of it."""
    made = []

    def factory():
        def make(run_id):
            backend = FakeBackend(dict(handlers))
            made.append(backend)
            return backend
        return make
    server.service.backend_factory = factory
    return made


async def settled(server, run_id):
    live = server.run_manager.live.get(run_id)
    if live is not None:
        await asyncio.wait_for(asyncio.shield(live.task), 10)
    return server.run_store.get_run(run_id)


# ------------------------------------------------------------------ settings and terms

def test_without_the_settings_block_no_workflow_starts_and_the_answer_names_the_keys():
    with pytest.raises(ValueError) as off:
        settings_of(tool_config_like({}))
    assert "no workflows: block" in str(off.value) and "max_output_tokens_ceiling" in str(off.value)
    with pytest.raises(ValueError) as half:
        settings_of(tool_config_like({"workflows": {"default_max_output_tokens": 1}}))
    assert "lacks max_output_tokens_ceiling, default_concurrency" in str(half.value)
    with pytest.raises(ValueError) as upside_down:
        settings_of(tool_config_like({"workflows": {**SETTINGS, "default_max_output_tokens": 50000}}))
    assert "above max_output_tokens_ceiling" in str(upside_down.value)
    with pytest.raises(ValueError) as fraction:  # a token count is whole
        settings_of(tool_config_like({"workflows": {**SETTINGS, "default_max_output_tokens": 2.5}}))
    assert "whole number" in str(fraction.value)


def tool_config_like(values):
    class Config:
        pass
    config = Config()
    for key, value in values.items():
        setattr(config, key, value)
    return config


def test_the_terms_take_the_smaller_cap_and_always_set_a_concurrency():
    settings = WorkflowSettings(**SETTINGS)
    text, cap, concurrency = with_terms(
        REVIEW.replace("{max_steps: 50}", "{max_steps: 50, max_output_tokens: 4000}"), settings, 1500)
    assert (cap, concurrency) == (1500, 3)
    assert "max_output_tokens: 1500" in text and "concurrency: 3" in text and "max_steps: 50" in text, text
    assert "# the author's own note survives" in text
    assert with_terms(REVIEW, settings, None)[1] == 2000           # neither: the default
    assert with_terms(REVIEW.replace("{max_steps: 50}", "{max_output_tokens: 500}"), settings, None)[1] == 500
    # a USD cap the machine sets itself stays, as one bound more
    assert "max_cost: 0.5" in with_terms(REVIEW.replace("{max_steps: 50}", "{max_cost: 0.5}"), settings, None)[0]
    assert with_terms(REVIEW.replace("{max_steps: 50}", "{concurrency: 2}"), settings, None)[2] == 2
    # a machine asking for more than the operator allows gets the most it may: what runs at once can spend past
    # the cap (it is checked before an activity starts)
    assert with_terms(REVIEW.replace("{max_steps: 50}", "{concurrency: 500}"), settings, None)[2] == 3
    wider = WorkflowSettings(**{**SETTINGS, "max_concurrency": 6})
    assert with_terms(REVIEW.replace("{max_steps: 50}", "{concurrency: 500}"), wider, None)[2] == 6


def test_a_cap_above_the_ceiling_is_refused_not_lowered():
    with pytest.raises(ValueError) as refused:
        with_terms(REVIEW, WorkflowSettings(**SETTINGS), 25000)
    assert "above this installation's ceiling of 10000" in str(refused.value)


def test_an_inline_machine_reads_no_file_beside_it():
    assert inline_problems({"stategraph": 1, "imports": {"x": "./x.yaml"}, "python": "m.py"})
    assert inline_problems({"stategraph": 1, "machines": {"one": {}}}) == []


# ------------------------------------------------------------------ the boundary: never more than its author

def test_a_workflow_names_only_agents_its_author_could_start_through_its_own_sam():
    bound = make_config_check(system(), runner="stategraph_runner", own_instance="stategraph", caller="lead")
    free = make_config_check(system(), runner="stategraph_runner", own_instance="stategraph")

    assert bound("agent", "finder", {}) is None
    refused = bound("agent", "root_agent", {})
    assert refused and "lead could not start it" in refused, refused
    assert free("agent", "root_agent", {}) is None, "a machine without an author keeps its reach"


def test_a_workflow_calls_only_tools_its_author_may_call():
    bound = make_config_check(system(), runner="stategraph_runner", own_instance="stategraph", caller="lead")
    free = make_config_check(system(), runner="stategraph_runner", own_instance="stategraph")

    assert bound("tool", "notes_read", {}) is None
    refused = bound("tool", "notes_write", {})
    assert refused and "not in lead's own tool allowlist" in refused, refused
    assert free("tool", "notes_write", {}) is None, "the runner alone bounds a machine without an author"


# ------------------------------------------------------------------ start_workflow

async def test_a_workflow_runs_from_its_snapshot_with_its_terms_written_in(server):
    fake_backends(server, {"find": "a bug", "verify/0": "stands"})
    started = await server.service.start_workflow(REVIEW, user_id="ann", caller="lead", max_output_tokens=1000)

    row = await settled(server, started["run_id"])
    assert row["status"] == "succeeded", row.get("error")
    assert row["output"] == {"found": ["a bug"]}
    assert server.machines.find("quick_review") is None, "an inline workflow is never saved"
    snapshot = row["definition"]["files"]
    text = next(iter(snapshot.values()))
    assert "max_output_tokens: 1000" in text and "concurrency: 3" in text, text
    assert row["workflow"]["caller"] == "lead" and row["workflow"]["max_concurrent_agents"] == 2
    assert started["max_output_tokens"] == 1000 and started["concurrency"] == 3


async def test_a_workflow_naming_an_agent_its_author_cannot_start_never_runs(server):
    made = fake_backends(server, {})
    with pytest.raises(ServiceError) as refused:
        await server.service.start_workflow(REVIEW.replace("agent: skeptic", "agent: root_agent"),
                                            user_id="ann", caller="lead")
    assert refused.value.status == 422 and "lead could not start it" in refused.value.message
    assert made == [] and server.run_store.list_runs() == []


async def test_the_catalog_for_a_workflow_offers_its_subset_its_profiles_and_its_terms(server):
    """A workflow's author learned the cap's ceiling and the profiles it may name only from a refused start, and was
    offered activities a workflow may not use."""
    from plugins.stategraph.server import WORKFLOW_KINDS

    plain = await server.catalog({})
    assert {"call", "acquire"} <= {k["key"] for k in plain["kinds"]} and "workflow_limits" not in plain, "fixture"

    found = await server.catalog({"for_workflow": True, "_agent_name": "lead"})

    keys = {k["key"] for k in found["kinds"]}
    assert keys and keys <= WORKFLOW_KINDS, keys
    assert found["workflow_limits"]["max_output_tokens_ceiling"] == SETTINGS["max_output_tokens_ceiling"], found
    configured = getattr(server.system_config.llm_system, "profiles", None) or {}
    assert found["llm_profiles"] == sorted(configured), found["llm_profiles"]
    unknown = await server.validate_machine({"yaml": REVIEW.replace('task: "find"', 'task: "find", llm_profile: nope'),
                                             "for_workflow": True, "_agent_name": "lead"})
    assert any("nope" in p["message"] and "(known:" in p["message"] for p in unknown["problems"]), unknown


async def test_validating_for_a_workflow_finds_what_starting_it_would_refuse(server):
    """validate_machine said valid, run_workflow then refused it: the author's reach was not checked."""
    beyond = REVIEW.replace("agent: skeptic", "agent: root_agent")
    plain = await server.validate_machine({"yaml": beyond})
    assert plain["valid"] is True, "fixture: valid as a saved machine"

    checked = await server.validate_machine({"yaml": beyond, "for_workflow": True, "_agent_name": "lead"})
    assert checked["valid"] is False and any(p["code"] == "SG007" for p in checked["problems"]), checked
    fine = await server.validate_machine({"yaml": REVIEW, "for_workflow": True, "_agent_name": "lead",
                                          "max_output_tokens": 1000.0})
    assert fine["valid"] is True and (fine["max_output_tokens"], fine["concurrency"]) == (1000, 3), fine


@pytest.mark.parametrize("change,status,says", [
    (lambda t: t.replace("context:", "imports: {x: ./x.yaml}\ncontext:"), 422, "self-contained"),
    (lambda t: t, 409, "names a saved machine"),
])
async def test_an_inline_workflow_that_would_read_or_be_another_machine_is_refused(server, change, status, says):
    from pathlib import Path

    if status == 409:
        (Path(server.machines.writable[0]) / "quick_review.yaml").write_text(REVIEW, encoding="utf-8")
    with pytest.raises(ServiceError) as refused:
        await server.service.start_workflow(change(REVIEW), user_id="ann", caller="lead")
    assert (refused.value.status, says in refused.value.message) == (status, True), refused.value.message


async def test_without_settings_or_without_an_agent_nothing_starts(server, tmp_path):
    with pytest.raises(ServiceError) as no_agent:
        await server.service.start_workflow(REVIEW, user_id="ann", caller=None)
    assert "started by an agent" in no_agent.value.message
    (tmp_path / "bare").mkdir()
    bare = StateGraphServer("stategraph", system(), tool_config(tmp_path / "bare"))
    try:
        with pytest.raises(ServiceError) as off:
            await bare.service.start_workflow(REVIEW, user_id="ann", caller="lead")
        assert "no workflows: block" in off.value.message
    finally:
        await bare.stop_plugin()


REVIEW_CHANGES = """\
stategraph: 1
id: review_changes
title: Review a diff in several directions, verify every finding
description: >-
  Each direction finds, then verifies its own findings with three skeptics,
  independently of the others (no barrier between directions).
params:
  diff: {type: string, required: true}
  lenses: {type: array, default: ["correctness", "security", "concurrency"]}
limits: {concurrency: 6, max_output_tokens: 3000, timeout: 1h, max_steps: 400}
context: {confirmed: [], lost: []}

machines:
  direction:
    params:
      lens: {type: string, required: true}
      diff: {type: string, required: true}
    context: {findings: []}
    initial: find
    states:
      find:
        do:
          agent: finder
          task: "Review this diff only for {{ params.lens }}. Answer as JSON.\n\n{{ params.diff }}"
          schema:
            type: object
            required: [findings]
            properties:
              findings:
                type: array
                items:
                  type: object
                  required: [file, claim]
                  properties: {file: {type: string}, line: {type: integer}, claim: {type: string}}
        transitions:
          - target: verify
            guard: 'len(out["findings"]) > 0'
            effect: 'ctx.findings = out["findings"]'
          - {target: done, guard: else}
      verify:
        do:
          map: ctx.findings
          as: finding
          concurrency: 3
          each:
            map: range(3)
            as: skeptic
            fail: collect
            each:
              agent: finder
              task: "Try to refute this finding against the diff. Default to refuted=true if unsure.\n\n{{ finding }}\n\n{{ params.diff }}"
              schema:
                type: object
                required: [refuted]
                properties: {refuted: {type: boolean}, reason: {type: string}}
        transitions:
          - target: done
            effect: |
              ctx.findings = [f for f, votes in zip(ctx.findings, out)
                              if sum(1 for v in votes if "out" in v and not v["out"]["refuted"]) >= 2]
      done: {type: final, output: {lens: "{{ params.lens }}", confirmed: "{{ ctx.findings }}"}}

initial: review
states:
  review:
    do:
      map: params.lenses
      as: lens
      concurrency: 3
      fail: collect
      each: {machine: direction, params: {lens: "{{ lens }}", diff: "{{ params.diff }}"}}
    transitions:
      - target: done
        effect: |
          ctx.confirmed = [f for r in out if "out" in r for f in r["out"]["confirmed"]]
          ctx.lost = [i for i, r in enumerate(out) if "error" in r]
  done:
    type: final
    output: {confirmed: "{{ ctx.confirmed }}", lost_directions: "{{ ctx.lost }}"}
"""


async def test_the_concepts_review_runs_as_a_pipeline_and_keeps_a_finding_only_by_majority(server):
    """The concept's example (docs: "dynamic workflows"), started as a workflow: three directions find, three
    skeptics per finding vote; a failed skeptic is no vote; a slow direction holds up nobody."""
    import json
    import time

    from plugins.stategraph.kinds import ActivityError

    findings = {"correctness": [{"file": "a.py", "claim": "A"}, {"file": "a.py", "claim": "B"}],
                "security": [{"file": "s.py", "claim": "C"}], "concurrency": []}
    votes = {"A": [False, False, True], "B": [True, True, False], "C": [False, None, False]}
    seen, t0 = [], time.monotonic()

    async def answer(call):
        path, task = call["path"], call["task"]
        seen.append((time.monotonic() - t0, path))
        if path.endswith("/find"):
            lens = next(lens for lens in findings if f"only for {lens}" in task)
            if lens == "concurrency":
                await asyncio.sleep(0.3)
                seen.append((time.monotonic() - t0, "slow find ended"))
            return json.dumps({"findings": findings[lens]})
        claim = next(c for c in votes if f'"claim": "{c}"' in task or f"'claim': '{c}'" in task)
        vote = votes[claim][int(path.rsplit("/", 1)[1])]
        if vote is None:
            raise ActivityError("agent_failed", "skeptic down")
        return json.dumps({"refuted": vote})

    class Every(dict):
        def get(self, path, default=None):
            return answer

    made = fake_backends(server, {})
    server.service.backend_factory = lambda: (lambda run_id: made.append(FakeBackend()) or made[-1])
    original = FakeBackend.__init__

    def with_answers(self, handlers=None, template_vars=None):
        original(self, None, template_vars)
        self.handlers = Every()
    FakeBackend.__init__ = with_answers
    try:
        started = await server.service.start_workflow(REVIEW_CHANGES, params={"diff": "diff --git"}, user_id="ann",
                                                      caller="lead")
        row = await settled(server, started["run_id"])
    finally:
        FakeBackend.__init__ = original

    assert row["status"] == "succeeded", row.get("error")
    assert [f["claim"] for f in row["output"]["confirmed"]] == ["A", "C"], row["output"]
    slow_began = next(t for t, p in seen if p == "review/2/find")
    slow_ended = next(t for t, p in seen if p == "slow find ended")
    assert any("/verify/" in p and slow_began < t < slow_ended for t, p in seen),         f"no direction was verified while the slow one was still finding: {seen}"


# ------------------------------------------------------------------ its end: wake and hand over

async def test_the_end_wakes_the_caller_that_asked_with_its_request_and_leaves_the_result_waiting(server,
                                                                                                 monkeypatch):
    import agent_system.core.session_presence as presence

    woken = []

    async def wake_session(config, session, user, what="", still_needed=None, started_by=None):
        woken.append((session, user, started_by, still_needed()))
        return "delivered"
    monkeypatch.setattr(presence, "wake_session", wake_session)
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann", workflow={
        "caller": "lead", "session": "s1", "request": "req-7", "wake": True})
    server.run_store.create_run("r2", "quick_review", {}, user_id="ann", workflow={
        "caller": "lead", "session": "s2", "request": "req-8", "wake": False})
    for run_id in ("r1", "r2"):  # the runner writes the end before it calls run_ended
        server.run_store.update_run(run_id, status="succeeded")

    await server.service._workflow_ended("r1", "succeeded")
    await server.service._workflow_ended("r1", "cancelled")   # whoever stops a run does not wait for it
    await server.service._workflow_ended("r2", "succeeded")   # it did not ask
    await asyncio.sleep(0)

    assert woken == [("s1", "ann", "req-7", True)], woken
    assert server.workflow_results.waiting("r1") and not server.workflow_results.waiting("r2")


async def test_a_workflow_resumed_after_its_caller_read_it_rings_again_when_it_ends_again(server, monkeypatch):
    import agent_system.core.session_presence as presence

    woken = []

    async def wake_session(config, session, user, what="", still_needed=None, started_by=None):
        woken.append(still_needed())
        return "delivered"
    monkeypatch.setattr(presence, "wake_session", wake_session)
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann", workflow={"session": "s1", "wake": True})
    server.run_store.update_run("r1", status="failed", finished_at="2026-11-10T10:00:00.000+00:00")
    await server.service._workflow_ended("r1", "failed")
    await asyncio.sleep(0)
    server.workflow_results.read("r1", "s1")  # its caller read the failure, then resumed the run
    server.run_store.update_run("r1", status="succeeded", finished_at="2099-01-01T00:00:00.000+00:00")
    await server.service._workflow_ended("r1", "succeeded")
    await asyncio.sleep(0)

    assert woken == [True, True], "the second end found its result already delivered: nobody rings, nothing comes"


@pytest.mark.parametrize("wake", [True, False])
async def test_a_workflow_a_restart_interrupted_is_resumed_by_itself_when_its_end_wakes_somebody(server, tmp_path,
                                                                                                 monkeypatch, wake):
    """Its caller was told it may end its turn and is woken: interrupted and left so, nobody would ever ring. One
    nobody is woken for stays interrupted: resumed, it would spend up to its cap for nobody."""
    import time

    from plugins.stategraph.schedules import Scheduler

    async def armed(user_id, session_id):
        return ""
    monkeypatch.setattr(server.service, "_wake_armed", armed)
    gate = asyncio.Event()
    fake_backends(server, {"find": held(gate)})
    started = await server.service.start_workflow(REVIEW, user_id="ann", caller="lead", caller_session="s1",
                                                  wake=wake)
    run_id = started["run_id"]
    await until(lambda: server.run_store.has_row(run_id, "activity", "s0"), what="the find agent in flight")
    await server.run_manager.shutdown()  # the process goes down with the run in flight
    assert server.run_store.get_run(run_id)["status"] == "interrupted", "fixture: the run did not stop interrupted"

    after = StateGraphServer("stategraph", system(), tool_config(tmp_path, workflows=dict(SETTINGS)))
    try:
        fake_backends(after, {"find": "a finding", "verify": "refuted"})
        assert (run_id in await Scheduler(after, after.schedules).resume_interrupted(time.time())) is wake
    finally:
        await after.stop_plugin()


def test_the_terms_keep_a_resume_the_workflow_sets_itself():
    from types import SimpleNamespace

    from plugins.stategraph.model.loader import parse_yaml

    settings = settings_of(SimpleNamespace(workflows=dict(SETTINGS)))
    assert parse_yaml(with_terms(REVIEW, settings, None, resume_auto=True)[0])["resume"] == "auto"
    assert "resume" not in parse_yaml(with_terms(REVIEW, settings, None)[0])
    manual = REVIEW.replace("initial: find", "resume: manual\ninitial: find")
    assert parse_yaml(with_terms(manual, settings, None, resume_auto=True)[0])["resume"] == "manual"


async def test_a_run_without_the_workflow_column_gets_no_end_hook(server, monkeypatch):
    """The writer's machines, the facade's runs: their backend is built as before."""
    import plugins.stategraph.service as service_module

    built = []
    monkeypatch.setattr(service_module, "ScarabHiveBackend", lambda **kw: built.append(kw) or object())
    monkeypatch.setattr(server, "resolve_runner", lambda name=None: object())
    server.run_store.create_run("plain", "m", {}, user_id="ann")
    server.run_store.create_run("wf", "m", {}, user_id="ann", workflow={"caller": "lead", "wake": True})

    server.service.backend_factory()("plain")
    server.service.backend_factory()("wf")

    assert built[0]["ended"] is None and built[0]["workflow"] is None
    assert built[1]["ended"] is not None and built[1]["workflow"]["caller"] == "lead"
    assert built[0]["config_check"]("agent", "root_agent", {}) is None
    assert "lead could not start it" in built[1]["config_check"]("agent", "root_agent", {})


async def test_the_result_reaches_the_callers_next_call_once(server):
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="succeeded", final_state="done", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")
    hooks = server.hooks_plugin

    other = await hooks.on_pre_llm_call(call("s2"))
    first = await hooks.on_pre_llm_call(call("s1"))
    held = first.context.messages
    again = await hooks.on_pre_llm_call(call("s1", step=1, messages=held))  # the next call: the history holds it

    assert not other.modified and len(other.context.messages) == 1
    assert first.modified and len(first.context.messages) == 2
    turn = first.context.messages[-1]
    assert turn.injected_by == "stategraph" and "ended succeeded in done" in turn.content
    assert turn.role == "user", "the workflow's output is data, not a note with an instruction's weight"
    assert "a bug" in turn.content
    assert not again.modified and len(again.context.messages) == 2
    assert not server.workflow_results.waiting("r1")


async def test_a_result_counts_as_delivered_only_once_a_history_holds_it(server):
    """A later hook of the chain can fail or be cancelled after this one appended the turn: then no history holds
    it, and the next call must bring it again -- until a call was made with it."""
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="succeeded", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")
    hooks = server.hooks_plugin

    lost = await hooks.on_pre_llm_call(call("s1"))
    assert lost.modified
    assert server.workflow_results.waiting("r1"), "marked delivered while only a call's copy held it"
    again = await hooks.on_pre_llm_call(call("s1", step=1))  # its history never got the turn
    assert again.modified and "a bug" in again.context.messages[-1].content
    await hooks.on_post_llm_call(answered("s1", again.context.messages))
    assert not server.workflow_results.waiting("r1"), "the call made with it did not mark it delivered"
    after = await hooks.on_pre_llm_call(call("s1", step=2))
    assert not after.modified


async def test_through_the_agents_hook_path_a_result_comes_and_counts_as_delivered_after_the_call(server):
    """The loop's own hook manager, the registry, the schema's two hooks: the call after the hand-over is the one
    that marks it delivered -- a turn's last call too, after which no pre_llm_call would come."""
    from types import SimpleNamespace

    from agent_system.hooks import get_hook_registry
    from agent_system.plugins.discovery import register_plugin_hooks
    from agent_system.servers.agent.components.hook_integration import HookIntegrationManager
    from plugins.stategraph.tests.test_plugin_stategraph_call_spend import unregister

    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="succeeded", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")
    registry = get_hook_registry()
    names = await register_plugin_hooks("stategraph", server.hooks_plugin, metadata=server.get_schema_data(),
                                        registry=registry)
    try:
        def lead(*hooks):
            return HookIntegrationManager(SimpleNamespace(name="lead", agent_config=SimpleNamespace(hooks=SimpleNamespace(
                enabled=True, overrides={f"stategraph.{h}_workflow_results": {"enabled": True} for h in hooks}))))
        only_deliver, both = lead("deliver"), lead("deliver", "confirm")

        sent = await only_deliver.execute_pre_llm_hooks([ChatMessage(role="user", content="go on")], 0, "req", "s1")
        assert "a bug" in sent[-1].content
        await only_deliver.execute_post_llm_hooks(sent, {}, 0, "req", "s1")
        assert server.workflow_results.waiting("r1"), "marked delivered without the confirming hook"
        await both.execute_post_llm_hooks(sent, {}, 0, "req", "s1")
        assert not server.workflow_results.waiting("r1"), "the call made with it did not mark it delivered"
    finally:
        await unregister(registry, names)


async def test_a_run_resumed_before_its_turn_was_confirmed_brings_no_turn_of_a_run_still_running(server):
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="failed", finished_at="2026-11-10T10:00:00.000+00:00",
                                error={"type": "agent_error"})
    server.workflow_results.add("s1", "r1", "2026-11-10T10:00:00.000+00:00")
    hooks = server.hooks_plugin

    first = await hooks.on_pre_llm_call(call("s1"))
    assert first.modified
    server.run_store.update_run("r1", status="running", finished_at=None, error=None)  # its caller resumed it
    lost = await hooks.on_pre_llm_call(call("s1", step=1))  # the first turn never reached this history
    assert not lost.modified, "a turn for a run that runs again"
    assert not server.workflow_results.waiting("r1"), "a running run counts as waiting: its wake would ring"
    held = await hooks.on_pre_llm_call(call("s1", step=2, messages=first.context.messages))
    assert not held.modified
    server.run_store.update_run("r1", status="succeeded", finished_at="2099-01-01T00:00:00.000+00:00", error=None)
    server.workflow_results.add("s1", "r1", "2099-01-01T00:00:00.000+00:00")
    assert server.workflow_results.waiting("r1"), "the first end's turn, delivered, swallowed the second end"


async def test_a_run_that_ended_again_before_its_first_turn_was_confirmed_brings_its_second_end(server):
    """The history holds the first end's turn: that is not the second end's, which must still come."""
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="failed", finished_at="2026-11-10T10:00:00.000+00:00",
                                error={"type": "agent_error"})
    server.workflow_results.add("s1", "r1", "2026-11-10T10:00:00.000+00:00")
    first = await server.hooks_plugin.on_pre_llm_call(call("s1"))
    server.run_store.update_run("r1", status="succeeded", finished_at="2099-01-01T00:00:00.000+00:00", error=None,
                                output={"found": ["the second end"]})
    server.workflow_results.add("s1", "r1", "2099-01-01T00:00:00.000+00:00")

    then = await server.hooks_plugin.on_pre_llm_call(call("s1", step=1, messages=first.context.messages))
    assert then.modified and "the second end" in then.context.messages[-1].content


async def test_a_process_that_loads_a_history_with_the_turn_delivers_it_and_adds_none(server, tmp_path):
    """The turn reached the history, the confirmation did not (the process ended): the next process must not
    bring it twice."""
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="succeeded", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")
    first = await server.hooks_plugin.on_pre_llm_call(call("s1"))
    woken = StateGraphServer("stategraph", system(), tool_config(tmp_path, workflows=dict(SETTINGS)))  # same runs.db
    try:
        loaded = await woken.hooks_plugin.on_pre_llm_call(call("s1", messages=first.context.messages))
    finally:
        await woken.stop_plugin()
    assert not loaded.modified, "the turn it already holds came twice"
    assert not server.workflow_results.waiting("r1")


async def test_a_handed_result_its_caller_read_meanwhile_is_not_brought_again(server):
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann", workflow={"caller": "lead", "session": "s1"})
    server.run_store.update_run("r1", status="succeeded", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")

    lost = await server.hooks_plugin.on_pre_llm_call(call("s1"))
    await server.get_run({"run_id": "r1", "_user_id": "ann", "_session_id": "s1"})
    again = await server.hooks_plugin.on_pre_llm_call(call("s1", step=1))
    assert lost.modified and not again.modified, "brought again although its caller read it"


def call(session, step=0, messages=None):
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="req", session_id=session, step=step,
                       messages=list(messages) if messages is not None else [ChatMessage(role="user", content="go on")])


def answered(session, messages):
    """What the loop hands post_llm_call hooks: the history the call was made with (execute_post_llm_hooks)."""
    return HookContext(hook_type=HookType.POST_LLM_CALL, request_id="req", session_id=session,
                       messages=list(messages))


async def test_a_woken_turn_in_another_process_gets_the_result_once(server, tmp_path):
    """Waking an idle session starts a process of its own: the result must be there, and only once."""
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="succeeded", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")
    woken = StateGraphServer("stategraph", system(), tool_config(tmp_path, workflows=dict(SETTINGS)))  # same runs.db
    try:
        later = await woken.hooks_plugin.on_pre_llm_call(call("s1", step=3))  # not a turn's first call: no look-up
        first = await woken.hooks_plugin.on_pre_llm_call(call("s1"))
        await woken.hooks_plugin.on_post_llm_call(answered("s1", first.context.messages))
        here = await server.hooks_plugin.on_pre_llm_call(call("s1", step=3))  # the process that ran it
    finally:
        await woken.stop_plugin()
    assert not later.modified
    assert first.modified and "a bug" in first.context.messages[-1].content
    assert not here.modified, "delivered twice: once in each process"


async def test_the_answer_says_whether_the_result_comes_by_itself(server, monkeypatch):
    """The delivery hook is off by default (each hook copies the history on every call): an agent switches it on."""
    from types import SimpleNamespace

    async def armed(user_id, session_id):
        return ""
    monkeypatch.setattr(server.service, "_wake_armed", armed)
    fake_backends(server, {"find": "a bug", "verify/0": "stands"})

    def lead(overrides):
        return SimpleNamespace(name="lead", agent_config=SimpleNamespace(
            hooks=SimpleNamespace(enabled=True, overrides=overrides)))

    async def answer(agent):
        result = await server.run_workflow({"yaml": REVIEW.replace("quick_review", f"r{id(agent)}"), "_agent": agent,
                                            "_agent_name": "lead", "_user_id": "ann", "_session_id": "s1"})
        assert result["status"] == "success", result
        return result["next"]

    both = {"stategraph.deliver_workflow_results": {"enabled": True},
            "stategraph.confirm_workflow_results": {"enabled": True}}
    assert "comes with your next turn" in await answer(lead(both))
    assert "does not come by itself" in await answer(lead({}))
    assert "does not come by itself" in await answer(lead({k: v for k, v in both.items() if "deliver" in k}))
    shipped = {hook["name"]: hook["enabled"] for hook in server.get_schema_data()["hooks"]}
    assert shipped["deliver_workflow_results"] is False and shipped["confirm_workflow_results"] is False


async def test_the_tool_takes_the_cap_in_output_tokens_as_a_model_writes_it(server):
    fake_backends(server, {"find": "a bug", "verify/0": "stands"})

    async def start(machine_id, cap):
        return await server.run_workflow({"yaml": REVIEW.replace("quick_review", machine_id), "max_output_tokens": cap,
                                          "_agent_name": "lead", "_user_id": "ann", "_session_id": "s1"})

    taken = await start("as_float", 1500.0)  # 1500.0: as often as 1500
    assert taken["status"] == "success" and taken["max_output_tokens"] == 1500, taken
    refused = await start("half_a_token", 1500.5)
    assert refused["status"] == "error" and "whole number" in refused["error"], refused


async def test_only_its_callers_get_run_reads_its_result(server):
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann", workflow={"caller": "lead", "session": "s1"})
    server.run_store.update_run("r1", status="succeeded", output={})
    server.workflow_results.add("s1", "r1")

    assert (await server.get_run({"run_id": "r1", "_user_id": "ann", "_session_id": "s9"}))["status"] == "success"
    assert (await server.get_run({"run_id": "r1", "_user_id": "ann"}))["status"] == "success"
    assert server.workflow_results.waiting("r1"), "another session's read took the caller's result"
    await server.get_run({"run_id": "r1", "_user_id": "ann", "_session_id": "s1"})
    assert not server.workflow_results.waiting("r1")


async def test_a_fork_of_a_workflow_keeps_its_authors_bound_and_wakes_nobody(server):
    fake_backends(server, {"find": "a bug", "verify/0": "stands"})
    started = await server.service.start_workflow(REVIEW, user_id="ann", caller="lead", caller_session="s1",
                                                  caller_request="req-1")
    source = await settled(server, started["run_id"])
    fork_id = await server.run_manager.fork(source["id"], at_step=1, backend=FakeBackend({"verify/0": "stands"}),
                                            user_id="ann")
    fork = await settled(server, fork_id)
    assert fork["workflow"]["caller"] == "lead" and fork["workflow"]["max_concurrent_agents"] == 2
    assert fork["workflow"]["wake"] is False and fork["workflow"]["session"] is None


# ------------------------------------------------------------------ the gate and the waiting results

async def test_the_places_are_one_semaphore_per_loop_whatever_the_capacity():
    assert agent_places(None) is None and agent_places(0) is None
    first = agent_places(2)
    assert agent_places(5) is first, "a run started under another setting would get places of its own"


def test_waiting_results_are_taken_once_per_session(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    results = WaitingResults(RunStore(tmp_path / "runs.db"))
    results.add("s1", "r1")
    results.add("s1", "r1")
    results.add("s1", "r2")
    assert results.waiting_for("s2", look_up=True) == []
    assert results.waiting_for("s1") == ["r1", "r2"], "a result this process parked is seen without a look-up"
    assert results.waiting_for("s1") == [], "without a look-up, only what ended here since"
    assert results.deliver("r1") and not results.deliver("r1")
    assert results.waiting_for("s1", look_up=True) == ["r2"]


# ------------------------------------------------------------------ the real backend: gate and end

def test_only_a_workflow_runs_backend_offers_places(tmp_path):
    from plugins.stategraph.engine.backend import ScarabHiveBackend

    async def offered(workflow):
        return ScarabHiveBackend(runner=None, system_config=None, session_id="sg_r", user_id="ann",
                                 workflow=workflow).agent_place()
    assert asyncio.run(offered(None)) is None
    assert asyncio.run(offered({"caller": "lead"})) is None            # no max_concurrent_agents: no bound
    assert asyncio.run(offered({"caller": "lead", "max_concurrent_agents": 1})) is not None


PAIR = """\
stategraph: 1
id: pair
initial: both
states:
  both:
    do:
      map: '[1, 2]'
      as: item
      concurrency: 2
      each: {agent: finder, task: "t {{ item }}"}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_the_engine_holds_a_workflows_agents_to_the_places_its_backend_offers(tmp_path):
    """Two runs of two agents each, one place: one agent at a time. The wait is before the activity starts: a
    timeout of the activity does not run while it waits."""
    from plugins.stategraph.tests.stategraph_testkit import Harness, runnable

    running, peak = 0, 0

    class Placed(FakeBackend):
        def __init__(self, places):
            super().__init__()
            self.places = places

        def agent_place(self):
            return self.places

        async def agent_create(self, act, **kw):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return "ok", "inst"

    harness = Harness(tmp_path)
    try:
        manager = harness.manager()

        async def runs(places):
            ids = [await manager.start(runnable({"pair.yaml": PAIR}, root="pair.yaml"), backend=Placed(places))
                   for _ in range(2)]
            return [await settled_in(manager, run_id) for run_id in ids]

        rows = await runs(agent_places(1))
        assert [r["status"] for r in rows] == ["succeeded"] * 2 and peak == 1, (peak, rows)
        peak = 0
        await runs(None)
        assert peak > 1, "without places the agents of a run may run at once"
    finally:
        await harness.close()


async def settled_in(manager, run_id):
    from plugins.stategraph.tests.stategraph_testkit import settle

    return await settle(manager, run_id, timeout=10)


WIDE = """\
stategraph: 1
id: wide_review
context: {items: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}
initial: fan
states:
  fan:
    do:
      map: ctx.items
      as: item
      concurrency: 10
      fail: collect
      each: {agent: skeptic, task: "check {{ item }}"}
    transitions: [{target: done, effect: 'ctx.items = [r["status"] for r in out]'}]
  done: {type: final, output: "{{ ctx.items }}"}
"""


async def test_a_wide_map_stops_at_the_cap_and_overshoots_by_at_most_what_ran_at_once(server):
    """The cap is checked before an activity starts: the items of a wide map pass that check together at 0 output
    tokens. Checked again once an item holds its turn, at most the items running at once produce past it."""
    class Writing(FakeBackend):  # every agent writes 1000 output tokens
        async def agent_create(self, act, **kw):
            try:
                await asyncio.sleep(0.02)
                return "ok", "inst"
            finally:
                tokens = act.meta.setdefault("tokens", {"prompt": 0, "completion": 0, "cached": 0})
                tokens["prompt"] += 50000  # input is not counted: it is mostly cached
                tokens["completion"] += 1000
    server.service.backend_factory = lambda: (lambda run_id: Writing())
    started = await server.service.start_workflow(WIDE, user_id="ann", caller="lead", max_output_tokens=2000)
    row = await settled(server, started["run_id"])
    cap, produced = server.run_manager.output_tokens(row["id"])
    assert cap == 2000
    assert started["concurrency"] == 3, "a machine's own concurrency is held to the operator's most"
    assert cap <= produced <= cap + 1000 * started["concurrency"], (produced, row["output"])
    assert row["output"].count("failed") >= 10 - (2 + started["concurrency"]), row["output"]
    assert "budget_exceeded" in str(server.run_store.tail(row["id"], limit=200)), "fixture: the cap stopped it"


COUNTED = """\
stategraph: 1
id: counted
title: write, then branch on what it wrote
initial: write
states:
  write:
    do: {agent: finder, task: "write"}
    transitions:
      - {target: much, guard: "run.output_tokens >= 1000"}
      - {target: little, guard: else}
  much: {type: final, output: much}
  little: {type: final, output: little}
"""


async def test_a_guard_reads_the_output_tokens_so_far_and_prompt_tokens_do_not_count(server):
    class Writing(FakeBackend):
        def __init__(self, completion):
            super().__init__({})
            self.completion = completion

        async def agent_create(self, act, **kw):
            act.meta["tokens"] = {"prompt": 90000, "completion": self.completion, "cached": 80000}
            return "ok", "inst"

    for completion, branch in ((1200, "much"), (300, "little")):
        server.service.backend_factory = lambda c=completion: (lambda run_id: Writing(c))
        started = await server.service.start_workflow(COUNTED, user_id="ann", caller="lead")
        row = await settled(server, started["run_id"])
        assert row["output"] == branch, (completion, row.get("error"))
        assert server.run_manager.output_tokens(row["id"]) == (2000, completion)  # the default cap, written in
        # and its reader sees it: get_run names what the run produced against its cap
        read = await server.get_run({"run_id": row["id"], "_user_id": "ann"})
        assert (read["output_tokens"], read["max_output_tokens"]) == (completion, 2000), read


async def test_the_result_turn_names_the_output_tokens_the_run_produced(server):
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.record("r1", "activity", "find", state="find", status="done",
                            data={"meta": {"tokens": {"prompt": 50000, "completion": 1234, "cached": 0}}})
    server.run_store.update_run("r1", status="succeeded", final_state="done", output={"found": []})
    server.workflow_results.add("s1", "r1")
    turn = (await server.hooks_plugin.on_pre_llm_call(call("s1"))).context.messages[-1]
    assert ", 1234 output tokens" in turn.content.splitlines()[0], turn.content


def test_a_submachines_own_cap_on_output_tokens_is_named_as_ignored(tmp_path):
    from plugins.stategraph.model.validate import validate_tree
    from plugins.stategraph.store import MachineStore

    (tmp_path / "inner.yaml").write_text(
        "stategraph: 1\nid: inner\ntitle: inner\nlimits: {max_output_tokens: 500}\ninitial: done\n"
        "states:\n  done: {type: final}\n", encoding="utf-8")
    outer = ("stategraph: 1\nid: outer\ntitle: outer\nimports: {inner: ./inner.yaml}\ninitial: run\nstates:\n"
             "  run:\n    do: {machine: inner}\n    transitions: [{target: done}]\n  done: {type: final}\n")
    tree = validate_tree(MachineStore([str(tmp_path)], [str(tmp_path)]).load_files({"outer.yaml": outer},
                                                                                   machine_id="outer"))
    named = [p for p in tree.problems if p.code == "SG108" and "max_output_tokens" in p.message]
    assert named, [(p.code, p.message) for p in tree.problems]


async def test_the_backend_hands_the_end_on_and_a_failing_hand_off_changes_nothing(tmp_path):
    from plugins.stategraph.engine.backend import ScarabHiveBackend
    from plugins.stategraph.tests.stategraph_testkit import AgentHost, FakeAgent

    ended = []

    async def hand_on(status):
        ended.append(status)
        raise RuntimeError("the caller is gone")

    backend = ScarabHiveBackend(runner=AgentHost(tmp_path / "s", FakeAgent("finder")), system_config=None,
                                session_id="sg_r1", user_id="ann", workflow={"caller": "lead"}, ended=hand_on)
    await backend.run_ended(status="succeeded", final_state="done", output={"x": 1}, error=None)
    assert ended == ["succeeded"]


def test_two_processes_that_both_saw_a_result_waiting_deliver_it_once(tmp_path):
    """Each read the waiting list before either delivered (a race): only one hands it over."""
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    store.park_result("r1", "s1")
    one, other = WaitingResults(store), WaitingResults(RunStore(tmp_path / "runs.db"))
    assert one.waiting_for("s1", look_up=True) == ["r1"] == other.waiting_for("s1", look_up=True)
    assert one.deliver("r1") is True
    assert other.deliver("r1") is False


def test_a_read_by_its_caller_before_the_park_keeps_it_delivered(tmp_path):
    """The caller polls between the run's end and the park: the park must not make it wait (and ring) again."""
    from plugins.stategraph.engine.journal import RunStore

    from plugins.stategraph.engine.journal import utc_now

    results = WaitingResults(RunStore(tmp_path / "runs.db"))
    ended = utc_now()
    results.read("r1", "s1")
    results.add("s1", "r1", ended)
    assert not results.waiting("r1")
    assert results.waiting_for("s1", look_up=True) == []


def test_a_resumed_workflow_that_ends_again_waits_for_its_caller_again(tmp_path):
    """Its caller read the first end and resumed it: the second end is news, and must wait (and ring) again."""
    import time

    from plugins.stategraph.engine.journal import RunStore, utc_now

    results = WaitingResults(RunStore(tmp_path / "runs.db"))
    results.add("s1", "r1", utc_now())
    results.read("r1", "s1")
    assert not results.waiting("r1")
    time.sleep(0.01)  # the timestamps have milliseconds
    results.add("s1", "r1", utc_now())
    assert results.waiting("r1"), "the second end was swallowed by the first one's read"


async def test_a_result_whose_turn_cannot_be_built_stays_waiting(server, monkeypatch):
    server.run_store.create_run("r1", "quick_review", {}, user_id="ann")
    server.run_store.update_run("r1", status="succeeded", output={"found": ["a bug"]})
    server.workflow_results.add("s1", "r1")

    def broken(run_id):
        raise RuntimeError("journal unreadable")
    monkeypatch.setattr(server.run_manager, "budget", broken)
    lost = await server.hooks_plugin.on_pre_llm_call(call("s1"))
    assert not lost.modified
    assert server.workflow_results.waiting("r1"), "delivered although its turn was never built"
    monkeypatch.undo()
    later = await server.hooks_plugin.on_pre_llm_call(call("s1"))
    assert later.modified and "a bug" in later.context.messages[-1].content


DECIDES = """\
stategraph: 1
id: decides
initial: both
states:
  both:
    do:
      map: '[1, 2]'
      as: item
      concurrency: 2
      each: {decide: noul, by: finder, question: "fine?", input: "{{ item }}"}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_decision_by_an_agent_takes_a_place_too(tmp_path):
    """decide with by: runs an agent: the process-wide bound must hold it like an agent activity."""
    from plugins.stategraph.tests.stategraph_testkit import Harness, runnable

    running, peak = 0, 0

    class Placed(FakeBackend):
        def __init__(self, places):
            super().__init__()
            self.places = places

        def agent_place(self):
            return self.places

        async def agent_create(self, act, **kw):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return '{"decision": 0.9}', "inst"

    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        ids = [await manager.start(runnable({"decides.yaml": DECIDES}, root="decides.yaml"),
                                   backend=Placed(agent_places(1))) for _ in range(2)]
        rows = [await settled_in(manager, run_id) for run_id in ids]
    finally:
        await harness.close()
    assert [r["status"] for r in rows] == ["succeeded"] * 2, [r.get("error") for r in rows]
    assert peak == 1, peak


async def test_a_workflows_caller_stops_it_and_nothing_else(server):
    """coder_ultra had control_run to stop a runaway workflow -- and with it resume, fork and set on every run its
    user may see. stop_workflow ends the caller's own workflow, nothing more."""
    gate = asyncio.Event()
    fake_backends(server, {"find": held(gate)})
    started = await server.service.start_workflow(REVIEW, user_id="ann", caller="lead", wake=False)
    run_id = started["run_id"]
    await until(lambda: server.run_store.has_row(run_id, "activity", "s0"), what="the find agent in flight")

    other = await server.stop_workflow({"run_id": run_id, "_user_id": "ann", "_agent_name": "someone_else"})
    assert other["status"] == "error" and "no workflow you started" in other["error"], other
    assert server.run_store.get_run(run_id)["status"] == "running"

    stopped = await server.stop_workflow({"run_id": run_id, "_user_id": "ann", "_agent_name": "lead"})
    assert stopped["stopped"] is True and stopped["run_status"] == "cancelled", stopped
