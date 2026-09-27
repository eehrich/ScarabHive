"""The stategraph tool server: machine authoring, runs and the debugger as tools, plus the panel.

Tools that validate, save, run or control machines require an active admin (or
a user in ``allowed_users``): machines contain Python and run agents and tools
(docs/stategraph_design.md §8.3). The panel's routes are admin-only through
``auth.plugin_security``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

from agent_system.tools.schema_based import SchemaBasedToolServer

from . import kinds as _kinds  # noqa: F401  -- registers the built-in activity kinds
from .engine.journal import RunStore
from .engine.runner import RunManager
from .service import ServiceError, StateGraphService
from .store import MachineStore

logger = logging.getLogger(__name__)

DEFAULT_MACHINE_DIRS = ("data/stategraph/machines", "src/plugins*/*/machines")
DEFAULT_WRITABLE = ("data/stategraph/machines",)
SWEEP_SECONDS = 60
MAX_WAIT = 3600.0  # seconds a tool call may wait for its run
READ_ONLY_TOOLS = frozenset({"catalog", "list_machines", "get_machine", "get_run"})


class StateGraphServer(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: Any, server_config: Any):
        super().__init__(name, system_config, server_config)
        self.system_config = system_config
        # flat keys, defaults in code: the framework does not validate plugin config
        self.machine_dirs = list(getattr(server_config, "machine_dirs", None) or DEFAULT_MACHINE_DIRS)
        self.writable_dirs = list(getattr(server_config, "writable_machine_dirs", None) or DEFAULT_WRITABLE)
        self.runs_db = str(getattr(server_config, "runs_db", None) or "data/stategraph/runs.db")
        self.runner_agent = str(getattr(server_config, "runner_agent", None) or "stategraph_runner")
        self.allowed_users = [str(u) for u in (getattr(server_config, "allowed_users", None) or [])]
        raw_inject = getattr(server_config, "inject_params", None) or {}
        self.inject_params = {str(k): dict(v) for k, v in raw_inject.items() if isinstance(v, dict)}
        self.default_max_wait = float(getattr(server_config, "default_max_wait", None) or 600)
        self.machines = MachineStore(self.machine_dirs, self.writable_dirs)
        self.run_store = RunStore(self.runs_db)
        self.run_manager = RunManager(self.run_store, on_cancel=self._cancel_requests,
                                      on_finish=self._release_token)
        self.service = StateGraphService(self)
        self._registry: Any = None
        self._sweeper: Optional[asyncio.Task] = None
        self._web: Any = None

    # ------------------------------------------------------------ plumbing
    def resolve_runner(self) -> Any:
        """The runner agent: the host of tool activities (its allowlist is their boundary) and the way to the
        registry agent activities run in."""
        for registry in self._registries():
            try:
                return registry.get(self.runner_agent)
            except Exception:
                continue
        logger.warning("stategraph: runner agent %r not found; runs have no backend", self.runner_agent)
        return None

    def _registries(self) -> list[Any]:
        found = []
        if self._registry is not None:
            found.append(self._registry)
        try:
            from agent_system.runtime import Runtime

            runtime = Runtime.last_started
            if runtime is not None and getattr(runtime, "registry", None) is not None:
                found.append(runtime.registry)
        except Exception:
            pass
        try:
            import agent_system.app as app

            if getattr(app, "_app_registry", None) is not None:
                found.append(app._app_registry)
        except Exception:
            pass
        return found

    def _agent_names(self) -> list[str]:
        """The agents a machine may run: every agent there is that SG007 accepts."""
        check = self.service.config_check()
        configured = getattr(getattr(self.system_config, "plugins", None), "servers", None) or {}
        for registry in self._registries():
            try:
                names = set(registry.list()) | set(configured)
            except Exception:
                continue
            return sorted(name for name in names
                          if self.is_agent(name) and (check is None or check("agent", name, {}) is None))
        return []

    def is_agent(self, name: str) -> Optional[bool]:
        """Whether the running registry holds ``name`` as an agent; None when no registry knows."""
        from agent_system.runtime import ServerView
        from agent_system.servers.agent.server import Agent

        for registry in self._registries():
            describe = getattr(registry, "describe", None)
            view = describe(name) if callable(describe) else None  # from the instance or a lazy declaration
            if isinstance(view, ServerView):  # declared but not built (its start failed): nothing to run yet
                return view.is_agent if view.built else None
            try:  # an unbound registry: the instance answers (ToolServerRegistry.describe's contract)
                if name in registry.list():
                    return isinstance(registry.get(name), Agent)
            except Exception:
                continue
        return None

    def cancel_token(self, run_id: str) -> Any:
        try:
            from agent_system.core.cancellation import get_cancellation_manager

            return get_cancellation_manager().create_token(run_id)
        except Exception:
            return None

    def _cancel_requests(self, run_id: str) -> None:
        from agent_system.core.cancellation import get_cancellation_manager

        # every <run>_NNN sub-run in flight -- not the run's own token: its finally activities start
        # sub-runs after this, and a cancelled run token would have them force-cancelled (§3.10); the
        # finally and close activities already running are protected (backend._cleanup_request)
        get_cancellation_manager().cancel_sub_requests(run_id)

    def _release_token(self, run_id: str) -> None:
        from agent_system.core.cancellation import get_cancellation_manager

        get_cancellation_manager().unregister_request(run_id)

    def _note_agent(self, params: dict[str, Any]) -> None:
        agent = params.get("_agent")
        if agent is not None and getattr(agent, "registry", None) is not None:
            self._registry = agent.registry

    def use_registry(self, registry: Any) -> None:
        """The registry runs resolve their runner agent in (the agent facade hands over its own)."""
        if registry is not None:
            self._registry = registry

    def _auth_enabled(self) -> bool:
        auth = getattr(self.system_config, "auth", None)
        return auth is not None and bool(getattr(auth, "enabled", False))

    @staticmethod
    def _is_admin(user_id: Any) -> bool:
        if not user_id:
            return False
        try:
            from agent_system.auth.database import get_user_by_username
            from agent_system.auth.models import UserRole

            user = get_user_by_username(str(user_id))
            return user is not None and user.is_active and user.role == UserRole.ADMIN
        except Exception:
            logger.debug("stategraph: role lookup for %r failed", user_id, exc_info=True)
            return False

    def sees_run(self, user_id: Optional[str], owner: Optional[str]) -> bool:
        """Whether ``user_id`` may see and control a run of ``owner`` (§8.3): an admin every run, anyone else their
        own and runs of nobody; without auth the app has one user."""
        return not self._auth_enabled() or owner in (None, user_id) or self._is_admin(user_id)

    def _authorize(self, params: dict[str, Any], tool: str) -> Optional[str]:
        """None when the caller may use ``tool``; else the refusal (§8.3)."""
        if tool in READ_ONLY_TOOLS or not self._auth_enabled():
            return None
        user_id = params.get("_user_id")
        if user_id and any(fnmatch.fnmatchcase(str(user_id), pattern) for pattern in self.allowed_users):
            return None
        if self._is_admin(user_id):
            return None
        return (f"stategraph: only admins may use {tool} (user {user_id or 'unknown'}); machines contain Python "
                "and run agents and tools. An operator can list the user in the instance's allowed_users.")

    async def _run_tool(self, params: dict[str, Any], tool: str, body: Any, describe: Any) -> dict[str, Any]:
        status = params.get("_status")
        self._note_agent(params)
        refusal = self._authorize(params, tool)
        if refusal:
            if status:
                await status.error(refusal[:140])
            return {"status": "error", "error": refusal}
        try:
            result = await body()
        except ServiceError as exc:
            if status:
                await status.error(f"{tool}: {exc.message}"[:140])
            return {"status": "error", "error": exc.message, "error_type": f"http_{exc.status}"}
        if status:
            await status.end(describe(result)[:140])
        return {"status": "success", **result} if isinstance(result, dict) else {"status": "success", "result": result}

    # ------------------------------------------------------------ tools: "{name}_x" -> x(params)
    async def catalog(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            return self._catalog(str(params.get("agents") or "*"))
        return await self._run_tool(params, "catalog", body,
                                    lambda r: f"catalog: {len(r['kinds'])} kinds, {len(r['agents'])} agents, "
                                              f"{len(r['tools'])} tools")

    def _catalog(self, agent_pattern: str = "*") -> dict[str, Any]:
        from agent_system.config.settings import get_tool_server_config

        kinds = []
        for kind in self.service.kinds():
            schema = kind.get("schema") or {}
            fields = {name: (prop.get("description") or prop.get("type") or "")
                      for name, prop in (schema.get("properties") or {}).items()}
            kinds.append({"key": kind["key"], "summary": kind["summary"], "fields": fields})
        agents = [{"name": name, "description": getattr(get_tool_server_config(name, self.system_config),
                                                        "description", "") or ""}
                  for name in self._agent_names() if fnmatch.fnmatchcase(name, agent_pattern)]
        runner = get_tool_server_config(self.runner_agent, self.system_config)
        tools_cfg = getattr(getattr(runner, "agent_config", None), "tools", None)
        patterns = list(getattr(tools_cfg, "allowed", None) or [])
        llm = getattr(self.system_config, "llm_system", None)
        return {"kinds": kinds, "agents": agents, "runner": self.runner_agent,
                "tools": patterns, "decision_profiles": sorted((getattr(llm, "decision_profiles", None) or {})),
                "examples": [m.id for m in self.machines.list() if not m.writable]}

    async def list_machines(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            return {"machines": self.service.list_machines()}
        return await self._run_tool(params, "list_machines", body,
                                    lambda r: f"{len(r['machines'])} machine(s), "
                                              f"{sum(1 for m in r['machines'] if not m.get('valid'))} with errors")

    async def get_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            result = self.service.get_machine(_need(params, "machine_id"))
            result.pop("graph", None)
            result.pop("layout", None)
            return result
        return await self._run_tool(params, "get_machine", body,
                                    lambda r: f"{r['id']}: {len(r['files'])} file(s), {_counts(r['problems'])}")

    async def validate_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            result = self.service.validate(files=_object(params, "files"), yaml=params.get("yaml"),
                                           machine_id=params.get("machine_id"))
            result.pop("graph", None)
            result["valid"] = not any(p["level"] == "error" for p in result["problems"])
            return result
        return await self._run_tool(params, "validate_machine", body,
                                    lambda r: f"{r['machine_id']}: {_counts(r['problems'])}")

    async def save_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            files = _object(params, "files")
            if not files:
                raise ServiceError(422, "files is required: {relative path: text}, root machine <id>.yaml")
            result = self.service.save_machine(params.get("machine_id"), files, _object(params, "expected_versions"))
            result.pop("graph", None)
            return result
        return await self._run_tool(params, "save_machine", body,
                                    lambda r: f"saved {r['machine_id']}: {len(r['versions'])} file(s), "
                                              f"{_counts(r['problems'])}")

    async def run_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            machine_id = _need(params, "machine_id")
            wait = _choice(params, "wait", ("finish", "background"))
            max_wait = _number(params, "max_wait", self.default_max_wait, 0, MAX_WAIT)
            started = await self.service.start_run(
                machine_id, params=_object(params, "params") or {}, mocks=_object(params, "mocks"),
                mock_only=_flag(params, "mock_only"), breakpoints=params.get("breakpoints") or (),
                watchpoints=params.get("watchpoints") or (), user_id=params.get("_user_id"),
                run_key=params.get("run_key"), caller_session=params.get("_session_id"))
            run_id = started["run_id"]
            if wait == "background" or started.get("attached") is False:
                # background -- or a run another process owns: nothing here to wait on, report its row
                row = self.run_store.get_run(run_id) or {}
                return {**started, **_summary(row)} if row else {**started, "state": None, "run_status": "running"}
            row = await self._wait(run_id, max_wait, params.get("_cancellation_token"))
            return {**started, **_summary(row)}
        return await self._run_tool(params, "run_machine", body,
                                    lambda r: f"run {r['run_id']}: {r.get('run_status')} in {r.get('state') or '-'}")

    async def _wait(self, run_id: str, max_wait: float, token: Any) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while True:
            row = await self.run_manager.wait(run_id, timeout=1.0)
            if row["status"] not in ("running",) or loop.time() >= deadline:
                return row
            live = self.run_manager.live.get(run_id)
            if token is not None and getattr(token, "is_cancelled", False) and live is not None and not live.ctx.lost:
                self.run_manager.control(run_id, "terminate")  # the caller stopped: so does its run
                return await self.run_manager.wait(run_id, timeout=5.0)

    async def get_run(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            row = self.service.get_run(_need(params, "run_id"), steps=params.get("steps") or 30,
                                       user_id=params.get("_user_id"))
            return {**_summary(row), "run_id": row["id"], "frames": (row.get("view") or {}).get("frames", []),
                    "journal": row.get("journal", []), "debug": row.get("debug")}
        return await self._run_tool(params, "get_run", body,
                                    lambda r: f"run {r['run_id']}: {r['run_status']} in {r.get('state') or '-'}")

    async def control_run(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            run_id, action = _need(params, "run_id"), _need(params, "action")
            kwargs = {k: params[k] for k in ("state", "machine", "at_step", "definition", "breakpoints", "watchpoints",
                                              "expr", "path") if k in params}
            if params.get("steps") is not None:  # as get_run's: the answer carries that many journal rows
                kwargs["steps"] = params["steps"]
            result = await self.service.control_run(run_id, action, user_id=params.get("_user_id"), **kwargs)
            if "id" in result:  # a run row
                return {**_summary(result), "run_id": result["id"], "action": action,
                        **({"journal": result.get("journal", [])} if "steps" in kwargs else {})}
            return {"action": action, **result}
        return await self._run_tool(params, "control_run", body,
                                    lambda r: f"{r['action']}: " + (f"run {r['run_id']} {r.get('run_status', '')}"
                                                                    if "run_id" in r else f"value {str(r.get('value'))[:80]}"))

    async def send_event(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            result = self.service.send_event(_need(params, "run_id"), _need(params, "name"), params.get("data"),
                                             params.get("frame"), user_id=params.get("_user_id"))
            if not result.get("accepted"):
                raise ServiceError(409, result.get("reason") or "not accepted")
            return result
        return await self._run_tool(params, "send_event", body,
                                    lambda r: "event queued: no frame accepts it yet" if r.get("queued")
                                    else f"event accepted by frame {r.get('frame') or 'root'}")

    # ------------------------------------------------------------ web
    def get_web_router(self) -> Any:
        from .web_endpoints import StateGraphWebEndpoints

        if self._web is None:
            self._web = StateGraphWebEndpoints(self)
        return self._web.get_web_router()

    def get_static_assets(self) -> Path:
        """Panel script, stylesheet and the vendored layout engine, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"

    # ------------------------------------------------------------ lifecycle (plugins/capabilities.py)
    async def start_plugin(self) -> None:
        """Mark runs whose owner's lease expired as interrupted -- in every process, never a live one."""
        self.run_manager._stopping = False  # started again after stop_plugin: runs start again
        try:
            swept = self.run_manager.sweep_expired()
            if swept:
                logger.info("stategraph: %d run(s) interrupted by an expired lease: %s", len(swept), swept)
        except Exception:
            logger.warning("stategraph: lease sweep failed", exc_info=True)
        self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        await self._report_facades()  # after the start, off its path: a machine agent's mistake shows in the log
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                self.run_manager.sweep_expired()
            except Exception:
                logger.debug("stategraph: lease sweep failed", exc_info=True)

    def facade_problems(self) -> dict[str, list[str]]:
        """What keeps each machine agent over this instance (``type: stategraph_machine``) from running: its machine
        is missing or has errors, or the params it passes leave a required one out -- else only the first request's
        answer would say so."""
        from agent_system.config.settings import _resolve_server_inheritance, get_tool_server_config

        servers = getattr(getattr(self.system_config, "plugins", None), "servers", None) or {}
        found: dict[str, list[str]] = {}
        for name, raw in servers.items():
            if not getattr(raw, "enabled", False):
                continue
            try:
                kind = _resolve_server_inheritance(name, self.system_config)[0]
            except Exception:
                kind = str(getattr(raw, "type", "") or "")
            if kind != "stategraph_machine":
                continue
            config = get_tool_server_config(name, self.system_config)
            if str(getattr(config, "stategraph", None) or "stategraph") == self.name:
                problems = self._facade_problems(config)
                if problems:
                    found[name] = problems
        return found

    def _facade_problems(self, config: Any) -> list[str]:
        machine_id = str(getattr(config, "machine", None) or "")
        if not machine_id:
            return ["no machine configured (machine: <machine id>)"]
        if self.machines.find(machine_id) is None:
            return [f"no machine {machine_id!r} in {', '.join(self.machine_dirs)}"]
        tree = self.service._validate(self.machines.load(machine_id))
        errors = [p for p in tree.problems if p.level == "error"]
        if errors:
            return [f"machine {machine_id} has {len(errors)} error(s), the first: {errors[0].code} {errors[0].message}"]
        declared = tree.files[tree.root].spec.params
        given = set(getattr(config, "params", None) or {})
        problems = []
        if str(getattr(config, "input", None) or "text") == "text":  # json: the message brings the params
            task = str(getattr(config, "task_param", None) or "task")
            if task not in declared:
                problems.append(f"task_param {task!r} is no param of {machine_id} (it has: "
                                f"{', '.join(declared) or 'none'})")
            given.add(task)
            missing = sorted(name for name, spec in declared.items() if spec.required and name not in given)
            if missing:
                problems.append(f"{machine_id} requires {', '.join(missing)}: neither in params nor the task_param")
        return problems

    async def _report_facades(self) -> None:
        """In a worker thread: validating the machines (SG007 walks every configured server) takes a few hundred
        milliseconds the loop would otherwise stand still."""
        try:
            for name, problems in (await asyncio.to_thread(self.facade_problems)).items():
                logger.error("stategraph: machine agent %s cannot run: %s", name, "; ".join(problems))
        except Exception:
            logger.warning("stategraph: checking the machine agents failed", exc_info=True)

    async def stop_plugin(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        await self.run_manager.shutdown()
        self.run_store.close()


def _need(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not value:
        raise ServiceError(422, f"{key} is required")
    return str(value)


def _object(params: dict[str, Any], key: str) -> Optional[dict[str, Any]]:
    """An object argument; a model often sends it as a JSON string, which is taken when it holds an object."""
    value = params.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ServiceError(422, f"{key} must be an object, not a string (JSON that does not parse)") from None
    if not isinstance(value, dict):
        raise ServiceError(422, f"{key} must be an object, not {type(value).__name__}")
    return value


def _flag(params: dict[str, Any], key: str) -> bool:
    value = params.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ServiceError(422, f"{key} must be true or false, not {value!r}")
    return value


def _number(params: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = params.get(key)
    if value is None or value == "":
        return float(default)
    try:
        number = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError):
        number = math.nan
    if not low <= number <= high:  # NaN fails it too
        raise ServiceError(422, f"{key} must be a number from {low:g} to {high:g}, not {value!r}")
    return number


def _choice(params: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    """One of ``choices``; the first when the argument is missing."""
    value = params.get(key) or choices[0]
    if value not in choices:
        raise ServiceError(422, f"{key} must be one of {', '.join(choices)}, not {value!r}")
    return value


def _counts(problems: list[dict[str, Any]]) -> str:
    errors = sum(1 for p in problems if p["level"] == "error")
    return f"{errors} error(s), {len(problems) - errors} warning(s)"


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    view = row.get("view") or {}
    frames = view.get("frames") or []
    root = frames[0] if frames else {}
    debug = row.get("debug") or {}
    return {"run_status": row.get("status"), "state": row.get("final_state") or root.get("state"),
            "output": row.get("output"), "error": row.get("error"), "paused": debug.get("paused"),
            "accepts": [{"frame": f.get("prefix", ""), "events": f.get("accepts")} for f in frames if f.get("accepts")],
            **({"mocks_unused": view["mocks_unused"]} if view.get("mocks_unused") else {})}


PLUGIN_FACTORY = StateGraphServer
