"""The stategraph machine format, version 1, as pydantic models.

One source of truth for the format: the loader validates YAML against these
models, ``schemas/machine.schema.json`` is generated from them, and the
panel's inspector reads that schema. The semantics are specified in
``docs/stategraph_design.md`` §2; this module only fixes the shape.

Activity mappings (``do:``) are kept as plain dicts here and validated by the
activity-kind registry (``kinds``), because plugins can add kinds.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORMAT_VERSION = 1

#: State names, machine ids, import aliases and parameter names. Check with ``fullmatch``: ``$`` alone would pass
#: a trailing newline (``"m\n"``).
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
_NAME = re.compile(NAME_PATTERN)

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}

#: A duration as written in YAML: ``30s``, ``10m``, ``2h``, ``500ms`` or seconds.
Duration = Union[int, float, str]

#: Trigger of a completion transition (the default).
TRIGGER_DONE = "done"
#: Trigger that catches errors raised in the state or below it.
TRIGGER_ERROR = "error"
#: Guard value that always holds; it must be the last transition of its state.
GUARD_ELSE = "else"


def parse_duration(value: Optional[Duration]) -> Optional[float]:
    """Seconds for a YAML duration; None stays None. Raises ValueError on garbage."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"duration must be a number or a string like '30s', not {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"duration must not be negative: {value!r}")
        return float(value)
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"duration {value!r}: write a number of seconds or '500ms', '30s', '10m', '2h'")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def check_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{what} {value!r} must match {NAME_PATTERN} (lowercase, digits, underscore)")
    return value


#: Activity paths use these words next to state names (``finally``, ``resources/<name>/open``).
RESERVED_STATE_NAMES = frozenset({"finally", "resources"})


def check_state_name(value: str) -> str:
    check_name(value, "state name")
    if value in RESERVED_STATE_NAMES:
        raise ValueError(f"state name {value!r} is reserved (the paths of finally and resource activities use it)")
    return value


class Strict(BaseModel):
    """Base for every format model: unknown keys are errors, never silently dropped."""

    model_config = ConfigDict(extra="forbid")


_PARAM_TYPES: dict[str, Any] = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                                "object": dict, "array": list}


class ParamSpec(Strict):
    """One machine parameter (run input or submachine parameter)."""

    type: Literal["string", "integer", "number", "boolean", "object", "array", "any"] = "any"
    required: bool = False
    default: Any = None
    enum: Optional[list[Any]] = None
    description: str = ""

    @model_validator(mode="after")
    def _default_fits(self) -> "ParamSpec":
        if self.default is None:
            return self
        expected = _PARAM_TYPES.get(self.type)
        wrong_bool = isinstance(self.default, bool) and self.type in ("integer", "number")
        if expected is not None and (wrong_bool or not isinstance(self.default, expected)):
            raise ValueError(f"default {self.default!r} is not a {self.type}")
        if self.enum is not None and self.default not in self.enum:
            raise ValueError(f"default {self.default!r} is not one of the enum {self.enum}")
        return self


#: Every error type the engine raises (docs/stategraph_design.md §3.5); plugins' kinds may add their own.
KNOWN_ERROR_TYPES = frozenset({
    "agent_failed", "schema_invalid", "parse_failed", "check_failed", "tool_failed", "tool_denied", "decision_failed",
    "call_failed", "submachine_failed", "join_failed", "activity_failed", "timeout", "interrupted", "template_failed", "params_invalid", "unmocked",
    "no_backend", "config", "loop_limit", "no_transition", "guard_failed", "action_failed", "wait_timeout",
    "not_serialisable"})

#: Error types a retry never repeats unless ``errors`` lists them (docs/stategraph_design.md §2.5).
NOT_RETRIED = frozenset({"cancelled", "interrupted", "timeout", "template_failed", "params_invalid",
                         "not_serialisable", "unmocked", "no_backend", "config", "tool_denied"})


class BackoffSpec(Strict):
    initial: Duration = 1
    factor: float = Field(2.0, ge=1.0, le=10.0)
    max: Duration = 60

    @field_validator("initial", "max")
    @classmethod
    def _durations(cls, value: Duration) -> Duration:
        parse_duration(value)
        return value


class RetrySpec(Strict):
    """Retries of an activity before its error becomes the state's error event."""

    attempts: int = Field(1, ge=1, le=20, description="total attempts, the first one included")
    backoff: Union[Duration, BackoffSpec] = Field(0, description="fixed pause, or {initial, factor, max}")
    errors: Optional[list[str]] = Field(None, description="error types to retry; default: all but NOT_RETRIED")

    @field_validator("backoff")
    @classmethod
    def _backoff(cls, value: Any) -> Any:
        if not isinstance(value, BackoffSpec):
            parse_duration(value)
        return value

    def delay(self, attempt: int) -> float:
        """Pause after the given failed attempt (1-based)."""
        if isinstance(self.backoff, BackoffSpec):
            initial = parse_duration(self.backoff.initial) or 0.0
            ceiling = parse_duration(self.backoff.max) or 0.0
            return min(ceiling, initial * self.backoff.factor ** (attempt - 1))
        return parse_duration(self.backoff) or 0.0

    def retries(self, error_type: str) -> bool:
        return error_type in self.errors if self.errors is not None else error_type not in NOT_RETRIED


class EventSpec(Strict):
    description: str = ""
    data: Optional[dict[str, Any]] = Field(None, description="JSON schema of the payload")


class TransitionSpec(Strict):
    """``trigger [guard] / effect -> target`` (UML). No target = internal transition."""

    target: Optional[str] = None
    trigger: str = Field(TRIGGER_DONE, description="'done' (completion, default), 'error', or an event name")
    guard: Optional[str] = Field(None, description="Python expression or 'else'")
    effect: Optional[str] = Field(None, description="Python statements")
    description: str = ""

    @field_validator("target")
    @classmethod
    def _target(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else check_name(value, "transition target")

    @field_validator("trigger")
    @classmethod
    def _trigger(cls, value: str) -> str:
        return check_name(value, "trigger")

    @field_validator("guard")
    @classmethod
    def _guard(cls, value: Optional[str]) -> Optional[str]:
        """A blank guard is no guard -- for the validator and the engine alike."""
        return value if value is None or value.strip() else None


class StateSpec(Strict):
    """A state or pseudostate. Which keys apply depends on ``type`` (checked by the validator)."""

    type: Literal["state", "choice", "junction", "final"] = "state"
    description: str = ""
    entry: Optional[str] = Field(None, description="Python statements, run on entry")
    exit: Optional[str] = Field(None, description="Python statements, run on exit")
    do: Optional[dict[str, Any]] = Field(None, description="the do-activity: exactly one kind key")
    transitions: list[TransitionSpec] = Field(default_factory=list)
    max_visits: Optional[int] = Field(None, ge=1)
    timeout: Optional[Duration] = Field(None, description="wait state only: raise wait_timeout after this long")
    after: Optional[Duration] = Field(
        None, description="timer state (no do, not composite): completes this long after it was entered -- its "
                          "completion transition goes on; an event it takes may come first")
    initial: Optional[str] = Field(None, description="composite state: the nested initial state")
    states: Optional[dict[str, "StateSpec"]] = Field(None, description="composite state: nested states")
    status: Optional[Literal["succeeded", "failed"]] = Field(None, description="final state of the root region only")
    output: Any = Field(None, description="final state only: template value")
    finally_: Optional[dict[str, Any]] = Field(
        None, alias="finally", description="an activity that runs once on every exit of this state, whatever the "
                                           "cause (transition, unhandled error, cancel); reads ending")

    @field_validator("timeout", "after")
    @classmethod
    def _timeout(cls, value: Optional[Duration]) -> Optional[Duration]:
        parse_duration(value)
        return value

    @field_validator("states")
    @classmethod
    def _state_names(cls, value: Optional[dict[str, "StateSpec"]]) -> Optional[dict[str, "StateSpec"]]:
        for name in value or {}:
            check_state_name(name)
        return value

    @property
    def is_composite(self) -> bool:
        return bool(self.states)

    @property
    def is_wait(self) -> bool:
        return is_wait_state(self.type, bool(self.states), self.do is not None, [t.trigger for t in self.transitions])


def is_wait_state(state_type: str, composite: bool, has_activity: bool, triggers: list[str]) -> bool:
    """A state that waits for named events: a simple state with no do-activity and no completion transition.

    The one definition for the engine, the validator and the editor's graph. An error transition does not make a
    state complete, and a state with no transition at all waits too (the validator says it waits forever).
    """
    return state_type == "state" and not composite and not has_activity and TRIGGER_DONE not in triggers


class ResourceSpec(Strict):
    """External state that belongs to one machine frame (docs/stategraph_design.md §2.8)."""

    description: str = ""
    open: dict[str, Any] = Field(description="activity run when the frame starts; its out is resources.<name>")
    fork: Optional[dict[str, Any]] = Field(
        None, description="activity a forked run's root frame runs instead of open; source is the source run's value")
    close: Optional[dict[str, Any]] = Field(
        None, description="activity run when the frame ends (not when the process stops); reads ending")


class LimitsSpec(Strict):
    max_steps: int = Field(1000, ge=1, description="macro steps per machine instance")
    timeout: Optional[Duration] = Field(None, description="wall time of a whole run")
    concurrency: Optional[int] = Field(
        None, ge=1, description="leaf activities (agent, tool, decide, call) of the whole run at once; the others "
                                "wait for their turn before they start")

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, value: Optional[Duration]) -> Optional[Duration]:
        parse_duration(value)
        return value


class LocalMachineSpec(Strict):
    """A machine inside another machine's file (``machines: {name: ...}``): ``machine: <name>`` runs it in its own
    frame. It shares that file's companion module and imports, and runs no other machine of the file."""

    title: str = ""
    description: str = ""
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    events: dict[str, EventSpec] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    vars: Union[dict[str, Any], str] = Field(default_factory=dict)
    vars_from: Optional[str] = None
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    resources: dict[str, "ResourceSpec"] = Field(default_factory=dict)
    finally_: Optional[dict[str, Any]] = Field(None, alias="finally")
    initial: str
    states: dict[str, StateSpec]


class MachineAgentSpec(Strict):
    """``agent:`` -- the machine offered as an agent (docs/stategraph_design.md §10): no config entry, every process
    that starts declares it (the stategraph_machine type's offer). The keys of a ``type: stategraph_machine`` entry."""

    name: Optional[str] = Field(None, description="the agent's name (a server name); default <machine id>_agent")
    description: str = Field("", description="what a caller reads; default the machine's title or description")
    input: Optional[Literal["text", "json"]] = Field(
        None, description="text: the message is the param task_param; json: a JSON object of the params; default "
                          "text when the machine has exactly one param of type string or any, else json")
    task_param: Optional[str] = Field(None, description="the param a text message fills; default that one param")
    on_wait: Literal["ask", "block"] = Field(
        "ask", description="ask: a wait state asks in the conversation (called as a tool it waits); block: the "
                           "request waits until the run ends")
    params: dict[str, Any] = Field(default_factory=dict, description="params every run of the agent gets")
    promote: list[str] = Field(default_factory=list, description="output keys its final event carries at the top")
    visibility: Literal["ui", "tool", "both", "private"] = Field(
        "private", description="private (default): only callers that name it; tool: a SAM may start it; ui: the chat "
                               "lists it; both")

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return check_name(value, "agent name") if value is not None else None


def agent_entry(spec: "MachineSpec", instance: str) -> tuple[str, dict[str, Any]]:
    """The agent a machine's ``agent:`` block offers: its name and the ``plugins.servers`` entry it stands for."""
    block = spec.agent or MachineAgentSpec()
    texts = [name for name, param in spec.params.items() if param.type in ("string", "any")]
    one = texts[0] if len(spec.params) == 1 and texts else None
    mode = block.input or ("text" if one else "json")
    entry: dict[str, Any] = {
        "type": "stategraph_machine", "enabled": True, "machine": spec.id, "stategraph": instance,
        "description": block.description or spec.title or spec.description or f"Runs the state machine {spec.id}",
        "input": mode, "on_wait": block.on_wait, "params": dict(block.params), "promote": list(block.promote),
        "metadata": {"visibility": block.visibility},
        "agent_config": {},  # an Agent needs one, a machine runs no LLM: the defaults (or the config's) will do
        "from_machine_file": True}  # offered by the block: a config entry of the same name is another thing
    if mode == "text":
        entry["task_param"] = block.task_param or one or "task"
    return block.name or f"{spec.id}_agent", entry


class MachineSpec(Strict):
    """One machine file."""

    stategraph: Literal[1] = Field(description="format version")
    id: str
    title: str = ""
    description: str = ""
    notes: dict[str, str] = Field(default_factory=dict, description="name -> free text for the reader, drawn as a note "
                                                                     "on the canvas; the engine ignores it")
    group: str = Field("", description="its folder in the panel's machine list, nested by / (Reviews/nightly); empty: "
                                       "the folder of where it comes from")
    python: Optional[str] = Field(None, description="companion module, relative to this file")
    imports: dict[str, str] = Field(default_factory=dict, description="alias -> ./relative.yaml or machine id")
    machines: dict[str, LocalMachineSpec] = Field(
        default_factory=dict, description="machines inside this file: machine: <name> runs one in its own frame; "
                                          "they share the companion module and the imports")
    agent: Optional[MachineAgentSpec] = Field(
        None, description="offer this machine as an agent: SAM spawns, AgentCaller and /events address it by name "
                          "-- declared by every process at its start, no config entry")
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    events: dict[str, EventSpec] = Field(default_factory=dict, description="named events this machine accepts")
    context: dict[str, Any] = Field(default_factory=dict)
    vars: Union[dict[str, Any], str] = Field(
        default_factory=dict, description="agent template vars: a map of templates, or one template that renders "
                                          "to an object of names")
    vars_from: Optional[str] = Field(None, description="agent whose configured template_vars lie under vars")
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    resources: dict[str, "ResourceSpec"] = Field(
        default_factory=dict, description="external state per machine frame: open at its start, close at its end")
    finally_: Optional[dict[str, Any]] = Field(
        None, alias="finally", description="an activity that runs once when the machine frame ends, whatever the "
                                           "cause (final state, failure, cancel); reads ending")
    initial: str
    states: dict[str, StateSpec]

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return check_name(value, "machine id")

    @field_validator("group")
    @classmethod
    def _group(cls, value: str) -> str:
        """Folder names without the blanks around them; a slash with nothing between is no folder."""
        return "/".join(part.strip() for part in value.split("/") if part.strip())

    @field_validator("imports")
    @classmethod
    def _imports(cls, value: dict[str, str]) -> dict[str, str]:
        for alias in value:
            check_name(alias, "import alias")
        return value

    @field_validator("machines")
    @classmethod
    def _machines(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            check_name(name, "machine name")
        return value

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            check_name(name, "note name")
        return value

    @field_validator("params", "context", "events")
    @classmethod
    def _names(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            check_name(name, "name")
            if name in (TRIGGER_DONE, TRIGGER_ERROR, "completion"):
                raise ValueError(f"{name!r} is reserved")
        return value

    @field_validator("states")
    @classmethod
    def _state_names(cls, value: dict[str, StateSpec]) -> dict[str, StateSpec]:
        if not value:
            raise ValueError("a machine needs at least one state")
        for name in value:
            check_state_name(name)
        return value

    @field_validator("resources")
    @classmethod
    def _resource_names(cls, value: dict[str, "ResourceSpec"]) -> dict[str, "ResourceSpec"]:
        for name in value:
            check_name(name, "resource name")
        return value


StateSpec.model_rebuild()
MachineSpec.model_rebuild()
