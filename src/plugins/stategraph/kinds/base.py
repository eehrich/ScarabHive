"""Activity kinds: the extension point of stategraph.

A state's ``do:`` mapping names exactly one kind by its key (``agent:``,
``tool:``, ...). A kind brings three things: a pydantic model of its mapping
(validation, JSON schema, the panel's inspector), metadata for the palette,
and ``run`` -- the coroutine that performs the activity.

Adding a kind (docs: ``src/plugins/stategraph/docs/extending.md``)::

    from plugins.stategraph.kinds.base import ActivityKind, KindSpec, register

    class SleepSpec(KindSpec):
        sleep: float

    @register
    class SleepKind(ActivityKind):
        key = "sleep"
        spec_model = SleepSpec
        title = "Sleep"
        icon = "clock"
        summary = "Wait a number of seconds"

        async def run(self, spec, act):
            await asyncio.sleep(spec.sleep)
            return None

``run`` never touches the machine's context: an activity's only effect on the
machine is its result, which the engine journals, so a resumed run can replay
it without running the activity again (docs/stategraph_design.md §4.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from pydantic import Field, ValidationError, field_validator

from plugins.stategraph.model.spec import Duration, RetrySpec, Strict, parse_duration

if TYPE_CHECKING:
    from plugins.stategraph.engine.activity import ActivityRun


class KindSpec(Strict):
    """Keys every activity accepts, besides its own."""

    retry: Optional[RetrySpec] = None
    timeout: Optional[Duration] = Field(None, description="deadline of one attempt")
    idempotent: Optional[bool] = Field(
        None, description="may a resumed run start it again if it was in flight at the crash? "
                          "default: true, tool: false")
    description: str = ""

    @field_validator("timeout")
    @classmethod
    def _timeout(cls, value: Optional[Duration]) -> Optional[Duration]:
        parse_duration(value)
        return value


class ActivityError(Exception):
    """An activity failed; becomes the state's ``error`` event (``error.type``, ``error.message``, ...)."""

    def __init__(self, type_: str, message: str, data: Any = None, *, cause: Optional[dict[str, Any]] = None,
                 **extra: Any):
        super().__init__(message)
        self.type = type_
        self.message = message
        self.data = data
        self.cause = cause
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "message": self.message, "data": self.data, "cause": self.cause, **self.extra}

    @classmethod
    def from_dict(cls, error: dict[str, Any]) -> "ActivityError":
        extra = {k: v for k, v in error.items() if k not in ("type", "message", "data", "cause", "state")}
        return cls(str(error.get("type", "error")), str(error.get("message", "")), error.get("data"),
                   cause=error.get("cause"), **extra)


class ActivityKind(ABC):
    """One kind of do-activity. Subclasses set the class attributes and ``run``."""

    key: ClassVar[str]
    spec_model: ClassVar[type[KindSpec]]
    title: ClassVar[str]
    icon: ClassVar[str]
    summary: ClassVar[str]
    #: Keys whose values are templates (docs/stategraph_design.md §2.6); everything else is literal.
    template_fields: ClassVar[tuple[str, ...]] = ()
    #: Keys whose values are Python expressions (not templates).
    code_fields: ClassVar[tuple[str, ...]] = ()
    #: Whether a resumed run may start the activity again after a crash mid-flight (§5.5).
    default_idempotent: ClassVar[bool] = True
    #: Whether it reaches outside the process: a mock-only run refuses to run it unmocked.
    external: ClassVar[bool] = False
    #: Keys holding one nested activity (``each``) or a map of them (``parallel``).
    nested_one: ClassVar[tuple[str, ...]] = ()
    nested_map: ClassVar[tuple[str, ...]] = ()
    #: Names a nested activity may use in addition to the machine scope, per key.
    nested_scope: ClassVar[tuple[str, ...]] = ()

    def parse(self, raw: dict[str, Any]) -> KindSpec:
        return self.spec_model.model_validate(raw)

    def extra_inputs(self, spec: KindSpec, act: "ActivityRun") -> dict[str, Any]:
        """Inputs besides the template fields that the journal's input hash must cover (pure)."""
        return {}

    def idempotent(self, spec: KindSpec) -> bool:
        return self.default_idempotent if spec.idempotent is None else bool(spec.idempotent)

    def label(self, spec: KindSpec) -> str:
        """Subtitle on the canvas: the agent, tool, question ... the kind is about."""
        value = getattr(spec, self.key, "")
        return value if isinstance(value, str) else self.title

    def children(self, spec: KindSpec) -> list[tuple[str, dict[str, Any]]]:
        """Nested activities as ``(label, raw mapping)``; labels are unique within the activity."""
        found: list[tuple[str, dict[str, Any]]] = []
        for key in self.nested_one:
            raw = getattr(spec, key, None)
            if isinstance(raw, dict):
                found.append((key, raw))
        for key in self.nested_map:
            for label, raw in (getattr(spec, key, None) or {}).items():
                found.append((label, raw))
        return found

    def child_scope_names(self, spec: KindSpec) -> tuple[str, ...]:
        """Extra names bound for nested activities (``map``: the item variable and ``index``)."""
        return ()

    def submachine(self, spec: KindSpec) -> Optional[str]:
        """The import alias or machine id this activity instantiates, if any."""
        return None

    def references(self, spec: KindSpec) -> dict[str, str]:
        """Literal names the validator checks against the configuration: ``agent``, ``tool``, ``profile``, ``sam``."""
        return {}

    @abstractmethod
    async def run(self, spec: KindSpec, act: "ActivityRun") -> Any:
        """Perform the activity; return its result (``out``) or raise ``ActivityError``."""


REGISTRY: dict[str, ActivityKind] = {}


def register(cls: type[ActivityKind]) -> type[ActivityKind]:
    """Class decorator: make a kind available to every machine."""
    kind = cls()
    if kind.key in REGISTRY and type(REGISTRY[kind.key]) is not cls:
        raise ValueError(f"activity kind {kind.key!r} is already registered by {type(REGISTRY[kind.key]).__name__}")
    REGISTRY[kind.key] = kind
    return cls


class KindLookupError(ValueError):
    pass


def kind_of(raw: Any) -> ActivityKind:
    """The kind a ``do:`` mapping names; exactly one registered key must be present."""
    if not isinstance(raw, dict):
        raise KindLookupError(f"do must be a mapping with one kind key ({', '.join(sorted(REGISTRY))})")
    keys = [key for key in raw if key in REGISTRY]
    if not keys:
        raise KindLookupError(
            f"do names no activity kind; use exactly one of: {', '.join(sorted(REGISTRY))}")
    if len(keys) > 1:
        raise KindLookupError(f"do names several kinds ({', '.join(keys)}); an activity has exactly one")
    return REGISTRY[keys[0]]


def parse_activity(raw: Any) -> tuple[ActivityKind, KindSpec]:
    """Kind and validated spec, or ``KindLookupError`` / ``ValidationError``."""
    kind = kind_of(raw)
    return kind, kind.parse(raw)


def describe_kinds() -> list[dict[str, Any]]:
    """The palette: every kind with its JSON schema."""
    return [
        {"key": kind.key, "title": kind.title, "icon": kind.icon, "summary": kind.summary,
         "schema": kind.spec_model.model_json_schema(by_alias=True)}
        for kind in sorted(REGISTRY.values(), key=lambda k: k.key)
    ]


__all__ = [
    "ActivityError", "ActivityKind", "KindLookupError", "KindSpec", "REGISTRY",
    "ValidationError", "describe_kinds", "kind_of", "parse_activity", "register",
]


def vars_object(value: Any, where: str) -> dict[str, Any]:
    """Rendered agent vars: an object of names (a single-template ``vars`` must render to one)."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ActivityError("template_failed", f"{where} must render to an object of names, got {type(value).__name__}")
    return value
