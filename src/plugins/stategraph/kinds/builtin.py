"""The built-in activity kinds: agent, tool, decide, call, machine, parallel, map."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from plugins.stategraph.model.spec import check_name
from plugins.stategraph.model.spec import Strict

from .base import ActivityError, ActivityKind, KindSpec, register

if TYPE_CHECKING:
    from plugins.stategraph.engine.activity import ActivityRun

_PARAM_REF = re.compile(r"^\{\{\s*params\.([a-z][a-z0-9_]*)\s*\}\}$")


def param_ref(value: Any) -> Optional[str]:
    """``{{ params.x }}`` -> ``x`` (a kind value chosen by a parameter; the validator checks its enum)."""
    match = _PARAM_REF.match(value.strip()) if isinstance(value, str) else None
    return match.group(1) if match else None


# ------------------------------------------------------------------------ agent

class AgentSpec(KindSpec):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: str = Field(description="agent name: a literal, or {{ params.x }} with an enum; the SAM must allow it")
    task: Any = Field(description="the task text (template)")
    schema_: Optional[dict[str, Any]] = Field(
        None, alias="schema", description="JSON schema: the answer is parsed as JSON and validated")
    parse: Optional[str] = Field(
        None, description="a companion function name (or package.module:function), fn(text) -> out; raising "
                          "ValueError sends its message back to the same instance as feedback (parse_retries times)")
    parse_retries: int = Field(1, ge=0, le=5, description="feedback rounds for schema/parse failures")
    vars: dict[str, Any] = Field(
        default_factory=dict, description="agent template vars for this call (templates), over the machine's vars")
    sam: Optional[str] = Field(None, description="SAM instance; default: the machine's sam or the plugin's")
    advanced: bool = Field(False, description="use the agent's advanced model profile")
    continue_: Optional[str] = Field(
        None, alias="continue", description="instance id (template) to follow up instead of spawning")

    @field_validator("parse")
    @classmethod
    def _parse(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else check_callable_ref(value, "parse")


@register
class AgentKind(ActivityKind):
    key = "agent"
    external = True
    spec_model = AgentSpec
    template_fields = ("agent", "task", "vars", "continue")
    title = "Agent"
    icon = "brain"
    summary = ("Spawn (or continue) a sub-agent with a task; out = its answer (parsed with schema/parse); "
               "activity.instance_id = the instance for a later continue")

    def references(self, spec: AgentSpec) -> dict[str, str]:
        refs: dict[str, str] = {}
        if "{{" not in spec.agent:
            refs["agent"] = spec.agent
        elif param_ref(spec.agent):
            refs["agent_param"] = param_ref(spec.agent) or ""
        if spec.sam:
            refs["sam"] = spec.sam
        return refs

    def extra_inputs(self, spec: AgentSpec, act: "ActivityRun") -> dict[str, Any]:
        return {"frame_vars": act.frame_vars()}

    async def run(self, spec: AgentSpec, act: "ActivityRun") -> Any:
        agent = str(act.render(spec.agent, "agent"))
        task = act.text(act.render(spec.task, "task"))
        variables = {**act.frame_vars(), **act.render(spec.vars, "vars")}
        sam = spec.sam or act.default_sam
        act.meta["agent"] = agent
        if spec.continue_:
            instance: Optional[str] = str(act.render(spec.continue_, "continue"))
            text = await act.backend.agent_continue(act, instance_id=instance, message=task, sam=sam,
                                                    advanced=spec.advanced, vars=variables)
        else:
            text, instance = await act.backend.agent_create(act, agent=agent, task=task, sam=sam,
                                                            advanced=spec.advanced, vars=variables)
        act.meta["instance_id"] = instance
        if spec.schema_ is None and spec.parse is None:
            return text
        parser = resolve_callable(spec.parse, act, "parse") if spec.parse else None
        for round_ in range(spec.parse_retries + 1):
            value, problem = parse_answer(text, parser, spec.schema_)
            if problem is None:
                return value
            if round_ == spec.parse_retries or not instance:
                raise ActivityError("parse_failed" if parser else "schema_invalid", f"{agent}: {problem}",
                                    data={"text": text[:4000], "instance_id": instance})
            act.meta["feedback_rounds"] = round_ + 1
            text = await act.backend.agent_continue(
                act, instance_id=instance, sam=sam, advanced=spec.advanced, vars=variables,
                message=f"Your answer could not be used: {problem}\nAnswer again, correcting exactly this.")
        raise AssertionError("unreachable")


def parse_answer(text: str, parser: Any, schema: Optional[dict[str, Any]]) -> tuple[Any, Optional[str]]:
    """``(value, None)`` or ``(None, problem)``; a parser rejects an answer by raising ValueError."""
    from plugins.stategraph.engine.backend import validate_answer

    try:
        if parser is not None:
            value = parser(text)
        else:
            from agent_system.core.agent_caller import parse_json_value

            value = parse_json_value(text)
    except ValueError as exc:
        return None, str(exc) or "the answer could not be parsed"
    if schema is not None:
        problem = validate_answer(value, schema)
        if problem:
            return None, f"the JSON does not match the required schema: {problem}"
    return value, None


# ------------------------------------------------------------------------- tool

class ToolSpec(KindSpec):
    tool: str = Field(description="flat tool name as agents see it, e.g. json_store_read")
    args: dict[str, Any] = Field(default_factory=dict, description="arguments (templates)")
    error_if: Optional[str] = Field(
        None, description="Python expression over out: true makes the result an error (type tool_failed)")


@register
class ToolKind(ActivityKind):
    key = "tool"
    external = True
    spec_model = ToolSpec
    template_fields = ("tool", "args")
    title = "Tool"
    icon = "wrench"
    summary = "Call a tool directly, no LLM; the runner agent's allowlist decides what is callable"
    code_fields = ("error_if",)
    default_idempotent = False

    def references(self, spec: ToolSpec) -> dict[str, str]:
        if "{{" not in spec.tool:
            return {"tool": spec.tool}
        return {"tool_param": param_ref(spec.tool) or ""} if param_ref(spec.tool) else {}

    async def run(self, spec: ToolSpec, act: "ActivityRun") -> Any:
        tool = str(act.render(spec.tool, "tool"))
        args = act.render(spec.args, "args")
        result = await act.backend.call_tool(act, tool=tool, args=args)
        if spec.error_if and act.evaluate(spec.error_if, "error_if", out=result):
            raise ActivityError("tool_failed", f"{tool}: error_if holds ({spec.error_if})", data=act.plain(result))
        return result


# ----------------------------------------------------------------------- decide

class QuestionSpec(Strict):
    type: Literal["noul", "choice", "score"]
    question: Any = Field(description="what to decide (template); sent as the question's instructions")
    criteria: Any = Field(None, description="choice: {option: meaning} (>=2); score: [lowest, ..., highest] (>=2)")


class DecideSpec(KindSpec):
    decide: Literal["noul", "choice", "score", "questions"] = Field(
        description="noul: probability of yes; choice: one option of criteria; score: a point on criteria's "
                    "scale; questions: several named questions in one call")
    input: Any = Field(description="the content to judge (template: text or object, not empty)")
    question: Any = Field(None, description="single question (template)")
    criteria: Any = Field(None, description="single question: its criteria")
    questions: Optional[dict[str, QuestionSpec]] = Field(None, description="decide: questions -> name: question")
    profile: Optional[str] = Field(None, description="decision profile; default: the configured one (jev)")

    @model_validator(mode="after")
    def _shape(self) -> "DecideSpec":
        if self.decide == "questions":
            if not self.questions:
                raise ValueError("decide: questions needs questions: {name: {type, question, criteria}}")
            if self.question is not None or self.criteria is not None:
                raise ValueError("decide: questions takes its questions from questions:, not question/criteria")
        else:
            if self.question is None:
                raise ValueError(f"decide: {self.decide} needs question:")
            if self.questions is not None:
                raise ValueError("questions: belongs to decide: questions")
        for name, question in (self.questions or {}).items():
            check_name(name, "question name")
            _check_criteria(question.type, question.criteria, name)
        if self.decide != "questions":
            _check_criteria(self.decide, self.criteria, "question")
        return self


def _check_criteria(kind: str, criteria: Any, name: str) -> None:
    if isinstance(criteria, str) and "{{" in criteria:
        return
    if kind == "choice" and not (isinstance(criteria, dict) and len(criteria) >= 2):
        raise ValueError(f"{name}: a choice needs criteria {{option: meaning}} with at least two options")
    if kind == "score" and not (isinstance(criteria, list) and len(criteria) >= 2):
        raise ValueError(f"{name}: a score needs criteria [lowest, ..., highest] with at least two levels")
    if kind == "noul" and criteria is not None and not isinstance(criteria, dict):
        raise ValueError(f"{name}: a noul's criteria, when given, is a mapping like {{true: ..., false: ...}}")


@register
class DecideKind(ActivityKind):
    key = "decide"
    external = True
    spec_model = DecideSpec
    template_fields = ("question", "criteria", "input", "questions")
    title = "Decision"
    icon = "gauge"
    summary = ("Ask a calibrated decision model (Jev); out = {value, confidence, probabilities} "
               "(confidence/probabilities may be null), or {name: answer} for decide: questions")

    def label(self, spec: DecideSpec) -> str:
        if spec.decide == "questions":
            return f"questions: {', '.join(spec.questions or {})}"[:80]
        question = spec.question if isinstance(spec.question, str) else ""
        return f"{spec.decide}: {question}"[:80]

    def references(self, spec: DecideSpec) -> dict[str, str]:
        return {"profile": spec.profile} if spec.profile else {}

    async def run(self, spec: DecideSpec, act: "ActivityRun") -> Any:
        state = act.plain(act.render(spec.input, "input"))
        if state in (None, "", [], {}):
            raise ActivityError("decision_failed", "input is empty: a decision needs content to judge")
        if spec.decide == "questions":
            questions = {name: _question(act, q.type, q.question, q.criteria, f"questions.{name}")
                         for name, q in (spec.questions or {}).items()}
        else:
            questions = {"decision": _question(act, spec.decide, spec.question, spec.criteria, "question")}
        answers = await act.backend.decide(act, questions=questions, input=state, profile=spec.profile)
        return answers if spec.decide == "questions" else answers["decision"]


def _question(act: "ActivityRun", kind: str, question: Any, criteria: Any, where: str) -> dict[str, Any]:
    built = {"type": kind, "instructions": act.text(act.render(question, where))}
    if criteria is not None:
        built["criteria"] = act.plain(act.render(criteria, f"{where}.criteria"))
    return built


# ------------------------------------------------------------------------- call

class CallSpec(KindSpec):
    call: str = Field(description="a companion function name, or package.module:function")
    args: dict[str, Any] = Field(default_factory=dict, description="keyword arguments (templates)")

    @field_validator("call")
    @classmethod
    def _call(cls, value: str) -> str:
        return check_callable_ref(value, "call")


@register
class CallKind(ActivityKind):
    key = "call"
    spec_model = CallSpec
    template_fields = ("args",)
    title = "Python"
    icon = "terminal"
    summary = ("Call a Python function (sync or async) with rendered keyword arguments; a first parameter "
               "named sg receives the read-only scope; out = its return value")

    async def run(self, spec: CallSpec, act: "ActivityRun") -> Any:
        fn = resolve_callable(spec.call, act, "call")
        args = act.render(spec.args, "args")
        params = list(inspect.signature(fn).parameters)
        result = fn(act.frozen_scope(), **args) if params[:1] == ["sg"] else fn(**args)
        if inspect.isawaitable(result):
            result = await result
        return result


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_REF = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def check_callable_ref(value: str, what: str) -> str:
    """A companion function name (``build_task``) or ``package.module:function``."""
    if not (_IDENT.match(value) or _MODULE_REF.match(value)):
        raise ValueError(f"{what} must name a companion function (build_task) or package.module:function")
    return value


def resolve_callable(ref: str, act: "ActivityRun", where: str) -> Any:
    if ":" not in ref:
        try:
            return act.namespace.function(ref, f"{act.path}.do.{where}")
        except Exception as exc:
            raise ActivityError("config", str(exc)) from exc
    module_name, _, attr = ref.partition(":")
    try:
        fn = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ActivityError("config", f"cannot import {ref}: {exc}") from exc
    if not callable(fn):
        raise ActivityError("config", f"{ref} is not callable")
    return fn


# ---------------------------------------------------------------------- machine

class SubmachineSpec(KindSpec):
    machine: str = Field(description="import alias of the submachine")
    params: dict[str, Any] = Field(default_factory=dict, description="parameters (templates)")

    @field_validator("machine")
    @classmethod
    def _machine(cls, value: str) -> str:
        return check_name(value, "submachine reference")


@register
class MachineKind(ActivityKind):
    key = "machine"
    spec_model = SubmachineSpec
    template_fields = ("params",)
    title = "Submachine"
    icon = "layers"
    summary = ("Run an imported machine with parameters in its own frame; out = the output of its final "
               "state (a failed final raises error submachine_failed with error.data = that output)")

    def submachine(self, spec: SubmachineSpec) -> Optional[str]:
        return spec.machine

    async def run(self, spec: SubmachineSpec, act: "ActivityRun") -> Any:
        params = act.render(spec.params, "params")
        return await act.run_submachine(spec.machine, params)


# --------------------------------------------------------------------- parallel

class ParallelSpec(KindSpec):
    parallel: dict[str, dict[str, Any]] = Field(description="branch name -> activity")
    fail: Literal["fast", "collect"] = Field(
        "fast", description="fast: the first failure cancels the rest and is the error; "
                            "collect: out[branch] = {status, out | error}")

    @field_validator("parallel")
    @classmethod
    def _branches(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(value) < 1:
            raise ValueError("parallel needs at least one branch")
        for name in value:
            check_name(name, "branch name")
        return value


@register
class ParallelKind(ActivityKind):
    key = "parallel"
    spec_model = ParallelSpec
    title = "Parallel"
    icon = "git-branch"
    summary = "Run several activities at once and join; out = {branch: out}"
    nested_map = ("parallel",)

    def label(self, spec: ParallelSpec) -> str:
        return ", ".join(spec.parallel)

    async def run(self, spec: ParallelSpec, act: "ActivityRun") -> Any:
        labels = list(spec.parallel)
        results = await join(act, [(f"b.{label}", spec.parallel[label], None) for label in labels], spec.fail,
                             concurrency=len(labels))
        return dict(zip(labels, results))


# -------------------------------------------------------------------------- map

class MapSpec(KindSpec):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    map: str = Field(description="Python expression giving the list of items")
    as_: str = Field("item", alias="as", description="name of the item variable in each's templates")
    each: dict[str, Any] = Field(description="the activity run per item")
    concurrency: int = Field(1, ge=1, le=64, description="items at once; 1 = strictly in list order")
    fail: Literal["fast", "collect"] = Field("fast", description="as in parallel")

    @field_validator("as_")
    @classmethod
    def _as(cls, value: str) -> str:
        from plugins.stategraph.model.code import SCOPE_NAMES

        if value in SCOPE_NAMES or value == "index":
            raise ValueError(f"as: {value!r} would shadow a name in scope; pick another item name")
        return check_name(value, "map variable")


@register
class MapKind(ActivityKind):
    key = "map"
    spec_model = MapSpec
    title = "Map"
    icon = "list-todo"
    summary = "Run one activity per item of a list; out = list of results in item order"
    code_fields = ("map",)
    nested_one = ("each",)

    def label(self, spec: MapSpec) -> str:
        return f"for {spec.as_} in {spec.map}"[:80]

    def child_scope_names(self, spec: MapSpec) -> tuple[str, ...]:
        return (spec.as_, "index")

    def extra_inputs(self, spec: MapSpec, act: "ActivityRun") -> dict[str, Any]:
        return {"items": act.plain(act.evaluate(spec.map, "map"))}

    async def run(self, spec: MapSpec, act: "ActivityRun") -> Any:
        items = act.evaluate(spec.map, "map")
        items = list(act.plain(items)) if items is not None else []
        return await join(act, [(f"i.{i}", spec.each, {spec.as_: item, "index": i}) for i, item in enumerate(items)],
                          spec.fail, concurrency=spec.concurrency)


async def join(act: "ActivityRun", children: list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]],
               fail: str, *, concurrency: int) -> list[Any]:
    """Run children with a concurrency bound; results in child order.

    ``fail: fast`` -- a replayed failure ends the join before anything runs live
    (so replay reproduces the original outcome), and a live failure cancels the
    rest. ``fail: collect`` -- every child runs; a result is {status, out|error}.
    With ``concurrency`` 1 the children run strictly one after another in order.
    """
    if fail == "fast":
        for label, _, _ in children:
            error = act.recorded_error(label)
            if error is not None:
                error.extra.setdefault("branch" if label.startswith("b.") else "index",
                                       label[2:] if label.startswith("b.") else int(label[2:]))
                raise error
    gate = asyncio.Semaphore(concurrency)
    results: list[Any] = [None] * len(children)

    async def one(index: int, label: str, raw: dict[str, Any], extra: Optional[dict[str, Any]]) -> None:
        async with gate:
            if fail == "fast":
                try:
                    results[index] = await act.child(label, raw, extra)
                except ActivityError as exc:
                    exc.extra.setdefault("branch" if label.startswith("b.") else "index",
                                         label[2:] if label.startswith("b.") else int(label[2:]))
                    raise
                return
            try:
                results[index] = {"status": "succeeded", "out": await act.child(label, raw, extra)}
            except ActivityError as exc:
                results[index] = {"status": "failed", "error": exc.as_dict()}

    tasks = [asyncio.ensure_future(one(i, *child)) for i, child in enumerate(children)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return results
