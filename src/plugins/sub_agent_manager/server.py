"""Sub-Agent Manager Tool Server implementation."""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import logging
import time
import weakref
from datetime import UTC, datetime

import anyio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, List, Optional

from agent_system.tools.hook_tool_server import SchemaBasedHookToolServer
from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.core.session_presence import presence_for, wake_blocked, wake_session
from agent_system.services.session_manager import SessionNotFoundError
from agent_system.services.session_service import is_ephemeral_session
from agent_system.utils.id import short_id
from agent_system.llm.token_utils import extract_text_from_content

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

from plugins.sub_agent_manager.manager import CallerMistake, SubAgentLimitReached, SubAgentManager, message_counts

logger = logging.getLogger(__name__)


def agent_allowed(agent_name: str, allowed: List[str], blocked: List[str]) -> bool:
    """Whether a manager with these lists may spawn the agent: blocked (exact) first, then `*`, exact, fnmatch."""
    if agent_name in blocked:
        return False
    if '*' in allowed or agent_name in allowed:
        return True
    return any(fnmatch.fnmatch(agent_name, pattern) for pattern in allowed)


#: An aborted run comes back as TEXT ("Error: ..."/"Cancelled: ..."), while
#: `status` said "completed" regardless -- so a failed reviewer read like a
#: clean book, and every consumer had to re-derive the truth from the prefix.
#: The outcome now travels in its OWN field, next to the lifecycle status.
#:
#: Deliberately not in `status` itself, though that is where it belongs: the
#: caller (agent_caller._invoke) raises the moment `status` names a failure,
#: and it raises BEFORE the transport-failure counter runs -- the counter that
#: tells a run which survived thirty network errors from a clean one. Moving
#: the verdict into `status` would silently switch that guard off. Counting
#: comes before judging; until the caller counts first, the verdict lives here
#: and `status` keeps meaning "the run is over".
_ABORT_STATUS = (("Error:", "error"), ("Cancelled:", "cancelled"))

#: An aborted outcome as the job status it gives and the status it stores on the instance --
#: the same word in both, and in the background and the blocking way alike.
_ABORTED_AS = {"error": "failed", "cancelled": "cancelled"}


def _blocking_ending(result_text: str) -> str:
    """The status a blocking run leaves on its instance -- the background run's rule
    (`_execute_async_job`): failed or cancelled for an answer that aborted; otherwise open again,
    which leaves one archived while it ran archived (the manager's rule, `_write_sub_agent`).
    Left "active" after an abort, the instance read as idle: done, the answer ready."""
    return _ABORTED_AS.get(_outcome_status(result_text)) or "active"

#: How many instance ids a 'not found' names -- the most recently used ones.
HINT_IDS = 30

#: The longest a `wait` waits between two looks at a sub-agent this process has no job for --
#: reached by doubling from half a second. Each of those looks reads the parent's session file
#: (see `_handle_wait`), so the cadence of the cheap turn would be paid in disk here.
WAIT_DB_POLL_MAX = 8.0

#: How long this process keeps a background job's ending after the job ended, when nobody takes
#: it: no poll, no wait, no woken caller, no continue or delete -- a caller that polls much later,
#: or a throwaway turn that never comes back. Kept for good, each held its whole result for the
#: life of the API process. A STORED ending goes after this: a poll then answers from the
#: sub-session's stored state, as after a restart.
FINISHED_JOB_RETENTION_SECONDS = 3600.0
#: An ending that could not be stored is the only answer there is, so it stays much longer --
#: but not for good: a parent deleted while its job ran is the usual reason a write fails, and
#: then no caller can read the entry at all (poll and wait answer only the parent).
UNSTORED_JOB_RETENTION_SECONDS = 86400.0


def _outcome_status(result_text: str) -> str:
    """Verdict for a finished run: 'completed' unless the text says otherwise."""
    for prefix, status in _ABORT_STATUS:
        if result_text.startswith(prefix):
            return status
    return "completed"


def _stamp(value: Any) -> Optional[datetime]:
    """A stored time. Every one written carries its zone (measured: 4326 of 4326)."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _left_mid_run(metadata: dict[str, Any]) -> bool:
    """Whether a stored activity nobody holds was left by a run that died, not after one ended.

    Every ending of a run that leaves the instance open records `last_used` after the run is over
    (one that closes it stores failed or cancelled, which is shown as such). An activity written
    later than that came from a run whose end was never recorded: its process died, or its
    caller's did before the ending reached anyone -- either way nobody has the result. One written
    earlier is the last status line of a run from before `_consume_run` cleared the activity at its
    ending. Measured 21.09.2026: 1118 of those ("✅ completed (N steps)") against 10 unrecorded
    ends (9 "⚙️ Calling LLM ...", 1 whose caller died). The words cannot tell them apart: mid-run
    a tool's status line ends with "completed" too.
    """
    written, ended = _stamp(metadata.get("activity_updated_at")), _stamp(metadata.get("last_used"))
    return bool(metadata.get("current_activity")) and written is not None \
        and (ended is None or written > ended)


def _log_failure(operation: str, error: Exception) -> None:
    """How a handler logs the error it answers with: a CallerMistake at INFO, anything else with
    its traceback."""
    if isinstance(error, CallerMistake):
        logger.info(f"{operation} refused: {error}")
    else:
        logger.exception(f"Error in {operation}: {error}")


def _without_status(params: dict) -> dict:
    """Params for an INTERNAL sub-step, so it cannot close our status scope.

    All handlers share one StatusScope per tool call. A handler that calls a
    sibling with the original params hands it ``_status``; the sibling ends
    the scope, and everything the outer handler says afterwards is dropped
    (``StatusScope.ended``). The outer handler owns the line.
    """
    return {k: v for k, v in params.items() if k != "_status"}


def _job_answer(job: dict[str, Any]) -> dict[str, Any]:
    """A background job as a poll or a wait hands it out: without the task handle (not
    serializable) and without our bookkeeping, every key of which starts with "_"."""
    return {k: v for k, v in job.items() if k != "task_handle" and not k.startswith("_")}


async def _ringing_over(job: dict[str, Any], bell: Callable[[], Awaitable[None]]) -> None:
    """Ring the bell of a job's ending with the job marked as rung for, so the retention leaves
    it alone meanwhile (`_drop_expired_endings`)."""
    job["_ringing"] = True
    try:
        await bell()
    finally:
        job["_ringing"] = False


def _register_request_user(request_id: str, user_id: str) -> None:
    """Register sub-agent request_id -> user_id mapping.

    This ensures sub-agent requests are properly associated with their user
    in the admin dashboard and other user-aware features.
    """
    from agent_system.core.request_context import register_request_user
    register_request_user(request_id, user_id)
    logger.debug(f"Registered sub-request {request_id} for user {user_id}")


def _injector_options(server_config: Any) -> dict:
    """The inject_sub_agent_context options: the server entry's
    ``hook_config.inject_sub_agent_context`` block."""
    hook_config = getattr(server_config, 'hook_config', None) or {}
    return dict(hook_config.get("inject_sub_agent_context") or {})


class SubAgentManagerServer(SchemaBasedHookToolServer):
    """tool server for sub-agent management with hook support.

    Provides a unified tool `manage_sub_agent`: create, continue, list, info, delete, and for
    background runs poll, wait, wait_all and cancel.

    Also implements a pre_llm_call hook that appends the session's sub-agents as a turn at the end.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """Initialize SubAgentManagerServer.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        # Tool server and hook in one: the base class initialises both
        # halves and builds the hook config (schema defaults, plugins.yaml on top).
        super().__init__(name, system_config, server_config)

        # Configuration
        self.max_sub_agents = int(getattr(server_config, 'max_sub_agents_per_session', 10))
        self.max_nesting_depth = int(getattr(server_config, 'max_nesting_depth', 5))
        self.max_history = int(getattr(server_config, 'max_message_history', 100))
        self.max_sub_agents_per_type = int(getattr(server_config, 'max_sub_agents_per_type', 3))
        
        # Auto-archive oldest sub-agent when limit is reached
        self.auto_archive_on_limit = bool(getattr(server_config, 'auto_archive_on_limit', False))

        # Timeout configuration
        self.default_wait_timeout = int(getattr(server_config, 'default_wait_timeout', 3600))  # Default 1 hour

        # 'info' pagination (see _handle_info): the coordinator most often
        # wants the tail, but has to be able to page through the FULL
        # transcript too, the same way a file-reading tool offers offset+limit.
        # info_max_limit is a hard per-call cap, not a policy choice — it stops
        # one call from dumping the entire history back into the coordinator's
        # own context; page with 'offset' instead.
        self.info_default_limit = int(getattr(server_config, 'info_default_limit', 20))
        self.info_max_limit = int(getattr(server_config, 'info_max_limit', 200))
        self.info_default_max_chars = int(getattr(server_config, 'info_default_max_chars', 4000))

        # Agent filtering (multi-instance support - by instance name, not type)
        self.allowed_agents = list(getattr(server_config, 'allowed_agents', ['*']))
        self.blocked_agents = list(getattr(server_config, 'blocked_agents', []))

        # Kosten-Riegel: darf der AUFRUFER use_advanced_model=true setzen?
        # LLM-Caller setzen das Flag gern aus Eigeninitiative (Prod-Befund
        # 2026-07-20: der v6-Coordinator spawnte JEDES Panel mit
        # use_advanced_model=true, ohne dass sein Prompt es verlangt — der
        # komplette Moderator-Run lief still auf der advanced-Kette).
        # False = Flag wird ignoriert (mit Log); Default True = Bestand.
        self.allow_advanced_model = bool(getattr(server_config, 'allow_advanced_model', True))

        # Same class of guard, one notch finer: for these agent types the
        # caller's use_advanced_model is honoured on `create` only; a
        # `continue` on them always runs the normal chain. Built for the v6
        # idea writers, whose advanced chain is the premium model: the
        # moderator's prompt legitimately asks for advanced continues
        # (synthesis, stuck), and each of those would be a premium call over
        # a 100k+ context. Empty by default = existing behaviour.
        raw_create_only = getattr(server_config, 'advanced_create_only_agents', None)
        self.advanced_create_only_agents = (
            set(raw_create_only) if isinstance(raw_create_only, (list, tuple, set)) else set()
        )

        # Options of the inject_sub_agent_context hook (hook_config block)
        self._injector_config = _injector_options(server_config)

        # Phase-based agent filtering (affects both tool schema and create validation)
        # Config is at top-level (same as allowed_agents), not inside hook_config
        phase_config = getattr(server_config, 'phase_filtering', {}) or {}
        self.phase_filtering_enabled = phase_config.get('enabled', False)
        self.phase_variable = phase_config.get('phase_variable', 'workflow_phase')
        self.phase_agents = phase_config.get('phase_agents', {})

        # Track running sub-agent instances to prevent concurrent execution
        # Format: {sub_session_id: True}
        self._running_agents: set[str] = set()
        self._running_lock = asyncio.Lock()
        # The task that took each running slot (`_take_slot`), weakly: a leaked slot must not keep
        # its ended run's frames alive for the life of the process.
        self._slot_holders: dict[str, weakref.ref[asyncio.Task[Any]]] = {}

        # A blocking create/continue, from the moment it is marked running: instance_id -> {agent, request_id
        # (both None until the run begins), parent_session_id, cancelled, early (a cancel came before that)}.
        # `cancel` stops it through the agent's own cancellation: the run ends with its "cancelled" event and
        # saves its session like any other end. (An async job below is stopped by its task handle.)
        self._blocking_runs: dict[str, dict[str, Any]] = {}

        # Track async jobs by instance_id: {instance_id: {task, status, started_at, result, error}}
        # A finished job stays until a poll has read its ending (or a continue replaces it) -- one its caller called off
        # itself (cancel, delete) only until that ending is stored; the stored state answers after
        self._async_jobs: dict[str, dict[str, Any]] = {}
        self._async_jobs_lock = asyncio.Lock()

    def reload_config(self, server_config: Any) -> dict:
        """Hot-reload the mutable, config-derived fields from a freshly parsed
        ToolServerConfig — WITHOUT tearing down this instance or its running sub-agents.

        Called by the deliberate config-reload flow (POST /admin/reload-config,
        `agent-cli reload`). Only the plain filter/limit knobs are refreshed;
        running jobs, sessions and history are untouched. Adding a brand-new
        agent *definition* still needs a restart (the agent must be registered),
        but changing this manager's ``allowed_agents`` / limits / phase filtering
        now takes effect live.

        Returns a dict of the fields that actually changed ({} if nothing did),
        so the caller can report exactly what the reload updated.
        """
        changes: dict[str, dict] = {}

        # The hook half reads its config lazily off server_config and caches
        # it (SchemaBasedHookToolServer), so both have to point at the new one.
        # Nothing in THIS plugin reads `self.config` today -- the injector gets
        # `_injector_config`, refreshed below -- but the base class hands
        # `self.config` to every hook author as the hook's configuration, and
        # it must not be the one answer in this object that a reload missed.
        # Note that re-pointing `server_config` also refreshes what is read
        # lazily from it elsewhere (custom tool descriptions); the report below
        # lists changed FIELDS, not that.
        self.server_config = server_config
        self._hook_config = None

        def _upd(attr: str, new_value: Any) -> None:
            old_value = getattr(self, attr, None)
            if old_value != new_value:
                changes[attr] = {"old": old_value, "new": new_value}
                setattr(self, attr, new_value)

        _upd("allowed_agents", list(getattr(server_config, 'allowed_agents', ['*'])))
        _upd("blocked_agents", list(getattr(server_config, 'blocked_agents', [])))
        _upd("allow_advanced_model", bool(getattr(server_config, 'allow_advanced_model', True)))
        _upd("max_sub_agents", int(getattr(server_config, 'max_sub_agents_per_session', 10)))
        _upd("max_nesting_depth", int(getattr(server_config, 'max_nesting_depth', 5)))
        _upd("max_sub_agents_per_type", int(getattr(server_config, 'max_sub_agents_per_type', 3)))
        _upd("max_history", int(getattr(server_config, 'max_message_history', 100)))
        _upd("auto_archive_on_limit", bool(getattr(server_config, 'auto_archive_on_limit', False)))
        _upd("default_wait_timeout", int(getattr(server_config, 'default_wait_timeout', 3600)))
        _upd("info_default_limit", int(getattr(server_config, 'info_default_limit', 20)))
        _upd("info_max_limit", int(getattr(server_config, 'info_max_limit', 200)))
        _upd("info_default_max_chars", int(getattr(server_config, 'info_default_max_chars', 4000)))

        raw_create_only = getattr(server_config, 'advanced_create_only_agents', None)
        _upd("advanced_create_only_agents",
             set(raw_create_only) if isinstance(raw_create_only, (list, tuple, set)) else set())

        _upd("_injector_config", _injector_options(server_config))

        phase_config = getattr(server_config, 'phase_filtering', {}) or {}
        _upd("phase_filtering_enabled", phase_config.get('enabled', False))
        _upd("phase_variable", phase_config.get('phase_variable', 'workflow_phase'))
        _upd("phase_agents", phase_config.get('phase_agents', {}))

        if changes:
            logger.info(
                "SubAgentManager '%s' config reloaded: %s",
                getattr(self, 'name', '?'), ", ".join(sorted(changes.keys())),
            )
        return changes

    def _effective_use_advanced(self, params: dict[str, Any]) -> bool:
        """Vom Aufrufer angefordertes ``use_advanced_model`` gegen den
        Instanz-Riegel ``allow_advanced_model`` prüfen. Unterdrückung wird
        geloggt (kein stilles Umbiegen) — der Sub-Agent läuft dann auf
        seiner normalen Profil-Kette."""
        requested = bool(params.get("use_advanced_model", False))
        if requested and not self.allow_advanced_model:
            logger.info(
                "[%s] use_advanced_model angefordert, aber per Config "
                "unterdrückt (allow_advanced_model=false) — Standard-Profil.",
                self.name,
            )
            return False
        return requested

    def _continue_use_advanced(self, agent_type: str, requested: bool) -> bool:
        """Second half of the guard, applied on `continue` once the instance's
        agent type is known: types listed in ``advanced_create_only_agents``
        get the advanced chain on their first call only. Suppression is
        logged, never silent."""
        if requested and agent_type in self.advanced_create_only_agents:
            logger.info(
                "[%s] use_advanced_model on continue of '%s' suppressed "
                "(advanced_create_only_agents) — normal chain.",
                self.name, agent_type,
            )
            return False
        return requested

    def is_agent_running(self, instance_id: str) -> bool:
        """Check if sub-agent is actually running (has active async task OR synchronous execution).
        
        Args:
            instance_id: Sub-agent instance ID
            
        Returns:
            True if agent has active task in _async_jobs with running/pending status,
            OR if agent is in _running_agents (synchronous continue/spawn execution),
            False otherwise
        """
        # Check synchronous running agents first (continue operation uses this)
        if instance_id in self._running_agents:
            return True
        
        # Check async jobs (async spawn operation uses this)
        if instance_id not in self._async_jobs:
            return False
        
        job_status = self._async_jobs[instance_id].get("status", "unknown")
        return job_status in ("pending", "running")

    def get_template_vars(self) -> dict:
        """Return template variables for schema rendering.

        Uses same filtering logic as GET /agents endpoint:
        - Check if server is an Agent instance
        - Respect _tool_public visibility flag

        This is called dynamically at runtime (not cached) to ensure
        the agent list is always up-to-date.
        """
        from agent_system.plugins.tool_adapter import plugin_tool_registry
        from agent_system.servers.agent.server import Agent

        agent_names = []
        for name in plugin_tool_registry.list_servers():
            if name.startswith('_') or name in self.blocked_agents:
                continue

            try:
                # Access plugin_servers dict directly (PluginToolRegistry has no .get() method)
                adapter = plugin_tool_registry.plugin_servers.get(name)
                if not adapter:
                    continue

                # Get the actual plugin instance from the adapter
                srv = adapter.plugin_server  # PluginToolAdapter.plugin_server is the actual server instance

                # Same logic as GET /agents endpoint
                if isinstance(srv, Agent):
                    # Check visibility flags: need either _tool_public (UI) OR _tool_visible (tool)
                    # Skip only if BOTH are explicitly False (private agents)
                    is_ui_visible = getattr(srv, '_tool_public', False)
                    is_tool_visible = getattr(srv, '_tool_visible', False)

                    if not is_ui_visible and not is_tool_visible:
                        continue  # Skip truly private agents

                    agent_names.append(name)
            except Exception as e:
                # Unexpected error - log and skip
                logger.debug(f"SubAgentManagerServer '{self.name}' skipping server '{name}': {type(e).__name__}: {e}")
                continue

        # The same check as a spawn: a glob in allowed_agents must not let an
        # agent be spawned that the model never sees listed.
        agent_names = [name for name in agent_names if self._is_agent_allowed(name)]

        return {
            'name': self.name,  # CRITICAL: Must include 'name' for {{ name }} template variable in schema.yaml
            'allowed_agents': agent_names,
            'phase_filtering_enabled': self.phase_filtering_enabled,
            'phase_variable': self.phase_variable
        }

    async def list_tools(self) -> list:
        """Override list_tools() to re-render schema dynamically.

        This ensures the agent list in the schema is always up-to-date,
        since agents are registered after this plugin is initialized.
        """
        # Invalidate schema cache to force re-rendering
        self._schema_cache = None

        # Call parent implementation (will re-render with fresh template vars)
        return await super().list_tools()

    def _extract_session_service(self, params: dict[str, Any]):
        """Extract session_service from params (_session_service or _agent._session_service).

        Args:
            params: Tool parameters with either _session_service or _agent

        Returns:
            SessionService instance

        Raises:
            RuntimeError: If session_service cannot be found
        """
        # Try direct injection first (from WebUI endpoints)
        session_service = params.get("_session_service")
        if session_service:
            return session_service

        # Try getting from agent instance (from ToolExecutionManager)
        agent = params.get("_agent")
        if agent and hasattr(agent, '_session_service'):
            return agent._session_service

        raise RuntimeError(
            "No session_service available - neither _session_service nor _agent._session_service found. "
            "This tool must be called from an agent or with explicit session_service injection."
        )

    def _get_manager(self, session_service, registry=None) -> SubAgentManager:
        """Get SubAgentManager with injected dependencies.

        Args:
            session_service: SessionService instance (injected from params["_session_service"])
            registry: ToolServerRegistry instance (injected from params["_registry"], optional for some ops)

        Returns:
            SubAgentManager instance

        Note:
            Uses injected session_service from Agent instead of creating a new one.
            This ensures all tools use the same session storage and avoids duplication.
        """
        # Use injected session_service (passed from Agent via ToolExecutionManager)
        if not session_service:
            raise RuntimeError("session_service is required but was not injected")

        # Create manager with injected dependencies
        return SubAgentManager(
            session_service,
            registry,
            self.max_nesting_depth,
            self.max_sub_agents_per_type,
            self.max_sub_agents,
            auto_archive_on_limit=self.auto_archive_on_limit,
        )

    def _extract_registry(self, params: dict[str, Any]):
        """Extract and validate registry from params.

        Args:
            params: Tool parameters with injected _agent

        Returns:
            ToolServerRegistry instance

        Raises:
            RuntimeError: If registry not found
        """
        # Extract from agent instance (new pattern - same as session_service)
        agent = params.get("_agent")
        if agent and hasattr(agent, 'registry'):
            return agent.registry

        raise RuntimeError(
            "No registry available - neither _registry nor _agent.registry found. "
            "This tool must be called from an agent."
        )

    def _infer_operation(self, params: dict[str, Any]) -> Optional[str]:
        """Best-effort, SAFE inference when the model omits ``operation`` — a
        common, turn-wasting mistake (especially forgetting it on ``continue``).

        Only the two unambiguous argument shapes are inferred; anything else
        returns None so the caller still gets the explicit "Missing operation"
        error rather than a silently wrong action:
        - ``agent_type`` present, no ``instance_id`` -> ``create`` (agent_type
          exists ONLY to spawn a new agent).
        - ``instance_id`` + a prompt (``message``/``task``), no ``agent_type`` ->
          ``continue`` (a follow-up to an existing instance; the read-only ops
          poll/wait/info/cancel/delete never carry a prompt).

        Deliberately NOT inferred (ambiguous -> explicit error): ``instance_id``
        alone (poll vs info vs cancel vs delete is unknowable), or both
        ``agent_type`` and ``instance_id`` together.
        """
        has_type = bool(params.get("agent_type"))
        has_id = bool(params.get("instance_id"))
        has_prompt = bool(params.get("message") or params.get("task"))
        if has_type and not has_id:
            return "create"
        if has_id and has_prompt and not has_type:
            return "continue"
        return None

    async def manage_sub_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        """Unified handler for all sub-agent operations.

        Dispatches to operation-specific handlers based on params["operation"].
        """
        operation = params.get("operation")
        status = params.get("_status")  # Get status early for error reporting

        if not operation:
            # Safety net: the model often forgets `operation` (esp. on continue).
            # Infer it from the argument shape when unambiguous — saves the retry
            # turn — but only for the two safe cases; otherwise error as before.
            operation = self._infer_operation(params)
            if operation:
                logger.info(
                    "SubAgentManager: no 'operation' given — inferred '%s' from the arguments",
                    operation,
                )

        if not operation:
            if status:
                await status.error("Missing 'operation' parameter")
            return {"status": "error", "error": "Missing 'operation' parameter"}

        try:
            if operation == "create":
                return await self._handle_create(params)
            elif operation == "continue":
                return await self._handle_continue(params)
            elif operation == "poll":
                return await self._handle_poll(params)
            elif operation == "wait":
                return await self._handle_wait(params)
            elif operation == "wait_all":
                return await self._handle_wait_all(params)
            elif operation == "cancel":
                return await self._handle_cancel(params)
            elif operation == "list":
                return await self._handle_list(params)
            elif operation == "info":
                return await self._handle_info(params)
            elif operation == "delete":
                return await self._handle_delete(params)
            else:
                if status:
                    await status.error(f"Unknown operation: {operation}")
                return {"status": "error", "error": f"Unknown operation: {operation}"}
        except Exception as e:
            logger.exception(f"Error in manage_sub_agent ({operation}): {e}")
            if status:
                await status.error(f"Sub-agent error ({operation}): {str(e)}")
            return {"status": "error", "error": str(e)}

    @staticmethod
    async def _take_the_session(agent: Any, instance_id: str, request_id: str) -> None:
        """The sub-agent's session lock, under the id its run takes it by (a re-entry there), before anything of
        the session is touched. _prepare_agent sets the metadata and vars a run of it reads, and the refresh and
        the reopen write its record: a run of this process that has the session -- a chat on the sub-agent's
        session, a run still finishing -- had them replaced under it, and this run was then refused at the lock
        itself. Raises CallerMistake while one has it (an append or /undo saving it is waited for)."""
        if not await agent._session_tracker.acquire_session_lock(instance_id, request_id, timeout=5.0):
            raise CallerMistake(
                f"Sub-agent '{instance_id}' is running in another request of this process right now. "
                "Wait for it before you continue it.")

    @staticmethod
    async def _let_go_of_the_session(agent: Any, instance_id: str, request_id: str) -> None:
        """The lock _take_the_session took, if its run never started. A run that did registered its request
        (Agent.run_events, before it takes the lock again) and holds the lock under the same id: its end lets go
        of it -- let go of here, a run still closing lost it to the next one, and saved its turn under that."""
        tracker = agent._session_tracker
        if (tracker.check_session_locked(instance_id) == (True, request_id)
                and tracker.get_session_for_request(request_id) is None):
            await tracker.release_session_lock(instance_id, request_id)

    async def _prepare_agent(
        self,
        agent: Any,
        manager: SubAgentManager,
        session_service: Any,
        params: dict[str, Any],
        *,
        parent_session_id: str,
        instance_id: str,
        agent_name: str,
        context_vars: Optional[dict[str, Any]] = None,
    ) -> str:
        """Hand a registry agent the session it is about to run, and return that session's user.

        The agents come from the registry and are shared, so every run sets this again rather than
        trusting what the last one left: the session service it persists through (the same pattern
        app.py and agent_cli.py use), and the session's metadata, which is where tool execution
        reads the user id from.

        `context_vars` is what the caller has already resolved -- continue refreshes them against
        the parent's live state. Without it, the sub-session's own vars are read, which is what it
        inherited from the parent when it was created.
        """
        agent._session_service = session_service
        user_id = manager._extract_user_id(parent_session_id, params)
        agent._session_tracker.set_session_metadata(instance_id, {
            "user_id": user_id,
            "agent_name": agent_name,
            "llm_profile": getattr(agent.agent_config, 'default_llm_profile', 'normal')
        })
        logger.debug(f"Set session metadata for sub-agent {instance_id}: user_id={user_id}")

        try:
            if context_vars is None:
                sub_session_data = await session_service.session_manager.load_session(
                    user_id, instance_id
                )
                context_vars = sub_session_data.get("context_vars", {})
            if context_vars:
                # Session-scoped template vars (session-isolated, no global mutation)
                agent._session_tracker.set_session_template_vars(instance_id, context_vars)
                logger.debug(
                    f"Restored context_vars for sub-agent {instance_id}: {list(context_vars.keys())}"
                )
        except Exception as e:
            # A run without its inherited vars renders a staler prompt; a run that does not
            # happen renders none. Both callers have always chosen the first.
            logger.warning(f"Could not restore context_vars for sub-agent {instance_id}: {e}")
        return user_id

    async def _consume_run(
        self,
        agent: Any,
        manager: SubAgentManager,
        *,
        parent_session_id: str,
        instance_id: str,
        task: str,
        request_id: str,
        use_advanced_model: bool = False,
        run: Optional[dict[str, Any]] = None,
    ) -> str:
        """One run of a sub-agent, from its first event to its last, and the text it ends with.

        Every caller runs a sub-agent the same way -- create, continue and the background job --
        so they share this. What the callers do differ in is what happens around the run, not
        inside it.

        `run` is the entry in `_blocking_runs`: with one, a cancel that arrived while the run was
        still being prepared reaches the request as soon as it exists. A background job has none;
        it is stopped by its task handle.

        The text is what the caller hands back to the model: the answer, or "Error: ..." /
        "Cancelled: ..." -- the two prefixes `_outcome_status` reads the outcome from.
        """
        async def track(activity: Optional[str]) -> None:
            try:
                await manager.update_sub_agent_activity(parent_session_id, instance_id, activity)
            except Exception as activity_err:
                # Don't fail execution if activity tracking fails
                logger.debug(f"Activity tracking failed: {activity_err}")

        # The activity is set only while the run holds its session's lock. A process that dies
        # mid-run leaves it behind, and that is what `_shown_status` reads as interrupted -- so an
        # activity nobody holds must never come from a run that is fine. The run takes its lock
        # right after "start" (Agent._presence_hold), before anything here sets one, and lets go
        # of it before its trailing status lines and "end" (Agent._run_events' finally): hence
        # cleared at the ending event, and nothing said after it. The finally is for a run that
        # raised.
        result_text, over = "", False
        events = agent.run_events(
            task=task,
            request_id=request_id,
            session_id=instance_id,
            use_advanced_model=use_advanced_model,
            # Note: config_overrides would go here if Agent.run_events supported them
            # For now, sub-agent uses its default configuration
        )
        try:
            async for event in events:
                if run is not None and run.pop("early", False):  # cancelled while it was prepared
                    await agent.cancel_request(request_id)
                event_type = event.get("type")
                ending = event_type in ("final", "error", "cancelled")

                # Track activity for the live status display
                activity = None
                if over or ending:
                    pass
                elif event_type == "thinking_delta":
                    activity = "💭 Thinking..."
                elif event_type in ("tool_call", "mcp_call"):  # the old name until every deployed side is new (rename 17.09.2026)
                    activity = f"🔧 Running tool: {event.get('action', 'tool')}"
                elif event_type == "status":
                    phase = event.get("phase", "progress")
                    icon = {"error": "❌", "end": "✅"}.get(phase, "⚙️")
                    activity = f"{icon} {event.get('message', 'Processing...')}"
                if activity:
                    await track(activity)
                if ending and not over:
                    over = True
                    await track(None)

                # Collect final result (can be "final", "error", or "cancelled")
                if event_type == "final":
                    result_text = event.get("summary", "")
                    # DON'T break here - continue iterating to get "end" event
                    # This ensures _finalize_request runs and messages are persisted
                elif event_type == "end":
                    # Generator fully completed, messages are now in SessionTracker
                    break
                elif event_type in ("error", "cancelled"):
                    if event_type == "error":
                        result_text = f"Error: {event.get('message', 'Unknown error')}"
                        logger.warning(f"Sub-agent {instance_id} returned error: {result_text}")
                    else:
                        # The event the agent loop sends carries neither, and then the run was stopped
                        # from outside: the sentence says that rather than "Unknown".
                        reason = event.get("reason") or event.get("message") or "Request was cancelled"
                        result_text = f"Cancelled: {reason}"
                        logger.info(f"Sub-agent {instance_id} was cancelled: {result_text}")
                    break  # Stop waiting for more events
        finally:
            try:
                await track(None)
            finally:
                # Closed here, not left to the garbage collector: that closes it in another task, and
                # this one would keep the run's request id and user (Agent.run_events restores them).
                # Shielded: a cancel scope hands its cancel out again at every await -- the close was
                # skipped with track(None), and the run lay at a yield holding its session's lock until
                # the collector came; or its last save and its session-end hooks were cut short.
                with anyio.CancelScope(shield=True):
                    await events.aclose()
        return result_text

    async def _handle_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'create' operation - create and execute new sub-agent."""
        # Get status context early (before try block) so it's available in except
        status = params.get("_status")
        
        try:
            # Extract & validate required parameters. LLMs (especially weak
            # local models) sometimes call 'create' without these — return an
            # actionable error so the agent can correct the call instead of
            # getting an opaque KeyError and blindly retrying the same broken
            # call (raw params["..."] surfaced just "'agent_type'" to the agent).
            agent_name = params.get("agent_type")  # This is actually the agent instance name
            task = params.get("task")
            missing = [
                key for key, val in (("agent_type", agent_name), ("task", task))
                if not (isinstance(val, str) and val.strip())
            ]
            if missing:
                phase_allowed = self._get_phase_allowed_agents(params)
                concrete = phase_allowed or [a for a in self.allowed_agents if a != "*"]
                agent_hint = (
                    f"one of: {', '.join(concrete)}" if concrete
                    else "the name of a registered agent"
                )
                error_msg = (
                    f"Missing required parameter(s) for 'create': {', '.join(missing)}. "
                    f"'create' needs 'agent_type' ({agent_hint}) and "
                    f"'task' (the instruction text for the sub-agent)."
                )
                logger.info(f"create rejected — {error_msg}")
                if status:
                    await status.error(error_msg)
                return {
                    "status": "error",
                    "error": error_msg,
                    "error_type": "missing_parameter",
                }

            instance_label = params.get("instance_label")
            use_advanced_model = self._effective_use_advanced(params)
            blocking = params.get("blocking", True)  # NEW: default to blocking behavior
            # Note: config_overrides would be used here when Agent.run_events supports them
            # For now, sub-agent uses its default configuration

            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available - this tool must be called from an agent")

            # Validate agent is allowed by this manager instance (with phase filtering)
            # The manager's own allow/block lists first: a denial there is not
            # the phase's doing, even while a phase is active.
            phase_allowed = self._get_phase_allowed_agents(params)
            if not self._is_agent_allowed(agent_name):
                allowed_str = ', '.join(self.allowed_agents)
                error_msg = (
                    f"Agent '{agent_name}' not allowed by this sub-agent manager. "
                    f"Allowed agents: {allowed_str}"
                )
                logger.info(f"Agent filtering blocked agent: {error_msg}")
                if status:
                    await status.error(error_msg)
                return {
                    "status": "error",
                    "error": error_msg,
                    "error_type": "agent_blocked"
                }
            if not self._is_agent_allowed_for_phase(agent_name, phase_allowed):
                current_phase = self._get_current_phase(params)
                error_msg = (
                    f"Agent '{agent_name}' not allowed in current phase '{current_phase}'. "
                    f"Allowed agents for this phase: {', '.join(phase_allowed)}"
                )
                logger.info(f"Phase filtering blocked agent: {error_msg}")
                if status:
                    await status.error(error_msg)
                return {
                    "status": "error",
                    "error": error_msg,
                    "error_type": "phase_blocked"
                }

            if status:
                await status.progress(f"Creating sub-agent: {agent_name}")

            # Get manager with injected dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)

            # Inject creator_plugin into params so manager knows which instance created this sub-agent
            params["_creator_plugin"] = self.name

            # Create sub-session (pass params for user_id extraction)
            try:
                sub_session_id = await manager.create_sub_session(
                    parent_session_id=parent_session_id,
                    agent_type=agent_name,
                    initial_message=task,
                    instance_label=instance_label,
                    params=params  # Pass params for user_id extraction and creator_plugin
                )
            except SubAgentLimitReached as e:
                return await self._limit_reached(e, status, instance_id=None, agent_type=agent_name)

            logger.info(f"Created sub-session {sub_session_id} for parent {parent_session_id}")

            # If non-blocking, start async execution and return immediately
            if not blocking:
                if status:
                    await status.progress(f"Starting async execution of {agent_name}")
                
                # Store job metadata
                async with self._async_jobs_lock:
                    self._async_jobs[sub_session_id] = {
                        "instance_id": sub_session_id,
                        "status": "pending",
                        "agent_type": agent_name,
                        "task": task,
                        "parent_session_id": parent_session_id,
                        "started_at": datetime.now(UTC).isoformat(),
                        "completed_at": None,
                        "result": None,
                        "error": None,
                        "task_handle": None
                    }

                # Start background execution
                task_coro = self._execute_async_job(
                    instance_id=sub_session_id,
                    params=params,
                    agent_name=agent_name,
                    task=task,
                    use_advanced_model=use_advanced_model
                )
                task_handle = asyncio.create_task(task_coro)
                
                # Store task handle
                async with self._async_jobs_lock:
                    self._async_jobs[sub_session_id]["task_handle"] = task_handle
                    self._async_jobs[sub_session_id]["status"] = "running"

                if status:
                    await status.end(f"Async execution started: {sub_session_id}")

                return {
                    "instance_id": sub_session_id,
                    "status": "running",
                    "agent_type": agent_name,
                    "message": await self._async_started_message(
                        params, parent_session_id, manager),
                }

            # BLOCKING path: Check if sub-agent is already running (prevent concurrent execution)
            async with self._running_lock:
                if sub_session_id in self._running_agents:
                    raise ValueError(
                        f"Sub-agent '{sub_session_id}' is already running. "
                        "Cannot execute the same sub-agent instance concurrently. "
                        "Wait for current execution to complete or use a different instance."
                    )
                # Mark as running -- and cancellable from here on, not only once the run has begun
                self._take_slot(sub_session_id)
                run = self._blocking_runs[sub_session_id] = {
                    "agent": None, "request_id": None, "parent_session_id": parent_session_id}

            # False while the stored status knows nothing of how this run ends (_store_blocking_abort)
            settled = False
            try:
                # Get agent from registry
                agent = registry.get(agent_name)
                if not agent:
                    raise ValueError(f"Agent '{agent_name}' not found in registry")

                user_id = await self._prepare_agent(
                    agent, manager, session_service, params,
                    parent_session_id=parent_session_id, instance_id=sub_session_id,
                    agent_name=agent_name,
                )

                # Execute sub-agent with initial task (blocking)
                if status:
                    await status.progress(f"Executing {agent_name} with initial task...")

                # Generate hierarchical request ID: parent_request_id + "_sub_" + counter
                parent_request_id = params.get("_request_id")
                if parent_request_id:
                    # Use parent's request ID as base
                    sub_request_id = f"{parent_request_id}_sub_{short_id(6)}"
                else:
                    # Fallback to simple ID if no parent request_id
                    sub_request_id = f"sub_{short_id()}"

                # Register sub-request user mapping for admin dashboard
                _register_request_user(sub_request_id, user_id)
                run.update(agent=agent, request_id=sub_request_id)

                result_text = await self._consume_run(
                    agent, manager,
                    parent_session_id=parent_session_id, instance_id=sub_session_id,
                    task=task, request_id=sub_request_id,
                    use_advanced_model=use_advanced_model, run=run,
                )

                # Save session with messages after execution
                user_id = manager._extract_user_id(parent_session_id, params)
                # Get actual LLM profile from agent configuration
                llm_profile = agent.agent_config.default_llm_profile
                await session_service.save_session(
                    agent=agent,
                    user_id=user_id,
                    session_id=sub_session_id,
                    agent_name=agent_name,
                    llm_profile=llm_profile,
                    was_new_session=True,
                    after_run=True,
                )
                logger.debug(f"Saved sub-agent session {sub_session_id} with messages")

                # Update metadata after execution: message_count, and how the run ended
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=sub_session_id,
                    last_used=datetime.now(UTC).isoformat(),
                    message_count=2,  # user + assistant for initial creation
                    status=_blocking_ending(result_text),
                )
                settled = True

                if status:
                    # result_text carries the outcome ("Error: ..."/"Cancelled: ...").
                    # The end line reported "Created ..." either way, so an aborted
                    # run was the green line that stayed in the WebUI.
                    if _outcome_status(result_text) != "completed":
                        await status.error(
                            f"Sub-agent {sub_session_id} ({agent_name}): "
                            f"{result_text[:70]}")
                    else:
                        await status.end(
                            f"Created sub-agent {sub_session_id} (type: {agent_name}), "
                            f"{len(result_text)} chars")

                return {
                    "instance_id": sub_session_id,
                    "status": "completed",          # lifecycle: the run is over
                    "outcome": _outcome_status(result_text),  # verdict: how it ended
                    "result": result_text,
                    "message_count": 2,  # user + assistant
                    "agent_type": agent_name
                }
            except (Exception, asyncio.CancelledError) as error:
                if not settled:
                    await self._store_blocking_abort(manager, parent_session_id, sub_session_id, error)
                raise
            finally:
                # ALWAYS release lock, even on error (CRITICAL for preventing deadlock)
                async with self._running_lock:
                    self._release_slot(sub_session_id)
                    self._blocking_runs.pop(sub_session_id, None)
                logger.debug(f"Released running lock for sub-agent {sub_session_id}")

        except Exception as e:
            _log_failure("create_sub_agent", e)
            if status:
                await status.error(f"Failed to create sub-agent: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_continue(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'continue' operation - continue existing sub-agent."""
        # Get status context early (before try block) so it's available in except
        status = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status:
                await status.error("Continue: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            # Extract and validate required parameters
            instance_id = params.get("instance_id")
            if not instance_id:
                if status:
                    await status.error("Continue: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}
            
            # Accept `task` as a fallback for `message`: when the model omits
            # `operation` and this call is inferred as a continue, it often put
            # the follow-up prompt in `task` (the create field) by habit.
            message = params.get("message") or params.get("task")
            if not message:
                if status:
                    await status.error("Continue: 'message' is required")
                return {"status": "error", "error": "Missing required parameter: 'message'"}
            use_advanced_model = self._effective_use_advanced(params)

            # Get parent session ID from injected context
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            # Get manager with injected dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)

            sub_session_data = await self._callers_sub_session(manager, user_id, parent_session_id, instance_id)

            # Get agent type from session data
            agent_type = sub_session_data.get("agent_name") or ""
            try:
                agent = registry.get(agent_type)
            except KeyError:  # the registry raises for a name it does not know
                agent = None
            if not agent:
                raise ValueError(f"Agent type '{agent_type}' not found")
            use_advanced_model = self._continue_use_advanced(agent_type, use_advanced_model)

            # A run holds the lock beside the sub-session for as long as it lasts: one of another
            # process -- a coordinator woken into a process of its own continues an instance whose
            # job still runs in the API -- or one of this process that is still finishing: a run
            # that ended on an error or a cancel saves its transcript after the slot is let go
            # (the agent's own finally, run once the stream it stopped reading is dropped). The
            # slots know neither, and two runs on one transcript each save their own, the later
            # one over the other.
            if instance_id not in self._running_agents \
                    and await self._runs_in_another_process(instance_id, user_id):
                where = ("is still finishing its last run"
                         if presence_for(self.system_config).held_here(instance_id, user_id)
                         else "is already running in another process")
                raise CallerMistake(
                    f"Sub-agent '{instance_id}' {where}. "
                    "Wait for it with 'wait' or 'poll' before you continue it.")

            # Check if sub-agent is already running (prevent concurrent execution)
            # CRITICAL: Use single try-finally to ensure _running_agents is ALWAYS cleaned up
            async with self._running_lock:
                if instance_id in self._running_agents:
                    raise (CallerMistake if self._slot_has_a_run(instance_id) else ValueError)(
                        f"Sub-agent '{instance_id}' is already running. "
                        "Cannot execute the same sub-agent instance concurrently. "
                        "Wait for current execution to complete."
                    )
                # Mark as running -- and cancellable from here on, not only once the run has begun
                self._take_slot(instance_id)
                run = self._blocking_runs[instance_id] = {
                    "agent": None, "request_id": None, "parent_session_id": parent_session_id}

            # Generate hierarchical request ID for continue operation -- before anything else: the
            # session's lock is taken under it, and the run takes it again under the same id
            parent_request_id = params.get("_request_id")
            if parent_request_id:
                sub_request_id = f"{parent_request_id}_sub_cont_{short_id(6)}"
            else:
                sub_request_id = f"sub_cont_{short_id()}"

            # True while the stored status knows nothing of this run: before the reopen it still tells
            # the last one's ending, and a reopen refused at a limit leaves it at that
            settled = True
            try:
                await self._take_the_session(agent, instance_id, sub_request_id)
                # Reopen BEFORE execution starts -- and only once this run is registered: a continue
                # refused as "already running" must not touch the run it met.
                await manager.reopen_sub_session(parent_session_id, instance_id)
                settled = False

                # An earlier background run of this instance that has ended: this run replaces its ending, which
                # a later poll or wait would report instead of this run's
                async with self._async_jobs_lock:
                    if self._async_jobs.get(instance_id, {}).get("status") in ("completed", "failed", "cancelled"):
                        del self._async_jobs[instance_id]

                # CRITICAL: Refresh context_vars from the PARENT's live state, rather than the
                # sub-session's own snapshot that _prepare_agent would read.
                #
                # A sub-agent inherits context_vars only once, at create time.
                # Re-reading only its own frozen snapshot here would render the
                # prompt from stale state: after the coordinator calls
                # set_context(aufgabe=World) and continues this sub-agent,
                # {{ aufgabe }} would still say the previous task while the task
                # text says "World" — two contradicting instructions. Vars the
                # sub-agent set itself survive (see merge_parent_context_vars).
                try:
                    context_vars = await manager.refresh_sub_context_vars(
                        user_id=user_id,
                        sub_session_id=instance_id,
                        sub_session_data=sub_session_data,
                        parent_agent=params.get("_agent"),
                        parent_session_id=parent_session_id,
                    )
                except Exception as e:
                    # None, not {}: `_prepare_agent` then reads the sub-session's own snapshot.
                    # An empty dict rendered the prompt with no vars at all.
                    logger.warning(f"Could not restore context_vars for continued sub-agent: {e}")
                    context_vars = None

                user_id = await self._prepare_agent(
                    agent, manager, session_service, params,
                    parent_session_id=parent_session_id, instance_id=instance_id,
                    agent_name=agent_type, context_vars=context_vars,
                )

                if status:
                    await status.progress(f"Continuing {agent_type} with new message...")

                # Execute sub-agent with new message (continues existing session)
                # Register sub-request user mapping for admin dashboard
                _register_request_user(sub_request_id, user_id)
                run.update(agent=agent, request_id=sub_request_id)

                result_text = await self._consume_run(
                    agent, manager,
                    parent_session_id=parent_session_id, instance_id=instance_id,
                    task=message, request_id=sub_request_id,
                    use_advanced_model=use_advanced_model, run=run,
                )

                # Save session with updated messages after execution
                user_id = manager._extract_user_id(parent_session_id, params)
                # Get actual LLM profile from agent configuration
                llm_profile = agent.agent_config.default_llm_profile
                await session_service.save_session(
                    agent=agent,
                    user_id=user_id,
                    session_id=instance_id,
                    agent_name=agent_type,
                    llm_profile=llm_profile,
                    was_new_session=False,  # Updating existing session
                    after_run=True,
                )
                logger.debug(f"Saved continued sub-agent session {instance_id} with messages")

                # Get current message count (after the new exchange)
                messages = sub_session_data.get("messages", [])
                new_message_count = len(messages) + 2  # existing + user + assistant

                # Update last_used, message_count, and how the run ended
                await manager.update_sub_session_metadata(
                    parent_session_id=parent_session_id,
                    sub_session_id=instance_id,
                    last_used=datetime.now(UTC).isoformat(),
                    message_count=new_message_count,
                    status=_blocking_ending(result_text),
                )
                settled = True

                if status:
                    # Same as the create path: an aborted continuation must
                    # not leave a green line behind.
                    if _outcome_status(result_text) != "completed":
                        await status.error(
                            f"Sub-agent {instance_id} ({agent_type}): "
                            f"{result_text[:70]}")
                    else:
                        await status.end(
                            f"Continued sub-agent {instance_id} (type: {agent_type}), "
                            f"{len(result_text)} chars")

                return {
                    "instance_id": instance_id,
                    "status": "completed",          # lifecycle: the run is over
                    "outcome": _outcome_status(result_text),  # verdict: how it ended
                    "result": result_text,
                    "message_count": len(messages) + 2,  # existing + user + assistant
                    "agent_type": agent_type
                }
            except (Exception, asyncio.CancelledError) as error:
                if not settled:
                    await self._store_blocking_abort(manager, parent_session_id, instance_id, error)
                raise
            finally:
                # The session's lock, if the run did not let go of it (it never started)
                await self._let_go_of_the_session(agent, instance_id, sub_request_id)
                # ALWAYS release lock, even on error (CRITICAL for preventing deadlock)
                async with self._running_lock:
                    self._release_slot(instance_id)
                    self._blocking_runs.pop(instance_id, None)
                logger.debug(f"Released running lock for sub-agent {instance_id}")

        except SubAgentLimitReached as e:
            # the reopen takes a place: refused like a create, not reported as a crash
            return await self._limit_reached(e, status, instance_id=params.get("instance_id"))
        except Exception as e:
            _log_failure("continue_sub_agent", e)
            if status:
                await status.error(f"Failed to continue sub-agent: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'list' operation - list all sub-agents.
        
        Only shows sub-agents created by this plugin instance (filtered by creator_plugin).
        """
        status = params.get("_status")
        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            include_completed = params.get("include_completed", False)

            # Get session_service from params (supports both WebUI and tool calls)
            session_service = self._extract_session_service(params)

            # Get manager with injected dependencies (registry optional for list)
            registry = params.get("_registry")  # Optional - won't fail if missing
            manager = self._get_manager(session_service, registry)

            # List sub-sessions - filter by this plugin instance. A run that failed or was
            # cancelled is listed too: its instance is still there to continue, and a caller that
            # did not end it itself learns of it nowhere else. Archived ones only on request.
            sub_sessions = [
                metadata for metadata in await manager.list_sub_sessions(
                    parent_session_id=parent_session_id,
                    include_completed=True,
                    creator_plugin=self.name  # Only show sub-agents created by THIS instance
                )
                if include_completed or metadata.get("status") != "archived"
            ]

            session_manager = session_service.session_manager
            user_id = manager._extract_user_id(parent_session_id, params)
            # From the parent's sub-index, as the panel counts: it loaded every transcript for it.
            counts = await message_counts(session_manager, user_id, parent_session_id)

            instances = []
            for metadata in sub_sessions:
                instance_id = metadata["instance_id"]
                sub_status = await self._shown_status(metadata, lambda: user_id)

                # A run left behind by a process that died: said once, here, and written, so the
                # stale activity stops reading as a run under way (the panel shows what is stored).
                if sub_status == "interrupted" and metadata["status"] != "interrupted":
                    logger.warning(
                        f"Sub-agent {instance_id} has status='{metadata['status']}' in DB but no active task. "
                        f"Marking as 'interrupted' (likely server restart or crash)."
                    )
                    healed = await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=instance_id,
                        # Only over what it judged: the list was read before the loop, and a run
                        # that ended since -- its ending written, then its slot let go -- reads as
                        # a crash on that copy. Healed anyway, a clean ending said "crashed".
                        expect={key: metadata.get(key) for key in ("status", "last_used", "activity_updated_at")},
                        status="interrupted",
                        completed_at=datetime.now(UTC).isoformat(),
                        error="Server restarted or crashed while sub-agent was running",
                        # Clear stale activity to prevent WebUI from displaying RUNNING forever.
                        current_activity=None,
                        activity_updated_at=None,
                    )
                    if healed:
                        metadata = {**metadata, "current_activity": None, "activity_updated_at": None}
                    else:
                        try:
                            fresh = await manager.list_sub_sessions(parent_session_id, include_completed=True)
                        except Exception as error:  # the heal was quiet about a failure, so is this
                            logger.warning(f"Could not re-read sub-agent {instance_id}: {error}")
                            fresh = []
                        metadata = next((m for m in fresh if m.get("instance_id") == instance_id), metadata)
                        if not include_completed and metadata.get("status") == "archived":
                            continue
                        sub_status = await self._shown_status(metadata, lambda: user_id)

                actual_message_count = counts.get(instance_id)
                if actual_message_count is None:  # no row: a sub-session older than its index, or a failed update
                    try:
                        actual_message_count = len(
                            (await session_manager.load_session(user_id, instance_id)).get("messages", []))
                    except Exception:
                        actual_message_count = metadata.get("message_count", 0)
                
                instances.append({
                    "instance_id": instance_id,
                    "agent_type": metadata["agent_type"],
                    "status": sub_status,
                    "created_at": metadata["created_at"],
                    "last_used": metadata["last_used"],
                    "task_summary": metadata["task_summary"],
                    "current_activity": metadata.get("current_activity"),
                    "activity_updated_at": metadata.get("activity_updated_at"),
                    "message_count": actual_message_count
                })

            # Emit descriptive status
            count = len(instances)
            if status:
                if count == 0:
                    await status.end("No sub-agents found")
                elif count == 1:
                    await status.end(f"Listed 1 sub-agent: {instances[0]['instance_id']}")
                else:
                    agent_ids = ", ".join(inst["instance_id"] for inst in instances[:3])
                    suffix = f", +{count-3} more" if count > 3 else ""
                    await status.end(f"Listed {count} sub-agents: {agent_ids}{suffix}")

            return {
                "instances": instances,
                "count": len(instances)
            }

        except Exception as e:
            logger.exception(f"Error in list_sub_agents: {e}")
            if status:
                await status.error(f"Failed to list sub-agents: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'delete' operation - archive sub-agent(s).
        
        Supports both single instance_id and multiple instance_ids (array).
        """
        status = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status:
                await status.error("Delete: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            # Support both single instance_id and array instance_ids
            instance_id = params.get("instance_id")
            instance_ids = params.get("instance_ids", [])
            
            # Build list of IDs to delete
            ids_to_delete: list[str] = []
            if instance_id:
                ids_to_delete.append(instance_id)
            if instance_ids:
                ids_to_delete.extend(instance_ids)
            
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_ids: list[str] = []
            for id_ in ids_to_delete:
                if id_ not in seen:
                    seen.add(id_)
                    unique_ids.append(id_)
            ids_to_delete = unique_ids
            
            if not ids_to_delete:
                if status:
                    await status.error("Delete: 'instance_id' or 'instance_ids' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id' or 'instance_ids'"}
            
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            # Get manager with injected dependencies (registry optional for delete)
            registry = params.get("_registry")  # Optional
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)

            # Process each instance
            results: list[dict[str, Any]] = []
            archived_count = 0
            
            if status and len(ids_to_delete) > 1:
                await status.progress(f"Archiving {len(ids_to_delete)} sub-agents...")
            
            for sub_id in ids_to_delete:
                try:
                    # The lookup continue and info make: an id of another session or user, or none
                    # a session can have, is not found -- and was a bare ValueError from the panel.
                    try:
                        sub_session_data = await self._callers_sub_session(
                            manager, user_id, parent_session_id, sub_id)
                    except CallerMistake as mistake:
                        results.append({"instance_id": sub_id, "status": "error", "error": str(mistake)})
                        continue

                    # Get agent type for status message
                    agent_type = sub_session_data.get("agent_name", "unknown")

                    # Update parent metadata (mark as archived)
                    written = await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=sub_id,
                        status="archived",
                        archived_at=datetime.now(UTC).isoformat(),
                        ending_unread=None,  # the caller's own: it is done with the ending
                    )
                    if not written:
                        # It gives up quietly (parent unreadable, no sub_agents metadata, id not
                        # among them) and raises nothing, so without this the caller is told
                        # "archived" about an instance that is still active, still counted and
                        # still pollable -- and its result has been dropped underneath it.
                        results.append({
                            "instance_id": sub_id,
                            "status": "error",
                            "error": f"Sub-agent '{sub_id}' could not be archived",
                        })
                        continue

                    await self._archive_job(sub_id)

                    results.append({
                        "instance_id": sub_id,
                        "status": "archived",
                        "agent_type": agent_type
                    })
                    archived_count += 1
                    
                except Exception as e:
                    results.append({
                        "instance_id": sub_id,
                        "status": "error",
                        "error": str(e)
                    })

            # Build response
            if len(ids_to_delete) == 1:
                # Single delete - return simple format for backwards compatibility
                result = results[0]
                if result["status"] == "archived":
                    if status:
                        await status.end(f"Archived sub-agent {result['instance_id']} (type: {result.get('agent_type', 'unknown')})")
                    return {
                        "instance_id": result["instance_id"],
                        "status": "archived",
                        "message": f"Sub-agent '{result['instance_id']}' archived successfully"
                    }
                else:
                    if status:
                        await status.error(result.get("error", "Unknown error"))
                    return result
            else:
                # Multiple deletes - return batch result
                failed_count = len(ids_to_delete) - archived_count
                if status:
                    if failed_count == 0:
                        await status.end(f"Archived {archived_count} sub-agents")
                    else:
                        await status.end(f"Archived {archived_count}/{len(ids_to_delete)} sub-agents ({failed_count} failed)")
                
                return {
                    "status": "completed",
                    "archived_count": archived_count,
                    "failed_count": failed_count,
                    "total": len(ids_to_delete),
                    "results": results
                }

        except Exception as e:
            logger.exception(f"Error in delete_sub_agent: {e}")
            if status:
                await status.error(f"Failed to delete sub-agent: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _known_instance_hint(self, manager: SubAgentManager, parent_session_id: str) -> str:
        """A one-line hint for a 'not found' error: what instance_id would have worked.

        Built after production hit this on 2026-09-16: a coordinator asked for
        info on an instance_id it had assembled itself from another instance's
        label and a third one's numeric suffix -- neither expired nor
        mistyped, just never issued. A bare "not found" gives a guessing agent
        nothing to correct itself with; naming the real siblings does.
        """
        try:
            known = await manager.list_sub_sessions(parent_session_id, include_completed=True)
        except Exception:
            return "Could not look up this session's sub-agents either."
        if not known:
            return "This session has no sub-agents at all -- 'create' one first."
        # The most recently used first, as `list_sub_sessions` sorts them, and not all of them: a
        # writer session has hundreds, and every one of them went into the model's context.
        ids = [m["instance_id"] for m in known if m.get("instance_id")]
        named = ", ".join(ids[:HINT_IDS]) + (
            f" and {len(ids) - HINT_IDS} more ('list' with include_completed names them)"
            if len(ids) > HINT_IDS else "")
        return f"This session's sub-agents are: {named}. Use the instance_id exactly as returned, never assembled from a label and a guessed number."

    async def _callers_sub_session(self, manager: SubAgentManager, user_id: str, parent_session_id: str,
                                   instance_id: str) -> dict[str, Any]:
        """The session of the caller's sub-agent `instance_id`. CallerMistake for an id that names
        none: one never issued, one of another session, or one no session id can be -- a label or a
        quoted id, which `load_session` refuses as a bare ValueError, as it does a corrupt file."""
        session_manager = manager._session_service.session_manager
        sub_session_data = None
        try:
            session_manager._validate_session_id(instance_id)
        except ValueError:
            pass  # no session is called that
        else:
            try:
                sub_session_data = await session_manager.load_session(user_id, instance_id)
            except (FileNotFoundError, SessionNotFoundError):
                pass
        if sub_session_data is None:
            raise CallerMistake(
                f"Sub-agent '{instance_id}' not found. "
                f"{await self._known_instance_hint(manager, parent_session_id)}"
            )
        parent_link = sub_session_data.get("parent_session", {}).get("session_id")
        if parent_link != parent_session_id:
            raise CallerMistake(
                f"Sub-agent '{instance_id}' does not belong to current session "
                f"(actual_parent={parent_link}, caller={parent_session_id})"
            )
        return sub_session_data

    def _take_slot(self, instance_id: str) -> None:
        """Mark the instance running, held by the task that runs it. Under `_running_lock`."""
        self._running_agents.add(instance_id)
        task = asyncio.current_task()
        if task is not None:
            self._slot_holders[instance_id] = weakref.ref(task)

    def _release_slot(self, instance_id: str) -> None:
        """Let go of the instance's running slot -- unless another run still going holds it. A job
        whose run finds its instance already running -- a continue took the slot first -- ends
        without having held it (`_finish_job`): freeing the slot then let a second continue run
        beside the first on the same transcript. Under `_running_lock`."""
        holder = self._slot_holder(instance_id)
        if holder is not None and holder is not asyncio.current_task():
            return
        self._running_agents.discard(instance_id)
        self._slot_holders.pop(instance_id, None)

    def _slot_holder(self, instance_id: str) -> Optional[asyncio.Task[Any]]:
        """The task still going that holds the instance's running slot, None for none."""
        ref = self._slot_holders.get(instance_id)
        task = ref() if ref is not None else None
        return task if task is not None and not task.done() else None

    def _slot_has_a_run(self, instance_id: str) -> bool:
        """Whether the instance's running slot is held by a run still going: the task that took it
        has not ended. Every run lets go of it in a `finally`, so a slot whose task has ended, or
        that no task took, leaked -- the manager's fault, not the caller's, and it refuses every
        continue on that instance for the life of the process."""
        return self._slot_holder(instance_id) is not None

    async def _handle_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'info' operation - read a sub-agent's transcript, paged.

        Mirrors a file-reading tool's offset/limit model rather than a fixed
        snapshot: most calls just want the tail (the default -- omit
        'offset'), but a coordinator that needs to audit or resume precise
        context has to be able to page through the FULL transcript, not be
        stuck with a six-message, 200-character peephole. See 'window' in the
        response to know whether there is more to page through.
        """
        status = params.get("_status") if params else None

        # Validate params
        if not params:
            if status:
                await status.error("Info: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}

        try:
            parent_session_id = params.get("_session_id")
            if not parent_session_id:
                raise ValueError("No session context available")

            instance_id = params.get("instance_id")
            if not instance_id:
                if status:
                    await status.error("Info: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}

            # Extract dependencies from injected params
            session_service = self._extract_session_service(params)

            # Get manager (registry is optional for info operation)
            registry = params.get("_registry")
            manager = self._get_manager(session_service, registry)
            user_id = manager._extract_user_id(parent_session_id, params)
            session_manager = manager._session_service.session_manager

            sub_session_data = await self._callers_sub_session(manager, user_id, parent_session_id, instance_id)

            # Extract info
            messages = sub_session_data.get("messages", [])
            total = len(messages)
            agent_type = sub_session_data.get("agent_name")

            # Get metadata from parent
            parent_data = await session_manager.load_session(user_id, parent_session_id)
            metadata = parent_data.get("metadata", {}).get("sub_agents", {}).get(instance_id, {})

            window_messages, window_meta = self._paginate_messages(messages, params)

            if status:
                await status.end(
                    f"Retrieved info for {instance_id} (type: {agent_type}, "
                    f"showing {window_meta['returned']} of {total} messages)"
                )

            return {
                "instance_id": instance_id,
                "agent_type": agent_type,
                # what it is doing, as `list` says it -- stored, a running and an idle one are both "active"
                "status": await self._shown_status({**metadata, "instance_id": instance_id}, lambda: user_id)
                if metadata else "unknown",
                "created_at": metadata.get("created_at"),
                "last_used": metadata.get("last_used"),
                "message_count": total,
                "task_summary": metadata.get("task_summary"),
                "messages": window_messages,
                "window": window_meta,
            }

        except Exception as e:
            _log_failure("get_sub_agent_info", e)
            if status:
                await status.error(f"Failed to get sub-agent info: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    def _paginate_messages(
        self, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Slice *messages* per the caller's limit/offset/max_chars, formatted for an LLM.

        Two modes, chosen by whether 'offset' was given:
        - tail (default, no 'offset'): the most recent `limit` messages --
          what a coordinator almost always wants right after a run.
        - paging ('offset' >= 0): an absolute window from the start, so a
          caller can walk the ENTIRE transcript in order, `limit` at a time.
        """
        total = len(messages)

        limit = params.get("limit")
        if not isinstance(limit, int) or limit <= 0:
            limit = self.info_default_limit
        limit = min(limit, self.info_max_limit)

        offset = params.get("offset")
        if isinstance(offset, int) and offset >= 0:
            mode = "offset"
            start = min(offset, total)
        else:
            mode = "tail"
            start = max(0, total - limit)

        max_chars = params.get("max_chars")
        if not isinstance(max_chars, int):
            max_chars = self.info_default_max_chars

        window = messages[start:start + limit]
        formatted = [
            self._format_message(start + i, msg, max_chars)
            for i, msg in enumerate(window)
        ]

        return formatted, {
            "mode": mode,
            "start_index": start,
            "returned": len(formatted),
            "total": total,
            "has_more_before": start > 0,
            "has_more_after": (start + len(formatted)) < total,
        }

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        """Truncate *text* to *max_chars*, saying so. `max_chars <= 0` means unlimited."""
        if max_chars > 0 and len(text) > max_chars:
            omitted = len(text) - max_chars
            return (
                f"{text[:max_chars]}\n"
                f"... [{omitted} more chars omitted -- raise 'max_chars' to see the rest]"
            )
        return text

    @classmethod
    def _format_message(cls, index: int, msg: dict[str, Any], max_chars: int) -> dict[str, Any]:
        """One transcript entry, readable on its own: text, and what a tool did.

        Assistant messages that only call a tool have empty `content` -- the
        tool name and arguments are the actually useful part, so they are
        surfaced explicitly rather than left for the caller to notice is
        missing. Multimodal content (text_file attachments, etc.) is
        flattened with the same helper token-counting uses elsewhere, so an
        image block does not come back as a raw Python repr.
        """
        entry: dict[str, Any] = {
            "index": index,
            "role": msg.get("role"),
            "content": cls._clip_text(extract_text_from_content(msg.get("content")), max_chars),
        }

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            entry["tool_calls"] = [
                {
                    "name": (tc.get("function") or {}).get("name"),
                    "arguments": cls._clip_text(
                        str((tc.get("function") or {}).get("arguments", "")), max_chars
                    ),
                }
                for tc in tool_calls
                if isinstance(tc, dict)
            ]

        if msg.get("role") == "tool" and msg.get("name"):
            entry["tool_name"] = msg["name"]

        return entry

    # ========== Async Job Management Handlers ==========

    async def _stored_result(self, manager: SubAgentManager, user_id: str,
                             instance_id: str) -> Optional[str]:
        """The answer a finished run left behind, read from its own transcript.

        The job in memory holds that text only until somebody reads it, and there are three ways
        to arrive here without one: after a restart, after an archiving, and after this process
        handed the job to whoever polled first. Saying "execution completed (session persisted)"
        in those cases is not an answer -- a model reads it AS the sub-agent's answer, and the
        real one sits one `info` call away that nobody knows to make.
        """
        try:
            data = await manager._session_service.session_manager.load_session(user_id, instance_id)
        except Exception as e:
            # Every reason to fail here ends the same way: the caller keeps the answer it would
            # have had without this, so a missing transcript must not turn a poll into an error.
            logger.debug("No stored result for %s: %s", instance_id, e)
            return None

        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return None

        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            # The LAST thing the run said, and only that. Looking further up when it says
            # nothing reads an earlier step as the answer: measured in this repo's own sessions,
            # a run that ended inside a tool call then hands its mid-run self-check to the
            # coordinator as the sub-agent's result. A run cut off there has no answer.
            if message.get("tool_calls"):
                # It asked for a tool and never got to say anything after it. What stands next to
                # the call is what a model narrates BEFORE working ("Let me look at the config
                # first"), and that sentence read as a result is worse than no result: the caller
                # takes it for the answer instead of reading the transcript.
                return None
            content = message.get("content")
            if isinstance(content, str):
                return content.strip() or None
            if isinstance(content, list):
                # Parts: a raw session file gives dicts, pydantic gives ContentItem objects.
                return "".join((part.get("text") if isinstance(part, dict)
                                else getattr(part, "text", None)) or "" for part in content).strip() or None
            return None
        return None

    async def _archive_job(self, instance_id: str) -> None:
        """What the caller's own `delete` does to the instance's background job: the caller is
        awake and done with it. A finished job goes -- nobody comes back for its entry. A running
        one keeps it, for its task handle is what a cancel needs, and is marked as called off by
        the caller, the same as a cancel of its own: its ending rings nobody, and goes once stored.

        An archiving that makes room at a limit (`auto_archive_on_limit`) changes nothing about the
        job. It happens behind the caller's back, which may be asleep over that very ending: it
        dropped a finished job's entry, which could then no longer say the ending is unread, and
        the ringing stopped; and a running one's at its ending, ringing without a guard. A poll
        finds an archived instance still, and a woken caller takes the entry (`_wake_parent`).
        """
        async with self._async_jobs_lock:
            job = self._async_jobs.get(instance_id)
            if job is None:
                return
            if job.get("status") in ("completed", "failed", "cancelled"):
                del self._async_jobs[instance_id]
            else:
                job["_ended_by_caller"] = True

    @staticmethod
    async def _limit_reached(error: SubAgentLimitReached, status: Any, **fields: Any) -> dict[str, Any]:
        """The answer when a limit leaves no room, to a create or a continue: the reason without the
        ids behind it, under a status the caller counts as a failure (agent_caller)."""
        message = str(error).split("Active sub-agents:")[0].strip()
        logger.info(f"Sub-agent limit reached: {message}")
        if status:
            await status.error(message)
        return {**fields, "status": "limit_reached", "error": message}

    async def _store_blocking_abort(
        self, manager: SubAgentManager, parent_session_id: str, instance_id: str, error: BaseException
    ) -> None:
        """A blocking run that ended by an exception stores how, as a background one does: failed,
        or cancelled when the tool call itself was. Left "active" it read as idle -- done, the
        answer ready. Best effort: the run's own error is the one to report."""
        ending = ({"status": "cancelled"} if isinstance(error, asyncio.CancelledError)
                  else {"status": "failed", "error": str(error)})
        try:
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id,
                sub_session_id=instance_id,
                completed_at=datetime.now(UTC).isoformat(),
                **ending,
            )
        except Exception as persist_error:
            logger.warning(f"Failed to persist {ending['status']} status for {instance_id}: {persist_error}")

    async def _finish_job(
        self,
        instance_id: str,
        params: dict[str, Any],
        job_status: str,
        *,
        stored: dict[str, Any],
        manager: Optional[SubAgentManager] = None,
        drop_task: bool = False,
        **fields: Any,
    ) -> Optional[Callable[[], Awaitable[None]]]:
        """How a background job ends -- one place, whichever of the three ways led here: the run
        came back, the task was cancelled from outside, or it raised.

        It records the ending and hands back the bell for the session that asked to be woken, or
        None: rung by the caller once the ending is recorded, outside whatever turns a cancel into
        an ending (`_execute_async_job`). Rung in here, a cancel while it rang -- a shutdown takes
        minutes of it while the caller's session is held -- was recorded as a second ending:
        "cancelled" stored over a finished job, and rung again.

        Each has to leave the same two traces, because two readers ask different sources. A poll
        while this process lives reads the job in memory; a poll after a restart reads the stored
        metadata of the sub-session, and a status that stays "active" there sends a coordinator
        into a polling loop over a job that nobody runs any more.

        The job is kept, marked `_awaiting_poll`, until a poll has read its ending, or the caller
        was woken into a process of its own (`_wake_parent`) -- dropping it here would send a
        `wait` running meanwhile to the stored state, which may still say active. One the caller
        called off itself goes once that state is written: nobody reads it later.
        `stored` is what the sub-session's metadata records; the ways differ in that, so each
        caller says it.
        """
        # Memory first, and before anything is awaited: this also runs while the task is being
        # cancelled, where an await can be cut short. A job left saying "running" in memory is
        # answered "running" for good -- the polling loop this method exists to end.
        # The cost is a window: between here and the stored write below, a poll reads the ending
        # and takes the job away, and a second one in the same window falls back to metadata that
        # still says active and reads as completed. It closes itself when the write lands.
        ended_by_caller = False
        try:
            async with self._async_jobs_lock:
                job = self._async_jobs.get(instance_id)
                if job is not None:
                    # Said by the caller's own `cancel` or `delete`, under THIS lock, before the task
                    # unwinds into here. Read as a mark and not inferred from the status: a status is
                    # terminal here too when an earlier call of this method was cut short by a cancel
                    # from elsewhere -- and that caller is asleep, and waits for its bell.
                    ended_by_caller = bool(job.get("_ended_by_caller"))
                if job is not None:
                    job.update(status=job_status, completed_at=datetime.now(UTC).isoformat(),
                               _awaiting_poll=True, _ended_at=time.monotonic(), **fields)
                    if drop_task:
                        # a task ended by CancelledError keeps it, and with it every frame of the run
                        job["task_handle"] = None
                self._drop_expired_endings()

            parent_session_id = params.get("_session_id")
            # Whoever reads this ending may run in another process -- a caller woken into a run of its
            # own -- where this entry says nothing. So the stored ending says it is unread, until a
            # reader of the stored state hands it over (`_hand_over`); the bell asks that too.
            rings = bool(params.get("wake_when_done") and parent_session_id and not ended_by_caller
                         and job is not None)
            written = False
            # A run still going that holds the instance owns what the sub-session's metadata says:
            # a continue that took the slot before this job's run began, which then fails as
            # already running. Its "failed" stored over that run, and `list` dropped the running one.
            holder = self._slot_holder(instance_id)
            superseded = holder is not None and holder is not asyncio.current_task()
            try:
                if parent_session_id and not superseded:
                    if manager is None:
                        registry = self._extract_registry(params)
                        manager = self._get_manager(self._extract_session_service(params), registry)
                    written = await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=instance_id,
                        **stored,
                        **({"ending_unread": True} if rings else {}),
                    ) is not False
            except Exception as persist_error:
                logger.warning(f"Failed to persist {job_status} status for {instance_id}: {persist_error}")

            if job is not None and written:
                # A poll can answer from the stored state now: the retention may let the entry go.
                job["_stored"] = True

            if ended_by_caller and job is not None and written:
                # Called off by the caller, awake: it knows how this ended and is not coming back
                # to read it, so an entry held for that read would stay for the life of the process.
                # Dropped only once the ending is stored -- a poll or wait meanwhile finds it there,
                # not a status that still says active; unstored (failed, or left to the run going after
                # this one), the entry stays.
                async with self._async_jobs_lock:
                    self._async_jobs.pop(instance_id, None)

            logger.info(f"Async execution finished for {instance_id} ({job_status})"
                        + ("" if instance_id not in self._async_jobs else ", awaiting result poll"))
        finally:
            # The RUN is over here, on every path that reaches this method, so the slot it held
            # goes now -- before the bell, not after it: the ringing that follows (in
            # `_execute_async_job`, once this has returned it) can last minutes, and
            # held that long the instance answers "already running" to the `continue` the woken
            # caller makes, and `list` reports a run that ended long ago. Here and nowhere else:
            # released again after the bell, it took the slot of the run that continue started.
            # And `_release_slot` leaves a slot another run holds alone.
            # And in a finally: an ending cut short (a second cancel) kept the slot for good.
            async with self._running_lock:
                self._release_slot(instance_id)

        # Whatever the ending was: the session that asked to be woken is waiting for this one,
        # and a job that failed leaves it waiting just as a job that finished does.
        #
        # Unless the caller is demonstrably not waiting, and the job entry as it stood when the
        # task unwound says so in two ways:
        #   * the caller CALLED IT OFF -- a `cancel` or a `delete` of its own marked it, awake, in
        #     a turn of its own;
        #   * it is GONE -- a poll, a wait, a `continue` or a delete of a finished job took it, and
        #     each of those is a caller awake and handling the ending itself.
        # Ringing anyway is not free even once: the marker a ring leaves behind turns into a whole
        # woken run when the caller's turn ends.
        if rings and parent_session_id and job is not None:  # `rings` says job is not None; mypy does not see it
            return functools.partial(_ringing_over, job, functools.partial(
                self._wake_parent, instance_id, parent_session_id, manager, params, stored=written))
        return None

    def _drop_expired_endings(self) -> None:
        """Let go of the endings nobody took: a stored one after FINISHED_JOB_RETENTION_SECONDS,
        one that could not be stored after UNSTORED_JOB_RETENTION_SECONDS. Called with
        `_async_jobs_lock` held whenever a job ends -- the only thing that leaves an ending here,
        so what is kept is bounded by the jobs of the retention.

        Never one whose bell is still ringing: the ringing stops once the entry is gone
        (`_ending_is_unread`), and the caller would sleep over its job.
        """
        now = time.monotonic()
        expired = []
        for instance_id, job in self._async_jobs.items():
            if job.get("status") not in ("completed", "failed", "cancelled") or job.get("_ringing"):
                continue
            # An ending that did not come through `_finish_job` -- a cancel whose task never
            # started -- has no time of its own: it ages from the first time it is seen here.
            ended_at = job.setdefault("_ended_at", now)
            keep = FINISHED_JOB_RETENTION_SECONDS if job.get("_stored") else UNSTORED_JOB_RETENTION_SECONDS
            if now - ended_at > keep:
                expired.append(instance_id)
        for instance_id in expired:
            del self._async_jobs[instance_id]
        if expired:
            logger.debug(f"Dropped {len(expired)} background job ending(s) nobody took: {expired}")

    async def _async_started_message(self, params: dict[str, Any], parent_session_id: str,
                                     manager: Optional[SubAgentManager]) -> str:
        """What `create(blocking=false)` tells the model about the job it just started.

        The tool description promises the wake without conditions, because it describes the
        argument and not this session. What `wake_blocked` can answer up front -- presence
        switched off, no session behind the call, a wake chain already at `max_wake_depth` --
        a caller told nothing about ends its turn over and waits for good. So the answer says
        which of the two it is, with the reason, because the reason is what a model can act on.

        `wake_blocked` names two exits it cannot check up front. One is checked here all the same
        (`_never_woken`): a sub-agent's own session, which is never woken. Told it may sleep, a
        sub-agent that starts a job ended its turn over it, handing its caller an answer that was
        only "I am waiting", and the job's result reached nobody. The other stays open: whether
        the process holding the job outlives the turn. It shows only at the ending, as a wake that
        did not happen, so being woken stays the good case and polling the fallback.
        """
        started = "Sub-agent execution started in background."
        if not params.get("wake_when_done"):
            return f"{started} Use poll or wait to check status."
        try:
            # `manager` is never None here: this runs on the create path, which built one.
            user_id = manager._extract_user_id(parent_session_id, params)
            blocked = (wake_blocked(self.system_config, parent_session_id, user_id)
                       or await self._never_woken(manager, user_id, parent_session_id))
        except Exception as e:
            # The job is running. A create must not fail over the wording of its own answer, and
            # the neutral sentence is the one that was there before any of this.
            logger.warning("Could not tell whether %s can be woken: %s", parent_session_id, e)
            return f"{started} Use poll or wait to check status."
        if blocked:
            return (f"{started} This session will NOT be woken ({blocked}) -- do not end your "
                    f"turn over it: poll or wait for the result yourself.")
        return (f"{started} You may end your turn: this session is woken when it finishes, "
                f"however it ends except a cancel or delete of your own, and you poll the "
                f"instance then.")

    @staticmethod
    async def _never_woken(manager: SubAgentManager, user_id: str, session_id: str) -> str:
        """Why a session that could be woken is not: it is a sub-agent's own -- the run that
        spawned it takes its answer, and the core wakes none (`notify`, by the same link
        `session_presence._stored_session` reads). "" otherwise.

        The core leaves this out of `wake_blocked` because it would parse the session file on the
        caller's loop at every armed wake. A create has just read and written this very session,
        so here it is the session manager's cache, or one read off the loop.

        And a throwaway session (a stateless call: openai_api, a headless run) is never woken
        either (`_wake_parent`): nobody continues it, so a woken run would answer nobody.
        """
        if is_ephemeral_session(session_id):
            return ("this call runs on a throwaway session that nobody continues, so a woken run "
                    "would answer nobody")
        session = await manager._session_service.session_manager.load_session(user_id, session_id)
        if session.get("parent_session"):
            return ("this is a sub-agent's own session, and those are never woken: ending your "
                    "turn hands your answer to the run that spawned you")
        return ""

    def _ending_is_unread(self, instance_id: str) -> bool:
        """Whether this job's ending is still waiting for somebody to read it.

        The ending sits in the job marked `_awaiting_poll` until somebody takes it, and whoever
        takes it takes the job with it -- a poll, a wait, a `continue` on the instance, or the
        caller's own `delete` of it. So the answer is "is it still there", not "did a poll
        happen": the three others each mean the caller is awake and handling it itself.

        The id is enough to ask with, and nothing here needs to hold the entry itself: an
        instance_id is minted once per spawn and checked against every session on disk
        (`_generate_instance_id`), so no second job is ever filed under one.

        Asked between rings to stop them: a ring that goes out after the woken run has already
        read the result starts a SECOND run, which is a whole turn on the user's money.

        Read without the lock on purpose. This is asked on the loop between rings, a dict lookup
        is atomic per step, and taking the lock would make the ringing wait on whatever else the
        manager is doing.
        """
        job = self._async_jobs.get(instance_id)
        return bool(job and job.get("_awaiting_poll"))

    async def _ending_still_unread(self, instance_id: str, manager: Optional[SubAgentManager],
                                   parent_session_id: str, user_id: str, stored: bool) -> bool:
        """`_ending_is_unread`, and for an ending stored unread, the stored state too -- the guard
        the bell hands the core.

        A caller woken into a run of its own reads the ending from the stored state, where this
        process's entry says nothing. The ringing went on over a read ending, up to its budget,
        and the marker its last ring left woke a second, paid run once that caller's turn let go.
        Its reading clears the stored mark (`_hand_over`), and then the entry here goes too:
        nobody reads it any more. An ending that could not be stored has only the entry.

        A load that fails raises: the core logs it and rings on, which costs at most a woken run.
        """
        if not self._ending_is_unread(instance_id):
            return False
        if not stored or manager is None:
            return True
        # From the file: a hand-over by another process that lands within one mtime tick of this
        # process's last read or write of the parent is invisible to the cache.
        parent = await manager._session_service.session_manager.load_session(
            user_id, parent_session_id, bypass_cache=True)
        entry = ((parent.get("metadata") or {}).get("sub_agents") or {}).get(instance_id) or {}
        # Handed over is an explicit None. A mark that is not there at all was lost -- a whole-file
        # save of the parent by another process can eat the write that stored it -- and stopping
        # then loses the news; ringing on costs at most a woken run.
        if entry.get("ending_unread", True):
            return True
        async with self._async_jobs_lock:
            job = self._async_jobs.get(instance_id)
            if job is not None and job.get("_awaiting_poll"):
                del self._async_jobs[instance_id]
        return False

    @staticmethod
    async def _hand_over(manager: SubAgentManager, parent_session_id: str, instance_id: str) -> None:
        """Clear the stored "unread" of an ending just handed to its caller from the stored state --
        the mark the bell of the process that ran the job asks for (`_ending_still_unread`)."""
        try:
            await manager.update_sub_session_metadata(
                parent_session_id=parent_session_id, sub_session_id=instance_id, ending_unread=None)
        except Exception as error:  # the answer is given; the bell rings on, which costs a woken run
            logger.debug(f"Could not mark the ending of {instance_id} read: {error}")

    async def _wake_parent(self, instance_id: str, parent_session_id: str,
                           manager: Optional[SubAgentManager], params: dict[str, Any],
                           *, stored: bool = False) -> None:
        """Tell the session that started this job to look: it may have ended its turn over it.

        Waking is core (`core/session_presence.wake_session`), and so is the REPEATING, which is
        the part this plugin got wrong on its own. A session that is still held when the job ends
        is only handed a marker, and the next LLM step of that same turn takes the marker in the
        belief that a pre-LLM hook passes the waiting input on. Nothing passes on "your sub-agent
        is done", so a single ring that lands inside the caller's own turn is thrown away and the
        caller sleeps over a finished job. How narrow that is: in the session it was reported
        from, the turn ended 4.3 seconds before its job did -- three times in a row it came in
        just behind the turn, and one more poll would have put it inside.

        `still_needed` says the ending is still in hand. It answers "no" once a poll or a wait has
        handed it over -- and also once the job is simply GONE, which is what the core's own
        wording covers ("read by its caller, or gone from the registry"): a `continue` on the
        instance takes it too, and so does the caller's own `delete`. In each of those the caller
        is demonstrably awake and working, which is when the ringing should stop.

        The guard is `_ending_still_unread`; whether the ending was stored (`stored`) is decided by
        `_finish_job`, the one place that has the job entry as the ending left it.

        A background job lives in the process that started it. That is the API, where the job runs
        on after the turn that asked for it, and `agent-cli chat`, whose prompt waits on the same
        loop. A one-shot `agent-cli run` that ends its turn takes the job with it, and nothing is
        left to wake anybody. README says so.

        A throwaway session is not woken: nobody continues it, and a woken run of its parent
        record (agent-cli, on the `Coordinator Session` this manager wrote) would be an agent's
        turn that answers nobody -- or, the record already deleted with its turn, a wake that
        finds no session.
        """
        if is_ephemeral_session(parent_session_id):
            logger.debug("Not waking %s for sub-agent %s: a throwaway session, nobody continues it",
                         parent_session_id, instance_id)
            return
        try:
            # Everything from here on belongs to the wake, reading the config included: a job that
            # is over and recorded must not end in its caller's exception handler, which would
            # record it a second time, as failed.
            #
            # Without a manager the job did not even get started -- and a caller that went to
            # sleep over it waits forever unless the id it injected answers instead.
            user_id = (manager._extract_user_id(parent_session_id, params) if manager is not None
                       else str(params.get("_user_id") or ""))
            if not user_id:
                logger.warning("Cannot wake %s for sub-agent %s: no user for the session",
                               parent_session_id, instance_id)
                return
            state = await wake_session(
                self.system_config, parent_session_id, user_id,
                what=f"sub-agent {instance_id}",
                still_needed=functools.partial(
                    self._ending_still_unread, instance_id, manager, parent_session_id, user_id, stored),
                # The run that asked for the job, by the id of its create call. Asked instead, the
                # core reads the ringing task's current request -- a job still carries its sub-agent's
                # there, and a stopped sub-agent read as a stopped caller that is never woken.
                started_by=str(params.get("_request_id") or ""),
            )
            if stored and state in ("woke_session", "being_woken"):
                # Woken into a process of its own (`notify` starts an agent-cli run): that run reads
                # the ending from the stored state, and nothing here reads this entry any more. Held
                # on, it kept the result for the life of this process, one per woken job. Only a
                # stored ending: unstored, the entry is the only answer there is.
                async with self._async_jobs_lock:
                    job = self._async_jobs.get(instance_id)
                    if job is not None and job.get("status") in ("completed", "failed", "cancelled"):
                        del self._async_jobs[instance_id]
        except Exception as e:
            # The job is done and recorded; a wake that fails costs the caller a poll, not the run.
            logger.warning("Could not wake %s for finished sub-agent %s: %s",
                           parent_session_id, instance_id, e)

    async def _execute_async_job(
        self,
        instance_id: str,
        params: dict[str, Any],
        agent_name: str,
        task: str,
        use_advanced_model: bool
    ) -> None:
        """Execute sub-agent in background and update job status.
        
        Args:
            instance_id: The sub_session_id (already created by caller)
            params: Full params dict with injected dependencies
            agent_name: Agent type name
            task: Task description
            use_advanced_model: Whether to use advanced model
        """
        try:
            # Get dependencies
            registry = self._extract_registry(params)
            session_service = self._extract_session_service(params)
            manager = self._get_manager(session_service, registry)
            parent_session_id = params["_session_id"]

            logger.info(f"Async execution started for {instance_id}")

            # Mark as running (prevent concurrent execution)
            async with self._running_lock:
                if instance_id in self._running_agents:
                    raise ValueError(f"Sub-agent '{instance_id}' is already running")
                self._take_slot(instance_id)

            # Get agent and execute
            agent = registry.get(agent_name)
            if not agent:
                raise ValueError(f"Agent '{agent_name}' not found in registry")

            user_id = await self._prepare_agent(
                agent, manager, session_service, params,
                parent_session_id=parent_session_id, instance_id=instance_id,
                agent_name=agent_name,
            )

            # Execute (collect final result)
            parent_request_id = params.get("_request_id")
            sub_request_id = f"{parent_request_id}_async_{short_id(6)}" if parent_request_id else f"async_{short_id()}"
            if instance_id in self._async_jobs:  # cancel reaches the job's tool calls and sub-agents by it
                self._async_jobs[instance_id]["request_id"] = sub_request_id

            # Register sub-request user mapping for admin dashboard
            _register_request_user(sub_request_id, user_id)

            result_text = await self._consume_run(
                agent, manager,
                parent_session_id=parent_session_id, instance_id=instance_id,
                task=task, request_id=sub_request_id,
                use_advanced_model=use_advanced_model,
            )

            # Save session
            llm_profile = agent.agent_config.default_llm_profile
            await session_service.save_session(
                agent=agent,
                user_id=user_id,
                session_id=instance_id,
                agent_name=agent_name,
                llm_profile=llm_profile,
                was_new_session=True,
                after_run=True,
            )

            # A run whose ANSWER is "Error: ..."/"Cancelled: ..." did not
            # complete -- it aborted and handed the transport's complaint
            # back as content. Only an exception marked such a job failed
            # until now, so `wait_all` counted it under "Completed: N,
            # Failed: 0" and a fan-out of dead reviewers read as a clean
            # one. The job status uses the vocabulary it already has.
            outcome = _outcome_status(result_text)
            job_status = _ABORTED_AS.get(outcome, "completed")

            # Update metadata: a finished-but-aborted run is not active any
            # more, the same way the two paths below record it.
            bell = await self._finish_job(
                instance_id, params, job_status, manager=manager,
                stored={"last_used": datetime.now(UTC).isoformat(),
                        "status": "active" if job_status == "completed" else job_status,
                        # what a reader of the stored state gets -- a woken caller in its own
                        # process; it answered "Sub-agent failed" without it. Cut, for it goes into
                        # the parent's session file: the whole answer is in the transcript (`info`).
                        **({} if job_status == "completed" else {"error": result_text[:2000]})},
                outcome=outcome, result=result_text,
            )

        except asyncio.CancelledError:
            # Job was cancelled (e.g., parent agent interrupted). Persisting it is what keeps a
            # coordinator from polling a job nobody runs any more after a restart.
            bell = await self._finish_job(
                instance_id, params, "cancelled", drop_task=True,
                stored={"status": "cancelled", "completed_at": datetime.now(UTC).isoformat()},
            )
            if bell is not None:
                await bell()
            raise

        except Exception as e:
            logger.exception(f"Async execution failed for {instance_id}: {e}")
            bell = await self._finish_job(
                instance_id, params, "failed", error=str(e),
                stored={"status": "failed", "completed_at": datetime.now(UTC).isoformat(),
                        "error": str(e)},
            )

        # Out here, once the ending is recorded: a cancel while this rings is no ending of the job.
        if bell is not None:
            await bell()

    async def _handle_poll(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'poll' - check status of running sub-agent without blocking."""
        status = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status:
                await status.error("Poll: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_id = params.get("instance_id")
            if not instance_id:
                if status:
                    await status.error("Poll: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}

            # First check if async execution is tracked
            async with self._async_jobs_lock:
                if instance_id in self._async_jobs:
                    job = self._async_jobs[instance_id].copy()
                    # Ownership check: _async_jobs is a process-wide singleton dict
                    # keyed by guessable instance_id. Without this guard any
                    # session could poll another user's in-flight sub-agent and
                    # read its full result. Mirrors the DB-path check at the
                    # parent_link comparison used by continue/info/delete.
                    caller_session = params.get("_session_id")
                    job_parent = job.get("parent_session_id")
                    if job_parent and job_parent != caller_session:
                        if status:
                            await status.error(f"Poll: {instance_id} not found")
                        return {"status": "error", "error": "Instance not found"}
                    job_status = job.get("status")

                    # If job is completed/failed/cancelled, remove from memory after returning
                    # Subsequent polls will load from DB (which is fine)
                    if job_status in ("completed", "failed", "cancelled"):
                        self._async_jobs.pop(instance_id, None)
                        logger.debug(f"Removed completed job {instance_id} from memory after poll")

                    job = _job_answer(job)
                    if status:
                        # A failed or cancelled job is not an END: that phase
                        # renders as a completed row.
                        if job_status in ("failed", "cancelled"):
                            await status.error(f"Poll: {instance_id} {job_status}")
                        else:
                            await status.end(f"Poll: {instance_id} status={job_status}")
                    return job

            # Not in async jobs - check if sub-agent exists in DB (may be completed or never ran async)
            # Only check DB if we have agent context (registry + session)
            try:
                registry = self._extract_registry(params)
                session_service = self._extract_session_service(params)
                manager = self._get_manager(session_service, registry)
                parent_session_id = params.get("_session_id")
                
                if not parent_session_id:
                    if status:
                        await status.error(f"Poll: {instance_id} not found (no session context)")
                    return {"status": "error", "error": "Instance not found in async tracking and no session context available"}

                # Check if sub-agent exists (only from this manager instance)
                sub_agents = await manager.list_sub_sessions(
                    parent_session_id, 
                    include_completed=False,
                    creator_plugin=self.name  # Only check sub-agents created by THIS instance
                )
                # Support both dict and Pydantic object access
                matching = [s for s in sub_agents if (s.get("instance_id") if isinstance(s, dict) else s.instance_id) == instance_id]
                was_archived = False

                if not matching:
                    # An archived instance drops out of that listing -- `list_sub_sessions` keeps
                    # active and interrupted ones. It is still a run of ours with an answer, and
                    # the caller may not even know it was archived: making room at a limit happens
                    # behind its back, to the oldest instance, running or not.
                    archived = [
                        s for s in await manager.list_sub_sessions(
                            parent_session_id, include_completed=True, creator_plugin=self.name)
                        if (s.get("instance_id") if isinstance(s, dict) else s.instance_id) == instance_id
                        and (s.get("status") if isinstance(s, dict) else s.status) == "archived"
                    ]
                    matching = archived
                    was_archived = bool(archived)

                if matching:
                    # Sub-agent exists but not in async tracking - it's completed, UNLESS it is
                    # still running: there are two ways to be running with no job here -- a
                    # blocking run never has one, and a job lives in the process that started it,
                    # while a coordinator is woken into a process of its own. Answering
                    # "completed" then hands the caller a transcript that is still being written
                    # as the result, and ends a wait on an answer that is not there yet.
                    #
                    # Both questions `list` asks before it calls a sub-agent dead: this process's
                    # own runs, and the lock a run holds next to its session, which answers the
                    # same in every process. The second alone would miss a blocking run of this
                    # one wherever `session_presence` is off.
                    sub_agent = matching[0]
                    user_id = manager._extract_user_id(parent_session_id, params)
                    if self.is_agent_running(instance_id) \
                            or await self._runs_in_another_process(instance_id, user_id):
                        if status:
                            await status.end(f"Poll: {instance_id} running")
                        return {
                            "instance_id": instance_id,
                            "status": "running",
                            "agent_type": (sub_agent.get("agent_type") if isinstance(sub_agent, dict)
                                           else sub_agent.agent_type),
                            # not "in another process": the same branch answers for a blocking run
                            # of this one, which never has a job here either
                            "message": ("Archived while it runs; its ending is still to come"
                                        if was_archived else
                                        "Still running; its ending is still to come"),
                        }
                    stored = sub_agent if isinstance(sub_agent, dict) else {}
                    if stored.get("status") == "interrupted" or _left_mid_run(stored):
                        # `_shown_status`'s rule: an activity nobody holds was left by a process
                        # that died mid-run. Answered "completed", the caller took the transcript
                        # of a run nobody finished for its answer, while `list` called the same
                        # instance interrupted.
                        if status:
                            await status.error(f"Poll: {instance_id} interrupted")
                        return {
                            "instance_id": instance_id,
                            "status": "interrupted",
                            "agent_type": stored.get("agent_type"),
                            "error": ("Its run was cut off: the process running it ended before the "
                                      "run did. Read how far it got with 'info', or 'continue' it."),
                        }
                    if stored.get("ending_unread"):
                        await self._hand_over(manager, parent_session_id, instance_id)
                    result = await self._stored_result(manager, user_id, instance_id)
                    if status:
                        await status.end(f"Poll: {instance_id} completed")
                    return {
                        "instance_id": instance_id,
                        "status": "completed",
                        "agent_type": sub_agent.get("agent_type") if isinstance(sub_agent, dict) else sub_agent.agent_type,
                        "started_at": sub_agent.get("created_at") if isinstance(sub_agent, dict) else (sub_agent.created_at.isoformat() if sub_agent.created_at else None),
                        "completed_at": sub_agent.get("last_used") if isinstance(sub_agent, dict) else sub_agent.last_used,
                        "result": result or "Sub-agent execution completed (session persisted)",
                        "message": "Use 'info' operation to see conversation history"
                    }

                # Not among the active ones -- but a run that ABORTED persisted
                # a terminal status, and list_sub_sessions keeps only
                # active/interrupted. Without this second look such a poll
                # answers "not found", which reads like an instance that never
                # existed. Everything else keeps its old answer: the lookup
                # widens only for the two statuses that mean "it ran and
                # failed".
                aborted = [
                    s for s in await manager.list_sub_sessions(
                        parent_session_id, include_completed=True,
                        creator_plugin=self.name)
                    if (s.get("instance_id") if isinstance(s, dict) else s.instance_id) == instance_id
                    and (s.get("status") if isinstance(s, dict) else s.status) in ("failed", "cancelled")
                ]
                if aborted:
                    sub_agent = aborted[0]
                    job_status = sub_agent.get("status") if isinstance(sub_agent, dict) else sub_agent.status
                    if isinstance(sub_agent, dict) and sub_agent.get("ending_unread"):
                        await self._hand_over(manager, parent_session_id, instance_id)
                    if status:
                        await status.error(f"Poll: {instance_id} {job_status}")
                    return {
                        "instance_id": instance_id,
                        "status": job_status,
                        "agent_type": sub_agent.get("agent_type") if isinstance(sub_agent, dict) else sub_agent.agent_type,
                        "error": (sub_agent.get("error") if isinstance(sub_agent, dict)
                                  else getattr(sub_agent, "error", None)) or f"Sub-agent {job_status}",
                        "message": "Use 'info' operation to see conversation history",
                    }

                if status:
                    await status.error(f"Poll: {instance_id} not found")
                return {"status": "error", "error": f"Instance '{instance_id}' not found"}
            except RuntimeError:
                # No registry available - can only check async tracking (already done above)
                if status:
                    await status.error(f"Poll: {instance_id} not found in async tracking")
                return {"status": "error", "error": f"Instance '{instance_id}' not found in async tracking"}

        except KeyError as e:
            # Missing required parameter
            logger.error(f"Missing parameter in poll: {e}")
            if status:
                await status.error(f"Poll: Missing parameter {e}")
            return {"status": "error", "error": f"Missing required parameter: {e}"}
        except Exception as e:
            logger.exception(f"Unexpected error in poll: {e}")
            if status:
                await status.error(f"Poll error: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'wait' - poll instance until completed or timeout."""
        status_ctx = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status_ctx:
                await status_ctx.error("Wait: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_id = params.get("instance_id")
            if not instance_id:
                if status_ctx:
                    await status_ctx.error("Wait: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}
            
            timeout = self.default_wait_timeout  # Use configured timeout only

            if status_ctx:
                await status_ctx.progress(f"Waiting for {instance_id}...")

            # Check if async execution is tracked
            async with self._async_jobs_lock:
                is_async = instance_id in self._async_jobs

            if not is_async:
                # Not in async jobs - check DB immediately (may already be completed)
                # Silent poll: sharing the scope let the inner poll close it,
                # so wait's own verdict below never reached the status stream.
                poll_result = await self._handle_poll(_without_status(params))
                if poll_result.get("status") == "completed":
                    if status_ctx:
                        # No result size here: on this branch `_handle_poll` reaches the stored
                        # path, whose `result` is the run's own words when its transcript had
                        # any and the fixed "…(session persisted)" sentence when it had none.
                        # A size would therefore measure the answer sometimes and a constant
                        # the rest of the time, and the two are not told apart here.
                        await status_ctx.end(
                            f"Instance {instance_id} already completed "
                            f"({poll_result.get('agent_type', 'unknown type')})")
                    return poll_result
                elif poll_result.get("status") == "error":
                    if status_ctx:
                        await status_ctx.error(f"Instance {instance_id} not found")
                    return poll_result

            # Async execution in progress - poll until done
            start_time = asyncio.get_event_loop().time()
            # Two turns of this loop cost very different things. With a job here it is a dict
            # lookup under a lock; without one it is the parent's session file, re-parsed whenever
            # a sub-agent wrote its activity since -- which a running one does all the time. Half a
            # second of THAT is four thousand reads over a forty-minute run, once per waiter of a
            # wait_all, so the turn without a job backs off instead.
            db_pause = 0.5

            while True:
                # Check status -- on a copy: _handle_poll below takes the same lock, and an asyncio
                # lock held across that call hung every job of this manager (a cancelled job is gone)
                async with self._async_jobs_lock:
                    job = self._async_jobs.get(instance_id)
                    job = None if job is None else job.copy()
                if job is None:
                    # Async tracking lost - check DB (silent, see above)
                    poll_result = await self._handle_poll(_without_status(params))
                    poll_status = poll_result.get("status")
                    if poll_status not in ["running", "pending"]:
                        # Every other answer a poll gives is an ending and is the poll's to word:
                        # "completed", the two aborted ones, and the error that says an instance
                        # is not there. Say it ourselves though -- the inner poll is silent here,
                        # and the scope default would drop the instance id.
                        if status_ctx and poll_status == "completed":
                            await status_ctx.end(f"Instance {instance_id} completed")
                        elif status_ctx and poll_status != "error":  # "error" the scope reports itself
                            await status_ctx.error(f"Instance {instance_id} {poll_status}")
                        return poll_result
                    # It runs, with no job of ours to watch: in the process that started it, or on
                    # past an archiving. Waiting is what was asked for, so the wait goes on and the
                    # timeout below is what ends it. This is where a wait used to answer
                    # "disappeared during wait" about a sub-agent that was working.
                    pause, db_pause = db_pause, min(db_pause * 2, WAIT_DB_POLL_MAX)
                else:
                    pause = 0.5
                    # Ownership check (see _handle_poll): the shared singleton
                    # _async_jobs is keyed by guessable instance_id; without this
                    # a session could wait on and read another user's sub-agent.
                    caller_session = params.get("_session_id")
                    job_parent = job.get("parent_session_id")
                    if job_parent and job_parent != caller_session:
                        if status_ctx:
                            await status_ctx.error(f"Instance {instance_id} not found")
                        return {"status": "error", "error": "Instance not found"}
                    job_status = job["status"]

                    if job_status in ["completed", "failed", "cancelled"]:
                        # The ending has been read, so the job may go -- the rule poll has always
                        # followed. Without it a job waited on but never polled kept its whole
                        # result text for the life of the process, and nothing else takes it out:
                        # only a continue of the same instance does, and the usual shape is
                        # create + wait_all + delete.
                        async with self._async_jobs_lock:
                            self._async_jobs.pop(instance_id, None)
                        job = _job_answer(job)

                        if status_ctx:
                            if job_status == "completed":
                                await status_ctx.end(f"Instance {instance_id} completed")
                            else:
                                await status_ctx.error(f"Instance {instance_id} {job_status}")

                        return job

                # Check timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    if status_ctx:
                        await status_ctx.error(f"Timeout waiting for {instance_id} ({elapsed:.1f}s)")
                    return {
                        "status": "error",
                        "error": f"Timeout waiting for instance '{instance_id}' (waited {elapsed:.1f}s)"
                    }

                # Wait before next poll
                await asyncio.sleep(pause)

        except Exception as e:
            logger.exception(f"Error in wait: {e}")
            if status_ctx:
                await status_ctx.error(f"Failed to wait for instance: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_wait_all(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'wait_all' - wait for multiple sub-agents to complete."""
        status_ctx = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status_ctx:
                await status_ctx.error("Wait_all: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_ids = params.get("instance_ids")
            if not instance_ids:
                if status_ctx:
                    await status_ctx.error("Wait_all: 'instance_ids' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_ids'"}
            if not isinstance(instance_ids, list):
                # No status.error here: the returned error IS the message, and
                # call_with_status publishes it for any error result the
                # handler did not report itself. A second call would only
                # duplicate the text (mutation-checked: removing it keeps the
                # test green, so it carried nothing).
                return {"status": "error",
                        "error": f"Wait_all: 'instance_ids' must be a list "
                                 f"(got {type(instance_ids).__name__})"}

            if status_ctx:
                await status_ctx.progress(f"Waiting for {len(instance_ids)} instances...")

            # Waiting twice for the same instance is waiting once, and the two waits would race
            # for its job: the loser finds it taken and answers from the stored state, without the
            # result. Deduplicated in order, as delete does it.
            instance_ids = list(dict.fromkeys(instance_ids))

            # Each wait gets the call's own params, minus the status scope this handler owns: a
            # rebuilt dict used to drop _user_id (so a lookup below fell back to scanning the
            # session directories), _request_id and the cancellation token.
            wait_tasks = [
                self._handle_wait({**_without_status(params), "instance_id": iid})
                for iid in instance_ids
            ]

            results = await asyncio.gather(*wait_tasks, return_exceptions=True)

            # Format results
            formatted_results: list[dict[str, Any]] = []
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    formatted_results.append({
                        "instance_id": instance_ids[i],
                        "status": "error",
                        "error": str(result)
                    })
                else:
                    # result is dict[str, Any] from wait_for_result
                    formatted_results.append(result)

            # Count statuses
            completed = sum(1 for r in formatted_results if r.get("status") == "completed")
            # a cancelled job did not complete either: counted nowhere, the totals read cleaner than the run was
            failed = sum(1 for r in formatted_results
                         if r.get("status") in ["failed", "error", "cancelled", "interrupted"])

            if status_ctx:
                await status_ctx.end(f"Completed: {completed}, Failed: {failed} of {len(instance_ids)} instances")

            return {
                "results": formatted_results,
                "total": len(instance_ids),
                "completed": completed,
                "failed": failed
            }

        except Exception as e:
            logger.exception(f"Error in wait_all: {e}")
            if status_ctx:
                await status_ctx.error(f"Failed to wait for instances: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle 'cancel' - stop a running sub-agent, a blocking run as well as an async job."""
        status_ctx = params.get("_status") if params else None
        
        # Validate params
        if not params:
            if status_ctx:
                await status_ctx.error("Cancel: params is None")
            return {"status": "error", "error": "Invalid parameters (None)"}
        
        try:
            instance_id = params.get("instance_id")
            if not instance_id:
                if status_ctx:
                    await status_ctx.error("Cancel: 'instance_id' is required")
                return {"status": "error", "error": "Missing required parameter: 'instance_id'"}

            # A blocking run first: a finished async job of the same instance can still wait for its poll
            run = self._blocking_runs.get(instance_id)
            if run is not None:
                # ownership, as for an async job below -- a caller without a session owns no sub-agent
                if run["parent_session_id"] != params.get("_session_id"):
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} not found")
                    return {"status": "error", "error": "Instance not found"}
                if run["request_id"] is None:
                    run["early"] = True  # still being prepared: the run stops itself at its first event
                elif not await run["agent"].cancel_request(run["request_id"]):
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} is already ending its run")
                    return {"status": "error", "error": f"Instance '{instance_id}' is already ending its run"}
                logger.info(f"Cancel requested for the blocking run of {instance_id} ({run['request_id']})")
                if status_ctx:
                    await status_ctx.end(f"Cancel requested for {instance_id}")
                # Requested, not done: the agent stops at its next check (a model not streaming finishes its
                # answer first). The run's caller learns the outcome; the run saves its session either way.
                return {"instance_id": instance_id, "status": "cancelling",
                        "message": "Cancel requested: the sub-agent stops at its next step; "
                                   "an answer it is already writing may still arrive"}

            async with self._async_jobs_lock:
                if instance_id not in self._async_jobs:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} not found or not running")
                    return {"status": "error", "error": f"Instance '{instance_id}' not found or not running"}

                job = self._async_jobs[instance_id]
                task_handle = job.get("task_handle")
                parent_session_id = job.get("parent_session_id")

                # Ownership check (see _handle_poll): cancel is a control/write
                # operation on the shared singleton _async_jobs - without this a
                # session could kill (DoS) another user's in-flight sub-agent.
                caller_session = params.get("_session_id")
                if parent_session_id and parent_session_id != caller_session:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} not found")
                    return {"status": "error", "error": "Instance not found"}

                if job["status"] not in ["pending", "running"]:
                    if status_ctx:
                        await status_ctx.error(f"Cancel: {instance_id} has status '{job['status']}'")
                    return {
                        "status": "error",
                        "error": f"Cannot cancel instance with status '{job['status']}'"
                    }

                if task_handle and not task_handle.done():
                    if job.get("request_id"):
                        # Its tool calls and sub-agents are tasks and requests of their own: cancelling the job's
                        # task alone leaves them running. Cancelled tokens stop them (and let the timeout monitor
                        # force what does not stop).
                        from agent_system.core.cancellation import get_cancellation_manager
                        get_cancellation_manager().cancel_request(job["request_id"])
                    task_handle.cancel()
                    logger.info(f"Cancelled async execution of {instance_id}")

                job["status"] = "cancelled"
                job["completed_at"] = datetime.now(UTC).isoformat()
                # the caller calling it off, awake: `_finish_job` rings nobody for this ending
                job["_ended_by_caller"] = True

            # CRITICAL: Update session metadata in database to persist cancelled status
            # This prevents polling loops when parent agent restarts
            if parent_session_id:
                try:
                    registry = self._extract_registry(params)
                    session_service = self._extract_session_service(params)
                    manager = self._get_manager(session_service, registry)
                    
                    await manager.update_sub_session_metadata(
                        parent_session_id=parent_session_id,
                        sub_session_id=instance_id,
                        status="cancelled",
                        completed_at=datetime.now(UTC).isoformat()
                    )
                    logger.info(f"Persisted cancelled status for {instance_id} in DB")
                except Exception as e:
                    logger.warning(f"Failed to persist cancelled status for {instance_id}: {e}")

            if status_ctx:
                await status_ctx.end(f"Cancelled {instance_id}")

            return {
                "instance_id": instance_id,
                "status": "cancelled",
                "message": "Sub-agent cancelled successfully"
            }

        except Exception as e:
            logger.exception(f"Error in cancel: {e}")
            if status_ctx:
                await status_ctx.error(f"Failed to cancel instance: {e}")
            return {"status": "error", "error": str(e)}

    # ========== End Async Job Management ==========

    def _is_agent_allowed(self, agent_name: str) -> bool:
        """Check if agent is allowed by this manager instance (ignoring phase filtering).

        Args:
            agent_name: Agent instance name (e.g., 'coding_agent', 'meta_agent')

        Returns:
            True if agent is allowed, False otherwise
        """
        allowed = agent_allowed(agent_name, self.allowed_agents, self.blocked_agents)
        if not allowed:
            logger.debug(f"Agent '{agent_name}' not allowed: allowed={self.allowed_agents} blocked={self.blocked_agents}")
        return allowed

    async def _runs_in_another_process(self, instance_id: str, user_id: str) -> bool:
        """Whether some other process has this sub-agent's session in hand, i.e. is running it.

        `is_agent_running` knows this process only. A coordinator is not tied to one: a run woken
        for a finished job continues the session in a process of its own, the API runs beside the
        writer's worker. Asking only ourselves, `list` calls a live sub-agent interrupted and
        writes that over its metadata -- the very thing the panel's read-only listing avoids
        (web_endpoints.py). A run holds the lock file next to its session for as long as it lasts
        (core/session_presence.py), and that is the same answer in every process.

        False while session presence is off: then there is no such answer, and `list` falls back
        to what it always did.
        """
        presence = presence_for(self.system_config)
        if presence is None:
            return False
        try:
            # The lock only: get() would parse the session file beside it too -- a sub-agent's
            # whole transcript, per sub-agent and per LLM call for the injected list. A probe does
            # not wait for anything, so it stays on the loop.
            state = presence.status(instance_id, user_id)
        except Exception as error:  # a listing must not fail over a lock file
            logger.debug(f"Session presence: could not read {instance_id}: {error}")
            return False
        return state in ("running", "waking")

    async def _shown_status(self, metadata: dict[str, Any], user_id: Callable[[], str],
                            *, ask_runs: bool = True) -> str:
        """running, idle, or how the last run ended -- a sub-agent's state as the model and the
        injected list name it.

        Stored, an open instance is "active" whether a run is under way or not: a clean ending
        stores it too, so the word says nothing a caller can act on. A run is what tells the two
        apart -- one of this process, or the lock a run holds beside its session in any other,
        from before its first activity to after its last. An activity with neither behind it was
        left by a process that died mid-run.

        The injected list asks this before every LLM call: the lock is a probe of one file, and
        `user_id` is found only when one is asked (it scans the session directories).

        `ask_runs` False answers from the entry alone: the panel's rule for an entry whose sub-session
        is not the viewer's (web_endpoints.own_sub_sessions).
        """
        stored = metadata.get("status")
        if stored not in ("active", "running", "pending"):
            return stored or "unknown"
        instance_id = metadata.get("instance_id", "")
        if ask_runs and (self.is_agent_running(instance_id)
                         or await self._runs_in_another_process(instance_id, user_id())):
            return "running"
        if stored == "active" and not _left_mid_run(metadata):
            return "idle"
        return "interrupted"

    def _get_current_phase(self, params: dict[str, Any]) -> Optional[str]:
        """Get current workflow phase from session template vars or agent config default.
        
        Args:
            params: Tool parameters with _agent reference
            
        Returns:
            Current phase value (from session vars or agent config default) or None
        """
        if not self.phase_filtering_enabled:
            return None
            
        try:
            agent = params.get("_agent")
            session_id = params.get("_session_id")
            if agent and session_id and hasattr(agent, '_session_tracker'):
                # First try session-scoped template vars (set via set_context)
                session_vars = agent._session_tracker.get_session_template_vars(session_id)
                phase = session_vars.get(self.phase_variable)
                if phase:
                    return phase
            
            # Fallback to agent config default (e.g., workflow_phase: "planning" in yaml)
            if agent and hasattr(agent, 'agent_config') and agent.agent_config:
                if hasattr(agent.agent_config, 'template_vars') and agent.agent_config.template_vars:
                    default_phase = agent.agent_config.template_vars.get(self.phase_variable)
                    if default_phase:
                        logger.debug(f"Using default phase from agent_config: {default_phase}")
                        return default_phase
        except Exception as e:
            logger.debug(f"Could not get phase from session vars: {e}")
        
        return None

    def _get_phase_allowed_agents(self, params: dict[str, Any]) -> Optional[List[str]]:
        """Get list of allowed agents for the current workflow phase.
        
        Args:
            params: Tool parameters with _agent reference
            
        Returns:
            List of allowed agents for current phase, or None if no phase filtering
        """
        if not self.phase_filtering_enabled:
            return None
            
        current_phase = self._get_current_phase(params)
        if not current_phase:
            return None
            
        phase_agents = self.phase_agents.get(current_phase)
        if phase_agents:
            logger.debug(f"Phase '{current_phase}' allows agents: {phase_agents}")
            return phase_agents
            
        # Check for default/fallback
        default_agents = self.phase_agents.get("_default")
        if default_agents is not None:
            if not default_agents:  # Empty list = all allowed_agents
                return None  # Return None to use all allowed_agents
            return default_agents
            
        return None

    def _is_agent_allowed_for_phase(self, agent_name: str, phase_allowed: Optional[List[str]]) -> bool:
        """Check if agent is allowed considering both base allowed_agents and phase filtering.
        
        Args:
            agent_name: Agent instance name
            phase_allowed: List of agents allowed for current phase, or None
            
        Returns:
            True if agent is allowed
        """
        # First check base allowed_agents
        if not self._is_agent_allowed(agent_name):
            return False
            
        # If no phase filtering active, base check is sufficient
        if phase_allowed is None:
            return True
            
        # Phase filtering active - check if agent in phase list
        return agent_name in phase_allowed

    # =========================================================================
    # Hook Implementation - Pre-LLM Call
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Inject sub-agent context into system prompt before LLM call.

        This hook adds information about active sub-agents to the conversation
        context, allowing the coordinator agent to be aware of its sub-agents.

        Args:
            context: Hook context with session_id and messages

        Returns:
            HookResult with modified=True if context was injected
        """
        try:
            from plugins.sub_agent_manager.hooks import SubAgentContextInjector

            # Get session_service from agent in context
            # IMPORTANT: Don't cache the injector! Each agent has its own session_service
            if not context.agent or not hasattr(context.agent, '_session_service') or not context.agent._session_service:
                agent_info = f"name={context.agent.name}" if context.agent and hasattr(context.agent, 'name') else "unknown"
                has_attr = hasattr(context.agent, '_session_service') if context.agent else False
                is_none = context.agent._session_service is None if (context.agent and has_attr) else True
                logger.warning(
                    f"[SubAgentManager] No session_service available from agent ({agent_info}, "
                    f"has_attr={has_attr}, is_none={is_none}), skipping hook"
                )
                return HookResult(success=True, modified=False, context=context)

            session_service = context.agent._session_service
            manager = self._get_manager(session_service, registry=None)  # No registry needed for hooks

            hook_config = self._injector_config

            # Create fresh injector for this call (each agent has different session_service)
            # Pass self.name so injector only shows sub-agents from THIS manager instance
            # Pass allowed_agents for display in the hook message
            # Pass phase_filtering config from server (top-level config, not hook_config)
            phase_filtering_config = {
                'enabled': self.phase_filtering_enabled,
                'phase_variable': self.phase_variable,
                'phase_agents': self.phase_agents
            }
            # found once, and only if a sub-agent's lock is asked about: it scans the session dirs
            user_id = functools.cache(lambda: manager._extract_user_id(context.session_id))

            async def status_of(metadata: dict[str, Any]) -> str:
                return await self._shown_status(metadata, user_id)

            injector = SubAgentContextInjector(
                manager,
                self.name,
                hook_config,
                self.allowed_agents,
                phase_filtering_config,
                status_of=status_of,
            )

            # Delegate to injector
            return await injector.inject_sub_agent_context(context)

        except Exception as e:
            logger.error(f"[SubAgentManager] Hook execution failed: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                metadata={"error": str(e)}
            )
