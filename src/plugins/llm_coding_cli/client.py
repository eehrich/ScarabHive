"""A coding CLI (Claude Code, Antigravity) as one link of an agent's profile chain.

The agent runs as always; its model call goes to a coding_cli instance instead of an API. The CLI gets the
conversation as one task, runs its own tool loop -- the tools of the instance's workdir, through the MCP gateway --
and its final answer comes back as the assistant message, without tool calls. A later call of the same session --
a nudge or format correction in the same agent run, the next turn of a continued sub-agent -- resumes that CLI run
with the messages added since. Whatever keeps the CLI from answering raises an error the chain moves on at: the
next link answers instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import logging
import re
import socket
import time
from collections import OrderedDict
from typing import Any, Sequence

from agent_system.llm.models import LLMClient, LLMConnectionError, LLMQuotaExhaustedError, LLMServerError

logger = logging.getLogger(__name__)

#: provider name -> the coding_cli `cli` it must find on the instance.
CLI_OF = {"claude_code": "claude", "antigravity": "antigravity"}
#: The CLI runs a later call of their session may go on with: (instance, user, session) -> its last runs, newest
#: last, each (run id, how many messages it was given, their fingerprint, the fingerprint of its answer). In this
#: process, not one client's: a continued sub-agent is built anew, with clients of its own. Several per session: a
#: hook's own call in between (another conversation) must not cost the agent's its run.
_conversations: "OrderedDict[tuple[str, str, str], list[tuple[str, int, str, str]]]" = OrderedDict()
MAX_REMEMBERED_SESSIONS = 256
#: What a call that brought no run's counts reports: nothing, at no cost. Never the size of the agent's context:
#: the CLI's run is a conversation of its own, its counts summed over its model calls -- the usage store keeps them as
#: throughput and lets no reader take them for how full the context is.
USAGE = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reflects_context": False}
RUNS_PER_SESSION = 8
WAIT_S = 600
#: How long an agy run that found its login expired waits before it starts once more, and how long no run is
#: started again after the second one failed the same way too: a login that is gone costs no wait per call.
AUTH_RETRY_S = 15
LOGIN_LOST_S = 600
#: What an agy run is told when it ended on a step refused headless: agy then ends the whole conversation with an
#: empty answer -- measured on a server, 7 of 16 runs of a Flash instance, each with a refused command -- and resumed
#: with these words it answered (4 of 4, gemini-3.8-flash, the refusal provoked). Once per call.
REFUSED_NOTE = ("Your last step was refused ({denied}): such steps are refused in this run, and there is no shell. "
                "Go on with your other tools and answer the task as it was asked.")
#: (coding_cli instance, user) -> until when that login counts as gone: not one client's -- every agent and hook
#: with a link to it builds a client of its own. Per user, as agy may keep the login in the user's home of the
#: instance (agy_home/<user>); where all homes share one (Windows: the credential store), a lost login costs each
#: user one second run per LOGIN_LOST_S, never one too few.
_login_lost_until: dict[tuple[str, str], float] = {}


def _field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def _text(content: Any) -> str:
    """A message's content as text; parts that are not text are named, not dropped without a word."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            kind = _field(part, "type")
            parts.append(str(_field(part, "text") or "") if kind == "text" else f"[{kind or 'part'} not passed on]")
        return "\n".join(p for p in parts if p)
    return str(content)


def render(messages: list[Any]) -> str:
    """The messages as one task text, in their order: the system prompt first, every message after it under its
    role -- hook insertions included, as the API would have read them. Tool calls an earlier link made are shown as
    what they were; the CLI calls its tools itself."""
    blocks = []
    for message in messages:
        role = str(_field(message, "role") or "user")
        text = _text(_field(message, "content"))
        if role == "system":
            blocks.append(f"# Instructions\n\n{text}")
            continue
        calls = _field(message, "tool_calls") or []
        for call in calls:
            function = _field(call, "function") or {}
            text += f"\n[called {_field(function, 'name')} with {_field(function, 'arguments')}]"
        if text.strip():
            blocks.append(f"## {role}\n\n{text.strip()}")
    return "\n\n".join(blocks)


def _fingerprint(messages: list[Any]) -> str:
    return hashlib.sha256(render(messages).encode("utf-8", "replace")).hexdigest()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _usage(answer: Any) -> dict:
    """The run's tokens in the usage store's terms, the cache's among them: prompt tokens with the cached reads and
    writes inside, at no cost -- the subscription bills no call. Both CLIs count the cache BESIDE their input:
    Claude Code as Anthropic's API names it (the normalizer adds it back), agy as cache_read_tokens (measured on a
    server: reads up to nine times its input_tokens, total_tokens = input_tokens + output_tokens)."""
    from agent_system.llm.pricing import normalize_usage

    raw = answer.get("usage") if isinstance(answer, dict) else None
    if not isinstance(raw, dict) or not raw:
        return dict(USAGE)
    call = normalize_usage(raw)
    reads = int(raw.get("cache_read_tokens") or 0)      # agy's, which the normalizer does not know
    prompt = call.prompt_tokens + reads
    # agy's output_tokens hold its thinking_tokens: its total_tokens is input + output (measured).
    return {**USAGE, "prompt_tokens": prompt, "completion_tokens": call.completion_tokens,
            "total_tokens": prompt + call.completion_tokens,
            "prompt_tokens_details": {"cached_tokens": call.cached_tokens + reads,
                                      "cache_write_tokens": call.cache_write_tokens}}


def _summed(*usages: dict) -> dict:
    """Several runs' usage (_usage) as the one call they answered."""
    if len(usages) == 1:
        return usages[0]
    total = {**USAGE}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = sum(u.get(key, 0) for u in usages)
    details = [u.get("prompt_tokens_details") or {} for u in usages]
    total["prompt_tokens_details"] = {key: sum(d.get(key, 0) for d in details)
                                      for key in ("cached_tokens", "cache_write_tokens")}
    return total


def _refused_resume(answer: Any) -> bool:
    """coding_cli refused to go on with the run it was asked to resume (run_task's "resume: ..." errors, and other
    bindings than the run's)."""
    error = str(answer.get("error") or "") if isinstance(answer, dict) and answer.get("status") == "error" else ""
    return (error.startswith(("resume:", "pin: a resume keeps"))
            # its worktree, as _start finds it: a file agy would run left in it, a record without its session
            or error.startswith("not started:") and ("the worktree carries" in error
                                                     or "holds no usable session id" in error))


def _content(value: Any) -> Any:
    """coding_cli hands the CLI's words over as {"untrusted": true, "content": ...}: here they are the answer."""
    return value.get("content") if isinstance(value, dict) and value.get("untrusted") else value


#: Claude Code's code at the end of the result of a run that did not reach its API (api_error connection_failed),
#: measured on 2.1.x, 10.11.2026: "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
#: with DNS gone, "... Connection refused ... (ECONNREFUSED)" at a refusing proxy, each after about 3 min of retries.
_NODE_ERROR_CODE = re.compile(r"\((E[A-Z_]+)\)\s*$")
#: Node's name for a lookup that failed: a socket.gaierror to Python.
_DNS_CODES = frozenset({"ENOTFOUND", "EAI_AGAIN"})


def _transport_cause(answer: dict) -> OSError | None:
    """The transport error a run that did not reach its API ended on, as Python names it, or None.

    The code decides, as it does for an HTTP client: a failed lookup is a gaierror, the others an OSError with their
    errno (ECONNREFUSED is then a ConnectionRefusedError, which fallback._never_reached counts as no outage: the
    host answered). A code Python does not know is an OSError without one -- reached, as far as the chain can tell."""
    if answer.get("api_error") != "connection_failed":
        return None
    match = _NODE_ERROR_CODE.search(str(_content(answer.get("result")) or ""))
    code = match.group(1) if match else ""
    if code in _DNS_CODES:
        return socket.gaierror(code)
    number = getattr(errno, code, None) if code else None
    return OSError(number, code) if number is not None else OSError(code or "connection_failed")


class CodingCliClient(LLMClient):
    def __init__(self, provider: str, instance: str, *, context_window: int, capabilities: Any = None):
        self.provider = provider
        self.instance = instance
        self.model = instance           # the key model_health blocks this link by
        self.base_url = f"coding_cli:{instance}"
        self.context_window = context_window
        self.capabilities = capabilities

    def supports_streaming(self) -> bool:
        return False

    async def chat(self, messages, cancellation_token=None, status_scope=None) -> str:
        return (await self.chat_tools(messages, [], cancellation_token=cancellation_token))["assistant"]["content"]

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None) -> dict:
        from agent_system.core.request_context import current_run_session, current_run_user, current_tool_bindings
        from agent_system.tools.status import current_request_id

        request_id, user, session = current_request_id.get(), current_run_user.get(), current_run_session.get()
        if not request_id or not user or not session:
            # The CLI's tool calls belong to the agent's session (a store's owner); there is none to give them.
            raise self._fail("no agent run around the call (request, user and session are needed)")
        server = self._server()
        fresh = {"task": render(messages)}
        args = self._going_on(messages, (self.instance, user, session)) or fresh
        bindings = current_tool_bindings.get()
        if bindings:
            # The agent run's tool bindings hold the CLI's tool calls too (coding_cli hands them to its grant); a
            # run without sends no pin at all, which a workdir without pins expects.
            args["pin"] = bindings
        key = (self.instance, user, session)
        started = time.monotonic()
        await self._pre(args)
        answer, error, posted = None, None, False
        went_on: list[str] = []         # runs a run of this call resumed: their conversation holds its turns
        spent: list[dict] = []          # the usage of runs of this call another went on from: counted too
        try:
            try:
                answer = await self._run(server, args, user, session, request_id, cancellation_token)
                if "resume" in args and _refused_resume(answer):
                    # Its run cannot go on (its worktree gone, another run in it): the whole conversation, fresh --
                    # a request of its own for whoever reads the notices, the refused one answered first.
                    logger.info("%s %s: run %s cannot be resumed (%s), starting a fresh one", self.provider,
                                self.instance, args["resume"], answer.get("error"))
                    posted = True
                    await self._post(answer, f"resume refused: {answer.get('error')}", started)
                    args = {**args, **fresh}
                    args.pop("resume")
                    started = time.monotonic()
                    await self._pre(args)
                    posted = False
                    answer = await self._run(server, args, user, session, request_id, cancellation_token)
                login = (self.instance, user)
                if self._login_raced(answer, login):
                    # agy renews its login itself; measured, one run ended before its first turn on "authentication
                    # failed or timed out" 73 s before the token's expiry, while another process used the same agy
                    # home, and the next run renewed it. A start once more is the cheap answer to such a moment
                    # (whether 15 s is long enough is not measured); a login that is really gone fails again, the
                    # chain moves on, and for LOGIN_LOST_S no call of this user on this instance starts a second run.
                    logger.info("%s %s: run %s found the login expired, starting once more in %d s", self.provider,
                                self.instance, answer.get("run_id"), AUTH_RETRY_S)
                    from plugins.llm_common.cancellation import await_call

                    await await_call(asyncio.ensure_future(asyncio.sleep(AUTH_RETRY_S)), cancellation_token)
                    answer = await self._run(server, args, user, session, request_id, cancellation_token)
                    if self._login_raced(answer, login):
                        _login_lost_until[login] = time.monotonic() + LOGIN_LOST_S
                if isinstance(answer, dict) and answer.get("state") == "done":
                    _login_lost_until.pop(login, None)  # the login is there again
                denied = self._ended_on_a_refusal(answer)
                if denied:
                    logger.info("%s %s: run %s ended without an answer on a refused step (%s), resuming it once",
                                self.provider, self.instance, answer.get("run_id"), ", ".join(denied))
                    posted = True
                    await self._post(answer, f"ended on a refused step: {', '.join(denied)}", started)
                    if "resume" in args:
                        went_on.append(args["resume"])
                    spent.append(_usage(answer))
                    args = {**args, "task": REFUSED_NOTE.format(denied=", ".join(denied)),
                            "resume": answer["run_id"]}
                    answer = None           # answered: a notice from here on is the resume's
                    started = time.monotonic()
                    await self._pre(args)
                    posted = False
                    answer = await self._run(server, args, user, session, request_id, cancellation_token)
            except Exception as exc:
                # coding_cli raised instead of answering (a file it could not write, a tool name it does not
                # know): an error the chain moves on at, not the end of the agent run. A cancel is no Exception.
                raise self._fail(f"coding_cli call failed: {type(exc).__name__}: {exc}") from exc
            text = self._answer_text(answer, earlier=spent)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if not posted:  # a cancel in the refused one's notice: that request is answered already
                await self._post(answer, f"{type(error).__name__}: {error}" if error is not None else None, started)
            if "resume" in args and not (isinstance(answer, dict) and answer.get("status") == "error"):
                # The run it resumed went on, ended well or not (a cancel, a failed or cut run): the CLI's
                # conversation holds the new turns, one that went on from the old run's history would get them twice.
                # Only a refusal before the start (coding_cli's error answer) leaves it as it was.
                went_on.append(args["resume"])
            if went_on:
                kept = [r for r in _conversations.get(key, []) if r[0] not in went_on]
                if kept:
                    _conversations[key] = kept
                else:
                    _conversations.pop(key, None)
        runs = [r for r in _conversations.pop(key, []) if r[0] != answer["run_id"]]
        _conversations[key] = (runs + [(answer["run_id"], len(messages), _fingerprint(messages),
                                        _digest(text))])[-RUNS_PER_SESSION:]
        while len(_conversations) > MAX_REMEMBERED_SESSIONS:
            _conversations.popitem(last=False)
        return {"assistant": {"role": "assistant", "content": text,
                              "served_by": f"{self.provider}:{self.instance}"},
                "usage": _summed(*spent, _usage(answer)), "finish_reason": "stop"}

    async def _pre(self, args: dict) -> None:
        await self._notify_pre_request({"provider": self.provider, "model": self.instance, "url": self.base_url,
                                        "payload": args, "is_streaming": False,
                                        "timestamp_ms": int(time.time() * 1000)})

    async def _post(self, answer: Any, error: str | None, started: float) -> None:
        await self._notify_post_response({
            "provider": self.provider,
            "model": (answer.get("model") if isinstance(answer, dict) else None) or self.instance,
            "url": self.base_url, "is_streaming": False, "duration_ms": int((time.monotonic() - started) * 1000),
            "timestamp_ms": int(time.time() * 1000), "response_data": answer, "usage": _usage(answer),
            "finish_reason": None if error else "stop", "error": error,
            "routing": {"selected": f"{self.provider}:{self.instance}"}})

    @staticmethod
    def _going_on(messages: list[Any], key: tuple[str, str, str]) -> dict | None:
        """The task that resumes a run of this session which these messages go on from -- the longest such
        conversation --, or None: a history compacted since, another conversation (a hook's own call), nothing new
        start a run of their own, or the CLI would answer another conversation."""
        for run_id, count, fingerprint, answered in sorted(_conversations.get(key, []), key=lambda r: -r[1]):
            if count >= len(messages) or _fingerprint(messages[:count]) != fingerprint:
                continue
            added = messages[count:]
            # Its own answer, as the agent keeps it, is the CLI's last turn already.
            if (_field(added[0], "role") == "assistant" and not _field(added[0], "tool_calls")
                    and _digest(_text(_field(added[0], "content"))) == answered):
                added = added[1:]
            task = render(added)
            return {"task": task, "resume": run_id} if task.strip() else None
        return None

    def _server(self) -> Any:
        from agent_system import app_state

        registry = app_state.registry  # the API's, or agent-run's: the instance runs in this process either way
        try:
            server = registry.get(self.instance) if registry is not None else None
        except KeyError:
            server = None
        if server is None:
            raise self._fail(f"coding_cli instance {self.instance!r} is not running in this process")
        cli = getattr(server, "cli", None)
        if cli != CLI_OF[self.provider]:
            raise self._fail(f"instance {self.instance!r} runs cli {cli!r}, provider {self.provider} needs "
                             f"{CLI_OF[self.provider]!r} -- fix the model entry", level=logging.ERROR)
        return server

    async def _run(self, server: Any, args: dict, user: str, session: str, request_id: str, token: Any) -> dict:
        """The finished run's answer. Cancelling the agent cancels the wait, which stops the run (stop_if_cancelled:
        coding_cli stops a run whose waiting caller is cancelled)."""
        from plugins.llm_common.cancellation import await_call

        # call_with_status routes the status by request_id and hands it on as _request_id, which run_task reads
        # (the prefix of the run's grant).
        context = {"_user_id": user, "_session_id": session, "request_id": request_id}

        async def drive() -> dict:
            answer = await server.call_with_status(f"{self.instance}_run_task", {**args, "wake": False, **context})
            try:
                while isinstance(answer, dict) and answer.get("state") == "running":
                    answer = await server.call_with_status(f"{self.instance}_get_run", {
                        "run_id": answer["run_id"], "wait_s": WAIT_S, "stop_if_cancelled": True, **context})
            except asyncio.CancelledError:
                # get_run stops the run only when the cancel lands in its handler; one that lands around it (its
                # status scope publishing) would leave the run going on the subscription, its answer thrown away.
                with contextlib.suppress(Exception):
                    await asyncio.shield(server.call_with_status(f"{self.instance}_cancel_run",
                                                                 {"run_id": answer["run_id"], **context}))
                raise
            return answer

        return await await_call(asyncio.ensure_future(drive()), token)

    def _ended_on_a_refusal(self, answer: Any) -> list[str]:
        """What an agy run that ended without an answer had refused (permission denials), or []."""
        if self.provider != "antigravity" or not isinstance(answer, dict) or answer.get("state") != "done":
            return []
        text = _content(answer.get("result"))
        if (isinstance(text, str) and text.strip()) or not answer.get("run_id"):
            return []
        denials = _content(answer.get("denials"))
        return [str(d) for d in denials if d] if isinstance(denials, list) else []

    def _login_raced(self, answer: Any, login: tuple[str, str]) -> bool:
        """An agy run that ended before its first turn on its login (agy: "authentication failed or timed out")."""
        if self.provider != "antigravity" or not isinstance(answer, dict) or answer.get("state") != "failed":
            return False
        if answer.get("turns") or time.monotonic() < _login_lost_until.get(login, 0.0):
            return False
        result = answer.get("result")
        return "authentication" in str((result.get("content") if isinstance(result, dict) else result) or "")

    def _answer_text(self, answer: Any, earlier: Sequence[dict] = ()) -> str:
        """The run's answer, or the error the chain moves on at. ``earlier``: the usage of runs of this call it went
        on from (a refused step resumed) -- their tokens show the provider was reached just as the run's own do."""
        if not isinstance(answer, dict):
            raise self._fail(f"no answer from coding_cli: {str(answer)[:200]}")
        limit = answer.get("limit")
        if answer.get("status") == "error" or answer.get("state") != "done":
            why = answer.get("error") or answer.get("note") or answer.get("state") or "no reason given"
            if isinstance(limit, dict):
                resets = limit.get("resets_at")
                wait = max(0.0, float(resets) - time.time()) if isinstance(resets, (int, float)) else None
                logger.warning("%s %s: the subscription's limit -- %s", self.provider, self.instance, why)
                raise LLMQuotaExhaustedError(f"{self.provider} {self.instance}: {why}", self.provider, self.instance,
                                             retry_after=wait)
            # Tokens counted are the provider's: a run that worked and then failed (its turns used up, its time, the
            # network gone midway) reached its model, whatever else it says -- asked again after a wait for the
            # network, its work and its tools' effects would run twice. First, before the cause it names. agy counts
            # a resumed conversation's tokens whole -- coding_cli hands on the run's own share -- and with the
            # network gone it fails before it loads one: 0 tokens on a resume too (measured 11.11.2026)
            tokens = sum(u["prompt_tokens"] + u["completion_tokens"] for u in (*earlier, _usage(answer)))
            # A run killed before its result (its time limit) counts none, but its stream shows the model answered
            reached = tokens > 0 or answer.get("model_answered") is True
            cause = _transport_cause(answer) if not reached else None
            if cause is not None:
                # As an HTTP client says it: the chain waits for the network when no link of it was reached
                # (fallback._never_reached reads the cause), instead of walking on to its end
                logger.warning("%s %s: the CLI did not reach its API -- %s", self.provider, self.instance, cause)
                raise LLMConnectionError(f"{self.provider} {self.instance}: {why}", self.provider,
                                         self.instance) from cause
            raise self._fail(f"run {answer.get('run_id') or '-'} {answer.get('state') or 'not started'}: {why}",
                             reached=reached)
        if answer.get("result_cut"):
            raise self._fail(f"run {answer.get('run_id')}: the answer is longer than the instance hands on "
                             "(coding_cli max_result_chars)", reached=True)
        text = _content(answer.get("result"))
        if not isinstance(text, str) or not text.strip():
            raise self._fail(f"run {answer.get('run_id')} ended without an answer", reached=True)
        return text

    def _fail(self, why: str, level: int = logging.WARNING, *, reached: bool = False) -> LLMServerError:
        """An error the profile chain moves on at (no block: the next call may find the CLI free again).

        ``reached``: the CLI's model answered (an answer cut or empty, tokens counted). Otherwise the run failed before or without
        saying why -- agy with the network gone, say, finds its model unknown -- and the chain does not count it as a
        provider that got the request (LLMServerError.reach_unknown): a link before or after it decides whether the
        step waits for the network."""
        logger.log(level, "%s %s: %s -- the next link of the chain answers", self.provider, self.instance, why)
        return LLMServerError(f"{self.provider} {self.instance}: {why}", self.provider, self.instance,
                              reach_unknown=not reached)
