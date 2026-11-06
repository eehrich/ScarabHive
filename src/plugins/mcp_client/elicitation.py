"""An MCP server's question to the person (elicitation), while one of its tools runs.

A server may ask in the middle of a tool call -- a form (``requestedSchema``: flat fields of text, number,
boolean or a choice) or an address to open (URL mode). The question goes where ask_user's and
tool_approval's go (``core.run_questions``): a status row of its own in the run, drawn by the chat from
the question's ``form``, answered through this plugin's ``/answer`` route.

Who is asked: the person watching the run whose tool call is in flight on that connection -- the agent
core hands the call over in ``request_context.current_external_call``, the pool keeps it on the
connection while the call runs (manager.py). Not exactly one call in flight, or nobody watching: the
question is not asked and the server hears "cancel" -- "decline" is the person's own refusal.

A form is asked field by field, each the way the chat can answer it: a choice field by its options (a
boolean: yes / no), a text or number field in the answer box, an optional field with Skip. No field: OK.
URL mode: Done, after opening the address. Always: Decline. An answer that does not fit (not a number,
not one of the options, outside the field's limits or format) is refused at the route, so the person can
correct it; a field whose values the protocol cannot carry back (an object, a list of numbers) is not
asked at all -- the server hears "cancel".
"""
from __future__ import annotations

import datetime
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from agent_system.core.run_questions import (AnswerRejected, CANCELLED, GONE, Question, QuestionBroker,
                                             is_read, put_to_person, status_line)
from agent_system.tools.status import StatusScope, get_status_bus

logger = logging.getLogger(__name__)

META_KEY = "mcp_elicitation"
DECLINE = "decline"
CANCEL = "cancel"
DONE = "done"
OK = "ok"
SKIP = "skip"
YES, NO = "yes", "no"
MAX_TEXT_CHARS = 4000
#: What the person sees of a server's message at most: foreign text.
MAX_MESSAGE_CHARS = 2000
#: Fields of one form asked at most: a server is not to keep a person answering.
MAX_FIELDS = 10
#: Options of one field at most, and an option's length: each is a button. More: the form is not asked.
MAX_OPTIONS = 25
MAX_OPTION_CHARS = 200
#: A field's name or title and its description as shown at most (foreign text, cut).
MAX_LABEL_CHARS = 200
MAX_ABOUT_CHARS = 1000
MAX_URL_CHARS = 2000
#: RFC 3339 -- what the protocol's date and date-time formats are; Python's fromisoformat takes more.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")

Field = Tuple[str, Dict[str, Any]]


@dataclass(frozen=True)
class ElicitAnswer:
    action: str                                   # accept | decline
    content: Optional[Dict[str, Any]] = None
    answered_by: Optional[str] = None


def _fields(schema: Any) -> Tuple[Tuple[Field, ...], Tuple[str, ...]]:
    """((name, property), ...) and the required names, of a flat form schema."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return (), ()
    fields = tuple((str(name), spec if isinstance(spec, dict) else {}) for name, spec in properties.items())
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return fields, tuple(str(r) for r in required)


def _choices(spec: Dict[str, Any]) -> List[Tuple[Any, str]]:
    """(value, label) of a choice field: ``enum``, or ``oneOf``/``anyOf`` of ``const`` (titled enums); for a
    multiple choice (``type: array``) those of its ``items``. The value keeps its type (an enum of numbers)."""
    if spec.get("type") == "array":
        spec = spec.get("items") if isinstance(spec.get("items"), dict) else {}
    if isinstance(spec.get("enum"), list):
        return [(v, str(v)) for v in spec["enum"]]
    for key in ("oneOf", "anyOf"):
        if isinstance(spec.get(key), list):
            return [(item["const"], str(item.get("title") or item["const"])) for item in spec[key]
                    if isinstance(item, dict) and "const" in item]
    return []


def _options(spec: Dict[str, Any]) -> List[str]:
    """The values of a choice field as the answer names them."""
    return [str(value) for value, _ in _choices(spec)]


def _unasked_because(spec: Dict[str, Any]) -> str:
    """Why a field cannot be put to the person, or ""."""
    kind = spec.get("type")
    if kind == "array" and not _choices(spec):
        return "a list without options"
    if kind not in (None, "string", "number", "integer", "boolean", "array"):
        return f"a field of type {kind}"
    choices = _choices(spec)
    if kind == "array" and not all(isinstance(v, str) for v, _ in choices):
        return "a list whose options are not texts"     # the protocol's lists hold strings only
    if len(choices) > MAX_OPTIONS or any(len(label) > MAX_OPTION_CHARS or len(str(v)) > MAX_OPTION_CHARS
                                         for v, label in choices):
        return f"more than {MAX_OPTIONS} options, or longer than {MAX_OPTION_CHARS} characters"
    return ""


def _place(index: int) -> str:
    """How an answer names the option at ``index``."""
    return f"#{index}"


def _picked(spec: Dict[str, Any], choices: Sequence[str]) -> List[Any]:
    """The schema's values of the options named in ``choices``, in the schema's order; AnswerRejected for
    a name that is no option."""
    values = _choices(spec)
    places = {_place(i): i for i in range(len(values))}
    if any(choice not in places for choice in choices):
        raise AnswerRejected(422, "pick from the question's options")
    return [values[i][0] for i in sorted({places[c] for c in choices})]


def _cut(text: Any, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_url(url: str) -> bool:
    """A plain http(s) address with a host, shown as it is: no spaces, no control or invisible characters
    (bidi overrides, zero-width), no ``user@`` in front of the host, not too long to show whole."""
    if not url or len(url) > MAX_URL_CHARS:
        return False
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname) and "@" not in parts.netloc


@dataclass
class ElicitQuestion(Question):
    server: str = ""
    message: str = ""
    url: str = ""
    #: The one field this question asks for, or None (a form without fields, an address).
    field: Optional[Field] = None
    required: bool = False
    #: "2/3" when a form has several fields.
    step: str = ""

    def to_public(self) -> Dict[str, Any]:
        return {**super().to_public(), "server": self.server, "message": self.message}

    def form(self) -> Dict[str, Any]:
        prompt = f"MCP server '{self.server}' asks: {self.message}"
        decline = {"value": DECLINE, "label": "Decline", "tone": "danger"}
        base = {"prompt": prompt, "detail": None, "warning": None, "multi_select": False, "text": None}
        if self.url:
            return {**base, "detail": f"Open this address, then answer Done: {self.url}",
                    "warning": "The address comes from the MCP server, not from this app.",
                    "choices": [{"value": DONE, "label": "Done", "tone": "primary"}, decline]}
        if self.field is None:
            return {**base, "choices": [{"value": OK, "label": "OK", "tone": "primary"}, decline]}
        name, spec = self.field
        label = _cut(spec.get("title") or name, MAX_LABEL_CHARS)
        about = f" -- {_cut(spec['description'], MAX_ABOUT_CHARS)}" if spec.get("description") else ""
        default = f" (default: {_cut(spec['default'], MAX_OPTION_CHARS)})" if "default" in spec else ""
        detail = f"{f'{self.step}: ' if self.step else ''}{label}{about}{default}"
        skip = [] if self.required else [{"value": SKIP, "label": "Skip"}]
        if spec.get("type") == "boolean":
            options = [(YES, YES), (NO, NO)]
        else:
            # By place, not by text: an option may be called "decline" or "skip", or two may print alike.
            options = [(_place(i), label) for i, (_, label) in enumerate(_choices(spec))]
        if options:
            return {**base, "detail": detail, "multi_select": spec.get("type") == "array",
                    "choices": [{"value": v, "label": label} for v, label in options] + skip + [decline]}
        return {**base, "detail": detail, "choices": skip + [decline],
                "text": {"label": f"{label} ({spec.get('type') or 'text'})", "alone": True,
                         "max_chars": MAX_TEXT_CHARS}}


def _value(name: str, spec: Dict[str, Any], raw: str) -> Any:
    """``raw`` as the field's type, or AnswerRejected that says what fits."""
    kind = spec.get("type")
    options = _options(spec)
    raw = raw.strip()
    if kind == "boolean":
        if raw.casefold() in ("yes", "true", "y", "1"):
            return True
        if raw.casefold() in ("no", "false", "n", "0"):
            return False
        raise AnswerRejected(422, f"{name}: answer yes or no")
    if options:
        if raw not in options:
            raise AnswerRejected(422, f"{name}: one of {', '.join(options)}")
        return dict((str(v), v) for v, _ in _choices(spec))[raw]     # as the schema has it: 2, not "2"
    if kind in ("integer", "number"):
        if kind == "integer":
            try:
                number: Any = int(raw)          # exact: a float would round 9007199254740993
            except ValueError:
                raise AnswerRejected(422, f"{name}: a whole number") from None
        else:
            try:
                number = float(raw)
            except ValueError:
                raise AnswerRejected(422, f"{name}: a number") from None
            if not math.isfinite(number):    # nan, inf, 1e309 -- JSON has none of them
                raise AnswerRejected(422, f"{name}: a number") from None
        if isinstance(spec.get("minimum"), (int, float)) and number < spec["minimum"]:
            raise AnswerRejected(422, f"{name}: at least {spec['minimum']}")
        if isinstance(spec.get("maximum"), (int, float)) and number > spec["maximum"]:
            raise AnswerRejected(422, f"{name}: at most {spec['maximum']}")
        return number
    if isinstance(spec.get("minLength"), int) and len(raw) < spec["minLength"]:
        raise AnswerRejected(422, f"{name}: at least {spec['minLength']} characters")
    if isinstance(spec.get("maxLength"), int) and len(raw) > spec["maxLength"]:
        raise AnswerRejected(422, f"{name}: at most {spec['maxLength']} characters")
    if not _of_format(raw, spec.get("format")):
        raise AnswerRejected(422, f"{name}: a {spec['format']}")
    return raw


def _of_format(raw: str, fmt: Any) -> bool:
    """Whether ``raw`` is of the string format the protocol names (email, uri, date, date-time)."""
    if fmt == "email":
        local, at, domain = raw.rpartition("@")
        return bool(at and local and "." in domain and not any(ch.isspace() for ch in raw))
    if fmt == "uri":
        try:
            parts = urlsplit(raw)
        except ValueError:
            return False
        return bool(parts.scheme and (parts.netloc or parts.path)) and not any(ch.isspace() for ch in raw)
    if fmt == "date":
        return bool(_DATE.fullmatch(raw)) and _is_date(raw)
    if fmt == "date-time":
        match = _DATE_TIME.fullmatch(raw)
        if not match:
            return False
        day, hour, minute, second = match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))
        return _is_date(day) and hour < 24 and minute < 60 and second <= 60      # 60: a leap second
    return True


def _is_date(raw: str) -> bool:
    try:
        datetime.date.fromisoformat(raw)
    except ValueError:
        return False
    return True


class ElicitBroker(QuestionBroker):
    """The servers' open questions by id; ``take`` turns an answer into the content the schema asks for."""

    def take(self, question_id: str, choices: Sequence[str], text: str,
             answered_by: Optional[str] = None) -> Question:
        question = self.get(question_id)
        if not isinstance(question, ElicitQuestion):
            return self.resolve(question_id, None)          # says nothing waits under this id
        if not isinstance(text, str) or not isinstance(choices, (list, tuple)):
            raise AnswerRejected(422, "choices must be a list and text a text")
        if len(text) > MAX_TEXT_CHARS:
            raise AnswerRejected(422, f"the answer is longer than {MAX_TEXT_CHARS} characters")
        multi = question.field is not None and question.field[1].get("type") == "array"
        if len(choices) > 1 and not multi:
            raise AnswerRejected(422, "this question takes one option")
        if DECLINE in choices:
            return self.resolve(question_id, ElicitAnswer(DECLINE, answered_by=answered_by))
        if question.url or question.field is None:
            if list(choices) != [DONE if question.url else OK]:
                raise AnswerRejected(422, "answer Done or Decline" if question.url else "answer OK or Decline")
            return self.resolve(question_id, ElicitAnswer("accept", {}, answered_by))
        name, spec = question.field
        if SKIP in choices:
            if len(choices) > 1:
                raise AnswerRejected(422, "Skip goes alone, without an option")
            if question.required:
                raise AnswerRejected(422, f"{name} is required")
            return self.resolve(question_id, ElicitAnswer("accept", {}, answered_by))
        if multi:
            picked = _picked(spec, choices)
            least, most = spec.get("minItems"), spec.get("maxItems")
            if not picked and question.required or isinstance(least, int) and len(picked) < least:
                raise AnswerRejected(422, f"{name}: pick at least {max(least or 0, 1)}")
            if isinstance(most, int) and len(picked) > most:
                raise AnswerRejected(422, f"{name}: pick at most {most}")
            return self.resolve(question_id, ElicitAnswer("accept", {name: picked} if picked else {}, answered_by))
        if choices and spec.get("type") != "boolean" and _choices(spec):
            return self.resolve(question_id, ElicitAnswer("accept", {name: _picked(spec, choices)[0]}, answered_by))
        raw = choices[0] if choices else text
        if not raw.strip():
            raise AnswerRejected(422, f"{name}: an answer, or Decline" if question.required
                                 else f"{name}: an answer, Skip or Decline")
        return self.resolve(question_id, ElicitAnswer("accept", {name: _value(name, spec, raw)}, answered_by))


class Elicitation:
    """Asks the person for one server (the connection's elicitation handler)."""

    def __init__(self, instance: str, broker: ElicitBroker, answer_url: str, *, timeout: float = 300.0,
                 reask_seconds: float = 20.0, gone_after_seconds: float = 15.0) -> None:
        self.instance = instance
        self.broker = broker
        self.answer_url = answer_url
        self.timeout = timeout
        self.reask_seconds = reask_seconds
        self.gone_after_seconds = gone_after_seconds

    async def ask(self, server: str, call: Any, params: Any) -> Any:
        """The ``ElicitResult`` for the server's ``params``, from the person watching ``call``'s run."""
        from mcp import types

        message = str(getattr(params, "message", "") or "")
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "…"
        url = ""
        if getattr(params, "mode", None) == "url":
            url = str(getattr(params, "url", "") or "")
            if not _safe_url(url):
                logger.warning("MCP server '%s' asked to open an address that is no plain http(s) one: cancelled",
                               server)
                return types.ElicitResult(action=CANCEL)
        if call is None or not call.request_id or call.token is None:
            logger.info("MCP server '%s' asked the person outside an agent's tool call: cancelled", server)
            return types.ElicitResult(action=CANCEL)
        if not is_read(call.request_id, self.gone_after_seconds):
            logger.info("MCP server '%s' asked, nobody watches run %s: cancelled", server, call.request_id)
            return types.ElicitResult(action=CANCEL)
        fields, required = _fields(getattr(params, "requested_schema", None))
        if len(fields) > MAX_FIELDS:
            logger.warning("MCP server '%s' asked for %d fields at once: cancelled", server, len(fields))
            return types.ElicitResult(action=CANCEL)
        unasked = next((f"{name}: {why}" for name, spec in fields if (why := _unasked_because(spec))), "")
        if unasked:
            # A form the person could not answer as the server wants it is not put at all.
            logger.warning("MCP server '%s' asked for %s, which the chat cannot ask: cancelled", server, unasked)
            return types.ElicitResult(action=CANCEL)
        if url or not fields:
            outcome = await self._ask_one(server, call, message, url=url)
            return self._result(outcome, None if url else {})
        content: Dict[str, Any] = {}
        for number, field in enumerate(fields, 1):
            step = f"{number}/{len(fields)}" if len(fields) > 1 else ""
            outcome = await self._ask_one(server, call, message, field=field, required=field[0] in required,
                                          step=step)
            if not isinstance(outcome, ElicitAnswer) or outcome.action != "accept":
                return self._result(outcome, {})
            content.update(outcome.content or {})
        return self._result(ElicitAnswer("accept", content), content)

    @staticmethod
    def _result(outcome: Union[ElicitAnswer, str], content: Optional[Dict[str, Any]]) -> Any:
        """The protocol's answer: content only with an accepted form (URL mode carries none)."""
        from mcp import types

        if isinstance(outcome, ElicitAnswer):
            return types.ElicitResult(action=outcome.action,
                                      content=content if outcome.action == "accept" else None)
        return types.ElicitResult(action=CANCEL)

    async def _ask_one(self, server: str, call: Any, message: str, *, url: str = "", field: Optional[Field] = None,
                       required: bool = False, step: str = "") -> Union[ElicitAnswer, str]:
        """One question on a row of its own: the answer, or why there is none (CANCELLED, GONE, a timeout)."""
        question = self.broker.open_question(
            ElicitQuestion, owner=call.user_id or None, session_id=call.session_id, request_id=call.request_id,
            agent_name=call.agent_name, timeout=self.timeout, server=server, message=message, url=url,
            field=field, required=required, step=step)
        asking = status_line(f"MCP server '{server}' asks: {message}")
        scope = StatusScope(get_status_bus(), self.instance, request_id=f"{call.request_id}_elicit_{question.id}",
                            start_msg=asking)
        try:
            async with scope:
                outcome = await put_to_person(
                    self.broker, question, scope, meta_key=META_KEY, answer_url=self.answer_url, line=asking,
                    token=call.token, reask_seconds=self.reask_seconds, gone_after_seconds=self.gone_after_seconds,
                    cut_off=f"{server}: question cut off while waiting")
                if isinstance(outcome, ElicitAnswer):
                    who = outcome.answered_by or "the user"
                    done = "answered" if outcome.action == "accept" else "declined"
                    await scope.end(status_line(f"{server}: {done} by {who}"))
                    return outcome
                why = {CANCELLED: "the run was cancelled", GONE: "nobody reads the run any more"}.get(
                    outcome, f"no answer within {self.timeout:g} s")
                await scope.error(status_line(f"{server}: not answered, {why}"))
                return outcome
        finally:
            self.broker.close(question)
