"""machines/showcase.yaml: the example that uses every element of the format.

It must stay what it claims. Every field of every spec model and activity kind -- the models found from
MachineSpec and the kinds' spec models through their annotations -- and every non-default choice of a Literal
field appears in it, so a new element of the format turns the first test red until the showcase uses it. Its
`agent:` block is a comment (a real one would declare an agent in every process); the check reads it
uncommented, and a test holds that it would be a valid block. The runs go from the first state to the saved
article, and to the rejection, with every agent, tool and decision mocked. That the machine validates against the
shipped config is test_plugin_stategraph_config.py's (every shipped machine).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel

from plugins.stategraph.kinds import REGISTRY, KindSpec
from plugins.stategraph.model import spec as model
from plugins.stategraph.model.validate import agent_params_problems
from plugins.stategraph.tests.stategraph_testkit import (FakeBackend, Harness, held, load, runnable, sequence, settle,
                                                         until, validate)

MACHINES = Path(__file__).resolve().parent.parent / "machines"
FILES = {name: (MACHINES / name).read_text(encoding="utf-8")
         for name in ("showcase.yaml", "showcase.py", "critique_round.yaml")}
ROOT = "showcase.yaml"

KINDS = {key: kind for key, kind in REGISTRY.items() if type(kind).__module__.startswith("plugins.stategraph.")}
KIND_OF = {kind.spec_model: key for key, kind in KINDS.items()}
ACTIVITIES = {("StateSpec", "do"), ("StateSpec", "finally_"), ("MachineSpec", "finally_"),
              ("ResourceSpec", "open"), ("ResourceSpec", "fork"), ("ResourceSpec", "close")}
ONE_TEMPLATE = ("MachineSpec", "vars", "one template")  # vars: "{{ ... }}" instead of a mapping
#: What the showcase leaves out, and why; each must stay unused (else it belongs back in the check).
LEFT_OUT = {
    ("decide", "profile"): "names a profile of the operator's LLM config, which another installation lacks",
    ("map", "fail", "collect"): "parallel's fail: collect shows it (review)",
    ("MachineAgentSpec", "input", "json"): "one agent: block shows one choice of each",
    ("MachineAgentSpec", "on_wait", "block"): "one agent: block shows one choice of each",
    ("MachineAgentSpec", "visibility", "ui"): "one agent: block shows one choice of each",
    ("MachineAgentSpec", "visibility", "tool"): "one agent: block shows one choice of each",
    ("MachineAgentSpec", "visibility", "both"): "one agent: block shows one choice of each",
}


def with_agent_block(text: str) -> str:
    """The showcase with its commented `agent:` block taken in."""
    lines = text.splitlines()
    start = lines.index("# agent:")
    end = start + 1
    while end < len(lines) and lines[end].startswith("#   "):
        end += 1
    return "\n".join(lines[:start] + [line[2:] for line in lines[start:end]] + lines[end:]) + "\n"


def owner(cls: type, name: str) -> str:
    if cls in KIND_OF:
        return "KindSpec" if name in KindSpec.model_fields else KIND_OF[cls]
    return "MachineSpec" if cls is model.LocalMachineSpec else cls.__name__


def models_in(annotation: Any) -> list[type]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [found for arg in get_args(annotation) for found in models_in(arg)]


def literals_in(annotation: Any) -> list[Any]:
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    return [found for arg in get_args(annotation) for found in literals_in(arg)]


def format_elements() -> set[tuple]:
    """(owner, field) for every field, (owner, field, value) for every non-default Literal choice."""
    expected, seen, todo = {ONE_TEMPLATE}, set(), [model.MachineSpec, *KIND_OF]
    while todo:
        cls = todo.pop()
        if cls in seen:
            continue
        seen.add(cls)
        for name, field in cls.model_fields.items():
            expected.add((owner(cls, name), name))
            expected |= {(owner(cls, name), name, value) for value in literals_in(field.annotation)
                         if value != field.default}
            todo += models_in(field.annotation)
    return expected


def showcase_elements() -> set[tuple]:
    used: set[tuple] = set()

    def activity(do: dict[str, Any]) -> None:
        kind = next(key for key in do if key in KINDS)
        walk(KINDS[kind].spec_model.model_validate(do))
        for key in KINDS[kind].nested_one:
            if key in do:
                activity(do[key])
        for key in KINDS[kind].nested_map:
            for child in (do.get(key) or {}).values():
                activity(child)

    def walk(obj: Any) -> None:
        if isinstance(obj, BaseModel):
            cls = type(obj)
            for name in obj.model_fields_set:
                value, who = getattr(obj, name), owner(cls, name)
                used.add((who, name))
                used.update((who, name, literal) for literal in literals_in(cls.model_fields[name].annotation)
                            if value == literal and type(value) is type(literal))
                if (who, name) == ONE_TEMPLATE[:2] and isinstance(value, str):
                    used.add(ONE_TEMPLATE)
                if (who, name) in ACTIVITIES:
                    activity(value)
                else:
                    walk(value)
        elif isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    files = {**FILES, ROOT: with_agent_block(FILES[ROOT])}
    walk(load(files, ROOT).files[ROOT].spec)
    return used


def test_the_showcase_uses_every_element_of_the_format():
    used = showcase_elements()

    missing = sorted(format_elements() - used - set(LEFT_OUT), key=str)
    assert not missing, f"the format has elements the showcase does not use: {missing}"
    assert not set(LEFT_OUT) & used, f"LEFT_OUT names what is used now: {sorted(set(LEFT_OUT) & used, key=str)}"


def test_the_agent_block_in_the_comment_would_be_valid():
    files = {**FILES, ROOT: with_agent_block(FILES[ROOT])}
    tree = validate(files, ROOT)
    spec = tree.files[ROOT].spec

    assert spec is not None and not tree.problems, [p.as_dict() for p in tree.problems]
    assert spec.agent is not None and spec.agent.task_param == "topic"
    assert agent_params_problems(spec.id, spec.params, spec.agent) == []


# ------------------------------------------------------------------ runs

def decision(value: Any) -> dict[str, Any]:
    return {"value": value, "confidence": None, "probabilities": None}


SECTION = "The tide rises twice a day. " * 10  # 60 words: long enough for the check
CLAIM = "The moon is 384,400 km away."


def answers() -> dict[str, Any]:
    never = asyncio.Event()  # the losers of join first / count wait on it until they are cancelled
    return {
        "plan/quick": sequence('{"sections": ["Tides", "More tides"]}',  # refused by check_outline
                               '{"sections": ["Why tides happen", "Spring and neap tides", "Reading a tide table", '
                               '"Extra"]}'),
        "plan/thorough": held(never),
        "write/0": sequence("Too short.", SECTION, SECTION),  # the check sends the first answer back
        "write/1": SECTION, "write/2": SECTION, "write/3": SECTION,
        "review/critique/critique": '{"notes": "fine as it is", "blocking": false}',
        "review/facts/0": "NONE.", "review/facts/1": CLAIM, "review/facts/2": "NONE",
        "review/clarity": {"decision": decision(3)},
        "review/fit": {"decision": decision("fits")},
        "review/tags": {"technical": decision(0.2), "audience": decision("beginners"), "length": decision(1)},
        "judge/editor": sequence('{"decision": 0.2}', '{"decision": 0.9}'),  # round 1 not ready, round 2 ready
        "judge/model": sequence({"decision": decision(0.3)}, {"decision": decision(0.8)}),
        "judge/reader": held(never),
        "polish/tighten": "```\nThe tightened article.\n```",
        "polish/name_it": " How the moon moves the sea ",
        "save": {"status": "ok", "doc": "article", "chars": 120},
        "resources/store/close": {"status": "ok", "count": 1, "docs": ["article"]},
    }


@pytest.fixture
async def harness(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    await harness.close()


async def at_the_approval(harness: Harness):
    backend = FakeBackend(answers())
    manager = harness.manager()
    run_id = await manager.start(runnable(FILES, ROOT), params={"topic": "tides", "keywords": ["moon"]},
                                 backend=backend)
    await until(lambda: harness.store.get_run(run_id)["status"] in ("waiting", "failed"), 10, "the approval wait")
    assert harness.store.get_run(run_id)["status"] == "waiting", harness.store.get_run(run_id)
    return manager, run_id, backend


async def test_the_showcase_runs_from_its_plan_to_the_saved_article(harness):
    manager, run_id, backend = await at_the_approval(harness)
    loop = asyncio.get_running_loop()

    assert manager.send_event(run_id, "comment", {"text": "nice intro"})["accepted"]  # internal: still waiting
    assert manager.send_event(run_id, "approve")["accepted"]
    approved_at = loop.time()
    row = await settle(manager, run_id, 10)

    assert (row["status"], row["output"]) == ("succeeded", {
        "title": "How the moon moves the sea", "article": "The tightened article.", "rounds": 2,
        "comments": ["nice intro"]}), row
    assert loop.time() - approved_at >= 0.9, "the pause (after: 1s) did not wait"
    calls = [call for call in backend.calls]
    by_path = {call["path"]: call for call in calls}  # the last call of each path
    # join first / join count: the losers were cancelled, the run did not wait for them
    assert "plan/thorough" in backend.cancelled and backend.cancelled.count("judge/reader") == 2
    # the refused outline's problem went to the planner, not on to the writers
    planned = [call["task"] for call in calls if call["path"] == "plan/quick"]
    assert len(planned) == 2 and "2 headings, 3 wanted" in planned[1]
    assert "2 headings" not in next(call["task"] for call in calls if call["path"] == "write/0")
    # map: only the wanted headings; until: the facts check stopped at the first claim to source
    assert backend.count("write/3") == 0 and backend.count("review/facts/2") == 0
    # check: the short answer went back to the same instance
    first = [call for call in calls if call["path"] == "write/0"]
    assert [call["kind"] for call in first] == ["agent_create", "agent_continue", "agent_create"]
    assert "too short" in first[1]["message"]
    # the review's notes reached the second round
    assert f"give a source for: {CLAIM}" in by_path["write/1"]["task"] and "NONE" not in by_path["write/1"]["task"]
    assert by_path["write/1"]["vars"]["section"] == "Spring and neap tides"
    namespace = f"showcase-{run_id}"
    assert by_path["write/1"]["vars"]["json_namespace"] == namespace and by_path["write/1"]["vars"]["tone"] == "casual"
    assert by_path["judge/editor"]["agent"] == "stategraph_example_agent"
    assert "keep the words moon" in by_path["polish/tighten"]["task"]
    assert by_path["polish/tighten"]["vars"]["step"] == "polish"
    assert (by_path["polish/name_it"]["kind"], by_path["polish/name_it"]["instance_id"]) == \
        ("agent_continue", "inst-polish/tighten")
    assert by_path["save"]["args"] == {"operation": "write", "namespace": namespace, "doc": "article",
                                       "if_exists": "replace", "data": {"title": "How the moon moves the sea",
                                                                        "text": "The tightened article.",
                                                                        "topic": "tides", "rounds": 2}}
    assert by_path["resources/store/close"]["args"] == {"operation": "list", "namespace": namespace}
    assert backend.notes[0].startswith("Waiting for approval of How the moon moves the sea: "
                                       "/plugins/stategraph/callback?token=")
    assert backend.notes[1:] == ["Approval step left (transition)", "Showcase ended: finished"]


async def test_a_rejection_ends_the_run_without_saving(harness):
    manager, run_id, backend = await at_the_approval(harness)

    assert manager.send_event(run_id, "reject", {"reason": "off topic"})["accepted"]
    row = await settle(manager, run_id, 10)

    assert (row["status"], row["output"]) == (
        "succeeded", {"title": "How the moon moves the sea", "rejected": "off topic"}), row
    assert backend.count("save") == 0
    assert backend.notes[1:] == ["Approval step left (transition)", "Showcase ended: finished"]
