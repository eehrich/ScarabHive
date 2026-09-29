"""OpenTelemetry traces for agent runs, LLM calls and tool calls -- from hooks.

Span model (GenAI semantic conventions, attribute names as strings):

    invoke_agent <agent>          one per run (request id); opened at the run's
    |                             first LLM or tool event, ended at session_end
    +-- chat <model>              one per LLM request the client reports
    |                             (post_llm_response), retries included
    +-- execute_tool <tool>       one per tool call (post_tool_call), timed by
    |                             the call's own start and end
    +-- invoke_agent <sub-agent>  a run whose request id extends this one's
                                  (``<id>_007_sub_...``) nests under it

Why these hooks and no others: pre_llm_call, post_llm_call and
pre_llm_request each hand the hook a deep copy of the whole conversation (or
the request payload) on every step. post_llm_response carries what a span
needs -- provider, model, duration, usage, finish reason, error -- without it.
The LLM span is placed in time from ``llm_duration_ms``.

A tool call whose post hook never comes (blocked by a later pre hook, the run
cancelled before it started, the run crashed) is kept as a small pending
record; session_end resolves it from the run's messages, an idle timeout or
the state limit ends it otherwise. A call blocked by a hook that ran before
this plugin's pre hook is found at session_end from its blocked result.

Every span is built and ended in one go with explicit timestamps, except the
run span, which stays open until its run ends -- the only state that could
leak, and it is bounded (``max_open_runs``) and expires
(``idle_timeout_seconds`` without an event of the run or of a run nested in
it). No hook here awaits anything; the spans go to a BatchSpanProcessor,
whose export runs on its own thread.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.servers.agent.components.session_tracking import callers_session

from .settings import TelemetrySettings

try:
    from opentelemetry import trace as trace_api
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError:  # the pipeline cannot be built then, and says so once
    trace_api = None

logger = logging.getLogger(__name__)

# GenAI semantic conventions. The constants in opentelemetry-semantic-conventions
# are marked deprecated (the GenAI conventions moved to their own repository),
# so the names are spelled out here.
OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
REQUEST_STREAM = "gen_ai.request.stream"
FINISH_REASONS = "gen_ai.response.finish_reasons"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
TOKEN_TYPE = "gen_ai.token.type"
AGENT_NAME = "gen_ai.agent.name"
CONVERSATION_ID = "gen_ai.conversation.id"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_TYPE = "gen_ai.tool.type"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
ERROR_TYPE = "error.type"
SERVER_ADDRESS = "server.address"
SERVER_PORT = "server.port"
SESSION_ID = "session.id"
USER_ID = "user.id"
# ScarabHive's own.
REQUEST_ID = "scarabhive.request_id"
RUN_OUTCOME = "scarabhive.run.outcome"
RUN_PERSISTED = "scarabhive.run.persisted"
LLM_RETRY = "scarabhive.llm.retry"
TOOL_SERVER = "scarabhive.tool.server"
TOOL_SOURCE = "scarabhive.tool.source"
TOOL_OUTCOME = "scarabhive.tool.outcome"

#: Every attribute that carries content (prompts, answers, tool arguments and
#: results). Exported only with ``capture_content: true``.
CONTENT_ATTRIBUTES = (TOOL_ARGUMENTS, TOOL_RESULT, INPUT_MESSAGES, OUTPUT_MESSAGES)

TRUNCATION_MARK = "…[truncated]"
#: Error texts (LLM transport, run) with capture_content: capped here.
_ERROR_TEXT_CHARS = 256
#: How far a request id is taken apart looking for the run it belongs to.
_MAX_ANCESTOR_STEPS = 16
_BLOCKED_TYPE = "ToolCallBlocked"
#: An HTTP status in an LLM error text ("HTTP 503", "status_code=429",
#: "Error code: 400"): what an error says without its text.
_HTTP_STATUS = re.compile(r"(?:http|status(?:_code)?|code)\W{0,3}([45]\d\d)\b", re.IGNORECASE)


def cap_text(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, marked when cut."""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - len(TRUNCATION_MARK))] + TRUNCATION_MARK


def _shrink(value: Any, limit: int, depth: int = 0) -> Any:
    """A copy of ``value`` small enough to encode cheaply: every string cut to
    ``limit``, containers to 64 entries, nesting to 8 levels. The encoded
    text is capped again afterwards; this only bounds the work."""
    if isinstance(value, str):
        return value[:limit + 1]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 8:
        return "…"
    if isinstance(value, dict):
        return {str(k)[:limit]: _shrink(v, limit, depth + 1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, limit, depth + 1) for v in list(value)[:64]]
    return str(value)[:limit + 1]


def _ns(seconds: Any) -> Optional[int]:
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        return int(seconds * 1_000_000_000)
    return None


def _stamp_ns(stamp: Any) -> Optional[int]:
    """A message timestamp (datetime, or ISO text from a dict) in ns, or None."""
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    if isinstance(stamp, datetime):
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1_000_000_000)
    return None


def _text_of(content: Any) -> str:
    """The text of a message's content; images and other parts are left out."""
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for item in content or ():
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _field(message: Any, name: str) -> Any:
    """A message field, from a ChatMessage or a plain dict alike."""
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _is_identifier(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 64
            and value.replace("_", "").replace(".", "").isalnum())


def _run_token(request_id: str) -> Any:
    """The cancellation token a live run of this id holds, None if none runs.
    A run registers it before its first call and drops it before its
    session_end hooks -- so it tells a new run under a reused id from late
    events of the one that ended."""
    if not request_id:
        return None
    try:
        from agent_system.core.cancellation import get_cancellation_manager
        return get_cancellation_manager().get_token(request_id)
    except Exception:  # noqa: BLE001 - no token is an answer too
        return None


@dataclass
class _Run:
    request_id: str
    span: Any
    context: Any
    last_seen: float
    #: the open run this one nests in; its events keep that one alive too
    parent_id: Optional[str] = None
    #: the live run's cancellation token when the span was opened
    token: Any = None
    #: call ids that got a span -- the session_end scan for blocked calls
    #: skips them. None once it outgrew the limit (the scan is skipped then).
    reported_calls: Optional[set] = field(default_factory=set)


@dataclass
class _EndedRun:
    context: Any
    token: Any
    #: ended by its session_end (not expired or evicted): a second session_end
    #: under the id belongs to another run
    closed: bool


@dataclass
class _PendingCall:
    key: Tuple[Any, ...]
    run_id: Optional[str]
    parent: Any
    call_id: Optional[str]
    name: str
    server: str
    source: str
    started_ns: int
    arguments: Optional[str]
    last_seen: float
    identity: Dict[str, Any]


class OtelHooks(SchemaBasedPluginHook):
    """The hooks of the otel plugin; see the module docstring for the model."""

    def __init__(self, plugin_dir: Path | str, server_config: Any = None):
        super().__init__(plugin_dir)
        config = dict(self.get_config())
        instance_config = getattr(server_config, "config", None) if server_config is not None else None
        if isinstance(instance_config, dict):
            config.update(instance_config)
        self.settings = TelemetrySettings.from_config(config)
        self._lock = threading.RLock()
        self._pipeline: Any = None
        self._stopped = False
        self._broken = False
        self._clock: Callable[[], float] = time.monotonic
        self._runs: "OrderedDict[str, _Run]" = OrderedDict()
        self._ended: "OrderedDict[str, _EndedRun]" = OrderedDict()
        # Waiting calls by arrival (sequence number), and by call key: two
        # calls may share a key (a backend that sends no call ids), and their
        # post hooks come in call order.
        self._pending: "OrderedDict[int, _PendingCall]" = OrderedDict()
        self._pending_by_key: Dict[Tuple[Any, ...], Deque[int]] = {}
        self._pending_seq = 0
        #: keys of calls already reported without a result (expired, evicted):
        #: a post hook that still comes for one must not report it twice
        self._forgotten: "OrderedDict[Tuple[Any, ...], None]" = OrderedDict()
        self._failures_reported: set = set()

    # ------------------------------------------------------------ lifecycle

    async def start_plugin(self) -> None:
        with self._lock:
            self._stopped = False
            self._broken = False  # a start after a failed one tries again
        self._ensure_pipeline()

    async def stop_plugin(self) -> None:
        """End what is still open, then flush and shut down the exporters."""
        with self._lock:
            pipeline = self._pipeline
            if pipeline is not None:
                try:
                    self._end_everything(pipeline)
                except Exception as error:  # noqa: BLE001 - stopping must go on
                    logger.warning("otel: ending open spans at stop failed: %s", error)
            self._pipeline = None
            self._stopped = True
        if pipeline is not None:
            await pipeline.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> None:
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.force_flush(timeout_millis)

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is not None or self._stopped or self._broken:
            return self._pipeline
        with self._lock:
            if self._pipeline is None and not self._stopped and not self._broken:
                try:
                    if trace_api is None:
                        raise ImportError("the opentelemetry packages are not installed "
                                          "(pip install opentelemetry-sdk)")
                    from .telemetry import TelemetryPipeline
                    self._pipeline = TelemetryPipeline.create(self.settings)
                except Exception as error:  # noqa: BLE001 - tracing off, the agent unaffected
                    self._broken = True
                    logger.error("otel: tracing is off -- the pipeline could not be built: %s", error)
        return self._pipeline

    # ---------------------------------------------------------------- hooks

    async def record_llm_call(self, context: HookContext) -> HookResult:
        """post_llm_response: one ``chat`` span per request the client reports."""
        self._guarded("record_llm_call", self._record_llm_call, context)
        return HookResult(success=True)

    async def open_tool_call(self, context: HookContext) -> HookResult:
        """pre_tool_call: remember the call until its post hook comes."""
        self._guarded("open_tool_call", self._open_tool_call, context)
        return HookResult(success=True)

    async def close_tool_call(self, context: HookContext) -> HookResult:
        """post_tool_call: one ``execute_tool`` span per call that ran."""
        self._guarded("close_tool_call", self._close_tool_call, context)
        return HookResult(success=True)

    async def close_run(self, context: HookContext) -> HookResult:
        """session_end: end the run span and what the run left pending."""
        self._guarded("close_run", self._close_run, context)
        return HookResult(success=True)

    def _guarded(self, hook: str, handler: Callable[[Any, HookContext], None],
                 context: HookContext) -> None:
        """Telemetry never fails a run: an error here is logged (once per hook
        at WARNING, then at DEBUG) and the hook reports success."""
        pipeline = self._ensure_pipeline()
        if pipeline is None or pipeline.tracer is None:
            return
        try:
            with self._lock:
                self._sweep(pipeline)
                handler(pipeline, context)
        except Exception as error:  # noqa: BLE001 - see docstring
            if hook in self._failures_reported:
                logger.debug("otel: %s failed: %s", hook, error, exc_info=True)
            else:
                self._failures_reported.add(hook)
                logger.warning("otel: %s failed, its spans are missing (%s: %s); "
                               "reported once per hook", hook, type(error).__name__, error,
                               exc_info=True)

    # ------------------------------------------------------------- LLM spans

    def _record_llm_call(self, pipeline: Any, ctx: HookContext) -> None:
        # The client's own clock when it reported the call (the hooks before
        # this one took their time since); never later than now.
        now_ns = time.time_ns()
        reported_ms = (ctx.metadata or {}).get("timestamp_ms")
        end_ns = now_ns
        if isinstance(reported_ms, (int, float)) and not isinstance(reported_ms, bool) and reported_ms > 0:
            end_ns = min(now_ns, int(reported_ms * 1_000_000))
        duration_ms = ctx.llm_duration_ms
        start_ns = end_ns
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            start_ns = end_ns - int(duration_ms * 1_000_000)
        parent, _ = self._parent_for(pipeline, ctx, start_ns, may_open_run=ctx.agent is not None)

        model = ctx.llm_model or ""
        provider = ctx.llm_provider or "unknown"
        attributes: Dict[str, Any] = {OPERATION: "chat", PROVIDER: provider,
                                      REQUEST_STREAM: bool(ctx.llm_is_streaming)}
        if model:
            attributes[REQUEST_MODEL] = model
        attributes.update(self._identity(ctx))
        attributes.update(self._server_of(ctx.llm_request_url))
        usage = None
        if ctx.llm_usage:
            from agent_system.llm.pricing import normalize_usage
            usage = normalize_usage(ctx.llm_usage)
            attributes[INPUT_TOKENS] = usage.prompt_tokens
            attributes[OUTPUT_TOKENS] = usage.completion_tokens
            attributes[CACHE_READ_TOKENS] = usage.cached_tokens
            attributes[CACHE_CREATION_TOKENS] = usage.cache_write_tokens
        error_type = description = None
        if ctx.llm_error:
            # The error text may echo what was sent; without capture_content
            # only the HTTP status it names leaves the process.
            status = _HTTP_STATUS.search(str(ctx.llm_error))
            error_type = status.group(1) if status else "_OTHER"
            attributes[ERROR_TYPE] = error_type
            if ctx.llm_finish_reason == "retry":
                attributes[LLM_RETRY] = True
            if self.settings.capture_content:
                description = cap_text(str(ctx.llm_error), _ERROR_TEXT_CHARS)
            else:
                description = f"LLM request failed (HTTP {status.group(1)})" if status else "LLM request failed"
        elif ctx.llm_finish_reason:
            attributes[FINISH_REASONS] = [str(ctx.llm_finish_reason)]

        span = pipeline.tracer.start_span(f"chat {model}".strip(), context=parent,
                                          kind=SpanKind.CLIENT, attributes=attributes,
                                          start_time=start_ns)
        if description is not None:
            span.set_status(Status(StatusCode.ERROR, description))
        span.end(end_time=end_ns)

        metric_attributes = {OPERATION: "chat", PROVIDER: provider}
        if model:
            metric_attributes[REQUEST_MODEL] = model
        if pipeline.operation_duration is not None:
            recorded = dict(metric_attributes)
            if error_type:
                recorded[ERROR_TYPE] = error_type
            pipeline.operation_duration.record((end_ns - start_ns) / 1e9, recorded)
        if pipeline.token_usage is not None and usage is not None:
            pipeline.token_usage.record(usage.prompt_tokens, {**metric_attributes, TOKEN_TYPE: "input"})
            pipeline.token_usage.record(usage.completion_tokens, {**metric_attributes, TOKEN_TYPE: "output"})

    # ------------------------------------------------------------ tool spans

    @staticmethod
    def _call_key(ctx: HookContext) -> Tuple[Any, ...]:
        call = ctx.tool_call or {}
        return (ctx.request_id or "", ctx.step, call.get("id"), call.get("name"))

    def _add_pending(self, call: _PendingCall) -> None:
        self._pending_seq += 1
        self._pending[self._pending_seq] = call
        self._pending_by_key.setdefault(call.key, deque()).append(self._pending_seq)

    def _drop_pending(self, seq: int) -> _PendingCall:
        call = self._pending.pop(seq)
        queue = self._pending_by_key.get(call.key)
        if queue is not None:
            try:
                queue.remove(seq)
            except ValueError:
                pass
            if not queue:
                del self._pending_by_key[call.key]
        return call

    def _take_pending(self, key: Tuple[Any, ...]) -> Optional[_PendingCall]:
        """The oldest waiting call under ``key`` (post hooks come in call order)."""
        queue = self._pending_by_key.get(key)
        if not queue:
            return None
        return self._drop_pending(queue[0])

    def _forget(self, call: _PendingCall) -> None:
        self._forgotten[call.key] = None
        while len(self._forgotten) > self.settings.max_pending_tool_calls:
            self._forgotten.popitem(last=False)

    def _open_tool_call(self, pipeline: Any, ctx: HookContext) -> None:
        call = ctx.tool_call or {}
        source = call.get("source") or "model"
        now_ns = time.time_ns()
        parent, run_id = self._parent_for(pipeline, ctx, now_ns,
                                          may_open_run=source == "model" and ctx.agent is not None)
        self._add_pending(_PendingCall(
            key=self._call_key(ctx), run_id=run_id, parent=parent, call_id=call.get("id"),
            name=str(call.get("name") or ""), server=str(call.get("server") or ""), source=source,
            started_ns=now_ns,
            arguments=self._content(call.get("arguments")) if self.settings.capture_content else None,
            last_seen=self._clock(), identity=self._identity(ctx)))
        while len(self._pending) > self.settings.max_pending_tool_calls:
            oldest = self._drop_pending(next(iter(self._pending)))
            self._emit_unresolved(pipeline, oldest, "unknown")
            self._forget(oldest)

    def _close_tool_call(self, pipeline: Any, ctx: HookContext) -> None:
        call = ctx.tool_call or {}
        outcome_of = ctx.tool_result or {}
        key = self._call_key(ctx)
        pending = self._take_pending(key)
        if pending is None and key in self._forgotten:
            del self._forgotten[key]  # reported as "unknown" already
            return
        source = call.get("source") or "model"
        end_ns = _ns(outcome_of.get("finished_at")) or time.time_ns()
        start_ns = (_ns(outcome_of.get("started_at"))
                    or (pending.started_ns if pending is not None else end_ns))
        start_ns = min(start_ns, end_ns)
        if pending is not None:
            parent, run_id = pending.parent, pending.run_id
            self._touch_id(run_id)
        else:
            parent, run_id = self._parent_for(pipeline, ctx, start_ns,
                                              may_open_run=source == "model" and ctx.agent is not None)

        result = outcome_of.get("result")
        if isinstance(result, dict) and result.get("cancelled") is True:
            outcome = "cancelled"
        elif outcome_of.get("is_error"):
            outcome = "error"
        else:
            outcome = "ok"
        attributes = self._tool_attributes(call.get("name"), call.get("id"), call.get("server"),
                                           source, outcome, self._identity(ctx))
        if self.settings.capture_content:
            attributes[TOOL_ARGUMENTS] = self._content(call.get("arguments"))
            attributes[TOOL_RESULT] = self._content(result)
        description = None
        if outcome != "ok":
            error_type = result.get("type") if isinstance(result, dict) else None
            attributes[ERROR_TYPE] = (error_type if _is_identifier(error_type)
                                      else ("cancelled" if outcome == "cancelled" else "tool_error"))
            description = f"tool call {outcome}"
            if self.settings.capture_content and isinstance(result, dict) and result.get("error"):
                description = cap_text(str(result["error"]), self.settings.content_max_chars)
        self._emit_tool_span(pipeline, parent, attributes, start_ns, end_ns, description)
        self._mark_reported(run_id, call.get("id"))

    def _tool_attributes(self, name: Any, call_id: Any, server: Any, source: str, outcome: str,
                         identity: Dict[str, Any]) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {OPERATION: "execute_tool", TOOL_NAME: str(name or ""),
                                      TOOL_TYPE: "function", TOOL_SERVER: str(server or ""),
                                      TOOL_SOURCE: source, TOOL_OUTCOME: outcome}
        if call_id:
            attributes[TOOL_CALL_ID] = str(call_id)
        attributes.update(identity)
        return attributes

    @staticmethod
    def _emit_tool_span(pipeline: Any, parent: Any, attributes: Dict[str, Any], start_ns: int,
                        end_ns: int, error_description: Optional[str]) -> None:
        span = pipeline.tracer.start_span(f"execute_tool {attributes[TOOL_NAME]}".strip(),
                                          context=parent, kind=SpanKind.INTERNAL,
                                          attributes=attributes, start_time=start_ns)
        if error_description is not None:
            span.set_status(Status(StatusCode.ERROR, error_description))
        span.end(end_time=end_ns)

    def _emit_unresolved(self, pipeline: Any, call: _PendingCall, outcome: str) -> None:
        """A call no post hook reported: blocked, cancelled before it ran, or
        unknown (the run crashed, the record expired or was evicted). It did
        not run, so its span has no duration."""
        attributes = self._tool_attributes(call.name, call.call_id, call.server, call.source,
                                           outcome, call.identity)
        if call.arguments is not None:
            attributes[TOOL_ARGUMENTS] = call.arguments
        description = None
        if outcome == "blocked":
            attributes[ERROR_TYPE] = _BLOCKED_TYPE
            description = "blocked by a pre_tool_call hook"
        elif outcome == "cancelled":
            attributes[ERROR_TYPE] = "cancelled"
            description = "cancelled before it ran"
        self._emit_tool_span(pipeline, call.parent, attributes, call.started_ns, call.started_ns,
                             description)
        self._mark_reported(call.run_id, call.call_id)

    def _mark_reported(self, run_id: Optional[str], call_id: Any) -> None:
        run = self._runs.get(run_id) if run_id else None
        if run is None or run.reported_calls is None or not call_id:
            return
        if len(run.reported_calls) >= self.settings.max_pending_tool_calls:
            run.reported_calls = None  # too many to remember: no blocked-call scan for this run
            return
        run.reported_calls.add(call_id)

    # -------------------------------------------------------------- run spans

    def _identity(self, ctx: HookContext) -> Dict[str, Any]:
        identity: Dict[str, Any] = {}
        if ctx.agent_name:
            identity[AGENT_NAME] = ctx.agent_name
        if ctx.session_id:
            # The conversation is the person's: an agent called as a tool runs on a session of its own below its
            # caller's, and its spans belong to the conversation at the top of that chain (callers_session) --
            # with its own session id as the session the run ran on. On the session alone, a backend grouping by
            # conversation split one into pieces.
            identity[CONVERSATION_ID] = callers_session(ctx.session_id)
            identity[SESSION_ID] = ctx.session_id
        if ctx.request_id:
            identity[REQUEST_ID] = ctx.request_id
        if self.settings.capture_user_id and ctx.user_id:
            identity[USER_ID] = ctx.user_id
        return identity

    @staticmethod
    def _server_of(url: Optional[str]) -> Dict[str, Any]:
        """Host and port only: a request URL may carry a key in its query."""
        if not url:
            return {}
        try:
            parts = urlsplit(url)
            server: Dict[str, Any] = {SERVER_ADDRESS: parts.hostname} if parts.hostname else {}
            if parts.port:
                server[SERVER_PORT] = parts.port
            return server
        except ValueError:
            return {}

    def _parent_for(self, pipeline: Any, ctx: HookContext, start_ns: int,
                    may_open_run: bool) -> Tuple[Any, Optional[str]]:
        """(the OTel context to parent a span on, the request id of its run).

        The run of the request id itself; a run already ended (its context
        stays a valid parent) -- unless a new run holds the id now; otherwise
        the run it descends from: tool and sub-agent request ids extend their
        run's (``<id>_007``, ``<id>_007_sub_...``, tool_script's
        ``<id>_007_ts01``). A new run is opened only for an event of an agent's
        own loop.
        """
        request_id = ctx.request_id or ""
        if not request_id:
            return None, None
        run = self._runs.get(request_id)
        if run is not None and may_open_run and run.token is not None:
            live = _run_token(request_id)
            if live is not None and live is not run.token:
                # Its end never reached this plugin (its session_end hook was
                # off or failed), and a new run holds the id now.
                del self._runs[request_id]
                self._end_run(run, "evicted", "its request id was taken by a new run "
                                              "before its end was observed")
                run = None
        if run is not None:
            self._touch(run)
            return run.context, request_id
        ended = self._ended.get(request_id)
        if ended is not None:
            live = _run_token(request_id)
            if not (may_open_run and live is not None and live is not ended.token):
                return ended.context, request_id
            # A job dispatched again under its id: the id's token is another
            # run's now, and this event is that run's.
            del self._ended[request_id]
        parent, parent_id = self._ancestor(request_id)
        if may_open_run:
            run = self._open_run(pipeline, ctx, start_ns, parent, parent_id)
            return run.context, request_id
        return parent, parent_id

    def _ancestor(self, request_id: str) -> Tuple[Any, Optional[str]]:
        candidate = request_id
        for _ in range(_MAX_ANCESTOR_STEPS):
            if "_" not in candidate:
                break
            candidate = candidate.rsplit("_", 1)[0]
            run = self._runs.get(candidate)
            if run is not None:
                self._touch(run)
                return run.context, candidate
            ended = self._ended.get(candidate)
            if ended is not None:
                return ended.context, candidate
        return None, None

    def _touch(self, run: _Run) -> None:
        """An event of ``run``: it and every open run it nests in are alive (a
        parent waits in the tool call that runs the sub-agent)."""
        now = self._clock()
        for _ in range(_MAX_ANCESTOR_STEPS):
            run.last_seen = now
            self._runs.move_to_end(run.request_id)
            parent = self._runs.get(run.parent_id) if run.parent_id else None
            if parent is None:
                return
            run = parent

    def _touch_id(self, request_id: Optional[str]) -> None:
        run = self._runs.get(request_id) if request_id else None
        if run is not None:
            self._touch(run)

    def _start_run_span(self, pipeline: Any, ctx: HookContext, start_ns: int, parent: Any) -> Any:
        attributes: Dict[str, Any] = {OPERATION: "invoke_agent"}
        attributes.update(self._identity(ctx))
        return pipeline.tracer.start_span(f"invoke_agent {ctx.agent_name}".strip(), context=parent,
                                          kind=SpanKind.INTERNAL, attributes=attributes,
                                          start_time=start_ns)

    def _open_run(self, pipeline: Any, ctx: HookContext, start_ns: int, parent: Any,
                  parent_id: Optional[str]) -> _Run:
        span = self._start_run_span(pipeline, ctx, start_ns, parent)
        run = _Run(request_id=ctx.request_id, span=span,
                   context=trace_api.set_span_in_context(span), last_seen=self._clock(),
                   parent_id=parent_id if parent_id in self._runs else None,
                   token=_run_token(ctx.request_id))
        self._runs[run.request_id] = run
        while len(self._runs) > self.settings.max_open_runs:
            _, oldest = self._runs.popitem(last=False)
            self._end_run(oldest, "evicted",
                          f"run end not observed: more than {self.settings.max_open_runs} runs open")
        return run

    def _end_run(self, run: _Run, outcome: str, description: Optional[str] = None,
                 end_ns: Optional[int] = None) -> None:
        run.span.set_attribute(RUN_OUTCOME, outcome)
        if outcome != "completed":
            run.span.set_attribute(ERROR_TYPE, "_OTHER" if outcome == "error" else outcome)
            run.span.set_status(Status(StatusCode.ERROR, description or f"run {outcome}"))
        run.span.end(end_time=end_ns)
        self._ended[run.request_id] = _EndedRun(
            context=trace_api.set_span_in_context(trace_api.NonRecordingSpan(run.span.get_span_context())),
            token=run.token, closed=outcome in ("completed", "error", "cancelled", "incomplete"))
        self._ended.move_to_end(run.request_id)
        while len(self._ended) > self.settings.max_open_runs:
            self._ended.popitem(last=False)

    def _close_run(self, pipeline: Any, ctx: HookContext) -> None:
        request_id = ctx.request_id or ""
        if not request_id:
            return
        metadata = ctx.metadata or {}
        messages = list(ctx.messages or ())
        run_messages = self._run_messages(messages, request_id)
        cancelled = bool(metadata.get("cancelled"))
        run = self._runs.get(request_id)
        if run is None:
            ended = self._ended.get(request_id)
            if ended is not None and not ended.closed:
                # Its span ended already (expired, evicted); what it left
                # waiting is settled, its outcome has no span to go on.
                self._resolve_pending(pipeline, request_id, run_messages, cancelled)
                ended.closed = True  # a later session_end under the id is another run's
                return
            # A run no LLM or tool event opened: it failed on its way in, the
            # client reports no calls, or it reused the id of a run that ended.
            # Timed from its opening message.
            opening = run_messages[0] if run_messages else None
            start_ns = _stamp_ns(_field(opening, "timestamp")) or time.time_ns()
            parent, parent_id = self._ancestor(request_id)
            span = self._start_run_span(pipeline, ctx, start_ns, parent)
            run = _Run(request_id=request_id, span=span,
                       context=trace_api.set_span_in_context(span), last_seen=self._clock(),
                       parent_id=parent_id if parent_id in self._runs else None)
            self._runs[request_id] = run
        # Still registered while its calls are resolved: they mark themselves
        # reported on it, and the scan for blocked calls must skip those.
        self._resolve_pending(pipeline, request_id, run_messages, cancelled)
        self._report_blocked(pipeline, run, run_messages, ctx)
        self._runs.pop(request_id, None)

        errors = [str(e) for e in metadata.get("errors") or () if e]
        run.span.set_attribute(RUN_PERSISTED, bool(metadata.get("persisted")))
        if self.settings.capture_content:
            self._attach_run_content(run.span, run_messages)
        if errors:
            # A crash text may quote content (a validation error quotes its input).
            self._end_run(run, "error", cap_text(errors[0], _ERROR_TEXT_CHARS)
                          if self.settings.capture_content else f"run reported {len(errors)} error(s)")
        elif cancelled:
            self._end_run(run, "cancelled", "run cancelled")
        elif metadata.get("completed") is False:
            self._end_run(run, "incomplete", "run ended without a final answer")
        else:
            self._end_run(run, "completed")

    @staticmethod
    def _run_messages(messages: List[Any], request_id: str) -> List[Any]:
        """The run's own messages: from the one that opened it (it carries the
        run's request id) to the end -- the last such one, should an id have
        been used again in this session. Empty if it is not there."""
        for index in range(len(messages) - 1, -1, -1):
            if _field(messages[index], "request_id") == request_id:
                return messages[index:]
        return []

    def _resolve_pending(self, pipeline: Any, request_id: str, run_messages: List[Any],
                         cancelled: bool) -> None:
        """The run's calls whose post hook never came, told apart by their
        result message: blocked, cancelled, or -- no result to go by -- the
        run's own outcome."""
        mine = [seq for seq, call in self._pending.items() if call.run_id == request_id]
        if not mine:
            return
        results = {_field(m, "tool_call_id"): m for m in run_messages if _field(m, "role") == "tool"}
        for seq in mine:
            call = self._drop_pending(seq)
            outcome = self._outcome_of_message(results.get(call.call_id)) if call.call_id else None
            if outcome is None:
                # The run's flag, not the call's token: that is the run's, and a
                # crash cancels it too (the calls of a crashed run are unknown).
                outcome = "cancelled" if cancelled else "unknown"
            self._emit_unresolved(pipeline, call, outcome)
            # A post may still come: a tool_script call runs on in its thread
            # after its run was cancelled.
            self._forget(call)

    @staticmethod
    def _outcome_of_message(message: Any) -> Optional[str]:
        content = _field(message, "content")
        if not isinstance(content, str):
            return None
        try:
            data = json.loads(content)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("type") == _BLOCKED_TYPE:
            return "blocked"
        if data.get("cancelled") is True:
            return "cancelled"
        return None

    def _report_blocked(self, pipeline: Any, run: _Run, run_messages: List[Any],
                        ctx: HookContext) -> None:
        """Blocked calls this plugin's pre hook never saw (a hook ordered
        before it blocked them): found by their blocked result message. Only
        results of calls the model named by id -- a call sent without an id
        gets a made-up one in its result and cannot be matched to its pre
        hook, so it is not reported a second time here."""
        if run.reported_calls is None:
            return
        names: Dict[str, str] = {}
        for message in run_messages:
            role = _field(message, "role")
            if role == "assistant":
                for tool_call in _field(message, "tool_calls") or ():
                    if isinstance(tool_call, dict) and tool_call.get("id"):
                        names[tool_call["id"]] = str((tool_call.get("function") or {}).get("name") or "")
            elif role == "tool":
                call_id = _field(message, "tool_call_id")
                if not call_id or call_id not in names or call_id in run.reported_calls:
                    continue
                if self._outcome_of_message(message) != "blocked":
                    continue
                at_ns = _stamp_ns(_field(message, "timestamp")) or time.time_ns()
                call = _PendingCall(
                    key=(run.request_id, None, call_id, names[call_id]), run_id=run.request_id,
                    parent=run.context, call_id=call_id, name=names[call_id], server="",
                    source="model", started_ns=at_ns, arguments=None,
                    last_seen=self._clock(), identity=self._identity(ctx))
                self._emit_unresolved(pipeline, call, "blocked")

    def _attach_run_content(self, span: Any, run_messages: List[Any]) -> None:
        """The run's input (the message that opened it) and output (its last
        answer), in the GenAI message format, each capped."""
        if not run_messages:
            return
        opening = run_messages[0]
        text = _text_of(_field(opening, "content"))
        if text:
            span.set_attribute(INPUT_MESSAGES, self._content(
                [{"role": _field(opening, "role") or "user", "parts": [{"type": "text", "content": text}]}]))
        for message in reversed(run_messages[1:]):
            if _field(message, "role") != "assistant":
                continue
            answer = _text_of(_field(message, "content"))
            if answer:
                span.set_attribute(OUTPUT_MESSAGES, self._content(
                    [{"role": "assistant", "parts": [{"type": "text", "content": answer}]}]))
                break

    # ------------------------------------------------------------ bookkeeping

    def _content(self, value: Any) -> str:
        """A content attribute: JSON (or the text itself), at most
        ``content_max_chars`` characters."""
        limit = self.settings.content_max_chars
        if isinstance(value, str):
            return cap_text(value, limit)
        try:
            text = json.dumps(_shrink(value, limit), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)[:limit + 1]
        return cap_text(text, limit)

    def _sweep(self, pipeline: Any) -> None:
        """End runs without an event (of their own or of a run nested in them)
        for ``idle_timeout_seconds``, then report waiting calls as old whose
        run is gone. A call whose run is still alive keeps waiting: a parent
        run waits in the tool call that runs its sub-agent."""
        horizon = self._clock() - self.settings.idle_timeout_seconds
        while self._runs:
            run = next(iter(self._runs.values()))
            if run.last_seen >= horizon:
                break
            self._runs.popitem(last=False)
            self._end_run(run, "expired", f"run end not observed within "
                                          f"{self.settings.idle_timeout_seconds:g}s of its last event")
        expired = []
        for seq, call in self._pending.items():
            if call.last_seen >= horizon:
                break
            if call.run_id is None or call.run_id not in self._runs:
                expired.append(seq)
        for seq in expired:
            call = self._drop_pending(seq)
            self._emit_unresolved(pipeline, call, "unknown")
            self._forget(call)

    def _end_everything(self, pipeline: Any) -> None:
        while self._pending:
            call = self._drop_pending(next(iter(self._pending)))
            self._emit_unresolved(pipeline, call, "unknown")
        while self._runs:
            _, run = self._runs.popitem(last=False)
            self._end_run(run, "shutdown", "the plugin stopped before the run ended")
