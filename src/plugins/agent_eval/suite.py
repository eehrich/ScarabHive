"""The suite file: which agent, which arms, which tasks, how each is checked.

A suite is YAML next to its task material (workspace templates, check scripts); every relative path in it
resolves against the suite file's directory. See agent_eval.guide for the format.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_system.config.layers import _resolve_relative_paths
from agent_system.paths import PROJECT_ROOT
from agent_system.utils import yaml_io

#: Ids end up in directory names and request ids.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,39}")


class Check(BaseModel):
    """How a finished run is scored. Exactly one kind per check."""

    model_config = ConfigDict(extra="forbid")

    #: Shell command in the workspace; exit 0 passes.
    command: Optional[str] = None
    #: pytest arguments (a path, a node id), run in the workspace; the score is the share of passed tests.
    pytest: Optional[str] = None
    #: Python script (relative to the suite) called as ``script <workspace> <run.json>``; prints
    #: ``{"score": 0..1, "passed": bool, "detail": "..."}``.
    script: Optional[str] = None
    #: ``module:function`` called in the runner as ``function(workspace, run, task)`` and returning the same
    #: dict -- the way a plugin brings checks of its own.
    python: Optional[str] = None
    #: Plain expectations on the final answer: every ``contains`` string and every ``regex`` must match.
    expect: Optional[Dict[str, List[str]]] = None
    timeout_s: float = 600

    @model_validator(mode="after")
    def _one_kind(self) -> "Check":
        kinds = [k for k in ("command", "pytest", "script", "python", "expect") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError(f"a check needs exactly one of command, pytest, script, python, expect (got {kinds or 'none'})")
        if self.expect is not None and not set(self.expect) <= {"contains", "regex"}:
            raise ValueError("expect takes 'contains' and 'regex' lists only")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in ("command", "pytest", "script", "python", "expect") if getattr(self, k))


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    input: str
    check: Check
    #: learn: what tuning may look at; regression: must keep passing; holdout: evidence only.
    kind: Literal["learn", "regression", "holdout"] = "learn"
    #: Directory copied fresh into every run's workspace.
    workspace: Optional[str] = None
    #: Command run ONCE per task (in a copy of the workspace template, with the task's own data directory);
    #: its result is frozen and every run gets a copy, so all arms start from the same state.
    setup: Optional[str] = None
    #: Task material for every arm (template_vars this task's prompt needs, say): merged over the agent's
    #: agent_config BEFORE the arm's overlay, so the arm still decides what it changes.
    agent_config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError(f"task id {value!r}: letters, digits and '-', at most 40")
        return value


class Arm(BaseModel):
    """One variant of the agent. Empty = the agent as configured."""

    model_config = ConfigDict(extra="forbid")

    #: Another agent than the suite's.
    agent: Optional[str] = None
    #: Merged over the agent's agent_config like a config entry over the one it inherits from
    #: (``+x``/``!x`` in lists). An admin overlay: anything the agent YAML may set.
    agent_config: Dict[str, Any] = Field(default_factory=dict)


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    agent: str
    description: str = ""
    repeats: int = Field(3, ge=1, le=50)
    #: Seconds one agent run may take before it is killed.
    timeout_s: float = 1800
    #: fresh: every run (or chain) gets an empty data directory; shared: the installation's own.
    data: Literal["fresh", "shared"] = "fresh"
    #: Runs at the same time.
    parallel: int = Field(1, ge=1, le=16)
    #: Stop starting runs once their cost reaches this many dollars.
    budget_usd: Optional[float] = None
    #: Run each arm's tasks in order in one shared workspace and data directory per repeat.
    chain: bool = False
    #: Arm names to overlays; the first is the reference the others are compared against.
    arms: Dict[str, Arm] = Field(default_factory=lambda: {"base": Arm()})
    tasks: List[Task]

    #: The suite file's directory, set by load_suite.
    root: Path = Field(default=Path("."), exclude=True)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError(f"suite name {value!r}: letters, digits and '-', at most 40")
        return value

    @field_validator("arms")
    @classmethod
    def _arms(cls, value: Dict[str, Arm]) -> Dict[str, Arm]:
        if not value:
            raise ValueError("a suite needs at least one arm")
        bad = [name for name in value if not _ID.fullmatch(name)]
        if bad:
            raise ValueError(f"arm names {bad}: letters, digits and '-', at most 40")
        return value

    @model_validator(mode="after")
    def _tasks(self) -> "Suite":
        if not self.tasks:
            raise ValueError("a suite needs at least one task")
        ids = [t.id for t in self.tasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {dupes}")
        return self

    def path(self, value: str) -> Path:
        """A path the suite names, resolved against its directory."""
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def reference_arm(self) -> str:
        return next(iter(self.arms))


def discover_suites(plugin_dirs: List[str], project_root: Path) -> List[Path]:
    """Every suite file the panel offers: ``suite.yaml`` below an ``evals/`` directory -- the project's own and
    each plugin's (``<plugin root>/<plugin>/evals/``). A plugin brings its suites the way it brings agents."""
    roots = [project_root / "evals"] + [p for d in plugin_dirs for p in Path(d).glob("*/evals") if p.is_dir()]
    return sorted({f.resolve() for r in roots if r.is_dir() for f in r.rglob("suite.yaml")})


def load_suite(path: str | Path) -> Suite:
    """The suite at *path*. Raises ValueError with the file name on a broken one."""
    path = Path(path).resolve()
    try:
        data = yaml_io.safe_load(path.read_text(encoding="utf-8")) or {}
        suite = Suite.model_validate(data)
    except Exception as e:
        raise ValueError(f"{path}: {e}") from e
    suite.root = path.parent
    for task in suite.tasks:
        for value in (task.workspace, task.check.script):
            if value and not suite.path(value).exists():
                raise ValueError(f"{path}: task {task.id}: {value} does not exist")
    # A './' path in an overlay means the suite's directory, as in an agent YAML it means the YAML's -- the run
    # would otherwise look for it in its own workspace.
    owners = [(f"arm {name}", arm.agent_config) for name, arm in suite.arms.items()]
    owners += [(f"task {task.id}", task.agent_config) for task in suite.tasks]
    for owner, overlay in owners:
        _resolve_relative_paths(overlay, suite.root)
        template = overlay.get("system_template")
        # Any other relative path stays what it is in a YAML: the project's, where the child runs.
        if isinstance(template, str) and not (PROJECT_ROOT / template).is_file():
            raise ValueError(f"{path}: {owner}: system_template {template} does not exist")
    return suite
