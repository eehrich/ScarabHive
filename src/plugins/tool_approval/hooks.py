"""The pre_tool_call hook: decide each tool call before it runs.

What a call is held to is the run's POLICY: the agent's own settings, and --
for a sub-run -- the policy of every run under approval above it (policy.py:
never looser than a parent). Order of the checks:

1. A deny rule of the run or of a run above it names the call -- blocked. An
   agent in ``off`` keeps the instance's deny rules (and inherited ones).
2. Effective mode ``off`` -- nothing else is checked.
3. The call starts work that passes no approval (spawns.py: a sub-agent whose
   agent has this hook off, Claude Code, a state machine) -- blocked in
   ``auto``; asked, with a warning, in ``ask``; blocked where nobody can be
   asked. Allow rules do not let it through: they cannot reach its calls.
4. A call of a script (``source`` not "model", tool_script's): the script was
   the model's call and passed this hook, shown with its code. Nothing inside
   it is asked -- what 2 and 3 do not block runs.
5. ``auto`` -- everything else runs.
6. ``ask`` -- an allow rule of every asking level names the call, or a person
   allowed the tool for this session: it runs. Otherwise the person watching
   the run is asked.
7. Nobody watches the run (``status_forwarding.attended_stream_of``): the
   ``unattended`` answer applies, ``block`` by default.

The question is a status line of the run with ``meta.tool_approval``; the chat
draws buttons for it and posts the answer to this plugin's route (web.py). The
hook waits for that answer, the run's cancellation, ``ask_timeout``, or until
nobody has read the run for ``gone_after_seconds`` -- whichever comes first.
Unanswered and cancelled calls do not run.

A rule that cannot be read blocks the call rather than letting it through: a
typo in a deny rule must not open what the rule was written to close.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from agent_system.core.run_questions import CANCELLED, GONE, put_to_person
from agent_system.core.run_questions import status_line as _line
from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.servers.agent.components.status_forwarding import attended_stream_of
from agent_system.tools.status import StatusScope, get_status_bus

from .broker import ALLOW_ONCE, ALLOW_SESSION, DECISIONS, DENY, Answer, ApprovalBroker, ApprovalQuestion
from .policy import Policy, PolicyStore
from .preview import arguments_preview
from .rules import Rule, RuleError, first_match, parse_rules
from .spawns import Spawn, is_script, spawn_of

logger = logging.getLogger(__name__)

MODES = ("ask", "auto", "off")
UNATTENDED = ("block", "allow")

#: Seconds the wait keeps short of the hook's own timeout: the hook ends the
#: question with its own words before the registry cuts it off with generic ones.
TIMEOUT_HEADROOM = 5.0


@dataclass(frozen=True)
class Settings:
    """What applies to one call: the instance's config, the agent's over it."""

    mode: str
    allow: Tuple[Rule, ...]
    deny: Tuple[Rule, ...]
    ask_timeout: float
    unattended: str
    reask_seconds: float
    gone_after_seconds: float
    #: The instance's deny rules hold for every agent that switches the hook on,
    #: its own mode off included -- unless the instance itself is off.
    instance_deny: Tuple[Rule, ...] = ()


def _choice(raw: Any, allowed: Tuple[str, ...], key: str) -> str:
    if raw is False and "off" in allowed:
        # YAML 1.1 reads a bare `off` as false: `mode: off` means off.
        raw = "off"
    value = str(raw).strip().lower() if raw is not None and not isinstance(raw, bool) else ""
    if value not in allowed:
        raise RuleError(f"{key} must be one of {', '.join(allowed)}, got {raw!r}")
    return value


def _seconds(raw: Any, key: str) -> float:
    if isinstance(raw, bool):
        raise RuleError(f"{key} must be a number of seconds, got {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"{key} must be a number of seconds, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuleError(f"{key} must be above 0 seconds, got {raw!r}")
    return value


def _pass(context: HookContext) -> HookResult:
    return HookResult(success=True, modified=False, context=context)


def _block(text: str) -> HookResult:
    return HookResult(success=True, modified=False, metadata={"block": text})


class ToolApprovalPlugin(SchemaBasedPluginHook):
    """The hook, the open questions (``broker``) and the route that answers them."""

    def __init__(self, plugin_dir: Path | str, name: str = "tool_approval",
                 server_config: Any = None, broker: Optional[ApprovalBroker] = None):
        super().__init__(plugin_dir)
        self.instance_name = name
        config = dict(self.get_config())
        instance_config = getattr(server_config, "config", None) if server_config is not None else None
        if isinstance(instance_config, Mapping):
            config.update(instance_config)
        self._base = config
        self.broker = broker or ApprovalBroker()
        self.policies = PolicyStore()
        try:
            self.settings_for({})
        except RuleError as exc:
            # Said at start, where the operator looks; every call it concerns is blocked.
            logger.error("tool_approval '%s': the configuration cannot be read (%s); "
                         "every call it would decide is blocked", name, exc)

    @property
    def answer_url(self) -> str:
        return f"/plugins/{self.instance_name}/answer"

    @property
    def hook_name(self) -> str:
        return f"{self.instance_name}.check_tool_call"

    def settings_for(self, agent_config: Mapping[str, Any]) -> Settings:
        """The settings for an agent whose override carries ``agent_config``.

        Every key of the agent's replaces the instance's, except allow and deny:
        the agent's rules are added to the instance's -- an
        agent can widen what runs without asking only by its own allow rules,
        and cannot drop a deny rule the instance sets.
        """
        agent_config = agent_config if isinstance(agent_config, Mapping) else {}
        merged = {**self._base, **{k: v for k, v in agent_config.items() if k not in ("allow", "deny")}}
        instance_deny = tuple(parse_rules(self._base.get("deny"), "deny"))
        instance_off = _choice(self._base.get("mode", "ask"), MODES, "mode") == "off"
        return Settings(
            mode=_choice(merged.get("mode", "ask"), MODES, "mode"),
            allow=tuple(parse_rules(self._base.get("allow"), "allow")
                        + parse_rules(agent_config.get("allow"), "allow (agent)")),
            deny=instance_deny + tuple(parse_rules(agent_config.get("deny"), "deny (agent)")),
            instance_deny=() if instance_off else instance_deny,
            ask_timeout=_seconds(merged.get("ask_timeout", 300), "ask_timeout"),
            unattended=_choice(merged.get("unattended", "block"), UNATTENDED, "unattended"),
            reask_seconds=_seconds(merged.get("reask_seconds", 20), "reask_seconds"),
            gone_after_seconds=_seconds(merged.get("gone_after_seconds", 15), "gone_after_seconds"),
        )

    def _wait_limit(self, ask_timeout: float) -> float:
        """``ask_timeout``, kept short of the hook's own timeout (schema, or the
        operator's hooks.overrides): past it the registry cuts the hook off, and
        the call is blocked with the framework's generic text."""
        info = get_hook_registry().get_hook_info(self.hook_name)
        hook_timeout = (info or {}).get("timeout")
        if not isinstance(hook_timeout, (int, float)) or hook_timeout <= 0:
            return ask_timeout
        limit = hook_timeout - min(TIMEOUT_HEADROOM, hook_timeout / 10)
        if ask_timeout > limit:
            logger.warning("tool_approval: ask_timeout %gs is not below the hook's timeout %gs; "
                           "questions wait %gs", ask_timeout, hook_timeout, limit)
            return limit
        return ask_timeout

    def get_web_router(self):
        from .web import build_router
        return build_router(self)

    # --- the hook ------------------------------------------------------------

    async def check_tool_call(self, context: HookContext) -> HookResult:
        call = context.tool_call or {}
        name = str(call.get("name") or "")
        server = str(call.get("server") or "")
        by_model = (call.get("source") or "model") == "model"
        raw_arguments = call.get("arguments")
        arguments: Mapping[str, Any] = raw_arguments if isinstance(raw_arguments, Mapping) else {}
        try:
            settings = self.settings_for(context.hook_config or {})
        except RuleError as exc:
            logger.error("tool_approval: agent %s: the configuration cannot be read (%s)",
                         context.agent_name, exc)
            return _block(f"The call to '{name}' did not run: the approval rules of this agent "
                          f"cannot be read ({exc}). Tell the user; do not retry it.")
        # An agent that says off keeps the instance's deny rules (and anything it
        # inherits); only its own rules and its questions are off.
        own_deny = settings.instance_deny if settings.mode == "off" else settings.deny
        policy = Policy.own(settings.mode, own_deny, settings.allow, settings.unattended,
                            session=(context.user_id, context.session_id or "")).under(
            self.policies.inherited(context.request_id))
        # Before anything else: a spawn this call makes starts its sub-run only
        # after the hook, and the sub-run looks its inheritance up here. (A
        # script's call records under its own id what its run has: the same.)
        self.policies.record(context.request_id, policy)

        rule = first_match(policy.deny, name, server, arguments)
        if rule is not None:
            # The model reads the tool pattern, not the argument patterns: a regex
            # in its result is a recipe for the call that slips past it.
            logger.info("tool_approval: agent %s: %s/%s denied by rule %s",
                        context.agent_name, server, name, rule.describe())
            return _block(f"The call to '{name}' is not allowed for this agent (rule on {rule.tool}); "
                          "it did not run. Do not send it again, and do not try to get the same "
                          "effect with another call. Tell the user what you would need.")
        if policy.mode == "off":
            return _pass(context)
        spawn = await spawn_of(context, server, name, arguments, self.hook_name)
        if spawn is not None and not spawn.guarded:
            return await self._unguarded_spawn(context, settings, policy, spawn, by_model,
                                               name, server, arguments)
        if not by_model:
            return _pass(context)   # the script it runs in was the call that passed this hook
        if policy.mode == "auto":
            return _pass(context)
        if policy.allows(name, server, arguments):
            return _pass(context)
        tool_key = f"{server}/{name}"
        if self.broker.granted(policy.grant_sessions, tool_key):
            return _pass(context)
        if attended_stream_of(context.request_id, settings.gone_after_seconds) is None:
            return self._unattended(context, policy.unattended, name)
        # A script is shown with its code and allowed call by call: allowed for the
        # session, every later script -- and every call inside one -- would run unread.
        decisions = tuple(d for d in DECISIONS if d != ALLOW_SESSION) if is_script(context, server) else DECISIONS
        return await self._ask(context, settings, policy, name, server, arguments, tool_key,
                               decisions=decisions)

    async def _unguarded_spawn(self, context: HookContext, settings: Settings, policy: Policy,
                               spawn: Spawn, by_model: bool, name: str, server: str,
                               arguments: Mapping[str, Any]) -> HookResult:
        """A call that starts work no approval reaches: its calls would not be held
        to this run's rules, so no allow rule lets it through."""
        where = "" if by_model else " inside the script"
        if policy.mode == "auto":
            return _block(f"The call to '{name}'{where} would start {spawn.target}, which runs without "
                          "tool approvals: the rules this run is held to would not apply to its calls. "
                          "It did not run. Use an agent that has approvals switched on, or tell the user "
                          f"that {spawn.target} needs them.")
        if not by_model:
            return _block(f"The call to '{name}' inside the script would start {spawn.target} without "
                          "tool approvals, and only a person may allow that -- a script cannot ask. "
                          "It did not run. Make this call on its own, outside the script, so the user "
                          "can be asked.")
        tool_key = f"{server}/{name}!unguarded:{spawn.target}"
        if self.broker.granted(policy.grant_sessions, tool_key):
            return _pass(context)
        if attended_stream_of(context.request_id, settings.gone_after_seconds) is None:
            return self._unattended(context, "block", name, spawn)
        warning = (f"{spawn.target} runs WITHOUT tool approvals: the rules of this run do not apply "
                   "to its calls.")
        return await self._ask(context, settings, policy, name, server, arguments, tool_key,
                               warning=warning, unattended="block")

    @staticmethod
    def _unattended(context: HookContext, unattended: str, name: str,
                    spawn: Optional[Spawn] = None) -> HookResult:
        if spawn is not None:
            return _block(f"The call to '{name}' would start {spawn.target} without tool approvals; "
                          "only a person may allow that, and nobody is watching this run. It did not "
                          "run. Go on without it, or finish and tell the user which call needs their "
                          "approval.")
        if unattended == "allow":
            return _pass(context)
        return _block(f"The call to '{name}' needs a person's approval, and nobody can give it in "
                      "this run: nobody is watching it. It did not run. Go on without it, or "
                      "finish and tell the user which call needs their approval.")

    async def _ask(self, context: HookContext, settings: Settings, policy: Policy, name: str,
                   server: str, arguments: Mapping[str, Any], tool_key: str,
                   warning: Optional[str] = None, unattended: Optional[str] = None,
                   decisions: Tuple[str, ...] = DECISIONS) -> HookResult:
        """Put the call to the person watching the run and wait for the answer."""
        unattended = unattended or policy.unattended
        shown, cut = arguments_preview(arguments)
        wait = self._wait_limit(settings.ask_timeout)
        question = self.broker.open(
            owner=context.user_id, session_id=context.session_id, request_id=context.request_id,
            agent_name=context.agent_name, tool=name, server=server, arguments_preview=shown,
            timeout=wait, arguments_cut=cut, warning=warning, decisions=decisions)
        # A row of its own under the run: a line of the run's own id would
        # overwrite the run's row, and the buttons would go with the next line.
        status_id = f"{context.request_id}_approval_{question.id}"
        asking = _line(f"Approve {name}? {'WITHOUT approvals: ' if warning else ''}{shown}")
        scope = StatusScope(get_status_bus(), self.instance_name, request_id=status_id, start_msg=asking)
        try:
            async with scope:
                # Cut off from outside (the registry's timeout, the run's teardown), the
                # call does not run (on_error: block) and the row ends with this line.
                outcome = await put_to_person(
                    self.broker, question, scope, meta_key="tool_approval", answer_url=self.answer_url,
                    line=asking, token=context.cancellation_token, reask_seconds=settings.reask_seconds,
                    gone_after_seconds=settings.gone_after_seconds,
                    cut_off=f"{name}: not run, the check was cut off while asking")
                return await self._settle(scope, outcome, context, unattended, wait, name, tool_key,
                                          policy, question)
        finally:
            self.broker.close(question)

    async def _settle(self, scope: StatusScope, outcome: Any, context: HookContext, unattended: str,
                      wait: float, name: str, tool_key: str, policy: Policy,
                      question: ApprovalQuestion) -> HookResult:
        """The status row's last line and what the hook returns, per outcome."""
        if isinstance(outcome, Answer):
            who = outcome.answered_by or "the user"
            if outcome.decision == ALLOW_ONCE:
                await scope.end(_line(f"{name}: allowed once by {who}"))
                return _pass(context)
            if outcome.decision == ALLOW_SESSION and ALLOW_SESSION in question.decisions:
                # kept in every session of the chain that asks: it holds only where
                # all of them would have asked, and no other chain reaches it. Only
                # in the run owner's own sessions -- a level of another user's (an
                # ended run's id taken as a prefix) is never written, so such a
                # chain holds no grant at all and asks again.
                self.broker.grant([s for s in policy.grant_sessions if s[0] == context.user_id], tool_key)
                await scope.end(_line(f"{name}: allowed for this session by {who}"))
                return _pass(context)
            if outcome.decision == ALLOW_SESSION:
                await scope.end(_line(f"{name}: allowed once by {who} (a script is never allowed for the session)"))
                return _pass(context)
            if outcome.decision == DENY:
                await scope.error(_line(f"{name}: denied by {who}"
                                        + (f" -- {outcome.reason}" if outcome.reason else "")))
                because = f" Their reason: {outcome.reason}" if outcome.reason else ""
                return _block(f"The user denied the call to '{name}'; it did not run.{because} "
                              "Do not send it again, and do not try to get the same effect with another "
                              "call. Take their answer into account, or ask the user how to proceed.")
        if outcome == CANCELLED:
            await scope.error(_line(f"{name}: not run, the run was cancelled while asking"))
            return _block(f"The call to '{name}' did not run: the run was cancelled while its "
                          "approval was asked.")
        if outcome == GONE:
            if unattended == "allow":
                await scope.end(_line(f"{name}: nobody reads the run any more, allowed (unattended: allow)"))
            else:
                await scope.error(_line(f"{name}: nobody reads the run any more, not run"))
            return self._unattended(context, unattended, name)
        await scope.error(_line(f"{name}: no answer within {wait:g} s, not run"))
        return _block(f"The call to '{name}' needs the user's approval, and nobody answered within "
                      f"{wait:g} seconds; it did not run. Do not send it again right away: go on "
                      "without it, or finish and tell the user which call is waiting for their "
                      "approval.")
