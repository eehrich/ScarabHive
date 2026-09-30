"""tool_preload — execute the predictable opening tool calls without an LLM turn.

The pattern this removes: an agent gets "ändere X im Dokument Y", and its first
LLM turn is — always — the tool call that loads Y. That turn costs a full LLM
round trip (and they have grown slow) plus the prompt tokens of the whole
context, and its outcome is known in advance.

So a rule matches the user message with a regex, the configured tool calls run
through ``Agent.dispatch_tool_call`` (same allowlist, same runtime params as the
LLM path — what the LLM may not call, this may not either), and the results are
appended to the conversation in the exact assistant(tool_calls) + tool shape the
LLM's own calls produce. The first real LLM turn then starts where the second
used to.

Placement of the injected pair — AFTER the user message — keeps the existing
prefix byte-stable, so provider prompt caching is unaffected: the pair only
extends it.

Fires once per user turn by construction: it only acts when the LAST message is
a user message, and after acting the last message is a tool result.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.message_roles import is_injected_note
from agent_system.llm.models import ChatMessage
from agent_system.llm.text_sanitizer import sanitize_json_content

logger = logging.getLogger(__name__)

#: Hard cap across all rules of one user turn. A greedy regex must not be able
#: to turn one message into an unbounded tool storm.
DEFAULT_MAX_CALLS_PER_TURN = 5

#: Seconds the calls of one turn may take together (setting ``max_seconds``).
#: Below the hook's 60 s timeout on purpose: when the registry cuts the hook
#: off, every pair is lost, done calls included, and the model repeats them.
DEFAULT_MAX_SECONDS = 45.0

#: A call the budget left unrun: its id prefix (dedup does not count it as
#: made) and the answer the model reads for it.
SKIPPED_ID_PREFIX = "preload_skipped_"
SKIPPED_TEXT = "Not run: the preload time budget was used up. Call the tool yourself if you need it."

#: Characters of the user message the rules see. A preload trigger is a
#: directive ("bearbeite Dokument X"), not something buried inside a pasted
#: 200 KB file — so the cap costs nothing real and bounds the input to a regex
#: whose cost can be exponential in its length. Measured with a nested
#: quantifier: 22 tokens ≈ 1 s, 24 tokens ≈ 4.4 s, and `re.search` holds the
#: GIL, so it stalls the whole event loop. The hook's declared timeout cannot
#: preempt it — asyncio.wait_for only cancels at an await point, and there is
#: none inside a regex match.
MATCH_TEXT_LIMIT = 2000

#: A single rule taking longer than this is reported. The match cannot be
#: interrupted, but the operator must be able to SEE which rule is pathological
#: instead of hunting a mysteriously slow agent.
_SLOW_MATCH_WARN_S = 0.1


def _user_text(message: Any) -> str:
    """The text of a user message, whichever content shape it arrived in --
    without what a hook wrote in front of it (``prefixed_by``, e.g.
    simple_prompt_inject ``task_start``): the rules look for the user's
    directive, and a long note there pushed it past MATCH_TEXT_LIMIT."""
    content = getattr(message, "content", None)
    prefixed = getattr(message, "prefixed_by", None)
    if isinstance(message, dict):
        content = message.get("content") if content is None else content
        prefixed = message.get("prefixed_by")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Multimodal: join the text parts. Both shapes occur — plain dicts from
        # session files, pydantic TextContent objects once ChatMessage has
        # validated them (its validator upgrades dicts to models).
        parts = []
        for part in content:
            part_text = (part.get("text") if isinstance(part, dict)
                         else getattr(part, "text", None))
            if isinstance(part_text, str):
                parts.append(part_text)
        text = " ".join(parts)
    for prefix in (prefixed or {}).values():
        text = text.replace(prefix, "", 1)
    return text


def _role(message: Any) -> str:
    if isinstance(message, dict):
        return message.get("role", "")
    return getattr(message, "role", "")


def _json_native(value: Any) -> Any:
    """A value json.dumps can serialise, whatever YAML produced.

    Params come straight from the agent's YAML, and PyYAML resolves unquoted
    dates and timestamps to ``datetime`` objects. Left alone they reach the
    serializer that builds the tool_call arguments AFTER the tool has already
    run — measured: the tool executed, the exception discarded every message
    pair, the hook reported "no preload", and the LLM then called the same
    tool a second time. For a state-changing tool that is a double execution.

    Normalising once, here, means no downstream serializer can fail on them.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


#: Both placeholder syntaxes in ONE pattern, so a single ``re.sub`` handles
#: them. The context alternative comes first: ``{{name}}`` must not be read as
#: a group ``{name}`` wrapped in literal braces.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(?P<ctx>\w+)\s*\}\}|\{(?P<grp>\w+)\}")


def _as_int_if_lossless(text: str) -> Any:
    """``"42"`` → 42, but ``"007"`` stays a string.

    Tool handlers validate ids as integers, so the conversion earns its keep —
    but only where it changes nothing else. Two measured traps:

    * ``"007"`` would become 7, and ``json_store`` stringifies that back to
      ``"7"``. The prompt's own ``{{ json_namespace }}`` still renders
      ``"007"``, so preload and the agent's own call would read two DIFFERENT
      namespaces and nothing would look wrong. Round-tripping through ``str``
      is the exact test for that.
    * ``str.isdigit()`` is wider than ``int()`` accepts: ``"²"`` and ``"⑦"``
      pass it and then raise ValueError — which is not a KeyError, so it
      escaped the per-rule handler and killed the whole turn's preload. Same
      for a 5000-digit run (int_max_str_digits).
    """
    try:
        number = int(text)
    except (ValueError, TypeError):
        return text
    return number if str(number) == text else text


def _fill_params(
    params: Dict[str, Any],
    groups: Dict[str, str],
    ctx_vars: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Substitute placeholders from two sources.

    * ``{group}`` — a **named group of the rule's regex**, i.e. something the
      user typed. This is the original source.
    * ``{{ var }}`` — a **context variable** of the session, the same value the
      prompt template renders. Needed for state that never appears in the user
      text: v6 keeps its store namespace in ``json_namespace``, set once at
      bootstrap and inherited by every sub-agent, whose task text is only
      "Aufgabe: World". Without this the rule could not name the store.

    Both syntaxes are resolved in ONE pass over a combined pattern, and that
    is load-bearing in two ways:

    * ``{{name}}`` must not be read as a group ``{name}`` in literal braces,
      so the context alternative comes first in the pattern.
    * A resolved value must never be scanned again. Two sequential passes did
      exactly that, and the v6 coordinator puts the entire user task into
      ``brief`` — a brief containing ``{sid}`` was then read as a group
      reference and killed the rule with a warning naming a group that appears
      in no YAML. Worse, where a group happened to share the name, the context
      value was silently rewritten with user text.

    Strings, dicts and lists are all templated, recursively. Templating only
    the top level meant a nested ``{filter: {namespace: "{{ ns }}"}}`` reached
    the tool verbatim; ``json_store`` then falls back to the DEFAULT namespace
    and returns a foreign document — silent, and the exact failure this
    feature exists to prevent. Everything else passes through, made JSON-native
    first (see :func:`_json_native`).

    A value that IS exactly one placeholder may become an int — see
    :func:`_as_int_if_lossless` for why "may". A mixed string ("kapitel_{n}")
    stays a string.

    Raises KeyError for a placeholder with no matching group or variable — the
    rule is misconfigured and must be skipped loudly, not called with a literal
    ``{doc}`` as the document name. A context var that is missing is the same
    class of error: preloading a read against the DEFAULT namespace instead of
    the run's own would silently hand the agent someone else's document.
    """
    ctx = ctx_vars or {}

    def _resolve(m: "re.Match[str]") -> str:
        """One placeholder → its value. KeyError for both sources alike."""
        if m.group("ctx") is not None:                   # {{ var }}
            name = m.group("ctx")
            if name not in ctx or ctx[name] is None:
                raise KeyError(name)
            return str(ctx[name])
        return groups[m.group("grp")]                    # {group}, KeyError ok

    def _fill(value: Any) -> Any:
        # Containers are templated too. Leaving them out meant a nested
        # ``{filter: {namespace: "{{ json_namespace }}"}}`` reached the tool
        # verbatim: json_store then falls back to the DEFAULT namespace and
        # hands over a foreign document — silently, which is precisely the
        # failure this whole feature exists to prevent.
        if isinstance(value, dict):
            return {k: _fill(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_fill(v) for v in value]
        if not isinstance(value, str):
            return _json_native(value)

        # ONE pass over both syntaxes. Two passes would re-scan the value a
        # context var resolved to — and the v6 coordinator puts the whole user
        # task into ``brief``, so a brief containing braces would be read as a
        # group reference and kill the rule with a warning naming a group that
        # appears in no YAML.
        lone = _PLACEHOLDER_RE.fullmatch(value)
        if lone:
            resolved = _resolve(lone)
            return _as_int_if_lossless(resolved)
        return _PLACEHOLDER_RE.sub(_resolve, value)

    return {key: _fill(value) for key, value in params.items()}


def _session_context_vars(context: Any) -> Dict[str, Any]:
    """The variables a ``{{ var }}`` placeholder may reference.

    Same precedence the prompt rendering uses: the agent's static
    ``template_vars`` as the base, the session-scoped ones (written by
    ``set_context``) on top. Reading them here means a preload names the same
    namespace the agent's own call would have named.

    Defensive throughout: this runs inside a hook, and a missing tracker must
    degrade to "no context vars" (the rule is then skipped for a missing
    placeholder), never sink the request.
    """
    out: Dict[str, Any] = {}
    agent = getattr(context, "agent", None)
    if agent is None:
        return out
    try:
        cfg = getattr(agent, "agent_config", None)
        if cfg is not None and getattr(cfg, "template_vars", None):
            out.update(dict(cfg.template_vars))
    except Exception:                                    # noqa: BLE001
        logger.debug("tool_preload: agent_config.template_vars unreadable",
                     exc_info=True)
    try:
        tracker = getattr(agent, "_session_tracker", None)
        session_id = getattr(context, "session_id", None)
        if tracker is not None and session_id:
            session_vars = tracker.get_session_template_vars(session_id)
            if session_vars:
                out.update(session_vars)
    except Exception:                                    # noqa: BLE001
        logger.debug("tool_preload: session template vars unreadable",
                     exc_info=True)
    return out


def _max_seconds(config: Dict[str, Any]) -> float:
    """The ``max_seconds`` setting; anything but a positive number is the default."""
    value = config.get("max_seconds", DEFAULT_MAX_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return DEFAULT_MAX_SECONDS
    return float(value)


def _rule_calls(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A rule's calls, in order. ``calls: [...]`` or the one-call shorthand."""
    calls = rule.get("calls")
    if isinstance(calls, list) and calls:
        return [c for c in calls if isinstance(c, dict)]
    if rule.get("tool"):
        return [{"tool": rule["tool"], "params": rule.get("params") or {}}]
    return []


class ToolPreloadPlugin(SchemaBasedPluginHook):
    """Hook implementation: rules → dispatch → append the call/result pairs."""

    def __init__(self, plugin_dir: Path, server_config: Any = None):
        super().__init__(plugin_dir)
        self.server_config = server_config
        logger.info("ToolPreloadPlugin initialized")

    async def preload(self, context: HookContext) -> HookResult:
        """pre_llm_call: act exactly once per user turn, per the rules."""
        unchanged = HookResult(success=True, modified=False, context=context)
        started = time.monotonic()
        try:
            messages = context.messages
            # The turn a person opened, behind any note the loop added after it
            # (the step budget note follows drained user input). Those notes are
            # `developer` now, so the walk asks for the marker, not the role --
            # on the role alone it stopped at the first note and no preload ran.
            last = len(messages) - 1
            while last >= 0 and is_injected_note(messages[last]):
                last -= 1
            if last < 0 or _role(messages[last]) != "user":
                return unchanged

            config = context.hook_config or {}
            rules = config.get("rules") or []
            if not rules:
                return unchanged

            agent = context.agent
            if agent is None or not hasattr(agent, "dispatch_tool_call"):
                return unchanged

            # Bounded before it ever reaches a regex — see MATCH_TEXT_LIMIT.
            text = _user_text(messages[last])[:MATCH_TEXT_LIMIT]
            if not text:
                return unchanged

            # Im Hook aufgeloest, wo `context` im Scope ist — _plan_calls
            # bekommt Daten, kein Kontext-Objekt.
            plan = self._plan_calls(
                rules, text, config, messages,
                ctx_vars=_session_context_vars(context))
            if not plan:
                return unchanged

            appended = await self._execute_plan(
                plan, context, deadline=started + _max_seconds(config))
            if not appended:
                return unchanged

            # A NEW list, not in-place: the agent core takes hook output only
            # when the list identity changed (see _select_llm_messages).
            context.messages = list(messages) + appended
            return HookResult(success=True, modified=True, context=context)

        except Exception as e:  # noqa: BLE001 - a preload must never sink the run
            logger.error(f"tool_preload failed, continuing without preload: {e}",
                         exc_info=True)
            return unchanged

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def _plan_calls(self, rules: List[Any], text: str, config: Dict[str, Any],
                    messages: List[Any],
                    ctx_vars: Dict[str, Any] | None = None) -> List[Tuple[int, str, Dict[str, Any]]]:
        """Which (rule number, tool, params) to run, in order, after matching
        and dedup. The rule number keeps the chains apart: a failure ends only
        the chain it happened in."""
        max_calls = int(config.get("max_calls_per_turn",
                                   DEFAULT_MAX_CALLS_PER_TURN))
        dedup = config.get("dedup", True)
        already = self._calls_in_history(messages) if dedup else set()
        ctx_vars = ctx_vars or {}

        plan: List[Tuple[int, str, Dict[str, Any]]] = []
        for rule_no, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("match")
            if not pattern:
                continue
            try:
                started = time.monotonic()
                match = re.search(pattern, text)
                elapsed = time.monotonic() - started
            except re.error as e:
                logger.warning(f"tool_preload: invalid regex {pattern!r}: {e}")
                continue
            if elapsed > _SLOW_MATCH_WARN_S:
                # Cannot be interrupted, but it must not be invisible: a rule
                # this slow is pathological (nested quantifiers), and the
                # operator needs to know WHICH one to look at.
                logger.warning(
                    f"tool_preload: rule {pattern!r} took {elapsed:.2f}s to "
                    f"match {len(text)} chars — this blocks the event loop; "
                    f"check it for nested quantifiers")
            if not match:
                continue
            groups = {k: v for k, v in match.groupdict().items() if v is not None}

            # Resolve the whole chain first — dedup and the cap apply to the
            # RULE, never to a single link of it.
            resolved: List[Tuple[str, Dict[str, Any]]] = []
            broken = False
            for call in _rule_calls(rule):
                tool = call.get("tool")
                if not tool:
                    continue
                try:
                    params = _fill_params(
                        call.get("params") or {}, groups, ctx_vars)
                except KeyError as e:
                    logger.warning(
                        f"tool_preload: rule {pattern!r} references {e} — "
                        f"neither a captured group nor a context var. "
                        f"Rule skipped.")
                    broken = True
                    break  # the rest of THIS rule's chain depends on it too
                resolved.append((tool, params))
            if broken or not resolved:
                continue

            keys = [(t, json.dumps(p, sort_keys=True, default=str))
                    for t, p in resolved]

            # Per RULE, not per call: a chain is "set the context var, THEN
            # load the content". Skipping only the already-seen first link
            # would run the rest against whatever state the var still holds —
            # silently, on the wrong scene. Only a chain whose calls ALL ran
            # already is genuinely redundant.
            if all(k in already for k in keys):
                logger.debug(f"tool_preload: rule {pattern!r} already satisfied, skipped")
                continue

            # Same reason for the cap: a chain is all-or-nothing. Cutting it in
            # the middle leaves the state set and the content unloaded, which
            # is worse than not preloading at all.
            if len(plan) + len(resolved) > max_calls:
                logger.warning(
                    f"tool_preload: call cap {max_calls} would be exceeded by "
                    f"rule {pattern!r} ({len(resolved)} calls) — rule skipped "
                    f"whole; the agent will make these calls itself")
                continue

            already.update(keys)
            plan.extend((rule_no, tool, params) for tool, params in resolved)
        return plan

    @staticmethod
    def _calls_in_history(messages: List[Any]) -> set:
        """(tool, params-json) of every tool call already in the conversation.

        Preloading the same document twice buys nothing and costs its tokens
        twice — the agent either still has it in context or edited it itself,
        in which case IT knows the current state better than a re-read.
        """
        seen: set = set()
        for msg in messages:
            tool_calls = (msg.get("tool_calls") if isinstance(msg, dict)
                          else getattr(msg, "tool_calls", None)) or []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name")
                if not name or str(tc.get("id") or "").startswith(SKIPPED_ID_PREFIX):
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    continue
                seen.add((name, json.dumps(args, sort_keys=True, default=str)))
        return seen

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _execute_plan(self, plan: List[Tuple[int, str, Dict[str, Any]]],
                            context: HookContext,
                            deadline: float = float("inf")) -> List[ChatMessage]:
        """Run the plan sequentially; return the message pairs to append.

        Sequential ON PURPOSE: "erst context_var setzen, dann Content laden" is
        a real dependency, so call k+1 must not start before call k finished.

        A call that the dispatcher rejects (unknown tool, not in the agent's
        allowlist) is an operator error: logged, the rest of its rule's chain
        dropped, pairs so far kept; the next rule still runs. A call whose TOOL returns an error is appended like any result —
        error answers carry recovery context these days, and the agent would
        have seen the same thing calling it itself. A tool that RAISES is
        recorded as an error result, the way the loop records it, and ends
        its rule's chain. Once ``deadline`` has passed no call starts; each one left
        gets a "not run" answer instead.
        """
        from agent_system.servers.agent.components.tool_execution import ToolDispatchError

        # HookContext carries no user_id -- and without one inject_runtime_params
        # leaves _user_id unset, so a preloaded call ran with less identity than
        # the same call from the LLM. The agent's own answer for its tool calls
        # (Agent.tool_user): the registered owner of the request, else the
        # session's stored user. The request alone is not enough: agent-cli
        # registers none, and an agent reached with no user stored "anonymous"
        # as its session's user and refused the run's next call as another user.
        # With the user, inject_runtime_params registers the request for it, as
        # for the model's own calls.
        user_id = None
        try:
            tool_user = getattr(context.agent, "tool_user", None)
            if callable(tool_user):
                user_id = tool_user(context.request_id, context.session_id)
        except Exception as e:  # noqa: BLE001 - identity is best-effort here
            logger.debug(f"tool_preload: could not resolve user_id: {e}")

        appended: List[ChatMessage] = []
        aborted = None  # the rule whose chain a failure ended
        for rule_no, tool, params in plan:
            if rule_no == aborted:
                continue  # the rest of that chain may depend on the failed call
            # Past the budget no call starts: the registry's timeout would
            # discard every pair, done ones included. The rest is recorded as
            # skipped, so the model makes those calls itself.
            skipped = time.monotonic() >= deadline
            call_id = f"{SKIPPED_ID_PREFIX if skipped else 'preload_'}{uuid.uuid4().hex[:10]}"
            failed = False
            if skipped:
                logger.warning(f"tool_preload: time budget used up, {tool} not run")
                result = {"error": SKIPPED_TEXT}
            else:
                try:
                    result = await context.agent.dispatch_tool_call(
                        tool, dict(params),
                        session_id=context.session_id,
                        user_id=user_id,
                        request_id=context.request_id,
                    )
                except ToolDispatchError as e:
                    logger.warning(f"tool_preload: {tool} not dispatchable: {e} — "
                                   f"chain aborted, later calls may depend on it")
                    aborted = rule_no
                    continue
                except Exception as e:  # noqa: BLE001
                    # The tool was called and failed -- possibly after a side
                    # effect. Recorded as the model's own call would record it: with
                    # nothing in the history the model called it again, blind.
                    logger.error(f"tool_preload: {tool} raised: {e} — chain aborted",
                                 exc_info=True)
                    result = {"error": f"Tool '{tool}' execution failed: {e}",
                              "type": type(e).__name__}
                    failed = True

            # Own guard: the tool has ALREADY RUN at this point. Anything that
            # raises while building the pair would otherwise discard the pairs
            # of every call before it too — the tools ran, the conversation
            # says they did not, and the LLM calls them again. Params are
            # JSON-native by construction (_json_native) and the result gets
            # default=str, so this is belt and braces; it is here because the
            # consequence of being wrong is a double execution.
            try:
                now = datetime.now(timezone.utc)
                pair = [
                    ChatMessage(
                        role="assistant",
                        content=None,
                        # Not written by the model: context_engineer's Pre-Layer T
                        # reads the unsent round as everything after the last
                        # assistant message that is NOT injected.
                        injected_by="tool_preload",
                        # the step whose LLM call these results feed, numbered as
                        # the loop stamps its own answers (ChatMessage.step): a
                        # session read back shows the pair in that step
                        step=(context.step or 0) + 1,
                        tool_calls=[{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool,
                                "arguments": json.dumps(
                                    params, ensure_ascii=False, default=str),
                            },
                        }],
                        timestamp=now,
                    ),
                    ChatMessage(
                        role="tool",
                        tool_call_id=call_id,
                        name=tool,
                        content=sanitize_json_content(
                            json.dumps(result, ensure_ascii=False, default=str)),
                        timestamp=now,
                    ),
                ]
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"tool_preload: {tool} ran but its result could not be "
                    f"recorded ({e}) — chain aborted, earlier pairs kept so the "
                    f"model does not repeat those calls", exc_info=True)
                aborted = rule_no
                continue
            appended.extend(pair)
            if failed:
                # Ends this rule's chain only: its later calls may depend on
                # this one, the next rule's do not.
                aborted = rule_no
                continue
            if skipped:
                continue
            logger.info(f"tool_preload: {tool}({params}) executed before the "
                        f"first LLM turn")
        return appended
