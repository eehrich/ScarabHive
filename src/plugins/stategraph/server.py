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
from typing import Any, Optional

from agent_system.paths import data_path
from agent_system.tools.schema_based import SchemaBasedToolServer

from . import kinds as _kinds  # noqa: F401  -- registers the built-in activity kinds
from .engine.journal import RunStore
from .engine.runner import RunManager, row_event_due as _event_due
from .schedules import Scheduler, parse_schedules
from .service import ServiceError, StateGraphService
from .model.validate import agent_params_problems
from .store import MachineStore, machine_dirs

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 60
MAX_WAIT = 3600.0  # seconds a tool call may wait for its run
# a tool answer bounds itself (plugin rules): texts in journal rows, frames and errors, and in a run's output
ROW_CHARS = 2000
OUTPUT_CHARS = 20000
ANSWER_CHARS = 200000  # a whole get_run answer: many short texts add up too
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
#: A wait that takes no event -- a timer state (after): it goes on by itself.
NEXT_TIMER = ("it waits out a timer (after) and goes on by itself: stategraph_get_run(run_id, wait='finish') waits "
              "for its end, a pause or a wait")


class StateGraphServer(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: Any, server_config: Any):
        super().__init__(name, system_config, server_config)
        self.system_config = system_config
        # flat keys, defaults in code: the framework does not validate plugin config
        self.machine_dirs, self.writable_dirs = machine_dirs(server_config)
        self.runs_db = str(getattr(server_config, "runs_db", None) or data_path("stategraph", "runs.db"))
        self.runner_agent = str(getattr(server_config, "runner_agent", None) or "stategraph_runner")
        self.allowed_users = [str(u) for u in (getattr(server_config, "allowed_users", None) or [])]
        raw_inject = getattr(server_config, "inject_params", None) or {}
        self.inject_params = {str(k): dict(v) for k, v in raw_inject.items() if isinstance(v, dict)}
        self.default_max_wait = float(getattr(server_config, "default_max_wait", None) or 600)
        self.schedules, self.schedule_problems = parse_schedules(getattr(server_config, "schedules", None))
        self.machines = MachineStore(self.machine_dirs, self.writable_dirs)
        self.run_store = RunStore(self.runs_db)
        self.run_manager = RunManager(self.run_store, on_cancel=self._cancel_requests,
                                      on_finish=self._release_token)
        base = str(getattr(server_config, "public_url", None) or "").rstrip("/")  # e.g. https://hive.example.com
        self.run_manager.callback_base = f"{base}/plugins/{name}/callback"
        self.service = StateGraphService(self)
        self._registry: Any = None
        self._sweeper: Optional[asyncio.Task] = None
        self._scheduler: Optional[asyncio.Task] = None
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

    def _holds_tokens(self, user_id: Optional[str], owner: Optional[str]) -> bool:
        """Whether a reader may see the run's callback URLs whole: its own user, or an admin (without auth, the one
        user). Anyone else who sees the run -- a run of nobody -- would fire its events past send_event's gate."""
        return not self._auth_enabled() or (owner is not None and owner == user_id) or self._is_admin(user_id)

    def _callback_tokens(self, run_id: str) -> set[str]:
        """The tokens of every callback URL the run made: in its callback activities' answers."""
        tokens: set[str] = set()
        for url in self.run_store.callback_urls(run_id):
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
            found = self._catalog(str(params.get("agents") or "*"))
            found["tools"] = await self._runner_tools(str(params.get("tools") or "*"))
            return found
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
                "examples": [m.id for m in self.machines.list() if not m.own]}

    async def _runner_tools(self, pattern: str) -> list[dict[str, Any]]:
        """The tools a machine's tool activity may call: every registered tool the runner's allowlist lets through
        (the matcher dispatch uses), flat name, what it does and its parameters -- not the allowlist's patterns."""
        from agent_system.config.settings import get_tool_server_config
        from agent_system.plugins.tool_adapter import plugin_tool_registry
        from agent_system.servers.agent.server import Agent
        from agent_system.servers.agent.tool_schema_builder import tool_matches_patterns

        runner = get_tool_server_config(self.runner_agent, self.system_config)
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
                                          status=status, user_id=user, all_users=not mine)
            return {"runs": [{key: row.get(key) for key in ("id", "machine_id", "status", "final_state", "created_at",
                                                              "finished_at", "run_key", "parent_run")}
                             | ({"error": _capped((row.get("error") or {}).get("message"), 300)} if row.get("error")
                                else {}) for row in rows]}
        return await self._run_tool(params, "list_runs", body, lambda r: f"{len(r['runs'])} run(s)")

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
            run_params = _object(params, "params") or {}
            if not params.get("machine_id") and params.get("request"):
                machine_id, given = _request(str(params["request"]), self._param_types)
                run_params = {**given, **run_params}
            else:
                machine_id = _need(params, "machine_id")
            wait = _choice(params, "wait", ("finish", "background"))
            max_wait = _number(params, "max_wait", self.default_max_wait, 0, MAX_WAIT)
            started = await self.service.start_run(
                machine_id, params=run_params, mocks=_object(params, "mocks"),
                mock_only=_flag(params, "mock_only"), pause_at_start=_flag(params, "pause_at_start"),
                breakpoints=params.get("breakpoints") or (),
                watchpoints=params.get("watchpoints") or (), user_id=params.get("_user_id"),
                run_key=params.get("run_key"), caller_session=params.get("_session_id"))
            run_id = started["run_id"]
            if wait == "background" or started.get("attached") is False:
                # background -- or a run another process owns: nothing here to wait on, report its row
                row = self.run_store.get_run(run_id) or {}
                return {**started, **_summary(row)} if row else {**started, "state": None, "run_status": "running"}
            row = await self._wait(run_id, max_wait, params.get("_cancellation_token"))
            return {**started, **_summary(row, full_output=_flag(params, "full_output"))}
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

            def read() -> dict[str, Any]:
                # 0 is refused like control_run's, not read as "not given"
                steps = params["steps"] if params.get("steps") is not None else 30
                return self.service.get_run(_need(params, "run_id"), steps=steps,
                                            user_id=params.get("_user_id"), after=params.get("after"),
                                            kinds=[kinds] if isinstance(kinds, str) else kinds, state=params.get("state"))
            row = read()  # the user's right to see it, and the filters, before any wait
            if wait == "finish" and (row["status"] == "running" or _timed(row) or _event_due(row)):  # polled elsewhere
                await self._wait(row["id"], max_wait, params.get("_cancellation_token"), terminate=False)
                row = read()
            if not self._holds_tokens(params.get("_user_id"), row.get("user_id")):
                # on the whole values, before any text is cut: a token cut in two would leave its first part
                row = _hidden(row, self._callback_tokens(row["id"]))
            view = row.get("view") or {}
            return _bounded({**_summary(row, full_output=_flag(params, "full_output")), "run_id": row["id"],
                             "frames": _capped(view.get("frames", []), ROW_CHARS),
                             **({"inbox": view["inbox"]} if view.get("inbox") else {}),
                             "journal": _capped(row.get("journal", []), ROW_CHARS), "debug": row.get("debug")})
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
                return {**_summary(result), "run_id": result["id"], "action": action,
                        **({"journal": _capped(result.get("journal", []), ROW_CHARS)} if "steps" in kwargs else {})}
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
            if not result.get("queued"):  # a read right after shows what the event started, not the old wait
                await self.run_manager.taken(_need(params, "run_id"))
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
        for problem in self.schedule_problems:
            logger.error("stategraph: %s -- left out", problem)
        if self.schedules:
            self._scheduler = asyncio.ensure_future(Scheduler(self, self.schedules).loop())

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

    async def stop_plugin(self) -> None:
        for task in (self._sweeper, self._scheduler):
            if task is not None:
                task.cancel()
        await self.run_manager.shutdown()
        if self._scheduler is not None:  # the next process (a restart, a reload) schedules at once
            try:
                self.run_store.release_scheduler(self.name, self.run_manager.owner)
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


def _bounded(answer: dict[str, Any]) -> dict[str, Any]:
    """A get_run answer of at most ANSWER_CHARS: the oldest journal rows go first, then each frame's ctx is
    replaced by its keys and their sizes -- each step said in the answer, with how to read what was left out."""
    size = lambda value: len(json.dumps(value, default=str))  # noqa: E731
    journal = list(answer.get("journal") or [])
    dropped = 0
    while journal and size({**answer, "journal": journal}) > ANSWER_CHARS:
        journal.pop(0)
        dropped += 1
    answer = {**answer, "journal": journal}
    if dropped:
        answer["journal_cut"] = (f"{dropped} older row(s) left out to keep the answer small: page with after=<seq>, "
                                 "or narrow with kinds or state")
    if size(answer) > ANSWER_CHARS:
        answer["frames"] = [{**frame, "ctx": {"$too_large": {key: size(value) for key, value in
                                                               (frame.get("ctx") or {}).items()}}}
                            for frame in answer.get("frames") or []]
        answer["frames_cut"] = ("ctx replaced by its keys and their sizes: read single values with "
                                "control_run(action=evaluate) while the run is paused")
    return answer


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
            "paused": debug.get("paused"),
            **({"next": NEXT_TIMER if _timed(row) else NEXT[row["status"]]} if row.get("status") in NEXT else {}),
            "accepts": [{"frame": f.get("prefix", ""), "events": f.get("accepts")} for f in frames if f.get("accepts")],
            **({"mocks_unused": view["mocks_unused"]} if view.get("mocks_unused") else {})}


def _timed(row: dict[str, Any]) -> bool:
    """Whether a run waits without taking an event: in a timer state (after), it goes on by itself."""
    frames = (row.get("view") or {}).get("frames") or []
    return row.get("status") == "waiting" and not any(frame.get("accepts") for frame in frames)


PLUGIN_FACTORY = StateGraphServer
