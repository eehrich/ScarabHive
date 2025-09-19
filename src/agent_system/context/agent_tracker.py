"""Per-agent context usage tracking and management."""

import logging
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AgentContextStats:
    """Statistics for a single agent's context usage."""
    agent_id: str
    agent_name: str
    context_window: int
    current_tokens: int = 0
    predicted_tokens: int = 0
    actual_tokens: int = 0
    message_count: int = 0
    summarization_count: int = 0
    last_activity: float = field(default_factory=time.time)
    session_start: float = field(default_factory=time.time)
    peak_tokens: int = 0
    total_llm_calls: int = 0
    total_tokens_processed: int = 0
    
    def update_activity(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()
    
    def get_context_usage_percent(self) -> float:
        """Get current context usage as percentage."""
        if self.context_window <= 0:
            return 0.0
        return (self.current_tokens / self.context_window) * 100
    
    def get_session_duration(self) -> float:
        """Get session duration in seconds."""
        return time.time() - self.session_start
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "context_window": self.context_window,
            "current_tokens": self.current_tokens,
            "predicted_tokens": self.predicted_tokens,
            "actual_tokens": self.actual_tokens,
            "message_count": self.message_count,
            "usage_percent": round(self.get_context_usage_percent(), 2),
            "summarization_count": self.summarization_count,
            "last_activity": self.last_activity,
            "session_duration": round(self.get_session_duration(), 2),
            "peak_tokens": self.peak_tokens,
            "total_llm_calls": self.total_llm_calls,
            "total_tokens_processed": self.total_tokens_processed,
            "efficiency_ratio": round(
                (self.actual_tokens / max(self.predicted_tokens, 1)) * 100, 2
            ) if self.predicted_tokens > 0 else 0
        }


class AgentContextTracker:
    """Global tracker for per-agent context usage."""
    
    def __init__(self):
        self._agents: Dict[str, AgentContextStats] = {}
        self._start_time = time.time()
    
    def register_agent(self, agent_id: str, agent_name: str, context_window: int) -> None:
        """Register a new agent for tracking."""
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentContextStats(
                agent_id=agent_id,
                agent_name=agent_name,
                context_window=context_window
            )
            logger.info(f"Registered agent {agent_id} ({agent_name}) for context tracking")
    
    def update_agent_context(
        self,
        agent_id: str,
        current_tokens: int,
        predicted_tokens: int,
        message_count: int,
        actual_tokens: Optional[int] = None
    ) -> None:
        """Update context usage for an agent."""
        if agent_id not in self._agents:
            logger.warning(f"Agent {agent_id} not registered, cannot update context")
            return
        
        stats = self._agents[agent_id]
        stats.current_tokens = current_tokens
        stats.predicted_tokens = predicted_tokens
        stats.message_count = message_count
        
        if actual_tokens is not None:
            stats.actual_tokens = actual_tokens
            stats.total_tokens_processed += actual_tokens
            stats.total_llm_calls += 1
        
        # Track peak usage
        if current_tokens > stats.peak_tokens:
            stats.peak_tokens = current_tokens
        
        stats.update_activity()
        
        logger.debug(
            f"Updated context for agent {agent_id}: "
            f"{current_tokens}/{stats.context_window} tokens "
            f"({stats.get_context_usage_percent():.1f}%)"
        )
    
    def record_summarization(self, agent_id: str) -> None:
        """Record that summarization occurred for an agent."""
        if agent_id in self._agents:
            self._agents[agent_id].summarization_count += 1
            self._agents[agent_id].update_activity()
            logger.debug(f"Recorded summarization for agent {agent_id}")
    
    def get_agent_stats(self, agent_id: str) -> Optional[AgentContextStats]:
        """Get stats for a specific agent."""
        return self._agents.get(agent_id)
    
    def get_all_agents(self) -> Dict[str, AgentContextStats]:
        """Get stats for all agents."""
        return self._agents.copy()
    
    def get_active_agents(self, inactive_threshold: float = 300) -> Dict[str, AgentContextStats]:
        """Get agents that were active within the threshold (seconds)."""
        current_time = time.time()
        return {
            agent_id: stats
            for agent_id, stats in self._agents.items()
            if (current_time - stats.last_activity) < inactive_threshold
        }
    
    def remove_agent(self, agent_id: str) -> bool:
        """Remove an agent from tracking."""
        if agent_id in self._agents:
            del self._agents[agent_id]
            logger.info(f"Removed agent {agent_id} from context tracking")
            return True
        return False
    
    def cleanup_inactive_agents(self, inactive_threshold: float = 3600) -> int:
        """Remove agents that have been inactive for too long."""
        current_time = time.time()
        inactive_agents = [
            agent_id for agent_id, stats in self._agents.items()
            if (current_time - stats.last_activity) > inactive_threshold
        ]
        
        for agent_id in inactive_agents:
            self.remove_agent(agent_id)
        
        if inactive_agents:
            logger.info(f"Cleaned up {len(inactive_agents)} inactive agents")
        
        return len(inactive_agents)
    
    def get_global_stats(self) -> Dict[str, Any]:
        """Get aggregated stats across all agents."""
        if not self._agents:
            return {
                "total_agents": 0,
                "active_agents": 0,
                "total_tokens": 0,
                "total_messages": 0,
                "total_summarizations": 0,
                "average_usage_percent": 0,
                "uptime": round(time.time() - self._start_time, 2)
            }
        
        active_agents = self.get_active_agents()
        
        total_tokens = sum(stats.current_tokens for stats in self._agents.values())
        total_messages = sum(stats.message_count for stats in self._agents.values())
        total_summarizations = sum(stats.summarization_count for stats in self._agents.values())
        
        # Calculate average usage percentage
        usage_percentages = [stats.get_context_usage_percent() for stats in self._agents.values()]
        avg_usage = sum(usage_percentages) / len(usage_percentages) if usage_percentages else 0
        
        return {
            "total_agents": len(self._agents),
            "active_agents": len(active_agents),
            "total_tokens": total_tokens,
            "total_messages": total_messages,
            "total_summarizations": total_summarizations,
            "average_usage_percent": round(avg_usage, 2),
            "uptime": round(time.time() - self._start_time, 2),
            "peak_concurrent_tokens": max(
                (stats.peak_tokens for stats in self._agents.values()), 
                default=0
            )
        }


# Global instance for application-wide agent tracking
_global_tracker: Optional[AgentContextTracker] = None


def get_agent_tracker() -> AgentContextTracker:
    """Get the global agent context tracker instance."""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = AgentContextTracker()
    return _global_tracker


def register_agent_for_tracking(agent_id: str, agent_name: str, context_window: int) -> None:
    """Register an agent for context tracking."""
    tracker = get_agent_tracker()
    tracker.register_agent(agent_id, agent_name, context_window)


def update_agent_context_usage(
    agent_id: str,
    current_tokens: int,
    predicted_tokens: int,
    message_count: int,
    actual_tokens: Optional[int] = None
) -> None:
    """Update context usage for an agent."""
    tracker = get_agent_tracker()
    tracker.update_agent_context(agent_id, current_tokens, predicted_tokens, message_count, actual_tokens)


def record_agent_summarization(agent_id: str) -> None:
    """Record that summarization occurred for an agent."""
    tracker = get_agent_tracker()
    tracker.record_summarization(agent_id)


def get_all_agent_stats() -> Dict[str, Dict[str, Any]]:
    """Get statistics for all tracked agents."""
    tracker = get_agent_tracker()
    all_agents = tracker.get_all_agents()
    return {agent_id: stats.to_dict() for agent_id, stats in all_agents.items()}