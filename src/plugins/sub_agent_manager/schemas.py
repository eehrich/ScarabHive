"""Pydantic schemas for sub-agent session validation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SubAgentMetadata(BaseModel):
    """Metadata stored in parent session for each sub-agent."""
    
    instance_id: str = Field(description="Unique sub-agent instance ID")
    agent_type: str = Field(description="Agent type (e.g., 'web_research_agent')")
    created_at: datetime = Field(description="Creation timestamp")
    last_used: datetime = Field(description="Last accessed timestamp")
    status: Literal["active", "archived"] = Field(description="Current status")
    task_summary: str = Field(max_length=200, description="Task summary (max 200 chars)")
    current_activity: str | None = Field(default=None, max_length=500, description="Current activity description (e.g., 'Thinking...', 'Running tool: writer_search')")
    activity_updated_at: datetime | None = Field(default=None, description="Last activity update timestamp")


class ParentSessionLink(BaseModel):
    """Link stored in sub-session pointing to parent."""
    
    session_id: str = Field(description="Parent session ID")
    created_at: datetime = Field(description="Link creation timestamp")
