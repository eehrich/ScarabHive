"""The built-in activity kinds: agent, tool, decide, call, machine, parallel, map."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import importlib
import inspect
import json
import re
from typing import TYPE_CHECKING, Any, Literal, Optional, Union

from pydantic import ConfigDict, Field, field_validator, model_validator

from plugins.stategraph.model.code import jsonable
from plugins.stategraph.model.spec import check_name
from plugins.stategraph.model.spec import Strict

from .base import ActivityError, ActivityKind, KindSpec, register, vars_object

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

    agent: str = Field(description="agent name: a literal, or {{ params.x }} with an enum; any configured agent")
    task: Any = Field(description="the task text (template)")
    schema_: Optional[dict[str, Any]] = Field(
        None, alias="schema", description="JSON schema: the answer is parsed as JSON and validated")
    parse: Optional[str] = Field(
        None, description="a companion function name (or package.module:function), fn(text) -> out; raising "
                          "ValueError sends its message back to the same instance as feedback (parse_retries times)")
    parse_retries: int = Field(1, ge=0, le=5, description="feedback rounds for schema/parse failures")
    vars: Union[dict[str, Any], str] = Field(
        default_factory=dict, description="agent template vars for this call, over the machine's: a map of "
                                          "templates, or one template that renders to an object of names")
    advanced: bool = Field(False, description="use the agent's advanced model profile")
    continue_: Optional[str] = Field(
        None, alias="continue", description="instance id (template) of this agent to follow up instead of spawning")

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
    summary = ("Run an agent (a new instance, or continue one) with a task; out = its answer (parsed with "
               "schema/parse); activity.instance_id = the instance for a later continue")

    def references(self, spec: AgentSpec) -> dict[str, str]:
        refs: dict[str, str] = {}
        if "{{" not in spec.agent:
            refs["agent"] = spec.agent
        elif param_ref(spec.agent):
            refs["agent_param"] = param_ref(spec.agent) or ""
        return refs

    def extra_inputs(self, spec: AgentSpec, act: "ActivityRun") -> dict[str, Any]:
        return {"frame_vars": act.frame_vars()}

    async def run(self, spec: AgentSpec, act: "ActivityRun") -> Any:
        agent = str(act.render(spec.agent, "agent"))
        task = act.text(act.render(spec.task, "task"))
        variables = {**act.frame_vars(), **vars_object(act.render(spec.vars, "vars"), f"{act.path}.vars")}
        act.meta["agent"] = agent
        if spec.continue_:
            instance: Optional[str] = str(act.render(spec.continue_, "continue"))
            text = await act.backend.agent_continue(act, agent=agent, instance_id=instance, message=task,
                                                    advanced=spec.advanced, vars=variables)
        else:
            text, instance = await act.backend.agent_create(act, agent=agent, task=task,
                                                            advanced=spec.advanced, vars=variables)
        act.meta["instance_id"] = instance
        if spec.schema_ is None and spec.parse is None:
            return text
        parser = resolve_callable(spec.parse, act, "parse") if spec.parse else None
        return await usable_answer(act, agent=agent, instance=instance, text=text, parser=parser,
                                   schema=spec.schema_, retries=spec.parse_retries, advanced=spec.advanced,
                                   variables=variables)


async def usable_answer(act: "ActivityRun", *, agent: str, instance: Optional[str], text: str, parser: Any,
                        schema: Optional[dict[str, Any]], retries: int, advanced: bool,
                        variables: dict[str, Any]) -> Any:
    """An agent's answer parsed (``parser``, else JSON) and checked against ``schema``; what fails goes back to the
    same instance as feedback, ``retries`` times, then fails the activity."""
    for round_ in range(retries + 1):
        value, problem = parse_answer(text, parser, schema)
        if problem is None:
            return value
        if round_ == retries or not instance:
            raise ActivityError("parse_failed" if parser else "schema_invalid", f"{agent}: {problem}",
                                data={"text": text[:4000], "instance_id": instance})
        act.meta["feedback_rounds"] = round_ + 1
        text = await act.backend.agent_continue(
            act, agent=agent, instance_id=instance, advanced=advanced, vars=variables,
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
    criteria: Any = Field(None, description="single question: its criteria -- choice: {option: meaning}; score: "
                                            "[lowest, ..., highest]", json_schema_extra={"x-yaml": True})
    questions: Optional[dict[str, QuestionSpec]] = Field(None, description="decide: questions -> name: question")
    profile: Optional[str] = Field(
        None, description="decision profile (llm_system.decision_profiles); default: the configured default")
    by: Optional[str] = Field(
        None, description="an agent that decides instead of a decision model: a literal, or {{ params.x }} with an "
                          "enum; it answers in JSON, checked like a schema (confidence and probabilities are null)")
    advanced: bool = Field(False, description="with by: use the agent's advanced model profile")
    parse_retries: int = Field(1, ge=0, le=5, description="with by: feedback rounds for an unusable answer")

    @model_validator(mode="after")
    def _shape(self) -> "DecideSpec":
        if self.by is not None:
            if self.profile is not None:
                raise ValueError("decide takes a decision profile or an agent (by:), not both")
            if "{{" in self.by and not param_ref(self.by):
                raise ValueError("by: a literal or {{ params.<name> }} with an enum -- a computed name would "
                                 "bypass the configuration check")
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
    template_fields = ("question", "criteria", "input", "questions", "by")
    title = "Decision"
    icon = "gauge"
    summary = ("Ask the configured decision model (or a decision profile), or an agent (by:); out = {value, "
               "confidence, probabilities} (confidence/probabilities may be null), or {name: answer} for "
               "decide: questions")

    def label(self, spec: DecideSpec) -> str:
        if spec.decide == "questions":
            text = f"questions: {', '.join(spec.questions or {})}"
        else:
            text = f"{spec.decide}: {spec.question if isinstance(spec.question, str) else ''}"
        return (f"{spec.by} · {text}" if spec.by else text)[:80]

    def references(self, spec: DecideSpec) -> dict[str, str]:
        if spec.by is not None:  # the agent activity's checks: configured, enabled, reaching no machine
            return {"agent_param": param_ref(spec.by) or ""} if "{{" in spec.by else {"agent": spec.by}
        return {"profile": spec.profile} if spec.profile else {}

    def extra_inputs(self, spec: DecideSpec, act: "ActivityRun") -> dict[str, Any]:
        return {"frame_vars": act.frame_vars()} if spec.by is not None else {}

    async def run(self, spec: DecideSpec, act: "ActivityRun") -> Any:
        state = act.plain(act.render(spec.input, "input"))
        if state in (None, "", [], {}):
            # the machine's own content, the same on every try: not a decision that failed (transient, retried)
            raise ActivityError("template_failed", "input is empty: a decision needs content to judge")
        if spec.decide == "questions":
            questions = {name: _question(act, q.type, q.question, q.criteria, f"questions.{name}")
                         for name, q in (spec.questions or {}).items()}
        else:
            questions = {"decision": _question(act, spec.decide, spec.question, spec.criteria, "question")}
        if spec.by is not None:
            answers = await _decide_by_agent(act, spec, questions, state)
        else:
            answers = await act.backend.decide(act, questions=questions, input=state, profile=spec.profile)
        return answers if spec.decide == "questions" else answers["decision"]


async def _decide_by_agent(act: "ActivityRun", spec: DecideSpec, questions: dict[str, dict[str, Any]],
                           content: Any) -> dict[str, dict[str, Any]]:
    """The questions as a task for an agent, its answer checked like a schema: the same out as a decision model's,
    without the confidence and the probabilities an agent cannot give."""
    agent = str(act.render(spec.by, "by"))
    # the criteria as a decision model gets them, JSON: an option `1:` or `true:` is "1" / "true" on both paths
    questions = jsonable(questions)
    schema = _decision_schema(questions)  # before the agent runs: criteria a template broke cost no call
    variables = act.frame_vars()
    act.meta["agent"] = agent
    text, instance = await act.backend.agent_create(act, agent=agent, task=_decision_task(questions, content),
                                                    advanced=spec.advanced, vars=variables)
    act.meta["instance_id"] = instance
    answer = await usable_answer(act, agent=agent, instance=instance, text=text, parser=None, schema=schema,
                                 retries=spec.parse_retries, advanced=spec.advanced, variables=variables)

    def value(name: str, question: dict[str, Any]) -> Any:
        # a decision model's score is the position on the scale, counted from 0: the agent names a level
        return question["criteria"].index(answer[name]) if question["type"] == "score" else answer[name]

    return {name: {"value": value(name, question), "confidence": None, "probabilities": None}
            for name, question in questions.items()}


def _decision_task(questions: dict[str, dict[str, Any]], content: Any) -> str:
    """What the agent is asked: every question with the form of its answer, then the content."""
    lines = ["Decide the following about the content below. Answer with one JSON object and nothing else, "
             "one key per question: {" + ", ".join(f'"{name}": ...' for name in questions) + "}", ""]
    for name, question in questions.items():
        criteria = question.get("criteria")
        lines.append(f'"{name}": {question["instructions"]}')
        if question["type"] == "noul":
            lines.append("  Answer: the probability that the answer is yes, a number from 0 to 1.")
            if isinstance(criteria, dict):
                lines.extend(f"  {key}: {meaning}" for key, meaning in criteria.items())
        elif question["type"] == "choice":
            lines.append("  Answer: exactly one of these options, as written:")
            lines.extend(f"  {json.dumps(option, ensure_ascii=False)}: {meaning}" for option, meaning in criteria.items())
        else:
            lines.append("  Answer: exactly one of these levels, as written (lowest first): "
                         + ", ".join(json.dumps(level, ensure_ascii=False) for level in criteria))
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
    return "\n".join([*lines, "", "Content:", body])


def _decision_schema(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The answer's schema: a probability for a noul, one of the options or levels (criteria rendered: a template
    can make them) otherwise."""
    def one(name: str, question: dict[str, Any]) -> dict[str, Any]:
        if question["type"] == "noul":
            return {"type": "number", "minimum": 0, "maximum": 1}
        criteria = question.get("criteria")
        shape = dict if question["type"] == "choice" else list
        if not isinstance(criteria, shape) or len(criteria) < 2:
            raise ActivityError("template_failed", f"{name}: the rendered criteria of a {question['type']} are not "
                                                   f"a {'mapping of options' if shape is dict else 'list of levels'}")
        return {"enum": list(criteria)}

    return {"type": "object", "required": list(questions), "additionalProperties": False,
            "properties": {name: one(name, question) for name, question in questions.items()}}


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
               "named sg receives the read-only scope and sg.tool() for journaled tool calls; out = its "
               "return value")

    async def run(self, spec: CallSpec, act: "ActivityRun") -> Any:
        """A sync function runs in a worker thread of ``_CALL_POOL``, so the loop -- terminate, timeouts, the lease
        heartbeat -- goes on meanwhile. A thread cannot be stopped: on a timeout or terminate the activity ends at
        once and the thread's late result is dropped, while the thread keeps its worker until it returns. Its
        ``sg`` works there except ``sg.tool()``, which a sync function can only return (it is awaited on the
        loop), not run."""
        fn = resolve_callable(spec.call, act, "call")
        args = act.render(spec.args, "args")
        params = list(inspect.signature(fn).parameters)
        sg = act.sg_api() if params[:1] == ["sg"] else None
        head = () if sg is None else (sg,)
        try:
            if inspect.iscoroutinefunction(fn):
                result = fn(*head, **args)
            else:  # the context vars go along (the run's request id and user), as with asyncio.to_thread
                run = functools.partial(contextvars.copy_context().run, _in_thread, fn, *head, **args)
                result = await asyncio.get_running_loop().run_in_executor(_CALL_POOL, run)
            return await result if inspect.isawaitable(result) else result
        finally:
            if sg is not None:
                sg._close()


def _in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """``fn`` in a worker thread. A StopIteration it raises never reaches the awaiting future -- asyncio cannot set
    it there, and the run would wait forever -- so it leaves as a coroutine turns it: a RuntimeError."""
    try:
        return fn(*args, **kwargs)
    except StopIteration as exc:
        raise RuntimeError(f"{getattr(fn, '__name__', 'the function')} raised StopIteration") from exc


#: Sync call functions run here, not in the loop's default executor, which the app shares (auth and others): a
#: hung call keeps one of these workers, never one of the app's.
# ponytail: one pool of 8 for every run of the process -- 8 hung calls make every further sync call wait (its
# timeout counts the wait); a pool per run, or a bigger one, if runs starve each other.
_CALL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="stategraph-call")

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

    A cancel ends the children; one that is still replaying replays on into the
    end its journal gives it (docs/stategraph_design.md §3.10), and one the crashed
    run had started that the cancel stopped before it began here replays into its
    end afterwards.
    """
    if fail == "fast":
        for label, _, _ in children:
            error = act.recorded_error(label)
            if error is not None:
                _name_child(error, label)
                await _end_branches(act, [child for child in children if child[0] != label])
                raise error
    gate = asyncio.Semaphore(concurrency)
    results: list[Any] = [None] * len(children)
    begun: set[str] = set()

    async def one(index: int, label: str, raw: dict[str, Any], extra: Optional[dict[str, Any]]) -> None:
        async with gate:
            begun.add(label)
            if fail == "fast":
                try:
                    results[index] = await act.child(label, raw, extra)
                except ActivityError as exc:
                    _name_child(exc, label)
                    raise
                return
            try:
                results[index] = {"status": "succeeded", "out": await act.child(label, raw, extra)}
            except ActivityError as exc:
                results[index] = {"status": "failed", "error": exc.as_dict()}

    tasks = [asyncio.ensure_future(one(i, *child)) for i, child in enumerate(children)]
    try:
        await asyncio.gather(*tasks)
    except BaseException as exc:
        from plugins.stategraph.engine.activity import ReplayDivergence, RunAbort

        for task in tasks:  # the rest ends; a branch's finally activities run to their end (§3.10)
            if not task.done() and not task.cancelling():
                task.cancel()
        # an abort (step_limit) or a divergence keeps its outcome: a terminate that comes while the rest ends
        # does not replace it -- only a halt does
        keep = isinstance(exc, (RunAbort, ReplayDivergence))
        while True:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                await _end_branches(act, [child for child in children if child[0] not in begun])
                break
            except asyncio.CancelledError:
                if not keep or act.run.stopping or act.run.lost:
                    raise
        raise exc
    return results


def _name_child(error: ActivityError, label: str) -> None:
    """``error.branch`` / ``error.index``: the child of THIS join that failed -- set, not defaulted, and the other
    one dropped, so a join nested in a branch or an item leaves no name of its own for the outer state to read."""
    branch = label.startswith("b.")
    error.extra.pop("index" if branch else "branch", None)
    error.extra["branch" if branch else "index"] = label[2:] if branch else int(label[2:])


async def _end_branches(act: "ActivityRun", children: list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]]) -> None:
    """Branches the crashed run had started that do not run here -- a fail-fast join had cancelled them, or a
    cancel stopped them before they began: each replays into its frame's end, never live, so its finally and
    close activities complete (docs/stategraph_design.md §3.10). A branch the crashed run never started has
    nothing to end. A cancel from outside meanwhile lets the ones that began end; the rest follow, then it
    raises on."""
    ending = [child for child in children if act.child_key(child[0]) in act.run.started]
    act.run.cancelled_below.update(act.child_key(child[0]) for child in ending)  # their endings are bounded
    arrived = False
    while ending and act.run.may_finalize():
        begun: set[str] = set()

        async def one(label: str, raw: dict[str, Any], extra: Optional[dict[str, Any]]) -> None:
            begun.add(label)
            await act.child(label, raw, extra, ending_only=True)

        tasks = [asyncio.ensure_future(one(*child)) for child in ending]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            arrived = True
            await asyncio.gather(*tasks, return_exceptions=True)
        ending = [child for child in ending if child[0] not in begun]
    if arrived:
        raise asyncio.CancelledError()
