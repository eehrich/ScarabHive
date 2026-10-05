"""
Pydantic models for the Lessons Learned plugin.

Data structures for lessons, categories, evidence, and deduplication results.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class LessonStatus(str, Enum):
    """Lesson lifecycle status."""
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class SourceType(str, Enum):
    """How the lesson was created."""
    AUTO = "auto"
    CROSS_AGENT = "cross_agent"
    MANUAL = "manual"
    REFLECTION = "reflection"


class EvidenceType(str, Enum):
    """Evidence supporting or contradicting a lesson."""
    CONFIRM = "confirm"
    CONTRADICT = "contradict"
    NEUTRAL = "neutral"


class Lesson(BaseModel):
    """A single lesson learned by an agent."""
    id: int = 0
    lesson_id: str = ""
    agent_name: str
    category: str = "general"
    title: str = Field(max_length=200)
    content: str = Field(max_length=2000)
    priority: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    status: LessonStatus = LessonStatus.DRAFT
    source_type: SourceType = SourceType.AUTO
    source_agent: Optional[str] = None
    source_session: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    context_filter: dict[str, Any] = Field(default_factory=dict)
    evidence_count: int = 0
    application_count: int = 0
    effectiveness: Optional[float] = None
    last_applied_at: Optional[datetime] = None
    last_confirmed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LessonEvidence(BaseModel):
    """A piece of evidence for/against a lesson."""
    id: int = 0
    lesson_id: str
    session_id: str
    agent_name: str
    evidence_type: EvidenceType = EvidenceType.CONFIRM
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LessonApplication(BaseModel):
    """Record of a lesson being injected into an agent's prompt."""
    id: int = 0
    lesson_id: str
    session_id: str
    agent_name: str
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    turn_count: Optional[int] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None


class Category(BaseModel):
    """Lesson category for organization."""
    id: int = 0
    name: str
    description: Optional[str] = None
    agent_name: Optional[str] = None  # None = global
    icon: str = "📝"
    sort_order: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DeduplicationResult(BaseModel):
    """Result of semantic deduplication check."""
    is_duplicate: bool
    existing_lesson_id: Optional[str] = None
    similarity: float = 0.0
    action: str = "create"  # create, confirm, merge
    #: Why the check could not run. Set, is_duplicate False means "not
    #: checked", not "no duplicate".
    error: Optional[str] = None


class LessonCandidate(BaseModel):
    """A lesson candidate extracted from a session, before dedup."""
    title: str
    content: str
    category: str = "general"
    priority: int = 5
    tags: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """Result of lesson extraction from a session."""
    session_id: str
    agent_name: str
    candidates: list[LessonCandidate] = Field(default_factory=list)
    created_count: int = 0
    merged_count: int = 0
    confirmed_count: int = 0
    skipped_count: int = 0
    token_usage: int = 0
    #: Names the last message read, when the reading was logged; None: the
    #: same messages are read again next time.
    anchor: Optional[str] = None


# Default categories
DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {"name": "style", "icon": "✍️", "description": "Writing style, formatting, conventions", "sort_order": 1},
    {"name": "workflow", "icon": "🔄", "description": "Process improvements, tool usage patterns", "sort_order": 2},
    {"name": "error_pattern", "icon": "⚠️", "description": "Common mistakes to avoid", "sort_order": 3},
    {"name": "domain_knowledge", "icon": "📚", "description": "Domain-specific facts and rules", "sort_order": 4},
    {"name": "tool_usage", "icon": "🔧", "description": "Effective tool call patterns", "sort_order": 5},
    {"name": "communication", "icon": "💬", "description": "How to interact with users/other agents", "sort_order": 6},
    {"name": "performance", "icon": "⚡", "description": "Efficiency tips, reducing unnecessary steps", "sort_order": 7},
    {"name": "quality", "icon": "✅", "description": "Quality criteria and standards", "sort_order": 8},
]
