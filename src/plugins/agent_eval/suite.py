"""The suite file: which agent, which arms, which tasks, how each is checked.

A suite is YAML next to its task material (workspace templates, check scripts); every relative path in it
resolves against the suite file's directory. See agent_eval.guide for the format.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from agent_system.config.layers import _resolve_relative_paths
from agent_system.paths import PROJECT_ROOT
from agent_system.utils import yaml_io

#: Ids end up in directory names and request ids.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,39}")
#: The kinds of check, in the order a check names them.
_KINDS = ("command", "pytest", "script", "python", "expect", "judge")


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
    #: The suite's judge, after all runs: the ids of its criteria this task is judged by, or true for all.
    judge: Union[bool, List[str], None] = None
    timeout_s: float = 600

    @model_validator(mode="after")
    def _one_kind(self) -> "Check":
        kinds = [k for k in _KINDS if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError(f"a check needs exactly one of {', '.join(_KINDS)} (got {kinds or 'none'})")
        if self.expect is not None and not set(self.expect) <= {"contains", "regex"}:
            raise ValueError("expect takes 'contains' and 'regex' lists only")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in _KINDS if getattr(self, k))


class Product(BaseModel):
    """What the judge reads of a run; without one, the agent's final answer."""

    model_config = ConfigDict(extra="forbid")

    #: Python script (relative to the suite) called as ``script <workspace> <run.json>``; what it prints is the
    #: product.
    script: Optional[str] = None
    #: ``module:function`` called in the runner as ``function(workspace, run, task)``, returning the product text.
    python: Optional[str] = None
    timeout_s: float = 600

    @model_validator(mode="after")
    def _one_kind(self) -> "Product":
        if bool(self.script) == bool(self.python):
            raise ValueError("a product needs exactly one of script, python")
        return self


class Judge(BaseModel):
    """Scoring by a model, after all runs: per task the products of every arm and repeat in one call, shuffled and
    unnamed, and for each product and criterion a yes or no -- a yes/no on a feature, where a grade would measure
    how much text there is. The judge's model has to be of a family no arm's agent runs on (a model favours its
    own family's text); runner.check_judge refuses one that is."""

    model_config = ConfigDict(extra="forbid")

    #: The agent that judges (one without tools, say): its llm_profile is the judge's model.
    agent: str
    #: Merged over its agent_config, as an arm's.
    agent_config: Dict[str, Any] = Field(default_factory=dict)
    #: What the products are and what to look for, before the criteria.
    instructions: str = ""
    #: Criterion id to its question, asked so that yes is good.
    criteria: Dict[str, str]
    product: Optional[Product] = None
    #: Seconds one judge call may take.
    timeout_s: float = 900

    @field_validator("criteria")
    @classmethod
    def _criteria(cls, value: Dict[str, str]) -> Dict[str, str]:
        if not value:
            raise ValueError("a judge needs at least one criterion")
        bad = [name for name in value if not _ID.fullmatch(name)]
        if bad:
            raise ValueError(f"criterion ids {bad}: letters, digits and '-', at most 40")
        labels = [name for name in value if re.fullmatch(r"P\d+", name)]
        if labels:
            raise ValueError(f"criterion ids {labels}: P<number> names a product in the judge's answer")
        return value


class Fork(BaseModel):
    """A task that starts in the middle of a machine run: a fork of a run ``agent-eval export-run`` froze, at a
    top-level state; the machine runs on by itself from there, an arm changes what its agents do."""

    model_config = ConfigDict(extra="forbid")

    #: The export directory (relative to the suite): the run's ``source.json``, and in ``data/`` what its fork hooks
    #: read (a store's documents), copied into every run's data directory.
    source: str
    #: The top-level state the fork starts at: where the source entered it the ``visit``-th time.
    at: str
    visit: int = Field(1, ge=1)
    #: Stop once the fork enters this state, and check what it made up to there; without it the fork runs to its end.
    until: Optional[str] = None
    #: current: the machine as it is now (what an arm's change is measured on); snapshot: as the source ran it.
    definition: Literal["current", "snapshot"] = "current"
    #: Mocks over the source run's ({state path: out}): stages the task does not measure, at no cost.
    mocks: Dict[str, Any] = Field(default_factory=dict)
    #: The stategraph instance that runs the fork.
    stategraph: str = "stategraph"

    #: The top-level step ``at``/``visit`` names in the source's journal, set by load_suite.
    _step: int = PrivateAttr(0)

    @property
    def step(self) -> int:
        return self._step


def fork_steps(journal: List[Dict[str, Any]], state: str) -> List[int]:
    """The top-level steps at which a run entered *state*, in order (its journal's trace rows)."""
    steps = set()
    for row in journal:
        if row.get("kind") == "trace" and row.get("status") == "enter" and row.get("state") == state:
            found = re.match(r"s(\d+):enter:", row.get("key") or "")
            if found:
                steps.add(int(found.group(1)))
    return sorted(steps)


def is_composite(machine: Any, machine_id: str, state: str) -> bool:
    """Whether *state* of the compiled *machine* is a composite -- which has no enter of its own: a fork stops at
    it by watching ``run.entered`` (the step of its last entry, it only grows), after the step that entered it and
    before the work of the state inside it."""
    from plugins.stategraph.engine.debugger import known_states

    node = known_states(machine).get(machine_id, {}).get(state)
    return node is not None and node.composite


def until_machine(definition: Any) -> Any:
    """A run's stored definition (a snapshot, as runs.db holds it), compiled -- without running its Python."""
    from plugins.stategraph.engine.machine import compile_tree
    from plugins.stategraph.model.loader import load_snapshot

    return compile_tree(load_snapshot(json.loads(definition) if isinstance(definition, str) else definition,
                                      execute_python=False))


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    #: The agent's task. A fork task has none: its machine runs on from the fork.
    input: Optional[str] = None
    fork: Optional[Fork] = None
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

    @model_validator(mode="after")
    def _input_or_fork(self) -> "Task":
        if (self.input is None) == (self.fork is None):
            raise ValueError(f"task {self.id}: needs exactly one of input and fork")
        return self


class Arm(BaseModel):
    """One variant of the agent. Empty = the agent as configured."""

    model_config = ConfigDict(extra="forbid")

    #: Another agent than the suite's.
    agent: Optional[str] = None
    #: Merged over the agent's agent_config like a config entry over the one it inherits from
    #: (``+x``/``!x`` in lists). An admin overlay: anything the agent YAML may set.
    agent_config: Dict[str, Any] = Field(default_factory=dict)
    #: Agent names to overlays for the whole run: laid over the configuration before anything is built from it,
    #: so the sub-agents the agent starts (a machine's, a panel's) run with them. Admin overlays, like agent_config.
    agents: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


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
    #: What a task with a ``judge`` check is judged by.
    judge: Optional[Judge] = None

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
        forks = [t.id for t in self.tasks if t.fork]
        # The source run goes into the run's runs.db: the installation's would keep it, a chain's holds it already.
        if forks and (self.data != "fresh" or self.chain):
            raise ValueError(f"fork tasks {forks} need data: fresh and no chain")
        # A fork runs the machine's agents, never the suite's: an overlay for that one would change nothing.
        own = [t.id for t in self.tasks if t.fork and t.agent_config]
        if own:
            raise ValueError(f"fork tasks {own} take no agent_config: the machine's agents run, put it in an "
                             "arm's agents")
        for task in self.tasks:
            wanted = task.check.judge
            if wanted and self.judge is None:
                raise ValueError(f"task {task.id}: a judge check needs the suite's judge")
            unknown = sorted(set(wanted) - set(self.judge.criteria)) if isinstance(wanted, list) else []
            if unknown:
                raise ValueError(f"task {task.id}: the judge has no criteria {unknown}")
        if len(forks) == len(self.tasks):
            idle = [name for name, arm in self.arms.items() if arm.agent or arm.agent_config]
            if idle:
                raise ValueError(f"arms {idle}: every task is a fork task, where agent and agent_config change "
                                 "nothing -- an arm changes the machine's agents in agents")
        return self

    def criteria(self, task: Task) -> Dict[str, str]:
        """The judge's criteria *task* is judged by."""
        wanted = task.check.judge
        return {k: v for k, v in self.judge.criteria.items() if wanted is True or k in wanted}

    def path(self, value: str) -> Path:
        """A path the suite names, resolved against its directory."""
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def reference_arm(self) -> str:
        return next(iter(self.arms))


def discover_suites(plugin_dirs: List[str], project_root: Path) -> List[Path]:
    """Every suite file the panel offers, below an ``evals/`` directory -- the project's own and each plugin's
    (``<plugin root>/<plugin>/evals/``); a plugin brings its suites the way it brings agents. A ``suite.yaml``
    always (a broken one shows its error), any other YAML that names an ``agent`` and ``tasks`` -- several suites
    in one directory."""
    roots = [project_root / "evals"] + [p for d in plugin_dirs for p in Path(d).glob("*/evals") if p.is_dir()]
    return sorted({f.resolve() for r in roots if r.is_dir() for f in r.rglob("*.yaml")
                   if f.name == "suite.yaml" or _names_a_suite(f)})


def _names_a_suite(path: Path) -> bool:
    try:
        data = yaml_io.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - workspace material that is no YAML of ours
        return False
    return isinstance(data, dict) and "agent" in data and "tasks" in data


def load_suite(path: str | Path) -> Suite:
    """The suite at *path*. Raises ValueError with the file name on a broken one."""
    path = Path(path).resolve()
    try:
        data = yaml_io.safe_load(path.read_text(encoding="utf-8")) or {}
        suite = Suite.model_validate(data)
    except Exception as e:
        raise ValueError(f"{path}: {e}") from e
    suite.root = path.parent
    product = suite.judge.product if suite.judge else None
    if product and product.script and not suite.path(product.script).exists():
        raise ValueError(f"{path}: judge: product script {product.script} does not exist")
    for task in suite.tasks:
        for value in (task.workspace, task.check.script):
            if value and not suite.path(value).exists():
                raise ValueError(f"{path}: task {task.id}: {value} does not exist")
        if task.fork:
            _resolve_fork(path, task)
    # A './' path in an overlay means the suite's directory, as in an agent YAML it means the YAML's -- the run
    # would otherwise look for it in its own workspace.
    owners = [(f"arm {name}", arm.agent_config) for name, arm in suite.arms.items()]
    owners += [(f"arm {name}, agent {agent}", overlay) for name, arm in suite.arms.items()
               for agent, overlay in arm.agents.items()]
    owners += [(f"task {task.id}", task.agent_config) for task in suite.tasks]
    owners += [("judge", suite.judge.agent_config)] if suite.judge else []
    for owner, overlay in owners:
        _resolve_relative_paths(overlay, suite.root)
        template = overlay.get("system_template")
        # Any other relative path stays what it is in a YAML: the project's, where the child runs.
        if isinstance(template, str) and not (PROJECT_ROOT / template).is_file():
            raise ValueError(f"{path}: {owner}: system_template {template} does not exist")
    return suite


def _resolve_fork(path: Path, task: Task) -> None:
    """Point a fork task at its export directory and find its step there -- a state the source never entered is
    refused now, not in every run."""
    fork = task.fork
    source = (path.parent / fork.source).resolve()
    if not (source / "source.json").is_file():
        raise ValueError(f"{path}: task {task.id}: {fork.source} has no source.json (agent-eval export-run makes it)")
    fork.source = str(source)
    try:
        journal = json.loads((source / "source.json").read_text(encoding="utf-8"))["journal"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{path}: task {task.id}: {fork.source}/source.json does not read: {e}") from e
    steps = fork_steps(journal, fork.at)
    if len(steps) < fork.visit:
        raise ValueError(f"{path}: task {task.id}: the source entered {fork.at!r} {len(steps)} time(s) at the top "
                         f"level, not {fork.visit}")
    fork._step = steps[fork.visit - 1]
    if fork.until and fork.definition == "snapshot":  # the machine now is the stategraph's to check, in the run
        from plugins.stategraph.engine.debugger import UnknownState, check_points

        from plugins.stategraph.engine.machine import CompileError

        try:
            run = json.loads((source / "source.json").read_text(encoding="utf-8"))["run"]
            machine = until_machine(run["definition"])
            if not is_composite(machine, run["machine_id"], fork.until):
                check_points(machine, [{"state": fork.until, "machine": run["machine_id"]}])
        except (UnknownState, CompileError, KeyError, TypeError, ValueError) as e:  # an export of a newer stategraph
            raise ValueError(f"{path}: task {task.id}: until: {type(e).__name__}: {e}") from e
