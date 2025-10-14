"""Context usage tracking for the context_usage_tracker plugin."""

import logging
import time
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
    
    def __init__(self, max_history: int = 1000):
        self.max_history = max_history
        self._history: deque = deque(maxlen=max_history)
        self._agent_stats: Dict[str, AgentStats] = {}
        self._lock = threading.Lock()
        self._latest_snapshot: Optional[ContextUsageSnapshot] = None
    
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
        logger.info("🗑️  Context usage history cleared")
