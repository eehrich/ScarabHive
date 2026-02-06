"""Tests for automatic tree metadata calculation in status events."""

import asyncio
import pytest
from agent_system.mcp.status import (
    publish_status, StatusPhase, StatusScope,
    _calculate_tree_metadata, get_status_bus
)


@pytest.fixture
def status_bus():
    """Get the global status bus for testing."""
    return get_status_bus()


class TestTreeMetadataCalculation:
    """Test automatic tree metadata calculation."""
    
    def test_calculate_tree_metadata_root(self):
        """Test tree metadata for root-level request_id."""
        parent_id, depth_level = _calculate_tree_metadata("abc123")
        
        assert parent_id is None
        assert depth_level == 0
    
    def test_calculate_tree_metadata_first_level(self):
        """Test tree metadata for first-level child."""
        parent_id, depth_level = _calculate_tree_metadata("abc123_001")
        
        assert parent_id == "abc123"
        assert depth_level == 1
    
    def test_calculate_tree_metadata_second_level(self):
        """Test tree metadata for second-level child."""
        parent_id, depth_level = _calculate_tree_metadata("abc123_001_002")
        
        assert parent_id == "abc123_001"
        assert depth_level == 2
    
    def test_calculate_tree_metadata_sub_agent(self):
        """Test tree metadata for sub-agent pattern (with 'sub' keyword)."""
        # Pattern: parent_seq_sub_subagentid_seq
        # Example: "abc123_001_sub_xyz789_005"
        # Parser only recognizes numeric suffixes (_001, _005)
        # So base_id = "abc123_sub_xyz789", parent = "abc123_sub_xyz789" (for _005)
        parent_id, depth_level = _calculate_tree_metadata("abc123_001_sub_xyz789_005")
        
        # Depth = number of numeric suffixes = 2 (001, 005)
        assert depth_level == 2
        # Parent of _005 is base_id (abc123_sub_xyz789) with first suffix (001)
        assert parent_id == "abc123_sub_xyz789_001"
    
    def test_calculate_tree_metadata_none(self):
        """Test tree metadata when request_id is None."""
        parent_id, depth_level = _calculate_tree_metadata(None)
        
        assert parent_id is None
        assert depth_level == 0


class TestPublishStatusWithTreeMetadata:
    """Test publish_status automatically adds tree metadata."""
    
    @pytest.mark.asyncio
    async def test_publish_status_root_level(self, status_bus):
        """Test that publish_status adds tree metadata for root request."""
        queue = await status_bus.subscribe()
        
        await publish_status(
            server="test_server",
            message="Test message",
            request_id="root123"
        )
        
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        
        assert event.request_id == "root123"
        assert event.parent_id is None
        assert event.depth_level == 0
    
    @pytest.mark.asyncio
    async def test_publish_status_child_level(self, status_bus):
        """Test that publish_status adds tree metadata for child request."""
        queue = await status_bus.subscribe()
        
        await publish_status(
            server="test_server",
            message="Test message",
            request_id="root123_005"
        )
        
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        
        assert event.request_id == "root123_005"
        assert event.parent_id == "root123"
        assert event.depth_level == 1
    
    @pytest.mark.asyncio
    async def test_publish_status_deep_hierarchy(self, status_bus):
        """Test tree metadata for deeply nested request."""
        queue = await status_bus.subscribe()
        
        await publish_status(
            server="test_server",
            message="Test message",
            request_id="root123_001_002_003"
        )
        
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        
        assert event.request_id == "root123_001_002_003"
        assert event.parent_id == "root123_001_002"
        assert event.depth_level == 3


class TestStatusScopeWithTreeMetadata:
    """Test StatusScope automatically adds tree metadata."""
    
    @pytest.mark.asyncio
    async def test_status_scope_start_has_tree_metadata(self, status_bus):
        """Test that StatusScope START event includes tree metadata."""
        queue = await status_bus.subscribe()
        
        async with StatusScope(
            bus=status_bus,
            server="test_server",
            request_id="root123_007",
            start_msg="Starting task"
        ):
            # Read START event
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            
            assert event.phase == StatusPhase.START
            assert event.request_id == "root123_007"
            assert event.parent_id == "root123"
            assert event.depth_level == 1
    
    @pytest.mark.asyncio
    async def test_status_scope_progress_has_tree_metadata(self, status_bus):
        """Test that StatusScope progress events include tree metadata."""
        queue = await status_bus.subscribe()
        
        async with StatusScope(
            bus=status_bus,
            server="test_server",
            request_id="root123_008"
        ) as scope:
            # Skip START event
            await queue.get()
            
            # Send progress
            await scope.progress("Progress update")
            
            # Read PROGRESS event
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            
            assert event.phase == StatusPhase.PROGRESS
            assert event.parent_id == "root123"
            assert event.depth_level == 1
    
    @pytest.mark.asyncio
    async def test_status_scope_end_has_tree_metadata(self, status_bus):
        """Test that StatusScope END event includes tree metadata."""
        queue = await status_bus.subscribe()
        
        async with StatusScope(
            bus=status_bus,
            server="test_server",
            request_id="root123_009"
        ):
            # Skip START event
            await queue.get()
        
        # Read END event (sent in __aexit__)
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        
        assert event.phase == StatusPhase.END
        assert event.parent_id == "root123"
        assert event.depth_level == 1
    
    @pytest.mark.asyncio
    async def test_status_scope_error_has_tree_metadata(self, status_bus):
        """Test that StatusScope error events include tree metadata."""
        queue = await status_bus.subscribe()
        
        async with StatusScope(
            bus=status_bus,
            server="test_server",
            request_id="root123_010"
        ) as scope:
            # Skip START event
            await queue.get()
            
            # Report error
            await scope.error("Something failed")
        
        # Read ERROR event
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        
        assert event.phase == StatusPhase.ERROR
        assert event.parent_id == "root123"
        assert event.depth_level == 1


class TestTreeMetadataInDict:
    """Test that tree metadata is included in event dict."""
    
    @pytest.mark.asyncio
    async def test_event_dict_includes_tree_metadata(self, status_bus):
        """Test that to_dict() includes tree metadata."""
        queue = await status_bus.subscribe()
        
        await publish_status(
            server="test_server",
            message="Test",
            request_id="root123_011"
        )
        
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        event_dict = event.to_dict()
        
        assert "tree" in event_dict
        assert event_dict["tree"]["parent_id"] == "root123"
        assert event_dict["tree"]["depth_level"] == 1
        assert "is_leaf" in event_dict["tree"]
        assert "child_count" in event_dict["tree"]
