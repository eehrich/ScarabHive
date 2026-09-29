"""Structured output: the final answer as JSON that matches a schema (F11).

One provider-neutral request, ``ResponseFormat``, which every client that can
maps to the field its API has for it -- ``response_format`` (Chat Completions),
``text.format`` (Responses), ``output_config.format`` (Anthropic),
``generationConfig.responseJsonSchema`` (Gemini), ``format`` (Ollama).

Two statements have to say yes before a client puts that field on the wire:

- the ROUTE: ``LLMClient.response_format_kinds``, the kinds its API has a
  field for -- a fact about the wire format, the same for every model on it;
- the MODEL: ``capabilities.structured_output``, declared on the model entry
  (for a schema AND for plain JSON mode: ``json_mode`` is not read -- its
  values in the catalogue were never verified), because one route reaches
  models that differ here (an OpenAI-compatible endpoint may be llama.cpp,
  OpenRouter has it per model).

Neither is a table of provider names. What neither covers is never dropped
silently: ``require_response_format`` raises ``StructuredOutputUnsupported``,
and a caller that asked for ``prompt_fallback`` gets the schema as an
instruction instead (``instruction_text``). The answer is checked either way
-- a provider's guarantee is not taken on trust, and the fallback has no other.

The checks of a schema and of an answer against it run in a process of their
own (``schema_worker``, started and bounded by ``SchemaWorkerPool``): the
schema is a client's input, and jsonschema and regex are not built to be
bounded. This process never runs them on it. ``prepare_response_format`` turns
a format into one whose schema passed the strict subset; ``check_answer``
checks an answer.

The core stays free of provider knowledge: the wire shapes of the OpenAI
family live in ``plugins/llm_common/structured_output.py``, the others with
their clients.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import schema_worker

logger = logging.getLogger(__name__)

JSON_SCHEMA = "json_schema"
JSON_OBJECT = "json_object"
RESPONSE_FORMAT_KINDS = (JSON_SCHEMA, JSON_OBJECT)

#: The capability flag a model entry declares for each kind. The same for both: a model that takes
#: a schema takes plain JSON mode too, and ``json_mode`` is a catalogue value nobody verified.
CAPABILITY_FLAGS = {JSON_SCHEMA: "structured_output", JSON_OBJECT: "structured_output"}

#: ``error_type`` of a structured run whose step LLM cannot take its response_format, when the
#: caller did not allow the prompt fallback. No LLM was called: the run's own model is asked before
#: the first step (a later switch of model to one that cannot take it is refused as a guard).
STRUCTURED_OUTPUT_UNSUPPORTED = "structured_output_unsupported"
#: ``error_type`` of a structured run whose final answer did not match its format -- after the one
#: correction the run asks for, on the final call, or when the answer could not be checked at all --
#: and of one whose format itself is not one (a schema outside the subset).
STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
#: ``error_type`` of a structured run whose answer (or format) could not be checked because the checker
#: itself was not there -- busy past its queue time, crashed, not started. No verdict on the answer: the
#: same request may well go through later (openai_api answers a retryable 503).
STRUCTURED_OUTPUT_UNAVAILABLE = "structured_output_unavailable"

#: Seconds the worker gets for checking a schema, and for checking an answer against one. Past them
#: it is killed, and the request fails closed.
PREPARE_DEADLINE = 5.0
CHECK_DEADLINE = 5.0
#: How many workers (processes) run at once; more requests wait. Bounds their memory together.
WORKER_COUNT = 2
#: Seconds a request waits -- behind its user's earlier requests, then for a free worker -- before it
#: fails as "busy" (not as the schema's fault).
QUEUE_DEADLINE = 30.0
#: How many of one user's requests run in workers at once. One: a user's flood waits in that user's own
#: queue and holds at most one worker, the others stay free for everyone else. Waiting, not refusing: a
#: user's two runs whose answers are checked at the same moment must both go through.
WORKERS_PER_USER = 1
#: Seconds an idle worker waits for its next request before it exits (the pool of a loop that is gone).
WORKER_IDLE_SECONDS = 300.0
_WORKER_PATH = Path(schema_worker.__file__).resolve()
#: One reply line may carry an answer of MAX_ANSWER_CHARS, JSON-escaped.
_LINE_LIMIT = 16 * 1024 * 1024


class InvalidResponseFormat(ValueError):
    """A response format that cannot be asked for: an unknown kind, a missing or broken schema, a bad name.

    ``field`` names what is wrong (type, name, strict, description, schema), for a caller that
    answers in a wire format with a parameter path (openai_api).
    """

    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class StructuredOutputUnsupported(Exception):
    """A model was asked for structured output that neither its route nor its model entry says it can give.

    Raised instead of sending the request without the field: an answer that merely looks like JSON
    must not pass for one the provider constrained.
    """

    def __init__(self, message: str, *, model: Optional[str] = None, kind: Optional[str] = None):
        super().__init__(message)
        self.model = model
        self.kind = kind


class SchemaCheckerError(RuntimeError):
    """The schema worker could not answer: it was killed at its deadline, died, or could not start."""


class SchemaCheckTimeout(SchemaCheckerError):
    """The worker did not answer within its deadline and was killed."""


@dataclass(frozen=True)
class ResponseFormat:
    """What the final answer must be: any JSON object, or JSON that matches ``schema``.

    ``name``, ``strict`` and ``description`` travel where a route has a field for them
    (OpenAI, OpenRouter); ``strict=None`` leaves the route's default. ``prompt_fallback`` is the
    caller's choice, never sent: where the model cannot take the field, describe the format in
    the conversation and validate the answer, instead of refusing the run.

    Building one checks only what is cheap. ``checked`` is set by ``prepare_response_format``: its
    schema passed the worker's subset. The agent prepares a format that is not checked yet before
    its first step.
    """

    type: str = JSON_SCHEMA
    schema: Optional[dict] = None
    name: str = "response"
    strict: Optional[bool] = None
    description: Optional[str] = None
    prompt_fallback: bool = False
    checked: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if self.type not in RESPONSE_FORMAT_KINDS:
            raise InvalidResponseFormat(
                f"type must be one of {list(RESPONSE_FORMAT_KINDS)}, not {self.type!r}", field="type")
        if not _is_name(self.name):
            raise InvalidResponseFormat(
                f"name must be 1-64 characters of a-z, A-Z, 0-9, _ or -, not {self.name!r}", field="name")
        if self.strict is not None and not isinstance(self.strict, bool):
            raise InvalidResponseFormat(f"strict must be true, false or absent, not {self.strict!r}",
                                        field="strict")
        if self.description is not None and not isinstance(self.description, str):
            raise InvalidResponseFormat("description must be a string", field="description")
        if self.type == JSON_OBJECT:
            if self.schema is not None:
                raise InvalidResponseFormat("a json_object format takes no schema -- use json_schema",
                                            field="schema")
            return
        if not isinstance(self.schema, dict):
            raise InvalidResponseFormat("a json_schema format needs its schema as a JSON object", field="schema")
        # Our own copy, as JSON: a caller that edits its dict afterwards must not change a request in
        # flight. Bounded first: the worker refuses a larger one anyway.
        try:
            encoded = json.dumps(self.schema, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as error:
            raise InvalidResponseFormat(f"the schema is not JSON: {error}", field="schema") from None
        if len(encoded) > schema_worker.MAX_SCHEMA_CHARS:
            raise InvalidResponseFormat(f"the schema is {len(encoded)} characters as JSON; at most "
                                        f"{schema_worker.MAX_SCHEMA_CHARS} are taken", field="schema")
        object.__setattr__(self, "schema", json.loads(encoded))


def _is_name(name: Any) -> bool:
    """OpenAI's rule for json_schema.name (^[A-Za-z0-9_-]{1,64}$), and the strictest of the routes."""
    return (isinstance(name, str) and 0 < len(name) <= 64
            and all(char.isascii() and (char.isalnum() or char in "_-") for char in name))


# ------------------------------------------------------------------ capability

def declared_support(capabilities: Any, kind: str) -> bool:
    """Whether a model entry declares the capability for ``kind`` (object, dict or None)."""
    flag = CAPABILITY_FLAGS.get(kind)
    if flag is None or capabilities is None:
        return False
    if isinstance(capabilities, dict):
        return capabilities.get(flag) is True
    return getattr(capabilities, flag, False) is True


def supports_response_format(llm: Any, response_format: ResponseFormat) -> bool:
    """Whether ``llm`` puts ``response_format`` on the wire. False for anything that cannot say.

    Only a client's own ``True`` counts: a stand-in without the method, a mock whose attribute
    answers with another mock or a coroutine, is a client that has not wired the field.
    """
    probe = getattr(llm, "supports_response_format", None)
    if not callable(probe):
        return False
    answer = probe(response_format)
    if inspect.isawaitable(answer):
        close = getattr(answer, "close", None)
        if callable(close):
            close()  # never awaited, and it must not warn about that
        return False
    return answer is True


def unsupported_message(llm: Any, response_format: ResponseFormat) -> str:
    """Why ``llm`` does not take ``response_format``, and what would change that."""
    model = getattr(llm, "model", None) or type(llm).__name__
    kinds = getattr(llm, "response_format_kinds", None)
    flag = CAPABILITY_FLAGS[response_format.type]
    if not isinstance(kinds, (tuple, list, frozenset, set)) or response_format.type not in kinds:
        reason = f"its client ({type(llm).__name__}) has no wire field for {response_format.type}"
    else:
        reason = f"its model entry does not declare capabilities.{flag}: true"
    return (f"Structured output ({response_format.type}) is not supported by model {model!r}: {reason}. "
            f"Ask with prompt_fallback to have the format described in the conversation and the "
            f"answer validated instead.")


def require_response_format(llm: Any, response_format: Optional[ResponseFormat]) -> None:
    """Raise StructuredOutputUnsupported unless ``llm`` puts ``response_format`` on the wire."""
    if response_format is None or supports_response_format(llm, response_format):
        return
    raise StructuredOutputUnsupported(
        unsupported_message(llm, response_format),
        model=getattr(llm, "model", None), kind=response_format.type)


# ------------------------------------------------------------------ the worker

class SchemaWorkerPool:
    """Up to WORKER_COUNT ``schema_worker`` processes, each request under a wall-clock deadline.

    A worker is started on demand and kept for the next request. One that misses its deadline -- or
    whose request is cancelled mid-way -- is killed, by the handle this pool holds and by nothing
    else, and the next request starts a fresh one. One that retires itself (its memory grew) or dies
    is replaced the same way. The semaphore bounds how many run at once, and so their memory together.
    """

    def __init__(self, size: Optional[int] = None):
        self._slots = asyncio.Semaphore(WORKER_COUNT if size is None else size)
        #: user -> [their own semaphore, how many of their requests are here]; gone when none is.
        self._lanes: dict[str, list] = {}
        self._idle: list[asyncio.subprocess.Process] = []
        self._all: set[asyncio.subprocess.Process] = set()
        self.started = 0
        self.killed = 0

    async def _start(self) -> asyncio.subprocess.Process:
        try:
            # -I: no PYTHON* variables, no user site, not the script's directory on sys.path.
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(_WORKER_PATH), str(WORKER_IDLE_SECONDS),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=_LINE_LIMIT)
        except OSError as error:
            raise SchemaCheckerError(f"the schema checker could not start: {error}") from error
        self.started += 1
        self._all.add(process)
        return process

    async def _end(self, process: asyncio.subprocess.Process, *, kill: bool) -> None:
        self._all.discard(process)
        if kill and process.returncode is None:
            self.killed += 1
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # Shielded: a cancelled caller must not leave the process unreaped.
        await asyncio.shield(process.wait())

    async def request(self, payload: dict, deadline: float, owner: Optional[str] = None) -> dict:
        """One request, in its user's lane (``owner``; None is one shared user, as auth-off is): behind that
        user's earlier requests, then for a free worker, QUEUE_DEADLINE in all; then ``deadline`` for the
        worker's answer."""
        until = time.monotonic() + QUEUE_DEADLINE
        key = owner or ""
        lane = self._lanes.setdefault(key, [asyncio.Semaphore(WORKERS_PER_USER), 0])
        lane[1] += 1
        try:
            if not await self._acquire(lane[0], until):
                raise SchemaCheckerError(f"the schema checker is busy with this user's earlier requests "
                                         f"(waited {QUEUE_DEADLINE:g} s)")
            try:
                if not await self._acquire(self._slots, until):
                    raise SchemaCheckerError(f"the schema checker is busy (no worker free within {QUEUE_DEADLINE:g} s)")
                try:
                    return await self._exchange(payload, deadline)
                finally:
                    self._slots.release()
            finally:
                lane[0].release()
        finally:
            lane[1] -= 1
            if lane[1] == 0 and self._lanes.get(key) is lane:
                del self._lanes[key]

    @staticmethod
    async def _acquire(semaphore: asyncio.Semaphore, until: float) -> bool:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=max(0.0, until - time.monotonic()))
            return True
        except asyncio.TimeoutError:
            return False

    def _take_idle(self) -> Optional[asyncio.subprocess.Process]:
        while self._idle:
            process = self._idle.pop()
            if process.returncode is None:
                return process
            self._all.discard(process)  # ended while idle (its idle time ran out, or it was killed)
        return None

    async def _exchange(self, payload: dict, deadline: float) -> dict:
        line = (json.dumps(payload) + "\n").encode()
        # A worker taken from the idle list may have ended since its last answer: a broken pipe there is
        # the worker's, not the request's -- once, the request goes to a fresh one.
        for attempt in (1, 2):
            reused = self._take_idle()
            process = reused or await self._start()

            async def exchange() -> bytes:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(line)
                await process.stdin.drain()
                return await process.stdout.readline()

            try:
                raw = await asyncio.wait_for(exchange(), timeout=deadline)
            except asyncio.TimeoutError:
                await self._end(process, kill=True)
                raise SchemaCheckTimeout(f"the check did not finish within {deadline:g} s") from None
            except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as broken:
                await self._end(process, kill=True)
                if reused is not None and attempt == 1 and isinstance(broken, OSError):
                    continue
                raise SchemaCheckerError(f"the schema checker failed: {type(broken).__name__}: {broken}") from None
            except BaseException:
                # Cancelled: the worker is mid-request and cannot be reused.
                await self._end(process, kill=True)
                raise
            if not raw:
                await self._end(process, kill=True)
                if reused is not None and attempt == 1:
                    continue
                raise SchemaCheckerError("the schema checker ended without an answer")
            try:
                reply = json.loads(raw)
            except ValueError:
                await self._end(process, kill=True)
                raise SchemaCheckerError(f"the schema checker answered no JSON: {raw[:80]!r}") from None
            if reply.pop("retire", False):
                await self._end(process, kill=False)
            else:
                self._idle.append(process)
            return reply
        raise SchemaCheckerError("the schema checker could not be reached")  # pragma: no cover -- loop returns

    async def close(self) -> None:
        """Ends every worker: an idle one reads EOF and exits, one that does not is killed."""
        for process in list(self._all):
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        for process in list(self._all):
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
                self._all.discard(process)
            except asyncio.TimeoutError:
                await self._end(process, kill=True)
        self._idle.clear()


#: One pool per event loop -- a subprocess belongs to the loop that started it -- kept ON the loop, so it
#: goes with it (a dict keyed by the loop kept every loop alive: the pool refers back to it). The
#: workers of a loop that ended without closing its pool exit after WORKER_IDLE_SECONDS.
_POOL_ATTRIBUTE = "_agent_system_schema_workers"


def worker_pool() -> SchemaWorkerPool:
    loop = asyncio.get_running_loop()
    pool = getattr(loop, _POOL_ATTRIBUTE, None)
    if pool is None:
        pool = SchemaWorkerPool()
        setattr(loop, _POOL_ATTRIBUTE, pool)
    return pool


async def close_schema_workers() -> None:
    """Ends the running loop's workers (tests, a plugin's stop)."""
    loop = asyncio.get_running_loop()
    pool = getattr(loop, _POOL_ATTRIBUTE, None)
    if pool is not None:
        delattr(loop, _POOL_ATTRIBUTE)
        await pool.close()


async def prepare_response_format(response_format: ResponseFormat, *, owner: Optional[str] = None) -> ResponseFormat:
    """The format with its schema checked against the strict subset and normalized -- by the worker, in the
    lane of ``owner`` (the user it is for).

    Raises InvalidResponseFormat for a schema outside the subset, or one the worker could not check
    within PREPARE_DEADLINE; SchemaCheckerError when the checker itself failed (busy, crashed).
    """
    if response_format.checked:
        return response_format
    if response_format.type == JSON_OBJECT:
        return dataclasses.replace(response_format, checked=True)
    try:
        reply = await worker_pool().request({"op": "prepare", "schema": response_format.schema}, PREPARE_DEADLINE,
                                            owner=owner)
    except SchemaCheckTimeout as slow:
        raise InvalidResponseFormat(f"the schema could not be checked: {slow}", field="schema") from None
    if not reply.get("ok"):
        if reply.get("field") == "worker":
            raise SchemaCheckerError(f"the schema checker failed: {reply.get('message')}")
        raise InvalidResponseFormat(str(reply.get("message")), field=str(reply.get("field") or "schema"))
    return dataclasses.replace(response_format, schema=reply["schema"], checked=True)


# ------------------------------------------------------------------ the answer

@dataclass
class OutputCheck:
    """An answer checked against its format.

    ``text`` is the answer as it is to be delivered: a whole-answer Markdown fence removed, nothing
    else touched. ``errors`` is empty when it matches. ``schema_failed`` says the answer could not be
    checked at all (the schema cannot be applied, the check ran out of time): asking the model again
    is pointless. ``checker_failed`` (with it) says why was not the answer's or the schema's doing: the
    checker was busy or broke -- no verdict, the same request may go through later.
    """

    text: str
    value: Any = None
    errors: list[str] = field(default_factory=list)
    schema_failed: bool = False
    checker_failed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


async def check_answer(text: Optional[str], response_format: ResponseFormat, *,
                       owner: Optional[str] = None) -> OutputCheck:
    """``text`` against ``response_format``, failing closed, in the lane of ``owner``.

    json_object needs no schema: parsed here, json only. json_schema goes to the worker, under
    CHECK_DEADLINE (the worker applies the subset again); a check that cannot be made is a failed one
    (``schema_failed``), never a pass -- a verdict when it ran out of time, ``checker_failed`` when the
    checker was busy or broke.
    """
    answer, value, errors = schema_worker.parse_answer(text)
    if errors or response_format.type == JSON_OBJECT:
        if not errors and not isinstance(value, dict):
            errors = [f"the answer is JSON, but a {type(value).__name__}, not an object"]
        return OutputCheck(text=answer, value=value, errors=errors)
    try:
        prepared = await prepare_response_format(response_format, owner=owner)
        reply = await worker_pool().request({"op": "check", "kind": JSON_SCHEMA, "schema": prepared.schema,
                                             "name": prepared.name, "text": answer}, CHECK_DEADLINE, owner=owner)
    except (SchemaCheckTimeout, InvalidResponseFormat) as failed:
        # A verdict: this answer, against this schema, cannot be checked in the time a check gets.
        logger.warning("Structured output: the answer could not be checked: %s", failed)
        return OutputCheck(text=answer, value=value, schema_failed=True,
                           errors=[f"the answer could not be checked: {failed}"])
    except SchemaCheckerError as broken:
        logger.warning("Structured output: the checker is not available: %s", broken)
        return OutputCheck(text=answer, value=value, schema_failed=True, checker_failed=True,
                           errors=[f"the answer could not be checked: {broken}"])
    if not reply.get("ok"):
        return OutputCheck(text=answer, value=value, schema_failed=True, checker_failed=True,
                           errors=[f"the answer could not be checked: {reply.get('message')}"])
    return OutputCheck(text=answer, value=value, errors=list(reply["errors"]),
                       schema_failed=bool(reply["schema_failed"]))


def instruction_text(response_format: ResponseFormat) -> str:
    """The format, told to a model that does not get it as a field (or gets no schema with it)."""
    if response_format.type == JSON_OBJECT:
        return ("When you give your final answer, write it as one JSON object and nothing else: "
                "no Markdown fences, no text before or after it. Steps that call tools are not affected.")
    about = f" ({response_format.description})" if response_format.description else ""
    return ("When you give your final answer, write it as one JSON value that matches the JSON schema "
            f"{response_format.name!r}{about} below, and nothing else: no Markdown fences, no text before "
            "or after it. Steps that call tools are not affected.\n\n"
            + json.dumps(response_format.schema, ensure_ascii=False, sort_keys=True))


def repair_text(errors: list[str]) -> str:
    """What a model is told once when its final answer did not match the format."""
    return ("Your final answer does not match the required JSON format:\n"
            + "\n".join(f"- {line}" for line in errors)
            + "\n\nWrite the corrected, complete answer again: only the JSON, no Markdown fences, "
              "no explanation.")
