"""Scores a workflow a planner agent wrote (suite workflow-authoring): does it parse, validate, stay in the
workflow subset, use only the agents the task allowed, and have the shape the task asks for?

The score is the share of criteria met; ``of`` is their number, so the report pools by criterion. The
machine is read from the agent's final answer (the last ```yaml block, else the whole answer) and checked
with the engine's own validator -- without the configuration check, which the agent ran itself through
stategraph_validate_machine; the agents are checked here against the task's list instead.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

import yaml

#: Keys a workflow must not use (skill stategraph-workflows, "The subset").
FORBIDDEN_TOP = ("python", "resources")
FORBIDDEN_KINDS = ("call", "callback", "acquire", "release")
FAN_OUT = ("map", "parallel")

#: Per task: the agents it may name and the shapes it needs.
TASKS: dict[str, dict[str, Any]] = {
    "diff-review": {"agents": {"coder_explorer", "coder_reviewer"}, "shapes": ["fan_out", "two_stage"]},
    "module-fixes": {"agents": {"coder", "coder_explorer", "coder_tester", "coder_reviewer"},
                     "shapes": ["pipeline", "collect"]},
    "cleanup-audit": {"agents": {"coder_explorer", "coder_reviewer"}, "shapes": ["loop", "fan_out"]},
    "menu-variants": {"agents": {"gamedev", "gamedev_tester", "godot_agent"}, "shapes": ["fan_out", "two_stage"]},
    "asset-pipeline": {"agents": {"blender_agent", "godot_agent", "gamedev_tester"},
                       "shapes": ["pipeline", "collect"]},
}

#: A fenced block -- or the last one left open: an answer cut short, or that forgot its closing fence.
_YAML_BLOCK = re.compile(r"```(?:yaml|yml)?[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL)


def machine_text(answer: str) -> str:
    """The machine in an answer: the last fenced block that holds ``stategraph:``, else the answer itself."""
    blocks = [b for b in _YAML_BLOCK.findall(answer or "") if "stategraph:" in b]
    return blocks[-1] if blocks else (answer or "")


def _dicts(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dicts(value)


def _activities(node: Any) -> Iterator[dict]:
    """Every activity: a ``do:`` value and, below it, every ``each`` and parallel branch."""
    for d in _dicts(node):
        if isinstance(d.get("do"), dict):
            yield from _below(d["do"])


def _below(activity: dict) -> Iterator[dict]:
    yield activity
    if isinstance(activity.get("each"), dict):
        yield from _below(activity["each"])
    if isinstance(activity.get("parallel"), dict):
        for branch in activity["parallel"].values():
            if isinstance(branch, dict):
                yield from _below(branch)


def _states(spec: dict) -> Iterator[tuple[str, dict]]:
    """Every state of the machine and of its local submachines, composites included."""
    def walk(states: Any) -> Iterator[tuple[str, dict]]:
        for name, state in (states or {}).items() if isinstance(states, dict) else ():
            if isinstance(state, dict):
                yield str(name), state
                yield from walk(state.get("states"))
    yield from walk(spec.get("states"))
    for local in (spec.get("machines") or {}).values() if isinstance(spec.get("machines"), dict) else ():
        if isinstance(local, dict):
            yield from walk(local.get("states"))


def shapes(spec: dict) -> dict[str, bool]:
    acts = list(_activities(spec))
    fan_outs = [a for a in acts if any(k in a for k in FAN_OUT)]
    loop = any("max_visits" in state and any(isinstance(t, dict) and t.get("target") == name
                                              for t in state.get("transitions") or [])
               for name, state in _states(spec))
    return {
        "fan_out": bool(fan_outs),
        "two_stage": len(fan_outs) >= 2,     # a fan-out to find or build, another to check
        "pipeline": any("map" in a and isinstance(a.get("each"), dict) and "machine" in a["each"] for a in acts),
        "collect": any(a.get("fail") == "collect" for a in fan_outs),
        "loop": loop,
    }


def agents_named(spec: dict) -> set[str]:
    return {str(a[key]) for a in _activities(spec) for key in ("agent", "by") if isinstance(a.get(key), str)}


def validation_errors(text: str) -> list[str]:
    from plugins.stategraph.model.loader import SnapshotSources, load_tree
    from plugins.stategraph.model.validate import validate_tree

    try:
        machine_id = str((yaml.safe_load(text) or {}).get("id") or "workflow")
    except Exception:
        machine_id = "workflow"
    path = f"{machine_id}.yaml"
    tree = load_tree(path, SnapshotSources({path: text}, {machine_id: path}))
    validate_tree(tree)
    return [f"{p.code} {p.path}: {p.message}" for p in tree.problems if p.level == "error"]


def score(workspace: Any, run: dict, task: Any) -> dict[str, Any]:
    """agent_eval's python check: ``task`` is the suite's Task object."""
    expected = TASKS[task.id]
    text = machine_text(str(run.get("answer") or ""))
    met: dict[str, bool] = {}
    notes: list[str] = []
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        spec, notes = None, [f"yaml: {exc}"[:200]]
    met["parses"] = isinstance(spec, dict) and spec.get("stategraph") == 1
    if not met["parses"]:
        spec = {}
    errors = validation_errors(text) if met["parses"] else ["does not parse"]
    met["validates"] = not errors
    notes += errors[:3]
    met["max_cost"] = isinstance((spec.get("limits") or {}).get("max_cost"), (int, float))
    used_forbidden = [k for k in FORBIDDEN_TOP if k in spec] + sorted(
        {k for a in _activities(spec) for k in FORBIDDEN_KINDS if k in a})
    met["subset"] = met["parses"] and not used_forbidden
    if used_forbidden:
        notes.append(f"outside the subset: {used_forbidden}")
    named = agents_named(spec)
    met["agents"] = bool(named) and named <= expected["agents"]
    if named - expected["agents"]:
        notes.append(f"agents not allowed: {sorted(named - expected['agents'])}")
    found = shapes(spec)
    for shape in expected["shapes"]:
        met[shape] = found[shape]
    missing = [name for name, ok in met.items() if not ok]
    return {"score": sum(met.values()) / len(met), "of": len(met), "passed": not missing,
            "detail": ("missing: " + ", ".join(missing) if missing else "all met") + ("; " + "; ".join(notes)
                                                                                     if notes else "")}
