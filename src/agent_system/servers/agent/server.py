"""
Enhanced Agent Core - Agent extends ToolServer for direct agent-to-agent communication
Supports multiple tool calls per conversation turn for better efficiency
"""
from __future__ import annotations

import asyncio
import copy
import errno
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from ...services.session_service import SessionService

from ...config.models import AgentSystemConfig, ToolServerConfig
from ...core.cancellation import get_cancellation_manager, CancellationToken
from ...core.session_presence import SessionBusy, presence_for
from ...tools.base import ToolServerRegistry, ToolServer
from ...utils.id import short_id
from ...utils.json_utils import history_safe_tool_calls
from ...utils.reasoning_artifacts import (
    strip_all_reasoning_artifacts,
    strip_foreign_reasoning_artifacts,
)
import httpx

from ...llm.message_roles import DEVELOPER, leading_instructions, role_of
from ...llm.model_health import model_health
from ...llm.models import ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError, LLMServerError, LLMConnectionError
from ...llm.text_sanitizer import sanitize_for_llm
from ...tools.status import (
    status_scope,
    StatusScope,
    status_bus,
    current_request_id
)
from .components.tool_integration import ToolIntegrationManager
from .components.tool_execution import ToolExecutionManager
from .components.status_forwarding import StatusEventForwarder, relay_run_event
from .components.session_tracking import SessionTracker
from .components.request_manager import AgentRequestManager
from .components.server_resolution import resolve_registry_server, resolve_longest_prefix
from .prompt_strategies import PromptRenderer, PromptContext
from .loop_detection import ToolCallLoopDetector
from .reasoning_loop import ReasoningLoopError, build_detectors
from .escalation import StuckEscalator
from .tool_discovery import ToolDiscoveryService
from .tool_schema_builder import ToolSchemaBuilder, server_matches_patterns


logger = logging.getLogger(__name__)

# llm_progress hooks fire every this many characters of thinking. A hook sets
# its own, coarser interval on top; this only bounds how often the loop pays
# for a hook dispatch while it streams.
_REASONING_PROGRESS_TICK = 2000

# Local descriptor exhaustion. httpx reports it as a ConnectError, which is
# indistinguishable from an unreachable endpoint unless the cause chain is
# inspected — see _is_local_resource_exhaustion.
_LOCAL_EXHAUSTION_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE})


def _is_local_resource_exhaustion(exc: BaseException) -> bool:
    """Is this transport failure OUR machine running out of descriptors?

    A provider being unreachable and this process being unable to open a socket
    both surface as httpx.ConnectError, but they call for opposite responses:
    the first is what fallback profiles exist for, the second cannot be helped
    by any profile — the next client hits the same wall. On 2026-08-30 a
    descriptor leak made the writer host do exactly that: 52 fallback switches
    onto pricier models, none of which could have succeeded.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno in _LOCAL_EXHAUSTION_ERRNOS:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _content_filter_as_error(assistant: Any, finish_reason: Optional[str], llm: Any) -> None:
    """Mark a provider content-filter stop as an upstream error, in place.

    The one rule for both paths of _call_llm_with_streaming. The filter is
    deterministic per content: nudging the same model with "Continue" asks
    the same question again, and a partial answer it cut is not a complete
    one -- moving on is exactly what the fallback chain is for, and the
    "error" key is what engages it.

    Left alone: an answer that already carries an error, and one that carries
    tool calls. Gemini reports a non-standard finish_reason while STILL
    returning usable tool calls; httpx _format_response keeps those, and
    erroring here would throw away a perfectly good turn.
    """
    if (finish_reason != "content_filter" or not isinstance(assistant, dict)
            or "error" in assistant or assistant.get("tool_calls")):
        return
    assistant["error"] = {
        "message": (f"Provider content filter blocked the response "
                    f"(model={getattr(llm, 'model', '?')})"),
        "type": "content_filter",
    }


def _name_the_model(event: dict, llm: Any) -> dict:
    """Name on a thinking_complete the model that ran the call, in place.

    A fallback or a walk around a blocked LLM runs on a client the caller never
    handed in, and a surface pricing the call needs its model.
    """
    model = getattr(llm, "model", None)
    if isinstance(model, str) and model:
        event["model"] = model
        batch = getattr(llm, "batch_provider", None)
        event["batch"] = isinstance(batch, str) and bool(batch)
    return event


def _instruction_head(messages: list, rebuilt: list) -> list:
    """The leading instruction block, minus whatever ``rebuilt`` already has.

    Both history rebuilds below are "the head + the conversation a hook handed
    back", and what that hook hands back differs per hook. context_engineer
    strips the prompts and returns only its own compaction system messages
    (the archive pointers, the prune breadcrumb); context_summarizer returns
    the WHOLE list, prompts included, and says so in a comment. So there is no
    fixed set to subtract -- the only rule that holds for both is: do not put
    back what is already there.

    Prepending blindly sent the breadcrumb twice on one hook and both prompts
    twice on the other, and the persist right after wrote the copies to disk;
    the next prune then read a doubled total. The version before that took
    ``messages[0]`` alone, which duplicated the first prompt and dropped the
    second one entirely.
    """
    def key(msg):
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        return role_of(msg), content if isinstance(content, str) else repr(content)

    # id() as well as the key: a hook usually hands the SAME objects back, and
    # two different prompts could in principle share a text.
    seen_ids = {id(msg) for msg in rebuilt}
    seen = {key(msg) for msg in rebuilt}
    return [msg for msg in leading_instructions(messages)
            if id(msg) not in seen_ids and key(msg) not in seen]


@dataclass
class ConversationContext:
    """Context for agent conversation execution."""
    messages: List[ChatMessage]
    available_tools: List[str]
    tools_schema: List[Dict[str, Any]]
    tool_name_mapping: Dict[str, str]
    max_steps: int
    main_token: CancellationToken
    context_reset_token: Any  # Token for resetting contextvars
    status_forwarder: 'StatusEventForwarder'  # Per-request forwarder instance
    session_id: Optional[str] = None  # Session ID for session-scoped operations
    user_reset_token: Any = None  # Token for resetting current_run_user


class Agent(ToolServer):
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

        # Allow dependency injection of an LLM client or a factory that
        # creates one. This makes testing and runtime wiring explicit.
        self.llm: LLMClient | None = llm
        self._llm_factory = llm_factory

        # Initialize LLM if not provided
        # Store LLM profile information for status display
        self.llm_profile_info = None
        
        # Store timeout configuration from agent_config
        self.timeouts = self.agent_config.timeouts if self.agent_config else None
        
        # Which LLMs are blocked (rate limit, quota, refused key) is no state of
        # this agent: the block belongs to the LLM, for every agent
        # (llm/model_health.py). What stays here are the clients of the
        # fallback profiles, built once each.
        self._fallback_clients: Dict[str, LLMClient] = {}
        # The client answering each session's running step (fallback, escalation
        # or override included); cleared when the request ends.
        self._step_llms: Dict[str, LLMClient] = {}

        # Extract profile info even if LLM is provided externally
        if self.llm is not None and self.agent_config and system_config.llm_system:
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

        if self.llm is None:
            # If a factory is provided, use it to create the client.
            if self._llm_factory is not None:
                try:
                    self.llm = self._llm_factory.create()
                except Exception as e:
                    logger.warning("LLM factory creation failed: %s", e)
                    self.llm = None
            else:
                # Create LLM using the new profile-based system (now required)
                try:
                    # Lazy import to avoid circular imports when testing
                    from ...llm.factory import create_llm_from_profile, resolve_llm_config_for_agent

                    # Use new profile-based resolution with agent config
                    resolved = resolve_llm_config_for_agent(system_config, self.agent_config)

                    # Store profile information for status display
                    self.llm_profile_info = self._extract_profile_info(system_config, name, {
                        "profile_name": resolved.profile_name,
                        "provider": resolved.spec.provider,
                        "model": resolved.spec.model,
                    })

                    # Use factory function that properly handles batch mode
                    self.llm = create_llm_from_profile(
                        config=system_config,
                        llm_profile=self.agent_config.default_llm_profile,
                        llm_params=self.agent_config.llm_params,
                    )
                except Exception as e:
                    # Missing API key is an expected situation in test/dev
                    # environments; avoid noisy warnings for that case.
                    try:
                        msg = str(e)
                    except Exception as e2:
                        logger.debug(f"Failed to stringify exception: {e2}")
                        msg = "<exception>"
                    # The provider factories raise "<VAR> is required when
                    # provider=<name>" — since the registry split there are
                    # two openai-family variants, so match both.
                    if isinstance(e, ValueError) and msg in (
                            "OPENAI_API_KEY is required when provider=openai",
                            "OPENAI_API_KEY is required when provider=openai_httpx"):
                        logger.debug("Agent '%s': LLM not initialized (no API key): %s",
                                     name, msg)
                    else:
                        # WITH the agent name. Without it these lines are
                        # indistinguishable: one model removed from llm.yaml
                        # produced 50 identical warnings (measured), and the
                        # agents kept running with llm=None until their first
                        # request.
                        logger.warning("Agent '%s': LLM initialization failed: %s",
                                       name, msg)
                    self.llm = None

        # Set per-agent OpenRouter app identity (unique HTTP-Referer per agent)
        if self.llm is not None and hasattr(self.llm, 'set_app_title'):
            self.llm.set_app_title(name)

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

        # Initialize component managers for better code organization
        # Create request manager first (owns _active_requests dict)
        self._request_manager = AgentRequestManager(self.name)
        # Session tracker shares the same _active_requests dict for coordination
        self._session_tracker = SessionTracker(self._request_manager._active_requests)
        self._tool_integration_manager = ToolIntegrationManager(self.system_config, self.agent_config)
        # Note: StatusEventForwarder is now created per-request (not shared) to prevent race conditions
        self._tool_execution_manager = ToolExecutionManager(
            self.registry,
            self
        )

        # Context management now handled by hook plugins via HookIntegrationManager

        # Initialize hook integration manager
        from .components.hook_integration import HookIntegrationManager
        self._hook_manager = HookIntegrationManager(self)
        
        # Wire LLM-client-level hooks (pre_llm_request / post_llm_response)
        if self.llm is not None:
            self._hook_manager.wire_llm_hooks(self.llm)
        
        # Store loop detection config for per-request detector creation.
        # Each request creates its own ToolCallLoopDetector to prevent
        # cross-request contamination when the Agent singleton handles
        # concurrent requests or sequential requests on the same session.
        # Reasoning loop detection, stored the same way — but the detector is
        # created per LLM CALL, not per request: its state is the thinking of
        # one call, and a retry has to start from an empty window.
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
            # Disabled config — detector will never trigger
            self._loop_detection_config = {
                "exact_match_threshold": 9999,
            }
            if loop_config and not loop_config.enabled:
                logger.debug(f"[{self.name}] Loop detection disabled via config")

        # Set agent reference in tool integration for cancellation support
        self._set_agent_reference_in_mcp()

    #: Agent-config knobs that a deliberate reload may change on a LIVE agent.
    #: Every one of them is a plain scalar that the run loop re-reads from
    #: ``self.agent_config`` on each request, so a change takes effect on the
    #: NEXT run while in-flight runs keep the value they started with.
    #:
    #: Deliberately NOT here, because the startup wired something from them and
    #: refreshing only the config would desync the two:
    #:   - llm_profile / advanced_llm_profile / llm_params / fallback_chain:
    #:     ``self.llm`` was built from these at startup.
    #:   - tools: the tool schemas are wired into the tool integration at startup.
    #:   - loop_detection / reasoning_loop / timeouts: read once into derived
    #:     objects (``_loop_detection_config``, ``_reasoning_loop_config``,
    #:     ``self.timeouts``).
    #: Those still need a restart, and saying so beats pretending otherwise.
    _RELOADABLE_AGENT_FIELDS = (
        "max_steps",
        "auto_escalate_on_stuck",
        "escalate_rounds",
        "escalate_max_calls",
        "escalate_error_streak",
        "fallback_recovery_seconds",
        "inherit_parent_llm",
    )

    def reload_config(self, server_config: Any) -> dict:
        """Refresh the live agent's plain config knobs from a fresh parse.

        Called by the deliberate config-reload flow (POST /admin/reload-config,
        ``agent-cli reload``). Without this the reload skipped every agent as
        "unsupported": raising an agent's ``max_steps`` needed a full API
        restart, which drops in-flight book runs.

        Returns the fields that actually changed ({} if none), so the caller
        can report exactly what took effect.
        """
        new_agent_cfg = getattr(server_config, "agent_config", None)
        if new_agent_cfg is None or self.agent_config is None:
            return {}

        changes: dict[str, dict] = {}
        for field in self._RELOADABLE_AGENT_FIELDS:
            if not hasattr(new_agent_cfg, field):
                continue
            new_value = getattr(new_agent_cfg, field)
            old_value = getattr(self.agent_config, field, None)
            if old_value == new_value:
                continue
            setattr(self.agent_config, field, new_value)
            changes[field] = {"old": old_value, "new": new_value}

        if changes:
            logger.info("[%s] config reload applied: %s", self.name, changes)
        return changes

    def _create_loop_detector(self) -> ToolCallLoopDetector:
        """Create a fresh loop detector for a single request.

        Each request gets its own detector so concurrent requests don't
        interfere, and previous-request history doesn't leak into new requests.
        """
        return ToolCallLoopDetector(**self._loop_detection_config)

    def _create_stuck_escalator(self, *, already_advanced: bool) -> StuckEscalator:
        """Per-request escalator (window + budget state must not leak across
        requests on this shared Agent singleton). Disabled — a no-op — when the
        config flag is off, no advanced profile exists, or the run is already on
        the advanced model (nothing to escalate to)."""
        cfg = self.agent_config
        # Gleichheits-Guard spiegelt _get_escalation_llm: advanced == default
        # kann keinen anderen Client bauen — Escalator wäre ein toter Trigger.
        has_advanced = bool(
            cfg and cfg.advanced_llm_profile
            and cfg.advanced_llm_profile != cfg.default_llm_profile)
        enabled = bool(
            cfg and getattr(cfg, "auto_escalate_on_stuck", False)
            and has_advanced and not already_advanced)
        return StuckEscalator(
            enabled=enabled,
            rounds=int(getattr(cfg, "escalate_rounds", 2)) if cfg else 0,
            max_calls=int(getattr(cfg, "escalate_max_calls", 6)) if cfg else 0,
        )

    @staticmethod
    def _tool_message_is_error(message: "ChatMessage") -> bool:
        """Whether a tool-result message reports a failure. Mirrors the two
        error shapes tools use: {"status":"error",...} and a bare {"error":...}
        (no status). Non-JSON / non-dict content is treated as non-error."""
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content:
            return False
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        if data.get("status") == "error":
            return True
        return "status" not in data and bool(data.get("error"))

    def llm_for_session(self, session_id: Optional[str]) -> Optional[LLMClient]:
        """The client answering the session's running step, else the agent's own.

        For tools that size the context themselves: agent.llm is the configured
        model, not the fallback, escalation or override that answers.
        """
        return self._step_llms.get(session_id) if session_id in self._step_llms else self.llm

    def _get_escalation_llm(self) -> Optional[LLMClient]:
        """The advanced-profile LLM client used for auto-escalation, built once
        and cached (same profile use_advanced_model picks: the last, most
        capable, of llm_profile). Hooks are wired so cost/debugger tracking
        captures escalated calls too. Returns None if it cannot be built.

        Only a SUCCESSFUL client is cached: a transient build failure is not
        remembered on this process-wide singleton, so a later run can retry
        (within a run, the caller disables the escalator on None to avoid
        re-attempting every step)."""
        cached = getattr(self, "_escalation_llm_cached", None)
        if cached is not None:
            return cached
        try:
            advanced_profile = self.agent_config.advanced_llm_profile if self.agent_config else None
            if not advanced_profile or advanced_profile == self.agent_config.default_llm_profile:
                return None
            from ...llm.factory import override_for_profile
            client, _ = override_for_profile(self.system_config, self.agent_config, advanced_profile)
            if hasattr(client, "set_app_title"):
                client.set_app_title(self.name)
            if self._hook_manager:
                self._hook_manager.wire_llm_hooks(client)
            logger.info("[%s] built escalation (advanced) LLM: %s",
                        self.name, advanced_profile)
            self._escalation_llm_cached = client
            return client
        except Exception as e:
            logger.warning("[%s] could not build escalation LLM: %s", self.name, e)
            return None

    def _llm_from_caller(self) -> Optional[tuple[LLMClient, str]]:
        """(client, profile info) for a run on the caller's LLM, else None.

        Only for an agent that asks for it (agent_config.inherit_parent_llm) and
        only when the calling run was switched to a profile (llm/caller_llm.py).
        Built like any override: the agent keeps its own llm_params for the
        profile, its own chain stays the fallback. A profile this config does
        not know leaves the agent on its own chain, with a warning -- a caller
        on a model the sub-agent cannot run must not cost the call.
        """
        if not (self.agent_config and self.agent_config.inherit_parent_llm):
            return None
        from ...llm.caller_llm import caller_llm_profile
        profile = caller_llm_profile()
        if not profile:
            return None
        try:
            from ...llm.factory import override_for_profile
            client, label = override_for_profile(self.system_config, self.agent_config, profile)
        except Exception as e:
            logger.warning("[%s] cannot run on the caller's LLM profile %r, runs its own: %s",
                           self.name, profile, e)
            return None
        logger.info("[%s] runs on the caller's LLM profile %s", self.name, profile)
        return client, f"{label} (from caller)"

    def _profile_to_hand_down(self, llm_override: Optional[LLMClient]) -> Optional[str]:
        """The profile this run hands to the sub-agents its tools start: the one
        it was switched to, else None. An override on the agent's own primary
        profile (--llm-params alone, a web pick of the same profile) switched
        nothing -- unless the run followed its caller onto it: that switch came
        from above and goes on down.
        """
        from ...llm.caller_llm import caller_llm_profile
        profile = getattr(llm_override, "profile_name", None)
        if (profile and self.agent_config and profile == self.agent_config.default_llm_profile
                and profile != caller_llm_profile()):
            return None
        return profile

    def _extract_profile_info(self, config, agent_name: str, llm_kwargs: dict) -> str:
        """Extract profile information for status display."""
        model = llm_kwargs.get("model", "unknown")
        provider = llm_kwargs.get("provider", "unknown")

        # Get profile name - priority order:
        # 1. From llm_kwargs (directly resolved profile used for this LLM)
        # 2. From agent_config.llm_profile (agent's configured profile)
        # 3. From agent_llm_profiles mapping (agent-specific override)
        # 4. From default_profile (system default)
        profile_name = llm_kwargs.get("profile_name")

        if not profile_name and hasattr(self, 'agent_config') and self.agent_config:
            profile_name = getattr(self.agent_config, 'llm_profile', None)
            # If llm_profile is a list, use the first element (default profile)
            if isinstance(profile_name, list):
                profile_name = profile_name[0] if profile_name else None

        if not profile_name and config.llm_system and config.llm_system.profiles:
            # Check agent-specific assignment
            if agent_name and hasattr(config, 'agent_llm_profiles') and config.agent_llm_profiles:
                profile_name = config.agent_llm_profiles.get(agent_name)
            # Fall back to default profile
            if not profile_name:
                profile_name = getattr(config.llm_system, 'default_profile', None)

        # Return with profile name if available, otherwise just provider/model
        if profile_name:
            return f"{profile_name}:{provider}/{model}"
        return f"{provider}/{model}"

    def _create_fallback_llm(self, fallback_profile: str) -> Optional[LLMClient]:
        """Create an LLM client for a fallback profile.
        
        Args:
            fallback_profile: Name of the fallback LLM profile to use
            
        Returns:
            LLM client instance or None if creation fails
        """
        try:
            from ...llm.factory import create_llm_from_profile

            # Fallbacks laufen mit DERSELBEN llm_params-Semantik wie das
            # Primaermodell — create_llm_from_profile loest die profil-
            # gekeyten Params selbst auf ("*"/flat fuer die ganze Kette,
            # exakter Eintrag gewinnt). Keine Sonderbehandlung hier.
            fallback_llm = create_llm_from_profile(
                config=self.system_config,
                llm_profile=fallback_profile,
                llm_params=self.agent_config.llm_params if self.agent_config else None,
            )
            fallback_llm.set_app_title(self.name)
            # Mirror init/llm_override: wire hooks so debugger + cost tracking
            # capture pre_llm_request / post_llm_response on fallback calls too.
            # Without this, every fallback round-trip is silently unrecorded.
            if self._hook_manager:
                self._hook_manager.wire_llm_hooks(fallback_llm)
            logger.info(f"[{self.name}] Created fallback LLM for profile: {fallback_profile}")
            return fallback_llm
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to create fallback LLM for profile '{fallback_profile}': {e}")
            return None

    def _fallback_client(self, profile: str) -> Optional[LLMClient]:
        """The client of a fallback profile, built on first use and kept: a step
        that walks around a blocked LLM would otherwise build one per step. A
        profile that did not build is tried again next time."""
        client = self._fallback_clients.get(profile)
        if client is None:
            client = self._create_fallback_llm(profile)
            if client is not None:
                self._fallback_clients[profile] = client
        return client

    def _strip_for_switch(self, profile: str, messages: List[ChatMessage]) -> None:
        """Strip every provider reasoning artifact before the call goes to
        another model: encrypted reasoning items / thought signatures are bound
        to the model that produced them — round-tripping them into a DIFFERENT
        model is useless at best and a hard 400 at worst. For the new model
        this is simply a fresh start."""
        stripped = strip_all_reasoning_artifacts(messages)
        if stripped:
            logger.info(
                f"[{self.name}] Stripped reasoning artifacts from {stripped} "
                f"message(s) on model switch to {profile}"
            )

    async def _save_session_to_disk(self, session_id: str) -> bool:
        """Persist a session to disk via SessionService (no-op without service
        or metadata). Shared by the turn-persistence helper and the
        compacted-messages branch in _finalize_request. Returns whether the
        session file was written: what a request carried only counts as
        delivered once it is (see _finalize_request)."""
        if not self._session_service:
            logger.debug("No session_service available, skipping disk save")
            return False
        session_meta = self._session_tracker.get_session_metadata(session_id)
        if not session_meta:
            logger.warning(f"No session metadata found for {session_id}, skipping disk save")
            return False
        written = await self._session_service.save_session(
            agent=self,
            user_id=session_meta.get("user_id", "anonymous"),
            session_id=session_id,
            agent_name=session_meta.get("agent_name", self.name),
            llm_profile=session_meta.get("llm_profile", self.agent_config.default_llm_profile),
            was_new_session=False  # Always update for intermediate/final saves
        )
        logger.debug(f"Saved session {session_id} to disk")
        return bool(written)

    async def _persist_conversation(self, session_id: str, messages: List[ChatMessage],
                                    *, to_disk: bool, note: str) -> bool:
        """Persist the conversation (non-system messages) to the in-memory
        tracker and optionally to disk — THE single implementation of the
        'filter system → set_session_messages → save_session' sequence that was
        copied at three points of the request lifecycle (after LLM response,
        after a completed tool turn, at request finalization). Never raises:
        persistence failures must not kill a running request -- it returns
        whether the session file was written instead, for callers that must not
        promise what the disk did not take."""
        from .components.hook_integration import is_compaction_system_message
        try:
            # System messages are rebuilt from config each turn and must not be
            # persisted — EXCEPT the ones that are compacted conversation
            # (archive pointers, the prune breadcrumb). Dropping those loses
            # conversation state for good: the content is in a store, but
            # nothing left in the session says it exists.
            # Volatile developer notes are dropped one level down, by
            # set_session_messages: five places write session messages and this
            # is only one of them.
            conversation_msgs = [
                msg for msg in messages
                if msg.role != "system" or is_compaction_system_message(msg)
            ]
            self._session_tracker.set_session_messages(session_id, conversation_msgs.copy())
            logger.debug(f"Persisted session {session_id} ({note}) with {len(conversation_msgs)} messages")
            if to_disk:
                return await self._save_session_to_disk(session_id)
        except Exception as e:
            logger.warning(f"Failed to persist session {session_id} ({note}): {e}", exc_info=True)
        return False

    async def next_internal_tool_request_id(self, base_request_id: str) -> str:
        """Return the next internal tool request id with a 3-digit suffix.

        This method is async and protected by an internal lock to ensure
        unique, monotonic counters for the lifetime of the Agent instance.
        """
        async with self._internal_tool_counter_lock:
            self._internal_tool_counter += 1
            return f"{base_request_id}_{self._internal_tool_counter:03d}"

    # ------------------------------------------------------------------
    # Unified Server Resolution (Central Method)
    # ------------------------------------------------------------------
    def _get_server_from_any_registry(self, server_name: str) -> Optional[ToolServer]:
        """Get a server from either plugin_registry or self.registry.

        This is the CENTRAL method for resolving servers. All code that needs to
        find a server should use this method instead of accessing registries directly.

        Search order:
        1. self.registry (contains ALL servers: plugins + config agents)
        2. plugin_registry (fallback for plugin adapters)

        Args:
            server_name: Name of the server to find (e.g., 'basic_operations', 'research_agent')

        Returns:
            The server instance or None if not found
        """
        registry = self.registry if hasattr(self, 'registry') else None
        return resolve_registry_server(registry, self._tool_integration_manager, server_name)

    # ------------------------------------------------------------------
    # Programmatic tool dispatch (used by tool_script and other in-process
    # callers that execute tools on the agent's behalf)
    # ------------------------------------------------------------------

    def _resolve_flat_tool_name(self, tool_name: str):
        """Resolve a flat tool name (e.g. 'v6_json_manage_json') to
        (server, server_name). Returns (None, None) if nothing matches.

        Order: exact server-name match (config agents / single-name servers),
        then longest-prefix match over '_'-joined segments (plugin tools), then
        the agent's own-tool prefix — mirroring ToolExecutionManager's paths.
        """
        server = self._get_server_from_any_registry(tool_name)
        if server:
            return server, tool_name
        server, candidate = resolve_longest_prefix(
            self._get_server_from_any_registry, tool_name)
        if server:
            return server, candidate
        if tool_name.startswith(f"{self.name}_"):
            return self, self.name
        return None, None

    def tool_dispatch_denial(self, tool_name: str, server_name: str) -> Optional[str]:
        """Why this agent may not dispatch *tool_name*, or None if it may.

        ONE definition of "may call", asked by two callers: ``dispatch_tool_call``
        raises it, and the chat's plugin commands ask it before LISTING a
        command, so /help never offers something that can only answer with a
        refusal. A second, drifting copy of these patterns is how a UI starts
        promising more than the agent has.

        Full fidelity to schema build: allowed first, then blocked, both matched
        on the same server/tool path by the SAME matcher schema build uses. No
        allowlist configured -> deny (schema build shows zero tools in that case
        too).
        """
        from .tool_schema_builder import tool_matches_patterns

        tools_config = getattr(self.agent_config, "tools", None) if getattr(
            self, "agent_config", None) else None
        allowed_patterns = list(getattr(tools_config, "allowed", None) or [])
        blocked_patterns = list(getattr(tools_config, "blocked", None) or [])
        if not allowed_patterns or not tool_matches_patterns(
                tool_name, server_name, allowed_patterns):
            return f"Tool '{tool_name}' is not in this agent's allowed tools."
        if blocked_patterns and tool_matches_patterns(
                tool_name, server_name, blocked_patterns):
            return f"Tool '{tool_name}' is blocked for this agent."
        return None

    async def dispatch_tool_call(self, tool_name: str, params: Dict[str, Any], *,
                                 session_id: Optional[str] = None,
                                 user_id: Optional[str] = None,
                                 request_id: Optional[str] = None) -> Any:
        """Execute one tool call programmatically with THIS agent's authorization.

        The in-process counterpart of the LLM tool path: same server resolution,
        same allowed/blocked pattern semantics as schema build (shared matcher
        ``tool_matches_patterns`` — what the LLM cannot see cannot be dispatched,
        in both directions), same runtime-param injection (shared
        ``inject_runtime_params``), same call_with_status/call dispatch.

        Used by the tool_script plugin ("scripted tool chains"); any future
        in-process caller (hooks, schedulers) should go through here as well.

        Raises ToolDispatchError with an agent-actionable message for unknown
        tools, unsupported tool types and authorization failures. Tool-level
        errors are returned as the tool's normal result (callers interpret the
        status convention themselves).
        """
        from .components.tool_execution import (
            FRAMEWORK_REQUEST_ID_KEYS, ToolDispatchError, inject_runtime_params)

        # External tools (dotted names) take a different execution branch
        # (MCP client sessions) that programmatic dispatch does not replicate.
        if "." in tool_name:
            raise ToolDispatchError(
                f"Tool '{tool_name}' is an external MCP tool — not supported in "
                f"programmatic dispatch (v1). Call it directly instead.")

        server, server_name = self._resolve_flat_tool_name(tool_name)
        if server is None:
            raise ToolDispatchError(
                f"Unknown tool: '{tool_name}'. Use the exact tool name from your "
                f"tool list.")

        denial = self.tool_dispatch_denial(tool_name, server_name)
        if denial is not None:
            raise ToolDispatchError(denial)

        # SECURITY: strip caller-supplied runtime params BEFORE injecting the
        # real ones — same guarantee the LLM tool path gives. This path is
        # driven by tool_script, whose call_tool forwards script-authored params
        # verbatim; a script could otherwise pass _session_id to impersonate
        # another agent and defeat json_store's owner-based write protection.
        # request_id/requestId likewise: status and cancellation route by them.
        forged = [k for k in params if k.startswith("_") or k in FRAMEWORK_REQUEST_ID_KEYS]
        if forged:
            logger.warning(
                "Dropping caller-supplied runtime param(s) %s from programmatic "
                "dispatch of %s", forged, tool_name)
            params = {k: v for k, v in params.items() if k not in forged}

        params = inject_runtime_params(
            params, session_id=session_id, user_id=user_id,
            request_id=request_id, agent=self)
        if request_id:
            params["request_id"] = params["requestId"] = request_id

        logger.info("Invoking tool %s via programmatic dispatch (agent=%s)",
                    tool_name, self.name)
        if hasattr(server, 'call_with_status'):
            result = await server.call_with_status(tool_name, params)
        else:
            result = await server.call(tool_name, params)
        logger.info("Tool %s returned (programmatic dispatch): %s",
                    tool_name, str(result)[:500])

        # A tool may stage a rewritten history via set_compacted_messages
        # (the summarizer's manual path does). During a run the request's own
        # machinery consumes that staging (the rebuild after tool execution /
        # _finalize_request) -- but a direct dispatch with no active request
        # (chat slash commands, web buttons) has no finalize: the staging sat
        # stale, /stats kept showing the old history, and the NEXT turn's
        # selection either discarded it or rebuilt the history around it.
        # Apply it here instead. "A request owns the session" == session lock
        # held; in-run dispatch (tool_script) therefore never takes this
        # branch. The live entry is refreshed too -- it outlives the previous
        # turn and is what stats-style readers see first.
        tracker = getattr(self, "_session_tracker", None)
        if session_id and tracker is not None:
            compacted = tracker.get_compacted_messages(session_id)
            if compacted is not None and not tracker.check_session_locked(session_id)[0]:
                tracker.set_session_messages(session_id, compacted)
                tracker.clear_compacted_messages(session_id)
                self._set_live_messages(session_id, compacted)
                try:
                    await self._save_session_to_disk(session_id)
                except Exception:
                    logger.warning(
                        "Compacted history for %s applied in memory but the "
                        "disk save failed", session_id, exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Prompt customization hook
    # ------------------------------------------------------------------
    def get_custom_system_prompt(self, context: Dict[str, Any]) -> Optional[str]:  # pragma: no cover - default noop
        """Subclass hook: return a fully rendered system prompt string or None.

        If a subclass returns a non-empty string here, that prompt is used as the
        primary system prompt and configuration driven template / raw prompt
        logic is skipped (except tools prompt injection which still occurs).

        Args:
            context: Dict containing keys like 'tools', 'max_steps', plus any
                     auto datetime context if enabled.

        Returns:
            The custom system prompt string or None to fall back to config logic.
        """
        return None

    #: Agents with fewer allowed steps get no step-budget note: a judge or
    #: extractor that answers in one call would read "finish now" as part of
    #: its task.
    _STEP_BUDGET_NOTE_MIN_STEPS = 5
    #: The note rides on this many of the last steps.
    _STEP_BUDGET_NOTE_LAST_STEPS = 2

    @staticmethod
    def _output_cap_note(completion_tokens: Optional[int]) -> ChatMessage:
        """What the run tells a model whose answer the output cap cut off.

        The RUN speaks (developer), as for the step budget: a person did not
        write this, and injected_by keeps it from counting as a turn.
        """
        at = f" ({completion_tokens} tokens)" if completion_tokens else ""
        return ChatMessage(
            role=DEVELOPER,
            content=(f"Your last answer was cut off at the output limit{at}. Everything after the cut is "
                     "lost, including any tool call you were writing -- nothing of it was executed. Do not "
                     "send it again in one piece: write a large file in parts (create it, then add section "
                     "by section), or keep the answer shorter."),
            timestamp=datetime.now(timezone.utc),
            injected_by="agent.output_cap",
        )

    @classmethod
    def _step_budget_note(cls, step: int, max_steps: int) -> Optional[ChatMessage]:
        """A note on the steps left, for the last steps of a run; else None.

        The step count used to sit in the system prompt ("Current step: 3/30").
        That prompt is re-rendered before every step and is the start of the
        prefix the provider caches, so every call re-billed the conversation
        behind it. The note goes into the history instead, like a loop
        intervention: sent only at the tail of one call, it would be missing
        from the next call's prefix, and a provider that caches at the last
        message (Anthropic) would find nothing to read back.

        step == max_steps is the final call after the budget, and it gets the
        max-steps request whatever the budget: it was sent to every agent
        before that call became a step of its own.
        """
        if step >= max_steps:
            return ChatMessage(
                role=DEVELOPER,
                content=(
                    f"You have reached the maximum number of steps ({max_steps}). "
                    "Please provide your final answer NOW based on the information you have gathered. "
                    "Do NOT use any tools in this response - just give me your best answer or summary of what you've accomplished."
                ),
                timestamp=datetime.now(timezone.utc),
                injected_by="agent.max_steps",
            )
        steps_left = max_steps - (step + 1)
        if max_steps < cls._STEP_BUDGET_NOTE_MIN_STEPS or steps_left >= cls._STEP_BUDGET_NOTE_LAST_STEPS:
            return None
        if steps_left == 0:
            note = (f"This is step {max_steps} of {max_steps}, the last one. Finish now with what "
                    f"your task requires, the final answer or the closing tool call, using what "
                    f"you have, and say what is still missing.")
        else:
            note = (f"Step {step + 1} of {max_steps}: {steps_left} step left after this one. "
                    f"Start wrapping up and do not begin new lines of work.")
        # Marked: hooks that look for the last message a person wrote (OKF seeds,
        # scripted follow-ups, tool preloads, compaction) must not take this one.
        return ChatMessage(role=DEVELOPER, content=note, timestamp=datetime.now(timezone.utc),
                           injected_by="agent.step_budget")

    # ------------------------------------------------------------------
    # Pre-LLM Message Selection
    # ------------------------------------------------------------------
    @staticmethod
    def _select_llm_messages(
        pre_hook_messages: List[ChatMessage],
        modified_messages: Optional[List[ChatMessage]],
    ) -> List[ChatMessage]:
        """Pick the final message list to send to the LLM.

        A hook that wants to change what the model sees returns a NEW list;
        list identity is the whole signal, and a hook mutating in place is not
        seen (tool_preload's own comment names this rule). The returned list
        carries every leading system message — the agent's own plus the ones
        hooks inject (pinned forum context, restoration hints, sub-agent
        context) — and the conversation, compacted if a hook compacted it.

        There used to be a second signal here: the ``compacted_messages``
        marker on the session tracker, rebuilt as ``[leading systems from
        pre-hook] + compacted``. That reconstruction is wrong whenever a hook
        injects a system block — it is built from the messages BEFORE the
        hooks ran, so it drops them; that is how the v5b synopsis moderator
        once looped 100× on ``context_engineer.recall()``. It was also
        unreachable: whoever sets that marker inside a hook returns a new list
        in the same round, and the tool path clears it in its own step.
        """
        if modified_messages is not None and modified_messages is not pre_hook_messages:
            return modified_messages
        return pre_hook_messages

    # ------------------------------------------------------------------
    # Central prompt rendering utilities (using strategy pattern)
    # ------------------------------------------------------------------
    def _render_prompts(
        self, 
        usable_tools: List[str], 
        max_steps: int, 
        current_step: int,
        session_id: Optional[str] = None
    ) -> tuple[str, Optional[str]]:
        """
        Render (system_prompt, tools_prompt) using strategy pattern.

        Args:
            usable_tools: List of tool names available to the agent
            max_steps: Maximum steps allowed for the agent
            current_step: Current step number (1-indexed, for dynamic per-step rendering)
            session_id: Optional session ID for session-scoped template vars

        Order of precedence:
          1. Subclass hook `get_custom_system_prompt`
          2. In-memory raw `agent_config.system_prompt`
          3. File/template based `agent_config.system_template`
          4. Default fallback

        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        # Get session-scoped template vars if session_id provided
        session_template_vars = None
        if session_id and hasattr(self, '_session_tracker') and self._session_tracker:
            session_template_vars = self._session_tracker.get_session_template_vars(session_id)
        
        renderer = PromptRenderer()
        context = PromptContext(
            agent_name=self.name,
            agent_config=self.agent_config,
            system_config=self.system_config,
            available_tools=usable_tools,
            max_steps=max_steps,
            current_step=current_step,
            agent_instance=self,  # Pass self for hook access
            session_template_vars=session_template_vars,  # Session-scoped vars (override agent_config)
            plugins=self._enabled_plugin_types(),
            mcp_servers=self._enabled_mcp_servers(),
        )
        return renderer.render(context)

    def _enabled_plugin_types(self) -> list[str]:
        """Plugin types installed and switched on (see ToolServerRegistry)."""
        plugin_types = getattr(getattr(self, "registry", None), "plugin_types", None)
        return plugin_types() if callable(plugin_types) else []

    def _enabled_mcp_servers(self) -> list[str]:
        """External MCP servers switched on, sorted -- not which are connected:
        a connection comes and goes, and the prompt must not change with it."""
        manager = getattr(self, "_tool_integration_manager", None)
        integration = getattr(manager, "tool_integration", None)
        if integration is None:
            return []
        return sorted(integration.configured_external_servers)

    async def get_current_system_prompt(self) -> str:
        """Async: render current system prompt (diagnostics endpoint)."""
        try:
            # Expanded as the run expands it, or `tools` in the prompt reads
            # server names here and tool names in every step that is sent.
            _schemas, usable_tools = await self._schemas_for(*await self.list_usable_tools())
        except Exception as e:
            logger.warning(f"Failed to list usable tools for system prompt: {e}", exc_info=True)
            usable_tools = []
        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
        system_msg, _ = self._render_prompts(usable_tools, max_steps, current_step=0)
        return system_msg


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

    async def cancel_request(self, request_id: str) -> bool:
        """
        Cancel an active request.

        Uses dual cancellation: global CancellationManager for tools +
        per-agent events for request loop. See docs/cancellation_architecture.md
        for design details.

        Args:
            request_id: The unique ID of the request to cancel

        Returns:
            True if the request was found and cancelled, False otherwise
        """
        return await self._request_manager.cancel_request(request_id)

    def _is_cancelled(self, request_id: Optional[str]) -> bool:
        """
        Check if a request has been cancelled.

        Args:
            request_id: The unique ID of the request to check

        Returns:
            True if the request has been cancelled, False otherwise
        """
        return self._request_manager.is_cancelled(request_id)
    
    async def append_user_message(self, request_id: str, content: str) -> bool:
        """
        Append a user message to an active request's conversation.
        Returns True if appended, False if request not found.
        """
        return await self._session_tracker.append_user_message(request_id, content)

    async def append_to_session(self, session_id: str, content: str) -> bool:
        """
        Append a user message directly to a persisted session.
        Returns True if appended, False if session not found.
        """
        return await self._session_tracker.append_to_session(session_id, content)

    # --- Per-session live conversation state (see __init__ for rationale) ----

    def _set_live_messages(self, session_id: Optional[str], messages: List[ChatMessage]) -> None:
        """Record the live message list for a session (request-scoped)."""
        # Keep the deprecated shared attr in sync for any unmigrated reader.
        self._current_messages = messages
        if not session_id:
            return
        entry = self._live_state_by_session.setdefault(session_id, {})
        entry["messages"] = messages
        self._evict_live_state(session_id)

    def _set_live_tools_schema(self, session_id: Optional[str], tools_schema: List[Dict[str, Any]]) -> None:
        """Record the live tool schema for a session (request-scoped)."""
        self._current_tools_schema = tools_schema
        if not session_id:
            return
        entry = self._live_state_by_session.setdefault(session_id, {})
        entry["tools_schema"] = tools_schema
        self._evict_live_state(session_id)

    def _evict_live_state(self, keep_session: str) -> None:
        """Bound the per-session live-state dict (simple FIFO eviction)."""
        if len(self._live_state_by_session) <= self._live_state_max_sessions:
            return
        for sid in list(self._live_state_by_session.keys()):
            if len(self._live_state_by_session) <= self._live_state_max_sessions:
                break
            if sid != keep_session:
                self._live_state_by_session.pop(sid, None)

    def get_live_messages(self, session_id: Optional[str]) -> Optional[List[ChatMessage]]:
        """Live (current-turn) messages for a session, or None if not tracked.

        Session-correct replacement for reading agent._current_messages. The
        caller should fall back to the persisted session tracker when this is
        None (e.g. a tool invoked outside an active step loop).
        """
        if session_id:
            entry = self._live_state_by_session.get(session_id)
            if entry and entry.get("messages") is not None:
                return entry["messages"]
        return None

    def get_live_conversation(self, session_id: Optional[str]) -> Optional[List[ChatMessage]]:
        """The in-flight messages of a session as a READER should see them.

        ``get_live_messages`` hands out the run's own working list: it opens
        with the rendered system prompt and carries the notes the run wrote for
        this one call. Persistence drops both -- the prompt is rebuilt per turn
        from config, a volatile note belongs to the call it was built for -- so
        a viewer joining a session mid-run must have them dropped too, or the
        chat shows the agent its own system prompt as a message.

        Both rules are the ones persistence uses, not copies of them: a system
        message that IS conversation (an archived_ref, a prune breadcrumb) stays
        by ``is_compaction_system_message``, and the volatile notes go by
        ``is_volatile_note``.
        """
        live = self.get_live_messages(session_id)
        if live is None:
            return None
        from .components.hook_integration import is_compaction_system_message
        from .components.session_tracking import is_volatile_note
        return [msg for msg in live
                if not is_volatile_note(msg)
                and (getattr(msg, "role", None) != "system"
                     or is_compaction_system_message(msg))]

    def get_live_tools_schema(self, session_id: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """Live tool schema for a session, or None if not tracked."""
        if session_id:
            entry = self._live_state_by_session.get(session_id)
            if entry and entry.get("tools_schema") is not None:
                return entry["tools_schema"]
        return None

    async def _drain_appended_messages(self, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Drain any appended messages for a request and add them to the conversation.
        Returns the updated messages list.
        """
        return await self._session_tracker.drain_appended_messages(request_id, messages)

    # ------------------------------------------------------------------
    # Tool filtering helpers
    # ------------------------------------------------------------------
    def _is_tool_allowed(self, tool_name: str, patterns: list[str]) -> bool:
        """Return True if the discovery-stage name matches any allowed pattern.

        Thin delegate to the SINGLE shared discovery-stage matcher
        ``tool_schema_builder.server_matches_patterns`` (see its docstring for
        pattern semantics). Kept as a method for existing callers/tests.
        NOTE: empty patterns now mean deny-all (security by default) — all
        production callers guard for non-empty patterns before calling.
        """
        return server_matches_patterns(tool_name, patterns)

    async def list_usable_tools(self) -> tuple[list[str], list[str] | None, list[str] | None]:
        """Return list of tool names this agent CAN USE (filtered by agent config).

        This is the INTERNAL interface - tools available for this agent's execution.
        Filtered by agent_config.tools.allowed patterns.

        Contrast with list_tools() which returns what this agent OFFERS to others.

        Returns:
            Tuple of:
            - List of tool server names this agent is allowed to use
            - List of allowed patterns (for fine-grained filtering after tool expansion)
            - List of blocked patterns (to be applied after tool expansion)
        """
        # Initialize tool integration (idempotent)
        await self._tool_integration_manager.setup_tool_integration()

        # Use ToolDiscoveryService for clean tool filtering
        discovery_service = ToolDiscoveryService(
            agent_name=self.name,
            agent_config=self.agent_config,
            tool_integration_manager=self._tool_integration_manager,
            registry=self.registry if hasattr(self, 'registry') else None
        )

        return await discovery_service.discover_allowed_tools()

    async def describe_context_inputs(
        self, session_id: Optional[str] = None
    ) -> tuple[str, list[Dict[str, Any]]]:
        """(system prompt, tool schemas) as they go into a call of *session_id*.

        What sits in the context window before the conversation does, and what
        `/context` counts. Two things matter about it:

        * The prompt is rendered WITH the session's template vars, because
          that is the prompt the session really sends -- rendered without
          them it is short by the whole var payload, on the one line the
          command exists to show.
        * One discovery, not two. Both halves need the list of usable tools,
          and asking for it twice means awaiting list_tools() on every
          registered server a second time.
        """
        tools_schema, usable_tools = await self._schemas_for(*await self.list_usable_tools())
        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
        system_msg, _ = self._render_prompts(
            usable_tools, max_steps, current_step=0, session_id=session_id)
        return system_msg, tools_schema

    async def _schemas_for(self, usable_tools: list[str],
                           allowed_patterns: Optional[list[str]],
                           blocked_patterns: Optional[list[str]]
                           ) -> tuple[list[Dict[str, Any]], list[str]]:
        """The LLM schemas for an ALREADY discovered set of tools, and the
        tool names after expansion and filtering -- what a run renders as
        ``tools``."""
        schema_builder = ToolSchemaBuilder(
            agent_name=self.name,
            tool_integration_manager=self._tool_integration_manager,
            server_getter_func=self._get_server_from_any_registry,
        )
        tools_schema, _mapping, usable, _display = await schema_builder.build_schemas(
            usable_tools,
            allowed_patterns=allowed_patterns,
            blocked_patterns=blocked_patterns,
        )
        return list(tools_schema), usable

    async def build_llm_tool_schemas(self) -> list[Dict[str, Any]]:
        """The tool schemas this agent hands the model, exactly as they go out.

        EXACTLY the pipeline that builds the LLM schema -- discovery (deny-all
        on empty allowed, _tool_visible, externals) plus ToolSchemaBuilder
        (tool-level allow/block, both tool interfaces, every schema dialect).
        A caller that re-implements half of it diverges on every point it
        skips: hybrid plugins (list_tools-only) went missing entirely, an
        empty allowlist meant allow-all in one place and deny-all in the
        other, and blocked patterns were never applied -- an agent asking what
        it could do got a different answer than the schema it ran with.

        Whole schemas, parameters and all: what a listing needs is the name,
        what a token count needs is the rest, and the parameters are usually
        the larger half of both.
        """
        schemas, _usable = await self._schemas_for(*await self.list_usable_tools())
        return schemas

    async def _list_usable_tools_with_details(self, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Return detailed info about tools this agent CAN USE (name + description).

        Internal utility for agent subclasses (e.g., BasicAgent's list_available_tools).
        Like list_usable_tools() but includes descriptions for user-facing output.

        Args:
            params: Parameters including optional '_status' for progress reporting

        Returns:
            List of dicts with 'name' and 'description' keys

        Raises whatever the listing raises. It used to answer every failure
        with an empty list, which every caller reads as "this agent has no
        tools" -- the chat even named the cause: an empty allowlist.
        """
        status = params.get("_status")
        tools_schema = await self.build_llm_tool_schemas()

        all_tools: list[Dict[str, Any]] = []
        for entry in tools_schema:
            function = entry.get("function", {}) if isinstance(entry, dict) else {}
            if not isinstance(function, dict):
                continue
            all_tools.append({
                "name": function.get("name", "unknown"),
                "description": function.get("description", "") or "",
            })

        if status:
            await status.end(f"Listed available tools ({len(all_tools)} tools)")

        return all_tools

    async def run_events(
        self,
        task: Union[str, ChatMessage],
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        llm_override: Optional[LLMClient] = None,
        llm_profile_info_override: Optional[str] = None,
        use_advanced_model: bool = False
    ):
        """Run the agent and yield structured events for UI streaming.

        Args:
            task: Either a string task description or a ChatMessage with multimodal content
            request_id: Optional request ID for tracking
            session_id: Optional session ID for conversation history
            llm_override: Optional LLM client to use instead of self.llm (for per-request profile overrides)
            llm_profile_info_override: Optional profile info string for status display (e.g., "turbo:openai_httpx/gpt-5-nano")
            use_advanced_model: If True and llm_override not set, use best available LLM profile
        """

        # Generate request ID if not provided
        if request_id is None:
            request_id = short_id()

        # Track if this is a newly generated session
        was_new_session = not session_id
        
        # If no session_id provided, generate one and persist empty history
        if not session_id:
            session_id = short_id()

        # CRITICAL: Set session metadata for newly generated sessions AND ensure it exists for existing ones
        # This ensures user_id is available for tool execution even in sub-agents
        if self._session_tracker:
            existing_metadata = self._session_tracker.get_session_metadata(session_id)
            if not existing_metadata:
                # Extract user_id from the request ownership map
                # (populated by API layer / tool execution / sub_agent_manager)
                from ...core.request_context import get_request_user
                user_id = get_request_user(request_id)
                
                # Use agent's default llm_profile for metadata
                effective_llm_profile = self.agent_config.default_llm_profile if self.agent_config else "normal"
                self._session_tracker.set_session_metadata(session_id, {
                    "user_id": user_id,
                    "agent_name": self.name,
                    "llm_profile": effective_llm_profile
                })
                if was_new_session:
                    logger.debug(f"Set session metadata for new session {session_id}: user_id={user_id}")
                else:
                    logger.debug(f"Set session metadata for existing session {session_id} (was missing): user_id={user_id}")

        # Handle Union[str, ChatMessage] input
        initial_message: Optional[ChatMessage] = None
        task_text: str = ""

        if isinstance(task, ChatMessage):
            # Extract task text from ChatMessage content for logging/tracking
            initial_message = task
            if isinstance(task.content, str):
                task_text = task.content
            elif isinstance(task.content, list):
                # Extract text from content items (Pydantic models, not dicts)
                text_parts = [getattr(item, "text", "") for item in task.content if hasattr(item, "type") and getattr(item, "type") == "text"]
                task_text = " ".join(text_parts) if text_parts else "[multimodal input]"
            else:
                task_text = "[multimodal input]"
        else:
            task_text = task

        # Create suffixed request IDs for coordinator and worker so their
        # status messages can be correlated separately while still linking
        # back to the base request_id. Use the Agent's centralized counter
        # to ensure monotonic, global numbering across components.
        coordinator_request_id = await self.next_internal_tool_request_id(request_id) if request_id else None
        worker_request_id = await self.next_internal_tool_request_id(request_id) if request_id else None

        # Handle use_advanced_model if no llm_override provided
        if use_advanced_model and not llm_override:
            from agent_system.llm.factory import override_for_profile

            # Ketten-Semantik: Advanced-Modell = llm_profile_advanced[0].
            # Keine Advanced-Kette konfiguriert oder advanced == default
            # (kein echtes Upgrade) → no-op (normale Kette läuft).
            advanced_profile = self.agent_config.advanced_llm_profile if self.agent_config else None
            if advanced_profile and self.agent_config and \
                    advanced_profile == self.agent_config.default_llm_profile:
                advanced_profile = None
            if advanced_profile:
                try:
                    llm_override, llm_profile_info_override = override_for_profile(
                        self.system_config, self.agent_config, advanced_profile)
                    logger.info(f"use_advanced_model=True mapped to profile: {llm_profile_info_override}")

                except Exception as e:
                    logger.error(f"Failed to create LLM override for use_advanced_model: {e}")
                    # Continue with default LLM

        # The caller's LLM (agent_config.inherit_parent_llm): after the choices
        # made for this very run, which win over it -- an override passed in, or
        # use_advanced_model where the agent has an advanced chain to go to.
        if llm_override is None:
            from_caller = self._llm_from_caller()
            if from_caller is not None:
                llm_override, llm_profile_info_override = from_caller

        # Create and start status forwarder BEFORE entering status_scope context managers
        # This ensures the forwarder is subscribed to status_bus before any START events are generated
        # Fixes race condition where status_scope generates events before forwarder is ready
        status_forwarder = StatusEventForwarder()
        await status_forwarder.start_forwarding(request_id)

        # Wire LLM hooks to llm_override if provided (per-request LLM clients
        # won't have hooks from __init__ since they are freshly created)
        if llm_override is not None and self._hook_manager:
            self._hook_manager.wire_llm_hooks(llm_override)

        # Set per-agent app identity on override LLMs (they bypass __init__'s
        # set_app_title call since they are freshly created by CLI --llm or
        # use_advanced_model).
        if llm_override is not None and hasattr(llm_override, 'set_app_title'):
            llm_override.set_app_title(self.name)

        # Start a background checkpoint loop so long-running tool calls don't
        # leave the session unsaved on disk. The loop persists messages up to
        # the last consistent tool_call/tool_result boundary, so the file is
        # always reload-safe (orphan-free).
        checkpoint_session_id: Optional[str] = None
        checkpoint_user_id: Optional[str] = None
        if self._session_service and session_id and self._session_tracker is not None:
            try:
                meta = self._session_tracker.get_session_metadata(session_id) or {}
                checkpoint_user_id = meta.get("user_id", "anonymous")
                self._session_service.start_checkpoint_loop(self, checkpoint_user_id, session_id)
                checkpoint_session_id = session_id
            except Exception as e:
                logger.debug(f"Could not start checkpoint loop for session {session_id}: {e}")

        # The run sets its request id and its user in the context it runs in -- the
        # caller's: this generator runs in whoever iterates it. Its cleanup resets
        # them once a conversation context was built; a setup that failed before, or
        # a consumer that closed the stream early, left them to the caller. What they
        # were is what they are again when this generator ends -- and at "end", its
        # last event: a consumer that stops there (app, agent_service) never closes
        # it, and the garbage collector closes it in another task.
        from ...core.request_context import current_run_user
        request_before, user_before = current_request_id.get(), current_run_user.get()
        try:
            # Pass status_scope parameters to _run_events which will open them AFTER
            # sending the 'start' event - this ensures frontend has currentRequestId
            # before any status events arrive
            async for event in self._run_events(
                task_text,
                request_id=request_id,
                session_id=session_id,
                coordinator_request_id=coordinator_request_id,
                worker_request_id=worker_request_id,
                initial_message=initial_message,
                llm_override=llm_override,
                llm_profile_info_override=llm_profile_info_override,
                status_forwarder=status_forwarder,
                use_advanced_model=use_advanced_model,
            ):
                # Before the yield: the consumer of a sub-run can stop reading at its
                # end, error or cancel (sub_agent_manager does), and the event it
                # stops at would never be relayed.
                relay_run_event(status_forwarder, event, self.name)
                if event.get("type") == "end":
                    current_request_id.set(request_before)
                    current_run_user.set(user_before)
                yield event
        except GeneratorExit:
            # Generator is being closed early - clean exit without error
            raise
        finally:
            # Always remove the status_forwarder's handler from the global
            # status_bus. _finalize_request only does this when a context was
            # built (context.status_forwarder), so failure paths that return
            # before context assignment - session-lock failure, 'No LLM
            # available', any exception in _initialize_request_and_conversation
            # - would otherwise leak the handler permanently (it accumulates on
            # the shared bus and every future publish() invokes the dead
            # handler). stop_forwarding() is idempotent, so the normal-path
            # call inside _finalize_request remains harmless.
            try:
                await status_forwarder.stop_forwarding()
            except Exception as e:
                logger.debug(f"Failed to stop status_forwarder for {request_id}: {e}")
            if checkpoint_session_id and self._session_service:
                try:
                    await self._session_service.stop_checkpoint_loop(checkpoint_session_id)
                except Exception as e:
                    logger.debug(f"Failed to stop checkpoint loop for {checkpoint_session_id}: {e}")
            current_request_id.set(request_before)
            current_run_user.set(user_before)

    async def _initialize_request_and_conversation(
        self,
        task: str,
        request_id: str,
        session_id: str,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[LLMClient] = None,
        status_forwarder: Optional[StatusEventForwarder] = None
    ) -> ConversationContext:
        """Initialize request tracking and build initial conversation context.

        Phase 1 of agent execution: Setup all state needed for the LLM loop.

        Steps:
        1. Create cancellation token
        2. Register request for cancellation/appends
        3. Set context vars
        4. Initialize session storage
        5. Use pre-created status event forwarder (passed from caller)
        6. Validate LLM availability
        7. Initialize tool integration
        8. Discover usable tools
        9. Build tool schemas (the expanded tool list the prompt renders)
        10. Render system prompts
        11. Load session history
        12. Execute session start hooks

        Args:
            task: User task description
            request_id: Unique request identifier
            session_id: Session identifier for history
            initial_message: Optional multimodal message
            llm_override: Optional LLM client override
            status_forwarder: Pre-created status event forwarder (must be started before status_scope)

        Returns:
            ConversationContext with all initialized state

        Raises:
            RuntimeError: If no LLM is available
        """
        # Determine which LLM to use
        active_llm = llm_override if llm_override is not None else self.llm

        # Create cancellation token for the main request
        cancellation_manager = get_cancellation_manager()
        main_token = cancellation_manager.create_token(request_id)
        logger.debug("Created main cancellation token for request %s", request_id)

        # Set the ContextVar so any publish_status() calls without explicit request_id
        # will inherit the current request id. Store token for reset in finally.
        try:
            context_reset_token = current_request_id.set(request_id)
        except Exception as e:
            logger.debug(f"Failed to set current_request_id context var: {e}")
            context_reset_token = None
        # Whose run this is, for the calls in it that reach the hooks without an
        # agent (a decision, TTS: llm/hook_notify.py) -- also once the request
        # that started a background sub-agent has ended and let go of its tree.
        from ...core.request_context import current_run_user
        metadata = self._session_tracker.get_session_metadata(session_id) if self._session_tracker else None
        run_user = metadata.get("user_id") if isinstance(metadata, dict) else None
        user_reset_token = current_run_user.set(run_user) if run_user else None

        # Status forwarder is passed in from caller (created before status_scope context managers)
        # This ensures forwarder is subscribed to status_bus before any START events are generated
        # Fallback to creating one here for backwards compatibility (though this defeats the purpose)
        if status_forwarder is None:
            logger.warning("status_forwarder not passed to _initialize_request_and_conversation - creating one here (may miss events)")
            status_forwarder = StatusEventForwarder()
            await status_forwarder.start_forwarding(request_id)

        # If no LLM is configured, raise error
        if active_llm is None:
            raise RuntimeError("No LLM available; agent requires an LLM to run")

        # Initialize tool integration
        await self._tool_integration_manager.setup_tool_integration()

        # Get tools this agent can use (filtered by agent_config)
        # Returns tuple: (tools, allowed_patterns, blocked_patterns)
        usable_tools, allowed_patterns, blocked_patterns = await self.list_usable_tools()

        # Build tool schemas using ToolSchemaBuilder
        # Pass allowed_patterns and blocked_patterns so they can be applied AFTER tools are expanded
        # Before the first render: `tools` in the prompt is the EXPANDED list
        # from here on, as in every step's re-render. Rendered from the
        # server-level list, the first prompt branched differently from the
        # ones sent (the session-start hooks saw that one).
        schema_builder = ToolSchemaBuilder(
            agent_name=self.name,
            tool_integration_manager=self._tool_integration_manager,
            server_getter_func=self._get_server_from_any_registry
        )

        tools_schema, tool_name_mapping, usable_tools, display_tools = await schema_builder.build_schemas(
            usable_tools,
            allowed_patterns=allowed_patterns,
            blocked_patterns=blocked_patterns
        )

        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))

        # Centralized prompt rendering (system + optional tools) using helper.
        # Initial render with step 0 (before loop starts)
        # Pass session_id to use session-scoped template vars
        system_msg, tools_msg = self._render_prompts(usable_tools, max_steps, current_step=0, session_id=session_id)

        # Initialize conversation from persisted session history
        session_msgs = self._session_tracker.get_session_messages(session_id)

        # Create initial system messages
        messages = [ChatMessage(role="system", content=system_msg)]
        if tools_msg:
            messages.append(ChatMessage(role="system", content=tools_msg))

        # Execute session start hooks for new sessions AFTER creating system messages
        # This allows hooks like markdown_formatter to inject additional system prompts
        # A session starts once: one taken back to no messages (/undo) is not new again
        if self._session_tracker.start_session(session_id):
            modified_messages = await self._hook_manager.execute_session_start_hooks(
                session_id, request_id, messages=messages
            )
            if modified_messages is not None:
                messages = modified_messages
                logger.debug(f"Session start hooks modified messages: {len(messages)} total messages")

        # include persisted session messages
        if session_msgs:
            # Convert dicts to ChatMessage objects if needed. Persisted
            # history may predate history_safe_tool_calls (or was written by
            # an older build) -- sanitize on load, or a session poisoned by
            # invalid arguments JSON stays dead on every resume.
            for msg in session_msgs:
                if isinstance(msg, dict):
                    if msg.get("tool_calls"):
                        msg = {**msg, "tool_calls":
                               history_safe_tool_calls(msg["tool_calls"])}
                    messages.append(ChatMessage(**msg))
                else:
                    if getattr(msg, "tool_calls", None):
                        msg.tool_calls = history_safe_tool_calls(msg.tool_calls)
                    messages.append(msg)

        # add the new user input as last message
        # Use initial_message if provided (for multimodal input), otherwise create from task
        opening = initial_message or ChatMessage(
            role="user", content=sanitize_for_llm(task), timestamp=datetime.now(timezone.utc))
        # The run's own id on the first message it stores (ChatMessage.request_id): a
        # session read back tells its runs apart by it, and finds a sub-agent's run
        # under the call that started it.
        opening.request_id = request_id
        messages.append(opening)

        # Also include any appended messages already queued for this request
        messages = await self._session_tracker.drain_appended_messages(request_id, messages)

        # Track live messages for this session (request-scoped)
        self._set_live_messages(session_id, messages.copy())

        # Track current tool schemas per-session for token estimation by hooks
        self._set_live_tools_schema(session_id, tools_schema)

        # Return initialized context
        return ConversationContext(
            messages=messages,
            available_tools=usable_tools,  # Use usable_tools for validation (includes both server names and tool names)
            tools_schema=tools_schema,
            tool_name_mapping=tool_name_mapping,
            max_steps=max_steps,
            main_token=main_token,
            context_reset_token=context_reset_token,
            status_forwarder=status_forwarder,
            session_id=session_id,
            user_reset_token=user_reset_token,
        )

    def _presence_hold(self, session_id: str, request_id: str) -> None:
        """Session presence (core/session_presence.py): the request holds its
        session for as long as it runs -- from its start, not from its first
        LLM call. A client that disconnects while the run is still setting
        itself up lets go of the endpoint's hold, and the session would look
        idle while it runs on: a direct message would wake a second run of it."""
        presence = presence_for(self.system_config)
        if presence is None or not session_id or request_id in self._presence_holds:
            return
        try:
            metadata = self._session_tracker.get_session_metadata(session_id) or {}
            user_id = metadata.get("user_id")
            if not user_id:
                from ...core.request_context import get_request_user
                user_id = get_request_user(request_id)
            held = None
            try:
                if presence.hold(session_id, user_id, self.name, run=request_id):
                    held = (presence, session_id, user_id)
            except SessionBusy as busy:
                # Forced past the refusal at the entry point. The input waiting
                # belongs to the process that holds the session.
                logger.warning("%s; this request runs it unheld", busy)
            self._presence_holds[request_id] = held
        except Exception as e:
            logger.warning("Session presence: holding %s failed: %s", session_id, e)

    def _presence_step(self, session_id: str, request_id: str) -> None:
        """Every LLM call takes the input waiting for the session -- the pre-LLM
        hooks hand it over -- as long as this request holds the session."""
        self._presence_hold(session_id, request_id)
        held = self._presence_holds.get(request_id)
        if not held:
            return
        presence, sid, user_id = held
        try:
            presence.take_pending(sid, user_id)
        except Exception as e:
            logger.warning("Session presence: step of %s failed: %s", session_id, e)

    def _presence_release(self, request_id: str) -> None:
        held = self._presence_holds.pop(request_id, None)
        if held is None:
            return
        presence, session_id, user_id = held
        try:
            presence.release(session_id, user_id)
        except Exception as e:
            logger.warning("Session presence: releasing %s failed: %s", session_id, e)

    async def _take_in_late_messages(self, request_id: str, session_id: Optional[str],
                                     messages: List[ChatMessage]) -> List[ChatMessage]:
        """Messages appended too late for the run to act on, into its conversation.

        The live copy is refreshed with them: it was taken at the run's final, and the
        session load serves it until the run's job has ended -- without them the chat
        showed the turn with the message missing that its note said was kept.
        """
        held = len(messages)
        messages = await self._session_tracker.drain_appended_messages(request_id, messages)
        if len(messages) > held:
            self._set_live_messages(session_id, messages.copy())
        return messages

    async def _finalize_request(
        self,
        request_id: str,
        session_id: str,
        status_coordinator: StatusScope,
        status_worker: StatusScope,
        context: Optional[ConversationContext],
        messages: Optional[List[ChatMessage]],
        results: Dict[str, Any],
        step: int
    ) -> None:
        """Finalize request and clean up resources.

        Phase 3 of agent execution: Cleanup and persistence.

        Steps:
        1. Execute session end hooks
        2. Unregister cancellation token
        3. Clean up request tracking
        4. Persist session messages (conversation history only)
        5. Shutdown tool integration
        6. Reset context vars
        7. Publish final status events
        8. Stop status forwarding
        9. Yield final pending status events

        Args:
            request_id: Request identifier
            session_id: Session identifier
            status_coordinator: Coordinator status scope
            status_worker: Worker status scope
            context: Conversation context (if initialized)
            messages: Final conversation messages
            results: Execution results dictionary
            step: Final step number
        """
        # First, before anything that awaits: a cancellation there would leave the
        # finished run's model registered for the session's next /compact.
        self._step_llms.pop(session_id, None)

        # Flush injected user messages that arrived too late to be processed
        # (e.g. during the very last LLM call) into the conversation so they
        # persist with the final save instead of being dropped with the request
        # entry. They are answered by the next run on this session.
        if messages is not None:
            try:
                messages = await self._take_in_late_messages(request_id, session_id, messages)
            except Exception as e:
                logger.debug("Failed to flush appended messages for %s: %s", request_id, e)

        # Clean up cancellation token
        cancellation_manager = get_cancellation_manager()
        cancellation_manager.unregister_request(request_id)

        # Clean up request tracking but preserve session data
        self._request_manager.unregister_active_request(request_id)
        logger.debug("Cleaned up request tracking for %s", request_id)

        # Release session lock BEFORE persisting (allows other requests to proceed)
        # Note: unregister_request also releases the lock, but we do it explicitly here
        # for clarity and to ensure it happens before session persistence
        sid = self._session_tracker.get_session_for_request(request_id)

        # Stop the background checkpoint loop BEFORE the final save. The loop
        # does its own load-modify-save every ~30s; if it overlaps the final
        # save it can resume after we persist and write its older, trimmed
        # snapshot over the newer one (silent message loss). Cancelling and
        # awaiting the task here guarantees any in-flight checkpoint write has
        # completed, so the final save below writes last and wins. Idempotent:
        # the outer run_events finally also calls stop_checkpoint_loop.
        if sid and self._session_service:
            try:
                await self._session_service.stop_checkpoint_loop(sid)
            except Exception as e:
                logger.debug(f"Failed to stop checkpoint loop for {sid} before final save: {e}")

        if sid:
            await self._session_tracker.release_session_lock(sid, request_id)
            logger.debug("Released session lock for %s (request %s)", sid, request_id)

        # Persist session messages and keep the request->session mapping for a while
        persisted = False
        if sid and messages:
            try:
                # Check if ANY tool modified the session messages during this request
                # Tools can call session_tracker.set_compacted_messages() to replace the history
                compacted_msgs = self._session_tracker.get_compacted_messages(sid)

                if compacted_msgs is not None:
                    # A tool replaced the message history - use those messages for
                    # persistence (already conversation-only, no filtering needed)
                    logger.debug(
                        f"Using {len(compacted_msgs)} tool-modified messages for session {sid} "
                        f"(request had {len(messages)} messages)"
                    )
                    self._session_tracker.set_session_messages(sid, compacted_msgs)
                    self._session_tracker.clear_compacted_messages(sid)
                    # Save to disk even if no SSE client is connected
                    # (e.g., browser disconnected during background job execution)
                    persisted = await self._save_session_to_disk(sid)
                else:
                    # Normal case: persist the request's conversation messages
                    persisted = await self._persist_conversation(
                        sid, messages, to_disk=True, note="at end of request")

                # Keep the request->session mapping (don't pop it immediately)
                # This allows append requests that arrive shortly after completion to find the session
            except Exception as e:
                logger.warning(f"Failed to persist session {sid}: {e}", exc_info=True)

        # Session end hooks AFTER the save, and told whether it happened. A hook
        # that counts what the request carried as delivered -- debate_forum does
        # that for direct messages -- would otherwise count it while the
        # conversation is still only in memory: a save that fails or is
        # cancelled would take the message with it and nothing would re-deliver
        # it. They read the conversation, none of them writes it, so running
        # them after the save changes nothing else.
        try:
            await self._hook_manager.execute_session_end_hooks(
                session_id, request_id, messages=messages, persisted=persisted
            )
        except Exception as e:
            logger.warning(f"Session end hooks failed: {e}", exc_info=True)

        # NOTE: no MCP shutdown here. The integration is process-wide state;
        # tearing it down at the end of EVERY request broke bootstrap-only
        # processes (writer pipelines) after their first request, because
        # ToolServerIntegration.shutdown() stops all plugins but leaves
        # `initialized` True -- so the next request found a half-dead
        # integration and never re-initialized it. Shutdown belongs to the
        # process entry point (shutdown_tools), never to an agent.

        # Reset the current_request_id ContextVar so it doesn't leak to other tasks
        if context and context.context_reset_token is not None:
            try:
                current_request_id.reset(context.context_reset_token)
            except Exception:
                pass
        if context and context.user_reset_token is not None:
            from ...core.request_context import current_run_user
            try:
                current_run_user.reset(context.user_reset_token)
            except Exception:
                pass

        # Signal completion using status contexts
        # Note: step is already 1-indexed (extracted from events that use step+1)
        final_msg = "completed" if not results.get('errors') else "completed with errors"
        step_display = step if step > 0 else 1  # Ensure at least 1 step shown

        if results.get('errors'):
            await status_worker.error(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.error(f"{final_msg} ({step_display} steps)", meta={"summary": results.get('summary')})
        else:
            await status_worker.end(final_msg, meta={"summary": results.get('summary')})
            await status_coordinator.end(f"{final_msg} ({step_display} steps)", meta={"summary": results.get('summary')})

        # Give a small delay to allow final status events to be processed by the forwarding task
        await asyncio.sleep(0.01)

        # Clean up status forwarding task AFTER publishing final status
        if context and context.status_forwarder:
            await context.status_forwarder.stop_forwarding()

    async def _call_llm_with_streaming(
        self,
        llm: LLMClient,
        messages: List[ChatMessage],
        tools_schema: List[Dict[str, Any]],
        cancellation_token: CancellationToken,
        step: int,
        yield_pending_status_fn,
        status_scope: Optional[StatusScope] = None,
        watch_reasoning: bool = True,
        on_reasoning_progress=None,
    ):
        """Call LLM with streaming support and interleaved status events.

        This method uses chat_tools_streaming() when available, yielding token deltas
        and checking status events between chunks. For non-streaming LLMs, falls back
        to regular chat_tools() with periodic status polling.

        Args:
            llm: LLM client instance
            messages: Conversation messages
            tools_schema: Available tools schema
            cancellation_token: Cancellation token for interruption
            step: Current step number
            yield_pending_status_fn: Function that yields pending status events
            status_scope: Optional status scope for LLM to report progress (batch status, etc.)
            on_reasoning_progress: Optional ``async (text, chars, previous_chars)``,
                awaited every _REASONING_PROGRESS_TICK characters of thinking

        Yields:
            - {"type": "thinking_delta", "step": int, "delta": str, "accumulated": str}
            - {"type": "status", ...}
            - {"type": "thinking_complete", "assistant": {...}}
        """
        if llm.supports_streaming():
            # Streaming LLM: zero-overhead real-time tokens
            accumulated_content = []
            final_assistant = None
            final_usage = None  # Store usage data from final chunk
            final_finish_reason = None  # "length", "content_filter", ...
            # Detectors per CALL: they hold this call's thinking, and a retry
            # must start from empty windows. Two of them — a short window for
            # short-period loops and a long one for the periods the short
            # window cannot span; see build_detectors.
            reasoning_detectors = build_detectors(
                # Off for the retry the detector itself asked for: watching
                # the second attempt too would mean a second abort policy,
                # and there is nothing sensible left to do after it.
                enabled=bool(self._reasoning_loop_config["enabled"]) and watch_reasoning,
                repetition_threshold=float(
                    self._reasoning_loop_config["repetition_threshold"]))
            # Thinking of THIS call, for llm_progress hooks; a retry starts empty.
            reasoning_parts: List[str] = []
            reasoning_chars = 0
            reasoning_ticked_at = 0

            async for chunk in llm.chat_tools_streaming(
                messages, tools_schema,
                cancellation_token=cancellation_token,
                status_scope=status_scope
            ):
                chunk_type = chunk.get("type")

                if chunk_type == "thinking_delta":
                    # Gemini reasoning/thinking tokens (not content)
                    yield {"type": "reasoning_delta", "step": step + 1, "delta": chunk["delta"]}

                    # Watch the thinking for a loop. What arrives here is
                    # whatever the client calls a thinking delta: raw
                    # reasoning for most models, and for the OpenAI family a
                    # SUMMARY of it — the threshold was calibrated on raw
                    # reasoning, so for those models this guards the
                    # degenerate case rather than measuring a known shape.
                    for _detector in reasoning_detectors:
                        loop_reason = _detector.record(chunk["delta"])
                        if loop_reason:
                            logger.warning(
                                "[%s] Aborting the call: %s (model=%s, %d characters "
                                "of thinking so far)",
                                self.name, loop_reason, getattr(llm, "model", "?"),
                                _detector.characters_seen)
                            raise ReasoningLoopError(
                                loop_reason,
                                characters=_detector.characters_seen)

                    if on_reasoning_progress is not None:
                        reasoning_parts.append(chunk["delta"])
                        reasoning_chars += len(chunk["delta"])
                        if reasoning_chars - reasoning_ticked_at >= _REASONING_PROGRESS_TICK:
                            try:
                                await on_reasoning_progress(
                                    "".join(reasoning_parts), reasoning_chars,
                                    reasoning_ticked_at)
                            except Exception as exc:  # an observer never breaks the call
                                logger.warning("[%s] llm_progress hooks failed: %s",
                                               self.name, exc)
                            reasoning_ticked_at = reasoning_chars

                    # Check status events after each token (zero overhead)
                    for status_event in yield_pending_status_fn():
                        yield status_event

                elif chunk_type == "content_delta":
                    # Yield token delta for real-time display
                    yield {"type": "thinking_delta", "step": step + 1, "delta": chunk["delta"], "accumulated": chunk["accumulated"]}
                    accumulated_content.append(chunk["delta"])

                    # Check status events after each token (zero overhead)
                    for status_event in yield_pending_status_fn():
                        yield status_event

                elif chunk_type == "tool_call_delta":
                    # Tool calls are accumulated server-side, we can skip yielding deltas for now
                    # Future: could yield tool_call_delta events for UI to show "Calling get_weather..."
                    # Still yield status events to prevent delays
                    for status_event in yield_pending_status_fn():
                        yield status_event

                elif chunk_type == "final":
                    final_assistant = chunk["assistant"]
                    # Preserve usage data from final chunk
                    if "usage" in chunk:
                        final_usage = chunk["usage"]
                    if chunk.get("finish_reason"):
                        final_finish_reason = chunk["finish_reason"]

            # Yield any remaining status events after streaming completes
            for status_event in yield_pending_status_fn():
                yield status_event

            # The streaming assembler builds its assistant dict itself and
            # never produces the "error" key that httpx _format_response sets
            # for a content filter -- so the fallback-profile switch was
            # unreachable while streaming. A filter is deterministic per
            # content: asking the same model again repeats the refusal, so it is
            # an error. A missing [DONE] (below) is a hiccup, so it is not.
            _content_filter_as_error(final_assistant, final_finish_reason, llm)
            if final_assistant is not None and "error" not in final_assistant:
                _model = getattr(llm, "model", "?")
                if final_finish_reason == "incomplete_stream":
                    # Deliberately NOT an error, not even when empty. An error
                    # here moves the rest of the run onto the fallback profile
                    # -- and for an agent with no fallback chain it ends the run
                    # outright, where the existing empty-response guard would
                    # simply have retried. Far too heavy a hammer for what is
                    # usually a transient network hiccup. Say it and move on.
                    logger.warning(
                        "[%s] Stream ended without a [DONE] marker; the answer may be "
                        "truncated (model=%s, chars=%d, tool_calls=%d)",
                        self.name, _model, len(final_assistant.get("content") or ""),
                        len(final_assistant.get("tool_calls") or []),
                    )

            # Yield final response with usage data
            if final_assistant:
                result = {"type": "thinking_complete", "step": step + 1, "assistant": final_assistant}
                if final_usage:
                    result["usage"] = final_usage
                # Without carrying it here the truncation guard below never
                # sees a "length" and silently accepts a cut-off answer.
                if final_finish_reason:
                    result["finish_reason"] = final_finish_reason
                yield result
            else:
                yield {"type": "thinking_complete", "step": step + 1, "assistant": {"role": "assistant", "content": "".join(accumulated_content)}}

        else:
            # Non-streaming LLM: Use polling with 100ms intervals
            # Create task for LLM call
            llm_task = asyncio.create_task(llm.chat_tools(
                messages, tools_schema, 
                cancellation_token=cancellation_token,
                status_scope=status_scope
            ))

            # Poll for status events while waiting
            # For batch mode: The batch queue manager has its own timeout (max_wait_hours in llm.yaml)
            # so we don't need an agent-side timeout. We use a very high limit (24h) as safety net.
            # For sync mode: The HTTPX client has its own request_timeout.
            # The agent-side limit is just a safety net for truly stuck calls.
            max_llm_iterations = self.timeouts.llm_task_max_iterations if self.timeouts else 864000  # 24h default
            llm_iteration_count = 0
            # Calculate heartbeat interval in iterations (config is in seconds, we poll every 0.1s)
            heartbeat_interval_seconds = self.system_config.status.llm_heartbeat_interval
            heartbeat_interval = int(heartbeat_interval_seconds / 0.1)  # Convert seconds to iterations
            
            while not llm_task.done():
                llm_iteration_count += 1
                if llm_iteration_count > max_llm_iterations:
                    timeout_seconds = max_llm_iterations * 0.1
                    logger.error(
                        "LLM task polling exceeded max iterations (%d = %.0fs), forcing exit",
                        max_llm_iterations, timeout_seconds
                    )
                    llm_task.cancel()
                    await asyncio.sleep(0.1)
                    # Raise timeout error so it can be caught and handled appropriately
                    # This is different from CancelledError (user cancellation)
                    raise asyncio.TimeoutError(
                        f"LLM task timed out after {timeout_seconds:.0f} seconds "
                        f"(max_iterations={max_llm_iterations})"
                    )

                # Check for status events
                for status_event in yield_pending_status_fn():
                    yield status_event

                # Send heartbeat event periodically to prevent SSE connection timeout
                # This keeps the connection alive during long LLM calls (30+ seconds)
                if llm_iteration_count % heartbeat_interval == 0:
                    yield {
                        "type": "heartbeat",
                        "step": step + 1,
                        "timestamp": asyncio.get_event_loop().time()
                    }

                # Wait 100ms before next poll
                try:
                    await asyncio.wait_for(asyncio.shield(llm_task), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Continue polling

            # Get result
            llm_out = await llm_task
            result = {"type": "thinking_complete", "step": step + 1, "assistant": llm_out.get("assistant", {})}
            # Preserve usage data if present
            if "usage" in llm_out:
                result["usage"] = llm_out["usage"]
            if llm_out.get("finish_reason"):
                result["finish_reason"] = llm_out["finish_reason"]
            # Not every blocking client turns the filter into an error itself
            # (openai_responses only reports the reason).
            _content_filter_as_error(result["assistant"], llm_out.get("finish_reason"), llm)
            yield result

    async def _execute_llm_loop(
        self,
        context: ConversationContext,
        request_id: str,
        session_id: str,
        status_coordinator: StatusScope,
        status_worker: StatusScope,
        llm_override: Optional[LLMClient] = None,
        llm_profile_info_override: Optional[str] = None,
        use_advanced_model: bool = False,
    ):
        """Execute the main LLM conversation loop with tool execution.

        Phase 2 of agent execution: Iterative LLM calls with tool execution.

        Loops up to max_steps, plus one final call that asks for the answer
        (see _step_budget_note) and goes through the same step machinery:
        1. Check cancellation
        2. Drain appended messages
        3. Call LLM with tools
        4. Handle thinking/status events
        5. Execute tool calls (if any)
        6. Check loop guards (empty responses, no tool calls)
        7. Update messages with results

        Args:
            context: Conversation context with messages and tools
            request_id: Request identifier
            session_id: Session identifier
            status_coordinator: Coordinator status scope
            status_worker: Worker status scope
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status

        Yields:
            Dict events: heartbeat, thinking, status, tool_*, final, error, cancelled

        Returns:
            Tuple of (messages, results, step) after loop completion
        """
        # Determine which LLM to use. Phase 1 already validated availability;
        # this guard keeps the invariant explicit for direct callers (and
        # narrows the type from LLMClient|None).
        active_llm = llm_override if llm_override is not None else self.llm
        if active_llm is None:
            raise RuntimeError("No LLM available; agent requires an LLM to run")

        # Request-LOCAL display label: the agent's configured profile at request
        # start, or the last in-run swap of the base -- not re-derived per step
        # (a 5xx run keeps showing the fallback label until the request ends).
        # A step that walks around a blocked LLM is labelled by the pick.
        display_profile_info = self.llm_profile_info
        # The longest block of an LLM this agent sets; a quota or a refused key
        # blocks this long at once.
        max_block_seconds = float(self.agent_config.fallback_recovery_seconds
                                  if self.agent_config else 3600)
        # The profile a request-scoped fallback swap made this run's base
        # (active_llm): later steps leave it out of their fallback chain, or the
        # fallback that fails next would be retried as its own fallback.
        base_profile: Optional[str] = None

        # Extract from context
        messages = context.messages
        tools_schema = context.tools_schema
        tool_name_mapping = context.tool_name_mapping
        max_steps = context.max_steps
        main_token = context.main_token

        # Initialize results
        results: Dict[str, Any] = {"task": "", "calls": []}

        # Add safeguards against infinite loops
        consecutive_no_tool_calls = 0
        consecutive_empty_responses = 0
        consecutive_tool_error_steps = 0  # steps whose tool calls ALL errored (stuck signal)
        prev_step_all_errored = False     # was the IMMEDIATELY preceding step an all-error tool step?
        max_consecutive_no_tools = 3  # Break after 3 consecutive responses without tool calls
        max_consecutive_empty = 2    # Break after 2 consecutive empty responses
        # Text answers cut off at the output cap in a row, each sent back with a
        # note (see _output_cap_note) -- only where the agent opted in
        # (agent_config.output_cap_notes): for an agent whose product is its
        # text the cut-off text is still the reply, and a model that loops
        # until a 120k cap must not be sent back for two more rounds of it.
        consecutive_cut_off = 0
        max_cut_off_notes = int(getattr(self.agent_config, "output_cap_notes", 0) or 0)

        # Create a request-scoped loop detector.
        # Each request gets its own detector so concurrent requests on the
        # same Agent singleton don't contaminate each other's history, and
        # history from a previous request on the same session doesn't
        # cause false positives at the start of a new request.
        loop_detector = self._create_loop_detector()

        # Per-request auto-escalation: swap in the advanced model for a few steps
        # when the run loop observes the agent is stuck (loop detector / repeated
        # tool errors). State is request-scoped (must not leak across requests on
        # this shared Agent singleton). Disabled unless configured and an advanced
        # profile exists and we're not already running advanced.
        escalator = self._create_stuck_escalator(
            already_advanced=(use_advanced_model or llm_override is not None))
        escalate_error_streak = int(
            getattr(self.agent_config, "escalate_error_streak", 2)) if self.agent_config else 2

        # Helper function to yield any pending status events from per-request forwarder
        def yield_pending_status_events():
            for event in context.status_forwarder.get_pending_events():
                yield event

        async def cancelled_events(step):
            logger.info("Request %s cancelled at step %d", request_id, step + 1)
            # Signal cancellation using status contexts FIRST (so events are queued)
            await status_worker.error(f"cancelled at step {step + 1}",
                                      meta={"step": step + 1, "reason": "cancelled"})
            await status_coordinator.error(f"cancelled at step {step + 1}",
                                           meta={"step": step + 1, "reason": "cancelled"})
            # Give status events a moment to be captured by forwarder
            await asyncio.sleep(0.01)
            # Yield all pending status events before cancelled event
            for status_event in context.status_forwarder.get_pending_events():
                yield status_event
            yield {"type": "cancelled", "request_id": request_id, "step": step + 1}

        # One iteration past the budget: the final call. It used to be a bare
        # chat_tools() after the loop, and so it skipped everything a step does
        # -- the pre-LLM hooks (message_validator dropped no orphaned tool call,
        # context_engineer capped nothing), the fallback chain, streaming, the
        # post-LLM hooks. As a step it has all of that; what differs is only
        # that no step follows it (see final_call below).
        for step in range(max_steps + 1):
            final_call = step == max_steps
            if final_call:
                logger.warning(
                    f"Max steps ({max_steps}) reached. Agent may not have completed the task. "
                    f"Making one final LLM call to attempt completion."
                )
            # Error-streak bookkeeping (auto-escalation): the streak counts
            # CONSECUTIVE all-error tool steps. Any other step type — text-only,
            # empty response, blocked-tools, cancelled/timeout, or a step where
            # some tool succeeded — breaks the run. Deciding this from the
            # previous step's flag at the top of the loop makes it robust to the
            # many `continue`/`break` paths below (they can't skip a reset here).
            if not prev_step_all_errored:
                consecutive_tool_error_steps = 0
            prev_step_all_errored = False

            # Drain any appended user messages before each step
            messages = await self._drain_appended_messages(request_id, messages)
            # Sync context.messages after draining
            context.messages = messages

            # Check for cancellation at the start of each step
            if self._is_cancelled(request_id):
                async for event in cancelled_events(step):
                    yield event
                return

            # Progress heartbeat using status_coordinator
            await status_coordinator.progress(
                "final call after the step budget" if final_call else f"step {step + 1}/{max_steps}",
                meta={"step": step + 1, "max_steps": max_steps}
            )

            # Yield a heartbeat for UI responsiveness (non-blocking)
            yield {"type": "heartbeat", "step": step + 1, "max_steps": max_steps}

            # Yield pending status events before LLM call
            for status_event in yield_pending_status_events():
                yield status_event

            # Re-render the system message (session-scoped template vars may have
            # changed). Keep it free of per-step values: it is the cached prefix.
            # The step count reaches the model through _step_budget_note.
            updated_system_msg, _ = self._render_prompts(
                context.available_tools, max_steps, current_step=step + 1, session_id=context.session_id
            )
            messages[0] = ChatMessage(role="system", content=updated_system_msg)

            # Emit thinking event before LLM call (for UI step display)
            yield {"type": "thinking", "step": step + 1}

            # Before the hooks: the input they hand over needs no wake at the end.
            self._presence_step(session_id, request_id)

            # Pick the model that answers this step BEFORE the hooks run: they
            # size the context by context.llm (context_engineer's arrival cap,
            # context_summarizer's trigger, context_usage_tracker's percentage).
            # Picked after them, a fallback with a smaller window was sized by
            # the original's window, and a switch back to a freed LLM stripped
            # the history the hooks had already worked on.

            # Auto-escalation: run this step on the advanced model when a window
            # is open and its LLM is not blocked. The budget round is only spent
            # once it is settled that the advanced client answers.
            # Not the final call: it only asks for the answer, and the old
            # post-loop call never escalated either.
            escalated_this_step = escalator.active and not final_call

            def _pick_step_llm(escalate: bool):
                """(client, fallback chain, escalated, fallback profile or None)
                for this step. Spends no budget.

                The wanted client — the escalation, the override or the run's
                base — when its LLM is not blocked (llm/model_health.py); else
                the first unblocked profile of the chain; else the wanted one
                all the same: a blocked LLM beats no LLM. Coming back once the
                block is lifted is a switch like any other, stripped below.
                """
                llm = active_llm
                # Profil des TATSÄCHLICH aktiven Modells, wenn es vom Config-
                # Primär abweicht: Eskalations-Swap oder explizites Override
                # (llm_profile_info_override = "profil:provider/model"). Wird
                # aus der Fallback-Kette exkludiert, sonst würde das gerade
                # fehlschlagende Modell als sein eigener Fallback erneut laufen.
                active_profile_override = None
                if base_profile is not None:
                    active_profile_override = base_profile
                elif llm_override is not None and llm_profile_info_override:
                    active_profile_override = llm_profile_info_override.split(":", 1)[0]
                if escalate:
                    escalation_llm = self._get_escalation_llm()
                    if escalation_llm is None:
                        # Advanced client couldn't be built — ran on standard.
                        # Disable escalation for this run so we don't retry the
                        # build every step (the window would never close).
                        escalate = False
                        escalator.disable()
                    elif model_health.available(escalation_llm, request_id):
                        llm = escalation_llm
                        active_profile_override = (
                            self.agent_config.advanced_llm_profile
                            if self.agent_config else None
                        )
                    else:
                        # Blocked for now: this step runs on standard, the
                        # window stays open for a step after the block.
                        escalate = False
                # Ketten-Semantik: llm_profile = [primär, fallback1, ...],
                # llm_profile_advanced analog. fallback_chain() liefert die
                # passende Reihenfolge (advanced-Kette zuerst, dann die
                # normale Kette als letztes Sicherheitsnetz).
                profiles = (
                    self.agent_config.fallback_chain(
                        use_advanced_model, exclude=active_profile_override)
                    if self.agent_config else []
                )
                if base_profile is not None and llm_override is not None and llm_profile_info_override:
                    # The override the swap replaced failed too: not a fallback.
                    failed_override = llm_profile_info_override.split(":", 1)[0]
                    profiles = [p for p in profiles if p != failed_override]
                if llm_override is not None and self.agent_config:
                    # On an override the agent's own primary is not the run's
                    # base: fallback_chain() leaves it out as the active model,
                    # yet it is the first fallback -- with a one-entry chain the
                    # only one.
                    own_primary = (self.agent_config.advanced_llm_profile
                                   if use_advanced_model and self.agent_config.advanced_llm_profile
                                   else self.agent_config.default_llm_profile)
                    override_profile = (llm_profile_info_override.split(":", 1)[0]
                                        if llm_profile_info_override
                                        else getattr(llm_override, "profile_name", None))
                    if own_primary and own_primary not in profiles and own_primary not in (
                            active_profile_override, override_profile):
                        profiles.insert(0, own_primary)
                if use_advanced_model and profiles:
                    logger.debug(
                        f"[{self.name}] use_advanced_model=True — fallback "
                        f"chain: {profiles}"
                    )
                if not model_health.available(llm, request_id):
                    blocked = getattr(llm, "model", "?")
                    for index, profile in enumerate(profiles):
                        client = self._fallback_client(profile)
                        if client is not None and model_health.available(client, request_id):
                            logger.info(
                                f"[{self.name}] LLM {blocked} is blocked for "
                                f"{model_health.remaining(llm):.0f}s more; this step runs on {profile}")
                            # The blocked profiles walked past stay in the
                            # chain: if this one fails, a blocked LLM beats none.
                            return client, profiles[:index] + profiles[index + 1:], escalate, profile
                    logger.warning(
                        f"[{self.name}] LLM {blocked} is blocked and no fallback is free: calling it anyway")
                return llm, profiles, escalate, None

            def _base_label() -> str:
                if base_profile is not None:
                    return base_profile
                if llm_override is not None and llm_profile_info_override:
                    return llm_profile_info_override.split(":", 1)[0]
                return (display_profile_info or "base").split(":", 1)[0]

            def _take_fallback():
                """(label, client) to retry a failed call on, each taken once
                per step: the first unblocked one of this step's chain, with the
                run's base LLM as a member — the first after a failed
                escalation, the last after a failed walk around a blocked base;
                else the first one left all the same — a blocked LLM beats
                none. None when all are used up."""
                candidates = [(profile, None) for profile in fallback_profiles]
                if escalated_this_step:
                    # A failed escalation says nothing about the base: back to it
                    # before the chain moves the step to another model.
                    candidates.insert(0, (_base_label(), active_llm))
                else:
                    candidates.append((_base_label(), active_llm))
                first_blocked = None
                for index, (label, client) in enumerate(candidates):
                    if index in fallback_taken:
                        continue
                    if client is None:
                        client = self._fallback_client(label)
                    if client is None or client is current_llm:
                        fallback_taken.add(index)
                        continue
                    if model_health.available(client, request_id):
                        fallback_taken.add(index)
                        return label, client
                    if first_blocked is None:
                        first_blocked = (index, label, client)
                if first_blocked is None:
                    return None
                index, label, client = first_blocked
                fallback_taken.add(index)
                return label, client

            async def _announce_llm():
                # Signal LLM call start — for the picked client, so an escalation
                # whose client did not build is not announced as one.
                if escalated_this_step:
                    llm_display = " (advanced — escalated: stuck)"
                elif step_profile:
                    llm_display = f" ({step_profile}:fallback)"
                elif llm_profile_info_override and base_profile is None:
                    llm_display = f" ({llm_profile_info_override})"
                else:
                    llm_display = f" ({display_profile_info})" if display_profile_info else " (unknown LLM)"
                await status_worker.progress(f"Calling LLM{llm_display}", meta={"step": step + 1})

            health_seen = model_health.version
            previous_step_llm = self._step_llms.get(session_id)
            current_llm, fallback_profiles, escalated_this_step, step_profile = (
                _pick_step_llm(escalated_this_step))
            fallback_taken: set = set()
            # Clients this step already asked a second time after an error in
            # the body -- each gets that once, see the upstream-error branch.
            # The clients themselves, not id()s: a dropped fallback client's id
            # can come back on the next one built.
            body_error_retried: list = []
            if previous_step_llm is not None and current_llm is not previous_step_llm:
                # A switch between steps: into or out of an escalation, around a
                # blocked LLM or back to it. The history carries the previous
                # model's reasoning.
                strip_all_reasoning_artifacts(messages)
            # A switch since the previous request (or restart) left no step
            # model behind; the artifacts name the model that produced them.
            strip_foreign_reasoning_artifacts(messages, getattr(current_llm, "model", None))
            # Before the hooks too: tool_preload runs tools inside them, and a
            # tool that sizes the context asks llm_for_session.
            self._step_llms[session_id] = current_llm
            await _announce_llm()

            # Execute pre-LLM hooks with real-time status streaming
            # NOTE: Hooks execute synchronously from this generator's perspective,
            # so we use asyncio.create_task() + polling to stream status events
            # during hook execution. This pattern is only needed for hooks that
            # emit status messages (currently only context_summarizer).
            # Other hook types (post_llm, session_start, session_end) don't need
            # this pattern as they don't emit status events.
            try:
                # Create async task for hook execution
                hook_task = asyncio.create_task(
                    self._hook_manager.execute_pre_llm_hooks(
                        messages=messages,
                        step=step,
                        request_id=request_id,
                        session_id=session_id,
                        llm=current_llm,
                        cancellation_token=main_token
                    )
                )

                # Stream status events while hook is running
                # NOTE: this polling loop has no deadline of its own. Each hook
                # is bounded by its per-hook timeout (asyncio.wait_for in
                # hooks/registry.py; a timed-out hook is logged and skipped), so
                # a long-running hook (e.g. context_summarizer) needs a timeout
                # configured high enough for it.
                while not hook_task.done():
                    for status_event in yield_pending_status_events():
                        yield status_event
                    await asyncio.sleep(0.1)  # Poll every 100ms

                # Get hook result
                modified_messages = await hook_task

                # Yield any final status events from hook execution
                for status_event in yield_pending_status_events():
                    yield status_event

                # Select the message list that will go to the LLM. The
                # selection logic is extracted to ``_select_llm_messages``
                # so it can be unit-tested in isolation (the surrounding
                # step-loop is generator-based and hard to test directly).
                messages = self._select_llm_messages(
                    pre_hook_messages=messages,
                    modified_messages=modified_messages,
                )
                context.messages = messages
                # Whatever a hook staged now stands in `messages`. What is left
                # in the marker is spent, and it is poisonous from here on: the
                # code after tool execution reads a still-set marker as "a tool
                # rewrote the history mid-request" and rebuilds around it,
                # dropping the assistant tool-call message just appended.
                self._session_tracker.clear_compacted_messages(session_id)
            except Exception as e:
                logger.warning(f"Pre-LLM hooks failed: {e}", exc_info=True)

            # AFTER the hooks, so the run keeps the last word. Plugins append
            # their blocks in pre_llm_call, and the max-steps request ("answer
            # NOW, do NOT use any tools") only does its job as the last thing
            # the model reads -- a todo list with open items behind it sends
            # the model back to the tools. Outside the try on purpose: a hook
            # chain that fell over must not also cost the run its step budget.
            budget_note = self._step_budget_note(step, max_steps)
            if budget_note is not None:
                messages.append(budget_note)
                context.messages = messages

            # The blocks are shared by every agent, and the hooks can take
            # minutes (context_summarizer): another request may have blocked
            # this step's LLM, or an answer lifted the block this step walked
            # around, meanwhile. Pick again rather than call an LLM known to be
            # limited, or leave a free one unused. The hooks are not re-run —
            # they are not idempotent (tool_preload runs tools); the next step's
            # hooks see the new model.
            if model_health.version != health_seen:
                previous_llm = current_llm
                current_llm, fallback_profiles, escalated_this_step, step_profile = (
                    _pick_step_llm(escalated_this_step))
                fallback_taken = set()
                self._step_llms[session_id] = current_llm
                if current_llm is not previous_llm:
                    # The first pick may have made this request the prober of
                    # the LLM it now leaves: hand the probe back.
                    model_health.drop_probe(previous_llm, request_id)
                    # A model switch either way: the history the hooks worked
                    # on carries the previous model's reasoning items.
                    strip_all_reasoning_artifacts(messages)
                    await _announce_llm()

            # Spent only now that it is settled which model answers: a re-pick
            # onto a fallback would otherwise have spent a round on no advanced call.
            if escalated_this_step:
                escalator.consume()

            # LLM call with streaming support and fallback handling
            llm_out = None

            # WHICH client was told to try again after its thinking looped —
            # not merely THAT one was. A fallback switch later in this step
            # replaces current_llm, and the new model has earned no exemption:
            # comparing the client re-arms the watchdog by itself, where a
            # plain flag would leave an innocent model unwatched.
            reasoning_loop_llm = None

            async def _reasoning_progress(text, chars, previous):
                # current_llm is read at call time: after a fallback switch the
                # hooks see the model that is actually thinking.
                await self._hook_manager.execute_llm_progress_hooks(
                    reasoning_text=text, reasoning_chars=chars,
                    previous_reasoning_chars=previous, step=step,
                    request_id=request_id, session_id=session_id, llm=current_llm)

            while True:  # Retry loop for fallbacks (rate limits + upstream errors)
                # For tools that size the context themselves (compact, summarize):
                # the model answering this session's step, see llm_for_session.
                self._step_llms[session_id] = current_llm
                pending_thinking_complete = None
                _llm_call_started = asyncio.get_event_loop().time()
                health_asked_at = model_health.now()
                try:
                    async for event in self._call_llm_with_streaming(
                        llm=current_llm,
                        messages=messages,
                        tools_schema=tools_schema,
                        cancellation_token=main_token,
                        step=step,
                        yield_pending_status_fn=yield_pending_status_events,
                        status_scope=status_worker,
                        watch_reasoning=current_llm is not reasoning_loop_llm,
                        on_reasoning_progress=(_reasoning_progress
                                               if self._hook_manager.wants_llm_progress()
                                               else None),
                    ):
                        event_type = event.get("type")

                        if event_type == "reasoning_delta":
                            # Yield Gemini reasoning/thinking tokens to WebUI
                            yield event
                        elif event_type == "thinking_delta":
                            # Yield real-time token deltas to WebUI
                            yield event
                        elif event_type in ("status", "sub_run"):
                            # What the forwarder collected meanwhile: status lines, and
                            # the events of sub-agents working while this call waits.
                            yield event
                        elif event_type == "thinking_complete":
                            # CRITICAL: Make a deep copy of assistant dict to prevent
                            # format_output hooks in app.py from modifying the stored message!
                            # app.py formats events for display, but we need raw Markdown in messages
                            llm_out = {"assistant": copy.deepcopy(event["assistant"])}
                            # Preserve usage data if present in event
                            if "usage" in event:
                                llm_out["usage"] = event["usage"]
                            # ...and finish_reason, which the truncation guard
                            # below reads off llm_out.
                            if event.get("finish_reason"):
                                llm_out["finish_reason"] = event["finish_reason"]
                            # Store event — DON'T yield yet, check for upstream errors first
                            pending_thinking_complete = event

                    # Check for upstream error in response body BEFORE yielding to client.
                    # Upstream errors (e.g. Qwen/Alibaba content filter) arrive as HTTP 200
                    # with {"error": ...} in the body — not as exceptions.
                    # Handling them here (inside the while-True retry loop) allows clean
                    # retry with a fallback LLM without leaking the error event to the client.
                    _assistant_check = llm_out.get("assistant", {}) if llm_out else {}
                    if "error" in _assistant_check:
                        error_info = _assistant_check["error"]
                        error_msg = error_info.get("message", "Unknown LLM error")
                        error_type = error_info.get("type", "unknown")
                        logger.warning(f"LLM returned upstream error: {error_type} - {error_msg}")
                        # Billed all the same: the failed call's usage still
                        # reaches the caller's sum of the turn -- its content does not.
                        if pending_thinking_complete and pending_thinking_complete.get("usage"):
                            yield _name_the_model({
                                "type": "thinking_complete",
                                "step": pending_thinking_complete.get("step"),
                                "assistant": {},
                                "usage": pending_thinking_complete["usage"],
                            }, current_llm)

                        if (not str(error_type).startswith("content_filter")
                                and not error_info.get("retried")
                                and not any(c is current_llm for c in body_error_retried)):
                            # Once more on the SAME model first. Measured
                            # 22.09.2026: the same request shape went through
                            # 405 times, and the two invalid_prompt bodies came
                            # 3 s apart -- a gateway hiccup, not the request.
                            # Straight to the fallback moved those runs onto
                            # it for good. An unknown deterministic error costs
                            # one call more, then switches. Straight on: a
                            # content filter (content_filter, httpx's
                            # content_filter_<native>) -- the same model blocks
                            # the same text again -- and an error its client
                            # marks "retried", which already went through a
                            # whole retry cycle with backoff.
                            body_error_retried.append(current_llm)
                            logger.warning(
                                f"[{self.name}] Upstream error from LLM, "
                                f"asking {getattr(current_llm, 'model', '?')} once more")
                            await status_worker.progress(
                                f"LLM error ({error_type}), retrying once",
                                meta={"step": step + 1})
                            continue
                        taken = _take_fallback()
                        if taken:
                            fallback_profile, fallback_llm = taken
                            logger.warning(
                                f"[{self.name}] Upstream error from LLM, "
                                f"switching to fallback: {fallback_profile}"
                            )
                            await status_worker.progress(
                                f"LLM error ({error_type}), switching to {fallback_profile}",
                                meta={"step": step + 1, "fallback": fallback_profile}
                            )
                            # No block, like the 5xx path it is the twin of: an
                            # upstream error says the gateway stumbled, not that
                            # this LLM is gone. Rescues THIS request; the next
                            # starts on the original again.
                            self._strip_for_switch(fallback_profile, messages)
                            # Also swap the run's base LLM so hooks use the
                            # fallback too — when the BASE failed. An escalation
                            # model that failed says nothing about the base; only
                            # this step is rescued.
                            if current_llm is active_llm:
                                display_profile_info = f"{fallback_profile}:fallback"
                                active_llm = fallback_llm
                                base_profile = fallback_profile
                            current_llm = fallback_llm
                            continue  # Retry LLM call with fallback in same step
                        # The chain is used up — hard error
                        yield {"type": "error", "message": error_msg, "error_type": error_type}
                        return

                    # No error — yield the deferred thinking_complete and exit retry loop.
                    # An answer lifts the LLM's block for every agent — one set
                    # before this call went out.
                    model_health.release(current_llm, asked_at=health_asked_at)
                    if pending_thinking_complete:
                        yield _name_the_model(pending_thinking_complete, current_llm)
                    break
                    
                except ReasoningLoopError as e:
                    # The model walked into a circle inside its own thinking.
                    # Nothing is wrong with the provider, the model or the
                    # request — the sampling was unlucky — so this is the one
                    # recovery here that does NOT switch profiles: a profile
                    # switch would punish a healthy model for one bad roll.
                    #
                    # The exemption belongs to the CLIENT, not to the step: the
                    # retry runs unwatched, but a fallback switch afterwards
                    # brings a different client, and that one is watched again
                    # — it can abort here too, in the same step. What bounds
                    # this is the fallback chain, which is finite and shrinks
                    # with every switch; no client is ever watched twice.
                    reasoning_loop_llm = current_llm
                    logger.warning(
                        "[%s] Reasoning loop after %d characters of thinking "
                        "(%s) — retrying the same model once: %s",
                        self.name, e.characters, e.reason,
                        getattr(current_llm, "model", "?"))
                    await status_worker.progress(
                        "Thinking went in circles, retrying once",
                        meta={"step": step + 1, "reasoning_characters": e.characters})

                    # The aborted attempt WAS produced, so it must leave a
                    # trace. The client's own post-response notification sits
                    # after the stream, which this abort never reaches, so
                    # without this the message debugger keeps a request with
                    # no response — and every later recalibration of the
                    # threshold reads that same database and would be blind to
                    # exactly the calls this guard aborted.
                    #
                    # What it can and cannot say: the reasoning characters and
                    # the score are known and travel in response_data, so a
                    # later measurement can use these rows. The TOKENS are not
                    # — usage arrives with the completed response, which this
                    # call never produced — so the cost report still misses
                    # what the abort spent. Naming that beats implying the
                    # entry closes it.
                    #
                    # Reaching into the client's notifier is a deliberate
                    # layer crossing: there is no public equivalent, and an
                    # entry carrying "error" is the shape it already uses for
                    # its own failed attempts, which is why it skips the
                    # latency stash and leaves cost attribution untouched.
                    notify = getattr(current_llm, "_notify_post_response", None)
                    if notify is not None:
                        await notify({
                            # Most clients name themselves in their own
                            # notifications but carry no _PROVIDER attribute;
                            # the class name keeps the row attributable
                            # instead of filing it under "unknown".
                            "provider": (getattr(current_llm, "_PROVIDER", None)
                                         or type(current_llm).__name__),
                            "model": getattr(current_llm, "model", "?"),
                            "url": "", "is_streaming": True,
                            "duration_ms": (asyncio.get_event_loop().time()
                                            - _llm_call_started) * 1000,
                            "error": f"reasoning loop aborted: {e.reason}",
                            "finish_reason": "reasoning_loop_aborted",
                            "response_data": {"reasoning_loop": {
                                "characters": e.characters,
                                "reason": e.reason,
                            }},
                        })
                    continue

                except (LLMRateLimitError, LLMQuotaExhaustedError) as e:
                    is_quota_exhausted = isinstance(e, LLMQuotaExhaustedError)
                    reason = "quota exhausted" if is_quota_exhausted else "rate limit hit"
                    # A block of the LLM, not of this agent: every agent walks
                    # around it until it runs out or the LLM answers someone. A
                    # rate limit starts short and grows while the LLM keeps
                    # failing; an exhausted quota does not get better in a minute.
                    pause = model_health.block(
                        current_llm, max_pause=max_block_seconds,
                        rate_limit=not is_quota_exhausted, retry_after=e.retry_after,
                        asked_at=health_asked_at, reason=f"{reason}, seen by {self.name}")
                    taken = _take_fallback()
                    if taken:
                        fallback_profile, fallback_llm = taken
                        logger.warning(
                            f"[{self.name}] {e.__class__.__name__}: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        retry_in = f", retry in {pause:.0f}s" if pause and pause >= 1 else ""
                        await status_worker.progress(
                            f"{reason.capitalize()}, switching to {fallback_profile}{retry_in}",
                            meta={"step": step + 1, "fallback": fallback_profile, "blocked_seconds": pause}
                        )
                        self._strip_for_switch(fallback_profile, messages)
                        # No swap of the run's base: the next step asks the
                        # blocks again and returns to the base once it is free.
                        current_llm = fallback_llm
                        continue  # Retry with fallback
                    logger.error(f"[{self.name}] No fallback profiles available, {reason}")
                    raise

                except LLMServerError as e:
                    # 5xx server errors (e.g. DeepSeek 504) — try fallback, but no block.
                    # Server errors are transient outages; the primary LLM should be retried next time.
                    taken = _take_fallback()
                    if taken:
                        fallback_profile, fallback_llm = taken
                        logger.warning(
                            f"[{self.name}] Server error {e.status_code} from {e.model}: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        await status_worker.progress(
                            f"Server error {e.status_code}, switching to {fallback_profile}",
                            meta={"step": step + 1, "fallback": fallback_profile}
                        )
                        self._strip_for_switch(fallback_profile, messages)
                        # Request-scoped swap when the BASE failed, like its
                        # twins (upstream error, connection error): without it
                        # every following step started on the failing base
                        # again, its hooks sizing the context for a model the
                        # call never reached.
                        if current_llm is active_llm:
                            display_profile_info = f"{fallback_profile}:fallback"
                            active_llm = fallback_llm
                            base_profile = fallback_profile
                        current_llm = fallback_llm
                        continue  # Retry with fallback (request-scoped)
                    logger.error(f"[{self.name}] No fallback profiles available, server error unrecoverable")
                    raise

                except (LLMConnectionError, httpx.TransportError,
                        httpx.HTTPStatusError) as e:
                    # Transport errors (connect/read timeout, network failure) — the
                    # endpoint is unreachable, there is no HTTP response. Try the next
                    # profile, no block (same reasoning as LLMServerError above).
                    # Raw httpx.TransportError covers clients that re-raise transport
                    # failures untyped (e.g. the OpenAI responses client).
                    #
                    # httpx.HTTPStatusError is the 4xx case (429/5xx arrive as typed
                    # errors before this). For a CHAIN it means: this provider refuses
                    # this request. Since 2026-08-20 the primary of 186 chains is an
                    # OpenRouter profile with a direct-API fallback behind it; without
                    # this clause a 4xx killed the run without ever trying the
                    # fallback the chain exists for.
                    #
                    # The status decides HOW to fall back (review finding: lumping
                    # them made an expired key look like a network error and re-probed
                    # it on every step):
                    #   400/413/422  request-shaped (too long, cap exceeded) — a
                    #                different request may pass: no block.
                    #   401/402/403/404  key-, credit- or model-level; holds for every
                    #                request on this endpoint. The LLM is BLOCKED for
                    #                every agent, so the dead endpoint is not
                    #                re-probed max_steps times. A cross-provider
                    #                chain member has its own key and still rescues
                    #                the run.
                    status_code = getattr(getattr(e, "response", None), "status_code", None)
                    endpoint_level = status_code in (401, 402, 403, 404)
                    if status_code is None and _is_local_resource_exhaustion(e):
                        # No profile can rescue this: the next client cannot open
                        # a socket either. Walking the chain would only burn the
                        # fallbacks — onto more expensive models — and hide the
                        # real cause behind a provider-shaped error message.
                        logger.error(
                            f"[{self.name}] Out of file descriptors while calling "
                            f"the LLM ({e}). This is a local resource limit, not a "
                            f"provider failure — not switching profiles. Check the "
                            f"process's open descriptors against LimitNOFILE."
                        )
                        raise
                    kind = (f"HTTP {status_code}" if status_code
                            else "Connection/transport error")
                    if endpoint_level:
                        model_health.block(
                            current_llm, max_pause=max_block_seconds, rate_limit=False,
                            asked_at=health_asked_at, reason=f"{kind}, seen by {self.name}")
                    taken = _take_fallback()
                    if taken:
                        fallback_profile, fallback_llm = taken
                        logger.warning(
                            f"[{self.name}] {kind} from LLM: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        await status_worker.progress(
                            f"Connection error, switching to {fallback_profile}",
                            meta={"step": step + 1, "fallback": fallback_profile}
                        )
                        self._strip_for_switch(fallback_profile, messages)
                        # Request-scoped swap when the BASE failed (like the
                        # upstream-error and 5xx paths): without it, EVERY
                        # following step retries the dead endpoint first
                        # (~connect timeout x retries per step).
                        if current_llm is active_llm:
                            display_profile_info = f"{fallback_profile}:fallback"
                            active_llm = fallback_llm
                            base_profile = fallback_profile
                        current_llm = fallback_llm
                        continue  # Retry with fallback
                    logger.error(f"[{self.name}] No fallback profiles available, connection error unrecoverable")
                    raise

                except asyncio.CancelledError:
                    # Streaming was cancelled - send proper status events and cancelled event
                    logger.info(f"Request {request_id} cancelled during LLM call at step {step + 1}")
                    await status_worker.error(f"cancelled at step {step + 1}",
                                          meta={"step": step + 1, "reason": "cancelled"})
                    await status_coordinator.error(f"cancelled at step {step + 1}",
                                                meta={"step": step + 1, "reason": "cancelled"})
                    await asyncio.sleep(0.01)
                    for status_event in context.status_forwarder.get_pending_events():
                        yield status_event
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                    return
                
                except asyncio.TimeoutError as e:
                    # LLM task timed out (e.g., batch job taking too long)
                    # This is different from user cancellation - report as timeout error
                    timeout_msg = str(e) if str(e) else "LLM request timed out"
                    logger.error(f"Request {request_id} timed out during LLM call at step {step + 1}: {timeout_msg}")
                    await status_worker.error(f"timeout at step {step + 1}: {timeout_msg}",
                                          meta={"step": step + 1, "reason": "timeout"})
                    await status_coordinator.error(f"timeout at step {step + 1}",
                                                meta={"step": step + 1, "reason": "timeout"})
                    await asyncio.sleep(0.01)
                    for status_event in context.status_forwarder.get_pending_events():
                        yield status_event
                    yield {"type": "error", "request_id": request_id, "step": step + 1, 
                           "message": timeout_msg, "error_type": "timeout"}
                    return

            # Signal LLM call completion
            await status_worker.progress("LLM (chat) response received", meta={"step": step + 1})

            assistant = llm_out.get("assistant", {}) if llm_out else {}

            content = assistant.get("content")
            tool_calls = assistant.get("tool_calls", [])
            # "length" = the model hit its output cap. With no content that is a
            # TRUNCATION, not an empty answer — see the empty-response guard below.
            finish_reason = llm_out.get("finish_reason") if llm_out else None
            # Provider-side encrypted thinking blocks (Gemini 3.x thought_signature
            # via OpenRouter's reasoning_details). MUST be carried through to the
            # next request or upstream returns MALFORMED_FUNCTION_CALL.
            reasoning_details = assistant.get("reasoning_details")
            answering_model = getattr(current_llm, "model", None)

            # Create assistant message and add it BEFORE post_llm hooks
            # so message debugger can capture the complete conversation
            # DeepSeek thinking mode: with `tools` in the request, the assistant's
            # reasoning_content MUST be passed back on every subsequent turn
            # (api-docs.deepseek.com/guides/thinking_mode#tool-call). Dropping it
            # here made _postprocess_messages_for_provider send an empty string,
            # so the model lost its chain of thought after every tool call and
            # re-derived it from scratch — reasoning grew with the conversation
            # until it hit the 65536-token cap (measured: 2.6s/103 reasoning
            # tokens on turn 1, 650s/65536 once tool results had accumulated).
            # Agents without tool calls (v4 pipeline) were never affected, which
            # is why this only showed up on the tool-heavy coding agents.
            assistant_msg = ChatMessage(
                role="assistant",
                content=content or "",
                tool_calls=history_safe_tool_calls(tool_calls) if tool_calls else None,
                reasoning_content=assistant.get("reasoning_content"),
                reasoning_details=reasoning_details,
                reasoning_model=(answering_model if reasoning_details
                                 and isinstance(answering_model, str) else None),
                # Anthropic thinking blocks (+ the model that signed them).
                # Same contract as reasoning_content above: with tool use they
                # must be echoed back complete and unmodified, so they have to
                # survive on the message.
                thinking_blocks=assistant.get("thinking_blocks"),
                thinking_model=assistant.get("thinking_model"),
                # OpenRouter backend of this turn: the next request pins to it.
                served_by=assistant.get("served_by"),
                # the step as the live events number it (ChatMessage.step)
                step=step + 1,
                timestamp=datetime.now(timezone.utc)
            )
            messages.append(assistant_msg)
            # Only append to context.messages if it's a different list
            if context.messages is not messages:
                context.messages.append(assistant_msg)

            # Execute post-LLM hooks to transform the response
            # NOTE: Using same polling pattern as pre_llm_hooks to support
            # future hooks that may emit status messages during execution.
            # Init per step: the continuation check below reads hook_metadata even
            # when the hook block fails — without this a first-step hook failure
            # raises NameError, and later steps would reuse the PREVIOUS step's
            # metadata (stale continuation signal).
            hook_metadata: Dict[str, Any] = {}
            try:
                # Create async task for hook execution
                hook_task = asyncio.create_task(
                    self._hook_manager.execute_post_llm_hooks(
                        messages=messages,
                        llm_response=llm_out,  # Pass full LLM response including usage data
                        step=step,
                        request_id=request_id,
                        session_id=session_id,
                        # The client that produced this response — after a
                        # fallback switch in the retry loop, not the run's base.
                        llm=current_llm
                    )
                )

                # Stream status events while hook is running
                # NOTE: this polling loop has no deadline of its own; each hook
                # is bounded by its per-hook timeout (hooks/registry.py).
                while not hook_task.done():
                    for status_event in yield_pending_status_events():
                        yield status_event
                    await asyncio.sleep(0.1)  # Poll every 100ms

                # Get hook result
                modified_response, hook_metadata = await hook_task

                # Yield any final status events
                for status_event in yield_pending_status_events():
                    yield status_event

                if modified_response is not None:
                    # Extract assistant data from modified response
                    modified_assistant = modified_response.get("assistant", {})
                    new_content = modified_assistant.get("content")
                    new_tool_calls = modified_assistant.get("tool_calls")

                    # Update content and tool_calls if hooks modified them
                    if new_content is not None:
                        content = new_content
                        assistant_msg.content = content or ""
                    if new_tool_calls is not None:
                        tool_calls = new_tool_calls
                        assistant_msg.tool_calls = history_safe_tool_calls(tool_calls) if tool_calls else None

                    # Set content_format from hook metadata (e.g., 'html', 'markdown', 'text')
                    if "content_format" in hook_metadata:
                        assistant_msg.content_format = hook_metadata["content_format"]
            except Exception as e:
                logger.warning(f"Post-LLM hooks failed: {e}", exc_info=True)

            # Format content for display (markdown -> HTML for web UI)
            formatted_content = content
            content_format = getattr(assistant_msg, 'content_format', 'text')  # Default to 'text' if not set by hooks
            try:
                if content and self._hook_manager:
                    formatted_content, content_format = await self._hook_manager.execute_format_output_hooks(
                        output=content,
                        request_id=request_id or "unknown",
                        session_id=session_id or "unknown",
                        output_format='html'
                    )
            except Exception as e:
                logger.warning(f"Failed to format content for display: {e}", exc_info=True)
                # Keep original content on error
                formatted_content = content

            # Emit thinking event with LLM response (for UI to show assistant reasoning)
            yield {"type": "thinking", "step": step + 1, "assistant": {"content": formatted_content, "tool_calls": tool_calls, "content_format": content_format}}

            # Also emit simplified thinking event if we have content and no tool calls (final answer)
            if content and not tool_calls:
                yield {"type": "thinking", "content": formatted_content, "content_format": content_format}

            # Yield pending status events after LLM response
            for status_event in yield_pending_status_events():
                yield status_event

            # Truncated but NOT empty. The guard below only covers "the model
            # produced nothing at all", so a cut-off answer WITH content fell
            # through as if it were complete — a scene ending mid-sentence, or
            # a tool call whose arguments JSON is half-written (which upstream
            # then rejects on the next turn as invalid_prompt).
            # Deliberately a warning and not an error: an error moves the run
            # onto the fallback profile and discards output that is
            # usually still usable — the same trade-off the incomplete_stream
            # branch settles the same way.
            # "In a row" means answers: one that ended on its own -- a tool call
            # of a model now writing in parts included -- starts the count again.
            if finish_reason != "length":
                consecutive_cut_off = 0
            if finish_reason == "length" and (content or tool_calls):
                logger.warning(
                    "[%s] Answer truncated at the output cap (finish_reason=length, "
                    "model=%s, chars=%d, tool_calls=%d) — it is NOT complete. "
                    "Raise max_tokens or lower the reasoning level if this recurs.",
                    self.name, getattr(current_llm, "model", "?"),
                    len(content or ""), len(tool_calls or []),
                )

            # Infinite loop guard: Track consecutive empty responses FIRST
            # (before checking tool calls, to catch completely empty responses)
            if not content and not tool_calls:
                consecutive_empty_responses += 1
                
                # Remove the empty assistant message we just added - it serves no purpose
                # and will just accumulate in the session causing validation issues
                if messages and messages[-1].role == "assistant" and not messages[-1].content and not messages[-1].tool_calls:
                    messages.pop()
                    if context.messages is not messages and context.messages and context.messages[-1].role == "assistant":
                        context.messages.pop()
                    logger.debug("Removed empty assistant message from conversation history")
                
                # Output cap exhausted with nothing to show: the model spent its
                # whole budget (typically on reasoning) and was cut off. This is
                # NOT "the model had nothing to say", and a 'Continue' nudge just
                # replays the same runaway — observed as 4 x ~11 min and ~260k
                # reasoning tokens burned for zero output. Fail fast and say why.
                if finish_reason == "length":
                    usage = (llm_out or {}).get("usage") or {}
                    reasoning_tokens = (
                        (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                    )
                    error_msg = (
                        "LLM hit its output token limit without producing any content "
                        f"(finish_reason=length, completion_tokens="
                        f"{usage.get('completion_tokens', '?')}"
                        + (f", of which reasoning={reasoning_tokens}" if reasoning_tokens else "")
                        + "). The model exhausted its budget before answering — lower the "
                        "thinking/reasoning level, raise max_tokens, or use a different model."
                    )
                    logger.error(error_msg)
                    results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg}
                    return

                # Not on the final call: no step follows to read the nudge, and
                # the session would keep it as the conversation's last word.
                if consecutive_empty_responses >= max_consecutive_empty and not final_call:
                    logger.warning(f"Empty response #{consecutive_empty_responses}: Injecting 'Continue' note to prompt LLM")
                    # Instead of breaking, nudge the LLM. The RUN asks for this,
                    # not a person -- the note used to arrive as a user message
                    # ("mimics the user typing weiter"), and stayed in the
                    # session afterwards as if someone had.
                    #
                    # The role stays `developer` although a prompt ENDING on one
                    # cannot be answered (measured 21.09.2026: 6/6 empty, and
                    # Google refuses such a request outright). That is a wire
                    # problem and it is fixed on the wire -- the stored message
                    # keeps saying who spoke, and the client lowers the LAST
                    # developer item to the user rung. Doing it here instead
                    # would put a user-role loop note back in the transcript,
                    # which is what test_agent_step_budget_note forbids.
                    continue_message = ChatMessage(role=DEVELOPER, content="Continue with your task.",
                                                   timestamp=datetime.now(timezone.utc),
                                                   injected_by="agent.empty_response")
                    messages.append(continue_message)
                    # Don't reset counter - if we get another empty response after this, we'll inject again
                    # But cap at a reasonable limit to prevent truly infinite loops
                    if consecutive_empty_responses >= max_consecutive_empty + 3:
                        logger.warning(f"Breaking loop: {consecutive_empty_responses} consecutive empty responses even after 'Continue' prompts")
                        error_msg = "LLM returned empty responses repeatedly despite continue prompts"
                        results.setdefault("errors", []).append(error_msg)
                        yield {"type": "error", "message": error_msg}
                        return
                    # Continue to next iteration with the injected message
                    continue
            else:
                consecutive_empty_responses = 0  # Reset counter
                
                # Persist session after each valid (non-empty) LLM response to preserve progress on cancellation
                # NOTE: We only persist non-empty responses to avoid accumulating useless empty messages
                await self._persist_conversation(
                    session_id, messages, to_disk=False,
                    note=f"after LLM response (step {step})")

            # Check if we have tool calls to execute
            if tool_calls:
                # Reset no-tool-calls counter
                consecutive_no_tool_calls = 0
                
                # ===== TOOL CALL LOOP DETECTION =====
                # Check for repeated tool call patterns that indicate the agent is stuck
                loop_result = loop_detector.record_batch_and_check(tool_calls, step)
                
                pending_intervention_msg: Optional[ChatMessage] = None

                if loop_result.is_loop:
                    # Log the detection
                    logger.warning(
                        f"[{self.name}] Tool loop detected at step {step}: "
                        f"type={loop_result.loop_type}, tool={loop_result.tool_name}, "
                        f"count={loop_result.repetition_count}"
                    )
                    
                    # Prepare intervention message to nudge the LLM
                    pending_intervention_msg = ChatMessage(
                        role=DEVELOPER,
                        content=loop_result.intervention,
                        timestamp=datetime.now(timezone.utc),
                        injected_by="agent.loop_intervention",
                    )
                    
                    # Emit status event for visibility
                    await status_worker.progress(
                        f"Loop detected: {loop_result.tool_name} ({loop_result.repetition_count}x)",
                        meta={"step": step + 1, "loop_type": loop_result.loop_type}
                    )

                    # Objective stuck signal → open an escalation window so the
                    # NEXT few steps run on the advanced model (budget permitting).
                    # No step follows the final call to run on it.
                    esc_reason = None if final_call else escalator.trigger(
                        f"tool-call loop ({loop_result.tool_name})")
                    if esc_reason:
                        logger.warning(
                            "[%s] auto-escalating to advanced model at step %d: %s "
                            "(budget used %d/%d)", self.name, step + 1, esc_reason,
                            escalator.calls_used, escalator.max_calls)
                        await status_worker.progress(
                            f"auto-escalating to advanced model ({esc_reason})",
                            meta={"step": step + 1})

                    # If tool should be blocked, filter it out
                    if loop_result.should_block_tool:
                        blocked_tools = loop_result.blocked_tools
                        original_count = len(tool_calls)
                        tool_calls = [
                            tc for tc in tool_calls 
                            if tc.get("function", {}).get("name") not in blocked_tools
                        ]
                        if len(tool_calls) < original_count:
                            logger.warning(
                                f"[{self.name}] Blocked {original_count - len(tool_calls)} tool calls "
                                f"due to loop detection. Blocked tools: {blocked_tools}"
                            )
                            if final_call:
                                # A blocked call gets no result. After a step the
                                # next call's message_validator drops it from the
                                # history; after the final call no call follows,
                                # and the session would keep it unanswered.
                                assistant_msg.tool_calls = (
                                    history_safe_tool_calls(tool_calls) if tool_calls else None)
                                if (not tool_calls and not (content and content.strip())
                                        and messages[-1] is assistant_msg):
                                    messages.pop()
                                    if (context.messages is not messages and context.messages
                                            and context.messages[-1] is assistant_msg):
                                        context.messages.pop()
                            # If all tools were blocked, continue to next iteration
                            # The intervention message will prompt the LLM to try something else
                            if not tool_calls:
                                # No tool results will follow, so inject now to keep the loop warning.
                                # Not after the final call, like the empty-response nudge.
                                if not final_call:
                                    messages.append(pending_intervention_msg)
                                    context.messages = messages
                                elif content and content.strip():
                                    # What is left is a text answer on the final
                                    # call: delivered like the no-tool answer below.
                                    results["summary"] = content
                                    self._set_live_messages(session_id, messages.copy())
                                    final_event = {"type": "final", "summary": formatted_content,
                                                   "content_format": content_format}
                                    if llm_out and "usage" in llm_out:
                                        final_event["usage"] = llm_out["usage"]
                                    yield final_event
                                    return
                                continue
                
                # Signal tool execution start
                await status_worker.progress(f"Executing Tools ({len(tool_calls)} total)", meta={"step": step + 1})

                # Assistant message with tool calls was already added above before post_llm hooks

                # Update tracked messages
                self._set_live_messages(session_id, messages.copy())

                # Execute all tools using streaming to get real-time status events from sub-agents
                tool_messages = []
                tool_results = []

                # Extract user_id from session metadata for multi-user tool isolation
                user_id: Optional[str] = None
                if self._session_tracker:
                    session_meta = self._session_tracker.get_session_metadata(session_id)
                    if session_meta:
                        user_id = session_meta.get("user_id")
                        logger.debug(f"[TOOL_EXEC] Extracted user_id='{user_id}' from session_metadata for session {session_id}")
                    else:
                        logger.warning(f"[TOOL_EXEC] No session_metadata found for session {session_id}")
                else:
                    logger.warning("[TOOL_EXEC] No _session_tracker available")

                # CRITICAL: Pass per-request status_forwarder as parameter to avoid race conditions
                # when multiple requests share the same agent instance (e.g., parent + sub-agent)
                # DO NOT set self._tool_execution_manager._status_forwarder - that causes race conditions!

                async for item in self._tool_execution_manager.execute_tools_streaming(
                    tool_calls=tool_calls,
                    tool_name_mapping=tool_name_mapping,
                    available_tools=context.available_tools,
                    step=step,
                    request_id=request_id,
                    session_id=session_id,
                    user_id=user_id,
                    status_forwarder=context.status_forwarder,
                    assistant_message=assistant_msg,
                    # What this run was switched to, for the sub-agents its
                    # tools start (agent_config.inherit_parent_llm).
                    llm_profile=self._profile_to_hand_down(llm_override),
                ):
                    if item.get("type") == "status":
                        # Yield status events in real-time during tool execution
                        yield item["event"]
                    elif item.get("type") == "tool_events":
                        # Yield tool execution events
                        for event in item["events"]:
                            yield event
                    elif item.get("type") == "complete":
                        # Store final results
                        tool_messages = item["messages"]
                        tool_results = item["results"]

                # Add tool results to the results dictionary
                results["calls"].extend(tool_results)

                # Stuck signal: a step whose tool calls ALL returned an error.
                # Catches the near-loops the exact-match detector misses (same
                # tool retried with slightly varied wrong args). N in a row →
                # open an escalation window.
                if tool_messages and all(
                        self._tool_message_is_error(m) for m in tool_messages):
                    consecutive_tool_error_steps += 1
                    prev_step_all_errored = True  # keep the streak alive next step
                    if consecutive_tool_error_steps >= escalate_error_streak and not final_call:
                        esc_reason = escalator.trigger(
                            f"{consecutive_tool_error_steps} all-error tool steps")
                        if esc_reason:
                            logger.warning(
                                "[%s] auto-escalating to advanced model at step %d: "
                                "%s (budget used %d/%d)", self.name, step + 1,
                                esc_reason, escalator.calls_used, escalator.max_calls)
                            await status_worker.progress(
                                f"auto-escalating to advanced model ({esc_reason})",
                                meta={"step": step + 1})
                # A non-all-error tool step leaves prev_step_all_errored False,
                # so the streak resets at the top of the next iteration.

                # CRITICAL: Check if ANY tool modified the session messages during execution.
                # Tools can set modified messages via session_tracker.set_compacted_messages()
                # This is a generic mechanism - any tool can use it to replace the message history.
                #
                # Examples of tools that use this:
                # - context_engineer.compact() - Compresses messages to save tokens
                # - context_summarizer - Summarizes old conversation history
                # - Any custom tool that wants to modify conversation state
                #
                # The session tracker stores conversation messages WITHOUT system message.
                compacted_messages = self._session_tracker.get_compacted_messages(session_id)
                
                if compacted_messages is not None:
                    # A tool replaced the message history - use the new messages
                    local_conversation = [m for m in messages if m.role != "system"]
                    logger.info(
                        f"Tool modified session: {len(compacted_messages)} new msgs replacing "
                        f"{len(local_conversation)} old msgs. Reconstructing conversation."
                    )
                    
                    # Reconstruct: the leading instruction block + the modified
                    # conversation + tool results. It used to be messages[0]
                    # alone, so the tools prompt — a second system message —
                    # and anything else standing at the head was dropped.
                    messages = (_instruction_head(messages, compacted_messages)
                                + list(compacted_messages) + tool_messages)
                    
                    # Clear compacted messages - they've been applied
                    self._session_tracker.clear_compacted_messages(session_id)
                    
                    # Update session storage to match
                    self._session_tracker.set_session_messages(session_id, list(compacted_messages))
                else:
                    # Normal case: no tool modified messages, just extend with tool results
                    messages.extend(tool_messages)

                if pending_intervention_msg is not None and not final_call:
                    messages.append(pending_intervention_msg)

                # Sync context.messages with the updated messages list
                context.messages = messages

                # Update tracked messages after tool execution
                self._set_live_messages(session_id, messages.copy())

                # Persist session after complete turn (tool calls + results processed).
                # Saving only at the turn boundary (not after each tool) avoids
                # orphaned tool calls; the disk save preserves progress even if
                # no SSE client is connected.
                await self._persist_conversation(
                    session_id, messages, to_disk=True,
                    note=f"after completing turn (step {step})")

                # Yield pending status events after tool execution
                for status_event in yield_pending_status_events():
                    yield status_event

                if final_call:
                    # A step's cancel is reported by the check at the top of the
                    # next iteration; after the final call there is none.
                    if self._is_cancelled(request_id):
                        async for event in cancelled_events(step):
                            yield event
                        return
                    # Tools on the final call run once: an agent that delivers
                    # through a tool (the v6 auditors) would otherwise lose the
                    # delivery its whole run was for. The budget stays a cap --
                    # no call follows to read the results -- and the run says so.
                    ran = ", ".join(
                        f"{m.name} ({'error' if self._tool_message_is_error(m) else 'ok'})"
                        for m in tool_messages) or "none"
                    logger.warning(
                        f"[{self.name}] Agent returned tool calls after max_steps limit and they "
                        f"ran without a further LLM call: {ran}. Increase max_steps or simplify the task."
                    )
                    # No final event: callers that read only final take it as
                    # success (the writer dispatch records ok=True, the job
                    # manager marks the run answered), whatever the tools
                    # returned. The results stay in the session.
                    error_msg = (f"Agent incomplete: max steps ({max_steps}) reached; "
                                 f"the final call ran tool calls ({ran}) that no step followed.")
                    results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg}
                    return

                # Continue to next iteration to let LLM respond to tool results
                continue

            # No tool calls - check if we should treat this as the final answer
            # Track consecutive responses without tool calls
            consecutive_no_tool_calls += 1
            # (prev_step_all_errored stays False → the error streak resets at the
            #  top of the next iteration; handled centrally, see loop top.)

            # === CONTINUATION HOOK SIGNAL ===
            # A post_llm_call hook (e.g. agent_continuation) may set
            # metadata["continue"] = True to prevent treating a text-only
            # response as the final answer.  This allows autonomous agents
            # to keep working when they emit intermediate status reports.
            # Not on the final call: no step is left to continue in.
            if hook_metadata.get("continue") and content and content.strip() and not final_call:
                cont_count = hook_metadata.get("continuation_count", "?")
                cont_reason = hook_metadata.get("continuation_reason", "hook signal")
                logger.info(
                    f"[{self.name}] Continuation #{cont_count} at step {step}: {cont_reason}"
                )
                # Stays a `user` turn, unlike the four notes the loop writes
                # itself. What a hook puts here is a SCRIPTED TURN -- the
                # `followups:` list in an agent's YAML is written to be said to
                # the agent, the way a person would say it -- and two readers
                # take it as one: v4's prose recovery reads the follow-up back
                # out of the stored transcript and matches it by its configured
                # TEXT (the info tool hands out no injected_by), and the
                # watchdog's judge needs it in the picture. As a volatile note
                # it would be neither a user turn nor stored at all.
                continuation_msg = ChatMessage(
                    role="user",
                    content=hook_metadata.get(
                        "continue_message",
                        "Continue with your task.",
                    ),
                    timestamp=datetime.now(timezone.utc),
                    # Marks the message as not typed by a person; a hook that
                    # scripts several turns counts its own messages by this.
                    # A hook that names no marker still gets one: unmarked, the
                    # nudge would count as a human turn.
                    injected_by=hook_metadata.get("continue_injected_by") or "post_llm_call_hook",
                )
                messages.append(continuation_msg)
                context.messages = messages
                await status_worker.progress(
                    f"Auto-continue #{cont_count}: {cont_reason}",
                    meta={"step": step + 1, "continuation": True},
                )
                # Yield event so frontend can display the injected message
                yield {
                    "type": "continuation",
                    "message": continuation_msg.content,
                    "count": cont_count,
                    "reason": cont_reason,
                    "step": step + 1,
                }
                consecutive_no_tool_calls = 0  # Reset — hook evaluated this
                continue

            # A user message may have been injected while the LLM produced this
            # response (mid-run append). Never finalize past fresh user input —
            # continue the loop so the next LLM call reacts to it. The interim
            # content was already surfaced via the thinking events above.
            # On the final call no step is left: the message stays in the
            # history behind the answer, persisted for the session's next run.
            pre_drain_count = len(messages)
            messages = await self._drain_appended_messages(request_id, messages)
            if len(messages) > pre_drain_count and not final_call:
                context.messages = messages
                self._set_live_messages(session_id, messages.copy())
                consecutive_no_tool_calls = 0
                continue

            # Cut off at the output cap with no tool call left: not an answer.
            # The call the model was writing is lost, and the text before it --
            # "now the engine, the big file:" -- is an announcement. Measured in a
            # coder session: three calls in a row stopped at 16384 tokens, each
            # ended the run as if that were its reply, and the user had to push
            # three times. The run goes on with a note saying what happened --
            # where the agent opted in (output_cap_notes), not on the final
            # call, and not past that many in a row.
            if (finish_reason == "length" and content and content.strip() and not final_call
                    and consecutive_cut_off < max_cut_off_notes):
                consecutive_cut_off += 1
                usage = (llm_out or {}).get("usage") or {}
                messages.append(self._output_cap_note(usage.get("completion_tokens")))
                context.messages = messages
                await status_worker.progress(
                    "answer cut off at the output limit -- asked to continue in parts",
                    meta={"step": step + 1, "cut_off": consecutive_cut_off})
                consecutive_no_tool_calls = 0
                continue

            # If we have content AND it's not just whitespace, treat as final answer
            if content and content.strip():
                # Assistant message was already added above before post_llm hooks
                results["summary"] = content
                # Update tracked messages with final response
                self._set_live_messages(session_id, messages.copy())

                # Use the already formatted content from above
                final_event = {"type": "final", "summary": formatted_content, "content_format": content_format}
                # Include usage data if available from last LLM call
                if llm_out and "usage" in llm_out:
                    final_event["usage"] = llm_out["usage"]
                yield final_event
                return
            
            # No tool calls AND (no content OR empty content)
            # Check consecutive no-tool-calls limit to avoid infinite loop
            # Not on the final call: an empty answer there is a spent budget,
            # which the error after the loop reports, not an empty success.
            if consecutive_no_tool_calls >= max_consecutive_no_tools and not final_call:
                logger.warning(f"Breaking loop: {consecutive_no_tool_calls} consecutive responses without tool calls (empty or no content)")
                # Treat whatever content we have as final (even if empty)
                results["summary"] = content or ""
                self._set_live_messages(session_id, messages.copy())
                final_event = {"type": "final", "summary": formatted_content or "", "content_format": content_format}
                if llm_out and "usage" in llm_out:
                    final_event["usage"] = llm_out["usage"]
                yield final_event
                return

            if final_call and not (content and content.strip()):
                # A blank answer to the final call is no answer: the error after
                # the loop reports the spent budget, and the session must not
                # end on a blank assistant turn. Drained input may follow it.
                for history in {id(messages): messages, id(context.messages): context.messages}.values():
                    for i in range(len(history or []) - 1, -1, -1):
                        if history[i] is assistant_msg:
                            del history[i]
                            break

            # Update tracked messages at end of each step (per-session)
            self._set_live_messages(session_id, messages.copy())

            # Drain any final appended messages before next step
            messages = await self._drain_appended_messages(request_id, messages)
            # Sync context.messages after draining
            context.messages = messages

        # Reached only when the final call ended without text and without a
        # tool call that ran: empty or blank, or its tool calls all blocked.
        # A step's cancel is reported at the top of the next iteration; there
        # is none after the final call.
        if self._is_cancelled(request_id):
            async for event in cancelled_events(max_steps):
                yield event
            return
        results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
        yield {"type": "error", "message": "LLM planner reached max steps without final answer."}

    async def _run_events(
        self,
        task: str,
        request_id: str,
        session_id: str,
        coordinator_request_id: str,
        worker_request_id: str,
        initial_message: Optional[ChatMessage] = None,
        llm_override: Optional[LLMClient] = None,
        llm_profile_info_override: Optional[str] = None,
        status_forwarder: Optional[StatusEventForwarder] = None,
        use_advanced_model: bool = False,
    ):
        """
        Core agent execution loop - orchestrates LLM conversation with tool usage.

        Now refactored into three focused phases (see REFACTORING_PLAN.md):
        1. Initialize: _initialize_request_and_conversation() - Setup and context building
        2. Execute: _execute_llm_loop() - Main LLM interaction loop with tool execution
        3. Finalize: _finalize_request() - Cleanup and persistence

        This orchestration method is now <150 LOC, delegating complex logic to focused helpers.

        Args:
            task: Text task description (may be empty if initial_message is provided)
            request_id: Request ID for tracking and cancellation
            session_id: Session ID for conversation history persistence
            coordinator_request_id: Request ID for coordinator status scope
            worker_request_id: Request ID for worker status scope
            initial_message: Optional ChatMessage with multimodal content
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status display
            status_forwarder: Pre-created status event forwarder (created before status_scope)

        Yields:
            Dict events: start, heartbeat, thinking, status, tool_*, final, error, cancelled, end
        """
        # Initialize state variables for access in finally block
        step = 0
        context = None
        messages = None
        results: Dict[str, Any] = {"task": task, "calls": []}

        # Register this request BEFORE emitting start event so appends work immediately
        request_entry = {
            "cancel": asyncio.Event(),
            "message_event": asyncio.Event(),
            "appended": []
        }
        self._request_manager.register_active_request(request_id, request_entry)
        self._session_tracker.register_request(request_id, session_id, request_entry)

        # Try to acquire session lock to prevent parallel requests on same session
        # This prevents race conditions when multiple browser tabs access the same session
        session_lock_timeout = self.timeouts.session_lock_timeout if self.timeouts else 5.0
        lock_acquired = await self._session_tracker.acquire_session_lock(session_id, request_id, timeout=session_lock_timeout)
        if not lock_acquired:
            # Another request is already processing this session
            is_locked, owner = self._session_tracker.check_session_locked(session_id)
            error_msg = f"Session {session_id} is currently locked by another request ({owner}). Please wait for that request to complete."
            logger.warning("Request %s failed to acquire lock for session %s (owner: %s)", 
                         request_id, session_id, owner)
            
            # CRITICAL: Clean up the request registration since we're aborting
            # Without this, the request stays registered as "active" forever
            self._request_manager.unregister_active_request(request_id)
            self._session_tracker.unregister_request(request_id)
            
            yield {"type": "error", "message": error_msg, "request_id": request_id}
            yield {"type": "end"}
            return

        # Emit start event BEFORE opening status_scope contexts
        # This ensures frontend has currentRequestId set before any status events arrive
        yield {"type": "start", "task": task, "request_id": request_id, "session_id": session_id}

        # Now open status_scope contexts - their START events will arrive AFTER the start event
        async with status_scope(status_bus, f"{self.name}_coordinator", coordinator_request_id) as status_coordinator, \
                   status_scope(status_bus, f"{self.name}_worker", worker_request_id) as status_worker:
            try:
                # Session presence: held from here on, not from the first LLM
                # call -- whoever lets go of the endpoint's hold meanwhile (a
                # client that disconnects) would leave the session looking idle
                # while this run has it, and a direct message would wake a
                # second run of it.
                self._presence_hold(session_id, request_id)

                # Phase 1: Initialize request and build conversation context
                try:
                    context = await self._initialize_request_and_conversation(
                        task=task,
                        request_id=request_id,
                        session_id=session_id,
                        initial_message=initial_message,
                        llm_override=llm_override,
                        status_forwarder=status_forwarder
                    )
                except RuntimeError as e:
                    # LLM not available - emit error and end stream
                    yield {"type": "error", "message": str(e), "request_id": request_id}
                    yield {"type": "end"}
                    return

                # Helper function to yield any pending status events from per-request forwarder
                def yield_pending_status_events():
                    if context and context.status_forwarder:
                        events = context.status_forwarder.get_pending_events()
                        for event in events:
                            yield event

                # CRITICAL: Give the forwarder task CPU time to process queued events
                # During Phase 1 (synchronous initialization), the forwarder task may not
                # have had a chance to read events from its queue. This sleep allows it to catch up.
                await asyncio.sleep(0)
                
                # CRITICAL: Yield any pending status events from Phase 1 initialization
                # This ensures START events from status_scope are delivered before LLM loop
                for status_event in yield_pending_status_events():
                    yield status_event

                # Track messages from context for updates during loop
                messages = context.messages

                # Phase 2: Execute main LLM loop with tool execution
                loop_generator = self._execute_llm_loop(
                    context=context,
                    request_id=request_id,
                    session_id=session_id,
                    status_coordinator=status_coordinator,
                    status_worker=status_worker,
                    llm_override=llm_override,
                    llm_profile_info_override=llm_profile_info_override,
                    use_advanced_model=use_advanced_model,
                )

                async for event in loop_generator:
                    yield event

                    # Yield any pending status events after each main event
                    # This ensures status messages are delivered in real-time, not batched at the end
                    for status_event in yield_pending_status_events():
                        yield status_event

                    # Track messages updates from context during loop execution
                    if context:
                        messages = context.messages

                    # Track step from events that contain step info
                    # This ensures we report accurate step count in completion message
                    if "step" in event:
                        step = event.get("step", step)
                    
                    # Capture summary and errors from events
                    if event.get("type") == "final" and "summary" in event:
                        results["summary"] = event["summary"]
                    elif event.get("type") == "error":
                        results.setdefault("errors", []).append(event.get("message", "Unknown error"))

            except Exception as e:
                logger.exception("Agent execution failed with exception:")
                yield {"type": "error", "message": f"Agent execution failed: {e}"}
                
                # CRITICAL: Cancel all sub-requests when parent agent fails
                # This ensures sub-agents don't continue running when the parent has an error
                # Uses prefix matching: request_id "abc123" will cancel "abc123_sub_xxx" etc.
                cancellation_manager = get_cancellation_manager()
                cancelled_count = cancellation_manager.cancel_request(request_id)
                if cancelled_count:
                    logger.info(f"Cancelled {cancelled_count} sub-request(s) due to parent agent error")
            finally:
                # Phase 3: Finalize and cleanup
                # Note: This runs even if generator is closed early, but we can't yield in that case
                try:
                    await self._finalize_request(
                        request_id=request_id,
                        session_id=session_id,
                        status_coordinator=status_coordinator,
                        status_worker=status_worker,
                        context=context,
                        messages=messages if messages else (context.messages if context else None),
                        results=results,
                        step=step
                    )
                finally:
                    # Session presence: after the save, so input still waiting
                    # wakes the session and the woken run finds the whole
                    # conversation on disk -- and in a finally, because a
                    # cancelled save (client gone) must not leave the session
                    # looking like it still runs.
                    self._presence_release(request_id)

            # Yield final status events and end marker
            # These won't execute if generator was closed early (GeneratorExit), which is fine
            if 'yield_pending_status_events' in locals():
                for status_event in yield_pending_status_events():
                    yield status_event

            yield {"type": "end"}


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

        # Extract session context from injected params (populated by ToolExecutionManager)
        request_id = params.get("request_id") or params.get("_request_id")
        session_id = params.get("session_id") or params.get("_session_id")

        try:
            # Execute the task using this agent
            logger.info("Agent %s executing task: %s", self.name, task[:100])
            from .result_utils import collect_final_result, extract_summary
            result = await collect_final_result(
                self, 
                str(task),
                request_id=request_id,
                session_id=session_id
            )

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
