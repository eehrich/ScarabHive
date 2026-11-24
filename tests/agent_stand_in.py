"""Stand-ins for the agent in tests: for the agent itself, or for the model it calls.

``stand_in_for_agent`` -- the real Agent class with the stand-in's body (CLI tests). Replacing the class in its
module instead (``monkeypatch.setattr("...server.Agent", Stand)``) poisons every plugin first imported during
that test: plugin discovery imports them all, and stategraph's ``class MachineAgent(Agent)`` then subclasses the
stand-in for the rest of the session (measured: 28 facade tests failed after test_cli_attachments.py). Patching the
methods keeps the class; subclasses built meanwhile stay real ones.

``stand_in_for_model`` -- the real agent, and in place of its language model a ``ModelStandIn`` that answers from
a script (tests/software: the browser tests of the web interface). Everything of the run but the model is
ScarabHive's own: the request, the session and its history, the events the page shows, a cancel. Installed at
the one construction seam of every client (llm/registry.build_client), so every agent, fallback and profile
switch gets it -- the same seam the root conftest swaps for its echoing fake.

A script is a list of answers, each chosen by the last thing the user wrote (``when``, a regular expression)
or forced by its name for the next run (``use``); without a match the default answers:

    stand_in = ModelStandIn.from_data({"default": {"text": "Done."},
                                       "answers": {"slow": {"when": "(?i)slow", "text": "...", "delay": 30}}})
    stand_in_for_model(monkeypatch, stand_in)
    stand_in.use("slow")            # the next run waits 30 s -- long enough to press Stop
    stand_in.when("(?i)weather", text="Sunny.")   # this test's own answer, before the script's
    stand_in.calls[-1]["user"]      # what the agent sent the model last
"""
from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional


def stand_in_for_agent(monkeypatch, stand_in) -> None:
    """``Agent(...)`` runs the stand-in's ``__init__`` and ``run_events`` for the test; isinstance holds."""
    from agent_system.servers.agent.server import Agent

    monkeypatch.setattr(Agent, "__init__", stand_in.__init__)
    monkeypatch.setattr(Agent, "run_events", stand_in.run_events)


# ── the model's stand-in ─────────────────────────────────────────────────────

#: how often a waiting answer looks whether its run was cancelled (seconds)
CANCEL_POLL = 0.05


@dataclass(frozen=True)
class Answer:
    """One answer of the model: its text, streamed in ``chunks``, after ``delay`` seconds -- or an ``error`` the
    call raises (the agent reports it, or moves to a fallback)."""

    name: str = "default"
    text: str = "Stand-in: done."
    when: str = ""            # a regular expression on the last user message; "" only by name or as the default
    delay: float = 0.0
    chunks: int = 1
    error: str = ""

    @classmethod
    def of(cls, name: str, data: dict[str, Any]) -> "Answer":
        unknown = set(data) - {"text", "when", "delay", "chunks", "error"}
        if unknown:
            raise ValueError(f"answer {name}: unknown fields {sorted(unknown)}")
        return cls(name=name, **data)

    def matches(self, user: str) -> bool:
        return bool(self.when) and re.search(self.when, user) is not None


@dataclass
class ModelStandIn:
    """The model of every agent of a test: answers from a script, records every call. Thread-safe: the server
    may run in a thread of its own while the test changes the script."""

    default: Answer = field(default_factory=Answer)
    answers: dict[str, Answer] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._script = (self.default, dict(self.answers))   # what reset() goes back to
        self._extra: list[Answer] = []                       # a test's own, before the script's
        self._forced: list[Answer] = []                      # by name, for the next runs in turn

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ModelStandIn":
        """From test data: ``{default: {text, ...}, answers: {name: {when, text, delay, chunks, error}}}``."""
        answers = {name: Answer.of(name, dict(entry or {})) for name, entry in (data.get("answers") or {}).items()}
        return cls(default=Answer.of("default", dict(data.get("default") or {})), answers=answers)

    # ── what a test does ─────────────────────────────────────────────────

    def use(self, name: str, *, times: int = 1) -> Answer:
        """The answer ``name`` of the script for the next ``times`` runs, whatever the user writes."""
        with self._lock:
            answer = self.answers.get(name) or (self.default if name == "default" else None)
            if answer is None:
                raise KeyError(f"the script has no answer {name!r}: {sorted(self.answers)}")
            self._forced.extend([answer] * times)
            return answer

    def when(self, pattern: str, **fields: Any) -> Answer:
        """An answer of this test's own, looked at before the script's: to the user's ``pattern``."""
        answer = Answer.of(f"when {pattern}", {"when": pattern, **fields})
        with self._lock:
            self._extra.insert(0, answer)
        return answer

    def reset(self) -> None:
        """Back to the script of the test data, the calls forgotten: before every test."""
        with self._lock:
            self.default, answers = self._script
            self.answers = dict(answers)
            self._extra.clear()
            self._forced.clear()
            self.calls.clear()

    # ── what the agent's client asks ─────────────────────────────────────

    def choose(self, user: str) -> Answer:
        with self._lock:
            if self._forced:
                return self._forced.pop(0)
            for answer in (*self._extra, *self.answers.values()):
                if answer.matches(user):
                    return answer
            return self.default

    def record(self, call: dict[str, Any]) -> None:
        with self._lock:
            self.calls.append(call)

    def client(self, cfg: Any = None) -> "StandInClient":
        """A client of the model for one resolved model config (registry.build_client's place)."""
        return StandInClient(self, provider=getattr(cfg, "provider", None) or "stand_in",
                             model=getattr(cfg, "model", None) or "stand-in",
                             rate_limit_retry_seconds=getattr(cfg, "rate_limit_retry_seconds", 0.0),
                             prompt_cache_ttl_minutes=getattr(cfg, "prompt_cache_ttl_minutes", None))


def _text(content: Any) -> str:
    """The text of a message's content: a string, or the text parts of a multimodal list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") if isinstance(part, dict) else str(getattr(part, "text", "") or "")
                        for part in content)
    return "" if content is None else str(content)


def _role(message: Any) -> str:
    return message.get("role", "") if isinstance(message, dict) else str(getattr(message, "role", "") or "")


def _content(message: Any) -> Any:
    return message.get("content") if isinstance(message, dict) else getattr(message, "content", None)


def last_user_text(messages: Any) -> str:
    """What the user wrote last in ``messages`` (ChatMessage objects or dicts)."""
    for message in reversed(list(messages or [])):
        if _role(message) == "user":
            return _text(_content(message))
    return ""


class StandInClient:
    """What the agent holds as its LLM client: the surface the step loop uses (llm_loop/llm_call.py) --
    streaming, the final assistant message, usage --, answered from the ModelStandIn it belongs to."""

    def __init__(self, stand_in: ModelStandIn, *, provider: str, model: str, rate_limit_retry_seconds: float = 0.0,
                 prompt_cache_ttl_minutes: Optional[float] = None) -> None:
        self.stand_in = stand_in
        self.provider = provider
        self.model = model
        self.rate_limit_retry_seconds = rate_limit_retry_seconds
        self.prompt_cache_ttl_minutes = prompt_cache_ttl_minutes
        self.profile_name = ""

    def supports_streaming(self) -> bool:
        return True

    def supports_response_format(self, response_format: Any) -> bool:
        return False

    def set_app_title(self, title: str) -> None:
        pass

    async def _answer(self, messages: Any, tools: Any, cancellation_token: Any) -> Optional[Answer]:
        user = last_user_text(messages)
        answer = self.stand_in.choose(user)
        self.stand_in.record({"user": user, "answer": answer.name, "model": self.model,
                              "tools": len(tools or []), "messages": len(list(messages or []))})
        waited = 0.0
        while waited < answer.delay:   # a cancel ends the wait: Stop in the chat
            if cancellation_token is not None and getattr(cancellation_token, "is_cancelled", False):
                return None
            await asyncio.sleep(CANCEL_POLL)
            waited += CANCEL_POLL
        if answer.error:
            raise RuntimeError(answer.error)
        return answer

    async def chat_tools_streaming(self, messages, tools=None, cancellation_token=None, status_scope=None, **kwargs):
        answer = await self._answer(messages, tools, cancellation_token)
        if answer is None:
            return
        size = max(1, -(-len(answer.text) // max(1, answer.chunks)))
        sent = ""
        for start in range(0, len(answer.text), size):
            delta = answer.text[start:start + size]
            sent += delta
            yield {"type": "content_delta", "delta": delta, "accumulated": sent}
        yield {"type": "final", "assistant": {"role": "assistant", "content": answer.text},
               "usage": {"prompt_tokens": 0, "completion_tokens": len(answer.text.split())}, "finish_reason": "stop"}

    async def chat_tools(self, messages, tools=None, cancellation_token=None, status_scope=None, **kwargs):
        answer = await self._answer(messages, tools, cancellation_token)
        return {"assistant": {"role": "assistant", "content": answer.text if answer else ""}, "finish_reason": "stop"}

    async def chat(self, messages, cancellation_token=None, **kwargs) -> str:
        answer = await self._answer(messages, None, cancellation_token)
        return answer.text if answer else ""


def stand_in_for_model(monkeypatch, stand_in: ModelStandIn) -> ModelStandIn:
    """Every LLM client built from now on is ``stand_in``'s (llm/registry.build_client); clients built before keep
    theirs -- install it before the app is built."""
    from agent_system.llm import registry

    monkeypatch.setattr(registry, "build_client", lambda cfg, ssl_verify=None, **_: stand_in.client(cfg))
    return stand_in


__all__ = ["Answer", "ModelStandIn", "StandInClient", "last_user_text", "stand_in_for_agent", "stand_in_for_model"]
