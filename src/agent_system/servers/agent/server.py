"""
Enhanced Agent Core - Agent extends ToolServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency

Agent is built from one mixin per responsibility (mixins/, the package docstring says why mixins):

- mixins/run.py: run_events, the run's entry, and _run_events, which drives its three phases;
  the session presence a request holds
- mixins/run_phases.py: Phase 1 (the conversation a run starts with) and Phase 3 (its end and
  final save)
- mixins/llm_loop/: Phase 2, the step loop, one module per part of a step, and the LLM call of a
  step
- mixins/llm_selection.py: the clients a run switches to (fallback, escalation, the caller's) and
  the per-request guards
- mixins/prompts.py: the system prompt and the notes a run writes for the model
- mixins/usable_tools.py: the tools the agent can use, their schemas, programmatic dispatch
- mixins/live_state.py: a request while it runs -- cancel, appended messages, live state
- mixins/persistence.py: the session's saves (tracker, disk, checkpoints)
- mixins/tool_session.py: the session an agent called as a tool runs on
- mixins/access.py: the role gate and the user a session is held for

This module keeps the constructor, the config reload and the ToolServer interface (what the agent
offers to others), and the module-level API callers import from here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...services.session_service import SessionService

from ...config.models import AgentSystemConfig, ToolServerConfig
from ...tools.base import ToolServerRegistry, ToolServer
from ...utils.id import short_id

from ...llm.models import ChatMessage, LLMClient
from .components.tool_integration import ToolIntegrationManager
from .components.tool_execution import ToolExecutionManager
from .components.session_tracking import SessionTracker
from .components.request_manager import AgentRequestManager
from .mixins.access import AccessMixin
from .mixins.live_state import LiveStateMixin
from .mixins.llm_loop import LLMLoopMixin
from .mixins.llm_selection import LLMSelectionMixin
from .mixins.persistence import PersistenceMixin
from .mixins.prompts import PromptsMixin
from .mixins.run import RunMixin
from .mixins.run_phases import RunPhasesMixin
from .mixins.tool_session import ToolSessionMixin, tool_session_id
from .mixins.usable_tools import UsableToolsMixin

# The module-level API callers import from here. Each name lives with the code that uses it.
from .refusals import (  # noqa: F401 - re-exported: the error_types of the refusals
    AGENT_ROLE_GATE,
    FOREIGN_SESSION,
    RECURSIVE_CALL,
    REFUSED_BEFORE_THE_RUN,
    SESSION_LOCKED,
    TOOL_SESSION_UNAVAILABLE,
    refused_before_the_run,
)
from .mixins.tool_session import TOOL_SESSION_ID_MAX  # noqa: F401 - re-exported
from .mixins.run_phases import ConversationContext  # noqa: F401 - re-exported
from .mixins.llm_loop.fallback import FORMAT_NOTE  # noqa: F401 - re-exported
from .mixins.llm_loop.llm_call import _name_the_model  # noqa: F401 - re-exported
from .mixins.llm_loop.tool_step import _instruction_head  # noqa: F401 - re-exported
#: STRUCTURED_OUTPUT_UNSUPPORTED and STRUCTURED_OUTPUT_INVALID are the
#: ``error_type``s of a structured run's error events; llm/structured_output.py says when.
from ...llm.structured_output import (  # noqa: F401 - re-exported
    STRUCTURED_OUTPUT_INVALID,
    STRUCTURED_OUTPUT_UNSUPPORTED,
)


logger = logging.getLogger(__name__)


def _plain(value: Any) -> bool:
    """Whether a config value goes into a reload report as is: a scalar, a short text, a list of names."""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 120
    return isinstance(value, list) and len(value) <= 12 and all(isinstance(v, str) and len(v) <= 80 for v in value)


class Agent(
    RunMixin,
    RunPhasesMixin,
    LLMLoopMixin,
    LLMSelectionMixin,
    PromptsMixin,
    UsableToolsMixin,
    LiveStateMixin,
    PersistenceMixin,
    ToolSessionMixin,
    AccessMixin,
    ToolServer,
):
    """Enhanced Agent with dual interface: execution engine + callable tool.

    TOOL INTERFACE CLARITY:
    ----------------------
    Agent has TWO distinct tool interfaces that are easily confused:

    1. EXTERNAL (what this agent OFFERS to others):
       - list_tools() → List[ToolDef] - Returns this agent as a callable tool
       - ToolServer interface: What OTHER agents see when they query our tools
       - Used by: ToolSchemaBuilder when other agents discover available tools

    2. INTERNAL (what this agent CAN USE):
       - list_usable_tools() → List[str] - Tool names this agent can call
       - Filtered by agent_config.tools.allowed patterns
       - Used by: _run_events() to build LLM prompt with available tools
       - Example: ["datetime", "web_search", "other_agent"]

    3. UTILITY (detailed info for user-facing endpoints):
       - _list_usable_tools_with_details() → List[Dict] - Name + description
       - Used by: BasicAgent's list_available_tools tool
       - For debugging/introspection, not for execution

    REMEMBER:
    - list_tools() = what I OFFER (ToolServer standard)
    - list_usable_tools() = what I CAN USE (internal execution)
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig,
                 registry: ToolServerRegistry | None = None,
                 llm: LLMClient | None = None, llm_factory: Any = None,
                 session_service: "SessionService | None" = None) -> None:
        """
        Initialize Agent as both an executor and a Tool server.

        Modern signature matching plugin pattern:
        - system_config: Complete system configuration
        - server_config: tool server configuration object (contains agent_config, type, enabled)
        - registry: Tool registry with available tools (required for agents)
        - session_service: SessionService for managing agent sessions (optional, will be injected if available)

        Args:
            name: Name of this agent (used when serving as Tool server)
            system_config: Complete system configuration (includes llm_system, network, context, etc.)
            server_config: tool server configuration object (ToolServerConfig with agent_config)
            registry: Tool registry with available tools
            llm: Optional LLM client instance (for testing)
            llm_factory: Optional LLM factory for creating client (for testing)
            session_service: Optional SessionService for session management (injected by CLI/App)
        """
        # Initialize as ToolServer with ToolServerConfig object
        super().__init__(name, system_config, server_config)

        # Extract agent_config from ToolServerConfig (required, no fallbacks)
        if not server_config.agent_config:
            raise ValueError(f"Agent '{name}' requires agent_config in ToolServerConfig")
        self.agent_config = server_config.agent_config

        # Agent-specific initialization (registry required for agents)
        if registry is None:
            raise ValueError(f"Agent '{name}' requires ToolServerRegistry instance")
        self.registry = registry

        # Store session_service for tools that need session access (e.g., sub-agent manager)
        # This is optional - if None, tools that need it will fail gracefully
        self._session_service: "SessionService | None" = session_service

        # Visibility flags control where the agent appears
        # _tool_public: Show in UI agent dropdown (GET /agents endpoint)
        # _tool_visible: Available as tool for other agents
        # Default both to False for config agents, can be overridden based on metadata
        self._tool_public = False
        self._tool_visible = False

        # Role gate, from THIS instance's merged config: the direct `type: agent`
        # gets no Runtime post-processing (apply_to), so the agent reads it itself.
        self.min_role = self._declared_min_role(server_config)

        # Allow dependency injection of an LLM client or a factory that
        # creates one (tests). Injected -- or assigned later, ``agent.llm = x`` --
        # the client is PINNED and never rebuilt. Otherwise ``llm`` is the client
        # of the agent's own profile, resolved against the config the agent holds
        # NOW (see the property): a config reload reaches it.
        self._llm_pinned = False
        self._llm_pin: LLMClient | None = None
        self._llm_shown: LLMClient | None = None
        self._llm_error: Exception | None = None
        # Every client this agent builds -- its own, the fallbacks, the advanced
        # profile -- kept by what it is built from (llm/factory.py ProfileClients).
        from ...llm.factory import ProfileClients
        self._clients = ProfileClients(on_build=self._wire_client, on_error=self._llm_build_failed)
        if llm is not None:
            self.llm = llm
        elif llm_factory is not None:
            try:
                self.llm = llm_factory.create()
            except Exception as e:
                logger.warning("LLM factory creation failed: %s", e)
                self.llm = None

        # Store profile information for status display
        self.llm_profile_info = None

        # Which LLMs are blocked (rate limit, quota, refused key) is no state of
        # this agent: the block belongs to the LLM, for every agent
        # (llm/model_health.py). What stays here are the fallback clients a run
        # walked to, by profile; a reload empties it, self._clients answers anew.
        self._fallback_clients: Dict[str, LLMClient] = {}
        # The client answering each session's running step (fallback, escalation
        # or override included); cleared when the request ends.
        self._step_llms: Dict[str, LLMClient] = {}

        # Extract profile info even if LLM is provided externally
        if self._llm_pinned and self.llm is not None and self.agent_config and system_config.llm_system:
            try:
                # Build llm_kwargs from config for profile info extraction
                llm_kwargs = {
                    "profile_name": self.agent_config.llm_profile,
                    "provider": getattr(self.llm, "provider", "external"),
                    "model": getattr(self.llm, "model", "external-model")
                }
                self.llm_profile_info = self._extract_profile_info(system_config, name, llm_kwargs)
            except Exception as e:
                logger.debug(f"Could not extract profile info for external LLM: {e}")

        # Context management now handled by hook plugins (context_engineer, context_summarizer)

        # NOTE: the global cancellation manager is configured ONCE at process
        # bootstrap (servers/bootstrap.py), not per agent — reconfiguring here
        # replaced the manager and orphaned tokens of in-flight requests.

        # Centralized internal tool-call counter (used to generate per-tool suffixes)
        self._internal_tool_counter = 0
        self._internal_tool_counter_lock = asyncio.Lock()

        # Session presence: request id -> the session it holds (_presence_step)
        self._presence_holds: dict[str, tuple] = {}

        # Cache for list_tools() to avoid creating new ToolDef objects on every call
        self._list_tools_cache: list | None = None

        # Per-session live conversation state (request-scoped). This agent is a
        # process-wide singleton shared by concurrent requests for DIFFERENT
        # sessions, so a single shared "current messages/tools" attribute is a
        # cross-session race: request B overwrites it while a compaction tool
        # for session A reads it, persisting B's conversation into A. Key the
        # live state by session_id instead. Readers (context_engineer /
        # context_summarizer compaction tools, token-estimating hooks) resolve
        # by their own session_id. Bounded by LRU eviction.
        self._live_state_by_session: dict[str, dict[str, Any]] = {}
        self._live_state_max_sessions: int = 200
        # Deprecated shared attributes - kept as a last-resort fallback for any
        # reader not yet migrated to get_live_messages/get_live_tools_schema.
        # They reflect the most recent request and are NOT session-correct.
        self._current_messages: List[ChatMessage] = []
        self._current_tools_schema: List[Dict[str, Any]] = []
        self._current_held_back_schemas: List[Dict[str, Any]] = []

        # Initialize component managers for better code organization
        # Create request manager first (owns _active_requests dict)
        self._request_manager = AgentRequestManager(self.name)
        # Session tracker shares the same _active_requests dict for coordination
        self._session_tracker = SessionTracker(self._request_manager._active_requests)
        self._session_tracker.agent_name = self.name  # the parent_agent of the sessions its tool calls run on
        # session id -> [the lock one opening of that tool session holds, how many hold or wait] (_opening_of)
        self._tool_session_openings: dict[str, list[Any]] = {}
        self._tool_integration_manager = ToolIntegrationManager(self.system_config, self.agent_config)

        # Context management now handled by hook plugins via HookIntegrationManager

        # Initialize hook integration manager
        from .components.hook_integration import HookIntegrationManager
        self._hook_manager = HookIntegrationManager(self)

        # Note: StatusEventForwarder is now created per-request (not shared) to prevent race conditions
        self._tool_execution_manager = ToolExecutionManager(
            self.registry,
            self,
            hook_manager=self._hook_manager,
        )

        # Wire LLM-client-level hooks (pre_llm_request / post_llm_response): an
        # injected client here, a built one in _wire_client. Built now, not on
        # the first run -- a profile that does not build is warned about at start.
        if self._llm_pinned:
            if self.llm is not None:
                self._wire_client(self.llm)
        else:
            self.llm  # noqa: B018 - builds it

        self._derive_from_config()

        # Set agent reference in tool integration for cancellation support
        self._set_agent_reference_in_mcp()

    def _derive_from_config(self) -> None:
        """What the agent reads once from its config into objects of its own -- at start and after a reload.

        Loop detection is stored for per-request detector creation: each request creates its own
        ToolCallLoopDetector, so concurrent requests on this singleton do not contaminate each other.
        Reasoning loop detection is stored the same way, but its detector is created per LLM CALL: its
        state is the thinking of one call, and a retry has to start from an empty window.
        """
        self.timeouts = self.agent_config.timeouts if self.agent_config else None
        reasoning_config = self.agent_config.reasoning_loop if self.agent_config else None
        self._reasoning_loop_config = {
            "enabled": reasoning_config.enabled if reasoning_config else True,
            "repetition_threshold": (reasoning_config.repetition_threshold
                                     if reasoning_config else 0.5),
        }

        loop_config = self.agent_config.loop_detection if self.agent_config else None
        if loop_config and loop_config.enabled:
            self._loop_detection_config = {
                "history_size": loop_config.history_size,
                "exact_match_threshold": loop_config.exact_match_threshold,
                "sequence_threshold": loop_config.sequence_threshold,
                "block_after_threshold": loop_config.block_after_threshold,
                "auto_unblock_after_steps": loop_config.auto_unblock_after_steps,
            }
        else:
            # Disabled: the detector does nothing (no note, no block, no stuck signal)
            self._loop_detection_config = {"enabled": False}
            if loop_config and not loop_config.enabled:
                logger.debug(f"[{self.name}] Loop detection disabled via config")

        # The hook switches of this agent (hooks.enabled, per-hook overrides).
        self._hook_manager._hooks_config = getattr(self.agent_config, "hooks", None)

    @property
    def llm(self) -> LLMClient | None:
        """The client of the agent's own profile: pinned if one was injected or assigned, else the one
        ``self._clients`` holds for the profile as the agent's config resolves it NOW. After a reload that
        changed the model, the first access builds the new client once; a run that started before keeps the
        one it took (``active_llm``, llm_loop/loop.py). None when the profile does not build."""
        if self._llm_pinned:
            return self._llm_pin
        cfg = self.agent_config
        clients = getattr(self, "_clients", None)
        if cfg is None or clients is None:
            return None
        from ...llm.factory import agent_params_for_profile
        profile = cfg.default_llm_profile
        client = clients.get(self.system_config, profile, agent_params_for_profile(cfg, profile))
        if client is not self._llm_shown:
            self._llm_shown = client
            self.llm_profile_info = self._own_profile_info() if client is not None else None
        return client

    @llm.setter
    def llm(self, client: LLMClient | None) -> None:
        self._llm_pinned = True
        self._llm_pin = client

    def _own_profile_info(self) -> str | None:
        """``profile:provider/model`` of the agent's own profile, for the status line."""
        try:
            from ...llm.factory import resolve_llm_config_for_agent
            resolved = resolve_llm_config_for_agent(self.system_config, self.agent_config)
            return self._extract_profile_info(self.system_config, self.name, {
                "profile_name": resolved.profile_name,
                "provider": resolved.spec.provider,
                "model": resolved.spec.model,
            })
        except Exception as e:
            logger.debug("[%s] no profile info: %s", self.name, e)
            return None

    def _wire_client(self, client: Any) -> None:
        """What every client of this agent gets: its OpenRouter app identity (a unique HTTP-Referer per agent)
        and the LLM-level hooks (pre_llm_request / post_llm_response), so debugger and cost tracking see every
        call -- own, fallback and escalation alike."""
        if hasattr(client, "set_app_title"):
            client.set_app_title(self.name)
        if getattr(self, "_hook_manager", None) is not None:
            self._hook_manager.wire_llm_hooks(client)

    def _llm_build_failed(self, profile: str, error: Exception) -> None:
        self._llm_error = error
        # Missing API key is an expected situation in test/dev environments;
        # avoid noisy warnings for that case. The provider factories raise
        # "<VAR> is required when provider=<name>" -- since the registry split
        # there are two openai-family variants, so match both.
        msg = str(error)
        if isinstance(error, ValueError) and msg in (
                "OPENAI_API_KEY is required when provider=openai",
                "OPENAI_API_KEY is required when provider=openai_httpx"):
            logger.debug("Agent '%s': LLM %s not initialized (no API key): %s", self.name, profile, msg)
        else:
            # WITH the agent name. Without it these lines are indistinguishable:
            # one model removed from llm.yaml produced 50 identical warnings
            # (measured), and the agents kept running with llm=None until their
            # first request.
            logger.warning("Agent '%s': LLM %s initialization failed: %s", self.name, profile, msg)

    #: The keys of an entry besides agent_config (which a reload takes whole) that a reload applies to a running
    #: agent -- read from server_config, applied by its declaration (Runtime.apply_to). ``type`` and a plugin
    #: type's own settings need a restart.
    RELOADED_ENTRY_KEYS = ("description", "metadata", "self_tool_descriptions")

    def reload_config(self, server_config: Any, system_config: Any = None) -> dict:
        """Take over a fresh parse: the agent's whole config, the system config, its own entry.

        Called by the deliberate config-reload flow (POST /admin/reload-config, ``agent-cli reload``) with
        the MERGED entry of this server and the freshly loaded system config. Until 2026-10-24 only seven
        scalars (max_steps and the escalation knobs) were copied: llm_profile, llm_params, fallback_chain and
        every model change in llm.yaml stayed at the start-up state, while a run switched to a profile
        already resolved against the fresh config -- half a reload.

        - Every field of the fresh agent_config is copied onto the agent's own object, in place: the tool
          integration and discovery hold the same object. What the agent derived from it at start is
          derived again (_derive_from_config); the LLM clients follow by themselves (``llm``).
        - The new primary client is built HERE, before anything changes: a profile that does not build
          raises, the reload reports it as this server's error, and the agent keeps running as it was --
          unless it had no working client to keep, then it takes the rest of its config.
        - A run in flight keeps its primary client; everything else it reads per step (fallback chain,
          prompt, tool lists) it reads from the fresh config from its next step on.
        - The role gate (``metadata.min_role``) is refreshed on THIS instance: the entries that read it from
          the instance (every HTTP check, the SAM and the backstop in run_events, through ``Runtime.view``)
          apply the new gate from the next run on. Not reached: the wake check and the start-up warning
          (both read the process's config, not a reloaded one).

        Returns the fields that changed ({} if none): scalars as {old, new}, anything bigger as
        {"changed": True}; ``llm`` ({old, new} as ``profile:provider/model``) when the agent's own client is
        another one now -- also when only llm.yaml changed, which no field of the entry shows.
        """
        changes: dict[str, dict] = {}
        new_agent_cfg = getattr(server_config, "agent_config", None)
        fresh_system = system_config if system_config is not None else self.system_config
        # The client before, to report a model changed in llm.yaml: the agent's own entry is the same then.
        running_llm, running_info = self._llm_shown, self.llm_profile_info

        if new_agent_cfg is not None and self.agent_config is not None and not self._llm_pinned:
            from ...llm.factory import agent_params_for_profile
            profile = new_agent_cfg.default_llm_profile
            built = self._clients.get(fresh_system, profile, agent_params_for_profile(new_agent_cfg, profile))
            # Only a working agent is protected: one whose own profile never built (it runs on overrides,
            # or a key is missing) still takes the rest of its config.
            if built is None and running_llm is not None:
                raise ValueError(f"LLM profile {profile!r} does not build ({self._llm_error}); "
                                 f"the agent keeps its running config")

        new_min_role = self._declared_min_role(server_config)
        if new_min_role != self.min_role:
            changes["min_role"] = {"old": self.min_role, "new": new_min_role}
            self.min_role = new_min_role

        if system_config is not None and system_config is not self.system_config:
            self.system_config = system_config
            self._tool_integration_manager.system_config = system_config
        for key in self.RELOADED_ENTRY_KEYS:
            old_value, new_value = getattr(self.server_config, key, None), getattr(server_config, key, None)
            if old_value != new_value:
                changes[key] = ({"old": old_value, "new": new_value} if _plain(old_value) and _plain(new_value)
                                else {"changed": True})
        self.server_config = server_config
        # What this agent offers as a tool (description, llm_profile enum of basic_agent) is cached.
        self._list_tools_cache = None
        clear = getattr(self, "clear_schema_cache", None)
        if callable(clear):
            clear()

        if new_agent_cfg is None or self.agent_config is None:
            if changes:
                logger.info("[%s] config reload applied: %s", self.name, changes)
            return changes

        for field in type(new_agent_cfg).model_fields:
            new_value = getattr(new_agent_cfg, field)
            old_value = getattr(self.agent_config, field, None)
            if old_value == new_value:
                continue
            setattr(self.agent_config, field, new_value)
            changes[field] = ({"old": old_value, "new": new_value} if _plain(old_value) and _plain(new_value)
                              else {"changed": True})

        self._derive_from_config()
        self._fallback_clients.clear()
        self._escalation_llm_cached = None
        if not self._llm_pinned and self.llm is not running_llm:
            changes["llm"] = {"old": running_info, "new": self.llm_profile_info}
        if changes:
            logger.info("[%s] config reload applied: %s", self.name, sorted(changes))
        return changes

    def _set_agent_reference_in_mcp(self) -> None:
        """Set agent reference in tool integration for cancellation support."""
        try:
            # Set agent reference in tool integration manager
            self._tool_integration_manager._agent_ref = self

            # Try to set agent reference in tool integration when it's available
            if hasattr(self._tool_integration_manager, 'tool_integration') and self._tool_integration_manager.tool_integration:
                self._tool_integration_manager.tool_integration.main_agent_ref = self
        except Exception as e:
            logger.debug("Failed to set agent reference in tool integration: %s", e)

    @property
    def description(self) -> str:
        """Get the agent description."""
        # Try to get description from server_config
        if hasattr(self, 'server_config') and self.server_config:
            desc = getattr(self.server_config, 'description', None)
            if desc:
                return desc
        return f"Agent: {self.name}"

    # ToolServer interface implementation
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """
        ToolServer interface: Handle tool calls from other agents.

        An agent is a tool that executes tasks. No special "actions" needed.

        Args:
            tool: The tool name (should be agent's name when called via MCP)
            params: Parameters including task/query/prompt (required)

        Returns:
            The agent's execution result
        """
        # Extract task from parameters
        task = params.get("task") or params.get("query") or params.get("prompt")
        if not task:
            return {
                "status": "error",
                "error": "Missing required parameter: 'task', 'query', or 'prompt'"
            }

        # Extract session context from injected params (populated by ToolExecutionManager).
        # The session only from the injected ``_session_id``, never a plain
        # ``session_id``: no agent tool schema offers one, and tool execution strips
        # a model's ``_*`` and request-id keys, not that one -- so the model could
        # name ANY session this agent holds, another user's sub-session included,
        # and the run would continue it with its history, its user and that
        # session's approvals instead of its caller's. And not the caller's session
        # itself: a sub-session of this agent's own below it (tool_session_id).
        request_id = params.get("request_id") or params.get("_request_id")
        caller_session_id = params.get("_session_id")
        session_id = tool_session_id(caller_session_id, self.name) if caller_session_id else None

        # A call that brings a ``_user_id`` but no request naming a user --
        # dispatched without a request id: a plugin command (run_plugin_command)
        # -- runs under an id of its own registered for that user, as
        # MachineAgent.call does. Unregistered, the run stored "anonymous" as
        # the session's user and ran its tools as anonymous, and the same user's
        # next call in the session was refused as somebody else's. A dispatch
        # that brings a request id and a user registered the request itself
        # (inject_runtime_params); one that brings no user injects none.
        own_request_id = self._request_for_injected_user(request_id, params.get("_user_id"))
        if own_request_id:
            request_id = own_request_id

        try:
            # The role gate and the session's user, asked here as well as in
            # run_events: refused there, the run's error would come back inside a
            # "success" answer -- the calling model should read a refusal as one.
            refusal = self._refusal_event(request_id, session_id)
            if refusal:
                return {"status": "error", "agent": self.name, "task": task, "error": refusal["message"],
                        "error_type": refusal["error_type"]}
            if session_id:
                refusal = await self._open_tool_session(request_id, caller_session_id, session_id,
                                                        caller_agent=params.get("_agent_name"), title=str(task))
                if refusal:
                    return {"status": "error", "agent": self.name, "task": task, **refusal}

            # Execute the task using this agent
            logger.info("Agent %s executing task: %s", self.name, task[:100])
            from .result_utils import collect_final_result, extract_summary
            result = await collect_final_result(
                self, 
                str(task),
                request_id=request_id,
                session_id=session_id
            )
            if result.get("refused"):
                # Refused at its start -- another call of the same caller session has the session (its lock):
                # the calling model reads a refusal as one, not a "success" with the error inside.
                errors = result.get("errors") or []
                return {"status": "error", "agent": self.name, "task": task,
                        "error": errors[-1] if errors else "the run was refused before it started",
                        "error_type": result["refused"]}

            # Wrap result with agent metadata
            return {
                "status": "success",
                "agent": self.name,
                "task": task,
                "result": result,
                "summary": extract_summary(result)
            }

        except Exception as e:
            logger.error("Agent %s failed to execute task: %s", self.name, e)
            return {
                "status": "error",
                "agent": self.name,
                "task": task,
                "error": str(e)
            }
        finally:
            if own_request_id:
                # What this call registered goes with it, as the API lets go of
                # its request tree when the request ends.
                from ...core.request_context import release_request_user_tree
                release_request_user_tree(own_request_id)

    @staticmethod
    def _request_for_injected_user(request_id: Optional[str], injected_user: Any) -> Optional[str]:
        """A request id registered for *injected_user*, or None when none is needed.

        None where the call's request id already names a user (the owner wins) or
        no user is injected. Otherwise an id of its own: derived from the caller's
        (so cancel and status stay under its prefix) or new, registered for the
        user -- the framework's injected ``_user_id``, which a model cannot set
        (tool execution strips ``_*`` keys from its arguments).
        """
        from ...core.request_context import get_request_user, register_request_user

        if not isinstance(injected_user, str) or not injected_user.strip():
            return None
        if request_id and get_request_user(str(request_id), default=None) is not None:
            return None
        own = f"{request_id}_{short_id(6)}" if request_id else short_id()
        register_request_user(own, injected_user.strip())
        return own

    def get_schema(self) -> dict[str, Any]:
        """
        ToolServer interface: Return the OpenAI function schema for this agent.

        Returns:
            OpenAI function schema dict
        """
        # Try to get description from: server_config.description -> fallback to agent name
        description = None
        if hasattr(self, 'server_config') and self.server_config:
            description = getattr(self.server_config, 'description', None)
        if not description:
            description = getattr(self, '_agent_description', f"Agent: {self.name}")

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The task/query/prompt to execute"
                        }
                    },
                    "required": ["task"],
                },
            },
        }

    async def list_tools(self) -> List:
        """Return tools this agent OFFERS to other agents (ToolServer interface).

        EXTERNAL INTERFACE - What this agent exposes as callable tools.
        When other agents query available tools, they get this agent's schema.

        Contrast with list_usable_tools() which returns tools this agent CAN USE.

        Returns:
            List[ToolDef] - Single ToolDef representing this agent
        """
        # Return cached tools to avoid creating new objects on every call
        if self._list_tools_cache is not None:
            return self._list_tools_cache

        from agent_system.tools.base import ToolDef

        # Get the agent's schema (what it offers as a callable tool)
        schema = self.get_schema()
        func = schema.get("function", {})

        # Convert to ToolDef format
        tool = ToolDef(
            name=func.get("name", self.name),
            description=func.get("description", f"Agent: {self.name}"),
            input_schema=func.get("parameters", {})
        )

        self._list_tools_cache = [tool]
        return self._list_tools_cache

    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> Any:
        """Call a tool by name with parameters.
        
        This is a convenience method that allows calling tools directly
        without going through the full agent run loop. Useful for:
        - Precondition checks (task_switch plugin)
        - Direct tool invocations from sub-agents
        - Testing individual tools
        
        Args:
            tool_name: The full tool name (e.g., "writer_content_production_status")
            params: Parameters to pass to the tool
            
        Returns:
            The tool's result
        """
        return await self._tool_execution_manager._invoke_tool(tool_name, params)
