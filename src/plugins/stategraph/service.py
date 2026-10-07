"""The operations behind stategraph's tools and panel: one implementation, two front ends.

Every method returns JSON-able data or raises ``ServiceError(status, message)``;
the tools turn that into ``{"status": "error", "error": ...}``, the web
endpoints into an HTTP error. Nothing here knows about FastAPI or tool params.
"""

from __future__ import annotations

import asyncio
import json
import time
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .engine.backend import NoBackend, ScarabHiveBackend, make_config_check
from .engine.debugger import Breakpoint, UnknownState, Watchpoint, parse_points
from .engine.journal import ACTIVE_STATUSES, TERMINAL_STATUSES, RunStore, utc_now
from .engine.machine import CompileError
from .engine.runner import REMOTE_CONTROLS, RunManager, failed_transiently
from .kinds import describe_kinds
from .model.loader import MachineTree, load_snapshot
from .model.spec import agent_entry
from .model.validate import validate_tree
from .runners import merged_inject_params, runner_inject_params, runner_of
from .store import FileInTheWay, MachineStore, VersionConflict, version_of

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

    # ------------------------------------------------------------ helpers
    def config_check(self, runner: Optional[str] = None) -> Optional[Callable[..., Optional[str]]]:
        """SG007 against the configuration. The validator's tool questions name the run's root file and get its
        runner (runners.py); ``runner`` answers the others -- a run's backend passes its own."""
        config = getattr(self.server, "system_config", None)
        if config is None or getattr(config, "plugins", None) is None:
            return None
        return make_config_check(config, runner=runner or self.server.runner_agent, own_instance=self.server.name,
                                 default_runner=self.server.runner_agent,
                                 is_agent=getattr(self.server, "is_agent", None))

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
    def list_machines(self) -> list[dict[str, Any]]:
        out = []
        for machine in self.store.list():
            entry: dict[str, Any] = {"id": machine.id, "file": str(machine.path), "root": machine.root,
                                     "writable": machine.writable, "title": "", "description": "",
                                     "group": _origin(machine)}
            try:
                tree = self._validate(self.store.load(machine.id))
                spec = tree.root_file.spec
                if spec is not None:
                    entry["title"], entry["description"] = spec.title, spec.description
                    entry["group"] = spec.group or entry["group"]
                errors = [p for p in tree.problems if p.level == "error"]
                entry.update(valid=not errors, errors=len(errors),
                             warnings=len(tree.problems) - len(errors))
            except Exception as exc:  # one broken file must not hide the others
                entry.update(valid=False, errors=1, warnings=0, error=f"{type(exc).__name__}: {exc}")
            out.append(entry)
        return out

    def get_machine(self, machine_id: str) -> dict[str, Any]:
        self._require(machine_id)
        found = self.store.find(machine_id)
        assert found is not None
        tree = self._validate(self.store.load(machine_id))
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
        return self.get_machine(machine_id)

    def delete_machine(self, machine_id: str, expected_version: str) -> dict[str, Any]:
        """Delete a machine of a writable root: its file (the version the caller saw), its layout and its companion
        module -- unless another machine uses that module, or it lies outside the writable roots. Refused while
        another machine imports it: that one would stop loading. Its runs keep their snapshot and stay readable."""
        self._require(machine_id)
        found = self.store.find(machine_id)
        assert found is not None
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
        return {"machine_id": machine_id, "versions": versions, "problems": self._problems(tree),
                "graph": self._graph(tree)}

    async def edit_machine(self, machine_id: str, op: dict[str, Any], expected_version: Optional[str],
                           drafts: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """One graph edit. On the file (its version the caller read), written at once; or, given ``drafts`` (the
        caller's unsaved files, by relative path), on the root file's draft, else the file: nothing is written, the
        answer is ``graph`` and ``problems`` of the drafts and ``draft``, the new root text."""
        from .model.yamledit import EditError, apply_op

        self._require(machine_id)
        found, text = self.store.read(machine_id)
        root = f"{machine_id}.yaml"
        if drafts is not None:
            try:
                draft = await asyncio.to_thread(apply_op, str(drafts.get(root, text)), op)
            except EditError as exc:
                raise ServiceError(422, str(exc)) from None
            tree = self._validate(self._tree_from({**drafts, root: draft}, None, machine_id)[2])
            return {"machine_id": machine_id, "problems": self._problems(tree), "graph": self._graph(tree),
                    "draft": draft}
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
        return self.get_machine(machine_id)

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
            inject = merged_inject_params(self.server.inject_params,
                                          runner_inject_params(self.server.system_config, name))
            return ScarabHiveBackend(runner=runner, system_config=self.server.system_config,
                                     session_id=f"sg_{run_id}", user_id=row.get("user_id"),
                                     token=self.server.cancel_token(run_id), nesting=row.get("nesting"),
                                     inject_params=inject, config_check=self.config_check(name))
        return make

    async def start_run(self, machine_id: str, params: Optional[dict[str, Any]] = None,
                        mocks: Optional[dict[str, Any]] = None, mock_only: bool = False, breakpoints: Any = (),
                        watchpoints: Any = (), pause_at_start: bool = False, user_id: Optional[str] = None,
                        run_key: Optional[str] = None, run_id: Optional[str] = None,
                        caller_session: Optional[str] = None) -> dict[str, Any]:
        """Start a run of ``machine_id``. With ``run_key`` the same request gets the run of that key, if there is
        one: attached to while it runs, resumed when interrupted, and once it ended its outcome again (``ended``:
        its status) -- only after a transient failure (runner.TRANSIENT_ERRORS) does a new run try it anew.

        ``run_id`` (the agent facade: ``<request id>_sg<n>``) keeps cancel, status and cost attribution under
        the caller's request id; it must be a session-id-safe word of at most 128 characters.
        ``caller_session``: the session of the agent that asks; the run's agent instances sit one level below it
        in its sub-agent tree.
        """
        self._require(machine_id)
        for name, value in (("params", params), ("mocks", mocks)):
            if value is not None and not isinstance(value, dict):
                raise ServiceError(422, f"{name} must be an object, not {type(value).__name__}")
        if not isinstance(mock_only, bool):
            raise ServiceError(422, f"mock_only must be true or false, not {mock_only!r}")
        if run_id is not None and not _RUN_ID.fullmatch(run_id):
            raise ServiceError(422, f"run_id {run_id!r} must match {_RUN_ID.pattern}")
        nesting = await self._nesting(user_id, caller_session)  # before the key's lookup: nothing awaits past it
        if run_key:
            existing = self.run_store.latest_by_key(run_key)
            if existing is not None and existing.get("user_id") not in (None, user_id):
                raise ServiceError(409, f"run_key {run_key!r} belongs to another user's run")
            if existing is not None:
                other = _other_request(existing, machine_id, params, mocks, mock_only)
                if other:  # a mock test's outcome must never answer a live request that reuses its key
                    raise ServiceError(409, f"run_key {run_key!r} names run {existing['id']} with other {other}; "
                                            "the same key is the same request -- use a new key for a new one")
                if existing["id"] in self.runs.live:
                    return {"run_id": existing["id"], "attached": True}
                if existing["status"] in ACTIVE_STATUSES:
                    self.runs.sweep_expired()  # a dead owner's lease has run out: the run is interrupted now
                    existing = self.run_store.get_run(existing["id"]) or existing
                if existing["status"] in ACTIVE_STATUSES:
                    return {"run_id": existing["id"], "attached": False, "owner": existing.get("owner"),
                            "note": "another process runs it; this call started nothing"}
                if existing["status"] == "interrupted":
                    await self._resume(existing["id"])
                    return {"run_id": existing["id"], "resumed": True}
                if not (existing["status"] == "failed" and failed_transiently(existing.get("error"))):
                    return {"run_id": existing["id"], "ended": existing["status"]}
        checked = self._validate(self.store.load(machine_id))
        errors = [p for p in checked.problems if p.level == "error"]
        if errors:
            raise ServiceError(422, f"{len(errors)} error(s): " + "; ".join(
                f"{p.code} {p.path} {p.message}" for p in errors[:3]))
        tree = load_snapshot(checked.snapshot())  # exactly what was validated: a save meanwhile does not slip in
        try:
            run_id = await self.runs.start(
                tree, params=params, mocks=mocks, mock_only=mock_only, breakpoints=breakpoints,
                watchpoints=watchpoints, pause_at_start=pause_at_start,
                backend_factory=None if mock_only else self.backend_factory(), user_id=user_id,
                run_key=run_key, run_id=run_id, nesting=nesting, runner=self._runner_of(checked))
        except (ValueError, CompileError) as exc:
            raise ServiceError(422, str(exc)) from None
        return {"run_id": run_id}

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

    async def _resume(self, run_id: str) -> None:
        row = self.run_store.get_run(run_id)
        options = (row or {}).get("mocks") or {}
        try:
            await self.runs.resume(run_id, backend_factory=None if options.get("mock_only")
                                   else self.backend_factory())
        except (ValueError, CompileError) as exc:
            raise ServiceError(409, str(exc)) from None

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

    async def _terminate_elsewhere(self, run_id: str) -> None:
        """A run no process here runs: resume it into its termination, so its finally activities run (§3.10).

        A run that ended is left alone; one another process holds is refused (409). If it cannot be resumed
        at all (its definition no longer loads), it is marked cancelled without them, and its error says so.
        """
        row = self.run_store.get_run(run_id) or {}
        if row.get("status") in ("succeeded", "failed", "cancelled"):
            return
        options = row.get("mocks") or {}
        try:
            await self.runs.resume(run_id, cancel=True, backend_factory=None if options.get("mock_only")
                                   else self.backend_factory())
        except CompileError as exc:
            self.runs.terminate_elsewhere(run_id, "terminated while interrupted; its finally activities did not "
                                                  f"run: its definition does not load ({exc})")

    def _run(self, run_id: str, user_id: Optional[str]) -> dict[str, Any]:
        """The run's row for a user who may see it (§8.3): an admin every run, anyone else their own -- another
        user's run answers like one that does not exist."""
        row = self.run_store.get_run(run_id)
        if row is None or not self.server.sees_run(user_id, row.get("user_id")):
            raise ServiceError(404, f"no run {run_id!r}")
        return row

    def list_runs(self, machine_id: Optional[str] = None, limit: int = 50, *, status: Optional[str] = None,
                  user_id: Optional[str] = None, all_users: bool = True,
                  before: Optional[str] = None, nested: bool = False) -> list[dict[str, Any]]:
        """A page of the newest runs; ``before``: the last run id of the page before; ``nested``: also the runs the
        machine ran in as a submachine."""
        if status is not None and status not in RUN_STATUSES:
            raise ServiceError(422, f"status must be one of {', '.join(RUN_STATUSES)}, not {status!r}")
        return self.run_store.list_runs(machine_id, limit=max(1, min(int(limit), 500)), status=status,
                                        user_id=user_id, all_users=all_users, before=before, nested=nested)

    def get_run(self, run_id: str, steps: int = 50, *, user_id: Optional[str] = None, after: Optional[int] = None,
                kinds: Optional[list[str]] = None, state: Optional[str] = None,
                frames: bool = False) -> dict[str, Any]:
        """A run and its journal rows: the last ``steps`` -- or, with ``after``, the first ``steps`` after that seq;
        only rows of ``kinds`` and of ``state`` when given. ``frames``: with the submachine frames it started
        (``frames_started``: prefix, machine, path)."""
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
        view = row.get("view") or {}
        row["accepts"] = [{"frame": f.get("prefix", ""), "state": f.get("state"), "events": f.get("accepts") or []}
                          for f in view.get("frames", []) if f.get("accepts")]
        wanted = kinds or JOURNAL_KINDS
        row["journal"] = (self.run_store.tail(run_id, limit=steps, kinds=wanted, state=state) if after is None
                          else self.run_store.page(run_id, after=after, limit=steps, kinds=wanted, state=state))
        if frames:
            row["frames_started"] = self.run_store.frames(run_id)
        return row

    def journal(self, run_id: str, after: int = 0, limit: int = 200, kinds: Optional[list[str]] = None) -> list[dict[str, Any]]:
        if self.run_store.get_run(run_id) is None:
            raise ServiceError(404, f"no run {run_id!r}")
        return self.run_store.page(run_id, after=after, limit=max(1, min(int(limit), 1000)), kinds=kinds)

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
                if run_id in self.runs.live:
                    self.runs.control(run_id, action)
                elif not await self._control_elsewhere(run_id, action):  # none holds it: resumed into its end here
                    await self._terminate_elsewhere(run_id)
                await self._await_end(run_id, TERMINATE_WAIT)  # its finally activities run first
            elif action in ("pause", "continue", "step", "run_to"):
                state = _required(kwargs, "state") if action == "run_to" else kwargs.get("state")
                if run_id in self.runs.live or not await self._control_elsewhere(run_id, action):
                    self.runs.control(run_id, action, state=state, machine=kwargs.get("machine"))
            elif action == "resume":
                await self._resume(run_id)
            elif action == "fork":
                return await self._fork(run_id, kwargs, user_id)
            elif action == "set_breakpoints":
                self.runs.set_points(run_id, breakpoints=kwargs.get("breakpoints") or [])
            elif action == "set_watchpoints":
                self.runs.set_points(run_id, watchpoints=kwargs.get("watchpoints") or [])
            elif action == "evaluate":
                return {"value": self.runs.evaluate(run_id, _required(kwargs, "expr"))}
            elif action == "set":
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
        return self.get_run(run_id, steps=steps, user_id=user_id)

    async def _fork(self, run_id: str, kwargs: dict[str, Any], user_id: Optional[str]) -> dict[str, Any]:
        """A fork is a new run, and it is the forking user's: they started it. It never continues an agent instance
        of its source (§5.6: its own session refuses them), so no conversation of the source's user runs on under
        another; its new instances are the fork's user's. Without a user (no auth) it keeps the source's."""
        row = self.run_store.get_run(run_id)
        assert row is not None
        tree = None
        if kwargs.get("definition") == "current":
            self._require(row["machine_id"])
            checked = self._validate(self.store.load(row["machine_id"]))
            if not checked.ok:
                raise ServiceError(422, "the current definition has errors; fix them before forking onto it")
            tree = load_snapshot(checked.snapshot())  # exactly what was validated
        options = row.get("mocks") or {}
        try:
            new_id = await self.runs.fork(run_id, at_step=kwargs.get("at_step"), tree=tree,
                                          backend_factory=None if options.get("mock_only")
                                          else self.backend_factory(), user_id=user_id or row.get("user_id"),
                                          pause_at_start=bool(kwargs.get("pause")), mocks=kwargs.get("mocks"),
                                          # the snapshot keeps the source's runner; the current file its folder's
                                          runner=self._runner_of(checked) if tree else None)
        except (ValueError, CompileError) as exc:
            raise ServiceError(422, str(exc)) from None
        return {"run_id": new_id, "forked_from": run_id}

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
                self.runs.sweep_expired()  # a dead owner's lease ran out: it is interrupted now
                run = self.run_store.get_run(run["id"]) or run
                if run.get("status") == "interrupted":
                    await self._resume(run["id"])
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



def _digest(token: str) -> str:
    """A callback token as runs.db keeps it: its hash only (the token is in the URL and the run's journal)."""
    import hashlib

    return hashlib.sha256(str(token).encode()).hexdigest()

#: control_run's arguments besides the action, and their types (the tool's and the panel's parameters).
#: The journal rows a run's answer carries (not the bookkeeping rows: request counts, leases).
JOURNAL_KINDS = ("activity", "trace", "event", "edit", "timer")

_CONTROL_ARGS: dict[str, type] = {"state": str, "machine": str, "at_step": int, "definition": str,
                                  "breakpoints": list, "watchpoints": list, "expr": str, "path": str,
                                  "pause": bool, "mocks": dict}


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


def _same_file(a: Any, b: Any) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
