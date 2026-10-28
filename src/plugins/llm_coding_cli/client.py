"""A coding CLI (Claude Code, Antigravity) as one link of an agent's profile chain.

The agent runs as always; its model call goes to a coding_cli instance instead of an API. The CLI gets the
conversation as one task, runs its own tool loop -- the tools of the instance's workdir, through the MCP gateway --
and its final answer comes back as the assistant message, without tool calls. A second call in the same agent run
(a nudge, a format correction) resumes that CLI run with the messages added since. Whatever keeps the CLI from
answering raises an error the chain moves on at: the next link answers instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

from agent_system.llm.models import LLMClient, LLMQuotaExhaustedError, LLMServerError

logger = logging.getLogger(__name__)

#: provider name -> the coding_cli `cli` it must find on the instance.
CLI_OF = {"claude_code": "claude", "antigravity": "antigravity"}
#: The CLI runs of the agent runs still going: one agent run's calls continue one CLI run.
MAX_REMEMBERED_RUNS = 256
WAIT_S = 600


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


def _content(value: Any) -> Any:
    """coding_cli hands the CLI's words over as {"untrusted": true, "content": ...}: here they are the answer."""
    return value.get("content") if isinstance(value, dict) and value.get("untrusted") else value


class CodingCliClient(LLMClient):
    def __init__(self, provider: str, instance: str, *, context_window: int, capabilities: Any = None):
        self.provider = provider
        self.instance = instance
        self.model = instance           # the key model_health blocks this link by
        self.base_url = f"coding_cli:{instance}"
        self.context_window = context_window
        self.capabilities = capabilities
        #: request id -> (run id, how many messages it was given, their fingerprint)
        self._runs: "OrderedDict[str, tuple[str, int, str]]" = OrderedDict()

    def supports_streaming(self) -> bool:
        return False

    async def chat(self, messages, cancellation_token=None, status_scope=None) -> str:
        return (await self.chat_tools(messages, [], cancellation_token=cancellation_token))["assistant"]["content"]

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None) -> dict:
        from agent_system.core.request_context import current_run_session, current_run_user
        from agent_system.tools.status import current_request_id

        request_id, user, session = current_request_id.get(), current_run_user.get(), current_run_session.get()
        if not request_id or not user or not session:
            # The CLI's tool calls belong to the agent's session (a store's owner); there is none to give them.
            raise self._fail("no agent run around the call (request, user and session are needed)")
        server = self._server()
        args = {"task": render(messages)}
        prior = self._runs.get(request_id)
        if prior and _fingerprint(messages[:prior[1]]) == prior[2]:
            added = render(messages[prior[1]:])
            if added.strip():
                # The same conversation, grown: the run goes on with what was added. Anything else -- a history
                # compacted since, another caller of this client in the same agent run (a hook's own call) --
                # starts a run of its own, or the CLI would answer another conversation.
                args = {"task": added, "resume": prior[0]}
        started = time.monotonic()
        await self._notify_pre_request({"provider": self.provider, "model": self.instance, "url": self.base_url,
                                        "payload": args, "is_streaming": False,
                                        "timestamp_ms": int(time.time() * 1000)})
        answer, error = None, None
        try:
            try:
                answer = await self._run(server, args, user, session, request_id, cancellation_token)
            except Exception as exc:
                # coding_cli raised instead of answering (a file it could not write, a tool name it does not
                # know): an error the chain moves on at, not the end of the agent run. A cancel is no Exception.
                raise self._fail(f"coding_cli call failed: {type(exc).__name__}: {exc}") from exc
            text = self._answer_text(answer)
        except BaseException as exc:
            error = exc
            raise
        finally:
            usage = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            await self._notify_post_response({
                "provider": self.provider,
                "model": (answer.get("model") if isinstance(answer, dict) else None) or self.instance,
                "url": self.base_url, "is_streaming": False, "duration_ms": int((time.monotonic() - started) * 1000),
                "timestamp_ms": int(time.time() * 1000), "response_data": answer, "usage": usage,
                "finish_reason": None if error else "stop",
                "error": f"{type(error).__name__}: {error}" if error is not None else None,
                "routing": {"selected": f"{self.provider}:{self.instance}"}})
        self._runs[request_id] = (answer["run_id"], len(messages), _fingerprint(messages))
        self._runs.move_to_end(request_id)
        while len(self._runs) > MAX_REMEMBERED_RUNS:
            self._runs.popitem(last=False)
        return {"assistant": {"role": "assistant", "content": text,
                              "served_by": f"{self.provider}:{self.instance}"},
                "usage": usage, "finish_reason": "stop"}

    def _server(self) -> Any:
        from agent_system import app_state

        registry = app_state.app_registry
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

    def _answer_text(self, answer: Any) -> str:
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
            raise self._fail(f"run {answer.get('run_id') or '-'} {answer.get('state') or 'not started'}: {why}")
        if answer.get("result_cut"):
            raise self._fail(f"run {answer.get('run_id')}: the answer is longer than the instance hands on "
                             "(coding_cli max_result_chars)")
        text = _content(answer.get("result"))
        if not isinstance(text, str) or not text.strip():
            raise self._fail(f"run {answer.get('run_id')} ended without an answer")
        return text

    def _fail(self, why: str, level: int = logging.WARNING) -> LLMServerError:
        """An error the profile chain moves on at (no block: the next call may find the CLI free again)."""
        logger.log(level, "%s %s: %s -- the next link of the chain answers", self.provider, self.instance, why)
        return LLMServerError(f"{self.provider} {self.instance}: {why}", self.provider, self.instance)
