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

#: Fallback when nothing configures a threshold: "whichever the model thinks
#: is more likely", which is the only value that needs no justification.
_DEFAULT_THRESHOLD = 0.5
#: Every strategy this hook knows. Mirrors the enum in schema.yaml, which
#: nothing enforces at load time.
_STRATEGIES = frozenset({"rules", "llm", "decision", "hybrid"})
#: Judges `hybrid` may hand over to. Mirrors the enum in schema.yaml, which
#: nothing enforces at load time.
_HYBRID_FALLBACKS = frozenset({"llm", "decision"})

#: injected_by of a scripted follow-up message — how the plugin counts them.
FOLLOWUP_MARKER = "agent_continuation.followup"


class AgentContinuationPlugin(SchemaBasedPluginHook):
    """Schema-based hook plugin for autonomous agent continuation."""

    def __init__(
        self,
        plugin_dir: Path | str,
        server_config: Any = None,
    ) -> None:
        super().__init__(plugin_dir)

        # Merge schema defaults with runtime config from plugins.yaml
        config = self.get_config()
        if server_config and hasattr(server_config, "config") and server_config.config:
            config.update(server_config.config)

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

        # Decision-model evaluator. Empty profile = whatever
        # llm_system.default_decision_profile names.
        self._decision_profile: str = str(config.get("decision_profile", "") or "")
        self._decision_question: str = str(config.get("decision_question", ""))
        self._decision_final_means: str = str(config.get("decision_final_means", ""))
        self._decision_continue_means: str = str(config.get("decision_continue_means", ""))
        self._decision_threshold: float = self._parse_threshold(
            config.get("decision_threshold"), _DEFAULT_THRESHOLD, "plugin config")
        self._hybrid_fallback: str = str(config.get("hybrid_fallback", "llm"))

        # Per-request continuation counter  request_id → count
        self._continuation_counts: Dict[str, int] = {}

        # Cached evaluator LLM instance (lazy)
        self._evaluator_llm: Any = None
        # Cached decision-model client (lazy), keyed by the profile that built
        # it: an agent may override the profile, and one cache slot would hand
        # the first agent's judge to every other agent.
        self._decision_clients: Dict[str, Any] = {}

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
                  max_continuations: 20
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

    def _resolve_max_continuations(
        self, agent_cfg: Dict[str, Any], agent_name: str
    ) -> int:
        """Continuation budget for this agent: its own value, else the plugin's.

        An agent that drives a long autonomous loop needs a different ceiling
        than a reviewer that should answer in three turns, and it configures
        that next to its rules. Before this resolution the key was accepted and
        ignored, so twenty agents carried a number that did nothing — including
        two asking for MORE than the plugin default and silently getting less.

        A non-numeric or non-positive value falls back to the plugin value: a
        budget of 0 would disable the whole hook through a typo, which is not
        what someone writing ``max_continuations`` means.
        """
        raw = agent_cfg.get("max_continuations")
        if raw is None:
            return self._max_continuations
        # bool before int(): YAML turns `true` into True and int(True) is 1,
        # so a typo would silently buy exactly one continuation.
        if isinstance(raw, bool):
            logger.warning(
                "[AgentContinuation] '%s': max_continuations=%r is a boolean — "
                "using the plugin value %d",
                agent_name, raw, self._max_continuations)
            return self._max_continuations
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[AgentContinuation] '%s': max_continuations=%r is not a "
                "number — using the plugin value %d",
                agent_name, raw, self._max_continuations)
            return self._max_continuations
        if value < 1:
            logger.warning(
                "[AgentContinuation] '%s': max_continuations=%d is below 1 — "
                "using the plugin value %d",
                agent_name, value, self._max_continuations)
            return self._max_continuations
        return value

    @staticmethod
    def _parse_threshold(raw: Any, fallback: float, where: str) -> float:
        """A probability, or the value one level up.

        Guarded the way max_continuations above is guarded, and for a sharper
        reason. ``decision_threshold: 70`` -- meant as a percentage, and
        invited by a description that says "raise it" -- is a perfectly good
        float, so a type check alone lets it through. Every probability is
        then below it, every response "continues", and each one pays for a
        decision call until the budget runs out. A config typo that spends
        money in a loop has to be refused, not rounded.

        The schema declares minimum/maximum, but nothing enforces those at
        load time (config_defaults_from_schema reads only `default`), so this
        is the only place the range is real.
        """
        if raw is None:
            return fallback
        # bool before float(): YAML turns `yes` into True and float(True) is
        # 1.0, a threshold that continues on everything but a certain answer.
        if isinstance(raw, bool):
            logger.warning(
                "[AgentContinuation] %s: decision_threshold=%r is a boolean — "
                "using %s", where, raw, fallback)
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[AgentContinuation] %s: decision_threshold=%r is not a "
                "number — using %s", where, raw, fallback)
            return fallback
        if not 0.0 <= value <= 1.0:
            logger.warning(
                "[AgentContinuation] %s: decision_threshold=%s is not a "
                "probability (0.0-1.0) — using %s. A percentage does not "
                "belong here: 70 would continue on every answer.",
                where, value, fallback)
            return fallback
        return value

    @staticmethod
    def _text(cfg: Dict[str, Any], key: str, fallback: str) -> str:
        """A configured text, where an EMPTY one is an answer, not a gap.

        `.get(key, fallback)` rather than `cfg.get(key) or fallback`: an agent
        writing `decision_continue_means: ""` to drop that criterion means it,
        and the `or` form would hand it the plugin-wide text instead -- while
        the same "" at plugin level does work. A key written with no value at
        all (`decision_question:` -> None) is a slip, not a choice, and still
        inherits.
        """
        raw = cfg.get(key, fallback)
        return str(fallback if raw is None else raw)

    @staticmethod
    def _followups_on_continue(agent_cfg: Dict[str, Any], agent_name: str) -> bool:
        """``followups_on_continue``: only a real boolean counts.

        A quoted ``"false"`` is a string, and a string compared with ``is False``
        silently leaves the option on — a continued session would then get the
        follow-up. Anything but a boolean keeps the default and says so.
        """
        raw = agent_cfg.get("followups_on_continue")
        if raw is None:
            return True
        if isinstance(raw, bool):
            return raw
        logger.warning(
            "[AgentContinuation] '%s': followups_on_continue=%r is not a boolean — "
            "follow-ups stay on for continued requests",
            agent_name, raw)
        return True

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

        # Resolve the per-agent config BEFORE the budget check: the budget is
        # one of the keys an agent may override, and a check against the
        # plugin-wide value would ignore it.
        agent_cfg = self._get_agent_config(context)
        max_continuations = self._resolve_max_continuations(agent_cfg, agent_name)

        # Budget check — use request_id for per-request tracking
        request_id = context.request_id
        count = self._continuation_counts.get(request_id, 0)
        if count >= max_continuations:
            # Also the ceiling for follow-ups: none is offered past it, so a
            # history that lost its markers cannot replay them forever.
            logger.warning(
                f"[AgentContinuation] Max continuations ({max_continuations}) "
                f"reached for request {request_id}"
            )
            self._continuation_counts.pop(request_id, None)
            return HookResult(success=True, modified=False)

        # Pick strategy (per-agent overrides global)
        strategy = agent_cfg.get("strategy") or self._strategy

        if strategy not in _STRATEGIES:
            # Silently inert since the plugin was written -- no branch below
            # matches, so the hook answers FINAL forever and looks switched
            # off. Worth a word now that there are four names and one of them,
            # "decision", is a letter away from the config key, the plugin
            # directory and the plural somebody will type. The BEHAVIOUR stays
            # inert on purpose: quietly running the rules instead would let a
            # keyword continue a loop nobody configured.
            logger.warning(
                f"[AgentContinuation] '{agent_name}': unknown strategy="
                f"{strategy!r} (known: {sorted(_STRATEGIES)}) — nothing is "
                f"evaluated, every response counts as final"
            )

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
        elif strategy == "decision":
            should_continue, reason = await self._evaluate_decision(
                content, agent_name, context, agent_cfg
            )
        elif strategy == "hybrid":
            should_continue, reason, matched = self._evaluate_rules(
                content, agent_name, context
            )
            if not matched:
                # No keyword matched — let the configured judge decide. Which
                # one matters more here than it looks: the decision model is
                # billed per call, so running the free rules first is the
                # point of hybrid, not a detail of it.
                fallback = str(
                    (agent_cfg or {}).get("hybrid_fallback") or self._hybrid_fallback
                ).strip()
                if fallback not in _HYBRID_FALLBACKS:
                    # Nothing enforces the schema's enum, and the plural
                    # "decisions" is one letter from the strategy name, the
                    # config key and the plugin directory. Silently routing to
                    # the other judge would bill a model the log did not name.
                    logger.warning(
                        f"[AgentContinuation] '{agent_name}': unknown "
                        f"hybrid_fallback={fallback!r} (known: "
                        f"{sorted(_HYBRID_FALLBACKS)}) — using 'llm'"
                    )
                    fallback = "llm"
                logger.info(
                    f"[AgentContinuation] No keyword matched for '{agent_name}' "
                    f"— falling back to {fallback} evaluation"
                )
                if fallback == "decision":
                    should_continue, reason = await self._evaluate_decision(
                        content, agent_name, context, agent_cfg
                    )
                else:
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
                    # Marked, or follow-up counting would take it for a
                    # message a person wrote and start the list again.
                    "continue_injected_by": "agent_continuation",
                    "continuation_count": count + 1,
                    "continuation_reason": reason,
                },
            )

        # A final answer — unless a scripted follow-up is still due.
        followup = self._next_followup(context, agent_cfg)
        if followup is not None:
            index, message = followup
            self._continuation_counts[request_id] = count + 1
            logger.info(
                f"[AgentContinuation] Follow-up {index + 1} for '{agent_name}'"
            )
            return HookResult(
                success=True,
                modified=False,
                metadata={
                    "continue": True,
                    "continue_message": message,
                    "continue_injected_by": FOLLOWUP_MARKER,
                    "continuation_count": count + 1,
                    "continuation_reason": f"follow-up {index + 1}",
                },
            )

        # Final answer — clean up counter
        self._continuation_counts.pop(request_id, None)
        return HookResult(success=True, modified=False)

    # ------------------------------------------------------------------
    # Scripted follow-ups
    # ------------------------------------------------------------------

    def _next_followup(
        self, context: HookContext, agent_cfg: Dict[str, Any]
    ) -> Tuple[int, str] | None:
        """(index, message) of the follow-up due after this final answer, or None.

        No state: the follow-ups already sent are the messages marked
        FOLLOWUP_MARKER after the last user message a person wrote (one with
        no ``injected_by``). A new request starts the list again; a cancelled
        run leaves nothing behind.

        ``followups_on_continue: false`` limits the list to the first request
        of a session: when an assistant answer precedes that user message, the
        request continues an earlier one (a pipeline asking a scorer to
        re-check or to assign ids) and gets no follow-up. The evidence is the
        history the hook sees: once a summarizer or pruning has replaced the
        earlier answers, a continued request looks like a first one.
        """
        raw = agent_cfg.get("followups") or []
        if isinstance(raw, str):
            # A single message written without the list dash: iterating the
            # string would send every character as its own follow-up.
            raw = [raw]
        followups = [str(item).strip() for item in raw if str(item).strip()]
        if not followups:
            return None
        messages = list(context.messages or [])
        sent = 0
        request_start = 0
        for pos in range(len(messages) - 1, -1, -1):
            msg = messages[pos]
            if getattr(msg, "role", None) != "user":
                continue
            marker = getattr(msg, "injected_by", None)
            if marker is None:
                request_start = pos
                break
            if marker == FOLLOWUP_MARKER:
                sent += 1
        if not self._followups_on_continue(agent_cfg, context.agent_name) and any(
            getattr(msg, "role", None) == "assistant" for msg in messages[:request_start]
        ):
            return None
        if sent >= len(followups):
            return None
        return sent, followups[sent]

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

    async def _evaluate_decision(
        self,
        content: str,
        agent_name: str,
        context: HookContext,
        agent_cfg: Dict[str, Any] | None = None,
    ) -> Tuple[bool, str]:
        """Ask a decision model the one question that decides this.

        Different from the LLM evaluator in the thing that matters: the answer
        is a PROBABILITY, not a word. "FINAL" from a chat model says nothing
        about how close the call was; 0.83 and 0.51 are both "final" and only
        one of them deserves to be trusted. The number is logged for that
        reason, and `decision_threshold` is where a run says how sure it wants
        to be before it stops -- 0.5 means "whichever the model thinks is more
        likely", which is the honest default.

        Every failure resolves to FINAL, like the LLM path: a judge that
        cannot answer must not put the loop into another turn.
        """
        cfg = agent_cfg or {}
        question = self._text(cfg, "decision_question", self._decision_question).strip()
        if not question:
            logger.warning(
                f"[AgentContinuation] No decision_question configured for "
                f"'{agent_name}' — defaulting to FINAL"
            )
            return False, "no decision_question configured"

        client = self._get_decisions_client(context, cfg)
        if client is None:
            return False, "no decisions client available"

        # .strip(): a folded YAML scalar ends in a newline, and every one of
        # those is a token paid for on every call.
        criteria = {
            "true": self._text(cfg, "decision_final_means",
                               self._decision_final_means).strip(),
            "false": self._text(cfg, "decision_continue_means",
                                self._decision_continue_means).strip(),
        }
        # An empty half is worse than none: it would tell the model that this
        # case means nothing. Send the pair only when both sides say something.
        if not (criteria["true"] and criteria["false"]):
            criteria = None
        threshold = self._parse_threshold(
            cfg.get("decision_threshold"), self._decision_threshold,
            f"agent {agent_name!r}")

        question_body: Dict[str, Any] = {"type": "noul", "instructions": question}
        if criteria:
            question_body["criteria"] = criteria

        try:
            result = await client.decide(
                # A mapping, not one glued string: the model is told which part
                # is the agent and which is its answer. Measured to be accepted.
                {"agent": agent_name, "response": content[:3000]},
                {"final_answer": question_body},
                cancellation_token=context.cancellation_token,
                session_id=context.session_id,
            )
            probability = float(result["final_answer"].value)
        except Exception as e:
            # A user cancel is NOT caught here: CancelledError is a
            # BaseException, so it travels on rather than being turned into a
            # verdict of any kind. (Swallowed, it would read as FINAL -- and
            # on the follow-up path below, FINAL is what still injects the
            # next scripted message.) Today the token is None for
            # POST_LLM_CALL anyway: execute_post_llm_hooks does not take one,
            # so a cancel arriving DURING the call cannot happen yet. Passed
            # on regardless, so this path is right when it does.

            logger.warning(f"[AgentContinuation] Decision evaluation failed: {e}")
            return False, f"decision error: {e}"

        should_continue = probability < threshold
        logger.info(
            f"[AgentContinuation] Decision model says "
            f"{'CONTINUE' if should_continue else 'FINAL'} for '{agent_name}' "
            f"(p(final)={probability}, threshold={threshold}, "
            f"cost={result.cost}, model={result.model})"
        )
        return should_continue, (
            f"decision model: p(final)={probability} < {threshold}"
            if should_continue
            else f"decision model: p(final)={probability} >= {threshold}"
        )

    def _get_decisions_client(
        self, context: HookContext, agent_cfg: Dict[str, Any]
    ) -> Any:
        """Lazily build the decision-model client for this agent's profile."""
        profile = str(agent_cfg.get("decision_profile")
                      or self._decision_profile or "")
        if profile in self._decision_clients:
            return self._decision_clients[profile]

        if not context.agent or not hasattr(context.agent, "system_config"):
            logger.warning(
                "[AgentContinuation] No system_config — cannot create decisions client"
            )
            return None
        try:
            from agent_system.llm.decisions import create_decisions_from_profile

            # No profile name = llm_system.default_decision_profile decides.
            client = create_decisions_from_profile(
                context.agent.system_config, profile or None)
        except Exception as e:
            logger.error(
                f"[AgentContinuation] Failed to create decisions client "
                f"(profile={profile or 'default'}): {e}"
            )
            return None
        self._decision_clients[profile] = client
        logger.info(
            f"[AgentContinuation] Created decisions client "
            f"(profile='{profile or 'default'}', model={client.model})"
        )
        return client

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
