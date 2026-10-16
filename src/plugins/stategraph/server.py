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
import re
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent_system import app_state
from agent_system.paths import data_path
from agent_system.tools.schema_based import SchemaBasedToolServer

from . import kinds as _kinds  # noqa: F401  -- registers the built-in activity kinds
from .engine.journal import RunStore, resumable_failure
from .engine.runner import RunManager, row_event_due as _event_due
from .hooks import StateGraphHooks
from .schedules import Scheduler, _drives, parse_schedules, resume_lease
from .service import ServiceError, StateGraphService
from .model.spec import NAME_PATTERN
from .model.validate import agent_params_problems
from .runners import DEFAULT_RUNNER, runner_of
from .store import MachineStore, machine_dirs
from .wait_questions import WaitBroker

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 60
#: How often the retention (retention_days) looks for ended runs to compact.
COMPACT_SECONDS = 3600
MAX_WAIT = 3600.0  # seconds a tool call may wait for its run
# a tool answer bounds itself (plugin rules): texts in journal rows, frames and errors, and in a run's output
ROW_CHARS = 2000
OUTPUT_CHARS = 20000
ANSWER_CHARS = 200000  # a whole tool answer: many short texts add up too
FILE_CHARS = 30000  # get_machine: a file of the tree larger than this is left out unless asked for by name
READ_ONLY_TOOLS = frozenset({"catalog", "list_machines", "get_machine", "get_run", "list_runs"})
#: The parameters control_run takes (schema.yaml); the framework's own start with "_".
CONTROL_PARAMS = frozenset({"run_id", "action", "steps", "mocks", "state", "machine", "at_step", "definition",
                            "breakpoints", "watchpoints", "expr", "path", "pause"})
#: What the framework adds to a tool call without the "_" (an agent's programmatic dispatch; tools/base.py reads it).
FRAMEWORK_PARAMS = frozenset({"request_id", "requestId"})
#: A callback URL's token as the callback kind makes it (secrets.token_urlsafe) -- after ?token= or /callback/.
CALLBACK_TOKEN = re.compile(r"(?:[?&]token=|/callback/)([A-Za-z0-9_-]+)")
#: What a run's state asks of whoever reads it next (tool answers carry it as ``next``).
NEXT = {
    "running": "it goes on by itself: stategraph_get_run(run_id, wait='finish') waits for its end, a pause or a wait",
    "waiting": "it waits for an event: stategraph_send_event(run_id, name, data) with one of `accepts`",
    "paused": "the debugger holds it: stategraph_control_run(run_id, action='continue' or 'step')",
    "interrupted": "its process stopped: stategraph_control_run(run_id, action='resume') goes on from its journal",
}
#: A run that ended on a resumable failure (docs/stategraph_design.md §5.3): a resume runs that activity again.
NEXT_RESUMABLE = ("it stopped on a resumable failure (its error says why, e.g. a budget used up): once that has changed, "
                  "stategraph_control_run(run_id, action='resume') runs that activity again and replays the rest")
NEXT_INTERNAL = ("it stopped on an engine error (error.type internal): once that is fixed, "
                 "stategraph_control_run(run_id, action='resume') replays its journal and goes on where it stopped")
#: A wait for a semaphore slot (the acquire kind): it goes on by itself once another run gives one back.
NEXT_SLOT = ("it waits for a semaphore slot (view.slot_waits names it) and goes on by itself once a holder gives one "
             "back: stategraph_get_run(run_id, wait='finish') waits for its end, a pause or a wait")
#: A wait that takes no event -- a timer state (after): it goes on by itself.
NEXT_TIMER = ("it waits out a timer (after) and goes on by itself: stategraph_get_run(run_id, wait='finish') waits "
              "for its end, a pause or a wait")


class Coalesced:
    """A costly read run once for every request that comes while it runs: a request that comes while one runs waits
    for the next run, which starts when that one has ended and answers every request that came meanwhile. So at
    most one runs and one waits, however many panels ask -- and no answer was read before its request came."""

    def __init__(self, compute: Callable[[], Awaitable[Any]]):
        self.compute = compute
        self._running: Optional[asyncio.Task] = None
        self._next: Optional[asyncio.Task] = None

    async def __call__(self) -> Any:
        if self._next is not None:  # queued, not started: it reads after this request came
            task = self._next
        elif self._running is not None:  # started before this request: it may have read already
            task = self._next = asyncio.ensure_future(self._after(self._running))
        else:
            task = self._running = asyncio.ensure_future(self._run())
        # a request that goes away (the panel aborted it) does not cancel a run the others wait for
        return await asyncio.shield(task)

    async def _after(self, before: asyncio.Task) -> Any:
        await asyncio.wait([before])
        self._running, self._next = self._next, None
        return await self._run()

    async def _run(self) -> Any:
        try:
            return await self.compute()
        finally:
            if self._running is asyncio.current_task():
                self._running = None


class StateGraphServer(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: Any, server_config: Any):
        super().__init__(name, system_config, server_config)
        self.system_config = system_config
        # flat keys, defaults in code: the framework does not validate plugin config
        self.machine_dirs, self.writable_dirs = machine_dirs(server_config)
        self.runs_db = str(getattr(server_config, "runs_db", None) or data_path("stategraph", "runs.db"))
        self.runner_agent = str(getattr(server_config, "runner_agent", None) or DEFAULT_RUNNER)
        self.allowed_users = [str(u) for u in (getattr(server_config, "allowed_users", None) or [])]
        raw_inject = getattr(server_config, "inject_params", None) or {}
        self.inject_params = {str(k): dict(v) for k, v in raw_inject.items() if isinstance(v, dict)}
        self.default_max_wait = float(getattr(server_config, "default_max_wait", None) or 600)
        self.schedules, self.schedule_problems = parse_schedules(getattr(server_config, "schedules", None))
        # opt-in: the machines whose interrupted runs the scheduler's process resumes by itself (FEAT-3)
        raw_auto = getattr(server_config, "auto_resume", None) or []
        self.auto_resume = {str(m) for m in raw_auto} if isinstance(raw_auto, list) else set()
        if not isinstance(raw_auto, list):
            self.schedule_problems.append(f"auto_resume must be a list of machine ids, not {type(raw_auto).__name__}")
        # opt-in: drop the journal of runs that ended more than so many days ago (FEAT-11); none: keep everything
        retention = getattr(server_config, "retention_days", None)
        self.retention_days = float(retention) if isinstance(retention, (int, float)) and retention > 0 else None
        if retention is not None and self.retention_days is None:
            self.schedule_problems.append(f"retention_days must be a number of days above 0, not {retention!r}")
        self.machines = MachineStore(self.machine_dirs, self.writable_dirs)
        self.run_store = RunStore(self.runs_db)
        self.run_manager = RunManager(self.run_store, on_cancel=self._cancel_requests,
                                      on_finish=self._run_finished)
        base = str(getattr(server_config, "public_url", None) or "").rstrip("/")  # e.g. https://hive.example.com
        self.run_manager.callback_base = f"{base}/plugins/{name}/callback"
        self._set_semaphores(server_config)  # the acquire kind's semaphores (FEAT-2)
        self.service = StateGraphService(self)
        self.hooks_plugin = StateGraphHooks(name)  # the cost of the LLM calls inside call activities
        # a run's wait put to the person who watches the request that waits for it (wait_questions.py)
        self.wait_questions = WaitBroker(
            lambda run_id, name, data, frame, user_id: self.service.send_event(run_id, name, data, frame,
                                                                               user_id=user_id),
            self._live_row)
        self._registry: Any = None
        self._sweeper: Optional[asyncio.Task] = None
        self._scheduler: Optional[asyncio.Task] = None
        self._web: Any = None
        self._ending: set[asyncio.Task] = set()  # end_children of ended runs (FEAT-1), held until done
        self._compacted_at = 0.0
        # checked_machines: one validation of every machine at a time, for the tool and the panel's route (API-2)
        self._checks = Coalesced(lambda: asyncio.to_thread(self.service.machine_checks))

    # ------------------------------------------------------------ plumbing
    def resolve_runner(self, name: Optional[str] = None) -> Any:
        """A runner agent (default: this instance's): the host of tool activities (its allowlist is their boundary)
        and the way to the registry agent activities run in."""
        name = name or self.runner_agent
        for registry in self._registries():
            try:
                return registry.get(name)
            except Exception:
                continue
        logger.warning("stategraph: runner agent %r not found; runs have no backend", name)
        return None

    def runner_for(self, machine_id: Optional[str]) -> tuple[str, Optional[str]]:
        """The runner of a machine, by the folder its file lies in (runners.py) -- a machine not saved yet by the
        folder it will be saved in -- and why none can be told, when two runners claim that folder."""
        if machine_id and not re.fullmatch(NAME_PATTERN, machine_id):  # no id: no path of it to resolve ("../x")
            return self.runner_agent, None
        found = self.machines.find(machine_id) if machine_id else None
        path = found.path if found is not None else (
            Path(self.machines.writable[0]) / f"{machine_id or 'new'}.yaml" if self.machines.writable else None)
        return runner_of(self.system_config, str(path) if path else None, self.runner_agent)

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
        if app_state.app_registry is not None:
            found.append(app_state.app_registry)
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

    def _run_finished(self, run_id: str) -> None:
        """A run's task here ended: its cancel token goes; the runs it started end too (FEAT-1) -- all that have not
        ended when it was terminated, the interrupted ones when it succeeded or failed for good (nobody resumes those
        any more). A run that failed resumably (resumable_failure), or stopped interrupted, keeps them: its resume may
        go on in them."""
        self._release_token(run_id)
        try:
            row = self.run_store.get_run(run_id) or {}
            status = row.get("status")
            ended = status in ("succeeded", "cancelled") or (
                status == "failed" and not resumable_failure(row.get("error")))
            if ended and self.run_store.children(run_id, limit=1):
                task = asyncio.get_running_loop().create_task(
                    self.service.end_children(run_id, interrupted_only=status != "cancelled"))
                self._ending.add(task)
                task.add_done_callback(self._ending.discard)
        except Exception:
            logger.warning("stategraph: the children of run %s not looked at", run_id, exc_info=True)

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
        own; a run of nobody (the writer's, a schedule's without a user) an admin -- or a caller without a user, the
        process's own code (run_to_end, schedules). Without auth the app has one user (API-3)."""
        return not self._auth_enabled() or owner == user_id or self._is_admin(user_id)

    def _admin_reader(self, params: dict[str, Any]) -> bool:
        """Whether a read-only tool's caller may see server paths and tracebacks: whoever may save and run machines
        (an admin, an allowed user; without auth the one user) -- anyone else reads machines and their own runs
        without them (API-3)."""
        return self._authorize(params, "save_machine") is None

    def _holds_tokens(self, user_id: Optional[str], owner: Optional[str]) -> bool:
        """Whether a reader may see the run's callback URLs whole: its own user, or an admin (without auth, the one
        user). Anyone else who sees the run -- a run of nobody -- would fire its events past send_event's gate."""
        return not self._auth_enabled() or (owner is not None and owner == user_id) or self._is_admin(user_id)

    def _callback_tokens(self, run_id: str) -> set[str]:
        """The tokens of every callback URL the run made: in its callback activities' answers, and the ones its
        fork made anew (the ``callbacks`` lifecycle row, API-4)."""
        tokens: set[str] = set()
        urls = list(self.run_store.callback_urls(run_id))
        for row in self.run_store.rows(run_id, kinds=("lifecycle",)):
            urls += [str(entry.get("url")) for entry in ((row.get("data") or {}).get("callbacks") or [])
                     if isinstance(entry, dict)]
        for url in urls:
            tokens.update(CALLBACK_TOKEN.findall(url))
        return tokens

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
            return {"status": "error", "error": refusal, "error_type": "http_403"}
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
            machine = str(params.get("machine_id") or "") or None
            found = self._catalog(str(params.get("agents") or "*"), machine)
            found["tools"] = await self._runner_tools(str(params.get("tools") or "*"), machine)
            return found
        return await self._run_tool(params, "catalog", body,
                                    lambda r: f"catalog: {len(r['kinds'])} kinds, {len(r['agents'])} agents, "
                                              f"{len(r['tools'])} tools")

    def _catalog(self, agent_pattern: str = "*", machine_id: Optional[str] = None) -> dict[str, Any]:
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
        name = self.runner_for(machine_id)[0]
        runner = get_tool_server_config(name, self.system_config)
        tools_cfg = getattr(getattr(runner, "agent_config", None), "tools", None)
        patterns = list(getattr(tools_cfg, "allowed", None) or [])
        llm = getattr(self.system_config, "llm_system", None)
        return {"kinds": kinds, "agents": agents, "runner": name,
                "tools": patterns, "decision_profiles": sorted((getattr(llm, "decision_profiles", None) or {})),
                "examples": [m.id for m in self.machines.list() if not m.own]}

    async def _runner_tools(self, pattern: str, machine_id: Optional[str] = None) -> list[dict[str, Any]]:
        """The tools a machine's tool activity may call: every registered tool its runner's allowlist lets through
        (the matcher dispatch uses), flat name, what it does and its parameters -- not the allowlist's patterns.
        No machine: those of a new machine's runner."""
        from agent_system.config.settings import get_tool_server_config
        from agent_system.plugins.tool_adapter import plugin_tool_registry
        from agent_system.servers.agent.server import Agent
        from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

        runner = get_tool_server_config(self.runner_for(machine_id)[0], self.system_config)
        tools_cfg = getattr(getattr(runner, "agent_config", None), "tools", None)
        allowed = list(getattr(tools_cfg, "allowed", None) or [])
        blocked = list(getattr(tools_cfg, "blocked", None) or [])
        found: list[dict[str, Any]] = []
        for server_name in sorted(plugin_tool_registry.list_servers()):
            adapter = plugin_tool_registry.get_server(server_name)
            if adapter is None or isinstance(getattr(adapter, "plugin_server", None), Agent):
                continue  # an agent is run by an agent activity, not called as a tool
            try:
                tools = await adapter.list_tools()
            except Exception:  # one broken server must not cost the catalog
                logger.debug("stategraph: tools of %s not listed", server_name, exc_info=True)
                continue
            for tool in tools:
                if not (tool_matches_patterns(tool.name, server_name, allowed)
                        and not tool_matches_patterns(tool.name, server_name, blocked)):
                    continue
                if not fnmatch.fnmatchcase(tool.name, pattern):
                    continue
                schema = tool.input_schema or {}
                found.append({"name": tool.name, "description": (tool.description or "")[:300],
                              "parameters": {name: {key: (str(value)[:200] if key == "description" else value)
                                                    for key, value in (spec or {}).items()
                                                    if key in ("type", "enum", "description", "default")}
                                             for name, spec in (schema.get("properties") or {}).items()},
                              "required": list(schema.get("required") or [])})
        return found

    async def list_runs(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            status = params.get("status") or None  # the service checks it
            user = params.get("_user_id")
            mine = self._auth_enabled() and not self._is_admin(user)
            rows = self.service.list_runs(params.get("machine_id") or None, int(_number(params, "limit", 20, 1, 200)),
                                          status=status, user_id=user, all_users=not mine,
                                          tags=_object(params, "tags"))
            runs: list[dict[str, Any]] = []
            room = ANSWER_CHARS - 500  # tags are up to some 5,000 characters a run: the newest runs while they fit
            for row in rows:
                run = ({key: row.get(key) for key in ("id", "machine_id", "status", "final_state", "created_at",
                                                      "finished_at", "run_key", "parent_run")}
                       | ({"tags": row["tags"]} if row.get("tags") else {})
                       | ({"error": _capped((row.get("error") or {}).get("message"), 300)} if row.get("error") else {}))
                room -= _size(run) + 2
                if room < 0:
                    break
                runs.append(run)
            if len(runs) < len(rows):
                return {"runs": runs, "runs_cut": f"{len(rows) - len(runs)} older run(s) left out to keep the answer "
                                                  "small: narrow with machine_id, status or tags, or a smaller limit"}
            return {"runs": runs}
        return await self._run_tool(params, "list_runs", body, lambda r: f"{len(r['runs'])} run(s)")

    async def checked_machines(self) -> dict[str, dict[str, Any]]:
        """Each machine's problem counts (service.machine_checks) in a worker thread, one computation for every
        caller that asks meanwhile, read after its request came (Coalesced) -- the panel's route and the tool share
        it (API-2)."""
        return await self._checks()

    async def list_machines(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            # every machine loaded and validated: seconds for a few dozen, which must not hold up the event loop
            machines = await asyncio.to_thread(self.service.list_machines)
            checks = await self.checked_machines()
            machines = [{**machine, **checks.get(machine["id"], {})} for machine in machines]
            return {"machines": machines if self._admin_reader(params) else _for_reader(machines)}
        return await self._run_tool(params, "list_machines", body,
                                    lambda r: f"{len(r['machines'])} machine(s), "
                                              f"{sum(1 for m in r['machines'] if not m.get('valid'))} with errors")

    async def get_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            result = await asyncio.to_thread(self.service.get_machine, _need(params, "machine_id"))
            result.pop("graph", None)
            result.pop("layout", None)
            result = _one_file(result, params.get("file")) if params.get("file") else _small_files(result)
            return result if self._admin_reader(params) else _for_reader(result)
        return await self._run_tool(params, "get_machine", body,
                                    lambda r: f"{r['id']}: {len(r['files'])} file(s), {_counts(r['problems'])}")

    async def validate_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            result = await asyncio.to_thread(self.service.validate, files=_object(params, "files"),
                                             yaml=params.get("yaml"), machine_id=params.get("machine_id"))
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
            result = await asyncio.to_thread(self.service.save_machine, params.get("machine_id"), files,
                                             _object(params, "expected_versions"))
            result.pop("graph", None)
            return result
        return await self._run_tool(params, "save_machine", body,
                                    lambda r: f"saved {r['machine_id']}: {len(r['versions'])} file(s), "
                                              f"{_counts(r['problems'])}")

    async def run_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            run_params = _object(params, "params") or {}
            if not params.get("machine_id") and params.get("request"):
                machine_id, given = _request(str(params["request"]), self._param_types)
                run_params = {**given, **run_params}
            else:
                machine_id = _need(params, "machine_id")
            wait = _choice(params, "wait", ("finish", "background"))
            max_wait = _number(params, "max_wait", self.default_max_wait, 0, MAX_WAIT)
            full_output = _flag(params, "full_output")
            started = await self.service.start_run(
                machine_id, params=run_params, mocks=_object(params, "mocks"),
                mock_only=_flag(params, "mock_only"), pause_at_start=_flag(params, "pause_at_start"),
                breakpoints=params.get("breakpoints") or (),
                watchpoints=params.get("watchpoints") or (), user_id=params.get("_user_id"),
                run_key=params.get("run_key"), caller_session=params.get("_session_id"), tags=_object(params, "tags"))
            run_id = started["run_id"]
            if wait == "background" or started.get("attached") is False:
                # background -- or a run another process owns: nothing here to wait on, report its row
                row = self.run_store.get_run(run_id) or {}
                return _bounded({**started, **_summary(row)}) if row else {**started, "state": None,
                                                                           "run_status": "running"}
            row = await self._wait(run_id, max_wait, params.get("_cancellation_token"))
            return _bounded({**started, **_summary(row, full_output=full_output)},
                            keep=("output",) if full_output else ())
        return await self._run_tool(params, "run_machine", body,
                                    lambda r: f"run {r['run_id']}: {r.get('run_status')} in {r.get('state') or '-'}")

    async def _wait(self, run_id: str, max_wait: float, token: Any, *, terminate: bool = True) -> dict[str, Any]:
        """Wait for a run to leave running. A cancelled caller stops waiting; one that started the run (terminate)
        takes the run with it -- a reader (get_run) does not: it may not control a run it only sees."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while True:
            row = await self.run_manager.wait(run_id, timeout=1.0)
            if (row["status"] != "running" and not _timed(row) and not _event_due(row)) or loop.time() >= deadline:
                return row
            if _timed(row):  # run_manager.wait answers a wait at once: look again in a moment, not in a spin
                await asyncio.sleep(max(0.0, min(1.0, deadline - loop.time())))
            if token is None or not getattr(token, "is_cancelled", False):
                continue
            if not terminate:
                return row
            live = self.run_manager.live.get(run_id)
            if live is not None and not live.ctx.lost:
                self.run_manager.control(run_id, "terminate")  # the caller stopped: so does its run
                return await self.run_manager.wait(run_id, timeout=5.0)

    async def get_run(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            kinds = params.get("kinds")
            wait = _choice(params, "wait", ("now", "finish"))
            max_wait = _number(params, "max_wait", self.default_max_wait, 0, MAX_WAIT)
            full_output = _flag(params, "full_output")

            def read() -> dict[str, Any]:
                # 0 is refused like control_run's, not read as "not given"
                steps = params["steps"] if params.get("steps") is not None else 30
                # the engine's bookkeeping rows (step hashes, counters) only when kinds asks for trace rows (DBG-7)
                return self.service.get_run(_need(params, "run_id"), steps=steps,
                                            user_id=params.get("_user_id"), after=params.get("after"),
                                            kinds=[kinds] if isinstance(kinds, str) else kinds, state=params.get("state"),
                                            bookkeeping=kinds is not None)
            # off the loop, as the panel reads it: a run that is not live parses its definition (RT-2)
            row = await asyncio.to_thread(read)  # the user's right to see it, and the filters, before any wait
            if wait == "finish" and (row["status"] == "running" or _timed(row) or _event_due(row)):  # polled elsewhere
                await self._wait(row["id"], max_wait, params.get("_cancellation_token"), terminate=False)
                row = await asyncio.to_thread(read)
            if not self._holds_tokens(params.get("_user_id"), row.get("user_id")):
                # on the whole values, before any text is cut: a token cut in two would leave its first part
                row = _hidden(row, self._callback_tokens(row["id"]))
            if not self._admin_reader(params):  # before the budget measures it: what the reader gets
                row = _run_for_reader(row)
            view = row.get("view") or {}
            answer = _bounded({**_summary(row, full_output=full_output), "run_id": row["id"], **_identity(row),
                               "frames": _capped(view.get("frames", []), ROW_CHARS),
                               **({"inbox": _capped(view["inbox"], ROW_CHARS)} if view.get("inbox") else {}),
                               **({"spend": row["spend"]} if row.get("spend") else {}),  # what its agents cost
                               "journal": _once(_capped(row.get("journal", []), ROW_CHARS),
                                                _capped(row.get("error"), ROW_CHARS)),
                               "debug": _debug(row.get("debug"))},
                              keep=("output",) if full_output else (), forward=params.get("after") is not None)
            return answer
        return await self._run_tool(params, "get_run", body,
                                    lambda r: f"run {r['run_id']}: {r['run_status']} in {r.get('state') or '-'}")

    async def control_run(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            unknown = sorted(key for key in params
                             if not key.startswith("_") and key not in CONTROL_PARAMS and key not in FRAMEWORK_PARAMS)
            if unknown:
                raise ServiceError(422, f"unknown argument(s) {', '.join(unknown)} for control_run; it takes "
                                        f"{', '.join(sorted(CONTROL_PARAMS))}")
            run_id, action = _need(params, "run_id"), _need(params, "action")
            kwargs = {k: params[k] for k in ("state", "machine", "at_step", "definition", "breakpoints", "watchpoints",
                                              "expr", "path", "pause") if k in params}
            if "mocks" in params:
                kwargs["mocks"] = _object(params, "mocks")
            if params.get("steps") is not None:  # as get_run's: the answer carries that many journal rows
                kwargs["steps"] = params["steps"]
            result = await self.service.control_run(run_id, action, user_id=params.get("_user_id"), **kwargs)
            if "id" in result:  # a run row
                return _bounded({**_summary(result), "run_id": result["id"], "action": action,
                                 **({"journal": _capped(result.get("journal", []), ROW_CHARS)} if "steps" in kwargs
                                    else {})})
            return _bounded({"action": action, **_capped(result, OUTPUT_CHARS)})
        return await self._run_tool(params, "control_run", body,
                                    lambda r: f"{r['action']}: " + (f"run {r['run_id']} {r.get('run_status', '')}"
                                                                    if "run_id" in r else f"value {str(r.get('value'))[:80]}"))

    async def send_event(self, params: dict[str, Any]) -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            result = await self.service.deliver_event(_need(params, "run_id"), _need(params, "name"),
                                                      params.get("data"), params.get("frame"),
                                                      user_id=params.get("_user_id"))
            if not result.get("accepted"):
                raise ServiceError(409, result.get("reason") or "not accepted")
            if not result.get("queued"):  # a read right after shows what the event started, not the old wait
                await self.run_manager.taken(_need(params, "run_id"))
            return result
        return await self._run_tool(params, "send_event", body,
                                    lambda r: "event queued: no frame accepts it yet" if r.get("queued")
                                    else f"event accepted by frame {r.get('frame') or 'root'}")

    def _live_row(self, run_id: str) -> Optional[dict[str, Any]]:
        """The run's status and view as they stand now, while this process runs it -- else None."""
        live = self.run_manager.live.get(run_id)
        if live is None or live.task.done() or live.ctx.lost:
            return None
        return {"id": run_id, "status": live.ctx.status, "view": live.ctx.view()}

    # ------------------------------------------------------------ web
    @property
    def wait_answer_url(self) -> str:
        """Where a client posts the answer to a wait question (core.run_questions)."""
        return f"/plugins/{self.name}/answer"

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
            swept = self.service.sweep()  # also the runs of a process of this host that is gone (RES-8)
            if swept:
                logger.info("stategraph: %d run(s) interrupted by an expired lease or a gone process: %s",
                            len(swept), swept)
        except Exception:
            logger.warning("stategraph: lease sweep failed", exc_info=True)
        self._sweeper = asyncio.ensure_future(self._sweep_loop())
        from .service import usable_inject

        usable_inject(self.inject_params, f"instance {self.name}")  # an empty value said at the start (API-11)
        for problem in [*self.schedule_problems, *filter(None, [self.semaphore_problem])]:
            logger.error("stategraph: %s -- left out", problem)
        if self.schedules or self.auto_resume or _drives():  # the API resumes machines with resume: auto too
            self._scheduler = asyncio.ensure_future(Scheduler(self, self.schedules).loop())

    async def _sweep_loop(self) -> None:
        await self._report_facades()  # after the start, off its path: a machine agent's mistake shows in the log
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                self.service.sweep()
            except Exception:
                logger.debug("stategraph: lease sweep failed", exc_info=True)
            await self.compact()

    async def compact(self, now: Optional[float] = None) -> list[str]:
        """Retention (``retention_days``, FEAT-11): at most every COMPACT_SECONDS, the journal of the runs that
        ended before then dropped, a batch at a time off the event loop. Off by default. The run ids compacted."""
        import time

        from .schedules import _stamp

        now = time.time() if now is None else now
        if self.retention_days is None or now - self._compacted_at < COMPACT_SECONDS:
            return []
        self._compacted_at = now
        before = _stamp(now - self.retention_days * 86400)
        done: list[str] = []
        try:
            while True:
                batch = await asyncio.to_thread(self.run_store.compact_ended, before=before)
                done += batch
                if len(batch) < 50:
                    break
        except Exception:
            logger.warning("stategraph: retention: compacting ended runs failed", exc_info=True)
        if done:
            logger.info("stategraph: retention: the journal of %d run(s) that ended before %s dropped", len(done),
                        before)
        return done

    def facade_problems(self) -> dict[str, list[str]]:
        """What keeps each machine agent over this instance (``type: stategraph_machine``) from running: its machine
        is missing or has errors, or the params it passes leave a required one out -- else only the first request's
        answer would say so."""
        found: dict[str, list[str]] = {}
        for name, config in self._facades():
            problems = self._facade_problems(config)
            if problems:
                found[name] = problems
        return found

    def _facades(self) -> list[tuple[str, Any]]:
        """The enabled machine agents over this instance: (name, its resolved config)."""
        from agent_system.config.settings import _resolve_server_inheritance, get_tool_server_config

        servers = getattr(getattr(self.system_config, "plugins", None), "servers", None) or {}
        found = []
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
                found.append((name, config))
        return found

    def agents_of(self, machine_id: str) -> list[dict[str, Any]]:
        """The machine agents that run ``machine_id``: how they are offered, and what keeps one from running."""
        agents = []
        for name, config in self._facades():
            if str(getattr(config, "machine", None) or "") != machine_id:
                continue
            metadata = getattr(config, "metadata", None)
            agents.append({"name": name, "visibility": getattr(metadata, "visibility", None) or "private",
                           "input": str(getattr(config, "input", None) or "text"),
                           "on_wait": str(getattr(config, "on_wait", None) or "block"),
                           "problems": self._facade_problems(config)})
        return agents

    def _param_types(self, machine_id: str) -> dict[str, str]:
        """{param: declared type} of a machine; nothing when it does not load (start_run says why)."""
        try:
            tree = self.machines.load(machine_id)
            return {name: spec.type for name, spec in tree.files[tree.root].spec.params.items()}
        except Exception:
            return {}

    def _facade_problems(self, config: Any) -> list[str]:
        from plugins.stategraph.facade import config_problems

        own = config_problems(config)  # the facade answers every request with these
        if own:
            return own
        machine_id = str(getattr(config, "machine", None) or "")
        if self.machines.find(machine_id) is None:
            return [f"no machine {machine_id!r} in {', '.join(self.machine_dirs)}"]
        tree = self.service._validate(self.machines.load(machine_id))
        errors = [p for p in tree.problems if p.level == "error"]
        if errors:
            return [f"machine {machine_id} has {len(errors)} error(s), the first: {errors[0].code} {errors[0].message}"]
        return agent_params_problems(machine_id, tree.files[tree.root].spec.params, config)

    async def _report_facades(self) -> None:
        """In a worker thread: validating the machines (SG007 walks every configured server) takes a few hundred
        milliseconds the loop would otherwise stand still."""
        try:
            for name, problems in (await asyncio.to_thread(self.facade_problems)).items():
                logger.error("stategraph: machine agent %s cannot run: %s", name, "; ".join(problems))
        except Exception:
            logger.warning("stategraph: checking the machine agents failed", exc_info=True)

    def _set_semaphores(self, server_config: Any) -> dict[str, Any]:
        """``semaphores`` ({name: capacity}) and ``semaphore_hold_seconds`` into the run manager, whose acquires
        read them at every try; the fields that changed. A bad entry is named in semaphore_problem -- this
        config's, so a reload that fixes it clears it."""
        from .engine.backend import semaphores_of
        from .engine.runner import SEMAPHORE_HOLD_SECONDS

        raw = getattr(server_config, "semaphores", None)
        known = semaphores_of(server_config)
        self.semaphore_problem = None
        if raw is not None and (not isinstance(raw, dict) or len(known) != len(raw)):
            self.semaphore_problem = f"semaphores must map names to whole numbers of at least 1, not {raw!r}"
        hold = getattr(server_config, "semaphore_hold_seconds", None)
        hold = (float(hold) if isinstance(hold, (int, float)) and not isinstance(hold, bool) and hold >= 0
                else SEMAPHORE_HOLD_SECONDS)
        changed = {}
        if known != self.run_manager.semaphores:
            changed["semaphores"] = {"old": self.run_manager.semaphores, "new": known}
            self.run_manager.semaphores = known
        if hold != self.run_manager.semaphore_hold:
            changed["semaphore_hold_seconds"] = {"old": self.run_manager.semaphore_hold, "new": hold}
            self.run_manager.semaphore_hold = hold
        return changed

    def reload_config(self, server_config: Any) -> dict:
        """A config reload (POST /admin/reload-config): the semaphores take effect at the next try of every acquire;
        everything else needs a restart."""
        changed = self._set_semaphores(server_config)
        if self.semaphore_problem:
            logger.error("stategraph: %s -- left out", self.semaphore_problem)
        return changed

    async def stop_plugin(self) -> None:
        for task in (self._sweeper, self._scheduler, *self._ending):
            if task is not None:
                task.cancel()
        await self.run_manager.shutdown()
        if self._scheduler is not None:  # the next process (a restart, a reload) schedules at once
            try:
                for lease in (self.name, resume_lease(self.name)):
                    self.run_store.release_scheduler(lease, self.run_manager.owner)
            except Exception:  # a locked database: the lease runs out by itself
                logger.warning("stategraph: giving up the scheduler lease failed", exc_info=True)
        self.run_store.close()


def _need(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not value:
        raise ServiceError(422, f"{key} is required")
    return str(value)


def _request(text: str, types: Any = lambda machine_id: {}) -> tuple[str, dict[str, Any]]:
    """``<machine id> [params]`` (the slash command's line): params as a JSON object or as ``key=value`` words
    (shell-quoted; ``types(machine_id)`` -- {param: declared type} -- says which values read as JSON)."""
    machine_id, _, rest = text.strip().partition(" ")
    rest = rest.strip()
    if not machine_id:
        raise ServiceError(422, "name the machine: <machine id> [{json params} | key=value ...]")
    if not rest:
        return machine_id, {}
    if rest.startswith("{"):
        try:
            given = json.loads(rest)
        except ValueError as exc:
            raise ServiceError(422, f"the params after the machine id are no JSON object: {exc}") from None
        if not isinstance(given, dict):
            raise ServiceError(422, "the params after the machine id must be a JSON object")
        return machine_id, given
    lexer = shlex.shlex(rest, posix=True)
    lexer.whitespace_split, lexer.escape = True, ""  # quotes group words; a backslash stays (C:\data\x.txt)
    try:
        words = list(lexer)
    except ValueError as exc:
        raise ServiceError(422, f"params: {exc} (quote a value with spaces: key='two words')") from None
    declared = types(machine_id)
    given = {}
    for word in words:
        key, sep, value = word.partition("=")
        if not sep or not key:
            raise ServiceError(422, f"params: key=value words or a JSON object, not {word!r}")
        given[key] = _word_value(value, declared.get(key))
    return machine_id, given


def _word_value(value: str, declared: Optional[str]) -> Any:
    """A key=value word is text; a param declared as another type reads it as JSON (n=3, flag=true, ids=[1,2])."""
    if declared in (None, "string", "any"):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value  # the run's param check says what is wrong


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


def _capped(value: Any, limit: int) -> Any:
    """Every text in ``value`` of more than ``limit`` characters cut, saying how long it was."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}... ({len(value)} characters)"
    if isinstance(value, dict):
        return {key: _capped(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_capped(item, limit) for item in value]
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def _too_large(value: Any) -> dict[str, int]:
    return {"$too_large": _size(value)}


def _bounded(answer: dict[str, Any], *, keep: tuple[str, ...] = (), forward: bool = False) -> dict[str, Any]:
    """A tool answer of at most ANSWER_CHARS, budgeted once (API-1/API-8/DBG-2). While the answer without its
    journal is over its share (half the budget when there are journal rows, else all of it), the frames' largest ctx
    values become {'$too_large': size} first, then the largest top-level values but ``keep`` (output with
    full_output) -- each said in ``cut``. The journal fills what is left: the newest rows first (``forward``: the
    oldest, a page after ``after``), its cut said in ``journal_cut`` with how to read on."""
    answer = dict(answer)
    journal = answer.pop("journal", None)
    share = ANSWER_CHARS // 2 if journal else ANSWER_CHARS
    total = _size(answer)
    cut: list[str] = []
    if total > share and answer.get("frames"):
        frames = [{**frame, "ctx": dict(frame["ctx"])} if isinstance(frame.get("ctx"), dict) else frame
                  for frame in answer["frames"]]
        values = sorted(((_size(value), index, key) for index, frame in enumerate(frames)
                         if isinstance(frame.get("ctx"), dict) for key, value in frame["ctx"].items()), reverse=True)
        replaced = 0
        for size, index, key in values:
            if total <= share:
                break
            frames[index]["ctx"][key] = {"$too_large": size}
            total -= size - _size({"$too_large": size})
            replaced += 1
        if replaced:
            answer["frames"] = frames
            cut.append(f"{replaced} ctx value(s) of the frames replaced by {{'$too_large': size}}: read one with "
                       "control_run(action=evaluate, expr='ctx.<key>') while the run is paused; kinds=['activity'] "
                       "shows what each activity answered")
            total = _size(answer)
    if total > share:  # many short texts (an output, a debug watch, a long inbox): the largest values go whole
        for size, key in sorted(((_size(value), key) for key, value in answer.items() if key not in keep),
                                reverse=True):
            if total <= share or size < 1000:
                break
            answer[key] = {"$too_large": size}
            total -= size - _size(answer[key])
            cut.append(f"{key} replaced by its size" + (": read it whole with full_output=true" if key == "output"
                                                       else ""))
    if cut:
        answer["cut"] = cut
    if journal is not None:
        room = ANSWER_CHARS - _size(answer) - len(', "journal": [], "journal_cut": ""') - 200
        kept: list[dict[str, Any]] = []
        for row in (journal if forward else reversed(journal)):
            size = _size(row) + 2
            if size > ANSWER_CHARS // 10:  # one row of very many short texts: its data by its size
                row = {**row, "data": _too_large(row.get("data"))}
                size = _size(row) + 2
            if size > room:
                break
            kept.append(row)
            room -= size
        if not forward:
            kept.reverse()
        answer["journal"] = kept
        dropped = len(journal) - len(kept)
        if dropped and forward:
            answer["journal_cut"] = (f"{dropped} later row(s) left out to keep the answer small: go on with "
                                     f"after={kept[-1]['seq'] if kept else journal[0]['seq'] - 1}")
        elif dropped:
            first = journal[0].get("seq")
            answer["journal_cut"] = (f"{dropped} older row(s) left out to keep the answer small: read them with "
                                     f"after={first - 1 if isinstance(first, int) else '<seq>'} and a smaller steps, "
                                     "or narrow with kinds or state")
    return answer


def _small(value: Any) -> Any:
    """A value the debugger shows (a paused activity's out, a watch's value): its texts cut at ROW_CHARS, and the
    whole by its size when many short texts still make it large (DBG-2)."""
    capped = _capped(value, ROW_CHARS)
    return capped if _size(capped) <= 10 * ROW_CHARS else _too_large(value)


def _held(paused: Any) -> Any:
    """Where the debugger holds a run, its out and error as _small shows them -- read whole with
    control_run(action=evaluate, expr='out')."""
    if not isinstance(paused, dict):
        return paused
    return {key: _small(value) if key in ("out", "error") else value for key, value in paused.items()}


def _debug(debug: Any) -> dict[str, Any]:
    """get_run's debug: no ``paused`` (the answer has it at the top), each watch's value as _small shows it."""
    debug = {key: value for key, value in (debug or {}).items() if key != "paused"}
    if isinstance(debug.get("watch"), dict):
        debug["watch"] = {point: {key: _small(value) if key == "value" else value for key, value in shown.items()}
                          if isinstance(shown, dict) else shown for point, shown in debug["watch"].items()}
    return _capped(debug, ROW_CHARS)


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    """Which run it is and where it stands in its life (DBG-7): machine, times, who holds it, who started it and
    how, what it was forked from, the mocks it runs with -- the fields it has."""
    found = {key: row.get(key) for key in ("machine_id", "created_at", "updated_at", "finished_at", "owner",
                                           "lease_until", "run_key", "parent_run", "fork_step", "user_id")
             if row.get(key) not in (None, "")}
    options = row.get("mocks") or {}
    if options.get("mocks") or options.get("mock_only"):
        found["mocks"] = _capped(options, ROW_CHARS)
    found.update({key: row[key] for key in ("children", "started_by", "tags") if row.get(key)})
    return found


def _once(rows: list[dict[str, Any]], error: Any) -> list[dict[str, Any]]:
    """The run's error said once (DBG-7): a trace row (``failed``, ``end``) that repeats it points to ``error``."""
    if not error:
        return rows
    found = []
    for row in rows:
        data = row.get("data")
        if row.get("kind") == "trace" and isinstance(data, dict) and data.get("error") == error:
            row = {**row, "data": {**data, "error": "(the run's error, above)"}}
        found.append(row)
    return found


def _for_reader(value: Any) -> Any:
    """``value`` for a reader who may not save or run machines (API-3): no traceback, and a ``file`` or a machine
    ``root`` (a folder) as its name only -- no absolute server path."""
    if isinstance(value, dict):
        return {key: (Path(item).name if key in ("file", "root") and isinstance(item, str) else _for_reader(item))
                for key, item in value.items() if key != "traceback"}
    if isinstance(value, list):
        return [_for_reader(item) for item in value]
    return value


#: keys every engine error has (ActivityError.as_dict), and a guard's record (Frame.guards)
_ENGINE_ERROR = frozenset(("type", "message", "data", "cause"))
_GUARD_RECORD = frozenset(("at", "guard"))


def _run_for_reader(value: Any) -> Any:
    """A run's ``value`` for a reader who may not save or run machines (API-3): in the engine's errors and guard
    records -- wherever they sit, a machine may keep one in ctx -- no traceback and a ``file`` as its name only. A
    machine's own data (output, ctx) keeps its keys: a ``file`` there is the machine's, not a server path."""
    if isinstance(value, list):
        return [_run_for_reader(item) for item in value]
    if not isinstance(value, dict):
        return value
    found = {key: _run_for_reader(item) for key, item in value.items()}
    if _ENGINE_ERROR <= found.keys() or _GUARD_RECORD <= found.keys():
        if isinstance(found.get("file"), str):
            found["file"] = Path(found["file"]).name
        if isinstance(found.get("data"), dict) and "traceback" in found["data"]:
            found["data"] = {key: item for key, item in found["data"].items() if key != "traceback"}
    return found


def _one_file(machine: dict[str, Any], rel: Any) -> dict[str, Any]:
    """get_machine's answer with only the file ``rel`` of its tree, whole (API-9); versions of every file."""
    files = machine.get("files") or {}
    if not isinstance(rel, str) or rel not in files:
        raise ServiceError(404, f"{rel!r} is no file of machine {machine.get('id')}: one of {', '.join(sorted(files))}")
    return {**machine, "files": {rel: files[rel]}}


def _small_files(machine: dict[str, Any]) -> dict[str, Any]:
    """get_machine's answer within its bound (API-9): the root file always; every other file larger than
    FILE_CHARS, or past the budget (the largest first), left out with its size -- ``file=<path>`` reads it."""
    files = dict(machine.get("files") or {})
    root = machine.get("root_file")
    total = _size({**machine, "files": files})
    left: dict[str, int] = {}
    for rel in sorted((rel for rel in files if rel != root), key=lambda rel: -len(files[rel])):
        if len(files[rel]) <= FILE_CHARS and total <= ANSWER_CHARS:
            break
        total -= _size(files[rel])
        left[rel] = len(files.pop(rel))
    if not left:
        return machine
    return {**machine, "files": files, "left_out": left,
            "left_out_hint": "large files left out (characters): get_machine(machine_id, file=<path>) reads one; "
                             "save_machine takes only the files you change"}


def _hidden(value: Any, tokens: set[str]) -> Any:
    """``value`` with every one of ``tokens`` replaced by <hidden>, wherever a text holds it."""
    if not tokens:
        return value
    if isinstance(value, str):
        for token in tokens:
            value = value.replace(token, "<hidden>")
        return value
    if isinstance(value, dict):
        return {(_hidden(key, tokens) if isinstance(key, str) else key): _hidden(item, tokens)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_hidden(item, tokens) for item in value]
    return value


def _choice(params: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    """One of ``choices``; the first when the argument is missing."""
    value = params.get(key) or choices[0]
    if value not in choices:
        raise ServiceError(422, f"{key} must be one of {', '.join(choices)}, not {value!r}")
    return value


def _counts(problems: list[dict[str, Any]]) -> str:
    errors = sum(1 for p in problems if p["level"] == "error")
    return f"{errors} error(s), {len(problems) - errors} warning(s)"


def _summary(row: dict[str, Any], *, full_output: bool = False) -> dict[str, Any]:
    view = row.get("view") or {}
    frames = view.get("frames") or []
    root = frames[0] if frames else {}
    debug = row.get("debug") or {}
    return {"run_status": row.get("status"), "state": row.get("final_state") or root.get("state"),
            "output": row.get("output") if full_output else _capped(row.get("output"), OUTPUT_CHARS),
            "error": _capped(row.get("error"), ROW_CHARS),
            # what the debugger holds: its out and error cut like a journal row's (DBG-2)
            "paused": _held(debug.get("paused")),
            **({"next": NEXT_SLOT if row.get("status") == "waiting" and view.get("slot_waits")
                else NEXT_TIMER if _timed(row) else NEXT[row["status"]]} if row.get("status") in NEXT
               else {"next": NEXT_RESUMABLE if (row.get("error") or {}).get("resumable") else NEXT_INTERNAL}
               if row.get("status") == "failed" and resumable_failure(row.get("error"))
               else {}),
            "accepts": [{"frame": f.get("prefix", ""), "events": f.get("accepts")} for f in frames if f.get("accepts")],
            **({"mocks_unused": view["mocks_unused"]} if view.get("mocks_unused") else {})}


def _timed(row: dict[str, Any]) -> bool:
    """Whether a run waits without taking an event: in a timer state (after), it goes on by itself."""
    frames = (row.get("view") or {}).get("frames") or []
    return row.get("status") == "waiting" and not any(frame.get("accepts") for frame in frames)


PLUGIN_FACTORY = StateGraphServer
