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

#: State names, machine ids, import aliases and parameter names.
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
    if not isinstance(value, str) or not _NAME.match(value):
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
    "agent_failed", "schema_invalid", "parse_failed", "tool_failed", "tool_denied", "decision_failed", "call_failed",
    "submachine_failed", "activity_failed", "timeout", "interrupted", "template_failed", "params_invalid", "unmocked",
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
    initial: Optional[str] = Field(None, description="composite state: the nested initial state")
    states: Optional[dict[str, "StateSpec"]] = Field(None, description="composite state: nested states")
    status: Optional[Literal["succeeded", "failed"]] = Field(None, description="final state of the root region only")
    output: Any = Field(None, description="final state only: template value")
    finally_: Optional[dict[str, Any]] = Field(
        None, alias="finally", description="an activity that runs once on every exit of this state, whatever the "
                                           "cause (transition, unhandled error, cancel); reads ending")

    @field_validator("timeout")
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

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, value: Optional[Duration]) -> Optional[Duration]:
        parse_duration(value)
        return value


class MachineSpec(Strict):
    """One machine file."""

    stategraph: Literal[1] = Field(description="format version")
    id: str
    title: str = ""
    description: str = ""
    python: Optional[str] = Field(None, description="companion module, relative to this file")
    imports: dict[str, str] = Field(default_factory=dict, description="alias -> ./relative.yaml or machine id")
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    events: dict[str, EventSpec] = Field(default_factory=dict, description="named events this machine accepts")
    context: dict[str, Any] = Field(default_factory=dict)
    vars: Union[dict[str, Any], str] = Field(
        default_factory=dict, description="agent template vars: a map of templates, or one template that renders "
                                          "to an object of names")
    vars_from: Optional[str] = Field(None, description="agent whose configured template_vars lie under vars")
    sam: Optional[str] = Field(None, description="default SAM instance for agent activities")
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

    @field_validator("imports")
    @classmethod
    def _imports(cls, value: dict[str, str]) -> dict[str, str]:
        for alias in value:
            check_name(alias, "import alias")
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
