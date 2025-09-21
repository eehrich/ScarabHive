"""Cross-session accumulated statistics for agent token usage."""

import json
import logging
import time
from typing import Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class TokenAccumulator:
    """Manages accumulated token usage statistics across sessions."""

    def __init__(self, storage_file: Optional[str] = None):
        """Initialize accumulator with persistent storage.

        Args:
            storage_file: Path to JSON file for persistent storage.
                         If None, uses 'logs/agent_token_accumulator.json'
        """
        if storage_file is None:
            # Default to logs directory
            logs_dir = Path("logs")
            logs_dir.mkdir(exist_ok=True)
            storage_file = logs_dir / "agent_token_accumulator.json"

        self.storage_file = Path(storage_file)
        self._accumulated_stats: Dict[str, Dict[str, Any]] = {}
        self._session_start = time.time()

        # Load existing accumulated data
        self._load_accumulated_stats()

    def _load_accumulated_stats(self) -> None:
        """Load accumulated statistics from persistent storage."""
        if self.storage_file.exists():
            try:
                with open(self.storage_file, 'r') as f:
                    data = json.load(f)
                    self._accumulated_stats = data.get("agents", {})
                    logger.debug(f"Loaded accumulated stats for {len(self._accumulated_stats)} agents")
            except Exception as e:
                logger.warning(f"Failed to load accumulated stats from {self.storage_file}: {e}")
                self._accumulated_stats = {}
        else:
            logger.debug(f"No existing accumulated stats file found at {self.storage_file}")
            self._accumulated_stats = {}

    def _save_accumulated_stats(self) -> None:
        """Save accumulated statistics to persistent storage."""
        try:
            data = {
                "last_updated": time.time(),
                "agents": self._accumulated_stats
            }

            # Ensure directory exists
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically using temp file
            temp_file = self.storage_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)

            # Atomic rename
            temp_file.replace(self.storage_file)
            logger.debug(f"Saved accumulated stats to {self.storage_file}")

        except Exception as e:
            logger.error(f"Failed to save accumulated stats to {self.storage_file}: {e}")

    def record_llm_usage(self, agent_id: str, agent_name: str, tokens: int) -> None:
        """Record LLM token usage for an agent.

        Args:
            agent_id: Unique agent identifier
            agent_name: Human-readable agent name
            tokens: Number of tokens used in this LLM call
        """
        if agent_id not in self._accumulated_stats:
            self._accumulated_stats[agent_id] = {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "total_tokens": 0,
                "total_calls": 0,
                "first_seen": time.time(),
                "last_activity": time.time(),
                "sessions": 0
            }
            # New agent - increment session count
            self._accumulated_stats[agent_id]["sessions"] = 1

        # Update accumulated stats
        stats = self._accumulated_stats[agent_id]
        stats["total_tokens"] += tokens
        stats["total_calls"] += 1
        stats["last_activity"] = time.time()
        stats["agent_name"] = agent_name  # Update in case it changed

        logger.debug(f"Recorded {tokens} tokens for agent {agent_id} (total: {stats['total_tokens']})")

        # Save to disk (could be optimized to batch writes)
        self._save_accumulated_stats()

    def get_agent_accumulated_stats(self, agent_id: str) -> Dict[str, Any]:
        """Get accumulated statistics for a specific agent.

        Args:
            agent_id: Agent identifier

        Returns:
            Dictionary with accumulated statistics, or empty dict if agent not found
        """
        return self._accumulated_stats.get(agent_id, {}).copy()

    def get_all_accumulated_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get accumulated statistics for all agents.

        Returns:
            Dictionary mapping agent_id to accumulated statistics
        """
        return {agent_id: stats.copy() for agent_id, stats in self._accumulated_stats.items()}

    def get_global_accumulated_stats(self) -> Dict[str, Any]:
        """Get aggregated statistics across all agents.

        Returns:
            Dictionary with global totals and statistics
        """
        if not self._accumulated_stats:
            return {
                "total_agents": 0,
                "total_tokens": 0,
                "total_calls": 0,
                "session_duration": time.time() - self._session_start,
                "average_tokens_per_call": 0
            }

        total_tokens = sum(stats.get("total_tokens", 0) for stats in self._accumulated_stats.values())
        total_calls = sum(stats.get("total_calls", 0) for stats in self._accumulated_stats.values())

        return {
            "total_agents": len(self._accumulated_stats),
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "session_duration": time.time() - self._session_start,
            "average_tokens_per_call": round(total_tokens / max(total_calls, 1), 2),
            "most_active_agent": max(
                self._accumulated_stats.items(),
                key=lambda x: x[1].get("total_tokens", 0)
            )[0] if self._accumulated_stats else None
        }

    def reset_agent_stats(self, agent_id: str) -> bool:
        """Reset accumulated statistics for a specific agent.

        Args:
            agent_id: Agent identifier

        Returns:
            True if agent was found and reset, False otherwise
        """
        if agent_id in self._accumulated_stats:
            del self._accumulated_stats[agent_id]
            self._save_accumulated_stats()
            logger.debug(f"Reset accumulated stats for agent {agent_id}")
            return True
        return False

    def reset_all_stats(self) -> None:
        """Reset all accumulated statistics."""
        self._accumulated_stats = {}
        self._save_accumulated_stats()
        logger.debug("Reset all accumulated statistics")


# Global accumulator instance
_global_accumulator: Optional[TokenAccumulator] = None


def get_token_accumulator() -> TokenAccumulator:
    """Get the global token accumulator instance."""
    global _global_accumulator
    if _global_accumulator is None:
        _global_accumulator = TokenAccumulator()
    return _global_accumulator


def record_agent_llm_usage(agent_id: str, agent_name: str, tokens: int) -> None:
    """Record LLM token usage for an agent (global convenience function)."""
    accumulator = get_token_accumulator()
    accumulator.record_llm_usage(agent_id, agent_name, tokens)


def get_agent_accumulated_stats(agent_id: str) -> Dict[str, Any]:
    """Get accumulated statistics for a specific agent (global convenience function)."""
    accumulator = get_token_accumulator()
    return accumulator.get_agent_accumulated_stats(agent_id)


def get_all_accumulated_stats() -> Dict[str, Dict[str, Any]]:
    """Get accumulated statistics for all agents (global convenience function)."""
    accumulator = get_token_accumulator()
    return accumulator.get_all_accumulated_stats()


def get_global_accumulated_stats() -> Dict[str, Any]:
    """Get aggregated statistics across all agents (global convenience function)."""
    accumulator = get_token_accumulator()
    return accumulator.get_global_accumulated_stats()