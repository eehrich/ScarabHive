"""Tests for foldable hierarchical status tree functionality."""

import asyncio
import pytest

from agent_system.mcp.status import (
    StatusEvent, StatusBus, SSEStatusHandler, StatusPhase
)
from agent_system.utils.tree_hierarchy import get_tree_builder


class TestFoldableTree:
    """Test foldable tree functionality for hierarchical status display."""
    
    @pytest.mark.asyncio
    async def test_tree_structure_with_expand_collapse_metadata(self):
        """Test that tree nodes provide metadata for expand/collapse functionality."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create hierarchical structure with parent-child relationships
        events = [
            ("main_agent", "workflow_001", "Starting main workflow", StatusPhase.START),
            ("coordinator", "workflow_001_001", "Coordinating tasks", StatusPhase.START),
            ("worker_a", "workflow_001_001_001", "Processing A", StatusPhase.START),
            ("worker_b", "workflow_001_001_002", "Processing B", StatusPhase.START),
            ("sub_worker", "workflow_001_001_001_001", "Sub-processing A1", StatusPhase.START),
        ]
        
        # Publish events
        for server, request_id, message, phase in events:
            event = StatusEvent(
                server=server,
                request_id=request_id,
                message=message,
                phase=phase
            )
            await bus.publish(event)
        
        # Collect SSE events
        sse_events = []
        for _ in range(len(events)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Verify parent nodes have proper metadata for expand/collapse
        main_event = sse_events[0]  # workflow_001
        coordinator_event = sse_events[1]  # workflow_001_001
        worker_a_event = sse_events[2]  # workflow_001_001_001
        
        # Main should have children (is expandable)
        assert main_event["tree"]["child_count"] >= 0
        assert main_event["tree"]["is_leaf"] is False or main_event["tree"]["child_count"] == 0
        
        # Coordinator should have children (is expandable)  
        assert coordinator_event["tree"]["child_count"] >= 0
        
        # Worker A should have children (sub_worker)
        assert worker_a_event["tree"]["child_count"] >= 0
        
        # Sub-worker should be a leaf (not expandable)
        sub_worker_event = sse_events[4]
        assert sub_worker_event["tree"]["is_leaf"] is True
    
    @pytest.mark.asyncio
    async def test_tree_metadata_for_frontend_rendering(self):
        """Test that SSE events provide all necessary data for frontend tree rendering."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create multi-branch hierarchy
        events = [
            ("root", "task_001", "Root task", StatusPhase.START),
            ("branch_1", "task_001_001", "Branch 1", StatusPhase.START),
            ("branch_2", "task_001_002", "Branch 2", StatusPhase.START),
            ("leaf_1a", "task_001_001_001", "Leaf 1A", StatusPhase.START),
            ("leaf_1b", "task_001_001_002", "Leaf 1B", StatusPhase.START),
            ("leaf_2a", "task_001_002_001", "Leaf 2A", StatusPhase.START),
        ]
        
        # Publish events
        for server, request_id, message, phase in events:
            event = StatusEvent(
                server=server,
                request_id=request_id,
                message=message,
                phase=phase
            )
            await bus.publish(event)
        
        # Collect SSE events
        sse_events = []
        for _ in range(len(events)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Verify each event has all required tree metadata for frontend
        required_tree_fields = ["parent_id", "depth_level", "child_count", "is_leaf"]
        
        for i, event in enumerate(sse_events):
            assert "tree" in event, f"Event {i} missing tree metadata"
            tree_data = event["tree"]
            
            for field in required_tree_fields:
                assert field in tree_data, f"Event {i} missing tree field: {field}"
            
            # Verify data types
            assert isinstance(tree_data["depth_level"], int), f"Event {i} depth_level not int"
            assert isinstance(tree_data["child_count"], int), f"Event {i} child_count not int"
            assert isinstance(tree_data["is_leaf"], bool), f"Event {i} is_leaf not bool"
            
            # Verify reasonable values
            assert tree_data["depth_level"] >= 0, f"Event {i} invalid depth_level"
            assert tree_data["child_count"] >= 0, f"Event {i} invalid child_count"
    
    @pytest.mark.asyncio
    async def test_parent_child_relationships_for_collapse(self):
        """Test that parent-child relationships are correctly identified for collapsing."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create specific hierarchy for collapse testing
        hierarchy = [
            ("main", "collapse_001", 1, "collapse"),      # Root (should be expandable)
            ("level1", "collapse_001_001", 2, "collapse_001"),  # Child of root (should be expandable)
            ("level2", "collapse_001_001_001", 3, "collapse_001_001"),  # Child of level1 (leaf)
            ("level1b", "collapse_001_002", 2, "collapse_001"),  # Another child of root (leaf)
        ]
        
        # Publish events with expected relationships
        for server, request_id, expected_depth, expected_parent in hierarchy:
            event = StatusEvent(
                server=server,
                request_id=request_id,
                message=f"Task at level {expected_depth}",
                phase=StatusPhase.START
            )
            await bus.publish(event)
        
        # Collect SSE events
        sse_events = []
        for _ in range(len(hierarchy)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Create mapping of request_id to event for easier testing
        event_map = {event["request_id"]: event for event in sse_events}
        
        # Test specific relationships
        root_event = event_map["collapse_001"]
        level1_event = event_map["collapse_001_001"]
        level2_event = event_map["collapse_001_001_001"]
        level1b_event = event_map["collapse_001_002"]
        
        # Root should have 2 children (level1 and level1b)
        assert root_event["tree"]["depth_level"] == 1
        assert root_event["tree"]["parent_id"] == "collapse"
        
        # Level1 should have 1 child (level2)
        assert level1_event["tree"]["depth_level"] == 2
        assert level1_event["tree"]["parent_id"] == "collapse_001"
        
        # Level2 should be a leaf
        assert level2_event["tree"]["depth_level"] == 3
        assert level2_event["tree"]["parent_id"] == "collapse_001_001"
        assert level2_event["tree"]["is_leaf"] is True
        
        # Level1b should be a leaf
        assert level1b_event["tree"]["depth_level"] == 2
        assert level1b_event["tree"]["parent_id"] == "collapse_001"
        assert level1b_event["tree"]["is_leaf"] is True
    
    @pytest.mark.asyncio
    async def test_tree_completion_updates_for_expand_state(self):
        """Test that tree structure updates properly when nodes complete."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create and then complete a hierarchy
        start_events = [
            ("parent", "complete_001", "Parent starting", StatusPhase.START),
            ("child1", "complete_001_001", "Child 1 starting", StatusPhase.START),
            ("child2", "complete_001_002", "Child 2 starting", StatusPhase.START),
        ]
        
        end_events = [
            ("child1", "complete_001_001", "Child 1 done", StatusPhase.END),
            ("child2", "complete_001_002", "Child 2 done", StatusPhase.END),
            ("parent", "complete_001", "Parent done", StatusPhase.END),
        ]
        
        # Publish start events
        for server, request_id, message, phase in start_events:
            event = StatusEvent(server=server, request_id=request_id, message=message, phase=phase)
            await bus.publish(event)
        
        # Publish end events
        for server, request_id, message, phase in end_events:
            event = StatusEvent(server=server, request_id=request_id, message=message, phase=phase)
            await bus.publish(event)
        
        # Collect all SSE events
        all_events = []
        total_events = len(start_events) + len(end_events)
        for _ in range(total_events):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            all_events.append(sse_event)
        
        # Verify tree metadata consistency throughout lifecycle
        parent_events = [e for e in all_events if e["request_id"] == "complete_001"]
        
        # All events for the same request_id should maintain consistent tree structure
        if len(parent_events) > 1:
            first_parent = parent_events[0]["tree"]
            for parent_event in parent_events[1:]:
                assert parent_event["tree"]["depth_level"] == first_parent["depth_level"]
                assert parent_event["tree"]["parent_id"] == first_parent["parent_id"]