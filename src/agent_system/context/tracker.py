"""Context usage tracking service for monitoring token consumption over time."""

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
    total_tokens: int
    user_tokens: int
    assistant_tokens: int
    tool_call_tokens: int
    tool_result_tokens: int
    system_tokens: int
    message_count: int
    context_window: int
    usage_percentage: float
    warning_level: Optional[str] = None
    management_triggered: bool = False
    management_strategy: Optional[str] = None


class ContextUsageTracker:
    """Tracks context usage over time for monitoring and debugging."""
    
    def __init__(self, max_history: int = 1000):
        self.max_history = max_history
        self._history: deque = deque(maxlen=max_history)
        self._lock = threading.Lock()
        self._latest_snapshot: Optional[ContextUsageSnapshot] = None
    
    def record_usage(self, 
                    total_tokens: int,
                    user_tokens: int = 0,
                    assistant_tokens: int = 0,
                    tool_call_tokens: int = 0,
                    tool_result_tokens: int = 0,
                    system_tokens: int = 0,
                    message_count: int = 0,
                    context_window: int = 0,
                    warning_level: Optional[str] = None,
                    management_triggered: bool = False,
                    management_strategy: Optional[str] = None) -> None:
        """Record a context usage snapshot."""
        
        usage_percentage = (total_tokens / context_window * 100) if context_window > 0 else 0
        
        snapshot = ContextUsageSnapshot(
            timestamp=time.time(),
            total_tokens=total_tokens,
            user_tokens=user_tokens,
            assistant_tokens=assistant_tokens,
            tool_call_tokens=tool_call_tokens,
            tool_result_tokens=tool_result_tokens,
            system_tokens=system_tokens,
            message_count=message_count,
            context_window=context_window,
            usage_percentage=usage_percentage,
            warning_level=warning_level,
            management_triggered=management_triggered,
            management_strategy=management_strategy
        )
        
        with self._lock:
            self._history.append(snapshot)
            self._latest_snapshot = snapshot
            
        logger.debug("📊 Context usage recorded: %d tokens (%.1f%%), %d messages", 
                    total_tokens, usage_percentage, message_count)
    
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
    
    def get_statistics(self, time_window_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Get usage statistics for a time window."""
        with self._lock:
            history_list = list(self._history)
        
        if not history_list:
            return {"error": "No usage data available"}
        
        # Filter by time window if specified
        if time_window_seconds is not None:
            cutoff_time = time.time() - time_window_seconds
            history_list = [s for s in history_list if s.timestamp >= cutoff_time]
        
        if not history_list:
            return {"error": "No usage data in specified time window"}
        
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
            "warnings": {
                "total_warnings": sum(1 for s in history_list if s.warning_level),
                "management_triggers": sum(1 for s in history_list if s.management_triggered),
                "last_warning": next((s.warning_level for s in reversed(history_list) if s.warning_level), None)
            }
        }
        
        return stats
    
    def clear_history(self) -> None:
        """Clear all usage history."""
        with self._lock:
            self._history.clear()
            self._latest_snapshot = None
        logger.info("🗑️  Context usage history cleared")


# Global tracker instance
_global_tracker: Optional[ContextUsageTracker] = None


def get_tracker() -> ContextUsageTracker:
    """Get the global context usage tracker."""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = ContextUsageTracker()
    return _global_tracker


def record_context_usage(**kwargs) -> None:
    """Convenient function to record context usage."""
    get_tracker().record_usage(**kwargs)