"""Semantic validation of a loaded machine tree (docs/stategraph_design.md §4).

Structure, pseudostate rules, Python (compiles, names exist and are bound where
they are used, purity), activities (kind, fields, templates), submachine
parameters, graph shape (networkx) and -- when a ``ConfigCheck`` is given --
whether the configuration can run what the machine names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import jsonschema
import networkx as nx
from pydantic import ValidationError

from plugins.stategraph.kinds import KindLookupError, parse_activity
from plugins.stategraph.kinds.builtin import param_ref
from plugins.stategraph.kinds.base import ActivityKind, KindSpec
from .code import (BINDINGS, SCOPE_NAMES, CodeError, analyse, braced, compile_expression, compile_statements,
                   scan_template, template_expressions)
from .loader import LoadedFile, MachineTree, dotted, schema_problems
from .spec import GUARD_ELSE, KNOWN_ERROR_TYPES, TRIGGER_DONE, TRIGGER_ERROR, MachineSpec, StateSpec, agent_entry

#: ``kind`` is "agent", "tool", "profile" or "vars_from" (with the name);
#: returns a problem text or None when the configuration can run it.
ConfigCheck = Callable[[str, str, dict[str, Any]], Optional[str]]

_BARE_REFERENCE = re.compile(r"^\s*(ctx|params|out|item|index|event|error|run)\s*[.\[]")


@dataclass
class _FileContext:
    loaded: LoadedFile
    tree: MachineTree
    spec: MachineSpec
    config_check: Optional[ConfigCheck]
    is_submachine: bool = False
    states: dict[str, StateSpec] = field(default_factory=dict)
    paths: dict[str, list[Any]] = field(default_factory=dict)
    parent: dict[str, Optional[str]] = field(default_factory=dict)
    ctx_reads: dict[str, list[Any]] = field(default_factory=dict)
    ctx_writes: set[str] = field(default_factory=set)
    pseudo_bound: dict[str, frozenset[str]] = field(default_factory=dict)
    open_resources: Optional[set[str]] = None  # while a resource's open/fork is checked: the ones before it

    @property
    def always_bound(self) -> frozenset[str]:
        """Names bound in every code field of this machine: resources, once it declares any (§2.8)."""
        return frozenset({"resources"}) if self.spec.resources else frozenset()

    def problem(self, level: str, code: str, message: str, path: list[Any]) -> None:
        # a machine inside another's file (machines:) is reported where it lies in that file
        self.tree.add(level, code, message, file=self.loaded.local_of or self.loaded.path,  # type: ignore[arg-type]
                      path=dotted(self.loaded.within + list(path)), line=self.loaded.line_of(path))


def validate_tree(tree: MachineTree, config_check: Optional[ConfigCheck] = None) -> MachineTree:
    imported = {target for loaded in tree.files.values() for target in loaded.imports.values()}
    for loaded in list(tree.files.values()):
        if loaded.spec is not None:
            _validate_file(_FileContext(loaded, tree, loaded.spec, config_check,
                                        is_submachine=loaded.path in imported))
    _dedupe(tree)
    return tree


def agent_params_problems(machine_id: str, declared: dict[str, Any], entry: Any) -> list[str]:
    """What in a machine agent's params keeps its runs from starting (``entry``: its config, an object or the mapping
    of an agent: block): a text message fills task_param, which must be a param; the required ones need a value."""
    read = entry.get if isinstance(entry, dict) else lambda key: getattr(entry, key, None)
    given = set(read("params") or {})
    problems = []
    if str(read("input") or "text") == "text":  # json: the message brings the params
        task = str(read("task_param") or "task")
        if task not in declared:
            problems.append(f"task_param {task!r} is no param of {machine_id} (it has: {', '.join(declared) or 'none'})")
        given.add(task)
        missing = sorted(name for name, param in declared.items() if param.required and name not in given)
        if missing:
            problems.append(f"{machine_id} requires {', '.join(missing)}: neither in params nor the task_param")
    return problems


def _check_agent(fc: _FileContext, spec: MachineSpec) -> None:
    """agent: -- what keeps the agent it offers from running, and a name another server holds (SG111): a warning,
    the machine itself runs all the same."""
    name, entry = agent_entry(spec, "")
    for problem in agent_params_problems(spec.id, spec.params, entry):
        fc.problem("warning", "SG111", f"agent: {problem}", ["agent"])
    if fc.config_check is not None:
        problem = fc.config_check("offer", name, {"machine": spec.id})
        if problem:
            fc.problem("warning", "SG111", f"agent: {problem}", ["agent", "name"] if spec.agent.name else ["agent"])


# ------------------------------------------------------------------ one file

def _validate_file(fc: _FileContext) -> None:
    spec = fc.spec
    _collect_states(fc, spec.states, ["states"], None)
    if spec.initial not in spec.states:
        where = "a nested state" if spec.initial in fc.states else "no state"
        fc.problem("error", "SG002", f"initial {spec.initial!r} is {where}; it must name a top-level state",
                   ["initial"])
    for name, value in spec.context.items():
        if not _is_json(value):
            fc.problem("error", "SG001", f"context.{name} is not JSON data", ["context", name])
    for name, param in spec.params.items():  # a YAML date (2024-01-01) reads as a date: the run could not store it
        for key, value in (("default", param.default), ("enum", param.enum)):
            if not _is_json(value):
                fc.problem("error", "SG001", f"params.{name}.{key} is not JSON data (a date or a tagged value? "
                                             "quote it)", ["params", name, key])
    for name, event in spec.events.items():
        _check_schema(fc, event.data, ["events", name, "data"], "SG001")
    for index, (name, resource) in enumerate(spec.resources.items()):
        fc.open_resources = set(list(spec.resources)[:index])
        _check_activity(fc, resource.open, ["resources", name, "open"], set())
        if resource.fork is not None:
            _check_activity(fc, resource.fork, ["resources", name, "fork"], {"fork_source"})
        fc.open_resources = None
        if resource.close is not None:
            _check_activity(fc, resource.close, ["resources", name, "close"], {"ending"})
    if spec.finally_ is not None:
        _check_activity(fc, spec.finally_, ["finally"], {"ending"})
    if spec.vars:
        _check_vars_shape(fc, spec.vars, ["vars"])
        _check_template(fc, spec.vars, ["vars"], BINDINGS["state"], set())
    if spec.vars_from and fc.config_check is not None:
        problem = fc.config_check("vars_from", spec.vars_from, {})
        if problem:
            fc.problem("error", "SG007", f"vars_from: {problem}", ["vars_from"])
    if isinstance(spec, MachineSpec) and spec.agent is not None:
        _check_agent(fc, spec)
    _pseudostate_bindings(fc)
    if fc.is_submachine and spec.limits.timeout is not None:
        fc.problem("warning", "SG108", "limits.timeout of a submachine is ignored; the calling activity's timeout "
                                       "bounds it", ["limits", "timeout"])
    if fc.is_submachine and spec.limits.concurrency is not None:
        fc.problem("warning", "SG108", "limits.concurrency of a submachine is ignored; the run's root machine "
                                       "bounds every activity of the run", ["limits", "concurrency"])
    for name, state in fc.states.items():
        _check_state(fc, name, state, fc.paths[name])
    _check_graph(fc)
    declared = set(spec.context)
    for field_name, where in fc.ctx_reads.items():
        if field_name not in declared and field_name not in fc.ctx_writes:
            fc.problem("warning", "SG105",
                       f"ctx.{field_name} is read but neither declared in context nor assigned anywhere", where)


def _collect_states(fc: _FileContext, states: dict[str, StateSpec], prefix: list[Any],
                    parent: Optional[str]) -> None:
    for name, state in states.items():
        path = prefix + [name]
        if name in fc.states:
            fc.problem("error", "SG002", f"state name {name!r} is used twice; names are unique per machine", path)
            continue
        fc.states[name] = state
        fc.paths[name] = path
        fc.parent[name] = parent
        if state.states:
            _collect_states(fc, state.states, path + ["states"], name)


def _pseudostate_bindings(fc: _FileContext) -> None:
    """A choice/junction sees what EVERY way into it binds (§2.6); an initial pseudostate binds no event names."""
    incoming: dict[str, set[Any]] = {n: set() for n, s in fc.states.items() if s.type in ("choice", "junction")}
    for name, state in fc.states.items():
        for transition in state.transitions:
            if transition.target in incoming:
                if state.type in ("choice", "junction"):
                    incoming[transition.target].add(("via", name))
                else:
                    trigger = transition.trigger
                    incoming[transition.target].add(trigger if trigger in (TRIGGER_DONE, TRIGGER_ERROR) else "event")
    initials = [fc.spec.initial] + [s.initial for s in fc.states.values() if s.states and s.initial]
    for name in initials:
        if name in incoming:
            incoming[name].add("state")
    resolved: dict[str, set[str]] = {}

    def kinds(name: str, seen: frozenset[str]) -> set[str]:
        if name in resolved:
            return resolved[name]
        found: set[str] = set()
        for item in incoming.get(name, ()):
            if isinstance(item, tuple):
                if item[1] not in seen:
                    found |= kinds(item[1], seen | {item[1]})
            else:
                found.add(item)
        resolved[name] = found
        return found

    for name in incoming:
        found = kinds(name, frozenset({name})) or {"state"}
        bound = set(BINDINGS["any"])
        for kind in found:
            bound &= set(BINDINGS["done" if kind == TRIGGER_DONE else "error" if kind == TRIGGER_ERROR
                                  else "event" if kind == "event" else "state"])
        fc.pseudo_bound[name] = frozenset(bound)


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _can_fail(fc: _FileContext, target: Optional[str], seen: frozenset[str]) -> bool:
    """Whether a transition into ``target`` can be disabled by a junction whose branches all fail (§3.2)."""
    state = fc.states.get(target or "")
    if state is None or state.type != "junction" or not state.transitions or target in seen:
        return False
    last = state.transitions[-1]
    if (last.guard or "").strip() not in ("", GUARD_ELSE):
        return True
    return _can_fail(fc, last.target, seen | {target})


def _accepts_somewhere(fc: _FileContext, name: str) -> bool:
    node: Optional[str] = name
    while node is not None:
        if any(t.trigger not in (TRIGGER_DONE, TRIGGER_ERROR) for t in fc.states[node].transitions):
            return True
        node = fc.parent[node]
    return False


def _check_state(fc: _FileContext, name: str, state: StateSpec, path: list[Any]) -> None:
    kind = state.type
    if state.finally_ is not None and kind != "state":
        fc.problem("error", "SG003", f"a {kind} state has no finally (only states that are entered and left do)",
                   path + ["finally"])
    if kind == "final":
        for key in ("do", "entry", "exit", "states", "max_visits", "initial", "timeout", "after"):
            if getattr(state, key):
                fc.problem("error", "SG003", f"a final state has no {key}", path + [key])
        if state.transitions:
            fc.problem("error", "SG003", "a final state has no outgoing transitions", path + ["transitions"])
        if state.status is not None and fc.parent[name] is not None:
            fc.problem("error", "SG003", "status belongs to finals of the root region; a nested final completes "
                                         "its composite", path + ["status"])
        if state.output is not None:
            _check_template(fc, state.output, path + ["output"], BINDINGS["state"], set())
        return
    if state.status is not None or state.output is not None:
        fc.problem("error", "SG003", "status and output belong to final states", path)
    if kind in ("choice", "junction"):
        for key in ("do", "entry", "exit", "states", "max_visits", "initial", "timeout", "after"):
            if getattr(state, key):
                fc.problem("error", "SG003", f"a {kind} pseudostate has no {key}", path + [key])
        if not state.transitions:
            fc.problem("error", "SG003", f"a {kind} needs outgoing transitions", path)
        for index, transition in enumerate(state.transitions):
            if transition.trigger != TRIGGER_DONE:
                fc.problem("error", "SG003", f"transitions of a {kind} have no trigger (they are taken at once)",
                           path + ["transitions", index, "trigger"])
            if transition.target is None:
                fc.problem("error", "SG003", f"transitions of a {kind} need a target", path + ["transitions", index])
        if kind == "choice" and (not state.transitions or (state.transitions[-1].guard or "").strip() != GUARD_ELSE):
            fc.problem("error", "SG003", "a choice ends with a transition whose guard is 'else'",
                       path + ["transitions"])
    if state.states:
        if state.do:
            fc.problem("error", "SG003", "a composite state has no do-activity (its nested states do the work)",
                       path + ["do"])
        if not state.initial:
            fc.problem("error", "SG003", "a composite state needs initial: <nested state>", path)
        elif state.initial not in state.states:
            fc.problem("error", "SG002", f"initial {state.initial!r} is not a direct child of {name!r}",
                       path + ["initial"])
    elif state.initial:
        fc.problem("error", "SG003", "initial belongs to composite states (with nested states)", path + ["initial"])

    if state.after is not None and kind == "state":
        if state.do is not None or state.states:
            fc.problem("error", "SG003", "after makes a timer state: one without do and without nested states (an "
                                         "activity has its own do.timeout)", path + ["after"])
        elif not any(t.trigger == TRIGGER_DONE for t in state.transitions):
            fc.problem("error", "SG003", f"{name!r} is a timer state (after) but has no completion transition (one "
                                         "without a trigger): nothing would go on when its time is up", path + ["after"])
        if state.timeout is not None:
            fc.problem("error", "SG003", "a timer state completes after its time (after); timeout raises wait_timeout "
                                         "in a wait state -- take one of them", path + ["timeout"])
    is_wait = state.is_wait
    if state.timeout is not None and not is_wait:
        fc.problem("error", "SG003", "timeout belongs to wait states (no do, no completion transition); an "
                                     "activity has its own do.timeout", path + ["timeout"])
    if is_wait and not _accepts_somewhere(fc, name):
        fc.problem("error", "SG003", f"{name!r} is a wait state (no do, no completion transition) but neither it "
                                     "nor an enclosing state accepts an event: it would wait forever", path)
    # completion is local (§3.3): the enclosing state's transitions do not take it. A warning, not an error: an
    # activity that only ever fails (its error transitions lead on) is a machine that works
    ends = ("its activity" if state.do is not None else
            "a final state inside it" if any(child.type == "final" for child in (state.states or {}).values()) else None)
    if kind == "state" and ends and not any(t.trigger == TRIGGER_DONE for t in state.transitions):
        fc.problem("warning", "SG109", f"{name!r} completes when {ends} ends but has no completion transition (one "
                                       "without a trigger): should it complete, the run fails with no_transition",
                   path + ["transitions"] if state.transitions else path)

    for key in ("entry", "exit"):
        source = getattr(state, key)
        if source:
            _check_code(fc, source, path + [key], mode="exec", bound=BINDINGS["state"])
    if state.do is not None:
        _check_activity(fc, state.do, path + ["do"], set())
    if state.finally_ is not None and kind == "state":
        _check_activity(fc, state.finally_, path + ["finally"], {"ending"})
    _check_transitions(fc, name, state, path)


def _check_transitions(fc: _FileContext, name: str, state: StateSpec, path: list[Any]) -> None:
    unguarded: dict[str, int] = {}
    for index, transition in enumerate(state.transitions):
        here = path + ["transitions", index]
        trigger = transition.trigger
        if trigger not in (TRIGGER_DONE, TRIGGER_ERROR) and trigger not in fc.spec.events:
            fc.problem("error", "SG002", f"trigger {trigger!r} is not a declared event (declare it under events:, "
                                         "or use done / error)", here + ["trigger"])
        if transition.target is None and trigger in (TRIGGER_DONE, TRIGGER_ERROR) and state.type == "state":
            fc.problem("error", "SG003", "a transition without target is an internal transition and needs a "
                                         "named-event trigger; completion and error transitions need a target", here)
        if transition.target is not None and transition.target not in fc.states:
            fc.problem("error", "SG002", f"transition target {transition.target!r} is not a state of this machine",
                       here + ["target"])
        guard = (transition.guard or "").strip()
        if guard == GUARD_ELSE and any(t.trigger == trigger for t in state.transitions[index + 1:]):
            fc.problem("error", "SG003", "'else' must be the last transition of its trigger", here + ["guard"])
        through_junction = _can_fail(fc, transition.target, frozenset())
        if trigger in unguarded:
            fc.problem("warning", "SG104",
                       f"never fires: transition {unguarded[trigger]} with the same trigger has no guard", here)
        elif (not guard or guard == GUARD_ELSE) and not through_junction:
            unguarded[trigger] = index  # a junction without else can leave the transition disabled
        bound = BINDINGS["done"] if trigger == TRIGGER_DONE else (
            BINDINGS["error"] if trigger == TRIGGER_ERROR else BINDINGS["event"])
        if state.type in ("choice", "junction"):
            bound = fc.pseudo_bound.get(name, BINDINGS["state"])  # what every way into it binds
        if guard and guard != GUARD_ELSE:
            _check_code(fc, guard, here + ["guard"], mode="eval", bound=bound)
        if transition.effect:
            _check_code(fc, transition.effect, here + ["effect"], mode="exec", bound=bound)


# ------------------------------------------------------------------ code

def _check_code(fc: _FileContext, source: str, path: list[Any], *, mode: str, bound: Iterable[str],
                extra: Iterable[str] = ()) -> None:
    if braced(source):
        fc.problem("error", "SG004", "code fields are plain Python: remove the {{ }}", path)
        return
    try:
        (compile_expression if mode == "eval" else compile_statements)(source, dotted(path))
        use = analyse(source, mode=mode, allowed=set(fc.loaded.namespace.names()) | set(extra))
    except CodeError as exc:
        fc.problem("error", "SG004", exc.message, path)
        return
    except SyntaxError as exc:
        fc.problem("error", "SG004", f"not Python: {exc.msg}", path)
        return
    for unknown in [] if fc.loaded.namespace.open else use.unknown:  # a star import may bring any name
        fc.problem("error", "SG004", f"unknown name {unknown!r} (in scope: ctx, params, out, error, event, run, "
                                     f"activity, the companion module, builtins)", path)
    unbound = sorted((use.loads & SCOPE_NAMES) - set(bound) - fc.always_bound - {"sg"})
    for name in unbound:
        fc.problem("error", "SG004", f"{name!r} is not bound here (out: completion transitions; error: error "
                                     "transitions; event: event transitions)", path)
    for name in sorted(use.params_reads - set(fc.spec.params)):
        fc.problem("error", "SG004", f"params has no field {name!r} (declared: {', '.join(fc.spec.params) or 'none'})",
                   path)
    for name in sorted(use.resources_reads - set(fc.spec.resources)):
        fc.problem("error", "SG004", f"resources has no {name!r} (declared: {', '.join(fc.spec.resources) or 'none'})",
                   path)
    if fc.open_resources is not None:
        for name in sorted((use.resources_reads & set(fc.spec.resources)) - fc.open_resources):
            fc.problem("error", "SG004", f"resources.{name} is not open yet here: a resource's open and fork see "
                                         "only the resources declared before it", path)
    for slip in use.misuse:
        fc.problem("error", "SG004", slip, path)
    for impure in use.impure:
        fc.problem("warning", "SG106", f"impure code: {impure} -- guards, actions and templates must be "
                                       "deterministic (replay re-runs them); read external state in an activity",
                   path)
    for field_name in use.ctx_reads:
        fc.ctx_reads.setdefault(field_name, path)
    fc.ctx_writes.update(use.ctx_writes)


def _check_template(fc: _FileContext, value: Any, path: list[Any], bound: Iterable[str], extra: set[str]) -> None:
    """Every leaf of a template value, each problem at the leaf that has it."""
    if isinstance(value, dict):
        for key, item in value.items():
            _check_template(fc, item, path + [key], bound, extra)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_template(fc, item, path + [index], bound, extra)
    elif isinstance(value, str):
        try:
            found = scan_template(value)
        except CodeError as exc:
            fc.problem("error", "SG004", exc.message, path)
            return
        for _, _, source in found:
            _check_code(fc, source, path, mode="eval", bound=bound, extra=extra)
        if _BARE_REFERENCE.match(value) and "{{" not in value:
            fc.problem("warning", "SG107", f"{value!r} is literal text here; did you mean {{{{ {value.strip()} }}}}?",
                       path)


def _check_schema(fc: _FileContext, schema: Any, path: list[Any], code: str) -> None:
    """A JSON schema the engine validates against at run time: an invalid one fails every value."""
    if not isinstance(schema, dict):
        return
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)  # what jsonschema.validate checks first
    except jsonschema.SchemaError as exc:
        fc.problem("error", code, f"not a valid JSON schema: {exc.message}", path + list(exc.path))


# ------------------------------------------------------------------ activities

def _check_vars_shape(fc: _FileContext, value: Any, path: list[Any]) -> None:
    """``vars`` (and ``llm_params``) is a map of templates, or ONE template (it must render to an object of names)."""
    if not isinstance(value, str):
        return
    try:
        expressions = list(template_expressions(value))
    except CodeError:
        return  # reported by the template check
    text = value.strip()
    if len(expressions) != 1 or not (text.startswith("{{") and text.endswith("}}")):
        fc.problem("error", "SG005", f"{path[-1]} must be a map, or exactly one {{{{ }}}} template that renders to "
                                     "an object", path)


def _check_activity(fc: _FileContext, raw: Any, path: list[Any], extra: set[str]) -> None:
    try:
        kind, spec = parse_activity(raw)
    except KindLookupError as exc:
        fc.problem("error", "SG005", str(exc), path)
        return
    except ValidationError as exc:
        def message(error: dict[str, Any]) -> str:
            if error.get("type") == "extra_forbidden":
                return f"unknown key {error['loc'][-1]!r} for a {kind_name(raw)}-activity"
            return str(error.get("msg", "invalid")).removeprefix("Value error, ")

        for loc, text in schema_problems(raw, exc.errors(), message):
            fc.problem("error", "SG005", text, path + loc)
        return

    bound = set(BINDINGS["state"]) | extra
    _check_schema(fc, raw.get("schema"), path + ["schema"], "SG005")
    for key in ("vars", "llm_params"):
        if key in kind.template_fields and key in raw:
            _check_vars_shape(fc, raw[key], path + [key])
    for key in kind.template_fields:
        if key in raw:
            _check_template(fc, raw[key], path + [key], bound, extra)
    for key in kind.code_fields:
        if key in raw and raw[key] is not None:
            field_bound = bound | {"out"} if key in ("error_if", "until") else bound
            field_extra = extra | set(kind.child_scope_names(spec)) if key == "until" else extra  # item, index
            _check_code(fc, str(raw[key]), path + [key], mode="eval", bound=field_bound, extra=field_extra)
    for key in ("parse", "call", "check"):
        reference = raw.get(key) if key in raw else None
        if isinstance(reference, str) and ":" not in reference and not fc.loaded.namespace.has_function(reference):
            fc.problem("error", "SG004", f"{key}: the companion module defines no function {reference!r}",
                       path + [key])

    child_extra = extra | set(kind.child_scope_names(spec))
    for key in kind.nested_one:
        if isinstance(raw.get(key), dict):
            _check_activity(fc, raw[key], path + [key], child_extra)
    for key in kind.nested_map:
        for label, child in (raw.get(key) or {}).items():
            _check_activity(fc, child, path + [key, label], child_extra)

    alias = kind.submachine(spec)
    if alias is not None:
        _check_submachine(fc, alias, getattr(spec, "params", {}) or {}, path)
    _check_references(fc, kind, spec, path)
    if spec.retry is not None and spec.retry.errors:
        for name in spec.retry.errors:
            if name not in KNOWN_ERROR_TYPES:
                fc.problem("warning", "SG110", f"retry.errors: {name!r} is not an error type the engine raises "
                                               f"(known: {', '.join(sorted(KNOWN_ERROR_TYPES))})",
                           path + ["retry", "errors"])
            elif name == "interrupted":
                fc.problem("warning", "SG110", "retry.errors: 'interrupted' is never retried -- a retry would start "
                                               "the interrupted activity a second time; mark that activity "
                                               "idempotent: true if running it again is safe",
                           path + ["retry", "errors"])


def kind_name(raw: Any) -> str:
    from plugins.stategraph.kinds import REGISTRY

    return next((key for key in raw if key in REGISTRY), "?") if isinstance(raw, dict) else "?"


def _check_submachine(fc: _FileContext, alias: str, params: dict[str, Any], path: list[Any]) -> None:
    target_path = fc.loaded.imports.get(alias)
    if target_path is None:
        if alias not in fc.spec.imports:
            fc.problem("error", "SG006", f"submachine {alias!r} is not imported; add imports: {{{alias}: ./file.yaml}}",
                       path + ["machine"])
        return
    target = fc.tree.files.get(target_path)
    if target is None or target.spec is None:
        return  # its own load problems are reported
    declared = target.spec.params
    for name in params:
        if name not in declared:
            fc.problem("error", "SG006", f"{alias} has no parameter {name!r} (it declares: "
                                         f"{', '.join(declared) or 'none'})", path + ["params", name])
    for name, param in declared.items():
        if param.required and param.default is None and name not in params:
            fc.problem("error", "SG006", f"{alias} requires parameter {name!r}", path + ["params"])


def _check_references(fc: _FileContext, kind: ActivityKind, spec: KindSpec, path: list[Any]) -> None:
    value = getattr(spec, kind.key, None)
    if kind.key == "callback" and value not in fc.spec.events:
        fc.problem("error", "SG006", f"callback: this machine declares no event {value!r} (events: "
                                     f"{', '.join(fc.spec.events) or 'none'})", path + ["callback"])
    if kind.key in ("agent", "tool") and isinstance(value, str) and "{{" in value and not param_ref(value):
        fc.problem("error", "SG005", f"{kind.key}: a kind value is a literal or {{{{ params.<name> }}}} with an "
                                     "enum -- a computed name would bypass the configuration check", path + [kind.key])
        return
    refs = kind.references(spec)
    for key, field in (("agent_param", kind.key), ("tool_param", kind.key), ("llm_profile_param", "llm_profile")):
        if key in refs:
            param = fc.spec.params.get(refs[key])
            if param is None or not param.enum:
                fc.problem("error", "SG005", f"{field}: {{{{ params.{refs[key]} }}}} needs a parameter "
                                             f"{refs[key]!r} with an enum (so the configuration check can see every "
                                             "value)", path + [field])
    if fc.config_check is None:
        return
    checks: list[tuple[str, str, dict[str, Any]]] = []
    if "agent" in refs:
        checks.append(("agent", refs["agent"], {}))
    if "agent_param" in refs and (param := fc.spec.params.get(refs["agent_param"])) and param.enum:
        checks.extend(("agent", str(value), {}) for value in param.enum)
    if "tool" in refs:
        checks.append(("tool", refs["tool"], {}))
    if "tool_param" in refs and (param := fc.spec.params.get(refs["tool_param"])) and param.enum:
        checks.extend(("tool", str(value), {}) for value in param.enum)
    if "profile" in refs:
        checks.append(("profile", refs["profile"], {}))
    if "llm_profile" in refs:
        checks.append(("llm_profile", refs["llm_profile"], {}))
    if "llm_profile_param" in refs and (param := fc.spec.params.get(refs["llm_profile_param"])) and param.enum:
        checks.extend(("llm_profile", str(value), {}) for value in param.enum)
    for what, name, extra in checks:
        problem = fc.config_check(what, name, extra)
        if problem:
            fc.problem("error", "SG007", problem, path + [kind.key])


# ------------------------------------------------------------------ graph

def _check_graph(fc: _FileContext) -> None:
    spec = fc.spec
    reach = nx.DiGraph()     # what can become active, starting at the initial state
    loops = nx.DiGraph()     # explicit transitions only: cycles = loops in the machine
    pseudo = nx.DiGraph()    # transitions between pseudostates only
    finals_top = {n for n, s in spec.states.items() if s.type == "final"}
    for name, state in fc.states.items():
        reach.add_node(name)
        loops.add_node(name)
        if state.states and state.initial in (state.states or {}):
            reach.add_edge(name, state.initial)
            loops.add_edge(name, state.initial)  # entering a composite enters its initial
        for transition in state.transitions:
            if transition.target and transition.target in fc.states:
                entered = _entered_chain(fc, name, transition.target)
                for a, b in zip([name] + entered[:-1], entered):
                    reach.add_edge(a, b)
                    loops.add_edge(a, b)
                if state.type in ("choice", "junction") and fc.states[transition.target].type in ("choice", "junction"):
                    pseudo.add_edge(name, transition.target)
    for component in nx.strongly_connected_components(pseudo):
        nodes = sorted(component)
        if len(nodes) > 1 or pseudo.has_edge(nodes[0], nodes[0]):
            fc.problem("error", "SG003", f"choice/junction states {', '.join(nodes)} form a cycle without a state "
                                         "in between (it would loop inside one step)", fc.paths[nodes[0]])
    exits = nx.DiGraph(reach)
    for name in fc.states:
        parent = fc.parent[name]
        while parent is not None:
            exits.add_edge(name, parent)
            parent = fc.parent[parent]
    active = _exits_from_active(reach, fc)
    if spec.initial in fc.states:
        reachable = nx.descendants(active, spec.initial) | {spec.initial}
        for name in fc.states:
            if name not in reachable:
                fc.problem("warning", "SG101", f"state {name!r} is unreachable from initial {spec.initial!r}",
                           fc.paths[name])
    if finals_top:
        can_finish: set[str] = set()
        for final in finals_top:
            can_finish |= nx.ancestors(exits, final) | {final}
        for name, state in fc.states.items():
            if name not in can_finish and state.type != "final":
                fc.problem("warning", "SG102", f"from {name!r} no path leads to a final state of the machine",
                           fc.paths[name])
    for component in nx.strongly_connected_components(loops):
        nodes = sorted(component)
        if len(nodes) == 1 and not loops.has_edge(nodes[0], nodes[0]):
            continue
        def bounds(node: str) -> bool:  # a re-entered enclosing composite resets its children's counts
            parent = fc.parent[node]
            while parent is not None:
                if parent in component:
                    return False
                parent = fc.parent[parent]
            return bool(fc.states[node].max_visits)

        if not any(bounds(n) for n in nodes):
            fc.problem("warning", "SG103",
                       f"loop through {', '.join(nodes)} has no max_visits that holds (a max_visits inside a "
                       "composite the loop re-enters is reset on every entry: bound the composite or a state "
                       "outside it)", fc.paths[nodes[0]])


def _entered_chain(fc: _FileContext, source: str, target: str) -> list[str]:
    """The states a transition source -> target enters: every ancestor of target below the domain, then target.

    The domain is the deepest state that is a PROPER ancestor of both (§3.2), so entering a nested state from
    outside enters (and re-enters) its composites -- which resets their children's visit counts.
    """
    def chain(name: str) -> list[str]:
        nodes = [name]
        parent = fc.parent.get(name)
        while parent is not None:
            nodes.insert(0, parent)
            parent = fc.parent.get(parent)
        return nodes

    target_chain = chain(target)
    source_ancestors = set(chain(source)[:-1])
    domain_index = -1
    for index, node in enumerate(target_chain[:-1]):
        if node in source_ancestors:
            domain_index = index
    return target_chain[domain_index + 1:]


def _exits_from_active(reach: nx.DiGraph, fc: _FileContext) -> nx.DiGraph:
    """Reachability where an active composite's transitions are available to its nested states."""
    graph = nx.DiGraph(reach)
    for name in fc.states:
        parent = fc.parent[name]
        while parent is not None:
            for _, target in reach.out_edges(parent):
                graph.add_edge(name, target)
            parent = fc.parent[parent]
    return graph


def _dedupe(tree: MachineTree) -> None:
    seen = set()
    unique = []
    for problem in tree.problems:
        key = (problem.level, problem.code, problem.message, problem.file, problem.path)
        if key not in seen:
            seen.add(key)
            unique.append(problem)
    tree.problems[:] = unique
