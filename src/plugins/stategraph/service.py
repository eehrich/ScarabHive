"""The operations behind stategraph's tools and panel: one implementation, two front ends.

Every method returns JSON-able data or raises ``ServiceError(status, message)``;
the tools turn that into ``{"status": "error", "error": ...}``, the web
endpoints into an HTTP error. Nothing here knows about FastAPI or tool params.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .engine.backend import NoBackend, ScarabHiveBackend, make_config_check
from .engine.debugger import Breakpoint, UnknownState, Watchpoint, parse_points
from .engine.journal import ACTIVE_STATUSES, TERMINAL_STATUSES, RunStore, resumable_failure, utc_now
from .engine.machine import CompileError
from .engine.runner import (LEASE_SECONDS, REMOTE_CONTROLS, RunManager, ended_without_outcome,
                            NotRewindable, failed_transiently, key_steps, rewound_rows)
from .kinds import describe_kinds
from .model.loader import MachineTree, load_snapshot
from .model.spec import agent_entry
from .model.validate import validate_tree
from .runners import merged_inject_params, runner_inject_params, runner_of
from .store import FileInTheWay, MachineStore, NotUtf8, VersionConflict, version_of

if TYPE_CHECKING:
    from .server import StateGraphServer

logger = logging.getLogger(__name__)

NEW_MACHINE = """\
stategraph: 1
id: {id}
title: {title}
description: ""
initial: start
states:
  start:
    transitions:
      - target: done
  done:
    type: final
"""


def _canonical(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, default=str)


def _other_request(row: dict[str, Any], machine_id: str, params: Optional[dict[str, Any]],
                   mocks: Optional[dict[str, Any]], mock_only: bool) -> str:
    """What differs between a run's row and a new request under its key: "" when it is the same request."""
    options = row.get("mocks") or {}
    differs = [name for name, equal in (
        ("machine", row.get("machine_id") == machine_id),
        ("params", _canonical(row.get("params")) == _canonical(params)),
        ("mocks", _canonical(options.get("mocks")) == _canonical(mocks)),
        ("mock_only", bool(options.get("mock_only")) == bool(mock_only))) if not equal]
    return ", ".join(differs)


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class RunFailed(ServiceError):
    """run_to_end(raise_error=True) of a run that failed with no exception of this process behind it (a timeout,
    a divergence, a run another process ended): ``error`` is the run's error, ``run_id`` the run."""

    def __init__(self, run_id: str, error: dict[str, Any]):
        super().__init__(500, f"run {run_id} failed: {error.get('type')}: {error.get('message')}")
        self.run_id = run_id
        self.error = error


class EventOutcomeUnknown(ServiceError):
    """Another process took the event but did not say what came of it: it most likely landed, so it is not one to
    send again blindly."""


#: A run id becomes the session id sg_<run id> and a journal key: letters, digits, _ and -.
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: How long terminate waits for the run to end (its finally activities run first) before it answers.
TERMINATE_WAIT = 10.0
#: How long a control request waits for the process that holds the run to take it (it looks each second).
CONTROL_WAIT = 5.0
#: The statuses a run row has.
RUN_STATUSES = ("running", "paused", "waiting", "interrupted", "succeeded", "failed", "cancelled")
#: The folder of machines in the first writable root that name none: the author's own.
OWN_GROUP = "My machines"


class StateGraphService:
    def __init__(self, server: "StateGraphServer"):
        self.server = server
        self.store: MachineStore = server.machines
        self.runs: RunManager = server.run_manager
        self.run_store: RunStore = server.run_store
        #: The live child runs a caller's pause halted (_control_children): only those does its continue release.
        self._paused_by_caller: set[str] = set()
        #: A machine's problem counts while its tree's files stand as they stood (_check): id -> (stamp, counts).
        self._checks: dict[str, tuple[Any, dict[str, Any]]] = {}

    # ------------------------------------------------------------ helpers
    def config_check(self, runner: Optional[str] = None) -> Optional[Callable[..., Optional[str]]]:
        """SG007 against the configuration. The validator's tool questions name the run's root file and get its
        runner (runners.py); ``runner`` answers the others -- a run's backend passes its own."""
        config = getattr(self.server, "system_config", None)
        if config is None or getattr(config, "plugins", None) is None:
            return None
        return make_config_check(config, runner=runner or self.server.runner_agent, own_instance=self.server.name,
                                 default_runner=self.server.runner_agent,
                                 is_agent=getattr(self.server, "is_agent", None),
                                 semaphores=lambda: self.runs.semaphores)  # as the runs read them (a reload)

    def _validate(self, tree: MachineTree) -> MachineTree:
        checked = validate_tree(tree, self.config_check())
        config = getattr(self.server, "system_config", None)
        if config is not None and getattr(config, "plugins", None) is not None:
            default = self.server.runner_agent
            name, claimed_twice = runner_of(config, tree.root, default)
            if claimed_twice:  # no runner can be told: nothing there runs, with or without tool activities
                checked.add("error", "SG007", claimed_twice, file=tree.root)
            elif name != default:
                # the whole tree runs with the root's runner: a file anyone may write (an import by machine id finds
                # the writable root first) must not get its tools and its params -- unless its folder is that
                # runner's too
                for path in sorted({getattr(loaded, "local_of", None) or path for path, loaded in tree.files.items()}):
                    if self.store.is_writable(Path(path)) and runner_of(config, path, default)[0] != name:
                        checked.add("error", "SG007", f"{path} lies in a writable folder, and this machine runs with "
                                    f"{name}, the runner of its folder: import the machines of its tree from that "
                                    "folder (./x.yaml)", file=tree.root)
                        break
        return checked

    def _hides_a_machine(self, machine_id: str, files: dict[str, str]) -> Optional[str]:
        """Why a file of a save would hide another machine: an ``<x>.yaml`` written into a machine root is machine x
        there, found before the one of that id that lies elsewhere -- its own runs, and every import of it by id,
        would take the new file."""
        base = self.store.base_dir(machine_id)
        # find() takes the first root that has the id: a file hides one only in a root searched before that one's
        order = [os.path.realpath(str(directory)) for _, directory in self.store.root_dirs()]
        for rel in files:
            target = Path(os.path.normpath(str(base / rel)))
            here = os.path.realpath(str(target.parent))
            if target.suffix.lower() != ".yaml" or target.stem == machine_id or here not in order:  # glob is caseless
                continue
            found = self.store.find(target.stem)
            there = os.path.realpath(str(found.path.parent)) if found is not None else None
            if there in order and order.index(here) < order.index(there):
                return (f"{rel} would hide machine {target.stem!r} ({found.path}): a file of this name in a machine "
                        "folder is that machine -- give it another name")
        return None

    def _runner_of(self, tree: MachineTree) -> str:
        """The runner a validated tree runs with: its root file's folder's."""
        return runner_of(self.server.system_config, tree.root, self.server.runner_agent)[0]

    def _graph(self, tree: MachineTree) -> dict[str, Any]:
        try:
            from .model.graph import graph_view
        except ImportError:  # the editor view is optional for tools
            return {}
        try:
            return graph_view(tree)
        except Exception:
            logger.warning("stategraph: graph view failed", exc_info=True)
            return {}

    @staticmethod
    def _problems(tree: MachineTree) -> list[dict[str, Any]]:
        return [p.as_dict() for p in tree.problems]

    def _files_of(self, machine_id: str, tree: MachineTree) -> tuple[dict[str, str], dict[str, str]]:
        files: dict[str, str] = {}
        versions: dict[str, str] = {}
        for path, loaded in tree.files.items():
            if loaded.local_of is not None:
                continue  # a machine inside the file: its text is the file's
            texts = [(path, loaded.text)]
            if loaded.python_path and loaded.python_text is not None:
                texts.append((loaded.python_path, loaded.python_text))
            for file_path, text in texts:
                rel = self.store.relative(machine_id, file_path)
                files[rel] = text
                versions[rel] = version_of(text)
        return files, versions

    def _not_in_tree(self, machine_id: str, files: dict[str, str], tree: MachineTree) -> list[str]:
        """The files of a save that the loaded tree does not hold: no import of it, no companion module."""
        def key(path: str) -> str:
            return os.path.normcase(os.path.normpath(path))

        held = {key(path) for path, loaded in tree.files.items() if loaded.local_of is None}
        held |= {key(loaded.python_path) for loaded in tree.files.values() if loaded.python_path}
        base = self.store.base_dir(machine_id)
        unchanged = self.store.unchanged(machine_id, files)  # left alone by write_files: no extra (API-5)
        return sorted(rel for rel in files if key(str(base / rel)) not in held and rel not in unchanged)

    def _own_files(self, machine_id: str) -> set[str]:
        """The relative paths of the saved machine's files (none when there is no such machine)."""
        if self.store.find(machine_id) is None:
            return set()
        own = {f"{machine_id}.yaml"}
        try:
            own |= set(self._files_of(machine_id, self.store.load(machine_id))[0])
        except Exception:  # a file that does not load: its root file is still its own
            pass
        return own

    def _require(self, machine_id: str) -> None:
        if self.store.find(machine_id) is None:
            raise ServiceError(404, f"no machine {machine_id!r} in the machine roots")

    # ------------------------------------------------------------ machines
    def list_machines(self, *, checked: bool = False) -> list[dict[str, Any]]:
        """Every machine with what its root file's head says (title, description, group): read, not loaded -- a
        load with its imports and the validation cost a few dozen machines seconds. ``checked`` adds each one's
        problem counts (``machine_checks``)."""
        out = []
        for machine in self.store.list():
            head = _head(machine.path)
            entry: dict[str, Any] = {"id": machine.id, "file": str(machine.path), "root": machine.root,
                                     "writable": machine.writable, "title": head.get("title", ""),
                                     "description": head.get("description", ""),
                                     "group": head.get("group") or _origin(machine)}
            if checked:
                entry.update(self._check(machine.id))
            out.append(entry)
        return out

    def machine_checks(self) -> dict[str, dict[str, Any]]:
        """Each machine's problem counts, by id: every machine loaded with its imports and validated. Costly: the
        panel asks for it apart from the list, off the event loop."""
        return {machine.id: self._check(machine.id) for machine in self.store.list()}

    def _check(self, machine_id: str) -> dict[str, Any]:
        # The counts of the last check while no file of the tree (YAML, companion modules) and no semaphore changed:
        # loading and validating every machine takes seconds each time the list is shown.
        # ponytail: python code a machine calls (module:function) and the agent config are not in the stamp -- after
        # such a change the list's badge waits for a tree file to change or a restart; opening a machine validates anew
        cached = self._checks.get(machine_id)
        if cached is not None and cached[0] == self._check_stamp(cached[0][0]):
            return cached[1]
        try:
            tree = self._validate(self.store.load(machine_id))
        except Exception as exc:  # one broken file must not hide the others
            return {"valid": False, "errors": 1, "warnings": 0, "error": f"{type(exc).__name__}: {exc}"}
        errors = [p for p in tree.problems if p.level == "error"]
        counts = {"valid": not errors, "errors": len(errors), "warnings": len(tree.problems) - len(errors)}
        paths = tuple(sorted({p for f in tree.files.values() for p in (f.path, f.python_path) if p}))
        self._checks[machine_id] = (self._check_stamp(paths), counts)
        return counts

    def _check_stamp(self, paths: tuple[str, ...]) -> tuple[Any, ...]:
        def stat(path: str) -> Any:
            try:
                st = os.stat(path)
            except OSError:
                return None
            return st.st_mtime_ns, st.st_size
        return paths, tuple(stat(p) for p in paths), tuple(sorted(self.runs.semaphores or {}))

    def get_machine(self, machine_id: str) -> dict[str, Any]:
        self._require(machine_id)
        found = self.store.find(machine_id)
        assert found is not None
        tree = self.store.load(machine_id)
        if tree.root not in tree.files:  # a root it could not read (not UTF-8, gone): the loader's problem says why,
            # by the file's name (API-3) -- not a KeyError of root_file with the absolute path
            raise ServiceError(422, "; ".join(p.message.replace(str(found.path), found.path.name)
                                              for p in tree.problems if p.level == "error")
                               or f"{found.path.name} could not be read")
        tree = self._validate(tree)
        files, versions = self._files_of(machine_id, tree)
        agents = self.server.agents_of(machine_id)
        spec = tree.root_file.spec
        offer = None
        if spec is not None and spec.agent is not None:  # declared: this process read the block as it started
            name = agent_entry(spec, self.server.name)[0]
            servers = getattr(getattr(self.server.system_config, "plugins", None), "servers", None) or {}
            held = servers.get(name)  # by whichever instance: machine folders overlap
            offer = {"name": name, "declared": bool(getattr(held, "from_machine_file", False))
                     and str(getattr(held, "machine", None) or "") == machine_id}
        return {"id": machine_id, "file": str(found.path), "writable": found.writable,
                "root_file": f"{machine_id}.yaml", "files": files, "versions": versions,
                "problems": self._problems(tree), "graph": self._graph(tree), "layout": self.store.layout(machine_id),
                "agents": agents, "offer": offer, **self._runner(machine_id)}

    def _runner(self, machine_id: str) -> dict[str, Any]:
        """The runner a run of the machine gets: its name, and why it is not the folder's when two claim it."""
        name, problem = self.server.runner_for(machine_id)
        return {"runner": name, **({"runner_problem": problem} if problem else {})}

    def create_machine(self, machine_id: str, title: Optional[str] = None) -> dict[str, Any]:
        from .model.spec import check_name

        try:
            check_name(machine_id, "machine id")
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from None
        if self.store.find(machine_id) is not None:
            raise ServiceError(409, f"machine {machine_id!r} exists already")
        title = title or machine_id.replace("_", " ").title()
        if not isinstance(title, str) or not title.isprintable():
            raise ServiceError(422, "title is one line of printable text")
        text = NEW_MACHINE.format(id=machine_id, title=json.dumps(title, ensure_ascii=False))  # JSON is valid YAML
        try:
            self.store.write_files(machine_id, {f"{machine_id}.yaml": text})
        except FileInTheWay:  # made meanwhile, or a file no root lists
            raise ServiceError(409, f"machine {machine_id!r} exists already") from None
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        except NotUtf8 as exc:
            raise ServiceError(422, str(exc)) from None
        return self.get_machine(machine_id)

    def delete_machine(self, machine_id: str, expected_version: str) -> dict[str, Any]:
        """Delete a machine of a writable root: its file (the version the caller saw), its layout and its companion
        module -- unless another machine uses that module, or it lies outside the writable roots. Refused while
        another machine imports it: that one would stop loading. Its runs keep their snapshot and stay readable."""
        self._require(machine_id)
        try:
            found, _ = self.store.read(machine_id)  # a file that is not UTF-8 says so, before it is loaded
        except NotUtf8 as exc:
            raise ServiceError(422, str(exc)) from None
        own = self.store.load(machine_id).root_file
        importers, sharers = [], []
        for other in self.store.list():
            if other.id == machine_id:
                continue
            try:
                tree = self.store.load(other.id)
            except Exception:  # a broken machine imports nothing that loads
                continue
            if any(_same_file(path, found.path) for path in tree.files if path != tree.root):
                importers.append(other.id)
            if own.python_path and any(f.python_path and _same_file(f.python_path, own.python_path)
                                       for f in tree.files.values()):
                sharers.append(other.id)
        if importers:
            raise ServiceError(409, f"{machine_id!r} is imported by {', '.join(sorted(importers))}: remove those "
                                    "imports first")
        module = Path(own.python_path) if own.python_path else None
        keep = module is None or sharers or not self.store.is_writable(module)
        try:
            removed = self.store.delete(machine_id, expected_version=expected_version,
                                        companion=None if keep else module)
        except VersionConflict as exc:
            raise ServiceError(409, f"the machine changed since it was read ({exc}): reload it first") from None
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        except NotUtf8 as exc:
            raise ServiceError(422, str(exc)) from None
        return {"deleted": machine_id, "files": [Path(path).name for path in removed],
                "kept_module": Path(module).name if module is not None and keep else None}

    def _tree_from(self, files: Optional[dict[str, str]], yaml: Optional[str],
                   machine_id: Optional[str]) -> tuple[str, dict[str, str], MachineTree]:
        if files is None and yaml is None:
            raise ServiceError(422, "pass files {relative path: text} or yaml")
        if files is not None and not isinstance(files, dict):
            raise ServiceError(422, f"files must be an object {{relative path: text}}, not {type(files).__name__}")
        if yaml is not None and not isinstance(yaml, str):
            raise ServiceError(422, f"yaml must be the machine's text, not {type(yaml).__name__}")
        if files is None:
            files = {}
        files = {str(k): str(v) for k, v in files.items()}
        if yaml is not None:
            machine_id = machine_id or _id_of(yaml)
            if not machine_id:
                raise ServiceError(422, "the YAML has no id: (and no machine_id was given)")
            files[f"{machine_id}.yaml"] = yaml
        if not machine_id:
            roots = [rel for rel in files if rel.endswith(".yaml") and "/" not in rel and os.sep not in rel]
            ids = [_id_of(files[rel]) for rel in roots]
            machine_id = next((i for i, rel in zip(ids, roots) if i and rel == f"{i}.yaml"), None)
            if not machine_id:
                raise ServiceError(422, "name the root machine with machine_id (its file is <machine_id>.yaml)")
        from .model.spec import check_name

        try:  # as create_machine's: "sub/x" would be saved where no machine root lists it (API-7)
            check_name(machine_id, "machine id")
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from None
        if f"{machine_id}.yaml" not in files and self.store.find(machine_id) is None:
            raise ServiceError(422, f"files has no {machine_id}.yaml")
        try:
            tree = self.store.load_files(files, machine_id=machine_id)
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        return machine_id, files, tree

    def validate(self, files: Optional[dict[str, str]] = None, yaml: Optional[str] = None,
                 machine_id: Optional[str] = None) -> dict[str, Any]:
        machine_id, _, tree = self._tree_from(files, yaml, machine_id)
        tree = self._validate(tree)
        return {"machine_id": machine_id, "problems": self._problems(tree), "graph": self._graph(tree)}

    def save_machine(self, machine_id: Optional[str], files: dict[str, str],
                     expected_versions: Optional[dict[str, str]] = None, force: bool = False) -> dict[str, Any]:
        machine_id, files, tree = self._tree_from(files, None, machine_id)
        tree = self._validate(tree)
        errors = [p for p in tree.problems if p.level == "error"]
        if errors and not force:
            first = "; ".join(f"{p.file.rsplit('/', 1)[-1]}:{p.line or '?'} {p.code} {p.message}" for p in errors[:3])
            raise ServiceError(422, f"{len(errors)} error(s), not saved: {first}")
        hidden = self._hides_a_machine(machine_id, files)
        if hidden:
            raise ServiceError(409, hidden)
        extra = [] if force else self._not_in_tree(machine_id, files, tree)
        if extra:  # validation saw only the tree: a file outside it would be written unchecked (API-7)
            raise ServiceError(422, f"{', '.join(extra)}: not part of machine {machine_id!r} (neither imported nor "
                                    "its python module) -- save each machine under its own id")
        try:
            versions = self.store.write_files(machine_id, files, expected_versions)
        except FileInTheWay as exc:
            if exc.rel in self._own_files(machine_id):  # the machine's own file, sent without the version read
                raise ServiceError(409, f"{exc.rel} is a file of machine {machine_id!r} already: pass its version "
                                        "from get_machine in expected_versions to change it") from None
            raise ServiceError(409, str(exc)) from None
        except VersionConflict as exc:
            raise ServiceError(409, str(exc)) from None
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        except NotUtf8 as exc:
            raise ServiceError(422, str(exc)) from None
        return {"machine_id": machine_id, "versions": versions, "problems": self._problems(tree),
                "graph": self._graph(tree)}

    async def edit_machine(self, machine_id: str, op: dict[str, Any], expected_version: Optional[str],
                           drafts: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """One graph edit. On the file (its version the caller read), written at once; or, given ``drafts`` (the
        caller's unsaved files, by relative path), on the root file's draft, else the file: nothing is written, the
        answer is ``graph`` and ``problems`` of the drafts and ``draft``, the new root text."""
        from .model.yamledit import EditError, apply_op

        self._require(machine_id)
        try:
            found, text = self.store.read(machine_id)
        except NotUtf8 as exc:
            raise ServiceError(422, str(exc)) from None
        root = f"{machine_id}.yaml"
        if drafts is not None:
            try:
                draft = await asyncio.to_thread(apply_op, str(drafts.get(root, text)), op)
            except EditError as exc:
                raise ServiceError(422, str(exc)) from None
            def checked() -> dict[str, Any]:  # load, validate and lay out: off the event loop (API-2)
                tree = self._validate(self._tree_from({**drafts, root: draft}, None, machine_id)[2])
                return {"machine_id": machine_id, "problems": self._problems(tree), "graph": self._graph(tree),
                        "draft": draft, **self._rename_warnings(machine_id, op, tree)}
            return await asyncio.to_thread(checked)
        if expected_version != version_of(text):
            raise ServiceError(409, f"the file changed since you read it (current version {version_of(text)})")
        try:
            # the edit renders the file anew (a batch once per edit it holds): off the event loop. The write stays
            # on it and checks the version again -- a request that wrote meanwhile makes this one a conflict
            new_text = await asyncio.to_thread(apply_op, text, op)
        except EditError as exc:
            raise ServiceError(422, str(exc)) from None
        try:
            self.store.write_files(machine_id, {root: new_text}, {root: expected_version})
        except VersionConflict as exc:
            raise ServiceError(409, str(exc)) from None
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        except NotUtf8 as exc:
            raise ServiceError(422, str(exc)) from None
        return await asyncio.to_thread(lambda: {**self.get_machine(machine_id),
                                                **self._rename_warnings(machine_id, op)})

    def _rename_warnings(self, machine_id: str, op: dict[str, Any], tree: Optional[MachineTree] = None
                         ) -> dict[str, Any]:
        """A rename_state (alone or in a batch) leaves the old name where code reads it as a string -- the
        companion module's ``run.visits["old"]``, say: the lines that still name it, as ``warnings`` (MOD-2)."""
        from .model.yamledit import companion_mentions

        ops = op.get("ops") if op.get("op") == "batch" and isinstance(op.get("ops"), list) else [op]
        renamed = [o.get("old") for o in ops if isinstance(o, dict) and o.get("op") == "rename_state"
                   and isinstance(o.get("old"), str)]
        if not renamed:
            return {}
        try:
            root = (tree or self.store.load(machine_id)).root_file
        except Exception:  # a machine that does not load: the answer's problems say why
            return {}
        if not root.python_text:
            return {}
        warnings = []
        for old in renamed:
            lines = companion_mentions(root.python_text, old)
            if lines:
                warnings.append(f"{Path(root.python_path or '').name} still names {old!r} at line "
                                f"{', '.join(map(str, lines))}: code that reads run.visits or run.state by that name "
                                "needs the new one")
        return {"warnings": warnings} if warnings else {}

    def save_layout(self, machine_id: str, layout: dict[str, Any]) -> dict[str, Any]:
        self._require(machine_id)
        try:
            self.store.write_layout(machine_id, layout)
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        return {}

    def kinds(self) -> list[dict[str, Any]]:
        return describe_kinds()

    # ------------------------------------------------------------ runs
    def backend_factory(self) -> Callable[[str], Any]:
        """A run's backend, as its row says: its user -- whoever resumes or terminates the run only triggers that;
        its agents run, and their instance sessions are found, as the run's user -- and its caller's place in a
        sub-agent tree."""
        def make(run_id: str) -> Any:
            row = self.run_store.get_run(run_id) or {}
            # the runner the run started with (its row; one from before the column: its machine's folder now): its
            # allowlist bounds the tool activities, its inject_params go into them over the instance's
            name = row.get("runner") or self.server.runner_for(row.get("machine_id"))[0]
            runner = self.server.resolve_runner(name)
            if runner is None:  # gone since the run started (renamed, disabled): it fails, not runs with another
                return NoBackend(f"its runner {name!r} is not there (renamed, disabled or its plugin off) -- "
                                 "restore it, or fork the run onto the current definition")
            inject = merged_inject_params(usable_inject(self.server.inject_params, f"instance {self.server.name}"),
                                          usable_inject(runner_inject_params(self.server.system_config, name),
                                                        f"runner {name}"))
            return ScarabHiveBackend(runner=runner, system_config=self.server.system_config,
                                     session_id=f"sg_{run_id}", user_id=row.get("user_id"),
                                     token=self.server.cancel_token(run_id), nesting=row.get("nesting"),
                                     inject_params=inject, config_check=self.config_check(name))
        return make

    async def start_run(self, machine_id: str, params: Optional[dict[str, Any]] = None,
                        mocks: Optional[dict[str, Any]] = None, mock_only: bool = False, breakpoints: Any = (),
                        watchpoints: Any = (), pause_at_start: bool = False, user_id: Optional[str] = None,
                        run_key: Optional[str] = None, run_id: Optional[str] = None,
                        caller_session: Optional[str] = None,
                        live: Optional[dict[str, Any]] = None,
                        tags: Optional[dict[str, Any]] = None, detached: bool = False) -> dict[str, Any]:
        """Start a run of ``machine_id``. With ``run_key`` the same request gets the run of that key, if there is
        one: attached to while it runs, resumed when interrupted, and once it ended its outcome again (``ended``:
        its status) -- only a transient failure (runner.TRANSIENT_ERRORS) goes on: resumed where it failed (_replayable,
        rewound_rows), else a new run.

        ``run_id`` (the agent facade: ``<request id>_sg<n>``) keeps cancel, status and cost attribution under
        the caller's request id; it must be a session-id-safe word of at most 128 characters.
        ``caller_session``: the session of the agent that asks; the run's agent instances sit one level below it
        in its sub-agent tree.
        ``live``: objects of the caller in this process for its call functions (``sg.live``) -- never journaled,
        handed again when the key resumes an interrupted run here.
        ``tags``: a flat object of text or number values (``{book: 95}``) that list_runs filters on (FEAT-7).
        ``detached``: started (or resumed by its key) from a call activity, the run is still nobody's child -- a
        hand-off, not a step of the caller's work: the caller's spend, pause and end leave it alone (FEAT-1).
        """
        self._require(machine_id)
        for name, value in (("params", params), ("mocks", mocks)):
            if value is not None and not isinstance(value, dict):
                raise ServiceError(422, f"{name} must be an object, not {type(value).__name__}")
        if not isinstance(mock_only, bool):
            raise ServiceError(422, f"mock_only must be true or false, not {mock_only!r}")
        if run_id is not None and not _RUN_ID.fullmatch(run_id):
            raise ServiceError(422, f"run_id {run_id!r} must match {_RUN_ID.pattern}")
        _check_tags(tags)
        nesting = await self._nesting(user_id, caller_session)
        if run_key:
            found = await self._by_key(run_key, machine_id, params, mocks, mock_only, user_id, live, detached)
            if found is not None:
                return found
        # loading and validating the tree takes seconds for a large machine: off the event loop (API-2)
        checked, tree = await asyncio.to_thread(self._checked_tree, machine_id)
        if run_key:
            # the key looked up again after that await, and the run started with no await in between: two requests
            # with one key never both start a run (a transiently failed run the first lookup could not resume
            # is not tried again)
            found = await self._by_key(run_key, machine_id, params, mocks, mock_only, user_id, live, detached,
                                       go_on=False)
            if found is not None:
                return found
        try:
            run_id = await self.runs.start(
                tree, params=params, mocks=mocks, mock_only=mock_only, breakpoints=breakpoints,
                watchpoints=watchpoints, pause_at_start=pause_at_start,
                backend_factory=None if mock_only else self.backend_factory(), user_id=user_id,
                run_key=run_key, run_id=run_id, nesting=nesting, runner=self._runner_of(checked), live=live)
        except (ValueError, CompileError) as exc:
            raise ServiceError(422, str(exc)) from None
        if tags:
            self._quietly(self.run_store.set_tags, run_id, tags)
        caller = None if detached else self._caller()
        if caller is not None and caller[0] != run_id:  # started from a call activity of a run here (FEAT-1)
            self._quietly(self.run_store.update_run, run_id, caller_run=caller[0], caller_request=caller[1])
        self._note(run_id, "started", who=user_id, why="start_run", machine=machine_id, run_key=run_key,
                   **({"caller_run": caller[0]} if caller is not None and caller[0] != run_id else {}))
        return {"run_id": run_id}

    def _checked_tree(self, machine_id: str) -> tuple[MachineTree, MachineTree]:
        """The machine validated, and the tree a run runs: exactly what was validated, loaded from its snapshot (a
        save meanwhile does not slip in). Refused (422) with its errors."""
        checked = self._validate(self.store.load(machine_id))
        errors = [p for p in checked.problems if p.level == "error"]
        if errors:
            raise ServiceError(422, f"{len(errors)} error(s): " + "; ".join(
                f"{p.code} {p.path} {p.message}" for p in errors[:3]))
        return checked, load_snapshot(checked.snapshot())

    async def _by_key(self, run_key: str, machine_id: str, params: Optional[dict[str, Any]],
                      mocks: Optional[dict[str, Any]], mock_only: bool, user_id: Optional[str],
                      live: Optional[dict[str, Any]], detached: bool = False,
                      go_on: bool = True) -> Optional[dict[str, Any]]:
        """The answer of the run ``run_key`` names, if that run answers this request -- None when a new run starts.
        Up to its answer nothing awaits but a resume of that run."""
        existing = self.run_store.latest_by_key(run_key)
        if existing is None:
            return None
        owner = existing.get("user_id")
        if owner not in (None, user_id) or not self.server.sees_run(user_id, owner):  # nobody's: an admin's (API-3)
            raise ServiceError(409, f"run_key {run_key!r} belongs to another user's run")
        other = _other_request(existing, machine_id, params, mocks, mock_only)
        if other:  # a mock test's outcome must never answer a live request that reuses its key
            raise ServiceError(409, f"run_key {run_key!r} names run {existing['id']} with other {other}; "
                                    "the same key is the same request -- use a new key for a new one")
        if existing["id"] in self.runs.live:
            return {"run_id": existing["id"], "attached": True}
        if existing["status"] in ACTIVE_STATUSES:
            self.sweep()  # a dead owner's lease ran out, or its process is gone: interrupted now
            existing = self.run_store.get_run(existing["id"]) or existing
        if existing["status"] in ACTIVE_STATUSES:
            return {"run_id": existing["id"], "attached": False, "owner": existing.get("owner"),
                    "note": "another process runs it; this call started nothing"}
        transient = existing["status"] == "failed" and failed_transiently(existing.get("error"))
        if transient and not (go_on and await asyncio.to_thread(self._replayable, existing)):  # off the loop: it
            return None                                                                       # loads the machine
        if existing["status"] == "interrupted" or transient:
            # whoever resumes it now is its caller: the run that started it once may have ended since, and its end
            # must not end a child another run goes on in (end_children); resumed from no run, or detached, it is
            # nobody's child
            caller = None if detached else self._caller()
            if caller is not None and caller[0] == existing["id"]:
                caller = None
            if (caller[0] if caller else None) != existing.get("caller_run"):
                self._quietly(self.run_store.update_run, existing["id"], caller_run=caller[0] if caller else None,
                              caller_request=caller[1] if caller else None)
            try:
                await self._resume(existing["id"], live=live, who=user_id, why="start_run with its run_key",
                                   transient=transient)
            except ServiceError as exc:
                if not transient:
                    raise
                # start_run looks again: another request's resume of it is attached to, else a new run starts
                logger.info("stategraph: run %s failed transiently and is not resumed here (%s)", existing["id"],
                            exc.message)
                return None
            return {"run_id": existing["id"], "resumed": True}
        return {"run_id": existing["id"], "ended": existing["status"]}

    def _replayable(self, row: dict[str, Any]) -> bool:
        """Whether a run that failed transiently can go on where it failed (rewound_rows, transient): its replay
        holds -- it did not diverge, and its machine's files are still those of its snapshot (a fix of the machine,
        a longer timeout, reaches only a new run). Else the same request starts a new run on the current files."""
        error, causes = row.get("error"), []
        while isinstance(error, dict) and len(causes) < 50:
            causes.append(error.get("type"))
            error = error.get("cause")
        if "diverged" in causes:
            return False
        try:
            current, _ = self._files_of(row["machine_id"], self.store.load(row["machine_id"]))
        except Exception:  # gone or broken: a new run says why
            return False
        return current == ((row.get("definition") or {}).get("files") or {})

    def _caller(self) -> Optional[tuple[str, str]]:
        """``(run id, request id)`` of the call activity of a run in this process that this code runs under, if
        any: its request id (``<run>_NNN``) is the task's current one."""
        try:
            from agent_system.tools.status import current_request_id

            request_id = current_request_id.get()
            enclosing = self.runs.run_of_request(request_id)
        except Exception:
            return None
        return (enclosing.ctx.id, str(request_id)) if enclosing is not None else None

    @staticmethod
    def _quietly(write: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """A write for the record (a lifecycle row, a link): one that fails never fails what it records."""
        try:
            write(*args, **kwargs)
        except Exception:
            logger.warning("stategraph: %s%s not stored", getattr(write, "__name__", "write"), args[:2], exc_info=True)

    def _note(self, run_id: str, what: str, *, who: Optional[str], why: str, level: int = logging.INFO,
              **data: Any) -> None:
        """A lifecycle row in the run's journal and a log line: what happened to the run, who did it, why, and
        which process (DBG-8)."""
        machine = data.pop("machine", None) or (self.run_store.get_run(run_id) or {}).get("machine_id")
        self._quietly(self.run_store.lifecycle, run_id, what,
                      {"who": who, "why": why, "process": self.runs.owner, **data})
        extra = ", ".join(f"{key}={value}" for key, value in data.items() if value not in (None, "", [], {}))
        logger.log(level, "stategraph: run %s of %s %s by %s (%s) in %s%s", run_id, machine, what, who or "-", why,
                   self.runs.owner, f": {extra}" if extra else "")

    # ------------------------------------------------------------ the lease sweep (RES-8)
    def sweep(self) -> list[str]:
        """Mark interrupted the active runs whose owner's lease ran out -- or whose owner is a process of this host
        that is gone (``owner_gone``): those at once, with their lease dropped, so a resume can take them now.
        Each gets its lifecycle row. The run ids."""
        swept = self.run_store.sweep(now=utc_now(), gone=owner_gone)
        for run_id, why in swept:
            self._note(run_id, "interrupted", who="sweep", why=why)
        return [run_id for run_id, _ in swept]

    async def _nesting(self, user_id: Optional[str], caller_session: Optional[str]) -> Optional[dict[str, Any]]:
        """The caller's place in a sub-agent tree, as the SAM keeps it in the caller's session: ``depth`` and
        ``depth_budget`` (the levels it may still grant below itself). None without a stored caller session."""
        if not caller_session:
            return None
        runner = self.server.resolve_runner()
        sessions = getattr(getattr(runner, "_session_service", None), "session_manager", None)
        if sessions is None:
            return None
        try:
            data = await sessions.load_session(user_id or "anonymous", caller_session)
        except Exception:  # no stored session of that user: the run's agents start a tree of their own
            return None
        depth, budget = data.get("depth"), data.get("depth_budget")
        # session: the run's own session hangs below it (backend.run_began), where its caller's tree is shown
        return {"depth": depth if _is_int(depth) else 1, "depth_budget": budget if _is_int(budget) else None,
                "session": caller_session}

    async def run_to_end(self, machine_id: str, params: Optional[dict[str, Any]] = None, *,
                         user_id: Optional[str] = None, keep_on_cancel: Optional[Callable[[], bool]] = None,
                         raise_error: bool = False, **start: Any) -> dict[str, Any]:
        """Start a run (``start_run``: run_key, run_id, live, ...) and wait in this process until its task here
        is over; its row (``status``, ``output``, ``error``, ``final_state``). For Python code that runs a machine
        as a step of its own work. The status is a terminal one -- or ``interrupted`` when this process stopped
        meanwhile, or the row of another owner that took the run over: the caller reads anything but
        ``succeeded`` as not done.

        Its caller cancelled: the run this call started or resumed is terminated, then the cancel goes on --
        unless ``keep_on_cancel()`` says the cancel came from outside the caller's work (its process stopping, a
        graceful shutdown cutting the request): then the run is interrupted (``RunManager.interrupt``) and a later
        ``run_to_end`` under its key resumes it. Called from a ``call`` activity of another run here, that run's
        ending decides first: terminated, its child is too; stopping (shutdown, interrupt, taken over), its child
        is interrupted. A key
        whose run another process holds is a 409 -- waiting here would wait on nobody's task -- and so is
        ``live`` for a key whose run already runs in this process: that run has its own caller's objects, and
        this caller's cancel must not end it.

        ``raise_error``: a run that failed raises instead of answering its row (FEAT-12) -- the exception its
        failing activity's own code raised in this process (``RunContext.exception``: the caller can except its own
        error type), else ``RunFailed`` with the run's error."""
        started = await self.start_run(machine_id, params=params, user_id=user_id, **start)
        run_id = started["run_id"]
        if started.get("attached") is False:
            raise ServiceError(409, f"run {run_id} runs in another process ({started.get('owner')})")
        if started.get("attached") and start.get("live"):
            raise ServiceError(409, f"run {run_id} already runs in this process with its own caller's live "
                                    "objects; use a new run_key for a new run")
        owned = not started.get("attached")
        live = self.runs.live.get(run_id)
        if live is not None:
            try:
                await asyncio.shield(live.task)
            except asyncio.CancelledError:
                if owned and self._cut_from_outside(run_id, keep_on_cancel):
                    self.runs.interrupt(run_id)
                    if live.task is not None:  # its row says interrupted when the cancel goes on
                        await asyncio.wait({live.task}, timeout=TERMINATE_WAIT)
                elif owned:
                    try:
                        await self.control_run(run_id, "terminate", user_id=user_id)
                    except Exception:
                        logger.warning("stategraph: terminate of %s after its caller's cancel failed", run_id,
                                       exc_info=True)
                raise
        row = self.run_store.get_run(run_id) or {"id": run_id, "status": "unknown"}
        if raise_error and row.get("status") == "failed":
            original = getattr(live.ctx, "exception", None) if live is not None else None
            if isinstance(original, BaseException):
                raise original
            raise RunFailed(run_id, row.get("error") or {})
        return row

    def _cut_from_outside(self, run_id: str, keep_on_cancel: Optional[Callable[[], bool]]) -> bool:
        """After the caller's cancel: interrupt the run (True) or terminate it. A caller that is itself a run's
        ``call`` activity here (its task carries that run's id) goes with that run's ending -- a terminate from
        the panel reaches no token of the caller's -- and only otherwise ``keep_on_cancel`` decides."""
        try:
            from agent_system.tools.status import current_request_id

            enclosing = self.runs.run_of_request(current_request_id.get())
        except Exception:
            enclosing = None
        if enclosing is not None and enclosing.ctx.id != run_id:
            if enclosing.ctx.stopping or enclosing.ctx.lost:
                return True
            if enclosing.ctx.cancelled:
                return False
        return keep_on_cancel is not None and _keeps(keep_on_cancel, run_id)

    async def _resume(self, run_id: str, live: Optional[dict[str, Any]] = None, *, who: Optional[str] = None,
                      why: str = "resume", transient: bool = False) -> None:
        """``transient``: a run that failed transiently goes on too, rewound (runner.resume) -- the same request
        again (_by_key); an operator's resume of it still replays that failure."""
        row = self.run_store.get_run(run_id) or {}
        if row.get("status") in ACTIVE_STATUSES and run_id not in self.runs.live:
            self.sweep()  # its process may be gone: then its lease is dropped and the resume takes it now
            row = self.run_store.get_run(run_id) or row
        if self.run_store.compacted(run_id):  # LC-R3: an empty journal would run every step live again
            raise ServiceError(409, f"run {run_id} is compacted (retention): its journal is gone, so no resume replays "
                                    "it; start the machine again")
        error = row.get("error") or {}
        if row.get("status") == "failed" and not resumable_failure(error) and not (
                transient and failed_transiently(error)):
            # a resume replays the journal up to that failure, and it fails the same way (API-10); an internal error
            # is the engine's -- a fixed engine may get past it
            raise ServiceError(409, f"run {run_id} failed ({error.get('type')}): a resume replays that failure; fork "
                                    "it from an earlier step (at_step) or onto the current definition")
        if self.run_store.half_made_fork(run_id):  # RES-6: it would run the rest of its source's prefix live
            self._quietly(self.runs.terminate_elsewhere, run_id, "fork not created: copying its source's journal "
                                                                 "broke off; fork the source again")
            raise ServiceError(409, f"run {run_id} is a fork whose making broke off: a resume would run its source's "
                                    "steps again; fork the source again")
        options = row.get("mocks") or {}
        try:
            await self.runs.resume(run_id, live=live, backend_factory=None if options.get("mock_only")
                                   else self.backend_factory(), transient=transient)
        except (ValueError, CompileError) as exc:
            raise ServiceError(409, str(exc)) from None
        self._note(run_id, "resumed", who=who, why=why, machine=row.get("machine_id"), was=row.get("status"),
                   **({"error": error.get("type")} if error else {}))
        skew = await asyncio.to_thread(self._skew, row)  # loads the machine and its snapshot: off the loop (API-2)
        if skew:  # FEAT-4: nothing changes for the run; whoever reads its history knows what it runs against now
            self._note(run_id, "code_changed", who=who, why="; ".join(skew), level=logging.WARNING,
                       machine=row.get("machine_id"))

    def _skew(self, row: dict[str, Any]) -> list[str]:
        """What changed since the run's snapshot was taken (FEAT-4): a companion module whose file reads otherwise
        now (the run keeps its snapshot's text, but a fork onto the current definition would not), a module its
        machines call or its companion modules import whose file changed since the run started -- this process may
        run older code of it than the file, or newer than the run began with -- and a function it calls that this
        process cannot import."""
        import ast
        import importlib
        import importlib.util

        from agent_system.paths import PROJECT_ROOT
        from .engine.machine import _module_refs

        found: list[str] = []
        try:
            definition = row.get("definition") or {}
            started = _timestamp(row.get("created_at"))
            texts = {rel: text for rel, text in (definition.get("files") or {}).items() if rel.endswith(".py")}
            if self.store.find(row.get("machine_id") or "") is not None:
                current, _ = self._files_of(row["machine_id"], self.store.load(row["machine_id"]))
                found += [f"companion module {rel} differs from the run's snapshot" for rel, text in sorted(texts.items())
                          if rel in current and current[rel] != text]
            modules: dict[str, str] = {}
            tree = load_snapshot(definition, execute_python=False)
            for loaded in tree.files.values():
                if loaded.spec is not None:
                    for _, ref in _module_refs(loaded.spec.model_dump(by_alias=True, exclude_none=True), ""):
                        name, _, attr = ref.partition(":")
                        modules.setdefault(name, ref)
            for text in texts.values():
                try:
                    nodes = ast.walk(ast.parse(text))
                except SyntaxError:
                    continue
                for node in nodes:
                    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        modules.setdefault(node.module, "")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            modules.setdefault(alias.name, "")
            project = os.path.realpath(str(PROJECT_ROOT))
            for name, ref in sorted(modules.items()):
                try:
                    spec = importlib.util.find_spec(name)
                except Exception:
                    spec = None
                origin = getattr(spec, "origin", None)
                if ref and (spec is None or origin is None):
                    found.append(f"cannot import {ref}")
                    continue
                if not origin or not os.path.isfile(origin) or "site-packages" in origin \
                        or not os.path.realpath(origin).startswith(project + os.sep):
                    continue  # the project's own code only: libraries change with a deploy, not a pull
                if started is not None and os.path.getmtime(origin) > started:
                    found.append(f"{name} changed since the run started")
                if ref:
                    try:
                        if not callable(getattr(importlib.import_module(name), ref.partition(":")[2])):
                            found.append(f"{ref} is not callable")
                    except Exception as exc:
                        found.append(f"cannot import {ref} ({type(exc).__name__})")
        except Exception:  # a warning that cannot be worked out is no reason to stop the resume
            logger.debug("stategraph: the code of run %s not compared", row.get("id"), exc_info=True)
        return found

    async def _await_end(self, run_id: str, timeout: float) -> None:
        """Until the run's task here -- or another process's run, by its row -- has ended, at most ``timeout``
        seconds (the run itself is never cancelled)."""
        live = self.runs.live.get(run_id)
        if live is None:
            deadline = time.monotonic() + timeout
            while ((self.run_store.get_run(run_id) or {}).get("status") not in TERMINAL_STATUSES
                   and time.monotonic() < deadline):
                await asyncio.sleep(0.2)
            return
        try:
            await asyncio.wait_for(asyncio.shield(live.task), timeout)
        except asyncio.TimeoutError:
            logger.info("stategraph: run %s still ends after %.0fs (finally activities)", run_id, timeout)

    async def _control_elsewhere(self, run_id: str, action: str) -> bool:
        """Hand ``action`` to the process that holds the run (it looks each second): whether one holds it. One that
        does not take it within CONTROL_WAIT gets it withdrawn, and the caller hears so."""
        request = {"action": action, "id": os.urandom(6).hex()}  # its own: a withdraw leaves another's alone
        asked = self.run_store.request_control(run_id, request, now=utc_now()) if action in REMOTE_CONTROLS else ""
        if asked == "open":
            raise ServiceError(409, f"run {run_id}: another control request waits for the process that holds it; "
                                    "try again in a moment")
        if asked != "requested":
            return False
        try:
            deadline = time.monotonic() + CONTROL_WAIT
            while (self.run_store.get_run(run_id) or {}).get("control") == request and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
        finally:  # not taken in time, or the asker stopped: taken back, so no later resume meets it
            withdrawn = self.run_store.withdraw_control(run_id, request)
        if withdrawn:
            raise ServiceError(409, f"run {run_id} is held by {(self.run_store.get_run(run_id) or {}).get('owner')},"
                                    f" which did not take the {action} within {CONTROL_WAIT:.0f}s")
        return True

    async def _event_elsewhere(self, run_id: str, name: str, data: Any, frame: Optional[str], *,
                               request_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Hand an event to the process that holds the run (it looks each second) and wait for what it made of it:
        its answer, or None when no process holds a live lease on the run. ``request_id`` names the request, for a
        caller that asks runs.db afterwards whether it was taken (``control_answer``)."""
        request = {"action": "event", "id": request_id or os.urandom(6).hex(), "name": name, "data": data,
                   "frame": frame}
        asked = self.run_store.request_control(run_id, request, now=utc_now())
        if asked == "open":
            raise ServiceError(409, f"run {run_id}: another control request waits for the process that holds it; "
                                    "try again in a moment")
        if asked != "requested":
            return None
        answer: dict[str, Any] = {}
        try:
            deadline = time.monotonic() + CONTROL_WAIT
            while time.monotonic() < deadline:
                answer = self.run_store.control_answer(run_id, request["id"]) or {}
                if "accepted" in answer:  # not the mark that it was taken: what came of it
                    return answer  # its holder cleared the request before it answered: nothing to take back
                await asyncio.sleep(0.1)
        finally:  # not taken in time, or the asker stopped: taken back, so no later owner meets it
            if "accepted" not in answer:
                withdrawn = self.run_store.withdraw_control(run_id, request)
        owner = (self.run_store.get_run(run_id) or {}).get("owner")
        if withdrawn:
            raise ServiceError(409, f"run {run_id} is held by {owner}, which did not take the event within "
                                    f"{CONTROL_WAIT:.0f}s")
        answer = self.run_store.control_answer(run_id, request["id"]) or {}  # answered just before the withdraw
        if "accepted" in answer:
            return answer
        if not answer.get("taken"):  # a sweep or a new owner cleared it, or a holder that takes no events
            raise ServiceError(409, f"run {run_id}: the event did not reach the process that holds it (its lease ran "
                                    "out, another process took the run over, or it takes no events from elsewhere); "
                                    "send it again")
        raise EventOutcomeUnknown(409, f"run {run_id} is held by {owner}, which took the event but did not say "
                                       f"within {CONTROL_WAIT:.0f}s what came of it: read the run before sending "
                                       "it again")

    def _taken_elsewhere(self, run_id: str, request_id: str) -> bool:
        """Whether another process took the event request ``request_id`` and did not refuse it: its mark, or its
        answer that it accepted, is in runs.db."""
        try:
            answer = self.run_store.control_answer(run_id, request_id) or {}
        except Exception:  # the database that failed the call: what it cannot say, it did not take
            return False
        return bool(answer.get("taken") or answer.get("accepted"))

    async def _terminate_elsewhere(self, run_id: str, who: Optional[str] = None, *,
                                   reason: Optional[str] = None) -> None:
        """A run no process here runs: resume it into its termination, so its finally activities run (§3.10).

        A run that ended is left alone -- but for one that failed resumably, which a terminate closes for good
        (FEAT-8): cancelled, its error saying who closed it and on what it had stopped (its finally activities ran
        at the failure). One another process holds is refused (409). If it cannot be resumed at all (its definition
        no longer loads), it is marked cancelled without them, and its error says so.
        """
        row = self.run_store.get_run(run_id) or {}
        if row.get("status") == "failed" and resumable_failure(row.get("error")):  # its children stayed for a resume
            self._close(run_id, row, who)
            await self.end_children(run_id)  # no task ends here: on_finish does not see this end (FEAT-1)
            return
        if row.get("status") in ("succeeded", "failed", "cancelled"):
            return
        options = row.get("mocks") or {}
        try:
            await self.runs.resume(run_id, cancel=True, backend_factory=None if options.get("mock_only")
                                   else self.backend_factory(), cancel_reason=reason)
        except CompileError as exc:
            self.runs.terminate_elsewhere(run_id, "terminated while interrupted; its finally activities did not "
                                                  f"run: its definition does not load ({exc})")
            await self.end_children(run_id)  # nor this one
        self._note(run_id, "terminated", who=who, why="terminate while no process ran it", was=row.get("status"))

    def _close(self, run_id: str, row: dict[str, Any], who: Optional[str]) -> None:
        """A run that failed resumably, closed: no resume runs it on (FEAT-8)."""
        owner = self.runs.owner
        if not self.run_store.take_lease(run_id, owner, utc_now(), now=utc_now()):
            raise ServiceError(409, f"run {run_id} is held by {row.get('owner')}; try again in a moment")
        error = row.get("error") or {}
        closed = {"type": "cancelled", "message": f"closed by {who or 'an operator'} after it stopped on a resumable "
                                                  f"failure ({error.get('type')}: {error.get('message') or ''})"[:2000],
                  "closed": {key: error.get(key) for key in ("type", "message", "state")}}
        if not self.run_store.update_run(run_id, fence=owner, status="cancelled", error=closed,
                                         finished_at=utc_now(), lease_until=""):
            raise ServiceError(409, f"run {run_id} was taken by another process meanwhile; try again")
        self._note(run_id, "closed", who=who, why="terminate of a run that failed resumably",
                   error=error.get("type"))

    async def _terminate(self, run_id: str, who: Optional[str], *, reason: Optional[str] = None) -> None:
        """Terminate a run wherever it is: here, in the process that holds it, or -- held by none -- here.
        ``reason``: named in its error (``terminated by <reason>``) when it ends here."""
        if run_id in self.runs.live:
            self.runs.control(run_id, "terminate", reason=reason)
            self._note(run_id, "terminate", who=who, why="control_run")
        elif not await self._control_elsewhere(run_id, "terminate"):  # none holds it: resumed into its end here
            await self._terminate_elsewhere(run_id, who, reason=reason)
        else:
            self._note(run_id, "terminate", who=who, why="handed to the process that holds it")

    async def end_children(self, run_id: str, *, interrupted_only: bool = False) -> None:
        """The runs a run that ended started from its call activities and that have not ended, terminated too
        (FEAT-1) -- a run_to_end child goes with its caller already; one started in the background would not.
        ``interrupted_only`` (the caller succeeded or failed for good): only the interrupted ones -- nobody resumes
        those any more; a live one started in the background goes on. Their error names the caller."""
        for child in self.run_store.children(run_id, unended=True):
            if child["status"] in TERMINAL_STATUSES or (interrupted_only and child["status"] != "interrupted"):
                continue
            try:
                await self._terminate(child["id"], f"run {run_id}", reason=f"its caller run {run_id} ending")
            except Exception:
                logger.warning("stategraph: child run %s of the terminated run %s not terminated", child["id"], run_id,
                               exc_info=True)

    def _control_children(self, run_id: str, action: str, _seen: Optional[set[str]] = None) -> None:
        """A pause or continue of a run here, passed on to the live runs it started from its call activities here,
        and to theirs (FEAT-1): a pause halts the whole tree at its next hooks. A continue reaches only a child that
        the caller's pause halted -- not one paused at its own breakpoint or by its own pause, nor one a step of its
        own drives. Nothing goes up: a child's pause is its own. Each run once: a resume by run_key makes its
        resumer the caller, and that may be its own child -- a cycle (RT-4)."""
        _seen = _seen if _seen is not None else set()
        if run_id in _seen:
            return
        _seen.add(run_id)
        if action == "pause":
            self._paused_by_caller &= set(self.runs.live)  # the ones that ended meanwhile
        for child in self.run_store.children(run_id, unended=True):
            live = self.runs.live.get(child["id"])
            if live is None or live.ctx.finished or live.ctx.cancelled:
                continue
            if action == "continue":
                passed = child["id"] in self._paused_by_caller
                self._paused_by_caller.discard(child["id"])
            else:  # paused already on its own: its own continue releases it, the caller's does not
                passed = not (live.ctx.status == "paused" or live.ctx.debugger.mode == "pause")
            if passed:
                try:
                    self.runs.control(child["id"], action)
                except Exception:
                    logger.warning("stategraph: %s of run %s not passed on to its child run %s", action, run_id,
                                   child["id"], exc_info=True)
                    continue
                if action == "pause":
                    self._paused_by_caller.add(child["id"])
            self._control_children(child["id"], action, _seen)  # its own children: the caller's pause reaches them all

    def _run(self, run_id: str, user_id: Optional[str]) -> dict[str, Any]:
        """The run's row for a user who may see it (§8.3): an admin every run, anyone else their own -- another
        user's run answers like one that does not exist."""
        row = self.run_store.get_run(run_id)
        if row is None or not self.server.sees_run(user_id, row.get("user_id")):
            raise ServiceError(404, f"no run {run_id!r}")
        return row

    def list_runs(self, machine_id: Optional[str] = None, limit: int = 50, *, status: Optional[str] = None,
                  user_id: Optional[str] = None, all_users: bool = True,
                  before: Optional[str] = None, nested: bool = False,
                  tags: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """A page of the newest runs; ``before``: the last run id of the page before; ``nested``: also the runs the
        machine ran in as a submachine; ``tags``: only runs with all of these tags (FEAT-7)."""
        if status is not None and status not in RUN_STATUSES:
            raise ServiceError(422, f"status must be one of {', '.join(RUN_STATUSES)}, not {status!r}")
        _check_tags(tags)
        return self.run_store.list_runs(machine_id, limit=max(1, min(int(limit), 500)), status=status,
                                        user_id=user_id, all_users=all_users, before=before, nested=nested,
                                        tags=tags)

    def get_run(self, run_id: str, steps: int = 50, *, user_id: Optional[str] = None, after: Optional[int] = None,
                kinds: Optional[list[str]] = None, state: Optional[str] = None,
                frames: bool = False, bookkeeping: bool = True) -> dict[str, Any]:
        """A run and its journal rows: the last ``steps`` -- or, with ``after``, the first ``steps`` after that seq;
        only rows of ``kinds`` and of ``state`` when given; ``bookkeeping=False``: without the engine's own trace
        rows (journal.BOOKKEEPING). ``frames``: with the submachine frames it started (``frames_started``: prefix,
        machine, path)."""
        self._run(run_id, user_id)
        _check_steps(steps)
        if after is not None and not (_is_int(after) and after >= 0):
            raise ServiceError(422, f"after must be a journal seq (0 or more), not {after!r}")
        unknown = sorted(set(kinds or ()) - set(JOURNAL_KINDS))
        if unknown or (kinds is not None and not isinstance(kinds, list)):
            raise ServiceError(422, f"kinds: a list of {', '.join(JOURNAL_KINDS)}, not {kinds!r}")
        if state is not None and not isinstance(state, str):
            raise ServiceError(422, f"state must be a state name, not {state!r}")
        try:
            row = self.runs.describe(run_id)
        except KeyError:
            raise ServiceError(404, f"no run {run_id!r}") from None
        if row["status"] == "failed":  # a resume takes it on (journal.resumable_failure): the panel's Restart and Close
            row["resumable"] = resumable_failure(row.get("error"))
        view = row.get("view") or {}
        row["accepts"] = [{"frame": f.get("prefix", ""), "state": f.get("state"), "events": f.get("accepts") or []}
                          for f in view.get("frames", []) if f.get("accepts")]
        wanted = kinds or JOURNAL_KINDS
        row["journal"] = self._marked(run_id, row["status"], (
            self.run_store.tail(run_id, limit=steps, kinds=wanted, state=state, bookkeeping=bookkeeping)
            if after is None else self.run_store.page(run_id, after=after, limit=steps, kinds=wanted, state=state,
                                                      bookkeeping=bookkeeping)))
        tags = self.run_store.tags_of([run_id]).get(run_id)
        if tags:
            row["tags"] = tags
        if frames:
            row["frames_started"] = self.run_store.frames(run_id)
        row["spend"] = self.run_store.spend(run_id, children=True)
        # what bounds it (FEAT-6): its own definition's limits.max_cost, and its own run.cost -- not its children's
        row["max_cost"], row["cost"] = self.runs.budget(run_id)
        children = self.run_store.children(run_id)
        if children:  # the runs its call activities started (FEAT-1)
            row["children"] = children
        if row.get("caller_run"):
            row["started_by"] = {"run_id": row["caller_run"], "request_id": row.get("caller_request")}
        return row

    def _marked(self, run_id: str, status: Optional[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The ``started`` activity rows that will never get an outcome (runner.ended_without_outcome) marked
        ``ended_without_outcome``: a reader shows them as ended, not in flight (PROD-6)."""
        started = [r for r in rows if r["kind"] == "activity" and r.get("status") == "started"]
        if not started:
            return rows
        known = [{"kind": "activity", "key": key, "status": "done"} for key in self.run_store.ended_activity_keys(run_id)]
        dead = ended_without_outcome(known + started, terminal=status in TERMINAL_STATUSES)
        for r in started:
            if r["key"] in dead:
                r["ended_without_outcome"] = True
        return rows

    def journal(self, run_id: str, after: int = 0, limit: int = 200, kinds: Optional[list[str]] = None, *,
                before: Optional[int] = None, state: Optional[str] = None,
                seq: Optional[int] = None) -> list[dict[str, Any]]:
        """A page of the run's journal: the rows after ``after`` -- or, with ``before``, the last ones before that
        seq (paging back through a long run, UI-16) -- of ``kinds`` and ``state``; ``seq``: that one row, whole."""
        row = self.run_store.get_run(run_id)
        if row is None:
            raise ServiceError(404, f"no run {run_id!r}")
        unknown = sorted(set(kinds or ()) - set(JOURNAL_KINDS))
        if unknown:
            raise ServiceError(422, f"kinds: {', '.join(JOURNAL_KINDS)}, not {', '.join(unknown)}")
        limit = max(1, min(int(limit), 1000))
        if seq is not None:
            found = self.run_store.row_at(run_id, seq)
            if found is None:
                raise ServiceError(404, f"run {run_id} has no journal row {seq}")
            rows = [found]
        elif before is not None:
            rows = self.run_store.tail(run_id, limit=limit, kinds=kinds, state=state, before=before)
        else:
            rows = self.run_store.page(run_id, after=after, limit=limit, kinds=kinds, state=state)
        return self._marked(run_id, row["status"], rows)

    async def control_run(self, run_id: str, action: str, *, user_id: Optional[str] = None, steps: int = 50,
                          **kwargs: Any) -> dict[str, Any]:
        """``user_id`` asks: they may control the runs they may see (``_run``); a resume, fork's source or terminate
        runs as the run's own user all the same (backend_factory). ``steps``: the journal rows of the answer, as
        ``get_run``'s."""
        self._run(run_id, user_id)
        _check_steps(steps)  # with the other arguments: nothing acts before all of them hold
        _check_control_args(kwargs)
        try:
            if action == "terminate":
                await self._terminate(run_id, user_id)
                await self._await_end(run_id, TERMINATE_WAIT)  # its finally activities run first
            elif action in ("pause", "continue", "step", "run_to"):
                state = _required(kwargs, "state") if action == "run_to" else kwargs.get("state")
                if run_id in self.runs.live or not await self._control_elsewhere(run_id, action):
                    self.runs.control(run_id, action, state=state, machine=kwargs.get("machine"))
                if action in ("pause", "continue") and run_id in self.runs.live:
                    self._paused_by_caller.discard(run_id)  # its own pause or continue now: the caller's no longer
                    self._control_children(run_id, action)
            elif action == "resume":
                await self._resume(run_id, who=user_id, why="control_run")
            elif action == "fork":
                return await self._fork(run_id, kwargs, user_id)
            elif action == "set_breakpoints":
                self.runs.set_points(run_id, breakpoints=kwargs.get("breakpoints") or [])
            elif action == "set_watchpoints":
                self.runs.set_points(run_id, watchpoints=kwargs.get("watchpoints") or [])
            elif action == "evaluate":
                return {"value": self.runs.evaluate(run_id, _required(kwargs, "expr"))}
            elif action == "set":
                if run_id not in self.runs.live and _stopped(self.run_store.get_run(run_id) or {}):
                    return {"value": self._set_stopped(run_id, _required(kwargs, "path"), _required(kwargs, "expr"),
                                                       user_id)}
                return {"value": self.runs.assign(run_id, _required(kwargs, "path"), _required(kwargs, "expr"))}
            else:
                raise ServiceError(422, f"unknown action {action!r}")
        except ServiceError:
            raise
        except UnknownState as exc:
            raise ServiceError(422, str(exc)) from None
        except ValueError as exc:
            raise ServiceError(409, str(exc)) from None
        except Exception as exc:
            from .model.code import CodeError

            if isinstance(exc, CodeError):
                raise ServiceError(422, exc.message) from None
            raise
        # off the loop: a run that is no longer live (a terminate) parses its definition, a second or more (RT-2)
        return await asyncio.to_thread(self.get_run, run_id, steps=steps, user_id=user_id)

    def _set_stopped(self, run_id: str, path: str, expr: str, who: Optional[str]) -> Any:
        """``set`` on an interrupted run, or one that failed resumably (FEAT-9): ``ctx.<path>`` -- journaled as a debugger edit where the run
        stands, applied when its resume replays to there -- or ``params.<name>``: into the run's params while it has
        recorded no step; else journaled the same way (LC-R1), so the steps before read the value they ran with.
        ``expr`` is a value (JSON or a Python literal): a stopped run has no state to evaluate an expression
        against. Under the run's lease, so no process resumes it meanwhile; a lifecycle row says who set what."""
        import ast

        try:
            value = json.loads(expr)
        except ValueError:
            try:
                value = ast.literal_eval(expr)
            except (ValueError, SyntaxError):
                raise ServiceError(422, "a stopped run has no state to evaluate an expression against: give a value "
                                        "(JSON or a Python literal)") from None
        try:
            value = json.loads(json.dumps(value))
        except (TypeError, ValueError):
            raise ServiceError(422, "the value must be JSON data") from None
        target = path.strip()
        if target.startswith("params."):
            name = target[len("params."):]
            if not name or "." in name:
                raise ServiceError(422, "set params.<name>: one param, a whole value")
            self._check_param(run_id, name, value)
        elif not target.startswith("ctx.") or not target[len("ctx."):].strip("."):
            raise ServiceError(422, "set ctx.<path> or params.<name> of a stopped run")
        owner = self.runs.owner
        if not self.run_store.take_lease(run_id, owner, _utc_in(LEASE_SECONDS), now=utc_now()):
            raise ServiceError(409, f"run {run_id} is held by another process; set it there")
        try:
            row = self.run_store.get_run(run_id) or {}
            if not _stopped(row):
                raise ServiceError(409, f"run {run_id} is {row.get('status')} now")
            param = target.startswith("params.")
            if param and not self._has_steps(run_id):  # nothing recorded read them: the run's params from the start
                params = {**(row.get("params") or {}), target[len("params."):]: value}
                if not self.run_store.update_run(run_id, fence=owner, params=params):
                    raise ServiceError(409, f"run {run_id} was taken by another process meanwhile")
                at = None
            else:  # a param too once steps are recorded (LC-R1): they replay with the params they ran with, and the
                # engine sets it where the run stands (the run's params keep the start's)
                at = self._frontier(run_id, row)
                edits = sum(1 for r in self.run_store.rows(run_id, kinds=("edit",)))
                if not self.run_store.record(run_id, "edit", f"{at}:{edits + 1}", status="applied", fence=owner,
                                             data={"at": at, "path": target, "value": value,
                                                   **({"param": True} if param else {})}):
                    raise ServiceError(409, f"run {run_id} was taken by another process meanwhile")
            self._note(run_id, "set", who=who, why="set before its resume", path=target, value=value, at=at)
        finally:
            self._quietly(self.run_store.update_run, run_id, fence=owner, lease_until="")
        return value

    def _has_steps(self, run_id: str) -> bool:
        """Whether the run journaled anything its replay checks against the params: an activity, event, timer,
        edit, or a step's trace row."""
        return any(row["kind"] != "trace" or row.get("status") == "step"
                   for row in self.run_store.rows(run_id, kinds=("activity", "event", "timer", "edit", "trace")))

    def _check_param(self, run_id: str, name: str, value: Any) -> None:
        """A param set before a resume must be one the run's machine declares, of its type: the resume binds them."""
        from .engine.interpreter import bind_params
        from .engine.runner import null_params_kept
        from .kinds import ActivityError

        row = self.run_store.get_run(run_id) or {}
        try:
            spec = load_snapshot(row.get("definition") or {}, execute_python=False).root_file.spec
        except Exception as exc:
            raise ServiceError(409, f"run {run_id}: its definition does not load ({exc})") from None
        try:
            bind_params(spec.params, {**(row.get("params") or {}), name: value}, row.get("machine_id") or "",
                        null_is_value=null_params_kept(row))
        except ActivityError as exc:
            raise ServiceError(422, exc.message) from None

    def _frontier(self, run_id: str, run: dict[str, Any]) -> str:
        """Where an edit of a stopped run applies on its resume: the root frame's last step -- at its exit hook when
        that step has an activity (done, or in flight: it runs again with the inputs it had, an edit before it would
        make its replay diverge), an event or a timer, so the value feeds the transition; else at its enter hook (a
        wait goes on, a state's activity has not begun). Of a run that failed resumably, what its resume sets aside
        (rewound_rows: the failed activity runs again) counts as not begun -- the rerun reads the value. One that
        failed internally is resumed without a rewind (runner.resume): it stands where it stopped, as interrupted."""
        rows = self.run_store.rows(run_id, kinds=("activity", "event", "timer", "trace", "edit"))
        aside: set[tuple[str, str]] = set()
        if run.get("status") == "failed" and (run.get("error") or {}).get("resumable"):
            try:
                aside = {(r["kind"], r["key"]) for r in rewound_rows(self.run_store.rows(run_id))}
            except NotRewindable as exc:
                raise ServiceError(409, f"run {run_id} cannot be resumed: {exc}") from None
        last, begun = 0, False
        for row in rows:
            kind, key, data = row["kind"], row["key"], row.get("data") or {}
            if key.startswith("rewound.") or (kind, key) in aside:
                continue
            anchor = data.get("at", key) if kind == "edit" else key
            head = anchor if kind == "activity" else anchor.rpartition(":")[0] if ":" in anchor else anchor
            steps = key_steps(head)
            if not steps or steps[0][0] != "" or (kind == "trace" and row.get("status") != "step"):
                continue
            step = steps[0][1]
            here = kind == "activity" or (kind in ("event", "timer") and key in (f"s{step}:event", f"s{step}:timer"))
            if step > last:
                last, begun = step, here
            elif step == last:
                begun = begun or here
        return f"s{last}:{'exit' if begun else 'enter'}"

    async def fork_run(self, run_id: str, *, user_id: Optional[str] = None, fork_id: Optional[str] = None,
                       **kwargs: Any) -> dict[str, Any]:
        """control_run's fork from code, as ``fork_id`` when given: ``<request id>_sg<n>`` keeps the fork's cancel,
        status and cost under the caller's request id, as start_run's ``run_id`` does (agent_eval forks so)."""
        self._run(run_id, user_id)
        _check_control_args(kwargs)
        if fork_id is not None and not _RUN_ID.fullmatch(fork_id):
            raise ServiceError(422, f"fork_id {fork_id!r} must match {_RUN_ID.pattern}")
        return await self._fork(run_id, kwargs, user_id, fork_id)

    async def _fork(self, run_id: str, kwargs: dict[str, Any], user_id: Optional[str],
                    fork_id: Optional[str] = None) -> dict[str, Any]:
        """A fork is a new run, and it is the forking user's: they started it. It never continues an agent instance
        of its source (§5.6: its own session refuses them), so no conversation of the source's user runs on under
        another; its new instances are the fork's user's. Without a user (no auth) it keeps the source's.
        ``breakpoints`` and ``watchpoints`` replace the source's (without them the fork keeps the source's)."""
        row = self.run_store.get_run(run_id)
        assert row is not None
        tree = None
        if kwargs.get("definition") == "current":
            self._require(row["machine_id"])
            # off the event loop, as a start's (API-2)
            checked = await asyncio.to_thread(lambda: self._validate(self.store.load(row["machine_id"])))
            if not checked.ok:
                raise ServiceError(422, "the current definition has errors; fix them before forking onto it")
            tree = await asyncio.to_thread(load_snapshot, checked.snapshot())  # exactly what was validated
        if self.run_store.compacted(run_id):
            raise ServiceError(422, f"run {run_id} is compacted (retention): its journal is gone, so no fork replays "
                                    "it; start the machine again")
        options = row.get("mocks") or {}
        try:
            new_id = await self.runs.fork(run_id, at_step=kwargs.get("at_step"), tree=tree,
                                          backend_factory=None if options.get("mock_only")
                                          else self.backend_factory(), user_id=user_id or row.get("user_id"),
                                          pause_at_start=bool(kwargs.get("pause")), mocks=kwargs.get("mocks"),
                                          breakpoints=kwargs.get("breakpoints"),
                                          watchpoints=kwargs.get("watchpoints"), fork_id=fork_id,
                                          # the snapshot keeps the source's runner; the current file its folder's
                                          runner=self._runner_of(checked) if tree else None)
        except (ValueError, CompileError) as exc:
            raise ServiceError(422, str(exc)) from None
        logger.info("stategraph: run %s of %s forked from %s at step %s by %s in %s", new_id, row["machine_id"], run_id,
                    kwargs.get("at_step"), user_id or "-", self.runs.owner)
        tags = self.run_store.tags_of([run_id]).get(run_id)
        if tags:  # the same business object: a fork is found by the source's tags
            self._quietly(self.run_store.set_tags, new_id, tags)
        callbacks = self._fork_callbacks(new_id)
        return {"run_id": new_id, "forked_from": run_id, **({"callbacks": callbacks} if callbacks else {})}

    def _fork_callbacks(self, fork_id: str) -> list[dict[str, Any]]:
        """The callback URLs the fork replays from its source (API-4): their tokens send to the run that made them.
        One whose run has ended goes to the fork, its URL in the fork's ctx as it is; one of a run that goes on
        stays that run's, and the fork gets a new URL for the same event, frame and expiry -- in the answer and its
        ``callbacks`` lifecycle row (the fork's ctx keeps the source's URL)."""
        import hashlib
        import secrets

        from .server import CALLBACK_TOKEN

        now = time.time()
        made: list[dict[str, Any]] = []
        for url in self.run_store.callback_urls(fork_id):
            for token in CALLBACK_TOKEN.findall(url):
                digest = _digest(token)
                held = self.run_store.callback(digest, now)
                if held is None or held["run_id"] == fork_id:
                    continue  # used, expired, or the fork's already
                ended = (self.run_store.get_run(held["run_id"]) or {}).get("status") in TERMINAL_STATUSES
                if ended and self.run_store.move_callback(digest, fork_id, now):
                    made.append({"event": held["event"], "url": url, "moved_from": held["run_id"]})
                    continue
                fresh = secrets.token_urlsafe(32)
                self.run_store.add_callback(hashlib.sha256(fresh.encode()).hexdigest(), fork_id, held["event"],
                                            held.get("frame"), held["expires_at"], now=now)
                made.append({"event": held["event"], "url": f"{self.runs.callback_base}?token={fresh}"})
        if made:  # the URLs hold tokens: a journal row (hidden from who may not hold them), never a log line
            self._quietly(self.run_store.lifecycle, fork_id, "callbacks",
                          {"why": "the fork's callback URLs (its ctx keeps the source's)", "callbacks": made})
        return made

    def callback(self, token: str) -> dict[str, Any]:
        """What a callback URL would send: {event, machine_id, expires}; 404 when it does not hold (never said why)."""
        row = self.run_store.callback(_digest(token), time.time())
        run = self.run_store.get_run(row["run_id"]) if row else None
        if row is None or run is None or run["status"] in TERMINAL_STATUSES:  # an ended run takes no event any more
            raise ServiceError(404, "no such callback")
        return {"event": row["event"], "machine_id": run["machine_id"], "expires": row["expires_at"]}

    async def use_callback(self, token: str, data: Any) -> dict[str, Any]:
        """Send a callback URL's event -- once: the URL is used when the run took it (or keeps it in its inbox). A
        run no process runs is resumed first; a run another process holds gets it through runs.db (as a pause from
        the panel), and its answer is that process's."""
        digest = _digest(token)
        self.callback(token)  # an ended run's URL is gone, not used up by a call that cannot land
        if not self.run_store.use_callback(digest, time.time()):
            raise ServiceError(404, "no such callback")
        row = self.run_store.callback_row(digest) or {}
        request_id = os.urandom(6).hex()  # if it goes to another process: whether that one took it, runs.db says
        try:
            run = self.run_store.get_run(row.get("run_id", "")) or {}
            if run.get("id") not in self.runs.live and run.get("status") in ("interrupted", "waiting", "running",
                                                                              "paused"):
                self.sweep()  # a dead owner's lease ran out, or its process is gone: it is interrupted now
                run = self.run_store.get_run(run["id"]) or run
                if run.get("status") == "interrupted":
                    await self._resume(run["id"], who="callback", why="a callback URL's event")
                    await self.runs.wait(run["id"], timeout=10.0)  # into its wait again
            answer = None
            if run.get("id") not in self.runs.live:  # another process holds it: handed over through runs.db
                answer = await self._event_elsewhere(row["run_id"], row["event"], data, row.get("frame"),
                                                     request_id=request_id)
            if answer is None:
                answer = self.runs.send_event(row["run_id"], row["event"], data, row.get("frame"))
        except EventOutcomeUnknown as exc:  # it most likely landed: the URL stays used, so it does not fire twice
            logger.info("stategraph: callback for run %s: %s", row.get("run_id"), exc.message)
            return {"sent": row["event"], "queued": None, "outcome": "unknown"}
        except (KeyError, ValueError, ServiceError) as exc:
            self.run_store.unuse_callback(digest)
            # the caller holds a URL, not an account: which process holds the run is the log's, not theirs
            logger.info("stategraph: callback for run %s not taken: %s", row.get("run_id"), getattr(exc, "message", exc))
            raise ServiceError(409, "the run cannot take the event now; try again later") from None
        except BaseException:  # anything else (a closed database, a cancelled request): the event did not land --
            # unless another process took it before the request was withdrawn: then it most likely did
            if not self._taken_elsewhere(row.get("run_id", ""), request_id):
                self.run_store.unuse_callback(digest)
            raise
        if not answer.get("accepted"):
            self.run_store.unuse_callback(digest)  # not taken: the URL holds for a corrected or later call
            if answer.get("data_refused"):  # what was wrong with the caller's own data is theirs to hear
                raise ServiceError(422, str(answer.get("reason")))
            logger.info("stategraph: callback for run %s not taken: %s", row.get("run_id"), answer.get("reason"))
            raise ServiceError(409, "the run cannot take the event now; try again later")
        return {"sent": row["event"], "queued": bool(answer.get("queued"))}

    async def deliver_event(self, run_id: str, name: str, data: Any = None, frame: Optional[str] = None, *,
                            user_id: Optional[str] = None) -> dict[str, Any]:
        """``send_event`` for a run any process holds: this one's here, another process's through runs.db -- it
        takes it within a second, as a pause from the panel (agent-cli and agent-run hold the runs they start).
        A run nobody holds is refused as before (409): resume it first."""
        self._run(run_id, user_id)
        if run_id not in self.runs.live:
            answer = await self._event_elsewhere(run_id, name, data, frame)
            if answer is not None:
                return answer
        return self.send_event(run_id, name, data, frame, user_id=user_id)

    def send_event(self, run_id: str, name: str, data: Any = None, frame: Optional[str] = None, *,
                   user_id: Optional[str] = None) -> dict[str, Any]:
        self._run(run_id, user_id)
        try:
            return self.runs.send_event(run_id, name, data, frame)
        except KeyError:
            raise ServiceError(404, f"no run {run_id!r}") from None
        except ValueError as exc:
            raise ServiceError(409, str(exc)) from None



#: The empty inject values already reported: one error per value, not one per run.
_EMPTY_INJECT: set[tuple[str, str, str]] = set()


def usable_inject(inject: dict[str, dict[str, Any]], where: str) -> dict[str, dict[str, Any]]:
    """``inject_params`` without its empty values (an unset ``${VAR}`` reads as ""): such a value is not injected,
    and an error names it once (API-11) -- the tool's own error then says what is missing."""
    usable: dict[str, dict[str, Any]] = {}
    for pattern, extra in inject.items():
        kept = {}
        for param, value in (extra or {}).items():
            if value is None or value == "":
                if (where, pattern, param) not in _EMPTY_INJECT:
                    _EMPTY_INJECT.add((where, pattern, param))
                    logger.error("stategraph: inject_params of %s: %s.%s is empty (an unset variable?) -- not "
                                 "injected", where, pattern, param)
                continue
            kept[param] = value
        usable[pattern] = kept
    return usable


def _keeps(keep_on_cancel: Callable[[], bool], run_id: str) -> bool:
    """``run_to_end``'s question after its caller's cancel; a question that breaks terminates, as before."""
    try:
        return bool(keep_on_cancel())
    except Exception:
        logger.warning("stategraph: keep_on_cancel of %s raised; the run is terminated", run_id, exc_info=True)
        return False


def _digest(token: str) -> str:
    """A callback token as runs.db keeps it: its hash only (the token is in the URL and the run's journal)."""
    import hashlib

    return hashlib.sha256(str(token).encode()).hexdigest()

#: control_run's arguments besides the action, and their types (the tool's and the panel's parameters).
#: The journal rows a run's answer carries (not the bookkeeping rows: request counts, leases).
JOURNAL_KINDS = ("activity", "trace", "event", "edit", "timer", "lifecycle")

_CONTROL_ARGS: dict[str, type] = {"state": str, "machine": str, "at_step": int, "definition": str,
                                  "breakpoints": list, "watchpoints": list, "expr": str, "path": str,
                                  "pause": bool, "mocks": dict}


def _stopped(row: dict[str, Any]) -> bool:
    """A run no process runs whose resume takes it on where it stopped: interrupted, or failed resumably (FEAT-9,
    journal.resumable_failure: an internal error too)."""
    return row.get("status") == "interrupted" or (row.get("status") == "failed"
                                                   and resumable_failure(row.get("error")))


def owner_gone(owner: str, lease_until: Optional[str]) -> bool:
    """Whether a run's owner (``host:pid:...``, runner.owner_id) is a process of this host that is gone: no
    process has its pid, or the one that has it started after the owner last renewed its lease (the pid was
    reused). Another host's owner -- or one in another pid namespace of this host (a container) -- is never
    judged: its lease decides (RES-8)."""
    import psutil

    from .engine.runner import host_identity

    host, _, rest = owner.partition(":")
    pid_text = rest.partition(":")[0]
    if host != host_identity() or not pid_text.isdigit():
        return False
    pid = int(pid_text)
    if pid == os.getpid():
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        renewed = _timestamp(lease_until)
        return renewed is not None and process.create_time() > renewed - LEASE_SECONDS + 1.0
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:  # not ours to look at: alive, as far as we can tell
        return False


def _timestamp(text: Optional[str]) -> Optional[float]:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(text)).timestamp() if text else None
    except ValueError:
        return None


def _utc_in(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_control_args(kwargs: dict[str, Any]) -> None:
    """Every argument of its type before anything acts on it: a wrong one is the caller's mistake (422), not a
    crash inside the run or the fork."""
    for key, value in kwargs.items():
        kind = _CONTROL_ARGS.get(key)
        if kind is None:
            raise ServiceError(422, f"unknown argument {key!r} for control_run")
        if value is not None and not (_is_int(value) if kind is int else isinstance(value, kind)):
            raise ServiceError(422, f"{key} must be {'a whole number' if kind is int else 'a ' + kind.__name__}, "
                                    f"not {type(value).__name__}")
    if kwargs.get("at_step") is not None and kwargs["at_step"] < 0:
        raise ServiceError(422, "at_step must be 0 or more")
    if kwargs.get("definition") not in (None, "snapshot", "current"):
        raise ServiceError(422, f"definition must be snapshot or current, not {kwargs['definition']!r}")
    _check_points(kwargs.get("breakpoints"), kwargs.get("watchpoints"))


#: At most so many tags per run, and so long a tag name or text value.
MAX_TAGS, TAG_CHARS = 20, 200


def _check_tags(tags: Any) -> None:
    """Tags are a flat object (FEAT-7): names a word, values a text or a number -- what list_runs filters on."""
    if tags is None:
        return
    if not isinstance(tags, dict) or len(tags) > MAX_TAGS:
        raise ServiceError(422, f"tags must be an object of at most {MAX_TAGS} entries")
    for name, value in tags.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ServiceError(422, f"tag name {name!r} must be a word of letters, digits, _ . -")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > TAG_CHARS:
            raise ServiceError(422, f"tag {name}: a text or a number of at most {TAG_CHARS} characters, "
                                    f"not {value!r}"[:300])


def _check_steps(steps: Any) -> None:
    if not _is_int(steps) or not 1 <= steps <= 500:
        raise ServiceError(422, f"steps must be a whole number from 1 to 500, not {steps!r}")


def _check_points(breakpoints: Any, watchpoints: Any) -> None:
    try:
        parse_points(breakpoints, Breakpoint)
        parse_points(watchpoints, Watchpoint)
    except ValueError as exc:
        raise ServiceError(422, str(exc)) from None


def _required(kwargs: dict[str, Any], key: str) -> str:
    value = kwargs.get(key)
    if not value:
        raise ServiceError(422, f"{key} is required for this action")
    return str(value)


def _id_of(yaml_text: str) -> Optional[str]:
    from .model.loader import parse_yaml

    try:
        data = parse_yaml(yaml_text)
    except Exception:
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return str(value) if value else None


__all__ = ["ServiceError", "StateGraphService", "load_snapshot", "Path"]


def _origin(machine: Any) -> str:
    """The folder a machine that names no group shows in: the author's own for the first writable root, else the
    folder that holds its machines/ directory -- the plugin it comes with, writable in place or not."""
    if machine.own:
        return OWN_GROUP
    folder = Path(machine.path).parent
    return folder.parent.name if folder.name == "machines" else folder.name


#: What the machine list shows of a file, read without loading the machine.
_HEAD_KEYS = ("title", "description", "group")


#: A ``%YAML 1.1`` directive before the document: the loader then reads its scalars by 1.1's rules.
_YAML_1_1 = re.compile(r"(?:[ \t]*(?:#[^\n]*)?\r?\n)*%YAML[ \t]+1\.1\b")
_STR_TAG = "tag:yaml.org,2002:str"


def _head(path: Path) -> dict[str, str]:
    """``title``, ``description`` and ``group`` of a machine file, those that are text: parsed by libyaml where it is
    there, some fifty times faster than the loader's round trip, its plain scalars resolved by the loader's rules
    (ruamel, YAML 1.2: ``title: Yes`` is text, not PyYAML's 1.1 bool). Composed, not constructed: no other value
    is built by rules that are not the loader's. A file that does not read gives none -- its problems are the
    check's to tell."""
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
        version = (1, 1) if _YAML_1_1.match(text) else (1, 2)
        try:
            node = yaml.compose(text, Loader=_head_loader(version, fast=True))
        except yaml.YAMLError:  # libyaml refuses some inputs the pure parser reads (agent_system.utils.yaml_io)
            node = yaml.compose(text, Loader=_head_loader(version, fast=False))
    except Exception:  # unreadable, not YAML, nested too deeply
        return {}
    if not isinstance(node, yaml.MappingNode):
        return {}
    head: dict[str, str] = {}
    for key, value in node.value:
        if (isinstance(key, yaml.ScalarNode) and key.tag == _TEXT_TAG and key.value in _HEAD_KEYS
                and isinstance(value, yaml.ScalarNode) and value.tag == _TEXT_TAG):
            head[key.value] = value.value
    return head


#: The tag ``_head_loader`` gives a scalar that resolves to text without a tag of its own. One that names it
#: (``!!str 3``) keeps ``tag:yaml.org,2002:str``: ruamel's round trip reads it as a TaggedScalar, not as text.
_TEXT_TAG = "tag:stategraph,head:text"


@functools.cache
def _head_loader(version: tuple[int, int], *, fast: bool) -> type:
    """A PyYAML loader -- libyaml's with ``fast`` where it is there -- whose untagged scalars get the tag ruamel's
    resolver gives them under ``version``, text marked ``_TEXT_TAG``."""
    import yaml
    from ruamel.yaml.nodes import ScalarNode as RuamelScalarNode
    from ruamel.yaml.resolver import VersionedResolver

    rules = VersionedResolver(version=version)
    base = (getattr(yaml, "CSafeLoader", None) if fast else None) or yaml.SafeLoader

    class HeadLoader(base):  # type: ignore[valid-type, misc]
        def resolve(self, kind: Any, value: Any, implicit: Any) -> Any:
            if kind is not yaml.ScalarNode:
                return super().resolve(kind, value, implicit)
            tag = rules.resolve(RuamelScalarNode, value, implicit) if implicit[0] else _STR_TAG
            tag = str(getattr(tag, "value", tag))  # newer ruamel answers a Tag
            return _TEXT_TAG if tag == _STR_TAG else tag

    return HeadLoader


def _same_file(a: Any, b: Any) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
