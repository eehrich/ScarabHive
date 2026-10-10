"""Which LLM answers a step: the pick before the hooks, the re-pick after them, and the fallback.

A step runs on the escalation model, the override or the run's base -- unless model_health has that
LLM blocked, then on the first free profile of the chain (docs/_arch_agent_architecture.md, "How a
Step Chooses Its LLM"). The pick happens before the pre-LLM hooks, which size the context by it, and
again after them when a block was set or lifted meanwhile. A failed call takes the next fallback
(_take_fallback) and switches to it (_switch_to_fallback); when to do that is the retry loop's
decision (fallback.py). The clients themselves are built in llm_selection.py.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from .....llm.model_health import model_health
from .....utils.reasoning_artifacts import (
    strip_all_reasoning_artifacts,
    strip_foreign_reasoning_artifacts,
)
from .state import LoopState, StepState

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


class StepLLMMixin:
    """The step's LLM and its fallbacks (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``agent_config`` and ``_step_llms``, and on
    llm_selection.py for the clients (``_get_escalation_llm``, ``_fallback_client``,
    ``_strip_for_switch``).
    """

    def _pick_step_llm(self: Agent, run: LoopState, escalate: bool) -> Tuple[Any, List[str], bool, Optional[str]]:
        """(client, fallback chain, escalated, fallback profile or None)
        for this step. Spends no budget.

        The wanted client — the escalation, the override or the run's
        base — when its LLM is not blocked (llm/model_health.py); else
        the first unblocked profile of the chain; else the wanted one
        all the same: a blocked LLM beats no LLM. Coming back once the
        block is lifted is a switch like any other, stripped below.
        """
        llm = run.active_llm
        # Profile of the model that is ACTUALLY active, when it differs
        # from the config primary: escalation swap or explicit override
        # (llm_profile_info_override = "profile:provider/model"). It is
        # excluded from the fallback chain, otherwise the model that
        # just failed would run again as its own fallback.
        active_profile_override = None
        if run.base_profile is not None:
            active_profile_override = run.base_profile
        elif run.llm_override is not None and run.llm_profile_info_override:
            active_profile_override = run.llm_profile_info_override.split(":", 1)[0]
        if escalate:
            escalation_llm = self._get_escalation_llm()
            if escalation_llm is None or not run.takes_format(escalation_llm):
                # Advanced client couldn't be built — ran on standard.
                # Disable escalation for this run so we don't retry the
                # build every step (the window would never close).
                # Same for one that cannot take the run's structured
                # output: it will not learn to within the run.
                escalate = False
                run.escalator.disable()
            elif model_health.available(escalation_llm, run.request_id):
                llm = escalation_llm
                active_profile_override = (
                    self.agent_config.advanced_llm_profile
                    if self.agent_config else None
                )
            else:
                # Blocked for now: this step runs on standard, the
                # window stays open for a step after the block.
                escalate = False
        # Chain semantics: llm_profile = [primary, fallback1, ...],
        # llm_profile_advanced likewise. fallback_chain() returns the
        # matching order (advanced chain first, then the
        # normal chain as the last safety net).
        profiles = (
            self.agent_config.fallback_chain(
                run.use_advanced_model, exclude=active_profile_override)
            if self.agent_config else []
        )
        if run.base_profile is not None and run.llm_override is not None and run.llm_profile_info_override:
            # The override the swap replaced failed too: not a fallback.
            failed_override = run.llm_profile_info_override.split(":", 1)[0]
            profiles = [p for p in profiles if p != failed_override]
        if run.llm_override is not None and self.agent_config:
            # On an override the agent's own primary is not the run's
            # base: fallback_chain() leaves it out as the active model,
            # yet it is the first fallback -- with a one-entry chain the
            # only one.
            own_primary = (self.agent_config.advanced_llm_profile
                           if run.use_advanced_model and self.agent_config.advanced_llm_profile
                           else self.agent_config.default_llm_profile)
            override_profile = (run.llm_profile_info_override.split(":", 1)[0]
                                if run.llm_profile_info_override
                                else getattr(run.llm_override, "profile_name", None))
            if own_primary and own_primary not in profiles and own_primary not in (
                    active_profile_override, override_profile):
                profiles.insert(0, own_primary)
        if run.use_advanced_model and profiles:
            logger.debug(
                f"[{self.name}] use_advanced_model=True — fallback "
                f"chain: {profiles}"
            )
        if not model_health.available(llm, run.request_id):
            blocked = getattr(llm, "model", "?")
            for index, profile in enumerate(profiles):
                client = self._fallback_client(profile)
                # takes_format first: asking model_health makes this
                # request the prober of an LLM it would then not call.
                if (client is not None and run.takes_format(client)
                        and model_health.available(client, run.request_id)):
                    logger.info(
                        f"[{self.name}] LLM {blocked} is blocked for "
                        f"{model_health.remaining(llm):.0f}s more; this step runs on {profile}")
                    # The blocked profiles walked past stay in the
                    # chain: if this one fails, a blocked LLM beats none.
                    return client, profiles[:index] + profiles[index + 1:], escalate, profile
            logger.warning(
                f"[{self.name}] LLM {blocked} is blocked and no fallback is free: calling it anyway")
        return llm, profiles, escalate, None

    def _apply_step_pick(self: Agent, run: LoopState, st: StepState) -> None:
        """Pick the step's LLM (_pick_step_llm) and start its fallback chain afresh."""
        st.current_llm, st.fallback_profiles, st.escalated, st.step_profile = (
            self._pick_step_llm(run, st.escalated))
        st.fallback_taken = set()

    async def _announce_step_llm(self: Agent, run: LoopState, st: StepState) -> None:
        # Signal LLM call start — for the picked client, so an escalation
        # whose client did not build is not announced as one.
        if st.escalated:
            llm_display = " (advanced — escalated: stuck)"
        elif st.step_profile:
            llm_display = f" ({st.step_profile}:fallback)"
        elif run.llm_profile_info_override and run.base_profile is None:
            llm_display = f" ({run.llm_profile_info_override})"
        else:
            llm_display = f" ({run.display_profile_info})" if run.display_profile_info else " (unknown LLM)"
        await run.status_worker.progress(f"Calling LLM{llm_display}", meta={"step": st.step + 1})

    async def _choose_step_llm(self: Agent, run: LoopState, st: StepState) -> None:
        """Phase: the step's LLM, picked before the pre-LLM hooks, and announced."""
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
        st.escalated = run.escalator.active and not st.final_call

        st.health_seen = model_health.version
        previous_step_llm = self._step_llms.get(run.session_id)
        self._apply_step_pick(run, st)
        if previous_step_llm is not None and st.current_llm is not previous_step_llm:
            # A switch between steps: into or out of an escalation, around a
            # blocked LLM or back to it. The history carries the previous
            # model's reasoning.
            strip_all_reasoning_artifacts(run.messages)
        # A switch since the previous request (or restart) left no step
        # model behind; the artifacts name the model that produced them.
        strip_foreign_reasoning_artifacts(run.messages, getattr(st.current_llm, "model", None))
        # Before the hooks too: tool_preload runs tools inside them, and a
        # tool that sizes the context asks llm_for_session.
        self._step_llms[run.session_id] = st.current_llm
        await self._announce_step_llm(run, st)

    async def _settle_step_llm(self: Agent, run: LoopState, st: StepState) -> None:
        """Phase: after the pre-LLM hooks, pick again if a block was set or lifted meanwhile."""
        # The blocks are shared by every agent, and the hooks can take
        # minutes (context_summarizer): another request may have blocked
        # this step's LLM, or an answer lifted the block this step walked
        # around, meanwhile. Pick again rather than call an LLM known to be
        # limited, or leave a free one unused. The hooks are not re-run —
        # they are not idempotent (tool_preload runs tools); the next step's
        # hooks see the new model.
        if model_health.version != st.health_seen:
            previous_llm = st.current_llm
            self._apply_step_pick(run, st)
            self._step_llms[run.session_id] = st.current_llm
            if st.current_llm is not previous_llm:
                # The first pick may have made this request the prober of
                # the LLM it now leaves: hand the probe back.
                model_health.drop_probe(previous_llm, run.request_id)
                # A model switch either way: the history the hooks worked
                # on carries the previous model's reasoning items.
                strip_all_reasoning_artifacts(run.messages)
                await self._announce_step_llm(run, st)

        # Spent only now that it is settled which model answers: a re-pick
        # onto a fallback would otherwise have spent a round on no advanced call.
        if st.escalated:
            run.escalator.consume()

    def _take_fallback(self: Agent, run: LoopState, st: StepState) -> Optional[Tuple[str, Any]]:
        """(label, client) to retry a failed call on, each taken once
        per step: the first unblocked one of this step's chain, with the
        run's base LLM as a member — the first after a failed
        escalation, the last after a failed walk around a blocked base;
        else the first one left all the same — a blocked LLM beats
        none. None when all are used up."""
        candidates: List[Tuple[str, Any]] = [(profile, None) for profile in st.fallback_profiles]
        if st.escalated:
            # A failed escalation says nothing about the base: back to it
            # before the chain moves the step to another model.
            candidates.insert(0, (run.base_label(), run.active_llm))
        else:
            candidates.append((run.base_label(), run.active_llm))
        first_blocked = None
        for index, (label, client) in enumerate(candidates):
            if index in st.fallback_taken:
                continue
            if client is None:
                client = self._fallback_client(label)
            if client is None or client is st.current_llm or not run.takes_format(client):
                st.fallback_taken.add(index)
                continue
            if model_health.available(client, run.request_id):
                st.fallback_taken.add(index)
                return label, client
            if first_blocked is None:
                first_blocked = (index, label, client)
        if first_blocked is None:
            return None
        index, label, client = first_blocked
        st.fallback_taken.add(index)
        return label, client

    def _switch_to_fallback(self: Agent, run: LoopState, st: StepState, profile: str, client: Any,
                            *, swap_base: bool) -> None:
        """Move the step's call onto a fallback _take_fallback handed out.

        The history loses the reasoning artifacts of the model it leaves (_strip_for_switch). With
        *swap_base*, a failure of the run's BASE also makes the fallback the base for the rest of
        the request (a request-scoped swap: the hooks of later steps size the context for it, and
        it is labelled as the fallback); a failed escalation model says nothing about the base, so
        then only this step moves.
        """
        self._strip_for_switch(profile, run.messages)
        if swap_base and st.current_llm is run.active_llm:
            run.display_profile_info = f"{profile}:fallback"
            run.active_llm = client
            run.base_profile = profile
        st.current_llm = client
