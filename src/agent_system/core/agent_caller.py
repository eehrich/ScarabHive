"""AgentCaller — code calls a sub-agent and gets text or a validated object back.

The one primitive for code-orchestrated workflows: Python owns the control
flow, an agent is a leaf call ``task -> structured result``. Lifted from the
v4 pipeline's ``SubAgentMixin`` (writer_pipeline_v4/pipeline_agent.py), which
had been forked into v5b; this is the single copy both are meant to use.
See docs/agent_workflow_orchestration_design.md.

Transport is the ``sub_agent_manager`` plugin's tool
``<instance>_manage_sub_agent`` (operations ``create`` / ``continue``),
reached in-process via ``Agent.call_tool``.

Contract with that tool worth knowing:
- ``status`` in ``error`` / ``limit_reached`` / ``cancelled`` is a failure.
- A sub-agent that never answered comes back as CONTENT, not as an error:
  ``result_text = "Error: ..."`` / ``"Cancelled: ..."``. We count those as
  transport failures so an unhealthy run is visible, and let the retry
  handle them (a transient network error is exactly what retry is for).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections import Counter
from typing import Any, Awaitable, Callable, MutableMapping, TypeVar

from pydantic import TypeAdapter, ValidationError

from agent_system.core.cancellation import CancellationToken
from agent_system.utils.json_utils import repair_json, strip_markdown_fences

logger = logging.getLogger(__name__)

T = TypeVar("T")

_FAILED_AS_CONTENT = ("Error: ", "Cancelled: ")
_FAILURE_STATUSES = ("error", "limit_reached", "cancelled")
_HTML_TAG_RE = re.compile(r"</?(?:p|br|pre|code|li|ul|ol|h[1-6])\b")


class AgentCaller:
    """Per-run handle: call sub-agents through one SAM instance.

    Args:
        agent: the host ``Agent`` (needs ``call_tool``).
        sam_instance: plugin instance name, e.g. ``"v5b_sam"``; the tool is
            ``f"{sam_instance}_manage_sub_agent"``.
        session_id / user_id / request_id: forwarded as ``_session_id`` etc.
        cancellation_token: checked before every attempt.
        retries: extra attempts after the first (agent error OR bad JSON);
            negative values are treated as 0.
        model_fallback: after all retries, try once more with the advanced
            (2nd) LLM profile — unless the call already used it.
        counters: shared mapping for call analytics (``total``, per agent
            type, ``transport_failures``, ``transport_failures:<agent type>``
            — follow-ups count under ``follow_up`` —, ``schema_retries``).
            Pass the run's own dict to keep the numbers with the run.
        progress: optional ``async (str) -> None`` for status lines.
    """

    def __init__(
        self,
        agent: Any,
        *,
        sam_instance: str,
        session_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        cancellation_token: CancellationToken | None = None,
        retries: int = 2,
        model_fallback: bool = False,
        counters: MutableMapping[str, int] | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if agent is None:
            raise RuntimeError("AgentCaller requires an agent context")
        self.agent = agent
        self.tool_name = f"{sam_instance}_manage_sub_agent"
        self.session_id = session_id
        self.user_id = user_id
        self.request_id = request_id
        self.cancellation_token = cancellation_token
        self.retries = max(0, retries)
        self.model_fallback = model_fallback
        self.counters: MutableMapping[str, int] = (
            counters if counters is not None else Counter()
        )
        self._progress = progress
        #: instance_id of the most recent create that returned one (also when
        #: its text was empty). Convenience for SEQUENTIAL follow-ups only —
        #: under ``asyncio.gather`` it belongs to whichever create finished
        #: last, not to your call.
        self.last_instance_id: str | None = None

    # -- single attempts ------------------------------------------------------

    async def call_text(
        self,
        agent_type: str,
        task: str,
        *,
        use_advanced_model: bool = False,
    ) -> str:
        """Spawn a sub-agent, one attempt, return its result text."""
        text, _ = await self._create(agent_type, task, use_advanced_model=use_advanced_model)
        return text

    async def _create(
        self, agent_type: str, task: str, *, use_advanced_model: bool,
    ) -> tuple[str, str | None]:
        """One ``create``; returns ``(text, instance_id)``.

        The instance id is returned rather than only stored, because several
        ``call()``s may run concurrently on one caller (``asyncio.gather``) and
        the schema feedback must go to the instance that produced the answer,
        not to whichever create finished last.
        """
        params: dict[str, Any] = {
            "operation": "create",
            "agent_type": agent_type,
            "task": task,
            "blocking": True,  # non-blocking create returns no result text; fan-out is gather()
        }
        if use_advanced_model:
            params["use_advanced_model"] = True
        await self._note(f"▶ {agent_type}")
        t0 = time.monotonic()
        result = await self._invoke(params, label=agent_type)
        instance_id = result.get("instance_id") or None
        if instance_id:
            self.last_instance_id = instance_id
        text = self._result_text(result, agent_type)
        # Counted here at the transport seam — not in call() — so that
        # text-level users (call_text / the v4 wrappers) are covered too.
        self._count_transport_failure(agent_type, text)
        await self._note(f"✓ {agent_type} ({time.monotonic() - t0:.1f}s)")
        return text, instance_id

    async def follow_up_text(
        self,
        instance_id: str,
        message: str,
        *,
        use_advanced_model: bool = False,
    ) -> str:
        """Continue an existing sub-agent, one attempt, return its text.

        The model is resolved per request: a follow-up does NOT inherit the
        create call's ``use_advanced_model`` — set it again or the follow-up
        silently runs on the default profile.
        """
        params: dict[str, Any] = {
            "operation": "continue",
            "instance_id": instance_id,
            "message": message,
        }
        if use_advanced_model:
            params["use_advanced_model"] = True
        result = await self._invoke(params, label=instance_id)
        text = self._result_text(result, instance_id)
        self._count_transport_failure("follow_up", text)
        return text

    # -- structured calls with retry --------------------------------------------

    async def call(
        self,
        agent_type: str,
        task: str,
        *,
        schema: type[T] | None = None,
        use_advanced_model: bool = False,
    ) -> Any:
        """Call a sub-agent and return its JSON result, with retries.

        Without ``schema``: a ``dict``. With ``schema`` (pydantic model,
        dataclass, TypedDict, ``list[X]`` …): the validated object; a
        validation failure is fed back once to the same instance before the
        attempt counts as failed.
        """
        self.counters[agent_type] = self.counters.get(agent_type, 0) + 1
        self.counters["total"] = self.counters.get("total", 0) + 1
        last_error: Exception | None = None

        for attempt in range(1 + self.retries):
            self.check_cancelled()
            try:
                return await self._call_once(
                    agent_type, task, schema=schema, use_advanced_model=use_advanced_model,
                )
            except Exception as e:  # noqa: BLE001 — retry covers agent + parse errors
                last_error = e
                if attempt < self.retries:
                    logger.warning(
                        "Sub-agent %s failed (attempt %d/%d): %s — retrying",
                        agent_type, attempt + 1, 1 + self.retries, e,
                    )
                else:
                    logger.error(
                        "Sub-agent %s failed after %d attempts: %s",
                        agent_type, 1 + self.retries, e,
                    )

        if self.model_fallback and not use_advanced_model:
            logger.warning(
                "All %d attempts for %s failed — trying advanced-model fallback",
                1 + self.retries, agent_type,
            )
            self.check_cancelled()
            try:
                return await self._call_once(
                    agent_type, task, schema=schema, use_advanced_model=True,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("Advanced-model fallback for %s also failed: %s", agent_type, e)

        assert last_error is not None
        raise last_error

    async def follow_up(
        self,
        instance_id: str,
        message: str,
        *,
        schema: type[T] | None = None,
        use_advanced_model: bool = False,
    ) -> Any:
        """Continue a sub-agent and return its JSON result, with retries."""
        last_error: Exception | None = None
        for attempt in range(1 + self.retries):
            self.check_cancelled()
            try:
                raw = await self.follow_up_text(
                    instance_id, message, use_advanced_model=use_advanced_model,
                )
                return await self._validate(
                    parse_json_value(raw), schema, instance_id, use_advanced_model,
                )
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < self.retries:
                    logger.warning(
                        "Follow-up %s failed (attempt %d/%d): %s — retrying",
                        instance_id, attempt + 1, 1 + self.retries, e,
                    )
                else:
                    logger.error(
                        "Follow-up %s failed after %d attempts: %s",
                        instance_id, 1 + self.retries, e,
                    )
        assert last_error is not None
        raise last_error

    def check_cancelled(self) -> None:
        """Raise ``asyncio.CancelledError`` if the run was cancelled."""
        if self.cancellation_token and self.cancellation_token.is_cancelled:
            raise asyncio.CancelledError("Run cancelled (user abort)")

    # -- internals ----------------------------------------------------------------

    async def _call_once(
        self, agent_type: str, task: str, *, schema: type[T] | None, use_advanced_model: bool,
    ) -> Any:
        raw, instance_id = await self._create(
            agent_type, task, use_advanced_model=use_advanced_model,
        )
        return await self._validate(
            parse_json_value(raw), schema, instance_id, use_advanced_model,
        )

    async def _validate(
        self, value: Any, schema: type[T] | None,
        instance_id: str | None, use_advanced_model: bool,
    ) -> Any:
        if schema is None:
            return _as_object(value)
        adapter = TypeAdapter(schema)
        try:
            return adapter.validate_python(value)
        except ValidationError as first:
            if not instance_id:
                raise
            self.counters["schema_retries"] = self.counters.get("schema_retries", 0) + 1
            logger.warning(
                "Result of %s does not match %s — feeding the errors back once: %s",
                instance_id, getattr(schema, "__name__", schema), _short_errors(first),
            )
            raw = await self.follow_up_text(
                instance_id, _schema_feedback(first), use_advanced_model=use_advanced_model,
            )
            return adapter.validate_python(parse_json_value(raw))

    async def _invoke(self, params: dict[str, Any], *, label: str) -> dict[str, Any]:
        params.update({
            "_session_id": self.session_id,
            "_user_id": self.user_id,
            "_request_id": self.request_id,
            "_agent": self.agent,
        })
        result = await self.agent.call_tool(self.tool_name, params)
        if not isinstance(result, dict):
            raise RuntimeError(f"{self.tool_name} returned {type(result).__name__}, expected dict")
        status = result.get("status", "")
        if status in _FAILURE_STATUSES:
            raise RuntimeError(result.get("error") or f"Sub-agent {label}: {status}")
        return result

    @staticmethod
    def _result_text(result: dict[str, Any], label: str) -> str:
        text = result.get("result", "")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Sub-agent {label} returned an empty result")
        return text

    def _count_transport_failure(self, label: str, raw: str) -> None:
        if not raw.startswith(_FAILED_AS_CONTENT):
            return
        self.counters["transport_failures"] = self.counters.get("transport_failures", 0) + 1
        key = f"transport_failures:{label}"
        self.counters[key] = self.counters.get(key, 0) + 1
        logger.warning(
            "Sub-agent %s did NOT answer (transport/lifecycle, not a format "
            "problem): %r — retry follows. Run total: %d.",
            label, raw[:120], self.counters["transport_failures"],
        )

    async def _note(self, line: str) -> None:
        if self._progress is not None:
            await self._progress(line)


# -- JSON parsing -------------------------------------------------------------------

def parse_json_value(text: str) -> Any:
    """Parse the JSON value (object or array) out of a sub-agent's text.

    Order: the sub-agent's own failure → fences stripped → strict
    ``json.loads`` → HTML-escaped guard → first ``{…}`` span → ``repair_json``.
    Raises ``ValueError`` when nothing yields a dict or list.
    """
    # FIRST, before any parsing: the sub-agent's own failure, handed back AS
    # its answer. ``_count_transport_failure`` recognises exactly this prefix
    # on exactly this string (``_create`` passes one text to both) and logs
    # "did NOT answer (transport/lifecycle, not a format problem)".
    #
    # This has to run before the brace-span and ``repair_json``, not after
    # them -- measured, because the first version of this guard sat at the end
    # and never fired for the common case:
    #
    #   Error: Agent execution failed: Error code: 429 - {'error': {...}}
    #     -> repair_json returns {'error': {...}}
    #     -> the caller's parsed.get("issues", []) yields []
    #     -> "0 findings", no exception, no retry: a failed review reads as a
    #        clean book.
    #
    # Any brace in a provider's error body is enough. Parsing it would be
    # parsing the transport's complaint as the agent's answer, so nothing
    # carrying this prefix is offered to the parser at all -- callers that can
    # still salvage the text get it unchanged from their own except-branch
    # (``pipeline_agent._parse_json_result`` -> markdown replacement table).
    if text.startswith(_FAILED_AS_CONTENT):
        raise ValueError(f"Sub-agent did not answer: {text[:200].strip()}")

    text = strip_markdown_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # HTML guard. An agent that inherits a Markdown->HTML output hook returns
    # ``<p>{<br> "a": 1</p>`` or ``<pre><code>{&quot;a&quot;: 1}``. That is
    # NOT a parse error: repair_json happily returns a dict whose keys are
    # ``quot;a&quot;`` or whose entries are cut at every <br> — no exception,
    # a clean-looking empty result. Measured on B57 (2026-08-05): twelve
    # findings silently lost, book passed the gate. The fix belongs in the
    # agent config; this guard makes the next misconfiguration loud. It sits
    # AFTER the fast path (valid JSON carrying <p> in a text field is never
    # touched) and BEFORE repair_json (the step that makes the damage silent).
    if "&quot;" in text or _HTML_TAG_RE.search(text):
        unescaped = html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()
        healed: Any = None
        try:
            healed = json.loads(unescaped)
        except json.JSONDecodeError:
            a, b = unescaped.find("{"), unescaped.rfind("}")
            if a >= 0 and b > a:
                try:
                    healed = json.loads(unescaped[a:b + 1])
                except json.JSONDecodeError:
                    pass
        if isinstance(healed, (dict, list)):
            logger.warning(
                "JSON arrived HTML-encoded (Markdown->HTML hook active?) — "
                "unescaped and parsed. The sending agent should disable "
                "`markdown_formatter.format_markdown_output`.",
            )
            return healed

    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            candidate = json.loads(text[a:b + 1])
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            pass

    repaired = repair_json(text, return_objects=True)
    # A repaired LIST counts only when it carries at least one object.
    # repair_json happily extracts `[2]` out of prose like "see items [1]
    # and [2] in the text" — accepting that as a result means no retry, no
    # error, and a consumer that sees "0 findings" (v4 review 2026-08-19).
    # A dict-free array as the WHOLE answer still passes via the strict
    # fast path above.
    if isinstance(repaired, dict) and repaired:
        return repaired
    if isinstance(repaired, list) and any(isinstance(v, dict) for v in repaired):
        return repaired

    head = text[:300].replace("\n", "\\n")
    tail = text[-150:].replace("\n", "\\n") if len(text) > 450 else ""
    logger.warning(
        "JSON parse failed after all strategies (len=%d): head=%r%s",
        len(text), head, f" | tail={tail!r}" if tail else "",
    )
    raise ValueError(f"Could not parse JSON from sub-agent output ({len(text)} chars)")


def parse_json_object(text: str) -> dict[str, Any]:
    """``parse_json_value`` narrowed to a dict (first dict of a list, else error)."""
    return _as_object(parse_json_value(text))


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        first = next((v for v in value if isinstance(v, dict)), None)
        if first is not None:
            logger.warning(
                "Sub-agent returned a JSON array of %d — taking the first object", len(value),
            )
            return first
    raise ValueError(f"Expected a JSON object, got {type(value).__name__}")


def _short_errors(err: ValidationError, limit: int = 8) -> str:
    parts = []
    for e in err.errors()[:limit]:
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


def _schema_feedback(err: ValidationError) -> str:
    return (
        "Your answer does not match the expected JSON format:\n"
        + "\n".join(f"- {line}" for line in _short_errors(err).split("; "))
        + "\n\nReturn ONLY the corrected, COMPLETE JSON answer — same "
        "structure, no explanation, no Markdown fences."
    )
