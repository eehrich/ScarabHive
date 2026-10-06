"""ask_user -- the model asks the person watching the run, and waits for the answer.

One tool, named like its instance. The question goes where tool_approval's
questions go (``core.run_questions``): a status line of the call's own row,
with ``meta.ask_user``; the web chat draws the question with its options and
an answer field on that row and posts the answer to this plugin's route
(web.py), agent-cli chat prints it from its ``form`` and answers in its own
process (``run_questions.answer_question``). The call waits for that answer, the run's cancellation,
``ask_timeout``, or until nobody reads the run any more -- whichever comes
first -- and the row's last line says which. A message the person types into
the chat meanwhile ends the wait too (in agent-cli chat: a line begun before the
question showed; one begun after is its answer): written to the call's own run, the model
reads it as their next message; written to a run above it (a sub-agent asks,
the person writes to the conversation they watch), the sub-agent is told to
finish with what it has, so the run above can read the message.

Nobody to ask -- a run no person watches (openai_api, agent-run, a one-shot
agent-cli, a JSON /run, a job, a writer dispatch, an async sub-agent whose
caller's stream ended) --
and the call returns at once with an error that tells the model to decide
itself. So does a call that is not the model's own turn (a script, a state
machine, a slash command): it has no cancellation token of a run nor a status
row of its own, and the caller's own timeout would cut the question off.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, Mapping, Optional

from agent_system.core.request_context import get_request_user
from agent_system.core.run_questions import CANCELLED, GONE, TIMEOUT, is_read, put_to_person, status_line
from agent_system.servers.agent.components.session_tracking import message_waits_for
from agent_system.tools.schema_based import SchemaBasedToolServer

from .questions import ArgumentError, AskUserBroker, UserAnswer, UserQuestion, parse_arguments

logger = logging.getLogger(__name__)

#: The key of ``meta`` that carries the question to the chat (static/js/chat_module.js).
META_KEY = "ask_user"

DEFAULT_ASK_TIMEOUT = 300.0
DEFAULT_REASK_SECONDS = 20.0
DEFAULT_GONE_AFTER_SECONDS = 15.0

#: The person wrote into the call's own run instead of answering.
WROTE = "wrote"
#: The person wrote into a run above the call's (the conversation a sub-agent's caller holds).
WROTE_ABOVE = "wrote_above"

NOBODY_TEXT = ("Nobody can answer: no person is watching this run (it runs over the API, as a job, "
               "or the chat was closed). Do not ask again in this run. Decide yourself, go on, and "
               "say in your reply what you assumed.")
GONE_TEXT = ("Nobody can answer any more: the person stopped watching this run while the question "
             "waited. Do not ask again in this run. Decide yourself, go on, and say in your reply "
             "what you assumed.")
WROTE_NOTE = ("A message from the user came in while the question waited; it follows as their next "
              "message. Read it -- it may answer the question -- and go on from there.")
WROTE_ABOVE_TEXT = ("The user wrote to the main conversation instead of answering. That message goes to "
                    "the agent that started you, and you do not see it. Do not ask again: finish your task "
                    "with what you have, say in your reply what you assumed, and return, so that agent can "
                    "read the message.")
NOT_OWN_CALL_TEXT = ("ask_user answers only a call the model makes itself, in its own turn -- not one "
                     "made from a script or by another tool. Call it directly.")


def _seconds(config: Mapping[str, Any], key: str, default: float) -> float:
    """A positive number of seconds from the instance config, else the default (logged)."""
    raw = config.get(key, default)
    try:
        value = float(raw) if not isinstance(raw, bool) else math.nan
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        logger.warning("ask_user: %s must be a number of seconds above 0, got %r; using %g",
                       key, raw, default)
        return default
    return value


def _run_of(request_id: str) -> str:
    """The run a call belongs to: the call runs as ``<run id>_<nnn>``
    (Agent.next_internal_tool_request_id)."""
    return request_id.rsplit("_", 1)[0] if "_" in request_id else request_id


def _owner(params: Dict[str, Any], request_id: str) -> Optional[str]:
    """Whose run asks: the session's user as the loop injects it, else the user
    the run was registered under (the app does that as the run starts)."""
    return (params.get("_user_id") or get_request_user(request_id, default=None)
            or get_request_user(_run_of(request_id), default=None))


def _written_to(request_id: str) -> Optional[Callable[[], Optional[str]]]:
    """A check for a message the person wrote while the question waits: WROTE
    when it waits for the call's own run (a mid-run message the model has not
    read yet -- also one that came while it was still thinking), WROTE_ABOVE
    when it waits for a run above it, by request-id ancestry (``<run>_<nnn>``
    is a call of ``<run>``, ``<call>_sub_<id>`` a sub-agent's run started by
    that call). The runs are looked up in every agent's tracker: a sub-agent
    runs on an agent of its own, the conversation the person watches on
    another. None for a call that belongs to no run."""
    run_id = _run_of(request_id)
    if run_id == request_id:
        return None
    parts = run_id.split("_")
    above = ["_".join(parts[:end]) for end in range(len(parts) - 1, 0, -1)]

    def written() -> Optional[str]:
        if message_waits_for(run_id):
            return WROTE
        if any(message_waits_for(ancestor) for ancestor in above):
            return WROTE_ABOVE
        return None

    return written


def _summary(answer: UserAnswer) -> str:
    """The answer in a few words, for the row's last line."""
    parts = [", ".join(answer.choices)] if answer.choices else []
    if answer.text:
        parts.append(f'"{answer.text}"')
    return " -- ".join(parts)


class AskUserServer(SchemaBasedToolServer):
    """The ask_user tool, its open questions (``broker``) and the route that answers them."""

    def __init__(self, name: str, system_config: Any, server_config: Any):
        super().__init__(name, system_config, server_config)
        config = getattr(server_config, "config", None) if server_config is not None else None
        config = config if isinstance(config, Mapping) else {}
        self.ask_timeout = _seconds(config, "ask_timeout", DEFAULT_ASK_TIMEOUT)
        self.reask_seconds = _seconds(config, "reask_seconds", DEFAULT_REASK_SECONDS)
        self.gone_after_seconds = _seconds(config, "gone_after_seconds", DEFAULT_GONE_AFTER_SECONDS)
        self.broker = AskUserBroker()

    @property
    def answer_url(self) -> str:
        return f"/plugins/{self.name}/answer"

    def get_web_router(self):
        from .web import build_router
        return build_router(self)

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool ``<instance>``: ask the person watching the run, return their answer."""
        status = params.get("_status")
        token = params.get("_cancellation_token")
        request_id = str(params.get("_request_id") or "")
        try:
            text, options, multi_select = parse_arguments(params)
        except ArgumentError as exc:
            if status is not None:
                await status.error(status_line(f"question not asked: {exc}"))
            return {"status": "error", "error": str(exc)}
        if token is None or status is None:
            # The model's own calls carry the run's token and a status row of their
            # own (tool_execution, call_with_status); a script's, a state machine's
            # or a slash command's carry no token, and their callers cut a call off
            # after their own timeout.
            if status is not None:
                await status.error(status_line(f"not asked, not the model's own call: {text}"))
            return {"status": "error", "error": NOT_OWN_CALL_TEXT, "reason": "not_own_call"}
        if getattr(token, "is_cancelled", False):
            return {"error": f"Tool '{self.name}' was cancelled.", "cancelled": True,
                    "forced": bool(getattr(token, "is_forced", False))}
        if not is_read(request_id, self.gone_after_seconds):
            if status is not None:
                await status.error(status_line(f"not asked, nobody watches this run: {text}"))
            return {"status": "error", "error": NOBODY_TEXT, "reason": "unattended"}

        question = self.broker.open_question(
            UserQuestion, owner=_owner(params, request_id), session_id=str(params.get("_session_id") or ""),
            request_id=request_id, agent_name=str(params.get("_agent_name") or ""),
            timeout=self.ask_timeout, question=text, options=options, multi_select=multi_select)
        try:
            # The call's own row carries the question.
            outcome = await put_to_person(
                self.broker, question, status, meta_key=META_KEY, answer_url=self.answer_url,
                line=status_line(f"Question: {text}"), token=token, reask_seconds=self.reask_seconds,
                gone_after_seconds=self.gone_after_seconds,
                cut_off=f"question cut off while waiting: {text}",
                interrupt=_written_to(request_id))
            return await self._settle(status, outcome, token)
        finally:
            self.broker.close(question)

    async def _settle(self, scope: Any, outcome: Any, token: Any) -> Dict[str, Any]:
        """The row's last line and the tool result, per outcome."""
        if isinstance(outcome, UserAnswer):
            await scope.end(status_line(f"answered by {outcome.answered_by or 'the user'}: {_summary(outcome)}"))
            return {"status": "success", "choices": list(outcome.choices), "text": outcome.text}
        if outcome == CANCELLED:
            await scope.error(status_line("not answered, the run was cancelled while the question waited"))
            return {"error": f"Tool '{self.name}' was cancelled.", "cancelled": True,
                    "forced": bool(getattr(token, "is_forced", False))}
        if outcome == WROTE:
            await scope.end(status_line("not answered here: the user wrote in the chat instead"))
            return {"status": "success", "choices": [], "text": "", "replied_in_chat": True,
                    "note": WROTE_NOTE}
        if outcome == WROTE_ABOVE:
            await scope.end(status_line("not answered here: the user wrote to the main conversation"))
            return {"status": "error", "reason": "replied_above", "error": WROTE_ABOVE_TEXT}
        if outcome == GONE:
            await scope.error(status_line("not answered, nobody reads the run any more"))
            return {"status": "error", "error": GONE_TEXT, "reason": "gone"}
        if outcome != TIMEOUT:   # pragma: no cover - wait_for_answer returns nothing else
            logger.error("ask_user: unexpected outcome %r", outcome)
        await scope.error(status_line(f"no answer within {self.ask_timeout:g} s"))
        return {"status": "error", "reason": "timeout",
                "error": (f"No answer came within {self.ask_timeout:g} seconds. Do not ask the same "
                          "question again right away: decide yourself, go on, and say in your reply "
                          "what you assumed -- the user can correct it.")}
