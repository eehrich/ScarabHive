"""Tests for tool call loop detection.

Verifies that the ToolCallLoopDetector correctly identifies:
1. Repeated identical tool calls
2. Repeated sequences of tool calls
3. Generates appropriate intervention messages
4. Blocks tools after threshold is reached
"""

import pytest
from agent_system.servers.agent.loop_detection import (
    ToolCallLoopDetector,
    ToolCallRecord,
    LoopDetectionResult,
    create_loop_detector_from_config
)


class TestToolCallRecord:
    """Tests for ToolCallRecord creation and hashing."""
    
    def test_from_tool_call_basic(self):
        """Test creating a record from a basic tool call."""
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "search",
                "arguments": '{"query": "test"}'
            }
        }
        record = ToolCallRecord.from_tool_call(tool_call, step=1)
        
        assert record.tool_name == "search"
        assert record.arguments == {"query": "test"}
        assert record.step == 1
        assert record.arguments_hash  # Should have a hash
    
    def test_identical_args_same_hash(self):
        """Test that identical arguments produce the same hash."""
        tc1 = {
            "function": {"name": "search", "arguments": '{"query": "test", "limit": 10}'}
        }
        tc2 = {
            "function": {"name": "search", "arguments": '{"limit": 10, "query": "test"}'}
        }
        
        r1 = ToolCallRecord.from_tool_call(tc1, step=1)
        r2 = ToolCallRecord.from_tool_call(tc2, step=2)
        
        # Same args, different order -> same hash (due to sort_keys=True)
        assert r1.arguments_hash == r2.arguments_hash
    
    def test_different_args_different_hash(self):
        """Test that different arguments produce different hashes."""
        tc1 = {
            "function": {"name": "search", "arguments": '{"query": "test1"}'}
        }
        tc2 = {
            "function": {"name": "search", "arguments": '{"query": "test2"}'}
        }
        
        r1 = ToolCallRecord.from_tool_call(tc1, step=1)
        r2 = ToolCallRecord.from_tool_call(tc2, step=2)
        
        assert r1.arguments_hash != r2.arguments_hash
    
    def test_signature_format(self):
        """Test signature format for matching."""
        tc = {
            "function": {"name": "my_tool", "arguments": '{"arg": "value"}'}
        }
        record = ToolCallRecord.from_tool_call(tc, step=1)
        
        sig = record.signature()
        assert sig.startswith("my_tool:")
        assert len(sig) > len("my_tool:")


class TestToolCallLoopDetector:
    """Tests for the main loop detector."""
    
    def test_no_loop_with_different_calls(self):
        """Test that different tool calls don't trigger loop detection."""
        detector = ToolCallLoopDetector(exact_match_threshold=3)
        
        calls = [
            {"function": {"name": "search", "arguments": '{"q": "a"}'}},
            {"function": {"name": "search", "arguments": '{"q": "b"}'}},
            {"function": {"name": "read", "arguments": '{"file": "x"}'}},
        ]
        
        for i, call in enumerate(calls):
            result = detector.record_and_check(call, step=i)
            assert not result.is_loop
    
    def test_exact_match_loop_detected(self):
        """Test detection of identical repeated tool calls."""
        detector = ToolCallLoopDetector(exact_match_threshold=3)
        
        identical_call = {"function": {"name": "search", "arguments": '{"query": "test"}'}}
        
        # First two calls - no loop
        result1 = detector.record_and_check(identical_call, step=0)
        result2 = detector.record_and_check(identical_call, step=1)
        assert not result1.is_loop
        assert not result2.is_loop
        
        # Third call - loop detected!
        result3 = detector.record_and_check(identical_call, step=2)
        assert result3.is_loop
        assert result3.loop_type == "exact"
        assert result3.repetition_count == 3
        assert result3.tool_name == "search"
    
    def test_intervention_message_generated(self):
        """Test that intervention messages are generated for loops."""
        detector = ToolCallLoopDetector(exact_match_threshold=2)
        
        call = {"function": {"name": "failing_tool", "arguments": '{"x": 1}'}}
        
        detector.record_and_check(call, step=0)
        result = detector.record_and_check(call, step=1)
        
        assert result.is_loop
        assert result.intervention
        assert "failing_tool" in result.intervention
        assert "2" in result.intervention  # Count mentioned
    
    def test_tool_blocking_after_threshold(self):
        """Test that tools get blocked after exceeding block threshold."""
        detector = ToolCallLoopDetector(
            exact_match_threshold=2,
            block_after_threshold=3
        )
        
        call = {"function": {"name": "problematic_tool", "arguments": '{"a": 1}'}}
        
        # Call 3 times to reach block threshold
        detector.record_and_check(call, step=0)
        detector.record_and_check(call, step=1)
        result = detector.record_and_check(call, step=2)
        
        assert result.should_block_tool
        assert "problematic_tool" in result.blocked_tools
    
    def test_auto_unblock_after_steps(self):
        """Test that blocked tools get unblocked after N steps."""
        detector = ToolCallLoopDetector(
            exact_match_threshold=2,
            block_after_threshold=2,
            auto_unblock_after_steps=2
        )
        
        call = {"function": {"name": "tool_a", "arguments": '{"x": 1}'}}
        
        # Get tool blocked at step 1
        detector.record_and_check(call, step=0)
        result = detector.record_and_check(call, step=1)
        assert "tool_a" in result.blocked_tools
        
        # After 2 more steps, should be unblocked
        other_call = {"function": {"name": "tool_b", "arguments": '{"y": 2}'}}
        detector.record_and_check(other_call, step=2)
        detector.record_and_check(other_call, step=3)
        
        # Check blocked tools at step 4 (3 steps after block at step 1)
        result = detector.record_and_check(other_call, step=4)
        assert "tool_a" not in result.blocked_tools
    
    def test_batch_check(self):
        """Test checking multiple tool calls at once."""
        detector = ToolCallLoopDetector(exact_match_threshold=2)
        
        # First batch
        batch1 = [
            {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
            {"function": {"name": "tool_b", "arguments": '{"y": 2}'}},
        ]
        result1 = detector.record_batch_and_check(batch1, step=0)
        assert not result1.is_loop
        
        # Second batch with repeated call
        batch2 = [
            {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
            {"function": {"name": "tool_c", "arguments": '{"z": 3}'}},
        ]
        result2 = detector.record_batch_and_check(batch2, step=1)
        assert result2.is_loop
        assert result2.tool_name == "tool_a"
    
    def test_sequence_detection(self):
        """Test detection of repeated sequences of tool calls."""
        detector = ToolCallLoopDetector(
            exact_match_threshold=10,  # High to not trigger
            sequence_threshold=2
        )
        
        sequence = [
            {"function": {"name": "step1", "arguments": '{}'}},
            {"function": {"name": "step2", "arguments": '{}'}},
        ]
        
        # First sequence
        for i, call in enumerate(sequence):
            result = detector.record_and_check(call, step=i)
            assert not result.is_loop
        
        # Second sequence - should trigger
        for i, call in enumerate(sequence):
            result = detector.record_and_check(call, step=i + 2)
        
        # The last call of the repeated sequence should detect the loop
        assert result.is_loop
        assert result.loop_type == "sequence"
    
    def test_reset(self):
        """Test that reset clears all state."""
        detector = ToolCallLoopDetector(exact_match_threshold=2)
        
        call = {"function": {"name": "tool", "arguments": '{}'}}
        
        # Build up state
        detector.record_and_check(call, step=0)
        detector.record_and_check(call, step=1)
        
        # Reset
        detector.reset()
        
        # Should start fresh - no loop on first two calls again
        result1 = detector.record_and_check(call, step=2)
        assert not result1.is_loop


class TestLoopDetectorFactory:
    """Tests for factory function."""
    
    def test_default_config(self):
        """Test creating detector with default config."""
        detector = create_loop_detector_from_config()
        
        assert detector.exact_match_threshold == 3
        assert detector.sequence_threshold == 2
        assert detector.history_size == 20
    
    def test_custom_config(self):
        """Test creating detector with custom config."""
        config = {
            "history_size": 10,
            "exact_match_threshold": 5,
            "sequence_threshold": 3,
            "block_after_threshold": 8,
            "auto_unblock_after_steps": 5
        }
        
        detector = create_loop_detector_from_config(config)
        
        assert detector.exact_match_threshold == 5
        assert detector.sequence_threshold == 3
        assert detector.history_size == 10
        assert detector.block_after_threshold == 8
        assert detector.auto_unblock_after_steps == 5


class TestInterventionMessages:
    """Tests for intervention message generation."""
    
    def test_gentle_nudge_at_threshold(self):
        """Test gentle message at exactly threshold."""
        detector = ToolCallLoopDetector(exact_match_threshold=2)
        
        call = {"function": {"name": "test_tool", "arguments": '{"arg": "value"}'}}
        
        detector.record_and_check(call, step=0)
        result = detector.record_and_check(call, step=1)
        
        assert "NOTICE" in result.intervention
        assert "test_tool" in result.intervention
    
    def test_warning_above_threshold(self):
        """Test warning message above threshold but before block."""
        detector = ToolCallLoopDetector(
            exact_match_threshold=2,
            block_after_threshold=5
        )
        
        call = {"function": {"name": "test_tool", "arguments": '{"arg": "value"}'}}
        
        for i in range(4):
            result = detector.record_and_check(call, step=i)
        
        assert "WARNING" in result.intervention
    
    def test_critical_at_block_threshold(self):
        """Test critical message when tool is blocked."""
        detector = ToolCallLoopDetector(
            exact_match_threshold=2,
            block_after_threshold=3
        )
        
        call = {"function": {"name": "test_tool", "arguments": '{"arg": "value"}'}}
        
        for i in range(5):
            result = detector.record_and_check(call, step=i)
        
        assert "CRITICAL" in result.intervention or "blocked" in result.intervention.lower()
