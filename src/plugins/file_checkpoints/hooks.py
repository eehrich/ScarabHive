"""The two tool hooks that record file changes, and the rewinder the chat asks.

``record_before_change`` (pre_tool_call) runs for every call of an agent that
switched it on. A call whose writes can be named (``targets.plan_for``) gets
the state of each path it is about to change recorded -- once per turn and
path, so a file edited twenty times in one turn costs one copy, and a file
nobody touches costs nothing. A call that changes files without naming them
(a shell command) is counted instead. ``record_after_change`` (post_tool_call)
then records what the call left: that is how a rewind later tells the agent's
own change from one made outside it since.

A sub-agent's changes belong to the turn that started it: a run whose request
id extends one this plugin has seen (``<run>_003_sub_x``, the scheme tool
approvals use too) records into the session and turn of that run -- provided
its agent has the hooks on as well.

The rewinder (``agent_system.file_rewind``) serves /undo files, /rewind and
their endpoints. Every surface passes the conversation as it holds it; the
record is placed in it by ``turns.position``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from agent_system.chat_actions import starts_a_turn
from agent_system.file_rewind import (
    NOTHING, PARTIAL, REFUSED, REWOUND, UNKNOWN_CHECKPOINT,
    register_file_rewinder, unregister_file_rewinder,
)
from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.servers.agent.components.hook_integration import hook_runs_for
from agent_system.servers.agent.components.session_tracking import session_chain
from agent_system.services.session_service import is_ephemeral_session
from agent_system.servers.agent.components.status_forwarding import run_is_live, run_streams

from . import fs
from .fs import ABSENT, DIR, FILE, SYMLINK, UNREADABLE, State
from .store import Change, ForeignJournal, Journal, Turn, is_session_id, journals_older_than
from .targets import (
    Plan, Target, is_untracked, plan_for, server_instance, server_roots, server_type, tool_of,
)
from .turns import TurnRef, current_turn, head_index, heads, position

logger = logging.getLogger(__name__)

#: Runs whose session and turn are kept for the sub-runs under them.
MAX_RUNS = 10000
#: Calls waiting for their post hook.
MAX_PENDING = 10000
#: Seconds between sweeps of the run map.
SWEEP_SECONDS = 60.0
#: Sessions whose current turn / parent session is kept in memory.
MAX_CACHED_SESSIONS = 256
#: How far up the links to a parent session a sub-session is followed.
MAX_SUB_DEPTH = 8
#: What the framework appends to a run's id for a run it starts under it: a tool
#: call (next_internal_tool_request_id), a script's call (tool_script), a sub-agent.
_SUB_RUN = re.compile(r"^_(\d{3}|ts\d+|sub_|async_)")

#: Files named per checkpoint in a listing.
LISTED_FILES = 5
_DAY = 86400.0


def _pass(context: HookContext) -> HookResult:
    return HookResult(success=True, modified=False, context=context)


def _inferred_operation(arguments: Mapping[str, Any]) -> Optional[str]:
    """manage_sub_agent's operation where the model left it out, read as the
    manager reads it (SubAgentManagerServer._infer_operation)."""
    has_type = bool(arguments.get("agent_type"))
    has_id = bool(arguments.get("instance_id"))
    has_prompt = bool(arguments.get("message") or arguments.get("task"))
    if has_type and not has_id:
        return "create"
    if has_id and has_prompt and not has_type:
        return "continue"
    return None


@dataclass(frozen=True)
class RunRef:
    """Where a run's changes are recorded: whose session, which turn."""

    user_id: str
    session_id: str
    turn: TurnRef


@dataclass(frozen=True)
class Pending:
    """A path a call is about to change, waiting for the state it leaves."""

    user_id: str
    session_id: str
    change_id: int
    path: str
    created: bool


def _held_parent(session_id: str) -> str:
    """The session ``session_id`` runs below as this process holds it -- an agent
    called as a tool runs on one of its own below its caller's -- or ""."""
    chain = session_chain(session_id)
    return chain[1] if len(chain) > 1 else ""


class _CachedTurn:
    """A turn read off a conversation, with its head message held."""

    def __init__(self, ref: TurnRef, messages: Sequence[Any]):
        self.ref = ref
        found = [i for i, message in enumerate(messages) if starts_a_turn(message)]
        self.head_position = found[-1]
        self.head = messages[self.head_position]


class _Budget:
    """How much one call may still keep."""

    def __init__(self, entries: int, content_bytes: int):
        self.entries = entries
        self.bytes = content_bytes


def inside_roots(path: str, roots: Sequence[Path]) -> bool:
    """Whether ``path`` lies inside one of ``roots`` -- judged by its parent,
    resolved: a rewind never follows the last component (fs module), so a link
    there is replaced or removed, never written through."""
    candidate = Path(path)
    try:
        resolved = candidate.parent.resolve() / candidate.name
    except (OSError, ValueError):
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def _redirected(path: str) -> str:
    """Why ``path`` is reached through a link now, or "".

    Recorded paths are canonical -- the tool servers resolve them before they
    write. A parent that no longer resolves to itself has a link in its chain:
    written through it, a rewind lands on a file it has no record of (a link
    this very rewind put back higher up included), inside the root or out of it.
    """
    parent = Path(path).parent
    try:
        resolved = parent.resolve()
    except (OSError, ValueError):
        return "its directory cannot be resolved"
    if resolved != parent:
        return f"a link now stands in its path ({_display(str(parent))} leads to {_display(str(resolved))})"
    return ""


def _display(path: str) -> str:
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path


def _change_label(before: State, after: Optional[State]) -> str:
    if after is None:
        return "changed (not confirmed)"
    if before.kind == ABSENT:
        return "created" if after.kind != DIR else "created dir"
    if after.kind == ABSENT:
        return "deleted" if before.kind != DIR else "removed dir"
    return "modified"


def _one_row_per_turn(rows: List[Change]) -> List[Change]:
    """A turn holds one row per path -- unless one file was recorded under two
    spellings in one step, before it existed under either. Those rows are one:
    the state before the first, what the last call left."""
    merged: List[Change] = []
    for row in rows:
        # Only a row recorded while its spelling did not exist yet: one that
        # found a file (the person's, put there under another spelling since the
        # first call) is a change the chain check has to see.
        # And only when the first did not exist either: a pair whose first row
        # found a file is chain-consistent when it is the agent's own doing (the
        # first call removed it), and a person's delete in between must show.
        if merged and merged[-1].turn_seq == row.turn_seq and row.before.kind == ABSENT \
                and merged[-1].before.kind == ABSENT:
            first = merged[-1]
            last = row if row.after_seq >= first.after_seq else first
            merged[-1] = Change(id=first.id, turn_seq=first.turn_seq, path=first.path, server=first.server,
                                tool=first.tool, before=first.before, after=last.after,
                                outside=first.outside or row.outside, after_seq=last.after_seq)
            continue
        merged.append(row)
    return merged


def _as_planned(now: State, planned: State) -> bool:
    """Whether a path is still as the plan found it (an unreadable one counts as
    unchanged only while it stays unreadable)."""
    return now.kind == planned.kind and now.digest == planned.digest


def _first_records(turns: Sequence[Turn]) -> Dict[str, int]:
    """Turn key -> the oldest record made in it or in any turn after it on its
    branch (every recorded turn whose key it is, or whose ancestors hold it)."""
    first: Dict[str, int] = {}
    for turn in turns:
        for key in (*turn.ancestors, turn.key):
            if key not in first or turn.seq < first[key]:
                first[key] = turn.seq
    return first


def _order(pos: float, turn: Turn, first: Dict[str, int]) -> Tuple[Any, ...]:
    """Where a recorded turn stands among the others.

    First its position in the conversation. Turns dropped behind the same turn
    share a position; among them the tree decides: a turn comes after every
    turn it grew from (its ancestors' key is a prefix of its own), and sibling
    branches by the oldest record made in each. Depth alone put a grandchild
    of an early branch behind a later sibling, and a rewind to that sibling
    deleted what the grandchild had left -- kept, and on disk when the
    sibling began. A turn's record is made by its first change, so the record
    order alone was wrong too: an async sub-agent of an earlier turn may write
    after a later turn did.
    """
    return (pos, tuple(first[key] for key in (*turn.ancestors, turn.key)), turn.seq)


def _effective(change: Change) -> bool:
    return change.after is None or not change.before.same(change.after)


class FileCheckpointsPlugin(SchemaBasedPluginHook):
    """Records what the agent changes on disk, and puts it back on request."""

    def __init__(self, plugin_dir: Path | str, name: str = "file_checkpoints",
                 server_config: Any = None):
        super().__init__(plugin_dir)
        self.instance_name = name
        config = dict(self.get_config())
        instance_config = getattr(server_config, "config", None) if server_config is not None else None
        if isinstance(instance_config, Mapping):
            config.update(instance_config)
        self.root = self._storage_root(config.get("storage_path"))
        self.max_checkpoints = max(1, int(config.get("max_checkpoints", 50)))
        self.max_file_bytes = max(0, int(config.get("max_file_bytes", 10 * 1024 * 1024)))
        self.max_call_bytes = max(0, int(config.get("max_call_bytes", 256 * 1024 * 1024)))
        self.max_session_bytes = max(0, int(config.get("max_session_bytes", 512 * 1024 * 1024)))
        self.max_entries_per_call = max(1, int(config.get("max_entries_per_call", 2000)))
        # Destructive, so off unless the operator asks for it.
        self.retention_days = float(config.get("retention_days", 0) or 0)
        self.untracked_tools = tuple(str(p) for p in (config.get("untracked_tools") or ()))
        self._runs: "OrderedDict[str, Tuple[RunRef, float]]" = OrderedDict()
        self._last_sweep = time.monotonic()
        self._pending: "OrderedDict[Tuple[Any, ...], List[List[Pending]]]" = OrderedDict()
        self._turn_cache: Dict[str, Tuple[int, Any, Optional["_CachedTurn"]]] = {}
        # (user, session) -> the parent its stored record names; only found links (_stored_parent)
        self._parents: "OrderedDict[Tuple[str, str], str]" = OrderedDict()
        self._journal_runs: Dict[Tuple[str, str], Set[str]] = {}
        self._locks: Dict[str, List[Any]] = {}
        self._locks_guard = threading.Lock()
        self._warned_agents: Set[str] = set()
        self._last_retention = 0.0
        #: (record directory, change id) -> calls whose post hook is still to come
        self._awaiting: Dict[Tuple[str, int], int] = {}
        self._pending_since: Dict[Tuple[Any, ...], float] = {}
        #: Records of sessions the person deleted in this process: never made again.
        self._forgotten: "OrderedDict[str, float]" = OrderedDict()
        self._awaiting_guard = threading.Lock()
        self._apply_retention()
        register_file_rewinder(self)

    @staticmethod
    def _storage_root(configured: Any) -> Path:
        if configured:
            path = Path(str(configured))
            if not path.is_absolute():
                from agent_system.paths import PROJECT_ROOT
                path = PROJECT_ROOT / path
            return path
        # Beside the sessions, wherever they are (AGENT_SESSION_STORAGE_PATH):
        # an isolated API or a test run keeps its checkpoints with its sessions.
        from agent_system.core.session_presence import sessions_dir
        return sessions_dir().parent / "file_checkpoints"

    @property
    def before_hook_name(self) -> str:
        return f"{self.instance_name}.record_before_change"

    @property
    def after_hook_name(self) -> str:
        return f"{self.instance_name}.record_after_change"

    async def stop_plugin(self) -> None:
        unregister_file_rewinder(self)

    # --- which session and turn -------------------------------------------

    @contextmanager
    def _lock(self, directory: Path) -> Iterator[None]:
        """Hold the lock of one record in this process -- by its directory, which
        is what the retention sweep has in hand too.

        One lock per record, kept only while someone holds or waits for it: a lock
        per record ever touched grew with every session, and a shared stripe made
        one session's recording wait for another session's rewind.
        """
        key = str(directory)
        with self._locks_guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._locks_guard:
                entry[1] -= 1
                if entry[1] == 0 and self._locks.get(key) is entry:
                    del self._locks[key]

    def _turn_of(self, agent: Any, session_id: str) -> Optional[TurnRef]:
        tracker = getattr(agent, "_session_tracker", None)
        if tracker is None or not session_id:
            return None
        try:
            messages = tracker.get_session_messages(session_id) or []
        except Exception:  # noqa: BLE001 - no conversation, no turn to file under
            logger.debug("file_checkpoints: no messages for %s", session_id, exc_info=True)
            return None
        # Read again only when the conversation moved. Compared by identity with
        # the objects held HERE: an id() alone is reused once its object is
        # gone, and a list cut by /undo and grown back to the same length then
        # passed for the old one -- its changes went under the dropped turn.
        last = messages[-1] if messages else None
        cached = self._turn_cache.get(session_id)
        if cached is not None:
            length, last_seen, turn = cached
            if length == len(messages) and last_seen is last and (
                    turn is None or messages[turn.head_position] is turn.head):
                return turn.ref if turn is not None else None
        ref = current_turn(messages)
        head = _CachedTurn(ref, messages) if ref is not None else None
        self._turn_cache.pop(session_id, None)
        self._turn_cache[session_id] = (len(messages), last, head)
        while len(self._turn_cache) > MAX_CACHED_SESSIONS:
            self._turn_cache.pop(next(iter(self._turn_cache)))
        return ref

    async def _inherited(self, context: HookContext, request_id: str, user_id: str) -> Optional[RunRef]:
        """The session and turn of the nearest run above ``request_id`` -- a run
        this one was started under, not merely one whose id it happens to extend.

        Two checks, because a client may choose its own request ids and a
        finished run stays remembered for a while: the rest of the id must be one
        the framework appends for a run it starts (``_NNN`` for a tool call,
        ``_tsNN`` for a script's call, ``_sub_``/``_async_`` for a sub-agent),
        and the session must be that run's own or one of its sub-sessions. A
        client's "chat_2" after "chat" in another session inherited that
        session's turn, and its changes were undone with it.
        """
        candidate = request_id
        while "_" in candidate:
            candidate = candidate.rsplit("_", 1)[0]
            entry = self._runs.get(candidate)
            if entry is None or entry[0].user_id != user_id:
                continue
            if not _SUB_RUN.match(request_id[len(candidate):]):
                continue
            ref = entry[0]
            if ref.session_id == context.session_id:
                # A script's call and an agent called as a tool run while their
                # run does; a finished run's id extended in the same session is
                # the client's next turn ("job", then "job_002").
                if run_streams(candidate):
                    return ref
                continue
            if await self._descends_from(context.agent, user_id, context.session_id, ref.session_id):
                return ref
        return None

    async def _descends_from(self, agent: Any, user_id: str, session_id: str, ancestor: str) -> bool:
        """Whether ``session_id`` is a sub-session (at any depth) of ``ancestor``.

        Each step asks this process first: an agent called as a tool runs on a
        session of its own below its caller's, and the agents' trackers name the
        caller's as its parent (session_chain) -- before its record is filed, and
        when it is never stored (below a throwaway caller). Asked of the stored
        record alone, such a session was nobody's, and a helper's changes were
        left out of the calling turn: a rewind did not take them back. Then the
        ``parent_session`` link of the stored record -- the one the sub-agent
        manager writes -- for what this process does not hold."""
        sessions = getattr(getattr(agent, "_session_service", None), "session_manager", None)
        current = session_id
        for _ in range(MAX_SUB_DEPTH):
            parent = _held_parent(current) or await self._stored_parent(sessions, user_id, current)
            if not parent:
                return False
            if parent == ancestor:
                return True
            current = parent
        return False

    async def _stored_parent(self, sessions: Any, user_id: str, session_id: str) -> str:
        """The parent the stored record names (_parent_of), kept once found. A miss
        is not kept: a session its caller's tool call opened is filed a moment
        later, and a miss kept from before stood for the life of the process."""
        key = (user_id, session_id)
        cached = self._parents.get(key)
        if cached:
            return cached
        parent = await self._parent_of(sessions, user_id, session_id)
        if parent:
            self._parents[key] = parent
            while len(self._parents) > MAX_CACHED_SESSIONS:
                self._parents.pop(next(iter(self._parents)))
        return parent

    @staticmethod
    async def _parent_of(sessions: Any, user_id: str, session_id: str) -> str:
        if sessions is None or not is_session_id(session_id):
            return ""
        try:
            reader = getattr(sessions, "peek_session", None) or sessions.load_session
            record = await reader(user_id, session_id)
        except Exception:  # noqa: BLE001 - no record, no lineage: the call records in its own session
            return ""
        parent = (record or {}).get("parent_session") if isinstance(record, Mapping) else None
        return str((parent or {}).get("session_id") or "") if isinstance(parent, Mapping) else ""

    def _remember(self, request_id: str, ref: RunRef) -> None:
        now = time.monotonic()
        self._runs.pop(request_id, None)
        self._runs[request_id] = (ref, now)
        self._journal_runs.setdefault((ref.user_id, ref.session_id), set()).add(request_id)
        if now - self._last_sweep >= SWEEP_SECONDS or len(self._runs) > MAX_RUNS:
            self._last_sweep = now
            for ended in [rid for rid, (_, at) in self._runs.items()
                          if now - at >= SWEEP_SECONDS and not run_is_live(rid)]:
                self._forget_run(ended)
        while len(self._runs) > MAX_RUNS:
            self._forget_run(next(iter(self._runs)))

    def _forget_run(self, request_id: str) -> None:
        entry = self._runs.pop(request_id, None)
        if entry is None:
            return
        ident = (entry[0].user_id, entry[0].session_id)
        runs = self._journal_runs.get(ident)
        if runs is not None:
            runs.discard(request_id)
            if not runs:
                del self._journal_runs[ident]

    async def _run_ref(self, context: HookContext) -> Optional[RunRef]:
        """Where this call's changes go: the session and turn of a run above
        it (a sub-agent's), else its own session's current turn."""
        user_id = context.user_id
        if not user_id:
            return None      # nobody's run: nobody could rewind it
        if not is_session_id(context.session_id or ""):
            return None      # no session a chat could name
        request_id = context.request_id or ""
        ref = await self._inherited(context, request_id, user_id) if request_id else None
        if ref is None:
            turn = self._turn_of(context.agent, context.session_id)
            if turn is None:
                return None
            ref = RunRef(user_id=user_id, session_id=context.session_id, turn=turn)
        if request_id:
            self._remember(request_id, ref)
        if is_ephemeral_session(ref.session_id):
            # A stateless run promises to leave nothing behind -- nor do the
            # runs it starts, which inherit this ref (hence remembered first).
            return None
        return ref

    def _after_hook_runs_for(self, agent: Any) -> bool:
        info = get_hook_registry().get_hook_info(self.after_hook_name)
        if info is None:
            return False
        hooks = getattr(getattr(agent, "agent_config", None), "hooks", None)
        return hook_runs_for(hooks, self.after_hook_name, bool(info.get("enabled", False)))

    # --- the hooks ---------------------------------------------------------

    async def record_before_change(self, context: HookContext) -> HookResult:
        call = context.tool_call or {}
        server = str(call.get("server") or "")
        name = str(call.get("name") or "")
        agent = context.agent
        if agent is None or not server or "." in server:
            return _pass(context)
        raw = call.get("arguments")
        arguments: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        ref = await self._run_ref(context)
        if ref is None:
            return _pass(context)
        instance = server_instance(agent, server)
        kind = server_type(agent, server, instance)
        tool = tool_of(server, name)
        plan = plan_for(kind, instance, tool, arguments)
        untracked = None
        if plan is None:
            if is_untracked(kind, tool, self.untracked_tools):
                untracked = name
            else:
                spawned = await self._spawn_without_record(context, agent, instance, kind, tool, arguments)
                if spawned:
                    untracked = f"{name} ({spawned} records no checkpoints)"
        if plan is None and untracked is None:
            return _pass(context)
        if not self._after_hook_runs_for(agent):
            agent_name = getattr(agent, "name", "?")
            if agent_name not in self._warned_agents:
                self._warned_agents.add(agent_name)
                logger.error("file_checkpoints: agent %s has %s on but %s off -- without it no change "
                             "can be told from one made outside the agent, so nothing is recorded. "
                             "Switch both on.", agent_name, self.before_hook_name, self.after_hook_name)
            return _pass(context)
        # The count is taken in the worker thread, under the record's lock, so a
        # post hook of another call never sees the row as nobody else's. Shielded:
        # a hook cut off meanwhile (its timeout, the run cancelled) stores no
        # entry for a post hook to take the count back -- the future gives it
        # back itself once the thread is done.
        work = asyncio.ensure_future(asyncio.to_thread(self._record_before, ref, server, untracked or name, plan,
                                                       time.monotonic()))
        try:
            pending = await asyncio.shield(work)
        except asyncio.CancelledError:
            work.add_done_callback(self._give_back_count)
            raise
        except Exception:  # noqa: BLE001 - the record must never stop the call
            logger.exception("file_checkpoints: recording %s for %s failed; the call runs unrecorded",
                             name, ref.session_id)
            return _pass(context)
        if pending:
            self._pending.setdefault(self._call_key(context), []).append(pending)
            self._pending_since.setdefault(self._call_key(context), time.monotonic())
            self._sweep_pending()
        return _pass(context)

    def _sweep_pending(self) -> None:
        """Let go of calls whose post hook will not come: beyond MAX_PENDING, and
        those of runs that ended SWEEP_SECONDS ago (cancelled before the call ran,
        or blocked after this hook)."""
        now = time.monotonic()
        stale = [key for key, since in self._pending_since.items()
                 if now - since >= SWEEP_SECONDS and not run_is_live(str(key[0] or ""))]
        while len(self._pending) - len(stale) > MAX_PENDING:
            stale.append(next(key for key in self._pending if key not in stale))
        for key in stale:
            for lost in self._pending.pop(key, []):
                self._await_after(lost, -1)
            self._pending_since.pop(key, None)

    async def record_after_change(self, context: HookContext) -> HookResult:
        waiting = self._pending.get(self._call_key(context))
        if not waiting:
            return _pass(context)
        pending = waiting.pop(0)
        if not waiting:
            self._pending.pop(self._call_key(context), None)
            self._pending_since.pop(self._call_key(context), None)
        try:
            # Shielded: the entry is popped already, so cut off before its thread
            # ran, the count it takes back stayed for good.
            await asyncio.shield(asyncio.ensure_future(asyncio.to_thread(self._record_after, pending)))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - logged; the change stays "not confirmed"
            logger.exception("file_checkpoints: recording what %s left failed", context.tool_call)
        return _pass(context)

    def _records(self, agent: Any) -> bool:
        """Whether ``agent`` has both hooks on -- its changes are recorded."""
        registry = get_hook_registry()
        hooks = getattr(getattr(agent, "agent_config", None), "hooks", None)
        for hook_name in (self.before_hook_name, self.after_hook_name):
            info = registry.get_hook_info(hook_name)
            if info is None or not hook_runs_for(hooks, hook_name, bool(info.get("enabled", False))):
                return False
        return True

    async def _spawn_without_record(self, context: HookContext, agent: Any, instance: Any,
                                    kind: Optional[str], tool: str, arguments: Mapping[str, Any]) -> str:
        """The agent a call starts that records no checkpoints ("agent 'x'"), or "".

        Its file changes would be missing from a rewind without a word, so the
        call is counted like a shell command. A sub-agent WITH the hooks records
        into this turn (request id inheritance) and is not named.
        """
        from agent_system.servers.agent.server import Agent

        target: Any = None
        if isinstance(instance, Agent):
            if tool == "list_available_tools":
                return ""            # only lists
            target = instance
        elif kind == "sub_agent_manager" and tool == "manage_sub_agent":
            operation = arguments.get("operation") or _inferred_operation(arguments)
            if operation == "create":
                name = arguments.get("agent_type")
                target = server_instance(agent, name) if isinstance(name, str) else None
                if target is None:
                    return ""        # the manager refuses an agent it does not have
            elif operation == "continue":
                name = await self._continued_agent(context, agent, arguments.get("instance_id"))
                if name == "":
                    return ""        # the manager refuses: no such sub-session of this one
                target = server_instance(agent, name) if name else None
                if target is None:
                    return f"sub-agent '{arguments.get('instance_id')}'"
            else:
                return ""
        else:
            return ""
        if not isinstance(target, Agent) or self._records(target):
            return ""
        return f"agent '{getattr(target, 'name', '?')}'"

    @staticmethod
    async def _continued_agent(context: HookContext, agent: Any, instance_id: Any) -> Optional[str]:
        """The agent of the caller's sub-session ``instance_id`` as the manager
        finds it; "" when the manager would refuse, None when it cannot be told."""
        sessions = getattr(getattr(agent, "_session_service", None), "session_manager", None)
        if sessions is None or not isinstance(instance_id, str) or not is_session_id(instance_id):
            return None if sessions is None else ""
        try:
            data = await sessions.load_session(context.user_id, instance_id)
        except Exception as exc:  # noqa: BLE001 - by kind: missing is a refusal, the rest unknown
            if isinstance(exc, FileNotFoundError) or type(exc).__name__ == "SessionNotFoundError":
                return ""
            return None
        if not isinstance(data, Mapping):
            return None
        if (data.get("parent_session") or {}).get("session_id") != context.session_id:
            return ""
        return str(data.get("agent_name") or "") or None

    @staticmethod
    def _call_key(context: HookContext) -> Tuple[Any, ...]:
        call = context.tool_call or {}
        if call.get("id"):
            return (context.request_id, call.get("id"))
        try:
            arguments = json.dumps(call.get("arguments"), sort_keys=True, default=str)
        except (TypeError, ValueError):
            arguments = repr(call.get("arguments"))
        return (context.request_id, call.get("name"), arguments)

    # --- recording (worker thread) -----------------------------------------

    def _record_before(self, ref: RunRef, server: str, tool: str, plan: Optional[Plan],
                       started: float = float("inf")) -> List[Pending]:
        journal = Journal(self.root, ref.user_id, ref.session_id)
        pending: List[Pending] = []
        if plan is not None:
            # One file, one record: under the spelling its directory stores
            # (fs.spelled_on_disk), whatever spelling the call used.
            listings: Dict[str, List[str]] = {}
            plan = Plan([Target(fs.spelled_on_disk(t.path, listings), t.with_contents,
                                fs.spelled_on_disk(t.contents_to, listings) if t.contents_to else None)
                         for t in plan.targets])
        with self._lock(journal.directory):
            forgot = self._forgotten.get(str(journal.directory))
            if forgot is not None and started <= forgot:
                return []         # the person deleted the session while this call was under way
            with journal.connect(create=True) as conn:
                seq = Journal.turn_seq(conn, ref.turn, create=True)
                # Room for this call first: the turn being recorded is the one most
                # likely to be undone, so older turns give way to it.
                forgotten = Journal.prune(conn, self.max_checkpoints, self.max_session_bytes, keep_seq=seq,
                                          needed=self._estimate(plan, conn, seq))
                room = max(0, self.max_session_bytes - Journal.kept_bytes(conn))
                budget = _Budget(self.max_entries_per_call, min(self.max_call_bytes, room))
                if plan is None:
                    Journal.note_untracked(conn, seq, tool)
                else:
                    for target in plan.targets:
                        self._record_target(journal, conn, seq, server, tool, target, budget, pending)
            # Committed: counted now, and nothing after this may fail the call's
            # result -- rows recorded but reported "unrecorded" had no post hook.
            self._await_after(pending, +1)
            if forgotten:
                self._quietly(journal.collect_blobs)
        self._quietly(self._maybe_apply_retention)
        return pending

    @staticmethod
    def _quietly(work: Any) -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 - housekeeping; the record is written
            logger.warning("file_checkpoints: housekeeping failed", exc_info=True)

    def _give_back_count(self, work: "asyncio.Future") -> None:
        if work.cancelled() or work.exception() is not None:
            return
        self._await_after(work.result() or [], -1)

    def _estimate(self, plan: Optional[Plan], conn: Any, seq: int) -> int:
        """At most how many bytes recording ``plan`` keeps. A path this turn has
        recorded already keeps nothing new: counted again, it made older turns
        give way for room that was never needed."""
        if plan is None:
            return 0
        total = 0
        entries = self.max_entries_per_call
        for target in plan.targets:
            paths = [target.path]
            if target.with_contents and fs.fingerprint(target.path).kind == DIR:
                found, complete = fs.walk(target.path, entries)
                if not complete:
                    continue          # recorded as one directory, nothing kept
                paths.extend(found)
            for path in paths:
                entries -= 1
                if Journal.change_id(conn, seq, str(path)) is not None:
                    continue
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                # A directory keeps no bytes; its st_size (64 KB for a big one) made
                # older turns give way for nothing.
                if info.st_size <= self.max_file_bytes and not stat.S_ISDIR(info.st_mode):
                    total += info.st_size
        return min(total, self.max_call_bytes)

    def _record_target(self, journal: Journal, conn: Any, seq: int, server: str, tool: str,
                       target: Target, budget: _Budget, pending: List[Pending]) -> None:
        paths: List[Tuple[Path, str]] = [(target.path, "")]
        if target.with_contents and fs.fingerprint(target.path).kind == DIR:
            entries, complete = fs.walk(target.path, budget.entries)
            if not complete:
                note = (f"a directory of more than {self.max_entries_per_call} entries; "
                        "its contents were not kept")
                self._record_path(journal, conn, seq, server, tool, target.path, budget, pending, note)
                if target.contents_to is not None:
                    self._record_path(journal, conn, seq, server, tool, target.contents_to, budget, pending)
                return
            paths.extend((entry, "") for entry in entries)
            if target.contents_to is not None:
                paths.extend((target.contents_to / entry.relative_to(target.path), "") for entry in entries)
        for path, note in paths:
            self._record_path(journal, conn, seq, server, tool, path, budget, pending, note)

    def _record_path(self, journal: Journal, conn: Any, seq: int, server: str, tool: str, path: Path,
                     budget: _Budget, pending: List[Pending], note: str = "") -> None:
        key = str(path)
        existing = Journal.change_id(conn, seq, key)
        if existing is not None:
            # Recorded earlier in this turn: the state before stays the one from
            # then. But if the path is no longer what the last call left, someone
            # changed it in between -- and a rewind would take that with it.
            # Not while a call that changed it is still to report what it left:
            # a sub-agent's call in the same step writes before its caller's
            # post hook has recorded the caller's own write.
            left = None if self._awaited(journal, existing) else Journal.after_of(conn, existing)
            if left is not None and not fs.fingerprint(path).same(left):
                Journal.mark_outside(conn, existing, "it was changed outside the agent's file tools "
                                                     "while the turn was running")
            pending.append(Pending(journal.user_id, journal.session_id, existing, key, created=False))
            return
        budget.entries -= 1
        limit = min(self.max_file_bytes, budget.bytes)
        state, content = fs.snapshot(path, limit)
        if note:
            state = State(state.kind, digest=state.digest, mode=state.mode, size=state.size,
                          kept=False, note=note)
            content = None
        elif not state.kept and state.kind in (FILE, SYMLINK) and limit < self.max_file_bytes \
                and (state.size or 0) <= self.max_file_bytes:
            state = State(state.kind, digest=state.digest, mode=state.mode, size=state.size, kept=False,
                          note="not kept: the checkpoint store of this session or call is full")
        if content is not None:
            journal.keep_blob(state.digest, content)
            budget.bytes -= len(content)
        change_id = Journal.add_change(conn, seq, key, server, tool, state)
        pending.append(Pending(journal.user_id, journal.session_id, change_id, key, created=True))

    def _awaited(self, journal: Journal, change_id: int) -> bool:
        with self._awaiting_guard:
            return self._awaiting.get((str(journal.directory), change_id), 0) > 0

    def _awaited_by_others(self, journal: Journal, item: Pending) -> bool:
        """Whether a call besides ``item``'s own still has to report on its row:
        dropped as "changed nothing", that call's change had no record left."""
        with self._awaiting_guard:
            return self._awaiting.get((str(journal.directory), item.change_id), 0) > 1

    def _await_after(self, pending: List[Pending], delta: int) -> None:
        """Count the calls whose post hook is still to come, per change."""
        with self._awaiting_guard:
            for item in pending:
                key = (str(Journal(self.root, item.user_id, item.session_id).directory), item.change_id)
                count = self._awaiting.get(key, 0) + delta
                if count > 0:
                    self._awaiting[key] = count
                else:
                    self._awaiting.pop(key, None)

    def _record_after(self, pending: List[Pending]) -> None:
        if not pending:
            return
        journal = Journal(self.root, pending[0].user_id, pending[0].session_id)
        dropped = False
        with self._lock(journal.directory):
            try:
                with journal.connect() as conn:
                    for item in pending:
                        after = fs.fingerprint(Path(item.path))
                        before = Journal.before_of(conn, item.change_id)
                        if before is None:
                            continue
                        if item.created and before.same(after) and not self._awaited_by_others(journal, item):
                            Journal.drop_change(conn, item.change_id)   # the call changed nothing here
                            dropped = True
                        else:
                            Journal.set_after(conn, item.change_id, after)
                    if dropped:
                        Journal.drop_empty_turns(conn)
            finally:
                # Once what it left is written (or failed to be): a call that
                # looks in between compares against this call's state.
                self._await_after(pending, -1)
            if dropped:
                journal.collect_blobs()

    # --- retention (opt-in) ------------------------------------------------

    def _apply_retention(self) -> None:
        if self.retention_days <= 0:
            return
        self._last_retention = time.time()
        for directory in journals_older_than(self.root, self.retention_days * _DAY):
            with self._lock(directory):
                logger.info("file_checkpoints: dropping the record of %s (older than %g days)",
                            directory, self.retention_days)
                shutil.rmtree(directory, ignore_errors=True)

    def _maybe_apply_retention(self) -> None:
        if self.retention_days > 0 and time.time() - self._last_retention >= _DAY:
            self._apply_retention()

    # --- the rewinder ------------------------------------------------------

    def _live_runs(self, ident: Tuple[str, str]) -> List[str]:
        return sorted(rid for rid in self._journal_runs.get(ident, ()) if run_is_live(rid))

    async def checkpoints(self, *, user_id: str, session_id: str, messages: Sequence[Any]) -> dict:
        journal = Journal(self.root, user_id, session_id)
        if not journal.exists():
            return {"checkpoints": [], "text": "No file changes are recorded for this session."}
        try:
            return await asyncio.to_thread(self._listing, journal, list(messages))
        except ForeignJournal as exc:
            return {"checkpoints": [], "text": f"No file changes can be listed: {exc}."}

    def _load(self, journal: Journal, messages: Sequence[Any]):
        """(turns in order with their position, their changes, their untracked calls, numbered checkpoints)."""
        index = head_index(messages)
        with journal.connect() as conn:
            turns = Journal.turns(conn)
            changes = Journal.changes(conn, [t.seq for t in turns])
            untracked = Journal.untracked(conn, [t.seq for t in turns])
        # By position, then -- between turns dropped behind the same one -- by
        # the tree they grew in (_tree_order), then by when they were recorded.
        first = _first_records(turns)
        placed = sorted(((_order(position(t.key, t.ancestors, index), t, first), t) for t in turns),
                        key=lambda item: item[0])
        by_turn: Dict[int, List[Change]] = {}
        for change in changes:
            by_turn.setdefault(change.turn_seq, []).append(change)
        numbered = [(order, turn) for order, turn in placed
                    if any(_effective(c) for c in by_turn.get(turn.seq, ())) or untracked.get(turn.seq)]
        return placed, by_turn, untracked, numbered

    def _listing(self, journal: Journal, messages: Sequence[Any]) -> dict:
        with self._lock(journal.directory):
            placed, by_turn, untracked, numbered = self._load(journal, messages)
        entries = []
        for order, turn in numbered:
            pos = order[0]
            files = [{"path": c.path, "change": _change_label(c.before, c.after)}
                     for c in by_turn.get(turn.seq, ()) if _effective(c)]
            dropped = pos != int(pos)
            entries.append({
                # The record's own sequence, not a place in this list: a listing
                # numbered 1..n shifted when an old turn was forgotten or a
                # turn's record came late, and "/rewind 3" meant another range.
                "number": turn.seq,
                "turn": None if dropped else int(pos) + 1,
                "after_turn": (int(pos + 0.5) if pos >= 0 else 0) if dropped else None,
                "dropped": dropped,
                "question": turn.question,
                "files": files,
                "untracked": [{"tool": tool, "count": count}
                              for tool, count in sorted(untracked.get(turn.seq, {}).items())],
            })
        return {"checkpoints": entries, "text": _listing_text(entries)}

    async def forget(self, *, user_id: str, session_id: str) -> bool:
        """Delete a session's record (the person deleted the session)."""
        if not is_session_id(session_id):
            return False
        journal = Journal(self.root, user_id, session_id)

        def remove() -> bool:
            with self._lock(journal.directory):
                if journal.exists():
                    try:
                        with journal.connect():
                            pass          # whose record it is: ForeignJournal if not this user's
                    except ForeignJournal:
                        return False
                # Remembered with its moment -- whether a record existed or not: a
                # recording already under way (its thread outlives the run's
                # cancel) would make the record again, blobs of the person's files
                # and all. Calls that start later -- a new session under the same
                # id -- record as usual.
                self._forgotten[str(journal.directory)] = time.monotonic()
                while len(self._forgotten) > MAX_CACHED_SESSIONS:
                    self._forgotten.pop(next(iter(self._forgotten)))
                existed = journal.directory.exists()
                shutil.rmtree(journal.directory, ignore_errors=True)
                return existed

        removed = await asyncio.to_thread(remove)
        if removed:
            logger.info("file_checkpoints: dropped the record of deleted session %s", session_id)
        return removed

    async def rewind(self, *, user_id: str, session_id: str, messages: Sequence[Any],
                     checkpoint: Optional[int], registry: Any, overwrite: bool = False) -> dict:
        journal = Journal(self.root, user_id, session_id)
        if not journal.exists():
            return _report(NOTHING, "No file changes are recorded for this session.")
        live = self._live_runs(journal.ident)
        if live:
            return _report(REFUSED, (
                f"Files not rewound: a run of this session is still going ({', '.join(live)}) and may "
                "write files while they are put back. Try again once it is done."))
        try:
            return await asyncio.to_thread(self._rewind, journal, list(messages), checkpoint, registry,
                                           bool(overwrite))
        except ForeignJournal as exc:
            return _report(REFUSED, f"Files not rewound: {exc}.")

    def _rewind(self, journal: Journal, messages: Sequence[Any], checkpoint: Optional[int],
                registry: Any, overwrite: bool) -> dict:
        with self._lock(journal.directory):
            placed, by_turn, untracked, numbered = self._load(journal, messages)
            if checkpoint is None:
                count = len(heads(messages))
                if not count:
                    return _report(NOTHING, "This conversation has no turn to rewind.")
                threshold: Tuple[Any, ...] = (float(count - 1), (), -1)
                what = "the last turn"
            else:
                found = next(((order, turn) for order, turn in numbered if turn.seq == checkpoint), None)
                if found is None:
                    known = ", ".join(str(turn.seq) for _, turn in numbered)
                    return _report(UNKNOWN_CHECKPOINT, (
                        f"There is no checkpoint {checkpoint}"
                        + (f" -- this session has {known}." if known else " -- this session has none.")
                        + " /rewind lists them."))
                threshold = found[0]
                what = f"checkpoint {checkpoint}"
            selected = [turn for order, turn in placed if order >= threshold]
            if not selected:
                return _report(NOTHING, f"No file changes were recorded since {what}.")
            tools: Dict[str, int] = {}
            for turn in selected:
                for tool, count in untracked.get(turn.seq, {}).items():
                    tools[tool] = tools.get(tool, 0) + count
            plan = self._plan(journal, [c for t in selected for c in by_turn.get(t.seq, ())], registry)
            plan.untracked = tools
            plan.what = what
            if (plan.conflicts or plan.unrestorable) and not overwrite:
                return plan.report(REFUSED, journal)
            failed = self._apply(journal, plan)
            with journal.connect() as conn:
                seqs = [t.seq for t in selected]
                # Still recorded: what could not be written, and what could not be
                # put back for a reason that may pass -- its server not loaded in
                # this process, a link in its path, files of the person's in a
                # directory. Forgotten, a later rewind found nothing and the file
                # stayed. What cannot come back at all (content never kept) goes.
                keep = {path for path, _ in failed} | plan.retry
                if keep:
                    # By the rows each kept path was planned from -- spelled again
                    # after the apply, a path renamed back to its old spelling
                    # matched none, and its only copy of the original went.
                    kept_ids = {change_id for path in keep for change_id in plan.ids.get(path, ())}
                    for change in Journal.changes(conn, seqs):
                        if change.id not in kept_ids:
                            Journal.drop_change(conn, change.id)
                    for seq in seqs:
                        conn.execute("DELETE FROM untracked WHERE turn_seq = ?", (seq,))
                    Journal.drop_empty_turns(conn)
                else:
                    Journal.forget_turns(conn, seqs)
            journal.collect_blobs()
            plan.failed = failed
            if failed or plan.retry:
                return plan.report(PARTIAL, journal)
            if not plan.restore and not plan.remove:
                return plan.report(NOTHING, journal)
            return plan.report(REWOUND, journal)

    def _plan(self, journal: Journal, changes: List[Change], registry: Any) -> "_RewindPlan":
        plan = _RewindPlan()
        by_path: "OrderedDict[str, List[Change]]" = OrderedDict()
        listings: Dict[str, List[str]] = {}
        for change in changes:
            # Two spellings recorded while the file did not exist yet are one
            # file now: grouped by the spelling on disk, in record order.
            spelled = str(fs.spelled_on_disk(Path(change.path), listings))
            by_path.setdefault(spelled, []).append(change)
        roots_of: Dict[str, Optional[Tuple[Path, ...]]] = {}
        for path, rows in by_path.items():
            plan.ids[path] = [row.id for row in rows]
            target = rows[0].before
            chain = _one_row_per_turn(rows)
            expected = chain[-1].after
            current = fs.fingerprint(Path(path))
            if current.same(target):
                plan.unchanged.append(path)
                continue
            outside = self._outside_roots(path, rows, registry, roots_of) or _redirected(path)
            if outside:
                plan.unrestorable.append((path, outside))
                plan.retry.add(path)
                continue
            if target.kind == UNREADABLE or (target.kind in (FILE, SYMLINK) and not target.kept) \
                    or (target.kind == DIR and target.note):
                plan.unrestorable.append((path, target.note or "its earlier state was not recorded"))
                continue
            if target.kind in (FILE, SYMLINK) and not (journal.blob_dir / str(target.digest)).is_file():
                plan.unrestorable.append((path, "its recorded content is missing from the store"))
                continue
            within = next((row.outside for row in rows if row.outside), "")
            if expected is None:
                plan.conflicts.append((path, "the call that changed it did not finish recording what it left"))
            elif within:
                plan.conflicts.append((path, within))
            elif any(not a.after or not a.after.same(b.before) for a, b in zip(chain, chain[1:])):
                plan.conflicts.append((path, "it was changed outside the agent's file tools between turns"))
            elif not current.same(expected):
                plan.conflicts.append((path, "it was changed since the agent last wrote it"))
            plan.actions[path] = (target, current)
            plan.roots[path] = [roots_of.get(server) or () for server in dict.fromkeys(row.server for row in rows)]
            # Put back under the name it had: grouped under today's spelling
            # ("foo.txt", made by a later call), it came back as that.
            first_name = Path(rows[0].path).name
            if target.kind != ABSENT and first_name != Path(path).name:
                plan.names[path] = first_name
        self._keep_unrecorded_dirs(plan)
        for path, (target, current) in plan.actions.items():
            if target.kind == ABSENT:
                plan.remove.append(path)
            else:
                plan.restore.append(path)
        return plan

    def _outside_roots(self, path: str, rows: List[Change], registry: Any,
                       roots_of: Dict[str, Optional[Tuple[Path, ...]]]) -> str:
        """Why ``path`` may not be written now, or "": every server that recorded
        it must still be loaded and still allow it."""
        for server in dict.fromkeys(row.server for row in rows):
            if server not in roots_of:
                instance = None
                if registry is not None:
                    try:
                        instance = registry.get(server)
                    except Exception:  # noqa: BLE001 - not loaded: said below
                        instance = None
                roots_of[server] = server_roots(instance) if instance is not None else None
            roots = roots_of[server]
            if roots is None:
                return f"its tool server {server} is not loaded here, so its directories cannot be checked"
            if not inside_roots(path, roots):
                return f"it is no longer inside the directories {server} may write"
        return ""

    @staticmethod
    def _keep_unrecorded_dirs(plan: "_RewindPlan") -> None:
        """A directory is removed only when everything in it goes with it: a
        rewind never deletes what it has no record of."""
        leaving = {path for path, (target, _) in plan.actions.items() if target.kind == ABSENT}
        for path, (target, current) in sorted(plan.actions.items(), key=lambda item: -len(item[0])):
            if current.kind != DIR or target.kind == DIR:
                continue
            try:
                entries, complete = fs.walk(Path(path), 10000)
            except OSError:
                entries, complete = [], False
            strangers = [str(entry) for entry in entries if str(entry) not in leaving]
            if strangers or not complete:
                del plan.actions[path]
                leaving.discard(path)
                plan.conflicts = [c for c in plan.conflicts if c[0] != path]
                shown = ", ".join(_display(s) for s in strangers[:3]) + (" ..." if len(strangers) > 3 else "")
                plan.unrestorable.append((path, f"kept: it holds files the agent did not create ({shown})"))
                plan.retry.add(path)

    def _apply(self, journal: Journal, plan: "_RewindPlan") -> List[Tuple[str, str]]:
        """Remove what must go (deepest first), then write back (shallowest first).

        Every step checks its path against the allowed directories AGAIN, on the
        disk as it is at that moment: the plan checked them before anything was
        put back, and a link this very rewind restores higher up would otherwise
        carry every write below it out of the root.
        """
        failed: List[Tuple[str, str]] = []
        outside = ("not written: a link now stands in its path, or it no longer lies inside the "
                   "directories its tool server may write")
        moved = "not written: it changed while the rewind ran -- left as it is now"
        clearing = sorted((p for p, (t, c) in plan.actions.items() if c.kind != ABSENT
                           and (t.kind == ABSENT or t.kind != c.kind)),
                          key=lambda p: (-len(Path(p).parts), p))
        cleared: Set[str] = set()
        for path in clearing:
            if not plan.inside(path):
                failed.append((path, outside))
                continue
            if not _as_planned(fs.fingerprint(Path(path)), plan.actions[path][1]):
                failed.append((path, moved))
                continue
            try:
                fs.remove(Path(path))
                cleared.add(path)
            except OSError as exc:
                failed.append((path, str(exc.strerror or exc)))
        gone = {p for p, _ in failed}
        writing = sorted((p for p, (t, _) in plan.actions.items() if t.kind != ABSENT and p not in gone),
                         key=lambda p: (len(Path(p).parts), p))
        for path in writing:
            target, current = plan.actions[path]
            where = Path(path)
            if not plan.inside(path):
                failed.append((path, outside))
                continue
            # Right before the write, on the disk as it is now: the plan read it
            # once, and a person (or a run of another session) may be at work.
            if not _as_planned(fs.fingerprint(where), State(ABSENT) if path in cleared else current):
                failed.append((path, moved))
                continue
            try:
                name = plan.names.get(path)
                if name:
                    wanted = where.with_name(name)
                    if os.path.lexists(where):
                        os.rename(where, wanted)      # the same entry: only its spelling changes
                    where = wanted
                where.parent.mkdir(parents=True, exist_ok=True)
                if target.kind == DIR:
                    if where.is_symlink():
                        failed.append((path, "not written: a link stands there now"))
                        continue
                    if not where.is_dir():
                        where.mkdir()
                    if target.mode is not None:
                        os.chmod(where, target.mode)
                elif target.kind == FILE:
                    fs.write_file(where, journal.read_blob(str(target.digest)), target.mode)
                elif target.kind == SYMLINK:
                    if os.path.lexists(where):
                        fs.remove(where)
                    os.symlink(os.fsdecode(journal.read_blob(str(target.digest))), where)
            except OSError as exc:
                failed.append((path, str(exc.strerror or exc)))
        return failed


class _RewindPlan:
    def __init__(self) -> None:
        self.actions: "OrderedDict[str, Tuple[State, State]]" = OrderedDict()
        #: Per path in ``actions``: the allowed directories of each server that recorded it.
        self.roots: Dict[str, List[Tuple[Path, ...]]] = {}
        #: Unrestorable for a reason that may pass: their record stays.
        self.retry: Set[str] = set()
        #: Paths to be put back under another spelling of their name (case, NFC/NFD).
        self.names: Dict[str, str] = {}
        #: Per planned path: the ids of the rows it was planned from.
        self.ids: Dict[str, List[int]] = {}
        self.restore: List[str] = []
        self.remove: List[str] = []
        self.unchanged: List[str] = []
        self.conflicts: List[Tuple[str, str]] = []
        self.unrestorable: List[Tuple[str, str]] = []
        self.failed: List[Tuple[str, str]] = []
        self.untracked: Dict[str, int] = {}
        self.what = ""

    def inside(self, path: str) -> bool:
        """Whether ``path`` may be written now: inside the directories of every
        server that recorded it, and reached without a link -- asked of the disk
        as it is at this moment."""
        groups = self.roots.get(path) or []
        return bool(groups) and not _redirected(path) and all(inside_roots(path, roots) for roots in groups)

    def report(self, status: str, journal: Journal) -> dict:
        refused = status == REFUSED
        restored = [] if refused else [str(Path(p).with_name(self.names[p])) if p in self.names else p
                                       for p in self.restore if p not in {f for f, _ in self.failed}]
        removed = [] if refused else [p for p in self.remove if p not in {f for f, _ in self.failed}]
        result = {
            "status": status,
            "restored": restored,
            "removed": removed,
            "conflicts": [{"path": p, "reason": r} for p, r in self.conflicts],
            "unrestorable": [{"path": p, "reason": r} for p, r in self.unrestorable],
            "failed": [{"path": p, "reason": r} for p, r in self.failed],
            "untracked": [{"tool": t, "count": c} for t, c in sorted(self.untracked.items())],
        }
        result["text"] = _rewind_text(status, self.what, result)
        return result


def _report(status: str, text: str) -> dict:
    return {"status": status, "restored": [], "removed": [], "conflicts": [], "unrestorable": [],
            "failed": [], "untracked": [], "text": text}


def _paths(items: List[dict], limit: int = 10) -> List[str]:
    lines = [f"    {_display(item['path'])} -- {item['reason']}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"    ... and {len(items) - limit} more")
    return lines


def _untracked_line(untracked: List[dict]) -> str:
    calls = ", ".join(f"{u['tool']} x{u['count']}" for u in untracked)
    return f"  Not tracked: {calls} -- whatever they changed is still as they left it."


def _rewind_text(status: str, what: str, result: dict) -> str:
    lines: List[str] = []
    if status == REFUSED:
        lines.append(f"Files not rewound to before {what} -- nothing was changed:")
        if result["conflicts"]:
            lines.append("  Changed outside the agent since it wrote them:")
            lines.extend(_paths(result["conflicts"]))
        if result["unrestorable"]:
            lines.append("  Cannot be put back:")
            lines.extend(_paths(result["unrestorable"]))
        lines.append("  Add 'overwrite' to put back the rest anyway"
                     + (" (the files changed outside the agent included)." if result["conflicts"] else "."))
        return "\n".join(lines)
    done = len(result["restored"]) + len(result["removed"])
    if status == NOTHING:
        lines.append(f"No file needed putting back since {what}.")
    else:
        lines.append(f"Files rewound to before {what}: {len(result['restored'])} put back, "
                     f"{len(result['removed'])} removed." if done else f"Files rewound to before {what}.")
        lines.extend(f"    put back  {_display(p)}" for p in result["restored"][:20])
        lines.extend(f"    removed   {_display(p)}" for p in result["removed"][:20])
        if done > 40:
            lines.append(f"    ({done} in all)")
    if result["unrestorable"]:
        lines.append("  Not put back:")
        lines.extend(_paths(result["unrestorable"]))
    if result["failed"]:
        lines.append("  Could not be written (still recorded -- try again):")
        lines.extend(_paths(result["failed"]))
    if result["untracked"]:
        lines.append(_untracked_line(result["untracked"]))
    return "\n".join(lines)


def _listing_text(entries: List[dict]) -> str:
    if not entries:
        return "No file changes are recorded for this session."
    lines = ["File checkpoints -- /rewind <n> puts the files back as they were before checkpoint n:"]
    for entry in entries:
        where = (f"turn {entry['turn']}" if not entry["dropped"]
                 else f"dropped turn (after turn {entry['after_turn']})" if entry["after_turn"]
                 else "dropped turn")
        files = entry["files"]
        shown = ", ".join(f"{f['change']} {_display(f['path'])}" for f in files[:LISTED_FILES])
        more = f", +{len(files) - LISTED_FILES} more" if len(files) > LISTED_FILES else ""
        lines.append(f"  {entry['number']:>2}  {where}: \"{entry['question']}\"")
        if files:
            lines.append(f"      {len(files)} file(s): {shown}{more}")
        if entry["untracked"]:
            lines.append("    " + _untracked_line(entry["untracked"]).strip())
    return "\n".join(lines)
