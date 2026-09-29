"""Unit tests for sub-agent schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from plugins.sub_agent_manager.schemas import ParentSessionLink, SubAgentMetadata


def test_sub_agent_metadata_validates():
    """Test SubAgentMetadata validation."""
    now = datetime.now(UTC)
    
    metadata = SubAgentMetadata(
        instance_id="parent_sub_web_001",
        agent_type="web_research",
        created_at=now,
        last_used=now,
        status="active",
        task_summary="Search for AI news"
    )
    
    assert metadata.instance_id == "parent_sub_web_001"
    assert metadata.agent_type == "web_research"
    assert metadata.status == "active"
    assert metadata.task_summary == "Search for AI news"


def test_parent_session_link_validates():
    """Test ParentSessionLink validation."""
    now = datetime.now(UTC)
    
    link = ParentSessionLink(
        session_id="parent123",
        created_at=now
    )
    
    assert link.session_id == "parent123"
    assert link.created_at == now


def test_invalid_status_rejected():
    """Test invalid status values are rejected."""
    now = datetime.now(UTC)
    
    with pytest.raises(ValidationError) as exc_info:
        SubAgentMetadata(
            instance_id="test",
            agent_type="web",
            created_at=now,
            last_used=now,
            status="invalid_status",  # Invalid
            task_summary="Test"
        )
    
    assert "status" in str(exc_info.value)


def test_task_summary_truncates():
    """Test task summary max length validation."""
    now = datetime.now(UTC)
    
    # Exactly 200 chars should pass
    summary_200 = "A" * 200
    metadata = SubAgentMetadata(
        instance_id="test",
        agent_type="web",
        created_at=now,
        last_used=now,
        status="active",
        task_summary=summary_200
    )
    assert metadata.task_summary == summary_200
    
    # 201 chars should fail
    summary_201 = "A" * 201
    with pytest.raises(ValidationError) as exc_info:
        SubAgentMetadata(
            instance_id="test",
            agent_type="web",
            created_at=now,
            last_used=now,
            status="active",
            task_summary=summary_201
        )
    
    assert "task_summary" in str(exc_info.value)
