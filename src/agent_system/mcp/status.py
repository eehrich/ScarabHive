from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Iterable
import asyncio
import re
import logging
import os
import time

logger = logging.getLogger(__name__)


PHASE_START = "start"
PHASE_PROGRESS = "progress"
PHASE_END = "end"
PHASE_ERROR = "error"
VALID_PHASES = {PHASE_START, PHASE_PROGRESS, PHASE_END, PHASE_ERROR}


@dataclass
class StatusEvent:
        """Represents a status update event from an MCP server.

        Unified schema (Task 0186):
            - server: plugin / server name (str)
            - request_id: optional correlation id (str|None)
            - phase: one of start|progress|end|error (str)
            - message: human-readable short status (str)
            - level: info|warning|error (str) (orthogonal to phase)
            - timestamp: ISO8601 moment of emission (datetime)
            - meta: optional structured details (dict|None)
        """
        server: str
        request_id: Optional[str]
        message: str
        timestamp: datetime
        phase: str = PHASE_PROGRESS
        level: str = "info"  # info, warning, error
        meta: Optional[dict] = field(default=None)

        def to_dict(self) -> dict:
                return {
                        "server": self.server,
                        "request_id": self.request_id,
                        "message": self.message,
                        "timestamp": self.timestamp.isoformat(),
                        "phase": self.phase,
                        "level": self.level,
                        "meta": self.meta,
                }


class StatusBus:
    """Lightweight async pub/sub bus for MCP status events.

    Allows subscribers to filter events by server and/or request_id.
    """

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._filters: list[dict] = []

    async def subscribe(
        self,
        server: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> asyncio.Queue:
        """Subscribe to status events with optional filtering.

        Args:
            server: Filter events to specific server (None for all)
            request_id: Filter events to specific request (None for all)

        Returns:
            AsyncQueue that will receive matching StatusEvents
        """
        queue: asyncio.Queue[StatusEvent] = asyncio.Queue()
        self._subscribers.append(queue)
        self._filters.append({"server": server, "request_id": request_id})
        logger.debug(f"New subscriber added. Total subscribers: {len(self._subscribers)}")
        return queue

    async def publish(self, event: StatusEvent) -> None:
        """Publish a status event to all matching subscribers.

        Args:
            event: The status event to publish
        """
        published_count = 0
        # Diagnostic: log subscriber count and filters to help debug delivery
        try:
            logger.debug("StatusBus.publish(): subscribers=%s filters=%s event_server=%s request_id=%s", len(self._subscribers), self._filters, event.server, event.request_id)
        except Exception:
            logger.debug("StatusBus.publish(): subscribers=%s event_server=%s", len(self._subscribers), event.server)
        for i, queue in enumerate(self._subscribers):
            filter_ = self._filters[i]
            if filter_["server"] and event.server != filter_["server"]:
                continue
            if filter_["request_id"] and event.request_id != filter_["request_id"]:
                continue

            try:
                await queue.put(event)
                published_count += 1
            except Exception as e:
                logger.warning(f"Failed to publish event to subscriber {i}: {e}")

        logger.debug(f"Published status event to {published_count} subscribers")
        _metrics["delivered"] += 1 if published_count > 0 else 0

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber.

        Args:
            queue: The queue returned by subscribe()
        """
        if queue in self._subscribers:
            idx = self._subscribers.index(queue)
            self._subscribers.pop(idx)
            self._filters.pop(idx)
            logger.debug(
                "Subscriber removed. Total subscribers: %s", len(self._subscribers)
            )

    def get_subscriber_count(self) -> int:
        """Get the number of active subscribers."""
        return len(self._subscribers)


############################################################
# Rate limiting / debounce / metrics (Tasks 0227 & 0228)
############################################################

# Simple metrics registry (not exhaustive, but enough for /status/meta)
_metrics: dict[str, int] = {
    "publish_attempted": 0,
    "delivered": 0,
    "suppressed_rate": 0,
    "suppressed_debounce": 0,
    "redacted": 0,
}

_server_rate: dict[str, tuple[float, int]] = {}
_last_event_signature: dict[tuple[str, str | None, str], float] = {}

def _config_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _config_patterns() -> list[re.Pattern]:
    """Compile redaction patterns from env var AGENT_STATUS_REDACT_PATTERNS.

    The variable may contain comma-separated regex snippets. Empty / invalid entries are ignored.
    """
    raw = os.getenv("AGENT_STATUS_REDACT_PATTERNS", "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    compiled: list[re.Pattern] = []
    for p in parts:
        try:
            compiled.append(re.compile(p))
        except Exception:
            logger.debug("Invalid redaction pattern skipped: %s", p)
    return compiled

def _redact_message(message: str, patterns: Iterable[re.Pattern], replacement: str) -> tuple[str, int]:
    redactions = 0
    new_msg = message
    for pat in patterns:
        if pat.search(new_msg):
            new_msg, count = pat.subn(replacement, new_msg)
            redactions += count
    return new_msg, redactions

def _redact_meta(meta: Optional[dict], patterns: Iterable[re.Pattern], replacement: str) -> tuple[Optional[dict], int]:
    if not meta:
        return meta, 0
    redactions = 0
    cleaned: dict = {}
    for k, v in meta.items():
        nv = v
        key_lower = str(k).lower()
        # Heuristic key-based masking for sensitive fields
        if any(tok in key_lower for tok in ("token", "secret", "password", "key")):
            if isinstance(v, str) and v:
                nv = replacement
                redactions += 1
        if isinstance(v, str):
            new_v, c = _redact_message(v, patterns, replacement)
            if c:
                nv = new_v
                redactions += c
        cleaned[k] = nv
    return cleaned, redactions

def _should_suppress(server: str, request_id: Optional[str], message: str) -> bool:
    """Return True if this event should be suppressed by rate limit or debounce."""
    # NOTE: publish_attempted is incremented by publish_status to ensure terminal
    # events (PHASE_END/PHASE_ERROR) are accounted for even when suppression
    # checks are bypassed.

    max_rps = _config_int("AGENT_STATUS_MAX_RPS", 0)
    if max_rps > 0:
        win_start, count = _server_rate.get(server, (time.time(), 0))
        now = time.time()
        if now - win_start >= 1.0:
            win_start, count = now, 0
        if count >= max_rps:
            _metrics["suppressed_rate"] += 1
            _server_rate[server] = (win_start, count)  # keep window
            return True
        _server_rate[server] = (win_start, count + 1)

    debounce_ms = _config_int("AGENT_STATUS_DEBOUNCE_MS", 0)
    if debounce_ms > 0:
        sig = (server, request_id, message)
        last_ts = _last_event_signature.get(sig)
        now = time.time()
        if last_ts is not None and (now - last_ts) * 1000 < debounce_ms:
            _metrics["suppressed_debounce"] += 1
            return True
        _last_event_signature[sig] = now

    return False

def get_status_metrics() -> dict:
    """Return a snapshot of status metrics and current config values."""
    return {
        **_metrics,
        "subscribers": status_bus.get_subscriber_count(),
        "config": {
            "AGENT_STATUS_MAX_RPS": _config_int("AGENT_STATUS_MAX_RPS", 0),
            "AGENT_STATUS_DEBOUNCE_MS": _config_int("AGENT_STATUS_DEBOUNCE_MS", 0),
            "AGENT_STATUS_REQUIRE_AUTH": os.getenv("AGENT_STATUS_REQUIRE_AUTH", "0"),
            "AGENT_STATUS_REDACT_PATTERNS": os.getenv("AGENT_STATUS_REDACT_PATTERNS", ""),
        },
    }

# Global status bus instance (declared after helper definitions)
status_bus = StatusBus()


async def publish_status(
    server: str,
    message: str,
    request_id: Optional[str] = None,
    level: str = "info",
    phase: str = PHASE_PROGRESS,
    meta: Optional[dict] = None,
    traceparent: Optional[str] = None,
) -> None:
    """Convenience function to publish a status event.

    Args:
        server: Name of the MCP server
        message: Status message
        request_id: Optional request identifier
        level: Log level (info, warning, error)
    """
    # Normalize / validate phase & level graciously (do not raise to avoid breaking user flows)
    if phase not in VALID_PHASES:
        logger.debug("Invalid phase '%s' provided; defaulting to 'progress'", phase)
        phase = PHASE_PROGRESS
    if level not in ("info", "warning", "error"):
        logger.debug("Invalid level '%s' provided; defaulting to 'info'", level)
        level = "info"
    if phase == PHASE_ERROR and level == "info":
        level = "error"  # escalate sensible default

    # Extract W3C trace id if traceparent provided (format: '00-<trace-id>-<span-id>-<flags>')
    if traceparent and isinstance(traceparent, str):
        try:
            m = re.match(r"^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-[\da-f]{2}$", traceparent.strip())
            if m:
                tid, sid = m.group(1), m.group(2)
                meta = {**(meta or {}), "trace_id": tid, "span_id": sid}
        except Exception:
            pass

    # Redaction (security Task 0229)
    patterns = _config_patterns()
    if patterns:
        replacement = os.getenv("AGENT_STATUS_REDACT_REPLACEMENT", "***")
        new_message, c_msg = _redact_message(message, patterns, replacement)
        if c_msg:
            message = new_message
        meta, c_meta = _redact_meta(meta, patterns, replacement)
        if c_msg or c_meta:
            _metrics["redacted"] += c_msg + c_meta

    # Suppression checks (rate limit & debounce) AFTER redaction so matching doesn't leak originals
    # Always allow terminal events (end/error) through so subscribers receive completion notices.
    # Count this publish attempt for metrics
    _metrics["publish_attempted"] += 1

    if phase not in (PHASE_END, PHASE_ERROR):
        if _should_suppress(server, request_id, message):
            return

    event = StatusEvent(
        server=server,
        request_id=request_id,
        message=message,
        timestamp=datetime.now(),
        phase=phase,
        level=level,
        meta=meta,
    )
    # Publish locally first
    await status_bus.publish(event)

    # Optionally forward to a remote SSE broker via HTTP POST when configured.
    # Use env var AGENT_STATUS_SSE_PUSH_URL to specify the broker publish endpoint
    # (e.g. http://127.0.0.1:8765/status/publish).
    push_url = os.environ.get("AGENT_STATUS_SSE_PUSH_URL")
    if not push_url:
        return

    payload = {
        "server": event.server,
        "request_id": event.request_id,
        "message": event.message,
        "level": event.level,
        "timestamp": event.timestamp.isoformat(),
        "phase": event.phase,
        "meta": event.meta,
    }

    # Fire-and-forget POST to the broker
    async def _post():
        try:
            try:
                import aiohttp
            except Exception:
                logger.debug("aiohttp not available; cannot push status to SSE broker")
                return

            async with aiohttp.ClientSession() as sess:
                async with sess.post(push_url, json=payload, timeout=5) as resp:
                    if resp.status >= 400:
                        logger.debug("Failed to push status to broker %s: %s", push_url, resp.status)
        except Exception as e:
            logger.debug("Exception while pushing status to broker: %s", e)

    try:
        asyncio.create_task(_post())
    except RuntimeError:
        # if there's no running loop, run in new loop in background thread
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_post())
            loop.close()
        except Exception:
            logger.debug("Failed to push status to broker in fallback path")
