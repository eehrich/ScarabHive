"""
Status Event Forwarding for Agent Server

Simplified architecture: Direct synchronous handler without background task.
Events are pushed directly to the list by the StatusBus handler - no queue, no task, no race conditions.
The run events of runs started under this one are appended to the same list, as `sub_run`
envelopes, by relay_run_event (called from Agent.run_events).
"""
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set

from ....core.request_context import in_request_tree
from ....tools.status import status_bus, StatusHandler, StatusEvent
from ....utils.tree_hierarchy import parse_request_id_hierarchy

logger = logging.getLogger(__name__)

# Every forwarder that is streaming a run right now, for the run events of the runs
# started under it (see relay_run_event).
_live_forwarders: Set["StatusEventForwarder"] = set()

#: The status lines a step keeps for the session read back, at most; a line's text at most this long.
MAX_STEP_LINES = 200
MAX_LINE_CHARS = 300
#: The phases that end an operation: its line does not change after one.
_FINAL_PHASES = frozenset({"end", "error"})
#: The level of a request id that says "a run started under the one before it" (utils/tree_hierarchy.py: the
#: forms sub_agent_manager and a coding CLI's view give their runs).
_RUN_BELOW = re.compile(r"_(?:sub_cont|sub|async|minlen)_[0-9a-zA-Z]+(?=_|$)")
#: The runs under another whose status lines nothing else keeps, by run id: the run above keeps them with its step.
_HOMELESS: Set[str] = set()


def lines_stay_with_the_caller(run_id: str, stay: bool = True) -> None:
    """Say that no session of its own keeps the status lines of the run ``run_id`` (a coding CLI answering as
    the model: one run per model call, kept nowhere), so the run it works under keeps them with its step
    (StatusEventForwarder.keep_line). ``stay=False`` once it has ended."""
    if stay:
        _HOMELESS.add(run_id)
    else:
        _HOMELESS.discard(run_id)

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
        if run_id and in_request_tree(request_id, run_id) and run_is_attended(run_id):
            unread = get_background_job_manager().unread_for(run_id)
            if unread is None or unread == 0.0 or unread < grace:
                return run_id
    return None


def run_is_live(request_id: str) -> bool:
    """Whether the run ``request_id`` streams now, or a run started under it
    does (an async sub-agent outlives the run that started it)."""
    if not request_id:
        return False
    return any(f.request_id and in_request_tree(f.request_id, request_id) for f in list(_live_forwarders))


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
    
    def __init__(self, request_id: str, events_list: List[Dict[str, Any]],
                 keep: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.request_id = request_id
        self.events_list = events_list
        self.keep = keep
    
    async def process(self, event: StatusEvent) -> None:
        """Directly append matching events to the list."""
        # Filter: only events for this request (exact match or prefix for sub-requests).
        # One that names no request is nobody's and is dropped: let through, it reached
        # every run's stream in the process, whoever that run belonged to.
        event_request_id = event.request_id
        if not event_request_id:
            return
        if self.request_id and not in_request_tree(event_request_id, self.request_id):
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
        if self.keep is not None:
            self.keep(status_sse_event)

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
        # The status lines of the run's steps as the session keeps them (keep_line): each
        # step's list by its number, the step under way (None before the first), and each
        # operation's line by its id while it may still change.
        self._lines: Dict[int, List[Dict[str, Any]]] = {}
        self._step: Optional[int] = None
        self._open_lines: Dict[str, Dict[str, Any]] = {}
        # the run's messages as last stamped: where a step's first line finds its message
        self._messages: Optional[List[Any]] = None

    def keep_line(self, event: Dict[str, Any]) -> None:
        """Keep a status line of this run's own work for the session read back.

        What a tool did and how it ended went out on the run's stream and nowhere else:
        a session opened again showed its calls without their outcome, and a model call
        that ran tools by itself (a coding CLI as the model) as if nothing had been
        called. Kept is the last line of each operation, with the step it began in
        (``stamp`` puts a step's lines on its message, ChatMessage.status_lines); an
        operation still going changes its line there. One that began before the run's
        first step is the run's own frame (its coordinator and worker lines) and no
        step's: read back, a row "step 3/30" still going.

        Not the lines of a run started under this one: that run keeps its own, in its
        own session -- one that streams now (an agent called as a tool, whose id is its
        call's), and one whose id says so (``_sub_``, ``_async_`` ...), streaming or
        not: a coding CLI's view lets go of its stream at once. Unless nothing else
        keeps them (``lines_stay_with_the_caller``).
        """
        request_id = event["request_id"]
        if any(f.request_id and _is_above(self, f.request_id) and in_request_tree(request_id, f.request_id)
               for f in _live_forwarders):
            return
        below = _RUN_BELOW.search(request_id, len(self.request_id or ""))
        if below is not None and request_id[:below.end()] not in _HOMELESS:
            return
        said = {"message": str(event.get("message") or "")[:MAX_LINE_CHARS], "phase": event.get("phase"),
                "timestamp": event.get("timestamp")}
        line = self._open_lines.get(request_id)
        if line is None:
            lines = self._lines.setdefault(self._step, []) if self._step is not None else None
            if lines is not None and len(lines) >= MAX_STEP_LINES:
                return
            tree = event.get("tree") or {}
            # Whole before it is in the list, and only its values change afterwards: the session is saved from
            # another thread, which may be reading this very line.
            line = {"request_id": request_id, "server": event.get("server"),
                    "tree": {"parent_id": tree.get("parent_id"), "depth_level": tree.get("depth_level")}, **said}
            self._open_lines[request_id] = line
            if lines is not None:
                lines.append(line)
                if len(lines) == 1:
                    self.stamp(self._messages)      # the step's first line: its message holds none yet
        else:
            line.update(said)
        if event.get("phase") in _FINAL_PHASES:
            del self._open_lines[request_id]

    def begin_step(self, step: int, messages: List[Any]) -> None:
        """The run goes on to step ``step`` (numbered as ChatMessage.step): the lines said from here on are that
        step's. ``messages``: the run's list as it stands, stamped first -- what the step before left there."""
        self.stamp(messages)
        self._step = step

    def stamp(self, messages: Optional[List[Any]]) -> None:
        """Put each step's lines on the message of that step as the run holds it NOW.

        Not given to a message once and for all: hooks hand the run copies of its messages -- a compaction
        rebuilds every one, a pre-LLM hook that changes the list gets a deep copy of it -- and a copy held the
        lines as they were at that moment: an operation still going never ended on it, and what the step's tools
        said after it was missing. So the lines stay here, by step, and are put on whatever message carries
        that step's number: when the step's message is made, when the next step begins, and before the run's
        last save. From the end back to where this run began: the message that opened it (it carries the run's
        id, ChatMessage.request_id; in a session older than that, the one a person typed) or a step numbered no
        lower than the one after it -- what stands before is the run before this one, whose steps are numbered
        from one as well. A message that holds more lines than its step's (two messages merged into one) keeps
        them.
        """
        self._messages = messages
        after: Optional[int] = None
        for message in reversed(messages or []):
            role, step = getattr(message, "role", None), getattr(message, "step", None)
            if getattr(message, "request_id", None) or (role == "user" and not getattr(message, "injected_by", None)):
                break
            if role != "assistant" or not isinstance(step, int):
                continue
            if after is not None and step >= after:
                break
            after = step
            lines = self._lines.get(step)
            if lines and len(getattr(message, "status_lines", None) or ()) <= len(lines):
                message.status_lines = lines

    async def start_forwarding(self, request_id: str) -> None:
        """Start forwarding status events for a specific request."""
        self.request_id = request_id
        self._lines, self._step, self._open_lines, self._messages = {}, None, {}, None
        # Above this run: the nearest run streaming now whose id it extends, and the
        # runs above that one. Settled now, not per event, because "above" is a
        # question of when as much as of ids: a stream begun later under an id this
        # one extends is another run -- the writer dispatches a job again under its
        # old id while a sub-agent of the old run is still at work, and neither that
        # sub-agent nor anything it starts afterwards is the new run's.
        nearest = max((f for f in _live_forwarders if _is_above(f, request_id)),
                      key=lambda f: len(f.request_id or ""), default=None)
        self.listeners = [*nearest.listeners, nearest] if nearest else []
        self.status_events_to_forward.clear()
        self._open_deltas.clear()
        
        # Create and register direct handler
        self._handler = DirectStatusHandler(request_id, self.status_events_to_forward, keep=self.keep_line)
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