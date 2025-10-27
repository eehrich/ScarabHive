"""Context usage tracking for the context_usage_tracker plugin."""

import logging
import time
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from collections import deque
import threading

logger = logging.getLogger(__name__)


@dataclass
class ContextUsageSnapshot:
    """A snapshot of context usage at a specific time."""
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


class UsageTracker:
    """Tracks context usage over time."""
    
    def __init__(self, max_history: int = 1000, storage_path: Optional[Path] = None, name: str = "context_usage_tracker"):
        self.name = name
        self.max_history = max_history
        self._history: deque = deque(maxlen=max_history)
        self._agent_stats: Dict[str, AgentStats] = {}
        self._lock = threading.Lock()
        self._latest_snapshot: Optional[ContextUsageSnapshot] = None
        
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
                    context_window: int = 0) -> None:
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
        )
        
        with self._lock:
            self._history.append(snapshot)
            self._latest_snapshot = snapshot
            
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
            
        self._save_to_disk()
        
        logger.debug(
            f"📊 Context usage recorded: agent={agent_name}, "
            f"tokens={total_tokens} ({usage_percentage:.1f}%), "
            f"messages={message_count}"
        )
    
    def get_history(self, last_n: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get usage history as a list of dictionaries."""
        with self._lock:
            history_list = list(self._history)
            
        if last_n is not None:
            history_list = history_list[-last_n:]
            
        return [asdict(snapshot) for snapshot in history_list]
    
    def get_latest(self) -> Optional[Dict[str, Any]]:
        """Get the latest usage snapshot."""
        with self._lock:
            if self._latest_snapshot:
                return asdict(self._latest_snapshot)
        return None
    
    def get_agent_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all agents."""
        with self._lock:
            return {
                agent_id: {
                    "agent_id": stats.agent_id,
                    "agent_name": stats.agent_name,
                    "total_calls": stats.total_calls,
                    "total_tokens": stats.total_tokens,
                    "peak_tokens": stats.peak_tokens,
                    "message_count": stats.message_count,
                    "last_activity": stats.last_activity,
                }
                for agent_id, stats in self._agent_stats.items()
            }
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get overall usage statistics."""
        with self._lock:
            history_list = list(self._history)
        
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
        
        return stats
    
    def clear_history(self) -> None:
        """Clear all usage history."""
        with self._lock:
            self._history.clear()
            self._agent_stats.clear()
            self._latest_snapshot = None
        self._save_to_disk()
        logger.info("🗑️  Context usage history cleared")
    
    def _save_to_disk(self) -> None:
        """Save agent stats and history to disk."""
        try:
            # Capture data while holding lock briefly
            with self._lock:
                agents_data = {
                    agent_id: {
                        "agent_id": stats.agent_id,
                        "agent_name": stats.agent_name,
                        "total_calls": stats.total_calls,
                        "total_tokens": stats.total_tokens,
                        "peak_tokens": stats.peak_tokens,
                        "message_count": stats.message_count,
                        "last_activity": stats.last_activity
                    }
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
    
    def _load_from_disk(self) -> None:
        """Load agent stats and history from disk."""
        if not self.storage_path.exists():
            logger.debug(f"No existing usage data at {self.storage_path}")
            return
        
        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Load agent stats
            agents_data = data.get("agents", {})
            with self._lock:
                for agent_id, stats_dict in agents_data.items():
                    self._agent_stats[agent_id] = AgentStats(
                        agent_id=stats_dict["agent_id"],
                        agent_name=stats_dict["agent_name"],
                        total_calls=stats_dict.get("total_calls", 0),
                        total_tokens=stats_dict.get("total_tokens", 0),
                        peak_tokens=stats_dict.get("peak_tokens", 0),
                        message_count=stats_dict.get("message_count", 0),
                        last_activity=stats_dict.get("last_activity", 0)
                    )
                
                # Load history snapshots
                history_data = data.get("history", [])
                for snapshot_dict in history_data:
                    snapshot = ContextUsageSnapshot(**snapshot_dict)
                    self._history.append(snapshot)
                
                # Load latest snapshot
                latest_data = data.get("latest")
                if latest_data:
                    self._latest_snapshot = ContextUsageSnapshot(**latest_data)
            
            logger.info(
                f"📂 Loaded usage data: {len(agents_data)} agents, "
                f"{len(history_data)} snapshots from {self.storage_path}"
            )
        except Exception as e:
            logger.warning(f"Failed to load usage data: {e}")
