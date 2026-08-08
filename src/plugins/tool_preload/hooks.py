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
from typing import Any, Dict, List, Optional, Tuple

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.models import ChatMessage
from agent_system.llm.text_sanitizer import sanitize_json_content

logger = logging.getLogger(__name__)

#: Hard cap across all rules of one user turn. A greedy regex must not be able
#: to turn one message into an unbounded tool storm.
DEFAULT_MAX_CALLS_PER_TURN = 5

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
    """The text of a user message, whichever content shape it arrived in."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multimodal: join the text parts. Both shapes occur — plain dicts from
        # session files, pydantic TextContent objects once ChatMessage has
        # validated them (its validator upgrades dicts to models).
        parts = []
        for part in content:
            text = (part.get("text") if isinstance(part, dict)
                    else getattr(part, "text", None))
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts)
    return ""


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


def _fill_params(params: Dict[str, Any], groups: Dict[str, str]) -> Dict[str, Any]:
    """Substitute ``{group}`` placeholders from the regex's named groups.

    Only string values are templated; everything else passes through (made
    JSON-native first — see :func:`_json_native`). A value that IS exactly one
    placeholder and captured pure digits becomes an int — ids are the
    overwhelmingly common case (scene_id=42), and tool handlers validate them
    as integers. A mixed string ("kapitel_{n}") stays a string.

    Raises KeyError for a placeholder with no matching group — the rule is
    misconfigured and must be skipped loudly, not called with a literal
    ``{doc}`` as the document name.
    """
    filled: Dict[str, Any] = {}
    for key, value in params.items():
        if not isinstance(value, str):
            filled[key] = _json_native(value)
            continue
        lone = re.fullmatch(r"\{(\w+)\}", value)
        if lone:
            group_value = groups[lone.group(1)]  # KeyError intended
            filled[key] = int(group_value) if group_value.isdigit() else group_value
            continue

        def _sub(m: "re.Match[str]") -> str:
            return groups[m.group(1)]  # KeyError intended

        filled[key] = re.sub(r"\{(\w+)\}", _sub, value)
    return filled


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

    def __init__(self, plugin_dir: Path, mcp_config: Any = None):
        super().__init__(plugin_dir)
        self.mcp_config = mcp_config
        logger.info("ToolPreloadPlugin initialized")

    async def preload(self, context: HookContext) -> HookResult:
        """pre_llm_call: act exactly once per user turn, per the rules."""
        unchanged = HookResult(success=True, modified=False, context=context)
        try:
            messages = context.messages
            if not messages or _role(messages[-1]) != "user":
                return unchanged

            config = context.hook_config or {}
            rules = config.get("rules") or []
            if not rules:
                return unchanged

            agent = context.agent
            if agent is None or not hasattr(agent, "dispatch_tool_call"):
                return unchanged

            # Bounded before it ever reaches a regex — see MATCH_TEXT_LIMIT.
            text = _user_text(messages[-1])[:MATCH_TEXT_LIMIT]
            if not text:
                return unchanged

            plan = self._plan_calls(rules, text, config, messages)
            if not plan:
                return unchanged

            appended = await self._execute_plan(plan, context)
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
                    messages: List[Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """Which (tool, params) to run, in order, after matching and dedup."""
        max_calls = int(config.get("max_calls_per_turn",
                                   DEFAULT_MAX_CALLS_PER_TURN))
        dedup = config.get("dedup", True)
        already = self._calls_in_history(messages) if dedup else set()

        plan: List[Tuple[str, Dict[str, Any]]] = []
        for rule in rules:
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
                    params = _fill_params(call.get("params") or {}, groups)
                except KeyError as e:
                    logger.warning(
                        f"tool_preload: rule {pattern!r} references group {e} "
                        f"that the match did not capture — rule skipped")
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
            plan.extend(resolved)
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
                if not name:
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
    async def _execute_plan(self, plan: List[Tuple[str, Dict[str, Any]]],
                            context: HookContext) -> List[ChatMessage]:
        """Run the plan sequentially; return the message pairs to append.

        Sequential ON PURPOSE: "erst context_var setzen, dann Content laden" is
        a real dependency, so call k+1 must not start before call k finished.

        A call that the dispatcher rejects (unknown tool, not in the agent's
        allowlist) is an operator error: logged, chain aborted, pairs so far
        kept. A call whose TOOL returns an error is appended like any result —
        error answers carry recovery context these days, and the agent would
        have seen the same thing calling it itself.
        """
        from agent_system.servers.agent.components.tool_execution import ToolDispatchError

        # HookContext carries no user_id, but the request does — and without it
        # inject_runtime_params leaves _user_id unset, so a preloaded call runs
        # with less identity than the same call from the LLM. Same source the
        # agent itself uses when it stamps the session metadata.
        user_id = None
        try:
            from agent_system.core.request_context import get_request_user
            if context.request_id:
                user_id = get_request_user(context.request_id, default=None)
        except Exception as e:  # noqa: BLE001 - identity is best-effort here
            logger.debug(f"tool_preload: could not resolve user_id: {e}")

        appended: List[ChatMessage] = []
        for tool, params in plan:
            call_id = f"preload_{uuid.uuid4().hex[:10]}"
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
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"tool_preload: {tool} raised: {e} — chain aborted",
                             exc_info=True)
                break

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
                break
            appended.extend(pair)
            logger.info(f"tool_preload: {tool}({params}) executed before the "
                        f"first LLM turn")
        return appended
