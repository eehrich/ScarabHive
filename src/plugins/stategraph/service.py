"""The operations behind stategraph's tools and panel: one implementation, two front ends.

Every method returns JSON-able data or raises ``ServiceError(status, message)``;
the tools turn that into ``{"status": "error", "error": ...}``, the web
endpoints into an HTTP error. Nothing here knows about FastAPI or tool params.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .engine.backend import NoBackend, ScarabHiveBackend, make_config_check
from .engine.journal import ACTIVE_STATUSES, RunStore
from .engine.machine import CompileError
from .engine.runner import RunManager
from .kinds import describe_kinds
from .model.loader import MachineTree, load_snapshot
from .model.validate import validate_tree
from .store import MachineStore, VersionConflict, version_of

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


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class StateGraphService:
    def __init__(self, server: "StateGraphServer"):
        self.server = server
        self.store: MachineStore = server.machines
        self.runs: RunManager = server.run_manager
        self.run_store: RunStore = server.run_store

    # ------------------------------------------------------------ helpers
    def config_check(self) -> Optional[Callable[..., Optional[str]]]:
        config = getattr(self.server, "system_config", None)
        if config is None or getattr(config, "plugins", None) is None:
            return None
        return make_config_check(config, runner=self.server.runner_agent, default_sam=self.server.default_sam,
                                 own_instance=self.server.name)

    def _validate(self, tree: MachineTree) -> MachineTree:
        return validate_tree(tree, self.config_check())

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
            texts = [(path, loaded.text)]
            if loaded.python_path and loaded.python_text is not None:
                texts.append((loaded.python_path, loaded.python_text))
            for file_path, text in texts:
                rel = self.store.relative(machine_id, file_path)
                files[rel] = text
                versions[rel] = version_of(text)
        return files, versions

    def _require(self, machine_id: str) -> None:
        if self.store.find(machine_id) is None:
            raise ServiceError(404, f"no machine {machine_id!r} in the machine roots")

    # ------------------------------------------------------------ machines
    def list_machines(self) -> list[dict[str, Any]]:
        out = []
        for machine in self.store.list():
            entry: dict[str, Any] = {"id": machine.id, "file": str(machine.path), "root": machine.root,
                                     "writable": machine.writable, "title": "", "description": ""}
            try:
                tree = self._validate(self.store.load(machine.id))
                spec = tree.root_file.spec
                if spec is not None:
                    entry["title"], entry["description"] = spec.title, spec.description
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
        return {"id": machine_id, "file": str(found.path), "writable": found.writable,
                "root_file": f"{machine_id}.yaml", "files": files, "versions": versions,
                "problems": self._problems(tree), "graph": self._graph(tree), "layout": self.store.layout(machine_id)}

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
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        return self.get_machine(machine_id)

    def _tree_from(self, files: Optional[dict[str, str]], yaml: Optional[str],
                   machine_id: Optional[str]) -> tuple[str, dict[str, str], MachineTree]:
        if files is None and yaml is None:
            raise ServiceError(422, "pass files {relative path: text} or yaml")
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
        try:
            versions = self.store.write_files(machine_id, files, expected_versions)
        except VersionConflict as exc:
            raise ServiceError(409, str(exc)) from None
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from None
        return {"machine_id": machine_id, "versions": versions, "problems": self._problems(tree),
                "graph": self._graph(tree)}

    def edit_machine(self, machine_id: str, op: dict[str, Any], expected_version: Optional[str]) -> dict[str, Any]:
        from .model.yamledit import EditError, apply_op

        self._require(machine_id)
        found, text = self.store.read(machine_id)
        if expected_version != version_of(text):
            raise ServiceError(409, f"the file changed since you read it (current version {version_of(text)})")
        try:
            new_text = apply_op(text, op)
        except EditError as exc:
            raise ServiceError(422, str(exc)) from None
        root = f"{machine_id}.yaml"
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
    def backend_factory(self, user_id: Optional[str]) -> Callable[[str], Any]:
        def make(run_id: str) -> Any:
            runner = self.server.resolve_runner()
            if runner is None:
                return NoBackend()
            return ScarabHiveBackend(runner=runner, system_config=self.server.system_config,
                                     session_id=f"sg_{run_id}", user_id=user_id,
                                     token=self.server.cancel_token(run_id), default_sam=self.server.default_sam,
                                     inject_params=self.server.inject_params, tool_check=self.config_check())
        return make

    async def start_run(self, machine_id: str, params: Optional[dict[str, Any]] = None,
                        mocks: Optional[dict[str, Any]] = None, mock_only: bool = False, breakpoints: Any = (),
                        watchpoints: Any = (), pause_at_start: bool = False, user_id: Optional[str] = None,
                        run_key: Optional[str] = None) -> dict[str, Any]:
        self._require(machine_id)
        if run_key:
            existing = self.runs.find_by_key(run_key)
            if existing is not None:
                if existing["id"] in self.runs.live:
                    return {"run_id": existing["id"], "attached": True}
                if existing["status"] in ACTIVE_STATUSES:
                    self.runs.sweep_expired()  # a dead owner's lease has run out: the run is interrupted now
                    existing = self.run_store.get_run(existing["id"]) or existing
                if existing["status"] in ACTIVE_STATUSES:
                    return {"run_id": existing["id"], "attached": False, "owner": existing.get("owner"),
                            "note": "another process runs it; this call started nothing"}
                if existing["status"] == "interrupted":
                    await self._resume(existing["id"], user_id)
                    return {"run_id": existing["id"], "resumed": True}
        checked = self._validate(self.store.load(machine_id))
        errors = [p for p in checked.problems if p.level == "error"]
        if errors:
            raise ServiceError(422, f"{len(errors)} error(s): " + "; ".join(
                f"{p.code} {p.path} {p.message}" for p in errors[:3]))
        tree = self.store.load(machine_id, execute_python=True)
        try:
            run_id = await self.runs.start(
                tree, params=params, mocks=mocks, mock_only=mock_only, breakpoints=breakpoints,
                watchpoints=watchpoints, pause_at_start=pause_at_start,
                backend_factory=None if mock_only else self.backend_factory(user_id), user_id=user_id,
                run_key=run_key)
        except (ValueError, CompileError) as exc:
            raise ServiceError(422, str(exc)) from None
        return {"run_id": run_id}

    async def _resume(self, run_id: str, user_id: Optional[str]) -> None:
        row = self.run_store.get_run(run_id)
        options = (row or {}).get("mocks") or {}
        try:
            await self.runs.resume(run_id, backend_factory=None if options.get("mock_only")
                                   else self.backend_factory(user_id or (row or {}).get("user_id")))
        except (ValueError, CompileError) as exc:
            raise ServiceError(409, str(exc)) from None

    def list_runs(self, machine_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.run_store.list_runs(machine_id, limit=max(1, min(int(limit), 500)))

    def get_run(self, run_id: str, steps: int = 50) -> dict[str, Any]:
        try:
            row = self.runs.describe(run_id)
        except KeyError:
            raise ServiceError(404, f"no run {run_id!r}") from None
        view = row.get("view") or {}
        row["accepts"] = [{"frame": f.get("prefix", ""), "state": f.get("state"), "events": f.get("accepts") or []}
                          for f in view.get("frames", []) if f.get("accepts")]
        row["journal"] = self.run_store.tail(run_id, limit=max(1, min(int(steps), 500)),
                                             kinds=("activity", "trace", "event", "edit", "timer"))
        return row

    def journal(self, run_id: str, after: int = 0, limit: int = 200, kinds: Optional[list[str]] = None) -> list[dict[str, Any]]:
        if self.run_store.get_run(run_id) is None:
            raise ServiceError(404, f"no run {run_id!r}")
        return self.run_store.page(run_id, after=after, limit=max(1, min(int(limit), 1000)), kinds=kinds)

    async def control_run(self, run_id: str, action: str, **kwargs: Any) -> dict[str, Any]:
        if self.run_store.get_run(run_id) is None:
            raise ServiceError(404, f"no run {run_id!r}")
        try:
            if action in ("pause", "continue", "step", "run_to", "terminate"):
                self.runs.control(run_id, action, state=kwargs.get("state"), machine=kwargs.get("machine"))
            elif action == "resume":
                await self._resume(run_id, kwargs.get("user_id"))
            elif action == "fork":
                return await self._fork(run_id, kwargs)
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
        except ValueError as exc:
            raise ServiceError(409, str(exc)) from None
        except Exception as exc:
            from .model.code import CodeError

            if isinstance(exc, CodeError):
                raise ServiceError(422, exc.message) from None
            raise
        return self.get_run(run_id)

    async def _fork(self, run_id: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        row = self.run_store.get_run(run_id)
        assert row is not None
        tree = None
        if kwargs.get("definition") == "current":
            self._require(row["machine_id"])
            checked = self._validate(self.store.load(row["machine_id"]))
            if not checked.ok:
                raise ServiceError(422, "the current definition has errors; fix them before forking onto it")
            tree = self.store.load(row["machine_id"], execute_python=True)
        options = row.get("mocks") or {}
        user_id = kwargs.get("user_id") or row.get("user_id")
        try:
            new_id = await self.runs.fork(run_id, at_step=kwargs.get("at_step"), tree=tree,
                                          backend_factory=None if options.get("mock_only")
                                          else self.backend_factory(user_id), user_id=user_id)
        except (ValueError, CompileError) as exc:
            raise ServiceError(422, str(exc)) from None
        return {"run_id": new_id, "forked_from": run_id}

    def send_event(self, run_id: str, name: str, data: Any = None, frame: Optional[str] = None) -> dict[str, Any]:
        try:
            return self.runs.send_event(run_id, name, data, frame)
        except KeyError:
            raise ServiceError(404, f"no run {run_id!r}") from None
        except ValueError as exc:
            raise ServiceError(409, str(exc)) from None


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
