"""Core Memory - Compact facts store always in context.

Core memory provides a small, always-present section in the system prompt
containing important facts, decisions, and preferences. It's inspired by
the MemGPT architecture where "core memory" acts like RAM - always available
to the LLM.

Key features:
- Maximum token limit (default 2000 tokens)
- Importance-based eviction when limit exceeded
- Categorized facts (decisions, preferences, facts, context, tasks)
- Automatic serialization to system prompt section
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_system.llm.token_utils import estimate_content_tokens

logger = logging.getLogger(__name__)


@dataclass
class Fact:
    """A single fact stored in core memory."""
    content: str
    category: str
    importance: float  # 0.0 - 1.0
    created_at: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    last_accessed: datetime | None = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "content": self.content,
            "category": self.category,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "access_count": self.access_count,
            "last_accessed": self.last_accessed.isoformat() if self.last_accessed else None
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fact":
        """Create from dictionary."""
        return cls(
            content=data["content"],
            category=data["category"],
            importance=data["importance"],
            created_at=datetime.fromisoformat(data["created_at"]),
            access_count=data.get("access_count", 0),
            last_accessed=datetime.fromisoformat(data["last_accessed"]) if data.get("last_accessed") else None
        )


class CoreMemory:
    """Compact, always-in-context facts storage.
    
    Core memory is designed to hold critical information that should always
    be available to the LLM without requiring retrieval. It has a strict
    token limit and uses importance-based eviction.
    
    Usage:
        core = CoreMemory(max_tokens=2000)
        core.add_fact("User prefers Python 3.11+", category="preferences", importance=0.8)
        core.add_fact("Decided to use SQLite", category="decisions", importance=0.9)
        
        # Get formatted section for system prompt
        section = core.to_system_prompt_section()
    """
    
    CATEGORIES = ["decisions", "preferences", "facts", "context", "tasks"]
    
    def __init__(
        self,
        storage_path: Path | str | None = None,
        max_tokens: int = 2000,
        session_id: str | None = None
    ):
        """Initialize core memory.
        
        Args:
            storage_path: Path to persist core memory (optional)
            max_tokens: Maximum tokens for the core memory section
            session_id: Session ID for isolation
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self.max_tokens = max_tokens
        self.session_id = session_id
        self.facts: list[Fact] = []
        self._current_tokens = 0
        
        # Load from storage if exists
        if storage_path and storage_path.exists():
            self._load()
    
    async def add_fact(
        self,
        content: str,
        category: str = "facts",
        importance: float = 0.5
    ) -> bool:
        """Add a fact to core memory.
        
        Args:
            content: The fact content (keep concise)
            category: Category for organization
            importance: Importance score (0.0-1.0), higher = less likely to be evicted
            
        Returns:
            True if fact was added, False if rejected
        """
        # Validate category
        if category not in self.CATEGORIES:
            logger.warning(f"Unknown category '{category}', defaulting to 'facts'")
            category = "facts"
        
        # Clamp importance
        importance = max(0.0, min(1.0, importance))
        
        # Check for duplicates (same content)
        for existing in self.facts:
            if existing.content.strip().lower() == content.strip().lower():
                # Update importance if higher — and keep it: without the save
                # the raise lived only until the process ended.
                if importance > existing.importance:
                    existing.importance = importance
                    logger.debug(f"Updated importance for existing fact: {content[:50]}...")
                    if self.storage_path:
                        await self._save()
                return True
        
        # Create new fact
        fact = Fact(
            content=content.strip(),
            category=category,
            importance=importance
        )
        
        # Make room only at the expense of facts that are not MORE important,
        # least important and then oldest first — and decide before evicting
        # anything. The loop this replaces evicted the least important EXISTING
        # fact whatever the new one was worth, and ran until the list was empty:
        # a 0.05 fact pushed out a 0.95 one, and a fact larger than the whole
        # budget wiped every fact and was then added anyway. Equal importance
        # must still rotate: the tool stores at 0.5 by default, and a memory
        # full of 0.5 facts would otherwise refuse every new one.
        # Counted fact by fact (_fact_tokens), the same way everywhere. The
        # rendered section is no stable measure: the estimator scores a whole
        # text by one content type, so removing the last fact that mentions
        # "from " or "class " re-scored all the others, and one short fact
        # evicted 51 facts where 2 were enough.
        overflow = self._current_tokens + self._fact_tokens(fact) - self.max_tokens
        victims: list[Fact] = []
        freed = 0
        # sorted() is stable and self.facts is in insertion order: oldest first.
        for candidate in sorted((f for f in self.facts if f.importance <= importance),
                                key=lambda f: f.importance):
            if freed >= overflow:
                break
            victims.append(candidate)
            freed += self._fact_tokens(candidate)
        if freed < overflow:
            logger.warning(
                f"Cannot add fact - core memory is full of more important facts: "
                f"{content[:50]}...")
            return False
        for victim in victims:
            self.facts.remove(victim)
            logger.debug(
                f"Evicted fact for a more important one: '{victim.content[:50]}...' "
                f"(importance={victim.importance:.2f})")

        # Add the fact
        self.facts.append(fact)
        self._recalculate_tokens()

        logger.debug(
            f"Added fact to core memory: category={category}, importance={importance:.2f}, "
            f"total={self._current_tokens}/{self.max_tokens}"
        )
        
        # Persist if storage configured
        if self.storage_path:
            await self._save()
        
        return True
    
    def get_facts(self, category: str | None = None) -> list[Fact]:
        """Get facts, optionally filtered by category.
        
        Args:
            category: Optional category filter
            
        Returns:
            List of facts
        """
        if category:
            return [f for f in self.facts if f.category == category]
        return list(self.facts)
    
    async def remove_fact(self, content: str) -> bool:
        """Remove a specific fact by content.
        
        Args:
            content: The fact content to remove
            
        Returns:
            True if removed, False if not found
        """
        for i, fact in enumerate(self.facts):
            if fact.content.strip().lower() == content.strip().lower():
                removed = self.facts.pop(i)
                self._recalculate_tokens()
                logger.debug(f"Removed fact from core memory: {removed.content[:50]}...")
                if self.storage_path:
                    await self._save()
                return True
        return False
    
    async def clear(self, category: str | None = None) -> int:
        """Clear facts, optionally only a specific category.
        
        Args:
            category: Optional category to clear (None = clear all)
            
        Returns:
            Number of facts cleared
        """
        if category:
            original_count = len(self.facts)
            self.facts = [f for f in self.facts if f.category != category]
            cleared = original_count - len(self.facts)
        else:
            cleared = len(self.facts)
            self.facts = []
        
        self._recalculate_tokens()
        
        if self.storage_path:
            await self._save()
        
        return cleared
    
    def to_system_prompt_section(self) -> str:
        """Format core memory as a system prompt section.
        
        Returns:
            Formatted string for injection into system prompt
        """
        if not self.facts:
            return ""
        
        # Group by category
        by_category: dict[str, list[Fact]] = {}
        for fact in self.facts:
            by_category.setdefault(fact.category, []).append(fact)
        
        lines = ["<core_memory>"]
        
        # Output in consistent order
        for category in self.CATEGORIES:
            if category in by_category:
                lines.append(f"## {category.title()}")
                for fact in sorted(by_category[category], key=lambda f: -f.importance):
                    lines.append(f"- {fact.content}")
        
        lines.append("</core_memory>")
        
        return "\n".join(lines)
    
    def get_stats(self) -> dict[str, Any]:
        """Get statistics about core memory.
        
        Returns:
            Dictionary with stats
        """
        by_category = {}
        for fact in self.facts:
            by_category[fact.category] = by_category.get(fact.category, 0) + 1
        
        return {
            "total_facts": len(self.facts),
            "current_tokens": self._current_tokens,
            "max_tokens": self.max_tokens,
            "usage_percentage": self._current_tokens / self.max_tokens if self.max_tokens > 0 else 0,
            "by_category": by_category,
            "avg_importance": sum(f.importance for f in self.facts) / len(self.facts) if self.facts else 0
        }
    
    def get_token_usage(self) -> int:
        """Get current token usage.
        
        Returns:
            Current token count
        """
        return self._current_tokens
    
    def get_categories(self) -> list[str]:
        """Get list of categories with facts.
        
        Returns:
            List of category names that have at least one fact
        """
        return list(set(f.category for f in self.facts))
    
    def fits_alone(self, content: str, category: str = "facts") -> bool:
        """Whether this fact would fit if every other fact made room."""
        fact = Fact(content=content.strip(), importance=1.0,
                    category=category if category in self.CATEGORIES else "facts")
        return self._fact_tokens(fact) <= self.max_tokens

    def _format_fact(self, fact: Fact) -> str:
        """Format a single fact for token counting."""
        return f"- {fact.content}"

    def _fact_tokens(self, fact: Fact) -> int:
        """What one fact costs against the budget — the one measure add_fact,
        eviction, fits_alone and the running total all use."""
        return estimate_content_tokens(self._format_fact(fact))

    def _recalculate_tokens(self) -> None:
        """Recalculate total token count.

        The sum of the facts, not an estimate of the rendered section: add_fact
        added line estimates, this counted the section, and the two scales
        (prose vs structured data) let the total drift up to 38 % apart.
        """
        self._current_tokens = sum(self._fact_tokens(fact) for fact in self.facts)
    
    def _save_sync(self) -> None:
        """Save to storage path - sync version for thread pool."""
        if not self.storage_path:
            return
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "session_id": self.session_id,
            "max_tokens": self.max_tokens,
            "facts": [f.to_dict() for f in self.facts]
        }
        
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    
    async def _save(self) -> None:
        """Save to storage path - async wrapper."""
        if not self.storage_path:
            return
        await asyncio.to_thread(self._save_sync)
    
    def _trim_to_budget(self) -> None:
        """Drop what no longer fits, in eviction order: least important, then oldest.

        A file saved under a larger max_tokens, or counted by an older estimator,
        loads over budget; add_fact would then evict a whole batch at once for
        its first new fact.
        """
        if self._current_tokens <= self.max_tokens:
            return
        before = len(self.facts)
        # sorted() is stable and self.facts is in insertion order: oldest first.
        for victim in sorted(self.facts, key=lambda f: f.importance):
            if self._current_tokens <= self.max_tokens:
                break
            self.facts.remove(victim)
            self._current_tokens -= self._fact_tokens(victim)
        logger.warning(
            f"Core memory {self.storage_path} was over its budget of {self.max_tokens} tokens: "
            f"dropped {before - len(self.facts)} of {before} facts")
        # Its own failure: in _load's handler it would discard the facts that
        # did load, and the next add_fact would overwrite the file with one.
        try:
            self._save_sync()
        except OSError as e:
            logger.warning(f"Could not save the trimmed core memory {self.storage_path}: {e}")

    def _load(self) -> None:
        """Load from storage path."""
        if not self.storage_path or not self.storage_path.exists():
            return
        
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            self.facts = [Fact.from_dict(fd) for fd in data.get("facts", [])]
            self._recalculate_tokens()
            self._trim_to_budget()

            logger.debug(
                f"Loaded core memory: {len(self.facts)} facts, "
                f"{self._current_tokens} tokens"
            )
        except Exception as e:
            logger.error(f"Failed to load core memory from {self.storage_path}: {e}")
            self.facts = []
            self._current_tokens = 0
