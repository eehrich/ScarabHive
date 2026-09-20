"""Client for OpenRouter's Decisions API -- a questionnaire, not a conversation.

A decision model (TypeSafe's Jev is the first) answers NAMED QUESTIONS about a
piece of content and returns typed answers with probabilities. There is no
message list and no generated prose, which is why this is not an ``LLMClient``:
OpenRouter says so themselves on the model page -- "chat completions SDKs will
not work with it". The TTS clients live beside the chat clients for the same
reason, with their own contract (``agent_system/llm/tts.py``). Why this lives
in its own package and not in ``llm_openrouter`` or ``llm_openai_compat``:
see the README beside this file.

The wire, measured against OpenRouter's OpenAPI document (2026-09-20)::

    POST https://openrouter.ai/api/alpha/decisions
    {"model": ..., "state": <str|dict|list>, "questions": {<name>: {...}}}
    -> {"id", "model", "provider", "answers": {<name>: {...}}, "usage": {...}}

Three question types, and their ``criteria`` differ in SHAPE -- the one thing
that turns into an HTTP 400 at runtime, so ``check_questions`` refuses it here:

    noul    a probability answer; criteria {"true": ..., "false": ...} (optional)
    choice  one named option; criteria {option: what it means}
    score   a point on an ordered scale; criteria [lowest, ..., highest]

The endpoint is NOT under ``/api/v1`` like everything else at OpenRouter, so
this client takes a full URL rather than a base_url. ``/api/alpha/`` also says
what it is: the shape may change, and it is pinned in one place here.

The answer carries its own ``cost`` -- no entry in llm_pricing.yaml is needed
or would be used.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

import httpx

from agent_system.llm import hook_notify
from agent_system.llm.tls import httpx_verify
from plugins.llm_common import cancellation
from plugins.llm_common.api_keys import resolve_api_key

logger = logging.getLogger(__name__)

#: The full endpoint: decisions do not live under /api/v1 (see the module docstring).
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
#: What a question's ``type`` may be, and which field of the answer decides.
_DECIDING_FIELD = {"noul": "noul", "choice": "choice", "score": "score"}
#: What the hooks see as the provider of these calls; also the name the
#: API key is resolved under.
_PROVIDER = "openrouter_decisions"

State = Union[str, Mapping[str, Any], Sequence[Any]]


@dataclass(frozen=True)
class Answer:
    """One question's answer.

    ``value`` is the field that decides: the probability for ``noul``, the
    chosen option for ``choice``, the point on the scale for ``score``. The
    rest is there when the model sends it -- ``confidence`` and
    ``probabilities`` are optional in the schema, so a caller that needs them
    must handle None rather than assume.
    """

    name: str
    type: str
    value: Union[float, str]
    confidence: Optional[float] = None
    probabilities: Optional[dict] = None
    legend: Optional[dict] = None


@dataclass(frozen=True)
class DecisionsResult:
    answers: dict[str, Answer]
    model: str
    provider: Optional[str]
    id: Optional[str]
    input_tokens: int
    output_tokens: int
    #: What the call cost, as the API reports it. None when the answer carried no
    #: cost at all -- which is not the same as free, and must not be added up as 0.
    cost: Optional[float]
    duration_ms: float

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]


class DecisionsError(RuntimeError):
    """The endpoint refused the request or could not be reached."""


class DecisionsClient:
    """One decision call, with the retries and the cancel the other clients have."""

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        url: str = DECISIONS_URL,
        request_timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        # The key follows the ENDPOINT, not the provider name (api_keys.py): it is
        # matched against the host of the url actually called, so pointing this
        # client at a proxy does NOT send the OpenRouter secret there. One gap,
        # and it is api_keys.py's deliberate one, not this client's: a host
        # WITHOUT A DOT counts as local (`_is_local`), and a local host gets
        # OPENAI_API_KEY. So `http://decisions-proxy:9000/...` -- a compose
        # service name -- travels with that key. Give such an endpoint its own
        # api_key in the config rather than relying on the fallback.
        self.api_key, _ = resolve_api_key(api_key, url, default_base_url=DECISIONS_URL,
                                          provider=_PROVIDER)
        self.model = model
        self.url = url
        self.request_timeout = request_timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------ checks

    @staticmethod
    def check_questions(questions: Mapping[str, Mapping[str, Any]]) -> None:
        """Refuse a questionnaire before it is sent.

        Two kinds of rule, and only the first is the endpoint's: ``type``,
        ``instructions`` and (for choice and score) ``criteria`` are required by
        its schema, and the SHAPE of criteria differs per type. The minimum of
        two options is this client's own -- a choice or a scale with one answer
        is a configuration mistake, not a question. Measured live: all three
        types answer, and criteria really is optional for noul. NOT measured:
        whether the endpoint itself would refuse a one-option choice, so that
        rule stays ours, in this one place, where it can be dropped in a line.

        Called by ``decide`` before the request, and available on its own for a
        caller that wants its configuration checked once at startup rather than
        at the first decision.
        """
        if not questions:
            raise ValueError("A decisions request needs at least one question")
        for name, question in questions.items():
            if not isinstance(question, Mapping):
                raise ValueError(f"Question {name!r} must be a mapping, not {type(question).__name__}")
            kind = question.get("type")
            if kind not in _DECIDING_FIELD:
                raise ValueError(
                    f"Question {name!r} has type {kind!r}; the endpoint knows "
                    f"{', '.join(sorted(_DECIDING_FIELD))}")
            if not question.get("instructions"):
                raise ValueError(f"Question {name!r} has no instructions -- the model is told nothing to decide")
            criteria = question.get("criteria")
            if kind == "score":
                if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)) or len(criteria) < 2:
                    raise ValueError(
                        f"Question {name!r} is a score: criteria must be an ordered list of at least two "
                        f"levels (lowest first), not {type(criteria).__name__}")
            elif kind == "choice":
                if not isinstance(criteria, Mapping) or len(criteria) < 2:
                    raise ValueError(
                        f"Question {name!r} is a choice: criteria must be a mapping of at least two "
                        f"options to what each one means, not {type(criteria).__name__}")
            elif criteria is not None and not isinstance(criteria, Mapping):
                raise ValueError(
                    f"Question {name!r} is a noul: criteria is optional, but when given it is a "
                    f"mapping (OpenRouter's example names 'true' and 'false'), not "
                    f"{type(criteria).__name__}")

    # ------------------------------------------------------------------- call

    async def decide(
        self,
        state: State,
        questions: Mapping[str, Mapping[str, Any]],
        *,
        cancellation_token: Any = None,
        session_id: Optional[str] = None,
    ) -> DecisionsResult:
        """Answer ``questions`` about ``state``.

        ``state`` is the content to judge: a plain string (a command, a ticket,
        a diff) or a JSON object when the parts have names. ``session_id`` only
        groups requests in OpenRouter's own logging; it never reaches the model.
        """
        self.check_questions(questions)
        if isinstance(state, (bytes, bytearray)) or not isinstance(state, (str, Mapping, Sequence)):
            raise ValueError(
                f"A decisions state is text, an object or an array -- not {type(state).__name__} "
                f"(the endpoint answers 400, and the number that reaches it is nobody's content)")
        if len(state) == 0:
            raise ValueError("A decisions request needs a state -- the content to judge")

        payload: dict[str, Any] = {"model": self.model, "state": state, "questions": dict(questions)}
        if session_id:
            payload["session_id"] = session_id
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        started = time.time()
        # Decision calls never pass through the per-agent hook wiring, so they
        # dispatch to the global registry themselves -- exactly as the TTS
        # clients do, and for the same reason: without it the debugger and the
        # latency capture are blind to a whole class of calls.
        await _notify_request(model=self.model, url=self.url, payload=payload, session_id=session_id)
        try:
            return await self._attempts(payload, headers, questions, started, cancellation_token, session_id)
        except BaseException as e:
            # EVERY exit says how it ended, the user's cancel included: a request
            # the debugger never sees an answer to is how a whole class of calls
            # went missing once before (agent_system/llm/tts.py).
            await _notify_response(model=self.model, url=self.url, session_id=session_id,
                                   duration_ms=(time.time() - started) * 1000,
                                   error=f"{type(e).__name__}: {e}", finish_reason="error")
            raise

    async def _attempts(self, payload: dict, headers: dict, questions: Mapping[str, Mapping[str, Any]],
                        started: float, cancellation_token: Any, session_id: Optional[str]) -> DecisionsResult:
        """The retry loop. Everything it raises is reported by its caller."""
        last_error: Optional[Exception] = None
        for attempt in range(max(0, self.max_retries) + 1):
            try:
                response = await self._post(payload, headers, cancellation_token)
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = DecisionsError(f"HTTP {response.status_code}: {response.text[:300]}")
                elif response.status_code >= 400:
                    raise DecisionsError(f"Decisions API error {response.status_code}: "
                                         f"{response.text[:500]} -- model={self.model}")
                else:
                    result = self._to_result(response, started, questions)
                    await _notify_response(
                        model=self.model, url=self.url, session_id=session_id,
                        duration_ms=result.duration_ms, served_by=result.model,
                        usage={"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                               "cost": result.cost},
                        data={"answers": {n: a.value for n, a in result.answers.items()}})
                    return result
            except httpx.TransportError as e:  # includes TimeoutException
                last_error = e
            if attempt < self.max_retries:
                delay = 2.0 * (attempt + 1)
                logger.warning("Decisions attempt %d/%d failed (%s) -- retrying in %.0fs",
                               attempt + 1, self.max_retries + 1, last_error, delay)
                await _notify_response(
                    model=self.model, url=self.url, session_id=session_id,
                    duration_ms=(time.time() - started) * 1000,
                    error=f"[RETRY {attempt + 1}/{self.max_retries + 1}] "
                          f"{type(last_error).__name__}: {last_error}", finish_reason="retry")
                # the wait is watched too: a cancel during the backoff would
                # otherwise sit out the full delay before anyone notices
                await cancellation.await_call(asyncio.ensure_future(asyncio.sleep(delay)), cancellation_token)
        raise DecisionsError(f"Decisions call failed: {type(last_error).__name__}: {last_error} "
                             f"-- model={self.model}")

    async def _post(self, payload: dict, headers: dict, cancellation_token: Any) -> httpx.Response:
        """The request, endable by the user's cancel while it is in flight."""
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.request_timeout, verify=httpx_verify()) as client:
                return await client.post(self.url, json=payload, headers=headers)

        if cancellation_token is not None and getattr(cancellation_token, "is_cancelled", False):
            raise asyncio.CancelledError("Request cancelled by user")
        return await cancellation.await_call(asyncio.ensure_future(send()), cancellation_token)

    # ----------------------------------------------------------------- answers

    def _to_result(self, response: httpx.Response, started: float,
                   questions: Mapping[str, Mapping[str, Any]]) -> DecisionsResult:
        try:
            data = response.json()
        except ValueError as e:
            raise DecisionsError(
                f"Decisions API answered {response.status_code} with no JSON "
                f"({response.text[:200]!r}) -- model={self.model}") from e
        if not isinstance(data, dict):
            # JSON, but not an object: `null` and `[]` parse fine and would only
            # fail further down, where the message points at a NoneType.
            raise DecisionsError(
                f"Decisions API answered {response.status_code} with {type(data).__name__}, not an object "
                f"({str(data)[:200]}) -- model={self.model}")
        answers = {}
        for name, answer in (data.get("answers") or {}).items():
            kind = answer.get("type")
            field = _DECIDING_FIELD.get(kind)
            # `field not in answer` would let a present-but-null value through, and
            # a None reaches the caller's threshold as a TypeError far from here.
            if field is None or answer.get(field) is None:
                # A type this client does not know, or the deciding field
                # missing: reported, never guessed. A silently dropped answer
                # would read as "the model did not answer that question".
                raise DecisionsError(
                    f"Decisions answer {name!r} has type {kind!r} with no usable value ({answer!r}) "
                    f"-- this client knows {', '.join(sorted(_DECIDING_FIELD))}")
            answers[name] = Answer(
                name=name, type=kind, value=answer[field],
                confidence=answer.get("confidence"),
                probabilities=answer.get("probabilities"),
                legend=answer.get("legend"),
            )
        unanswered = [name for name in questions if name not in answers]
        if unanswered:
            # Silence is the one thing a decision may not be: a caller reading
            # result["urgency"] would get a bare KeyError, naming neither the
            # model nor what it did answer.
            raise DecisionsError(
                f"Decisions API left {', '.join(sorted(unanswered))} unanswered "
                f"(answered: {', '.join(sorted(answers)) or 'nothing'}) -- model={self.model}")
        usage = data.get("usage") or {}
        return DecisionsResult(
            answers=answers,
            # The served model, not the one asked for: an alias (~typesafe/jev-latest)
            # resolves to a dated version, and that is what the cost belongs to.
            model=data.get("model") or self.model,
            provider=data.get("provider"),
            id=data.get("id"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost=None if usage.get("cost") is None else float(usage["cost"]),
            duration_ms=(time.time() - started) * 1000,
        )

# --------------------------------------------------------------------- hooks
# The dispatch lives in agent_system/llm/hook_notify.py, shared with the TTS
# clients: the same "no agent around this call, so tell the registry yourself"
# problem, and the same once-per-phase warning when that dispatch is dead.
# What stays here is this API's vocabulary -- answers, a served model, a token
# usage -- which is exactly the half that could NOT be shared.


async def _notify_request(*, model: str, url: str, payload: dict,
                          session_id: Optional[str] = None) -> None:
    await hook_notify.notify_request(
        provider=_PROVIDER, model=model, url=url, payload=payload,
        session_id=session_id or "")


async def _notify_response(*, model: str, url: str, duration_ms: float, data: Optional[dict] = None,
                           usage: Optional[dict] = None, served_by: Optional[str] = None,
                           session_id: Optional[str] = None,
                           error: Optional[str] = None, finish_reason: Optional[str] = None) -> None:
    await hook_notify.notify_response(
        provider=_PROVIDER, model=model, url=url, duration_ms=duration_ms,
        response_data=data, usage=usage, session_id=session_id or "",
        error=error, finish_reason=finish_reason,
        # where the debugger reads the backend a gateway routed to
        metadata={"served_by": served_by})
