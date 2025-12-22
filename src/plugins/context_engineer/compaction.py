"""Layered Compaction Strategy - Progressive context compression.

This module implements a multi-stage compaction strategy that applies
increasingly aggressive compression techniques based on token budget.

Layers (in order of application):
1. **Reversible Compaction** - Operations that can be fully undone:
   - Store tool outputs with references
   - Create variables for large content blocks
   
2. **Semi-Reversible Compaction** - Operations partially recoverable:
   - Archive old messages with summaries
   - Truncate very old tool results
   
3. **Irreversible Compaction** - Last resort operations:
   - Drop old messages entirely
   - Compress remaining summaries

The strategy is token-budget aware and tries to reach target token count
with minimum information loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent_system.llm.token_utils import estimate_content_tokens

from .archival_memory import ArchivalMemory
from .core_memory import CoreMemory
from .tool_result_store import ToolResultStore
from .variable_manager import VariableManager

logger = logging.getLogger(__name__)


@dataclass
class CompactionConfig:
    """Configuration for layered compaction."""
    
    # Token thresholds for each layer
    layer1_threshold: int = 80000  # Start reversible compaction
    layer2_threshold: int = 100000  # Start semi-reversible compaction
    layer3_threshold: int = 120000  # Start irreversible compaction
    
    # Target tokens after compaction
    target_tokens: int = 60000
    
    # Tool result settings
    tool_result_min_size: int = 500  # Min tokens to store externally
    tool_result_keep_last: int = 3   # Keep last N tool results inline (unless too large)
    tool_result_max_inline_size: int = 5000  # Max tokens before auto-archive (even if in last N)
    
    # Variable settings
    variable_min_size: int = 200     # Min tokens to create variable
    
    # Message archival settings
    archive_after_turns: int = 10    # Archive messages older than N turns
    keep_system_messages: bool = True  # Never archive system messages
    
    # Irreversible settings
    drop_after_turns: int = 50       # Drop messages older than N turns
    max_summary_tokens: int = 100    # Max tokens for archived summaries


@dataclass
class CompactionResult:
    """Result of a compaction operation."""
    
    original_tokens: int
    final_tokens: int
    tokens_saved: int
    
    # What was done
    tool_results_stored: int = 0
    variables_created: int = 0
    messages_archived: int = 0
    messages_dropped: int = 0
    
    # Layer applied
    layers_applied: list[int] = field(default_factory=list)
    
    # Modified messages
    modified_messages: list[dict[str, Any]] = field(default_factory=list)
    
    @property
    def reduction_percent(self) -> float:
        """Calculate reduction percentage."""
        if self.original_tokens == 0:
            return 0.0
        return ((self.original_tokens - self.final_tokens) / self.original_tokens) * 100


class LayeredCompactionStrategy:
    """Implements progressive context compaction.
    
    This strategy applies compression techniques in layers, starting with
    fully reversible operations and progressively moving to more aggressive
    techniques only if needed to reach the target token budget.
    
    Usage:
        strategy = LayeredCompactionStrategy(
            tool_store, variable_manager, core_memory, archival_memory, config
        )
        
        result = strategy.compact(messages, current_tokens)
        
        # Use result.modified_messages as the new conversation history
    """
    
    def __init__(
        self,
        tool_store: ToolResultStore,
        variable_manager: VariableManager,
        core_memory: CoreMemory,
        archival_memory: ArchivalMemory,
        config: CompactionConfig | None = None
    ):
        """Initialize the compaction strategy.
        
        Args:
            tool_store: Store for tool results
            variable_manager: Manager for variable substitution
            core_memory: Core memory for important facts
            archival_memory: Archive for old messages
            config: Compaction configuration
        """
        self.tool_store = tool_store
        self.variable_manager = variable_manager
        self.core_memory = core_memory
        self.archival_memory = archival_memory
        self.config = config or CompactionConfig()
    
    def _build_tool_call_map(self, messages: list[dict[str, Any]]) -> dict[str, list[int]]:
        """Build map of tool_call_id to related message indices.
        
        Returns dict mapping tool_call_id to list of [assistant_idx, tool_result_idx]
        This ensures tool calls and results are always handled together.
        """
        tool_map: dict[str, list[int]] = {}
        
        for i, msg in enumerate(messages):
            role = msg.get("role")
            
            # Track assistant messages with tool_calls
            if role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        tc_id = tc.get("id")
                        if tc_id:
                            if tc_id not in tool_map:
                                tool_map[tc_id] = []
                            tool_map[tc_id].append(i)
            
            # Track tool results
            elif role == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    if tc_id not in tool_map:
                        tool_map[tc_id] = []
                    tool_map[tc_id].append(i)
        
        return tool_map
    
    def compact(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        force: bool = False
    ) -> CompactionResult:
        """Apply layered compaction to messages.
        
        Args:
            messages: Conversation messages
            current_tokens: Current token count (calculated if not provided)
            force: Force compaction even if below target threshold
            
        Returns:
            CompactionResult with modified messages
        """
        if current_tokens is None:
            current_tokens = self._estimate_messages_tokens(messages)
        
        result = CompactionResult(
            original_tokens=current_tokens,
            final_tokens=current_tokens,
            tokens_saved=0,
            modified_messages=messages.copy()
        )
        
        # Check if compaction needed (unless forced)
        if not force and current_tokens <= self.config.target_tokens:
            logger.debug(
                f"No compaction needed: {current_tokens} tokens "
                f"<= {self.config.target_tokens} target"
            )
            return result
        
        logger.info(
            f"Starting compaction{' (FORCED)' if force else ''}: {current_tokens} tokens, "
            f"target {self.config.target_tokens}"
        )
        
        # Apply layers progressively based on TOKEN thresholds
        # force=True only bypasses Layer 1 threshold (always run Layer 1)
        # Layer 2 and 3 still respect their token thresholds because
        # they do turn-based operations that don't make sense on small contexts
        
        # Layer 1: Always apply if forced, or if above threshold
        if force or current_tokens >= self.config.layer1_threshold:
            self._apply_layer1(result)
            result.layers_applied.append(1)
            
            if result.final_tokens <= self.config.target_tokens:
                return self._finalize(result)
        
        # Layer 2: Only apply if above threshold (turn-based archival)
        if result.final_tokens >= self.config.layer2_threshold:
            self._apply_layer2(result)
            result.layers_applied.append(2)
            
            if result.final_tokens <= self.config.target_tokens:
                return self._finalize(result)
        
        # Layer 3: Only apply if above threshold (turn-based dropping)
        if result.final_tokens >= self.config.layer3_threshold:
            self._apply_layer3(result)
            result.layers_applied.append(3)
        
        return self._finalize(result)
    
    def _apply_layer1(self, result: CompactionResult) -> None:
        """Layer 1: Reversible compaction.
        
        - Store tool outputs with references (auto-archives large results > max_size)
        - Create variables for large content blocks
        """
        logger.debug("Applying Layer 1: Reversible compaction")
        
        messages = result.modified_messages
        
        # Process messages in reverse (newer first, but skip last N tool results UNLESS too large)
        tool_results_seen = 0
        
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            
            # Process tool results
            if msg.get("role") == "tool":
                tool_results_seen += 1
                
                content = msg.get("content", "")
                token_count = estimate_content_tokens(content)
                
                # Always archive if exceeds max size (even if in last N)
                is_too_large = token_count >= self.config.tool_result_max_inline_size
                is_old_enough = tool_results_seen > self.config.tool_result_keep_last
                should_archive = token_count >= self.config.tool_result_min_size and (is_too_large or is_old_enough)
                
                if should_archive:
                    # Store and replace with reference
                    tool_name = msg.get("name", "unknown")
                    tool_call_id = msg.get("tool_call_id", "")
                    
                    reference = self.tool_store.store_and_reference(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        content=content
                    )
                    
                    messages[i] = {**msg, "content": reference}
                    result.tool_results_stored += 1
                    result.tokens_saved += token_count - estimate_content_tokens(reference)
                    
                    if is_too_large:
                        logger.debug(
                            f"Auto-archived large tool result '{tool_name}' "
                            f"({token_count} tokens, exceeds max_inline_size)"
                        )
            
            # Process assistant messages with large content
            elif msg.get("role") == "assistant":
                content = msg.get("content")
                if content and isinstance(content, str):
                    token_count = estimate_content_tokens(content)
                    
                    if token_count >= self.config.variable_min_size:
                        var_name, summary = self.variable_manager.create_variable(content)
                        if var_name:  # Non-empty var_name means variable was created
                            var_ref = f"{var_name} [{summary}]"
                            messages[i] = {**msg, "content": var_ref}
                            result.variables_created += 1
                            result.tokens_saved += (
                                token_count - estimate_content_tokens(var_ref)
                            )
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 1 complete: stored {result.tool_results_stored} tool results, "
            f"created {result.variables_created} variables, "
            f"saved {result.tokens_saved} tokens"
        )
    
    def _apply_layer2(self, result: CompactionResult) -> None:
        """Layer 2: Semi-reversible compaction.
        
        - Archive old messages with summaries
        - Ensures tool_calls and tool_results are archived together
        """
        logger.debug("Applying Layer 2: Semi-reversible compaction")
        
        messages = result.modified_messages
        
        # Build tool_call mapping to keep pairs together
        tool_map = self._build_tool_call_map(messages)
        
        # Find turn boundaries (user messages)
        user_indices = [
            i for i, msg in enumerate(messages) if msg.get("role") == "user"
        ]
        
        # Calculate turn number for each message
        current_turn = len(user_indices)
        
        # Collect indices to archive (including tool_call pairs)
        indices_to_archive = set()
        
        for i, msg in enumerate(messages):
            role = msg.get("role")
            
            # Never archive system messages if configured
            if role == "system" and self.config.keep_system_messages:
                continue
            
            # Calculate message age in turns
            message_turn = sum(1 for ui in user_indices if ui <= i)
            turns_old = current_turn - message_turn
            
            if turns_old >= self.config.archive_after_turns:
                indices_to_archive.add(i)
                
                # If this is a tool call or result, add related messages
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg.get("tool_calls", []):
                        tc_id = tc.get("id")
                        if tc_id and tc_id in tool_map:
                            indices_to_archive.update(tool_map[tc_id])
                
                elif role == "tool":
                    tc_id = msg.get("tool_call_id")
                    if tc_id and tc_id in tool_map:
                        indices_to_archive.update(tool_map[tc_id])
        
        # Archive collected messages
        for i in indices_to_archive:
            msg = messages[i]
            archive_id = self.archival_memory.store(msg)
            
            # Create compact reference
            summary = self.archival_memory._generate_summary(msg)
            if len(summary) > self.config.max_summary_tokens * 4:  # ~4 chars per token
                summary = summary[:self.config.max_summary_tokens * 4] + "..."
            
            messages[i] = {
                "role": "system",
                "content": f"[Archived: {summary}] (ref: {archive_id})"
            }
            result.messages_archived += 1
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 2 complete: archived {result.messages_archived} messages "
            f"(including tool_call pairs)"
        )
        
        # Cleanup unreferenced variables after archiving messages
        removed = self.variable_manager.cleanup_unused_variables(messages)
        if removed > 0:
            logger.debug(f"Cleaned up {removed} unreferenced variables")
    
    def _apply_layer3(self, result: CompactionResult) -> None:
        """Layer 3: Irreversible compaction.
        
        - Drop old messages entirely
        - Ensures tool_calls and tool_results are dropped together
        - Compress remaining summaries
        """
        logger.debug("Applying Layer 3: Irreversible compaction")
        
        messages = result.modified_messages
        
        # Build tool_call mapping to keep pairs together
        tool_map = self._build_tool_call_map(messages)
        
        # Find turn boundaries
        user_indices = [
            i for i, msg in enumerate(messages) if msg.get("role") == "user"
        ]
        current_turn = len(user_indices)
        
        # Collect indices to remove (including tool_call pairs)
        indices_to_remove = set()
        
        for i, msg in enumerate(messages):
            role = msg.get("role")
            
            # Always keep system messages
            if role == "system" and self.config.keep_system_messages:
                continue
            
            # Calculate message age
            message_turn = sum(1 for ui in user_indices if ui <= i)
            turns_old = current_turn - message_turn
            
            if turns_old >= self.config.drop_after_turns:
                indices_to_remove.add(i)
                
                # If this is a tool call or result, add related messages
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg.get("tool_calls", []):
                        tc_id = tc.get("id")
                        if tc_id and tc_id in tool_map:
                            indices_to_remove.update(tool_map[tc_id])
                
                elif role == "tool":
                    tc_id = msg.get("tool_call_id")
                    if tc_id and tc_id in tool_map:
                        indices_to_remove.update(tool_map[tc_id])
        
        # Remove in reverse order to preserve indices
        result.messages_dropped = len(indices_to_remove)
        for i in sorted(indices_to_remove, reverse=True):
            del messages[i]
        
        # Compress archive references
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith("[Archived:"):
                # Shorten archive reference
                if len(content) > 100:
                    # Keep just the ref ID
                    ref_start = content.find("(ref:")
                    if ref_start != -1:
                        messages[i] = {**msg, "content": content[ref_start:]}
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 3 complete: dropped {result.messages_dropped} messages "
            f"(including tool_call pairs)"
        )
        
        # Cleanup unreferenced variables after dropping messages
        removed = self.variable_manager.cleanup_unused_variables(messages)
        if removed > 0:
            logger.debug(f"Cleaned up {removed} unreferenced variables")
    
    def _estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens in messages."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += estimate_content_tokens(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        total += estimate_content_tokens(part["text"])
            
            # Add overhead for role and structure
            total += 4  # Role tokens + message structure
            
            # Tool calls
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    total += estimate_content_tokens(func.get("name", ""))
                    total += estimate_content_tokens(func.get("arguments", ""))
        
        return total
    
    def _finalize(self, result: CompactionResult) -> CompactionResult:
        """Finalize compaction result."""
        result.tokens_saved = result.original_tokens - result.final_tokens
        
        logger.info(
            f"Compaction complete: {result.original_tokens} -> {result.final_tokens} tokens "
            f"({result.reduction_percent:.1f}% reduction), "
            f"layers applied: {result.layers_applied}"
        )
        
        return result
    
    def get_restoration_context(self) -> str:
        """Generate context section explaining how to restore information.
        
        This is added to the system prompt so the LLM knows how to access
        stored/archived information.
        
        Returns:
            System prompt section
        """
        sections = []
        
        # Tool result references
        tool_stats = self.tool_store.get_stats()
        if tool_stats["total_entries"] > 0:
            sections.append(
                "## Tool Results\n"
                f"There are {tool_stats['total_entries']} stored tool results. "
                "When you see a reference like `[Tool:name ref:xxx hash:yyy]`, "
                "you can retrieve the full result using the `get_tool_result` tool "
                "with the reference ID or hash."
            )
        
        # Variables
        var_section = self.variable_manager.to_system_prompt_section()
        if var_section:
            sections.append(var_section)
        
        # Archival memory
        archive_stats = self.archival_memory.get_stats()
        if archive_stats["total_messages"] > 0:
            sections.append(
                "## Conversation Archive\n"
                f"There are {archive_stats['total_messages']} archived messages "
                f"({archive_stats['total_tokens']} tokens). "
                "When you see `[Archived: summary] (ref: xxx)`, the full message "
                "has been stored and can be retrieved using the `recall` tool."
            )
        
        # Core memory
        core_section = self.core_memory.to_system_prompt_section()
        if core_section:
            sections.append(core_section)
        
        if not sections:
            return ""
        
        return (
            "\n# Context Engineer - Stored Information\n\n"
            "Some information has been stored externally to save context space. "
            "Use the provided tools to retrieve full content when needed.\n\n"
            + "\n\n".join(sections)
        )
