"""Tests for frontend hierarchical status tree functionality."""

import asyncio
import pytest

from agent_system.tools.status import (
    StatusEvent, StatusBus, SSEStatusHandler, StatusPhase
)
from agent_system.utils.tree_hierarchy import get_tree_builder


class TestStatusHierarchyFrontend:
    """Test hierarchical status tree functionality for frontend."""
    
    @pytest.mark.asyncio
    async def test_sse_hierarchy_metadata_generation(self):
        """Test that SSE events contain proper tree metadata for frontend."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create hierarchical status events
        events = [
            ("main_agent", "main_001", "Starting main task", "start"),
            ("coordinator", "main_001_001", "Delegating subtasks", "start"), 
            ("worker_a", "main_001_001_001", "Processing data A", "start"),
            ("worker_b", "main_001_001_002", "Processing data B", "start"),
            ("worker_a", "main_001_001_001", "Data A completed", "end"),
            ("worker_b", "main_001_001_002", "Data B completed", "end"),
            ("coordinator", "main_001_001", "All subtasks complete", "end"),
            ("main_agent", "main_001", "Main task finished", "end"),
        ]
        
        # Publish events
        for server, request_id, message, phase in events:
            event = StatusEvent(
                server=server,
                request_id=request_id,
                message=message,
                phase=StatusPhase.START if phase == "start" else StatusPhase.END
            )
            await bus.publish(event)
        
        # Collect SSE events
        sse_events = []
        for _ in range(len(events)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Verify hierarchy structure in SSE events
        main_event = sse_events[0]
        assert main_event["tree"]["depth_level"] == 1
        assert main_event["tree"]["parent_id"] == "main"
        
        coordinator_event = sse_events[1]
        assert coordinator_event["tree"]["depth_level"] == 2
        assert coordinator_event["tree"]["parent_id"] == "main_001"
        
        worker_a_event = sse_events[2]
        assert worker_a_event["tree"]["depth_level"] == 3
        assert worker_a_event["tree"]["parent_id"] == "main_001_001"
        
        worker_b_event = sse_events[3]
        assert worker_b_event["tree"]["depth_level"] == 3
        assert worker_b_event["tree"]["parent_id"] == "main_001_001"
        
        # Verify all events contain tree metadata
        for event in sse_events:
            assert "tree" in event
            assert "depth_level" in event["tree"]
            assert "parent_id" in event["tree"]
            assert "child_count" in event["tree"]
            assert "is_leaf" in event["tree"]
    
    @pytest.mark.asyncio
    async def test_concurrent_hierarchical_branches(self):
        """Test hierarchy with concurrent branches."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create concurrent hierarchical branches
        events = [
            ("main_agent", "task_001", "Starting task", "start"),
            ("branch_a", "task_001_001", "Branch A processing", "start"),
            ("branch_b", "task_001_002", "Branch B processing", "start"),
            ("sub_a1", "task_001_001_001", "Sub A1 work", "start"),
            ("sub_a2", "task_001_001_002", "Sub A2 work", "start"),
            ("sub_b1", "task_001_002_001", "Sub B1 work", "start"),
        ]
        
        # Publish events
        for server, request_id, message, phase in events:
            event = StatusEvent(
                server=server,
                request_id=request_id,
                message=message,
                phase=StatusPhase.START
            )
            await bus.publish(event)
        
        # Collect SSE events
        sse_events = []
        for _ in range(len(events)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Verify concurrent branches have correct hierarchy
        main_event = sse_events[0]
        assert main_event["tree"]["depth_level"] == 1
        
        branch_a = sse_events[1]
        branch_b = sse_events[2]
        assert branch_a["tree"]["depth_level"] == 2
        assert branch_b["tree"]["depth_level"] == 2
        assert branch_a["tree"]["parent_id"] == "task_001"
        assert branch_b["tree"]["parent_id"] == "task_001"
        
        sub_a1 = sse_events[3]
        sub_a2 = sse_events[4]
        sub_b1 = sse_events[5]
        assert sub_a1["tree"]["depth_level"] == 3
        assert sub_a2["tree"]["depth_level"] == 3
        assert sub_b1["tree"]["depth_level"] == 3
        assert sub_a1["tree"]["parent_id"] == "task_001_001"
        assert sub_a2["tree"]["parent_id"] == "task_001_001"
        assert sub_b1["tree"]["parent_id"] == "task_001_002"
    
    @pytest.mark.asyncio
    async def test_deep_nesting_hierarchy(self):
        """Test deep nesting hierarchy handling."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create deep nesting (5 levels)
        deep_request_ids = [
            "deep_001",
            "deep_001_001", 
            "deep_001_001_001",
            "deep_001_001_001_001",
            "deep_001_001_001_001_001"
        ]
        
        # Publish events for each level
        for i, request_id in enumerate(deep_request_ids):
            event = StatusEvent(
                server=f"agent_level_{i+1}",
                request_id=request_id,
                message=f"Level {i+1} processing",
                phase=StatusPhase.START
            )
            await bus.publish(event)
        
        # Collect SSE events
        sse_events = []
        for _ in range(len(deep_request_ids)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Verify progressive depth increase
        for i, sse_event in enumerate(sse_events):
            expected_depth = i + 1
            assert sse_event["tree"]["depth_level"] == expected_depth, f"Event {i} should have depth {expected_depth}"
            
            if i > 0:
                expected_parent = deep_request_ids[i-1]
                assert sse_event["tree"]["parent_id"] == expected_parent, f"Event {i} should have parent {expected_parent}"
    
    @pytest.mark.asyncio 
    async def test_tree_metadata_consistency(self):
        """Test that tree metadata remains consistent across operations."""
        # Clear tree builder state
        get_tree_builder().clear()
        
        sse_queue = asyncio.Queue()
        handler = SSEStatusHandler(sse_queue)
        bus = StatusBus()
        bus.add_handler(handler)
        
        # Create and complete a full workflow
        workflow_events = [
            ("main", "workflow_001", "start", StatusPhase.START),
            ("main", "workflow_001", "in progress", StatusPhase.PROGRESS),
            ("coordinator", "workflow_001_001", "coordinating", StatusPhase.START),
            ("worker1", "workflow_001_001_001", "working", StatusPhase.START),
            ("worker1", "workflow_001_001_001", "done", StatusPhase.END),
            ("worker2", "workflow_001_001_002", "working", StatusPhase.START),
            ("worker2", "workflow_001_001_002", "done", StatusPhase.END),
            ("coordinator", "workflow_001_001", "coordination complete", StatusPhase.END),
            ("main", "workflow_001", "workflow complete", StatusPhase.END),
        ]
        
        # Publish all events
        for server, request_id, message, phase in workflow_events:
            event = StatusEvent(
                server=server,
                request_id=request_id,
                message=message,
                phase=phase
            )
            await bus.publish(event)
        
        # Collect all SSE events
        sse_events = []
        for _ in range(len(workflow_events)):
            sse_event = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
            sse_events.append(sse_event)
        
        # Group events by request_id
        events_by_id = {}
        for event in sse_events:
            rid = event["request_id"]
            if rid not in events_by_id:
                events_by_id[rid] = []
            events_by_id[rid].append(event)
        
        # Verify tree metadata consistency within each request_id group
        for request_id, events in events_by_id.items():
            # All events for the same request_id should have same tree metadata
            first_event = events[0]
            depth = first_event["tree"]["depth_level"]
            parent_id = first_event["tree"]["parent_id"]
            
            for event in events[1:]:
                assert event["tree"]["depth_level"] == depth, f"Inconsistent depth for {request_id}"
                assert event["tree"]["parent_id"] == parent_id, f"Inconsistent parent for {request_id}"