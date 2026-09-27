"""Context usage tracking for the context_usage_tracker plugin."""

import json
import logging
import threading
import time
from pathlib import Path

from agent_system.paths import data_path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict, fields as dataclass_fields

from .database import UsageDatabase

logger = logging.getLogger(__name__)


@dataclass
class ContextUsageSnapshot:
    """A snapshot of context usage at a specific time (one per LLM call)."""
    timestamp: float
    agent_id: str
    agent_name: str
    session_id: str
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    message_count: int
    context_window: int
    usage_percentage: float
    cached_tokens: int = 0  # cached prompt tokens (cache READ)
    tool_definition_tokens: int = 0  # Estimated tokens for tool schemas/definitions
    cache_write_tokens: int = 0  # tokens written to the provider prompt cache
    cost: Optional[float] = None  # USD cost (billed, or estimated — see flag)
    cost_is_estimate: bool = False  # True = computed from config/llm_pricing.yaml
    model: str = ""  # model identifier of the client that served the call
    request_id: str = ""  # request this call belonged to
    latency_ms: Optional[float] = None  # wall-clock response time of the call


#: Known snapshot fields — used to load persisted data tolerantly (older files
#: lack newer fields, newer files must not crash older code readers).
_SNAPSHOT_FIELDS = {f.name for f in dataclass_fields(ContextUsageSnapshot)}


def _snapshot_from_dict(data: Dict[str, Any]) -> ContextUsageSnapshot:
    """Build a snapshot from a persisted dict, ignoring unknown keys and
    relying on dataclass defaults for missing ones."""
    return ContextUsageSnapshot(**{k: v for k, v in data.items() if k in _SNAPSHOT_FIELDS})


@dataclass
class AgentStats:
    """Statistics for a single agent."""
    agent_id: str
    agent_name: str
    total_calls: int = 0
    total_tokens: int = 0
    peak_tokens: int = 0
    message_count: int = 0
    last_activity: float = 0
    total_cached_tokens: int = 0  # Accumulated cached tokens (cache reads)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    # Cache-rate numerator and denominator, incremented in LOCKSTEP so the ratio
    # always covers the same calls. The all-time sums above cannot do that: they
    # were introduced at different times and keep accumulating across upgrades,
    # so an agent active before total_prompt_tokens existed carries cache reads
    # with no matching prompt tokens — measured in production at up to 799 %.
    cache_rate_cached_tokens: int = 0
    cache_rate_prompt_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cost: float = 0.0  # Sum of costs (billed + estimated; no-cost calls contribute 0)
    cost_known_calls: int = 0  # How many calls carried a cost at all
    cost_estimated_calls: int = 0  # ...of which were pricing-table estimates
    total_latency_ms: float = 0.0  # Sum of response times (ms) over calls that reported one
    latency_calls: int = 0  # How many calls carried a latency measurement


def _series(values: List[float]) -> Dict[str, Any]:
    """Current, min, max and average over a series that may be empty.

    Empty is not an error here: a window in which nothing carried a context
    (only decisions or synthesis calls) has no context series, and zeros say
    that. The panel draws the row either way.
    """
    if not values:
        return {"current": 0, "min": 0, "max": 0, "avg": 0.0}
    return {
        "current": values[-1],
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
    }


class UsageTracker:
    """Tracks context usage over time.

    Backed by SQLite, NOT by process memory. That is the whole point: the
    previous JSON file was read once at construction and rewritten in full on
    every change, so `agent-cli` and `agent-api` each kept a private copy and
    whichever wrote last destroyed the other's runs. Nothing here is cached
    between calls — a read sees what every process has written.

    ``max_history`` is now the QUERY WINDOW, not a storage cap: the panel keeps
    looking at the last N calls exactly as before, while the database retains
    far more for retention and later analysis.
    """

    def __init__(self, max_history: int = 1000, storage_path: Optional[Path] = None, name: str = "context_usage_tracker"):
        self.name = name
        self.max_history = max_history

        # Accepts the old .json path so existing config keeps working. The
        # database gets its OWN directory rather than sitting next to it: WAL
        # mode adds a -wal and a -shm file, and three files per store scattered
        # through data/ is what this is moving away from.
        self.storage_path = Path(storage_path) if storage_path else data_path(
            "context_usage_tracker.json")
        self.data_dir = self.storage_path.parent / self.storage_path.stem
        self.db_path = self.data_dir / "usage.db"
        self._db: Optional[UsageDatabase] = None
        self._db_lock = threading.Lock()

    @property
    def db(self) -> UsageDatabase:
        """The store, opened on FIRST USE rather than in the constructor.

        Constructing this plugin must not touch the filesystem: several tests
        boot the real config/config.yaml purely to get an Agent, which builds
        every plugin — and with eager initialisation that created a database in
        the production data directory and migrated the live JSON, from a test
        that never recorded anything.
        """
        db = self._db
        if db is None:
            with self._db_lock:
                db = self._db
                if db is None:
                    self.storage_path.parent.mkdir(parents=True, exist_ok=True)
                    db = UsageDatabase(self.db_path)
                    self._import_legacy_json(db)
                    # Published LAST, on purpose: the fast path above reads
                    # this attribute WITHOUT the lock, so assigning before the
                    # migration lets another thread record a snapshot into a
                    # half-set-up store — and the migration then sees a
                    # non-empty database and skips the legacy file for good.
                    self._db = db
        return db

    def record_usage(self,
                    agent_id: str,
                    agent_name: str,
                    session_id: str,
                    total_tokens: int,
                    prompt_tokens: int = 0,
                    completion_tokens: int = 0,
                    message_count: int = 0,
                    context_window: int = 0,
                    cached_tokens: int = 0,
                    tool_definition_tokens: int = 0,
                    cache_write_tokens: int = 0,
                    cost: Optional[float] = None,
                    cost_is_estimate: bool = False,
                    model: str = "",
                    request_id: str = "",
                    latency_ms: Optional[float] = None) -> None:
        """Record a context usage snapshot."""

        usage_percentage = (total_tokens / context_window * 100) if context_window > 0 else 0

        snapshot = ContextUsageSnapshot(
            timestamp=time.time(),
            agent_id=agent_id,
            agent_name=agent_name,
            session_id=session_id,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            message_count=message_count,
            context_window=context_window,
            usage_percentage=usage_percentage,
            cached_tokens=cached_tokens,
            tool_definition_tokens=tool_definition_tokens,
            cache_write_tokens=cache_write_tokens,
            cost=cost,
            cost_is_estimate=cost_is_estimate,
            model=model,
            request_id=request_id,
            latency_ms=latency_ms,
        )

        # One transaction: the snapshot, the agent's totals and clearing the
        # session's stale flag. Accumulation happens IN SQL, so two processes
        # recording at the same moment add up instead of overwriting each other.
        self.db.record(asdict(snapshot))

        logger.debug(
            f"📊 Context usage recorded: agent={agent_name}, "
            f"tokens={total_tokens} ({usage_percentage:.1f}%), "
            f"messages={message_count}"
        )

    def _window(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The query window: the newest ``max_history`` snapshots, oldest first.

        This is what the deque used to be. It is a WINDOW now, not a cap — the
        database keeps more, but the panel keeps reasoning about the same span.

        A session is narrowed in SQL, so its own window is ``max_history`` of
        ITS calls. Filtering a global window afterwards would hide a quiet
        session behind a busy one, which the process-local deque never did.
        """
        return self.db.recent_snapshots(self.max_history, session_id=session_id)

    def get_history(self, last_n: Optional[int] = None, session_id: Optional[str] = None,
                    agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get usage history as a list of dictionaries (optionally filtered).

        The session filter includes the session's sub-agent calls (see
        UsageDatabase.recent_snapshots).
        """
        history_list = self._window(session_id)
        if agent_id is not None:
            history_list = [s for s in history_list if s.get("agent_id") == agent_id]

        if last_n is not None:
            history_list = history_list[-last_n:]

        return history_list

    def get_latest(self, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get the latest usage snapshot.
        
        If the session has been invalidated (after context optimization), the
        returned snapshot will include 'is_stale': True to indicate the data
        may not reflect the current message list.
        
        Returns:
            Snapshot dict with optional 'is_stale' flag, or None if no data
        """
        if session_id is not None:
            result = self.db.latest_for_session(session_id)
            if result is None:
                return None
            if self.db.is_invalidated(session_id):
                result['is_stale'] = True
                logger.debug(
                    f"Session {session_id} usage data marked as stale - "
                    f"context was optimized since last LLM call"
                )
            return result

        return self.db.latest_snapshot()
    
    def invalidate_session(self, session_id: str, reason: str = "context_optimization") -> None:
        """Mark usage data for a session as stale after context optimization.
        
        This marks the session's snapshots with 'is_stale': True in get_latest(),
        indicating the token counts may not reflect the current message list.
        This prevents over-optimization when multiple context optimization hooks 
        run in sequence (e.g., context_engineer followed by context_summarizer).
        
        The data is preserved for display purposes (Web UI, history), but plugins
        should check the 'is_stale' flag and ignore actual token counts if set.
        
        Args:
            session_id: The session whose usage data should be marked stale
            reason: Reason for invalidation (for logging)
        """
        # In the database, not in a per-process set: the process that renders
        # the panel is usually NOT the one that ran the compaction, and a flag
        # only the writer can see marks nothing.
        self.db.invalidate_session(session_id)
        logger.info(
            f"📊 Marked usage data as stale for session {session_id} (reason: {reason})"
        )

    def get_agent_stats(self, session_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all agents."""
        if session_id is None:
            # All-time totals, held in their own table so retention can prune
            # snapshots without rewriting history.
            return self.db.agent_stats()

        # Per session: accumulated over the window, including sub-agent calls.
        session_snapshots = self._window(session_id)

        session_agent_stats: Dict[str, Dict[str, Any]] = {}
        for snapshot in session_snapshots:
            agent_id = snapshot.get("agent_id", "")
            if agent_id not in session_agent_stats:
                session_agent_stats[agent_id] = {
                    "agent_id": agent_id,
                    "agent_name": snapshot.get("agent_name", ""),
                    "total_calls": 0,
                    "total_tokens": 0,
                    "peak_tokens": 0,
                    "message_count": snapshot.get("message_count", 0),
                    "last_activity": snapshot.get("timestamp", 0),
                    "total_cached_tokens": 0,
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                    "total_cache_write_tokens": 0,
                    "total_cost": 0.0,
                    "cost_known_calls": 0,
                    "cost_estimated_calls": 0,
                    "total_latency_ms": 0.0,
                    "latency_calls": 0,
                }

            stats = session_agent_stats[agent_id]
            stats["total_calls"] += 1
            stats["total_tokens"] += snapshot.get("total_tokens", 0)
            stats["peak_tokens"] = max(stats["peak_tokens"], snapshot.get("total_tokens", 0))
            stats["message_count"] = snapshot.get("message_count", 0)
            stats["last_activity"] = max(stats["last_activity"], snapshot.get("timestamp", 0))
            stats["total_cached_tokens"] += snapshot.get("cached_tokens", 0)
            stats["total_prompt_tokens"] += snapshot.get("prompt_tokens", 0)
            stats["total_completion_tokens"] += snapshot.get("completion_tokens", 0)
            stats["total_cache_write_tokens"] += snapshot.get("cache_write_tokens", 0)
            if snapshot.get("cost") is not None:
                stats["total_cost"] += snapshot["cost"]
                stats["cost_known_calls"] += 1
                if snapshot.get("cost_is_estimate"):
                    stats["cost_estimated_calls"] += 1
            if snapshot.get("latency_ms") is not None:
                stats["total_latency_ms"] += snapshot["latency_ms"]
                stats["latency_calls"] += 1

        return session_agent_stats

    def get_statistics(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Get overall usage statistics."""
        history_list = self._window(session_id)

        if not history_list:
            return {"error": "No usage data available"}

        # "Context tokens" and "Context used" are one series the panel reads
        # two ways, so they take the same rows: only a call that HAS a window
        # fills one. A call without a conversation -- the decisions client, a
        # synthesis that reports tokens -- is recorded with context_window 0,
        # and its total is a per-call amount, not a context size. Mixing the
        # two halves the figure for how full the window ran, holds `min` at
        # the smallest stray call forever, and puts audio tokens on the same
        # line as a conversation. Those tokens are real SPEND and stay in
        # `totals` below, which is where spend is read.
        in_context = [s for s in history_list if s.get("context_window", 0) > 0]
        token_counts = [s.get("total_tokens", 0) for s in in_context]
        percentages = [s.get("usage_percentage", 0.0) for s in in_context]

        stats = {
            "timespan": {
                "start": history_list[0].get("timestamp", 0),
                "end": history_list[-1].get("timestamp", 0),
                "duration_seconds": (history_list[-1].get("timestamp", 0)
                                     - history_list[0].get("timestamp", 0)),
                "sample_count": len(history_list)
            },
            "tokens": _series(token_counts),
            "usage_percentage": _series(percentages),
        }

        # Cost / cache aggregates over the (filtered) window — the analysis
        # numbers the panel's overview cards are built from.
        costs = [s["cost"] for s in history_list if s.get("cost") is not None]
        prompt_sum = sum(s.get("prompt_tokens", 0) for s in history_list)
        completion_sum = sum(s.get("completion_tokens", 0) for s in history_list)
        cached_sum = sum(s.get("cached_tokens", 0) for s in history_list)
        stats["totals"] = {
            "cost": sum(costs),
            "cost_known_calls": len(costs),
            "cost_estimated_calls": sum(1 for s in history_list if s.get("cost_is_estimate")),
            "prompt_tokens": prompt_sum,
            "completion_tokens": completion_sum,
            "cached_tokens": cached_sum,
            "cache_write_tokens": sum(s.get("cache_write_tokens", 0) for s in history_list),
            "cache_hit_rate": (cached_sum / prompt_sum * 100) if prompt_sum > 0 else 0.0,
        }

        return stats

    def clear_history(self) -> None:
        """Clear all usage history."""
        self.db.clear()
        logger.info("🗑️  Context usage history cleared")

    # ------------------------------------------------------------------
    # One-time migration off the JSON file
    # ------------------------------------------------------------------
    #: Marker key: set once the legacy file has been imported.
    _LEGACY_MARKER = "legacy_json_imported"

    def _import_legacy_json(self, db: UsageDatabase) -> None:
        """Import a pre-SQLite usage file exactly once, across ALL processes.

        The file is NOT renamed or deleted. A marker row in the database is
        what makes this idempotent — moving the file would be a destructive
        side effect of merely *reading* usage data, and while a rollout is in
        progress an old-code process is still actively writing that very file.

        The marker is CLAIMED atomically before anything is read. A plain
        "check, then import, then mark" spans several transactions, and
        agent-api and agent-writer-worker are restarted together: reproduced
        with three simultaneous starts, the history landed twice.
        """
        if not self.storage_path.exists():
            return
        if not db.claim_once(self._LEGACY_MARKER):
            return  # another process owns it, or it already ran

        try:
            if db.count() or db.agent_stats():
                # Live data behind a fresh claim: a store from before the marker
                # existed, or one already in use. Importing on top would double
                # the totals, so record the decision and stop.
                db.set_meta(self._LEGACY_MARKER, "skipped-nonempty")
                return

            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            history = [_snapshot_from_dict(s) for s in (data.get("history") or [])]
            agent_fields = {f.name for f in dataclass_fields(AgentStats)}
            agents = {
                agent_id: AgentStats(**{**{k: v for k, v in stats.items()
                                           if k in agent_fields},
                                        "agent_id": stats.get("agent_id") or agent_id})
                for agent_id, stats in (data.get("agents") or {}).items()
            }

            # The all-time totals are taken as they stand and NOT recomputed
            # from the snapshots: the old history was a 1000-entry ring buffer,
            # so recomputation would silently reset every long-running agent to
            # whatever fits in that window.
            self._seed_cache_rate(agents, history)
            db.import_legacy(
                [asdict(s) for s in history],
                {aid: asdict(st) for aid, st in agents.items()},
            )
            db.set_meta(self._LEGACY_MARKER, str(time.time()))
            logger.info(
                f"📂 Migrated usage data to SQLite: {len(agents)} agents, "
                f"{len(history)} snapshots from {self.storage_path}. The file "
                f"is left in place and can be deleted once every process runs "
                f"this version."
            )
        except Exception as e:
            # A failed migration must not stop the plugin from working; it only
            # costs the old numbers. Give the claim back so a later start
            # retries instead of leaving the data stranded behind a marker.
            db.release_claim(self._LEGACY_MARKER)
            logger.warning(f"Failed to migrate usage data: {e}")

    @staticmethod
    def _seed_cache_rate(agents: Dict[str, AgentStats],
                         history: List[ContextUsageSnapshot]) -> None:
        """Fill an empty cache-rate pair from the retained snapshots.

        Files written before the pair existed carry all-time sums that cannot
        be divided (see :class:`AgentStats`), which would leave every agent
        without a rate until it next runs. The history holds per-call prompt
        and cached counts that ARE matched, so seeding from those states a
        measured window instead of an empty one.

        Only for agents whose pair is still empty: a file written after the
        pair existed already counted those very snapshots at record time, and
        seeding again would count them twice.
        """
        seeded: Dict[str, List[int]] = {}
        for snap in history:
            stats = agents.get(snap.agent_id)
            if stats is None or stats.cache_rate_prompt_tokens:
                continue
            acc = seeded.setdefault(snap.agent_id, [0, 0])
            acc[0] += snap.prompt_tokens
            acc[1] += snap.cached_tokens
        for agent_id, (prompt, cached) in seeded.items():
            stats = agents[agent_id]
            stats.cache_rate_prompt_tokens = prompt
            stats.cache_rate_cached_tokens = cached
        if seeded:
            logger.info("Seeded cache-rate window for %d agent(s) from history",
                        len(seeded))
