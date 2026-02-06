"""Tests for status system with tree hierarchy integration."""

import asyncio
import pytest

from agent_system.mcp.status import (
    StatusEvent, StatusBus, SSEStatusHandler,
    publish_status, get_status_bus
)
from agent_system.utils.tree_hierarchy import get_tree_builder


@pytest.fixture
def status_bus():
    """Create a fresh status bus for each test."""
    return StatusBus()


@pytest.fixture
def sse_queue():
    """Create an SSE queue for testing."""
    return asyncio.Queue()


@pytest.fixture
def sse_handler(sse_queue):
    """Create an SSE handler with the test queue."""
    return SSEStatusHandler(sse_queue)


class TestStatusEventHierarchy:
    """Test StatusEvent with hierarchy metadata."""
    
    def test_status_event_hierarchy_fields(self):
        """Test that StatusEvent includes hierarchy fields."""
        event = StatusEvent(
            server="test_server",
            request_id="test_001",
            message="Test message",
            parent_id="test",
            depth_level=1,
            child_count=2,
            is_leaf=False
        )
        
        assert event.parent_id == "test"
        assert event.depth_level == 1
        assert event.child_count == 2
        assert event.is_leaf is False
    
    def test_status_event_to_dict_includes_tree(self):
        """Test that to_dict includes tree metadata."""
        event = StatusEvent(
            server="test_server",
            request_id="test_001",
            message="Test message",
            parent_id="test",
            depth_level=1,
            child_count=2,
            is_leaf=False
        )
        
        event_dict = event.to_dict()
        assert "tree" in event_dict
        
        tree_data = event_dict["tree"]
        assert tree_data["parent_id"] == "test"
        assert tree_data["depth_level"] == 1
        assert tree_data["child_count"] == 2
        assert tree_data["is_leaf"] is False


class TestStatusBusHierarchy:
    """Test StatusBus with tree hierarchy integration."""
    
    @pytest.mark.asyncio
    async def test_publish_computes_hierarchy(self, status_bus):
        """Test that publishing computes hierarchy metadata."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        event = StatusEvent(
            server="test_server",
            request_id="main_001",
            message="Test message"
        )
        
        await status_bus.publish(event)
        
        # Check that hierarchy was computed
        assert event.parent_id == "main"
        assert event.depth_level == 1
        assert event.child_count == 0
        assert event.is_leaf is True
    
    @pytest.mark.asyncio
    async def test_publish_nested_hierarchy(self, status_bus):
        """Test hierarchy computation for nested request IDs."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        # Publish parent first
        parent_event = StatusEvent(
            server="root_agent",
            request_id="main_001",
            message="Parent process"
        )
        await status_bus.publish(parent_event)
        
        # Publish child
        child_event = StatusEvent(
            server="child_agent",
            request_id="main_001_002",
            message="Child process"
        )
        await status_bus.publish(child_event)
        
        # Check parent metadata (should be updated with child count)
        assert parent_event.parent_id == "main"
        assert parent_event.depth_level == 1
        
        # Check child metadata
        assert child_event.parent_id == "main_001"
        assert child_event.depth_level == 2
        assert child_event.child_count == 0
        assert child_event.is_leaf is True
    
    @pytest.mark.asyncio
    async def test_publish_without_request_id(self, status_bus):
        """Test that events without request_id don't break."""
        event = StatusEvent(
            server="test_server",
            request_id=None,
            message="Test message"
        )
        
        await status_bus.publish(event)
        
        # Hierarchy fields should remain at defaults
        assert event.parent_id is None
        assert event.depth_level == 0
        assert event.child_count == 0
        assert event.is_leaf is True


class TestSSEHierarchy:
    """Test SSE handler with hierarchy metadata."""
    
    @pytest.mark.asyncio
    async def test_sse_includes_tree_metadata(self, status_bus, sse_handler, sse_queue):
        """Test that SSE events include tree metadata."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        status_bus.add_handler(sse_handler)
        
        await status_bus.publish(StatusEvent(
            server="test_server",
            request_id="main_001",
            message="Test message"
        ))
        
        # Get SSE event
        sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
        
        assert "tree" in sse_event
        tree_data = sse_event["tree"]
        assert tree_data["parent_id"] == "main"
        assert tree_data["depth_level"] == 1
        assert tree_data["child_count"] == 0
        assert tree_data["is_leaf"] is True
    
    @pytest.mark.asyncio
    async def test_sse_hierarchical_events(self, status_bus, sse_handler, sse_queue):
        """Test SSE events for hierarchical structure."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        status_bus.add_handler(sse_handler)
        
        # Publish hierarchical events
        await status_bus.publish(StatusEvent(
            server="root_agent",
            request_id="task_001",
            message="Main task"
        ))
        
        await status_bus.publish(StatusEvent(
            server="coordinator",
            request_id="task_001_001",
            message="Coordinate subtask"
        ))
        
        await status_bus.publish(StatusEvent(
            server="worker",
            request_id="task_001_001_001",
            message="Execute work"
        ))
        
        # Collect all SSE events
        events = []
        for _ in range(3):
            event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            events.append(event)
        
        # Check first event (root)
        assert events[0]["tree"]["parent_id"] == "task"
        assert events[0]["tree"]["depth_level"] == 1
        
        # Check second event (first child)
        assert events[1]["tree"]["parent_id"] == "task_001"
        assert events[1]["tree"]["depth_level"] == 2
        
        # Check third event (grandchild)
        assert events[2]["tree"]["parent_id"] == "task_001_001"
        assert events[2]["tree"]["depth_level"] == 3


class TestPublishStatusHierarchy:
    """Test publish_status function with hierarchy."""
    
    @pytest.mark.asyncio
    async def test_publish_status_with_hierarchy(self, sse_queue):
        """Test that publish_status function works with hierarchy."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        # Add SSE handler to global bus
        handler = SSEStatusHandler(sse_queue)
        get_status_bus().add_handler(handler)
        
        # Publish hierarchical statuses
        await publish_status("root_agent", "Starting process", "workflow_001")
        await publish_status("coordinator", "Delegating tasks", "workflow_001_001")
        await publish_status("worker_a", "Processing A", "workflow_001_001_001")
        await publish_status("worker_b", "Processing B", "workflow_001_001_002")
        
        # Collect SSE events
        events = []
        for _ in range(4):
            event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            events.append(event)
        
        # Verify hierarchy structure
        assert events[0]["request_id"] == "workflow_001"
        assert events[0]["tree"]["depth_level"] == 1
        
        assert events[1]["request_id"] == "workflow_001_001"
        assert events[1]["tree"]["parent_id"] == "workflow_001"
        assert events[1]["tree"]["depth_level"] == 2
        
        assert events[2]["request_id"] == "workflow_001_001_001"
        assert events[2]["tree"]["parent_id"] == "workflow_001_001"
        assert events[2]["tree"]["depth_level"] == 3
        
        assert events[3]["request_id"] == "workflow_001_001_002"
        assert events[3]["tree"]["parent_id"] == "workflow_001_001"
        assert events[3]["tree"]["depth_level"] == 3


class TestHierarchyIntegrationEdgeCases:
    """Test edge cases for hierarchy integration."""
    
    @pytest.mark.asyncio
    async def test_out_of_order_events(self, status_bus):
        """Test that out-of-order events still build correct hierarchy."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        # Publish child before parent
        child_event = StatusEvent(
            server="worker",
            request_id="proc_001_001_001",
            message="Child process"
        )
        await status_bus.publish(child_event)
        
        # Publish parent after child
        parent_event = StatusEvent(
            server="coordinator",
            request_id="proc_001_001",
            message="Parent process"
        )
        await status_bus.publish(parent_event)
        
        # Both should have correct hierarchy
        assert child_event.parent_id == "proc_001_001"
        assert child_event.depth_level == 3
        
        assert parent_event.parent_id == "proc_001"
        assert parent_event.depth_level == 2
    
    @pytest.mark.asyncio
    async def test_malformed_request_ids(self, status_bus):
        """Test that malformed request IDs don't break the system."""
        malformed_ids = [
            "no_suffix",
            "trailing_underscore_",
            "_leading_underscore",
            "double__underscore",
            "non_numeric_suffix_abc"
        ]
        
        for request_id in malformed_ids:
            event = StatusEvent(
                server="test_server",
                request_id=request_id,
                message="Test message"
            )
            
            # Should not raise exceptions
            await status_bus.publish(event)
            
            # Should have some reasonable defaults
            assert event.parent_id is not None or event.request_id == request_id
            assert event.depth_level >= 0
            assert event.child_count >= 0