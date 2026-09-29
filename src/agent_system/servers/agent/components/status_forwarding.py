"""
Status Event Forwarding for Agent Server

Simplified architecture: Direct synchronous handler without background task.
Events are pushed directly to the list by the StatusBus handler - no queue, no task, no race conditions.
The run events of runs started under this one are appended to the same list, as `sub_run`
envelopes, by relay_run_event (called from Agent.run_events).
"""
import logging
from typing import List, Dict, Any, Optional, Set

from ....tools.status import status_bus, StatusHandler, StatusEvent
from ....utils.tree_hierarchy import parse_request_id_hierarchy

logger = logging.getLogger(__name__)

# Every forwarder that is streaming a run right now, for the run events of the runs
# started under it (see relay_run_event).
_live_forwarders: Set["StatusEventForwarder"] = set()

# Not relayed: status lines reach every ancestor over the status bus already, a
# sub_run envelope was relayed by the run that produced it, and a heartbeat only
# keeps ONE connection alive.
_NOT_RELAYED = frozenset({"status", "sub_run", "heartbeat"})


def relay_run_event(forwarder: "StatusEventForwarder", event: Dict[str, Any], agent_name: str) -> None:
    """Hand one event of a run to the runs streaming above it (the run's `forwarder.listeners`).

    A sub-agent's run is consumed by whoever started it (sub_agent_manager reads it
    for its activity line and its answer), so its thinking, its steps and its tool
    calls never reached the stream of the run the viewer watches -- only its status
    lines did, because those travel over the process-wide status bus. This is the
    same path for the rest: the prefix a sub-run's id carries is the one the status
    handler already filters by.

    Said by the run itself rather than by the plugin that started it, so it holds for
    whatever starts runs under a run, and at any depth. Who is above was settled when
    the run started (StatusEventForwarder.start_forwarding), so it still holds for the
    run's last events, which come after its forwarder has stopped.
    """
    if not forwarder.request_id or event.get("type") in _NOT_RELAYED:
        return
    listeners = [f for f in forwarder.listeners if f in _live_forwarders]
    if not listeners:
        return
    hierarchy = parse_request_id_hierarchy(forwarder.request_id)
    for listener in listeners:
        listener.add_sub_run_event(forwarder.request_id, hierarchy, agent_name, event)


def in_line_with_a_live_run(request_id: str) -> bool:
    """Whether `request_id` extends the id of a run streaming now by `_…`, or one extends it.

    Either way the two would be taken for a run and its sub-run: the status handler
    and the relay both go by that prefix. The server names sub-runs so; an id a
    caller chooses must stay out of line with every live run.
    """
    return any(_is_above(f, request_id) or f.request_id.startswith(f"{request_id}_")
               for f in _live_forwarders if f.request_id)


def attended_stream_of(request_id: str, grace: float = 0.0) -> Optional[str]:
    """The id of the run stream a person watches that a status line of
    ``request_id`` reaches now, or None when none does.

    A line reaches the stream of every run streaming now whose id is ``request_id``
    or a prefix of it at a ``_`` (DirectStatusHandler's rule): a sub-agent's lines
    reach the stream of the run that started it. That stream is watched when the
    client that started its run said so (``request_context.set_run_attended``). A
    run whose stream has ended reaches nobody any more -- an async sub-agent that
    outlives the run above it asks no one, whatever that run's client was -- and
    neither does a run that goes on as a job nobody has read for ``grace``
    seconds (the tab was closed; a reload reads it again within moments). A run
    streamed inline, with no job, is read for as long as it runs.
    """
    if not request_id:
        return None
    from ....core.request_context import run_is_attended
    from ....services.background_job_manager import get_background_job_manager
    for forwarder in list(_live_forwarders):
        run_id = forwarder.request_id
        if run_id and (request_id == run_id or request_id.startswith(f"{run_id}_")) \
                and run_is_attended(run_id):
            unread = get_background_job_manager().unread_for(run_id)
            if unread is None or unread == 0.0 or unread < grace:
                return run_id
    return None


def run_is_live(request_id: str) -> bool:
    """Whether the run ``request_id`` streams now, or a run started under it
    does (an async sub-agent outlives the run that started it)."""
    if not request_id:
        return False
    return any(f.request_id and (f.request_id == request_id or f.request_id.startswith(f"{request_id}_"))
               for f in list(_live_forwarders))


def run_streams(request_id: str) -> bool:
    """Whether the run ``request_id`` ITSELF streams now -- unlike run_is_live,
    not counting runs whose ids merely extend it: a client may name its next
    run "job_002" after "job", and that one streaming says nothing about "job"."""
    return bool(request_id) and any(f.request_id == request_id for f in list(_live_forwarders))


def _is_above(forwarder: "StatusEventForwarder", request_id: str) -> bool:
    """Whether `request_id` reads as a run started under the one `forwarder` streams."""
    return bool(forwarder.request_id) and request_id.startswith(f"{forwarder.request_id}_")


class DirectStatusHandler(StatusHandler):
    """Handler that directly appends events to a list (no queue, no task)."""
    
    def __init__(self, request_id: str, events_list: List[Dict[str, Any]]):
        self.request_id = request_id
        self.events_list = events_list
    
    async def process(self, event: StatusEvent) -> None:
        """Directly append matching events to the list."""
        # Filter: only events for this request (exact match or prefix for sub-requests).
        # One that names no request is nobody's and is dropped: let through, it reached
        # every run's stream in the process, whoever that run belonged to.
        event_request_id = event.request_id
        if not event_request_id:
            return
        if self.request_id and not (event_request_id == self.request_id or
                                    event_request_id.startswith(f"{self.request_id}_")):
            return  # Not our event
        
        # Convert to SSE format and append directly
        status_sse_event = {
            "type": "status",
            "server": event.server,
            "request_id": event.request_id,
            "message": event.message,
            "phase": event.phase.value,
            "level": event.level,
            "timestamp": event.timestamp.isoformat(),
            "meta": event.meta or {},
            # The same tree SSEStatusHandler sends. Without it the page never learns
            # that one operation ran under another: a sub-agent's lines sat flat
            # between the parent's own, and the whole nesting half of the status
            # display (depth, connectors, collapsing a sub-tree) was inert, because
            # this is the handler the run's own stream goes through.
            "tree": {
                "parent_id": event.parent_id,
                "depth_level": event.depth_level,
                "child_count": event.child_count,
                "is_leaf": event.is_leaf,
            },
        }
        self.events_list.append(status_sse_event)

class StatusEventForwarder:
    """Manages forwarding of status events to SSE streams.
    
    Simplified design: Uses a direct handler that synchronously appends to the events list.
    No background task, no queue, no race conditions.
    """

    def __init__(self):
        self.request_id: Optional[str] = None
        # The streams this run's events are relayed to, settled when it starts.
        self.listeners: List["StatusEventForwarder"] = []
        self.status_events_to_forward: List[Dict[str, Any]] = []
        self._handler: Optional[DirectStatusHandler] = None
        # run id -> that run's token-delta envelope still in the queue, the one the
        # next delta of the same call is folded into (see add_sub_run_event).
        self._open_deltas: Dict[str, Dict[str, Any]] = {}

    async def start_forwarding(self, request_id: str) -> None:
        """Start forwarding status events for a specific request."""
        self.request_id = request_id
        # Above this run: the nearest run streaming now whose id it extends, and the
        # runs above that one. Settled now, not per event, because "above" is a
        # question of when as much as of ids: a stream begun later under an id this
        # one extends is another run -- the writer dispatches a job again under its
        # old id while a sub-agent of the old run is still at work, and neither that
        # sub-agent nor anything it starts afterwards is the new run's.
        nearest = max((f for f in _live_forwarders if _is_above(f, request_id)),
                      key=lambda f: len(f.request_id), default=None)
        self.listeners = [*nearest.listeners, nearest] if nearest else []
        self.status_events_to_forward.clear()
        self._open_deltas.clear()
        
        # Create and register direct handler
        self._handler = DirectStatusHandler(request_id, self.status_events_to_forward)
        status_bus.add_handler(self._handler)
        _live_forwarders.add(self)
        logger.debug("Request %s registered direct status handler", request_id)

    async def stop_forwarding(self) -> None:
        """Stop forwarding and cleanup."""
        _live_forwarders.discard(self)
        if self._handler:
            status_bus.remove_handler(self._handler)
            logger.debug("Request %s removed direct status handler", self.request_id)
            self._handler = None

    def add_sub_run_event(self, run_id: str, hierarchy: Dict[str, Any], agent_name: str,
                          event: Dict[str, Any]) -> None:
        """Queue one event of a run started under this one, wrapped as `sub_run`.

        `spawned_by` is the id the run hangs from (the parent in its id): the tool call
        that started it for a sub_agent_manager run (`<call>_sub_`, `_async_`,
        `_sub_cont_`), the caller's run for an agent called as a tool (whose run id is
        the call's own), the run it retries for a `_minlen_` retry. The page puts the
        run where it was started, not where its events happen to arrive: an async
        sub-agent's lines are delivered while the parent waits for it, steps later.

        Token streams are folded into the run's own delta still in the queue: three
        sub-agents reasoning at once would otherwise put one queued event per token
        into the stream. Per run, not "the last entry": sub-agents running side by side
        take turns token by token, and with a single last entry nothing would ever
        fold. A reasoning delta extends the previous one of the same call; an answer
        delta carries the whole answer so far, so the newest replaces the one before.
        Anything else the run says closes its delta, so nothing it says later is moved
        in front of what it said in between.
        """
        kind = event.get("type")
        open_delta = self._open_deltas.get(run_id)
        if (open_delta is not None and kind in ("reasoning_delta", "thinking_delta")
                and open_delta["event"].get("type") == kind
                and open_delta["event"].get("step") == event.get("step")):
            if kind == "reasoning_delta":
                open_delta["event"]["delta"] = (open_delta["event"].get("delta") or "") + (event.get("delta") or "")
            else:
                open_delta["event"] = dict(event)
            return
        copied = dict(event)
        if isinstance(copied.get("assistant"), dict):
            # Delivered to every reader, long after the run yielded it -- and the
            # run goes on using that dict (thinking_complete's is the one it keeps).
            copied["assistant"] = dict(copied["assistant"])
        envelope = {
            "type": "sub_run",
            "run_id": run_id,
            "spawned_by": hierarchy.get("parent_id"),
            "depth_level": hierarchy.get("depth", 0),
            "agent": agent_name,
            # A copy: the delta above is extended in place, and the dict the run
            # yielded is its consumer's.
            "event": copied,
        }
        self.status_events_to_forward.append(envelope)
        if kind in ("reasoning_delta", "thinking_delta"):
            self._open_deltas[run_id] = envelope
        else:
            self._open_deltas.pop(run_id, None)

    def get_pending_events(self) -> List[Dict[str, Any]]:
        """Get and clear all pending status events.
        
        This is now reliable - events are added synchronously by the handler
        during StatusBus.publish(), so they're immediately available.
        """
        events = self.status_events_to_forward.copy()
        self.status_events_to_forward.clear()
        self._open_deltas.clear()  # handed on: what follows is folded into nothing sent already
        return events

    async def drain_pending_events(self, max_wait_ms: float = 100) -> List[Dict[str, Any]]:
        """Drain all pending events.

        With the direct handler design, events are immediately available.
        This method is kept for API compatibility but simply returns get_pending_events().
        """
        # Small yield to let any in-flight publish() calls complete
        import asyncio
        await asyncio.sleep(0.001)
        return self.get_pending_events()