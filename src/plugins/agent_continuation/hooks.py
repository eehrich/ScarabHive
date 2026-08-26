"""Agent Continuation Plugin — Hook Logic.

Evaluates every text-only LLM response (no tool_calls) to decide whether
it is a final answer or an intermediate status report.  When the response
looks intermediate the hook returns ``metadata["continue"] = True`` which
the core LLM loop picks up to inject a continuation user-message instead
of exiting.

Strategies
----------
* **rules** — fast, deterministic keyword / step / length checks.
* **llm** — calls a lightweight LLM (e.g. turbo) to classify the response.
* **hybrid** — rules first; if no keyword matched, fall back to LLM.

Rule Types
----------
* **keyword_continue** / **keyword_final** — substring matching (case-insensitive)
  or regex patterns if ``regex: true`` is set.
* **min_length** — continue if response shorter than threshold.
* **step_check** — continue if current step below minimum.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)


class AgentContinuationPlugin(SchemaBasedPluginHook):
    """Schema-based hook plugin for autonomous agent continuation."""

    def __init__(
        self,
        plugin_dir: Path | str,
        mcp_config: Any = None,
    ) -> None:
        super().__init__(plugin_dir)

        # Merge schema defaults with runtime config from plugins.yaml
        config = self.get_config()
        if mcp_config and hasattr(mcp_config, "config") and mcp_config.config:
            config.update(mcp_config.config)

        self._max_continuations: int = int(config.get("max_continuations", 10))
        self._default_continue_message: str = str(
            config.get(
                "default_continue_message",
                "Continue with your task.",
            )
        ).strip()
        self._strategy: str = str(config.get("strategy", "rules"))
        self._llm_profile: str = str(config.get("llm_profile", "turbo"))
        self._llm_prompt_template: str = str(config.get("llm_prompt", ""))
        self._agent_rules: Dict[str, Any] = dict(config.get("agent_rules", {}))

        # Per-request continuation counter  request_id → count
        self._continuation_counts: Dict[str, int] = {}

        # Cached evaluator LLM instance (lazy)
        self._evaluator_llm: Any = None

        logger.info(
            f"[AgentContinuation] Initialized: strategy={self._strategy}, "
            f"max_continuations={self._max_continuations}, "
            f"agent_rules={list(self._agent_rules.keys()) or 'global defaults'}"
        )

    # ------------------------------------------------------------------
    # Agent config resolution
    # ------------------------------------------------------------------

    def _get_agent_config(self, context: HookContext) -> Dict[str, Any]:
        """Resolve per-agent config for the current agent.

        Resolution order (highest priority first):
        1. ``context.hook_config`` — auto-populated by the hook registry
           from the agent's ``hooks.overrides`` (system keys stripped).
        2. ``agent_rules[agent_name]`` from ``plugins.yaml``
        3. Empty dict (global defaults apply)

        This allows agents to carry their continuation config directly::

            hooks:
              overrides:
                agent_continuation.evaluate_completion:
                  enabled: true
                  strategy: "rules"
                  default: "continue"
                  continue_message: "Keep working!"
                  rules:
                    - type: keyword_final
                      keywords: ["done"]
        """
        # 1. Agent-level hook_config (injected by registry)
        if context.hook_config:
            return context.hook_config

        # 2. Fall back to agent_rules from plugins.yaml
        return self._agent_rules.get(context.agent_name, {})

    # ------------------------------------------------------------------
    # Hook handler (must match name in schema.yaml)
    # ------------------------------------------------------------------

    async def evaluate_completion(self, context: HookContext) -> HookResult:
        """POST_LLM_CALL hook: decide if response is final or intermediate."""

        # Only evaluate text responses (no tool_calls → potential final answer)
        assistant = (context.llm_response or {}).get("assistant", {})
        tool_calls = assistant.get("tool_calls")
        content: str = assistant.get("content") or ""

        if tool_calls or not content.strip():
            # Has tool calls (loop continues anyway) or empty → skip
            logger.debug(
                f"[AgentContinuation] Skipping '{context.agent_name}' step {context.step}: "
                f"tool_calls={bool(tool_calls)} ({len(tool_calls) if isinstance(tool_calls, list) else type(tool_calls).__name__}), "
                f"content_empty={not content.strip()}"
            )
            return HookResult(success=True, modified=False)

        agent_name = context.agent_name

        # Note: no agent gating here — the hook registry already ensures
        # this only fires for agents that opt-in via hooks.overrides.

        # Budget check — use request_id for per-request tracking
        request_id = context.request_id
        count = self._continuation_counts.get(request_id, 0)
        if count >= self._max_continuations:
            logger.warning(
                f"[AgentContinuation] Max continuations ({self._max_continuations}) "
                f"reached for request {request_id}"
            )
            self._continuation_counts.pop(request_id, None)
            return HookResult(success=True, modified=False)

        # Pick strategy (per-agent overrides global)
        agent_cfg = self._get_agent_config(context)
        strategy = agent_cfg.get("strategy") or self._strategy

        logger.debug(
            f"[AgentContinuation] Evaluating '{agent_name}' step {context.step}, "
            f"strategy={strategy}, content_len={len(content)}, "
            f"hook_config_keys={list(agent_cfg.keys())}"
        )

        should_continue = False
        reason = ""

        if strategy == "rules":
            should_continue, reason, _matched = self._evaluate_rules(
                content, agent_name, context
            )
        elif strategy == "llm":
            should_continue, reason = await self._evaluate_llm(
                content, agent_name, context, agent_cfg
            )
        elif strategy == "hybrid":
            should_continue, reason, matched = self._evaluate_rules(
                content, agent_name, context
            )
            if not matched:
                # No keyword matched — let LLM decide
                logger.info(
                    f"[AgentContinuation] No keyword matched for '{agent_name}' "
                    f"— falling back to LLM evaluation"
                )
                should_continue, reason = await self._evaluate_llm(
                    content, agent_name, context, agent_cfg
                )

        logger.debug(
            f"[AgentContinuation] Decision for '{agent_name}': "
            f"should_continue={should_continue}, reason={reason}"
        )

        if should_continue:
            self._continuation_counts[request_id] = count + 1
            continue_msg = (
                agent_cfg.get("continue_message")
                or self._default_continue_message
            )
            logger.info(
                f"[AgentContinuation] Continuing agent '{agent_name}' "
                f"(#{count + 1}): {reason}"
            )
            return HookResult(
                success=True,
                modified=False,  # Don't modify the LLM response content
                metadata={
                    "continue": True,
                    "continue_message": continue_msg,
                    "continuation_count": count + 1,
                    "continuation_reason": reason,
                },
            )

        # Final answer — clean up counter
        self._continuation_counts.pop(request_id, None)
        return HookResult(success=True, modified=False)

    # ------------------------------------------------------------------
    # Rule engine
    # ------------------------------------------------------------------

    def _evaluate_rules(
        self,
        content: str,
        agent_name: str,
        context: HookContext,
    ) -> Tuple[bool, str, bool]:
        """Rule-based evaluation.  Fast, deterministic, no LLM cost.

        Keyword rules are collected across ALL rule blocks before making a
        decision.  When both ``keyword_continue`` and ``keyword_final``
        match the same response, ``keyword_continue`` wins — it is safer
        to continue than to stop prematurely.  Structural rules
        (``min_length``, ``step_check``) still short-circuit immediately.

        Returns
        -------
        (should_continue, reason, matched)
            *matched* is True when at least one keyword / structural rule
            fired.  When False the caller knows no rule was decisive —
            useful for the hybrid strategy where no-match falls to LLM.
        """

        agent_cfg = self._get_agent_config(context)
        rules: List[Dict[str, Any]] = agent_cfg.get("rules", [])
        content_lower = content.lower()

        # Collect keyword matches from ALL rule blocks before deciding.
        # This prevents rule-order from determining the outcome when both
        # keyword_continue and keyword_final match the same text.
        continue_matches: List[str] = []
        final_matches: List[str] = []

        for rule in rules:
            rule_type = rule.get("type")
            use_regex = rule.get("regex", False)

            if rule_type == "keyword_continue":
                for kw in rule.get("keywords", []):
                    if use_regex:
                        try:
                            if re.search(kw, content, re.IGNORECASE):
                                continue_matches.append(
                                    f"keyword_continue regex match: '{kw}'"
                                )
                        except re.error as e:
                            logger.warning(
                                f"[AgentContinuation] Invalid regex '{kw}': {e}"
                            )
                    else:
                        if kw.lower() in content_lower:
                            continue_matches.append(
                                f"keyword_continue match: '{kw}'"
                            )

            elif rule_type == "keyword_final":
                for kw in rule.get("keywords", []):
                    if use_regex:
                        try:
                            if re.search(kw, content, re.IGNORECASE):
                                final_matches.append(
                                    f"keyword_final regex match: '{kw}'"
                                )
                        except re.error as e:
                            logger.warning(
                                f"[AgentContinuation] Invalid regex '{kw}': {e}"
                            )
                    else:
                        if kw.lower() in content_lower:
                            final_matches.append(
                                f"keyword_final match: '{kw}'"
                            )

            # Structural rules still short-circuit (unambiguous signals)
            elif rule_type == "min_length":
                min_chars = int(rule.get("min_chars", 200))
                if len(content) < min_chars:
                    return True, f"response too short ({len(content)} < {min_chars})", True

            elif rule_type == "step_check":
                min_steps = int(rule.get("min_steps", 3))
                if context.step < min_steps:
                    return True, f"below min steps ({context.step} < {min_steps})", True

        # Decide based on collected keyword matches.
        # keyword_continue wins over keyword_final when both match.
        if continue_matches and final_matches:
            reason = (
                f"{continue_matches[0]} (overrides {final_matches[0]}, "
                f"continue wins when both match)"
            )
            logger.info(
                f"[AgentContinuation] Both keyword_continue and keyword_final "
                f"matched for '{agent_name}' — continue wins. "
                f"continue={continue_matches}, final={final_matches}"
            )
            return True, reason, True
        if continue_matches:
            return True, continue_matches[0], True
        if final_matches:
            return False, final_matches[0], True

        # Default action when no rule matched
        default_action = agent_cfg.get("default", "final")
        if default_action == "continue":
            return True, "default action is continue", False
        return False, "no rule matched, default final", False

    # ------------------------------------------------------------------
    # LLM evaluator
    # ------------------------------------------------------------------

    async def _evaluate_llm(
        self,
        content: str,
        agent_name: str,
        context: HookContext,
        agent_cfg: Dict[str, Any] | None = None,
    ) -> Tuple[bool, str]:
        """Call a lightweight LLM to classify the response."""

        llm = self._get_evaluator_llm(context)
        if llm is None:
            logger.warning(
                f"[AgentContinuation] No evaluator LLM available for '{agent_name}' "
                f"— defaulting to FINAL"
            )
            return False, "no evaluator LLM available"

        # Resolve prompt: per-agent hook_config overrides plugin-level default
        prompt = (
            (agent_cfg or {}).get("llm_prompt")
            or self._llm_prompt_template
        )
        if not prompt or not prompt.strip():
            logger.warning(
                f"[AgentContinuation] No llm_prompt configured for '{agent_name}' "
                f"— cannot evaluate via LLM, defaulting to FINAL"
            )
            return False, "no llm_prompt configured"

        prompt = prompt.replace("{{ agent_name }}", agent_name)
        prompt = prompt.replace("{{ response }}", content[:3000])

        try:
            logger.debug(
                f"[AgentContinuation] Calling evaluator LLM for '{agent_name}' "
                f"(prompt_len={len(prompt)}, content_len={len(content)})"
            )
            response = await llm.chat(
                messages=[
                    ChatMessage(
                        role="user",
                        content=prompt,
                        timestamp=datetime.now(),
                    )
                ],
                cancellation_token=context.cancellation_token,
            )
            # llm.chat() returns str, not dict (unlike chat_tools)
            if isinstance(response, str):
                answer = response.strip().upper()
            else:
                # Fallback for dict response format
                answer = (
                    response.get("assistant", {}).get("content", "").strip().upper()
                )
            if "CONTINUE" in answer:
                logger.info(
                    f"[AgentContinuation] LLM evaluator says CONTINUE "
                    f"for '{agent_name}' (raw='{answer[:50]}')"
                )
                return True, "LLM evaluation: CONTINUE"
            logger.info(
                f"[AgentContinuation] LLM evaluator says FINAL "
                f"for '{agent_name}' (raw='{answer[:50]}')"
            )
            return False, "LLM evaluation: FINAL"
        except Exception as e:
            logger.warning(f"[AgentContinuation] LLM evaluation failed: {e}")
            return False, f"LLM evaluation error: {e}"

    def _get_evaluator_llm(self, context: HookContext) -> Any:
        """Lazily create a lightweight LLM for evaluation."""

        if self._evaluator_llm is not None:
            return self._evaluator_llm

        if not context.agent or not hasattr(context.agent, "system_config"):
            logger.warning(
                "[AgentContinuation] No system_config — cannot create evaluator LLM"
            )
            return None

        try:
            # create_llm_from_profile forwards EVERY resolved field. Listing the
            # factory arguments by hand (pre-registry make_llm) dropped thinking_level, max_tokens,
            # safety_settings, service_tier and provider_routing - harmless for
            # the profile configured today, silently wrong the moment this points
            # at an OpenRouter profile. It also gets batch wrapping right, which
            # the hand-rolled call never did.
            from agent_system.llm.factory import create_llm_from_profile

            ssl_verify = None
            try:
                ssl_verify = context.agent.system_config.network.ssl_verify
            except Exception:
                pass

            self._evaluator_llm = create_llm_from_profile(
                context.agent.system_config, self._llm_profile,
                ssl_verify=ssl_verify)
            model_name = getattr(
                self._evaluator_llm, "model_name", "unknown"
            )
            logger.info(
                f"[AgentContinuation] Created evaluator LLM "
                f"(profile='{self._llm_profile}', model={model_name})"
            )
            return self._evaluator_llm
        except Exception as e:
            logger.error(
                f"[AgentContinuation] Failed to create evaluator LLM: {e}"
            )
            return None
