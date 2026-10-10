"""The LLM clients a run switches to, and the per-request guards that decide a switch.

The clients besides the agent's own -- the caller's profile (inherit_parent_llm), the advanced
profile of stuck escalation, the fallback profiles -- built and kept here; the client answering a
session's running step (llm_for_session); and the guards a run builds per request: the stuck
escalator and what it counts, the tool-call loop detector. The step loop (llm_loop/) decides when
to switch; this module answers with what. Kept apart from the loop: plugins ask llm_for_session, and
tests patch these methods one by one.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, List, Optional

from ....utils.reasoning_artifacts import strip_all_reasoning_artifacts
from ....llm.models import ChatMessage, LLMClient
from ..components.tool_execution import tool_result_is_error
from ..loop_detection import ToolCallLoopDetector
from ..escalation import StuckEscalator

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


class LLMSelectionMixin:
    """Escalation, fallback and caller clients, and the per-request guards (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``system_config``, ``agent_config``, ``llm``,
    ``_hook_manager``, ``_loop_detection_config``, ``_fallback_clients`` and ``_step_llms``;
    ``_escalation_llm_cached`` is set here, on first use.
    """

    def _create_loop_detector(self: Agent) -> ToolCallLoopDetector:
        """Create a fresh loop detector for a single request.

        Each request gets its own detector so concurrent requests don't
        interfere, and previous-request history doesn't leak into new requests.
        """
        return ToolCallLoopDetector(**self._loop_detection_config)

    def _switches_model(self: Agent, llm_override: Optional[LLMClient]) -> bool:
        """Whether *llm_override* takes the run off the agent's own models -- then stuck escalation stays off.
        A profile of its own chain is one it runs on anyway: its primary with params only (--llm-params, a
        machine call's llm_params), or a cheaper member a machine call picks (llm_profile). Its advanced
        profile, a foreign one (--llm) or a client that does not say which count as a switch."""
        if llm_override is None:
            return False
        cfg = self.agent_config
        chain = (cfg.llm_profile if isinstance(cfg.llm_profile, list) else [cfg.llm_profile]) if cfg else []
        profile = getattr(llm_override, "profile_name", None)
        return profile is None or profile not in chain or profile == cfg.advanced_llm_profile

    def _create_stuck_escalator(self: Agent, *, already_advanced: bool) -> StuckEscalator:
        """Per-request escalator (window + budget state must not leak across
        requests on this shared Agent singleton). Disabled — a no-op — when the
        config flag is off, no advanced profile exists, or the run is already on
        the advanced model (nothing to escalate to)."""
        cfg = self.agent_config
        # Equality guard mirrors _get_escalation_llm: advanced == default
        # cannot build a different client — the escalator would be a dead trigger.
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
        return tool_result_is_error(data)

    def llm_for_session(self: Agent, session_id: Optional[str]) -> Optional[LLMClient]:
        """The client answering the session's running step, else the agent's own.

        For tools that size the context themselves: agent.llm is the configured
        model, not the fallback, escalation or override that answers.
        """
        return self._step_llms.get(session_id) if session_id in self._step_llms else self.llm

    def _get_escalation_llm(self: Agent) -> Optional[LLMClient]:
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
            from ....llm.factory import override_for_profile
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

    def _llm_from_caller(self: Agent) -> Optional[tuple[LLMClient, str]]:
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
        from ....llm.caller_llm import caller_llm_profile
        profile = caller_llm_profile()
        if not profile:
            return None
        try:
            from ....llm.factory import override_for_profile
            client, label = override_for_profile(self.system_config, self.agent_config, profile)
        except Exception as e:
            logger.warning("[%s] cannot run on the caller's LLM profile %r, runs its own: %s",
                           self.name, profile, e)
            return None
        logger.info("[%s] runs on the caller's LLM profile %s", self.name, profile)
        return client, f"{label} (from caller)"

    def _profile_to_hand_down(self: Agent, llm_override: Optional[LLMClient]) -> Optional[str]:
        """The profile this run hands to the sub-agents its tools start: the one
        it was switched to, else None. An override on the agent's own primary
        profile (--llm-params alone, a web pick of the same profile) switched
        nothing -- unless the run followed its caller onto it: that switch came
        from above and goes on down.
        """
        from ....llm.caller_llm import caller_llm_profile
        profile = getattr(llm_override, "profile_name", None)
        if (profile and self.agent_config and profile == self.agent_config.default_llm_profile
                and profile != caller_llm_profile()):
            return None
        return profile

    def _extract_profile_info(self: Agent, config, agent_name: str, llm_kwargs: dict) -> str:
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

    def _create_fallback_llm(self: Agent, fallback_profile: str) -> Optional[LLMClient]:
        """Create an LLM client for a fallback profile.
        
        Args:
            fallback_profile: Name of the fallback LLM profile to use
            
        Returns:
            LLM client instance or None if creation fails
        """
        try:
            from ....llm.factory import create_llm_from_profile

            # Fallbacks run with the SAME llm_params semantics as the
            # primary model — create_llm_from_profile resolves the profile-
            # keyed params itself ("*"/flat for the whole chain,
            # exact entry wins). No special handling here.
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

    def _fallback_client(self: Agent, profile: str) -> Optional[LLMClient]:
        """The client of a fallback profile, built on first use and kept: a step
        that walks around a blocked LLM would otherwise build one per step. A
        profile that did not build is tried again next time."""
        client = self._fallback_clients.get(profile)
        if client is None:
            client = self._create_fallback_llm(profile)
            if client is not None:
                self._fallback_clients[profile] = client
        return client

    def _strip_for_switch(self: Agent, profile: str, messages: List[ChatMessage]) -> None:
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
