"""Tests for tool call loop detection.

Verifies that the ToolCallLoopDetector correctly identifies:
1. Repeated identical tool calls
2. Repeated sequences of tool calls
3. Generates appropriate intervention messages
4. Blocks tools after threshold is reached
5. Per-request isolation (no cross-contamination between requests)
"""

from agent_system.servers.agent.loop_detection import (
    ToolCallLoopDetector,
    ToolCallRecord,
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
    
    def test_no_loop_when_interleaved_with_other_tools(self):
        """Test that same tool+args called multiple times with other tools
        in between does NOT trigger exact match - only consecutive calls count."""
        detector = ToolCallLoopDetector(
            exact_match_threshold=3,
            sequence_threshold=99  # Disable sequence detection for this test
        )
        
        target_call = {"function": {"name": "set_task", "arguments": '{"task": "content"}'}}
        other_call = {"function": {"name": "check_status", "arguments": '{"scope": "structure"}'}}
        
        # Pattern: set_task → check_status → set_task → check_status → set_task
        # Total set_task calls = 3, but never more than 1 consecutive
        for i in range(3):
            result = detector.record_and_check(target_call, step=i * 2)
            assert not result.is_loop, f"Should not trigger at call {i+1} (interleaved)"
            if i < 2:
                detector.record_and_check(other_call, step=i * 2 + 1)
    
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
    
    def test_batch_check_consecutive_in_batch(self):
        """Test that identical consecutive calls within batches are detected."""
        detector = ToolCallLoopDetector(exact_match_threshold=2)
        
        # Batch where same tool is called consecutively
        batch = [
            {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
            {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
        ]
        result = detector.record_batch_and_check(batch, step=0)
        assert result.is_loop
        assert result.tool_name == "tool_a"
    
    def test_batch_check_non_consecutive_across_batches(self):
        """Test that same tool in different batches with other tools in between does NOT trigger."""
        detector = ToolCallLoopDetector(exact_match_threshold=2)
        
        # First batch has tool_a and tool_b
        batch1 = [
            {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
            {"function": {"name": "tool_b", "arguments": '{"y": 2}'}},
        ]
        result1 = detector.record_batch_and_check(batch1, step=0)
        assert not result1.is_loop
        
        # Second batch has tool_a again, but tool_b was in between → NOT consecutive
        batch2 = [
            {"function": {"name": "tool_a", "arguments": '{"x": 1}'}},
            {"function": {"name": "tool_c", "arguments": '{"z": 3}'}},
        ]
        result2 = detector.record_batch_and_check(batch2, step=1)
        assert not result2.is_loop  # tool_b broke the consecutive chain
    
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


class TestLoopDetectorConstruction:
    """Konstruktion wie im Produktionspfad: Agent._create_loop_detector baut
    den Detector via ToolCallLoopDetector(**config-dict)."""

    def test_default_config(self):
        """Detector mit Default-Parametern."""
        detector = ToolCallLoopDetector()

        assert detector.exact_match_threshold == 3
        assert detector.sequence_threshold == 2
        assert detector.history_size == 20

    def test_custom_config(self):
        """Detector aus Config-Dict (Produktions-Pattern: **config)."""
        config = {
            "history_size": 10,
            "exact_match_threshold": 5,
            "sequence_threshold": 3,
            "block_after_threshold": 8,
            "auto_unblock_after_steps": 5
        }

        detector = ToolCallLoopDetector(**config)

        assert detector.exact_match_threshold == 5
        assert detector.sequence_threshold == 3
        assert detector.history_size == 10
        assert detector.block_after_threshold == 8
        assert detector.auto_unblock_after_steps == 5

    def test_disabled_detects_nothing(self):
        """enabled=False (loop_detection.enabled: false) switches every
        strategy off: no sequence, no exact match, no note, no blocking."""
        detector = ToolCallLoopDetector(enabled=False)
        a = {"function": {"name": "a", "arguments": "{}"}}
        b = {"function": {"name": "b", "arguments": "{}"}}

        results = [detector.record_and_check(c, step=i)
                   for i, c in enumerate([a, b, a, b, a, b])]
        results += [detector.record_and_check(a, step=6 + i) for i in range(8)]
        results.append(detector.record_batch_and_check([a, b, a, b], step=14))

        for result in results:
            assert not result.is_loop
            assert not result.intervention
            assert not result.should_block_tool
            assert not result.blocked_tools


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


class TestPerRequestIsolation:
    """Verify that separate ToolCallLoopDetector instances don't share state.

    The Agent creates a fresh detector per request via _create_loop_detector()
    so that concurrent requests and sequential requests don't contaminate each
    other's loop history.
    """

    def test_separate_detectors_dont_share_history(self):
        """Two detectors built from the same config must be independent."""
        config = {"exact_match_threshold": 3, "block_after_threshold": 5}

        det_a = ToolCallLoopDetector(**config)
        det_b = ToolCallLoopDetector(**config)

        call = {"function": {"name": "search", "arguments": '{"q": "x"}'}}

        # Feed 2 identical calls into detector A (just below threshold)
        det_a.record_and_check(call, step=0)
        det_a.record_and_check(call, step=1)

        # Detector B should still be empty — one more call should NOT trigger
        result_b = det_b.record_and_check(call, step=0)
        assert not result_b.intervention, (
            "Fresh detector must not inherit history from another instance"
        )

    def test_sequential_request_isolation(self):
        """Simulates two sequential requests each creating their own detector.

        Even though both requests make the same calls, the second request's
        detector starts fresh and should not trigger based on the first
        request's history.
        """
        config = {"exact_match_threshold": 3, "block_after_threshold": 5}
        call = {"function": {"name": "search", "arguments": '{"q": "x"}'}}

        # --- First "request" ---
        det_1 = ToolCallLoopDetector(**config)
        for step in range(2):
            det_1.record_and_check(call, step=step)
        # det_1 has 2 consecutive identical calls (1 below threshold=3)

        # --- Second "request" (fresh detector) ---
        det_2 = ToolCallLoopDetector(**config)
        result = det_2.record_and_check(call, step=0)
        assert not result.intervention, (
            "Second request's detector must not carry over from first request"
        )

    def test_concurrent_detectors_independent_blocking(self):
        """Simulates two concurrent requests where only one exceeds threshold.

        Detector A receives many repeated calls and triggers blocking.
        Detector B receives the same call once — it must NOT block.
        """
        config = {"exact_match_threshold": 2, "block_after_threshold": 3}
        call = {"function": {"name": "write_file", "arguments": '{"path": "/tmp/x"}'}}

        det_a = ToolCallLoopDetector(**config)
        det_b = ToolCallLoopDetector(**config)

        # Drive detector A past the block threshold
        for step in range(5):
            det_a.record_and_check(call, step=step)
        assert "write_file" in det_a.get_blocked_tools(), (
            "Detector A should have blocked write_file"
        )

        # Detector B has only one call — no blocking
        result_b = det_b.record_and_check(call, step=0)
        assert not result_b.intervention
        assert det_b.get_blocked_tools() == set(), (
            "Independent detector B must have no blocked tools"
        )

    def test_construction_produces_independent_instances(self):
        """Zwei Detector-Konstruktionen aus demselben Config-Dict teilen keinen
        Zustand (Produktions-Pattern: per-Request-Detector via **config)."""
        config = {
            "exact_match_threshold": 2,
            "block_after_threshold": 4,
        }
        det_x = ToolCallLoopDetector(**config)
        det_y = ToolCallLoopDetector(**config)

        assert det_x is not det_y, "Construction must not return the same object"
        assert det_x._history is not det_y._history, (
            "Instances must not share the same history deque"
        )
