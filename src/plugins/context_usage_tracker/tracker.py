"""Context usage tracking for the context_usage_tracker plugin."""

import asyncio
import concurrent.futures
import logging
import time
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict, fields as dataclass_fields
from collections import deque
import threading

logger = logging.getLogger(__name__)

# Thread pool for async disk I/O - single thread to avoid file contention
_io_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _get_io_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Get or create the I/O thread pool."""
    global _io_executor
    if _io_executor is None:
        _io_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="context_tracker_io"
        )
    return _io_executor


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
    cost: Optional[float] = None  # billed cost in USD (None if provider sent none)
    model: str = ""  # model identifier of the client that served the call
    request_id: str = ""  # request this call belonged to


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
    total_cache_write_tokens: int = 0
    total_cost: float = 0.0  # Sum of billed costs (calls without cost contribute 0)
    cost_known_calls: int = 0  # How many calls actually carried a cost


class UsageTracker:
    """Tracks context usage over time."""

    def __init__(self, max_history: int = 1000, storage_path: Optional[Path] = None, name: str = "context_usage_tracker"):
        self.name = name
        self.max_history = max_history
        self._history: deque = deque(maxlen=max_history)
        self._agent_stats: Dict[str, AgentStats] = {}
        self._lock = threading.Lock()
        self._latest_snapshot: Optional[ContextUsageSnapshot] = None
        self._pending_save: bool = False  # Debounce flag for saves
        self._save_interval: float = 5.0  # Save at most every 5 seconds
        
        # Track invalidated sessions - snapshots for these sessions should be ignored
        # until a new snapshot is recorded. This prevents stale data from being used
        # after context optimization (compaction/summarization).
        self._invalidated_sessions: set = set()

        self.storage_path = storage_path or Path("data/context_usage_tracker.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

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
                    model: str = "",
                    request_id: str = "") -> None:
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
            model=model,
            request_id=request_id,
        )

        with self._lock:
            self._history.append(snapshot)
            self._latest_snapshot = snapshot
            
            # Clear invalidation flag - new snapshot means fresh data
            self._invalidated_sessions.discard(session_id)

            # Update agent stats
            if agent_id not in self._agent_stats:
                self._agent_stats[agent_id] = AgentStats(
                    agent_id=agent_id,
                    agent_name=agent_name
                )

            stats = self._agent_stats[agent_id]
            stats.total_calls += 1
            stats.total_tokens += total_tokens
            stats.peak_tokens = max(stats.peak_tokens, total_tokens)
            stats.message_count = message_count
            stats.last_activity = time.time()
            stats.total_cached_tokens += cached_tokens
            stats.total_prompt_tokens += prompt_tokens
            stats.total_completion_tokens += completion_tokens
            stats.total_cache_write_tokens += cache_write_tokens
            if cost is not None:
                stats.total_cost += cost
                stats.cost_known_calls += 1

        # Schedule async save (debounced to avoid too frequent writes)
        self._schedule_save()

        logger.debug(
            f"📊 Context usage recorded: agent={agent_name}, "
            f"tokens={total_tokens} ({usage_percentage:.1f}%), "
            f"messages={message_count}"
        )

    @staticmethod
    def _filter_session_tree(history_list: List["ContextUsageSnapshot"],
                             session_id: str) -> List["ContextUsageSnapshot"]:
        """Calls belonging to a session INCLUDING its sub-agents (any depth).

        Sub-agent calls run in their own sub-sessions, so a plain session_id
        match hides them. Their request_ids are hierarchical
        (``<parent_request>_sub_<id>``, transitively for sub-sub-agents — see
        sub_agent_manager), so the session's own request ids expand the filter
        to the whole tree. Calls recorded before request_ids existed (pre-2.0
        snapshots) can only match by exact session.
        """
        own_requests = {s.request_id for s in history_list
                        if s.session_id == session_id and s.request_id}
        prefixes = tuple(r + "_" for r in own_requests)
        return [
            s for s in history_list
            if s.session_id == session_id
            or (prefixes and s.request_id and s.request_id.startswith(prefixes))
        ]

    def get_history(self, last_n: Optional[int] = None, session_id: Optional[str] = None,
                    agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get usage history as a list of dictionaries (optionally filtered).

        The session filter includes the session's sub-agent calls (see
        _filter_session_tree).
        """
        with self._lock:
            history_list = list(self._history)

        # Filter by session_id (incl. sub-agent tree) / agent_id if provided
        if session_id is not None:
            history_list = self._filter_session_tree(history_list, session_id)
        if agent_id is not None:
            history_list = [s for s in history_list if s.agent_id == agent_id]

        if last_n is not None:
            history_list = history_list[-last_n:]

        return [asdict(snapshot) for snapshot in history_list]

    def get_latest(self, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get the latest usage snapshot.
        
        If the session has been invalidated (after context optimization), the
        returned snapshot will include 'is_stale': True to indicate the data
        may not reflect the current message list.
        
        Returns:
            Snapshot dict with optional 'is_stale' flag, or None if no data
        """
        with self._lock:
            if session_id is not None:
                is_stale = session_id in self._invalidated_sessions
                
                # Filter by session and get the latest
                session_snapshots = [s for s in self._history if s.session_id == session_id]
                if session_snapshots:
                    result = asdict(session_snapshots[-1])
                    if is_stale:
                        result['is_stale'] = True
                        logger.debug(
                            f"Session {session_id} usage data marked as stale - "
                            f"context was optimized since last LLM call"
                        )
                    return result
                return None

            if self._latest_snapshot:
                return asdict(self._latest_snapshot)
        return None
    
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
        with self._lock:
            self._invalidated_sessions.add(session_id)
        logger.info(
            f"📊 Marked usage data as stale for session {session_id} (reason: {reason})"
        )

    def get_agent_stats(self, session_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all agents."""
        with self._lock:
            if session_id is not None:
                # Calculate stats from filtered history (incl. sub-agent calls)
                session_snapshots = self._filter_session_tree(list(self._history), session_id)

                # Build stats from session-filtered snapshots
                session_agent_stats: Dict[str, Dict[str, Any]] = {}
                for snapshot in session_snapshots:
                    agent_id = snapshot.agent_id
                    if agent_id not in session_agent_stats:
                        session_agent_stats[agent_id] = {
                            "agent_id": snapshot.agent_id,
                            "agent_name": snapshot.agent_name,
                            "total_calls": 0,
                            "total_tokens": 0,
                            "peak_tokens": 0,
                            "message_count": snapshot.message_count,
                            "last_activity": snapshot.timestamp,
                            "total_cached_tokens": 0,
                            "total_prompt_tokens": 0,
                            "total_completion_tokens": 0,
                            "total_cache_write_tokens": 0,
                            "total_cost": 0.0,
                            "cost_known_calls": 0,
                        }

                    stats = session_agent_stats[agent_id]
                    stats["total_calls"] += 1
                    stats["total_tokens"] += snapshot.total_tokens
                    stats["peak_tokens"] = max(stats["peak_tokens"], snapshot.total_tokens)
                    stats["message_count"] = snapshot.message_count
                    stats["last_activity"] = max(stats["last_activity"], snapshot.timestamp)
                    stats["total_cached_tokens"] += getattr(snapshot, 'cached_tokens', 0)
                    stats["total_prompt_tokens"] += snapshot.prompt_tokens
                    stats["total_completion_tokens"] += snapshot.completion_tokens
                    stats["total_cache_write_tokens"] += getattr(snapshot, 'cache_write_tokens', 0)
                    if getattr(snapshot, 'cost', None) is not None:
                        stats["total_cost"] += snapshot.cost
                        stats["cost_known_calls"] += 1

                return session_agent_stats

            # Return global stats
            return {agent_id: asdict(stats) for agent_id, stats in self._agent_stats.items()}

    def get_statistics(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Get overall usage statistics."""
        with self._lock:
            history_list = list(self._history)

        # Filter by session_id if provided (incl. the session's sub-agent calls)
        if session_id is not None:
            history_list = self._filter_session_tree(history_list, session_id)

        if not history_list:
            return {"error": "No usage data available"}

        # Calculate statistics
        token_counts = [s.total_tokens for s in history_list]
        percentages = [s.usage_percentage for s in history_list]

        stats = {
            "timespan": {
                "start": history_list[0].timestamp,
                "end": history_list[-1].timestamp,
                "duration_seconds": history_list[-1].timestamp - history_list[0].timestamp,
                "sample_count": len(history_list)
            },
            "tokens": {
                "current": token_counts[-1],
                "min": min(token_counts),
                "max": max(token_counts),
                "avg": sum(token_counts) / len(token_counts)
            },
            "usage_percentage": {
                "current": percentages[-1],
                "min": min(percentages),
                "max": max(percentages),
                "avg": sum(percentages) / len(percentages)
            },
        }

        # Cost / cache aggregates over the (filtered) window — the analysis
        # numbers the panel's overview cards are built from.
        costs = [s.cost for s in history_list if s.cost is not None]
        prompt_sum = sum(s.prompt_tokens for s in history_list)
        completion_sum = sum(s.completion_tokens for s in history_list)
        cached_sum = sum(s.cached_tokens for s in history_list)
        stats["totals"] = {
            "cost": sum(costs),
            "cost_known_calls": len(costs),
            "prompt_tokens": prompt_sum,
            "completion_tokens": completion_sum,
            "cached_tokens": cached_sum,
            "cache_write_tokens": sum(s.cache_write_tokens for s in history_list),
            "cache_hit_rate": (cached_sum / prompt_sum * 100) if prompt_sum > 0 else 0.0,
        }

        return stats

    def clear_history(self) -> None:
        """Clear all usage history."""
        with self._lock:
            self._history.clear()
            self._agent_stats.clear()
            self._latest_snapshot = None
            self._invalidated_sessions.clear()
        self._save_to_disk_sync()  # Immediate sync save for clear operation
        logger.info("🗑️  Context usage history cleared")

    def _schedule_save(self) -> None:
        """Schedule an async save operation (debounced)."""
        if self._pending_save:
            return  # Already scheduled
        
        self._pending_save = True
        
        try:
            loop = asyncio.get_running_loop()
            # Schedule the save in the background
            loop.call_later(self._save_interval, self._execute_async_save)
        except RuntimeError:
            # No running event loop, fall back to sync save in thread pool
            executor = _get_io_executor()
            executor.submit(self._delayed_sync_save)
    
    def _delayed_sync_save(self) -> None:
        """Delayed sync save for when no event loop is available."""
        time.sleep(self._save_interval)
        self._pending_save = False
        self._save_to_disk_sync()
    
    def _execute_async_save(self) -> None:
        """Execute the async save operation."""
        self._pending_save = False
        try:
            loop = asyncio.get_running_loop()
            # Run the blocking save in thread pool
            loop.run_in_executor(_get_io_executor(), self._save_to_disk_sync)
        except RuntimeError:
            # No event loop, run synchronously in thread pool
            executor = _get_io_executor()
            executor.submit(self._save_to_disk_sync)

    def _save_to_disk_sync(self) -> None:
        """Save agent stats and history to disk (synchronous, runs in thread pool)."""
        try:
            # Capture data while holding lock briefly
            with self._lock:
                agents_data = {
                    agent_id: asdict(stats)
                    for agent_id, stats in self._agent_stats.items()
                }

                # Save history snapshots (as list of dicts)
                history_data = [asdict(snapshot) for snapshot in self._history]

                # Save latest snapshot
                latest_data = asdict(self._latest_snapshot) if self._latest_snapshot else None

            # Write to disk WITHOUT holding lock
            data = {
                "agents": agents_data,
                "history": history_data,
                "latest": latest_data,
                "last_updated": time.time()
            }

            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"💾 Saved usage data to {self.storage_path} ({len(history_data)} snapshots)")
        except Exception as e:
            logger.error(f"Failed to save usage data: {e}")

    def force_save(self) -> None:
        """Force immediate synchronous save to disk (for testing/shutdown)."""
        self._pending_save = False
        self._save_to_disk_sync()

    def _load_from_disk(self) -> None:
        """Load agent stats and history from disk."""
        if not self.storage_path.exists():
            logger.debug(f"No existing usage data at {self.storage_path}")
            return

        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Load agent stats (tolerant: unknown keys ignored, missing keys
            # fall back to dataclass defaults — files written by older or newer
            # versions both load).
            agent_fields = {f.name for f in dataclass_fields(AgentStats)}
            agents_data = data.get("agents", {})
            with self._lock:
                for agent_id, stats_dict in agents_data.items():
                    self._agent_stats[agent_id] = AgentStats(
                        **{k: v for k, v in stats_dict.items() if k in agent_fields}
                    )

                # Load history snapshots (same tolerance via _snapshot_from_dict)
                history_data = data.get("history", [])
                for snapshot_dict in history_data:
                    self._history.append(_snapshot_from_dict(snapshot_dict))

                # Load latest snapshot
                latest_data = data.get("latest")
                if latest_data:
                    self._latest_snapshot = _snapshot_from_dict(latest_data)

            logger.info(
                f"📂 Loaded usage data: {len(agents_data)} agents, "
                f"{len(history_data)} snapshots from {self.storage_path}"
            )
        except Exception as e:
            logger.warning(f"Failed to load usage data: {e}")
