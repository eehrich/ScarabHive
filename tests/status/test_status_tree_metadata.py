"""Tests for automatic tree metadata calculation in status events."""

import asyncio
import pytest
from agent_system.tools.status import (
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
        """Sub-agent pattern: suffixes are peeled off the RIGHT edge only.

        The old parser matched _nnn anywhere and mangled the id into
        "abc123_sub_xyz789" -- a parent request that never existed, under
        which the frontend then hung a phantom tree."""
        parent_id, depth_level = _calculate_tree_metadata("abc123_001_sub_xyz789_005")

        # Real chain: abc123 -> _001 (tool) -> _sub_xyz789 (sub-agent) -> _005
        assert depth_level == 3
        assert parent_id == "abc123_001_sub_xyz789"

    def test_calculate_tree_metadata_sub_agent_leaf(self):
        """A bare sub-agent id hangs under the tool request that spawned it."""
        parent_id, depth_level = _calculate_tree_metadata("abc123_001_sub_xyz789")

        assert depth_level == 2
        assert parent_id == "abc123_001"

    def test_calculate_tree_metadata_covers_all_sub_agent_id_forms(self):
        """sub_agent_manager mints three more shapes: _sub_cont_<id>
        (continue), _async_<id> and _minlen_<n> (retry). Each must hang under
        its parent, not become its own root tree."""
        cases = {
            "abc123_001_sub_cont_xy12ab": ("abc123_001", 2),
            "abc123_001_async_xy12ab": ("abc123_001", 2),
            "abc123_001_sub_xyz789_minlen_2": ("abc123_001_sub_xyz789", 3),
        }
        for request_id, (want_parent, want_depth) in cases.items():
            parent_id, depth_level = _calculate_tree_metadata(request_id)
            assert parent_id == want_parent, request_id
            assert depth_level == want_depth, request_id

    def test_bare_async_id_without_parent_is_a_root(self):
        """f"async_{short_id()}" (no parent) must not eat itself as suffix."""
        parent_id, depth_level = _calculate_tree_metadata("async_xy12ab")

        assert parent_id is None
        assert depth_level == 0
    
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
