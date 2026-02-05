"""
Tool Call Loop Detection

Detects when an agent is stuck in a loop calling the same tool(s) repeatedly
with identical or similar arguments. This is a common issue with LLMs,
especially Gemini, which can get "stuck" repeating the same action.

Detection strategies:
1. Exact match: Same tool + identical arguments
2. Sequence detection: Same sequence of tools repeated
3. Similarity: Same tool with very similar arguments (fuzzy matching)

Intervention strategies:
1. Inject user message: Nudge the LLM with a warning/redirect
2. Temporary tool blocking: Disable the problematic tool for N steps
3. Force text response: Remove tools temporarily to force reasoning
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ToolCallRecord:
    """Record of a single tool call for loop detection."""
    tool_name: str
    arguments_hash: str
    arguments: Dict[str, Any]
    step: int
    
    @classmethod
    def from_tool_call(cls, tool_call: Dict[str, Any], step: int) -> "ToolCallRecord":
        """Create a record from a tool call dict."""
        func = tool_call.get("function", {})
        tool_name = func.get("name", "unknown")
        args_str = func.get("arguments", "{}")
        
        # Parse arguments for comparison
        try:
            if isinstance(args_str, str):
                arguments = json.loads(args_str)
            else:
                arguments = args_str or {}
        except json.JSONDecodeError:
            arguments = {"_raw": args_str}
        
        # Create stable hash of arguments
        # Sort keys for consistent hashing
        args_hash = hashlib.md5(
            json.dumps(arguments, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        
        return cls(
            tool_name=tool_name,
            arguments_hash=args_hash,
            arguments=arguments,
            step=step
        )
    
    def signature(self) -> str:
        """Return a signature for exact match detection."""
        return f"{self.tool_name}:{self.arguments_hash}"


@dataclass
class LoopDetectionResult:
    """Result of loop detection check."""
    is_loop: bool = False
    loop_type: str = ""  # "exact", "sequence", "similar"
    tool_name: str = ""
    repetition_count: int = 0
    intervention: str = ""  # Suggested intervention message
    should_block_tool: bool = False
    blocked_tools: Set[str] = field(default_factory=set)


class ToolCallLoopDetector:
    """Detects and handles tool call loops in agent execution.
    
    Tracks recent tool calls and detects patterns that indicate the agent
    is stuck in a loop. Provides intervention suggestions.
    
    Usage:
        detector = ToolCallLoopDetector()
        
        # For each step with tool calls:
        for tool_call in tool_calls:
            result = detector.record_and_check(tool_call, step)
            if result.is_loop:
                # Handle loop - inject message, block tool, etc.
                intervention_msg = result.intervention
    """
    
    def __init__(
        self,
        history_size: int = 20,
        exact_match_threshold: int = 3,
        sequence_threshold: int = 2,
        block_after_threshold: int = 5,
        auto_unblock_after_steps: int = 3
    ):
        """Initialize the loop detector.
        
        Args:
            history_size: Number of recent tool calls to track
            exact_match_threshold: Trigger after N identical calls
            sequence_threshold: Trigger after N repeated sequences
            block_after_threshold: Block tool after N repetitions
            auto_unblock_after_steps: Unblock tools after N steps without that tool
        """
        self.history_size = history_size
        self.exact_match_threshold = exact_match_threshold
        self.sequence_threshold = sequence_threshold
        self.block_after_threshold = block_after_threshold
        self.auto_unblock_after_steps = auto_unblock_after_steps
        
        # State
        self._history: deque[ToolCallRecord] = deque(maxlen=history_size)
        self._signature_counts: Dict[str, int] = {}
        self._blocked_tools: Dict[str, int] = {}  # tool_name -> step_when_blocked
        self._current_step: int = 0
        self._last_detection_step: int = -1
    
    def reset(self) -> None:
        """Reset all detection state."""
        self._history.clear()
        self._signature_counts.clear()
        self._blocked_tools.clear()
        self._current_step = 0
        self._last_detection_step = -1
    
    def get_blocked_tools(self) -> Set[str]:
        """Get currently blocked tools."""
        # Auto-unblock after N steps
        current_blocked = set()
        for tool_name, blocked_step in list(self._blocked_tools.items()):
            if self._current_step - blocked_step < self.auto_unblock_after_steps:
                current_blocked.add(tool_name)
            else:
                # Auto-unblock
                del self._blocked_tools[tool_name]
                logger.info(f"Auto-unblocked tool '{tool_name}' after {self.auto_unblock_after_steps} steps")
        return current_blocked
    
    def record_and_check(
        self,
        tool_call: Dict[str, Any],
        step: int
    ) -> LoopDetectionResult:
        """Record a tool call and check for loops.
        
        Args:
            tool_call: The tool call dict from LLM response
            step: Current execution step
            
        Returns:
            LoopDetectionResult with detection info and suggested intervention
        """
        self._current_step = step
        record = ToolCallRecord.from_tool_call(tool_call, step)
        signature = record.signature()
        
        # Add to history
        self._history.append(record)
        
        # Update signature counts
        self._signature_counts[signature] = self._signature_counts.get(signature, 0) + 1
        count = self._signature_counts[signature]
        
        result = LoopDetectionResult()
        result.tool_name = record.tool_name
        result.blocked_tools = self.get_blocked_tools()
        
        # Check for exact match loop
        if count >= self.exact_match_threshold:
            result.is_loop = True
            result.loop_type = "exact"
            result.repetition_count = count
            
            # Only log and generate intervention once per detection
            if self._last_detection_step != step:
                self._last_detection_step = step
                logger.warning(
                    f"Tool call loop detected: '{record.tool_name}' called {count} times "
                    f"with identical arguments. Step {step}."
                )
            
            # Generate intervention message
            result.intervention = self._generate_intervention_message(record, count)
            
            # Check if we should block the tool
            if count >= self.block_after_threshold:
                result.should_block_tool = True
                if record.tool_name not in self._blocked_tools:
                    self._blocked_tools[record.tool_name] = step
                    logger.warning(f"Blocking tool '{record.tool_name}' after {count} repetitions")
                result.blocked_tools = self.get_blocked_tools()
        
        # Check for sequence patterns (same N tools repeated)
        if not result.is_loop:
            sequence_result = self._check_sequence_pattern()
            if sequence_result:
                result.is_loop = True
                result.loop_type = "sequence"
                result.repetition_count = sequence_result[1]
                result.intervention = self._generate_sequence_intervention(sequence_result[0])
                logger.warning(
                    f"Tool sequence loop detected: {sequence_result[0]} repeated {sequence_result[1]} times"
                )
        
        return result
    
    def record_batch_and_check(
        self,
        tool_calls: List[Dict[str, Any]],
        step: int
    ) -> LoopDetectionResult:
        """Record multiple tool calls and check for loops.
        
        Checks for loops across the batch. Returns the most severe result.
        
        Args:
            tool_calls: List of tool call dicts
            step: Current execution step
            
        Returns:
            LoopDetectionResult representing the most concerning pattern
        """
        worst_result = LoopDetectionResult()
        worst_result.blocked_tools = self.get_blocked_tools()
        
        for tool_call in tool_calls:
            result = self.record_and_check(tool_call, step)
            if result.is_loop:
                # Keep the result with highest repetition count
                if result.repetition_count > worst_result.repetition_count:
                    worst_result = result
        
        return worst_result
    
    def _check_sequence_pattern(self) -> Optional[Tuple[List[str], int]]:
        """Check for repeated sequences of tool calls.
        
        Returns:
            Tuple of (sequence, count) if pattern found, None otherwise
        """
        if len(self._history) < 4:
            return None
        
        # Try different sequence lengths (2, 3, 4)
        signatures = [r.signature() for r in self._history]
        
        for seq_len in range(2, min(5, len(signatures) // 2 + 1)):
            # Check if last N signatures repeat
            last_seq = signatures[-seq_len:]
            prev_seq = signatures[-2*seq_len:-seq_len]
            
            if last_seq == prev_seq:
                # Count total repetitions
                count = 2
                for i in range(3, len(signatures) // seq_len + 1):
                    check_seq = signatures[-i*seq_len:-(i-1)*seq_len]
                    if check_seq == last_seq:
                        count += 1
                    else:
                        break
                
                if count >= self.sequence_threshold:
                    tool_names = [r.tool_name for r in list(self._history)[-seq_len:]]
                    return (tool_names, count)
        
        return None
    
    def _generate_intervention_message(self, record: ToolCallRecord, count: int) -> str:
        """Generate an intervention message for the LLM.
        
        The message is designed to:
        1. Make the LLM aware it's repeating itself
        2. Suggest alternative approaches
        3. Not be too aggressive (allow recovery)
        """
        args_preview = json.dumps(record.arguments, default=str)[:100]
        
        if count <= self.exact_match_threshold:
            # Gentle nudge
            return (
                f"NOTICE: You've called '{record.tool_name}' {count} times with the same arguments. "
                f"If this isn't working, try a different approach or explain what you're trying to achieve."
            )
        elif count <= self.block_after_threshold:
            # Stronger warning
            return (
                f"WARNING: Tool '{record.tool_name}' has been called {count} times with identical arguments: "
                f"{args_preview}... This appears to be a loop. Please:\n"
                f"1. Check if you're getting the expected result\n"
                f"2. Try a different tool or approach\n"
                f"3. If stuck, explain the issue to the user"
            )
        else:
            # Critical - tool may be blocked
            return (
                f"CRITICAL: Tool '{record.tool_name}' has been called {count} times in a loop and may be temporarily blocked. "
                f"You MUST try a different approach. Consider:\n"
                f"1. Using a different tool\n"
                f"2. Breaking down the task differently\n"
                f"3. Asking the user for clarification"
            )
    
    def _generate_sequence_intervention(self, sequence: List[str]) -> str:
        """Generate intervention message for sequence loops."""
        seq_str = " → ".join(sequence)
        return (
            f"NOTICE: You're repeating a sequence of tool calls: {seq_str}. "
            f"This pattern suggests you may be stuck. Please step back and reconsider your approach."
        )


def create_loop_detector_from_config(config: Optional[Dict[str, Any]] = None) -> ToolCallLoopDetector:
    """Create a loop detector with optional config overrides.
    
    Args:
        config: Optional dict with keys:
            - history_size: int
            - exact_match_threshold: int
            - sequence_threshold: int
            - block_after_threshold: int
            - auto_unblock_after_steps: int
            
    Returns:
        Configured ToolCallLoopDetector instance
    """
    if config is None:
        config = {}
    
    return ToolCallLoopDetector(
        history_size=config.get("history_size", 20),
        exact_match_threshold=config.get("exact_match_threshold", 3),
        sequence_threshold=config.get("sequence_threshold", 2),
        block_after_threshold=config.get("block_after_threshold", 5),
        auto_unblock_after_steps=config.get("auto_unblock_after_steps", 3)
    )
