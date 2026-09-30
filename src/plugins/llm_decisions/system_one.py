"""Client for the System One wire -- a questionnaire, not a conversation.

A decision model (TypeSafe's Jev is the first) answers NAMED QUESTIONS about a
piece of content and returns typed answers with probabilities. There is no
message list and no generated prose, which is why this is not an ``LLMClient``:
OpenRouter says so themselves on the model page -- "chat completions SDKs will
not work with it". The TTS clients live beside the chat clients for the same
reason, with their own contract (``agent_system/llm/tts.py``). Why this lives
in its own package and not in ``llm_openrouter`` or ``llm_openai_compat``:
see the comment in plugin.toml beside this file.

The wire is TypeSafe's ("System One"). Two of its hosts were measured with
one questionnaire (2026-09-25); TypeSafe's own is taken from its API
reference -- there was no key here to call it::

    POST <url>
    {"model": ..., "state": <str|dict|list>, "questions": {<name>: {...}}}
    -> {"model", "answers": {<name>: {...}}, "usage": {...}}   (+ "id", "provider")

    OpenRouter  /api/alpha/decisions and /api/v1/systemone: the same body back
                from both, ``id``, ``provider`` and ``usage.cost`` included
    TypeSafe    https://api.typesafe.ai/v1/systemone -- per their reference:
                model, answers, usage{input_tokens, output_tokens}, no cost
    laya-serve  /v1/systemone on a local Laya (Apache-2.0 weights): the same
                answers plus fields of its own this client leaves alone
                (answer_confidence, action, routing); ``model`` is always
                "laya-rl-agent", and ``usage`` carries no cost
    Ollama      /v1/systemone from 0.35 on (nimble, tev1) -- per its API
                reference: model as asked, answers, usage{input_tokens,
                output_tokens}; no id, provider or cost; bodies up to 64 KiB

So one client, and what differs per host is data (``Host``): the provider name
the hooks and the tracker book a call under, the default endpoint, and whether
the host takes OpenRouter's ``session_id``.

Three question types, and their ``criteria`` differ in SHAPE -- the one thing
that turns into an HTTP 400 at runtime, so ``check_questions`` refuses it here:

    noul    a probability answer; criteria {"true": ..., "false": ...} (optional)
    choice  one named option; criteria {option: what it means}
    score   a point on an ordered scale; criteria [lowest, ..., highest]

The endpoint is NOT under ``/api/v1`` at OpenRouter, and laya-serve lives
wherever it was started, so this client takes a full URL rather than a
base_url. ``/api/alpha/`` also says what it is: the shape may change, and it is
pinned in one place here.

Where the answer carries a ``cost`` it is the price -- no entry in
llm_pricing.yaml is needed or would be used. Where it carries none, the cost is
None: unknown, not free.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

import httpx

from agent_system.llm import hook_notify
from agent_system.llm.tls import httpx_verify
from plugins.llm_common import cancellation
from plugins.llm_common.api_keys import resolve_api_key
from plugins.llm_common.http_status import RETRYABLE_STATUS

logger = logging.getLogger(__name__)

#: What a question's ``type`` may be, and which field of the answer decides.
_DECIDING_FIELD = {"noul": "noul", "choice": "choice", "score": "score"}

State = Union[str, Mapping[str, Any], Sequence[Any]]

#: The longest Retry-After this client sits out. A host asking for more gets no
#: further attempt: the call fails at once and says how long it was asked to wait.
MAX_RETRY_AFTER = 60.0


def _retry_after(response: httpx.Response) -> Optional[float]:
    """The host's Retry-After in seconds; None when absent or no duration."""
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:  # absent, or an HTTP date
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


@dataclass(frozen=True)
class Host:
    """What one host of the wire needs that the others do not.

    ``provider`` is the name in the manifest's ``provides_decisions``, and the
    name the hooks, the debugger and the usage tracker book a call under -- a
    local Laya booked as OpenRouter would be spend that happened elsewhere.
    ``url`` is the whole default endpoint, nothing is appended to it.
    ``takes_session_id``: OpenRouter documents ``session_id`` for grouping its
    logs; TypeSafe's reference lists model, state and questions only, and a
    field a host does not document is not sent there.
    """

    provider: str
    url: str
    takes_session_id: bool


OPENROUTER = Host("openrouter_decisions", "https://openrouter.ai/api/alpha/decisions", True)
#: TypeSafe's own endpoint, and the wire laya-serve speaks: a local Laya is this
#: host with the url of the machine it runs on.
SYSTEM_ONE = Host("systemone_decisions", "https://api.typesafe.ai/v1/systemone", False)
#: Ollama 0.35+ serves its decision models (nimble, tev1) on this wire; the
#: default port and address are llm_ollama's. Its reference lists model, state,
#: questions and keep_alive -- no session_id.
OLLAMA = Host("ollama_decisions", "http://127.0.0.1:11434/v1/systemone", False)


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
    """The endpoint refused the request, could not be reached, or answered with
    something this client cannot use.

    ``usage`` is set in that last case when the answer carried one -- the call
    was billed all the same, and a caller adding up spend must count it:
    ``{"input_tokens", "output_tokens", "cost"}``, cost None when unreported.
    """

    def __init__(self, message: str, usage: Optional[dict] = None) -> None:
        super().__init__(message)
        self.usage = usage


class DecisionsClient:
    """One decision call, with the retries and the cancel the other clients have."""

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        request_timeout: int = 60,
        max_retries: int = 2,
        host: Host = OPENROUTER,
    ) -> None:
        self.host = host
        self.url = url or host.url
        # The key follows the ENDPOINT, not the provider name (api_keys.py): it is
        # matched against the host of the url actually called, so pointing this
        # client at a proxy does NOT send the OpenRouter secret there. A LOCAL
        # endpoint (a host without a dot, a private address) gets no key at all
        # unless the config names one: api_keys.py would hand it OPENAI_API_KEY,
        # which suits a local OpenAI-compatible server and not this wire. A
        # local laya-serve takes any request unless it runs with LAYA_API_KEY,
        # and then answers 401 -- the config's api_key is the place for it.
        self.api_key, _ = resolve_api_key(api_key, self.url, default_base_url=host.url,
                                          provider=host.provider, local_fallback=False)
        self.model = model
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
        a diff) or a JSON object when the parts have names. ``session_id``
        groups the call in the hooks, and at OpenRouter also in their own
        logging; it never reaches the model.
        """
        self.check_questions(questions)
        if isinstance(state, (bytes, bytearray)) or not isinstance(state, (str, Mapping, Sequence)):
            raise ValueError(
                f"A decisions state is text, an object or an array -- not {type(state).__name__} "
                f"(the endpoint answers 400, and the number that reaches it is nobody's content)")
        if len(state) == 0:
            raise ValueError("A decisions request needs a state -- the content to judge")

        payload: dict[str, Any] = {"model": self.model, "state": state, "questions": dict(questions)}
        if session_id and self.host.takes_session_id:
            payload["session_id"] = session_id
        headers = {"Content-Type": "application/json"}
        if self.api_key:  # a local endpoint without a configured key gets none
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.time()
        # Decision calls never pass through the per-agent hook wiring, so they
        # dispatch to the global registry themselves -- exactly as the TTS
        # clients do, and for the same reason: without it the debugger and the
        # latency capture are blind to a whole class of calls.
        await _notify_request(provider=self.host.provider, model=self.model, url=self.url,
                              payload=payload, session_id=session_id)
        try:
            return await self._attempts(payload, headers, questions, started, cancellation_token, session_id)
        except BaseException as e:
            # EVERY exit says how it ended, the user's cancel included: a request
            # the debugger never sees an answer to is how a whole class of calls
            # went missing once before (agent_system/llm/tts.py).
            # A refused answer that said what it cost goes out WITH that usage:
            # the tracker books an error row only when it was billed.
            await _notify_response(provider=self.host.provider, model=self.model, url=self.url,
                                   session_id=session_id, duration_ms=(time.time() - started) * 1000,
                                   usage=getattr(e, "usage", None),
                                   error=f"{type(e).__name__}: {e}", finish_reason="error")
            raise

    async def _attempts(self, payload: dict, headers: dict, questions: Mapping[str, Mapping[str, Any]],
                        started: float, cancellation_token: Any, session_id: Optional[str]) -> DecisionsResult:
        """The retry loop. Everything it raises is reported by its caller."""
        last_error: Optional[Exception] = None
        for attempt in range(max(0, self.max_retries) + 1):
            asked: Optional[float] = None
            try:
                response = await self._post(payload, headers, cancellation_token)
                if response.status_code in RETRYABLE_STATUS:
                    last_error = DecisionsError(f"HTTP {response.status_code}: {response.text[:300]}")
                    asked = _retry_after(response)
                    if asked is not None and asked > MAX_RETRY_AFTER:
                        raise DecisionsError(f"Decisions API busy (HTTP {response.status_code}), asks to retry "
                                             f"after {asked:.0f}s -- model={self.model}")
                elif response.status_code >= 400:
                    raise DecisionsError(f"Decisions API error {response.status_code}: "
                                         f"{response.text[:500]} -- model={self.model}")
                else:
                    result = self._to_result(response, started, questions)
                    await _notify_response(
                        provider=self.host.provider, model=self.model, url=self.url, session_id=session_id,
                        duration_ms=result.duration_ms, served_by=result.model,
                        usage={"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                               "cost": result.cost},
                        data={"answers": {n: a.value for n, a in result.answers.items()}})
                    return result
            except httpx.TransportError as e:  # includes TimeoutException
                last_error = e
            if attempt < self.max_retries:
                delay = asked if asked is not None else 2.0 * (attempt + 1)
                logger.warning("Decisions attempt %d/%d failed (%s) -- retrying in %.0fs",
                               attempt + 1, self.max_retries + 1, last_error, delay)
                await _notify_response(
                    provider=self.host.provider, model=self.model, url=self.url, session_id=session_id,
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
        usage = data.get("usage") or {}
        try:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cost = None if usage.get("cost") is None else float(usage["cost"])
        except (AttributeError, TypeError, ValueError) as e:
            raise DecisionsError(f"Decisions API answered a usage this client cannot read "
                                 f"({str(usage)[:200]}) -- model={self.model}") from e
        # An answer refused below was billed all the same: the error carries what
        # it cost, when the answer said so at all.
        billed = ({"input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost}
                  if usage else None)
        answers = {}
        received = data.get("answers") or {}
        if not isinstance(received, Mapping):
            raise DecisionsError(f"Decisions API answered {type(received).__name__} where the answers object "
                                 f"belongs ({str(received)[:200]}) -- model={self.model}", usage=billed)
        for name, answer in received.items():
            kind = answer.get("type") if isinstance(answer, Mapping) else None
            field = _DECIDING_FIELD.get(kind)
            # `field not in answer` would let a present-but-null value through, and
            # a None reaches the caller's threshold as a TypeError far from here.
            if field is None or answer.get(field) is None:
                # A type this client does not know, or the deciding field
                # missing: reported, never guessed. A silently dropped answer
                # would read as "the model did not answer that question".
                raise DecisionsError(
                    f"Decisions answer {name!r} has type {kind!r} with no usable value ({str(answer)[:200]}) "
                    f"-- this client knows {', '.join(sorted(_DECIDING_FIELD))}", usage=billed)
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
                f"(answered: {', '.join(sorted(answers)) or 'nothing'}) -- model={self.model}", usage=billed)
        return DecisionsResult(
            answers=answers,
            # The served model, not the one asked for: an alias (~typesafe/jev-latest)
            # resolves to a dated version, and that is what the cost belongs to.
            model=data.get("model") or self.model,
            provider=data.get("provider"),
            id=data.get("id"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            duration_ms=(time.time() - started) * 1000,
        )

# --------------------------------------------------------------------- hooks
# The dispatch lives in agent_system/llm/hook_notify.py, shared with the TTS
# clients: the same "no agent around this call, so tell the registry yourself"
# problem, and the same once-per-phase warning when that dispatch is dead.
# What stays here is this API's vocabulary -- answers, a served model, a token
# usage -- which is exactly the half that could NOT be shared.


async def _notify_request(*, provider: str, model: str, url: str, payload: dict,
                          session_id: Optional[str] = None) -> None:
    await hook_notify.notify_request(
        provider=provider, model=model, url=url, payload=payload,
        session_id=session_id or "")


async def _notify_response(*, provider: str, model: str, url: str, duration_ms: float,
                           data: Optional[dict] = None, usage: Optional[dict] = None,
                           served_by: Optional[str] = None, session_id: Optional[str] = None,
                           error: Optional[str] = None, finish_reason: Optional[str] = None) -> None:
    await hook_notify.notify_response(
        provider=provider, model=model, url=url, duration_ms=duration_ms,
        response_data=data, usage=usage, session_id=session_id or "",
        error=error, finish_reason=finish_reason,
        # where the debugger reads the backend a gateway routed to
        metadata={"served_by": served_by})
