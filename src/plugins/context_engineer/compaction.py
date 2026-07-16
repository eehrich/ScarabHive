"""Layered Compaction Strategy - Progressive context compression.

This module implements a multi-stage compaction strategy that applies
increasingly aggressive compression techniques based on token budget.

Layers (in order of application):
1. **Reversible Compaction** - Operations that can be fully undone:
   - Store tool outputs with references
   - Create variables for large content blocks
   - Replace large audio/image inline data with references
   - Deduplicate media by hash (keep newest, compact older duplicates)
   
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

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
from agent_system.llm.token_utils import (
    estimate_content_tokens,
    estimate_inline_data_tokens,
    estimate_token_count,
)

from .archival_memory import ArchivalMemory
from .core_memory import CoreMemory
from .media_store import MediaStore
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
    
    # Byte size limit (Gemini has 100MB limit, use 90MB as safe threshold)
    # If request_bytes exceeds this, force compaction regardless of token count
    max_request_bytes: int = 90 * 1024 * 1024  # 90 MB
    target_request_bytes: int = 70 * 1024 * 1024  # 70 MB target after compaction
    
    # Tool result settings
    tool_result_min_size: int = 500  # Min tokens to store externally
    tool_result_keep_last: int = 3   # Keep last N tool results inline (unless too large)
    tool_result_max_inline_size: int = 5000  # Max tokens before auto-archive (even if in last N)
    
    # Variable settings
    variable_min_size: int = 200     # Min tokens to create variable
    assistant_keep_last: int = 3    # Keep last N assistant messages inline (never create variables for recent messages)
    
    # Message archival settings
    archive_after_turns: int = 10    # Archive messages older than N turns
    keep_system_messages: bool = True  # Never archive system messages
    
    # Irreversible settings
    drop_after_turns: int = 50       # Drop messages older than N turns
    max_summary_tokens: int = 100    # Max tokens for archived summaries
    
    # Hard message limit - drops oldest messages if exceeded (runs in Layer 3)
    # Counts ALL messages including tool calls/results, not just user messages
    # Set to 0 to disable
    max_messages: int = 0            # 0 = disabled, e.g., 200 = keep max 200 messages
    
    # Media deduplication settings
    deduplicate_media: bool = True  # Auto-compact older duplicate media (by file hash)
    
    # Media removal settings (compact media after certain events)
    compact_media_after_user_message: bool = False  # Compact all media when a new user message arrives
    compact_media_after_final_response: bool = False  # Compact all media when agent sends final response
    
    # Always compact media setting - runs regardless of token count
    # Removes ALL media items except those in the last N messages that contain media
    # Set to 0 to disable, >0 to enable and keep last N media-containing messages
    always_compact_media_keep_last: int = 0  # 0 = disabled, 5 = keep last 5 messages with media
    
    # Media store settings (for storing inline base64 before compaction)
    store_media_before_compaction: bool = True  # Save inline media to disk before removing
    media_store_ttl_seconds: int = 86400 * 7  # 7 days TTL for stored media
    media_store_max_files: int = 500  # Max files per session


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
    media_deduplicated: int = 0  # Duplicate media compacted
    media_compacted_after_event: int = 0  # Media compacted due to user/final message trigger
    media_always_compacted: int = 0  # Media compacted by always_compact_media_keep_last
    media_bytes_saved: int = 0  # Bytes saved by media compaction (for byte-limit compaction)
    messages_pruned: int = 0  # Messages pruned by max_messages limit (Pre-Layer P)
    
    # Layer applied (int for L1/L2/L3, str "M" for media, "B" for byte-limit, "P" for prune)
    layers_applied: list[int | str] = field(default_factory=list)
    
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
        config: CompactionConfig | None = None,
        media_store: MediaStore | None = None
    ):
        """Initialize the compaction strategy.
        
        Args:
            tool_store: Store for tool results
            variable_manager: Manager for variable substitution
            core_memory: Core memory for important facts
            archival_memory: Archive for old messages
            config: Compaction configuration
            media_store: Optional store for inline media before compaction
        """
        self.tool_store = tool_store
        self.variable_manager = variable_manager
        self.core_memory = core_memory
        self.archival_memory = archival_memory
        self.config = config or CompactionConfig()
        self.media_store = media_store
    
    def _add_media_hint_to_content(
        self,
        msg: dict[str, Any],
        hint_text: str
    ) -> None:
        """Add a media compaction hint to a message's content.
        
        For user/assistant messages with list content, appends a text item.
        For tool messages with string content, appends hint after the JSON.
        
        Args:
            msg: Message dict to modify
            hint_text: Hint text to add (e.g., "[Audio removed. Use recall(...)]")
        """
        import json
        
        content = msg.get("content")
        role = msg.get("role", "")
        
        if isinstance(content, list):
            # List content (user/assistant) - append text item
            content.append({"type": "text", "text": hint_text})
        elif isinstance(content, str):
            # String content (tool messages) - append to JSON or plain text
            if role == "tool":
                # Try to inject into JSON, otherwise append as text
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        data["_media_compacted"] = hint_text
                        msg["content"] = json.dumps(data, ensure_ascii=False)
                    else:
                        # Non-dict JSON, append as suffix
                        msg["content"] = content + f"\n\n{hint_text}"
                except (json.JSONDecodeError, TypeError):
                    # Plain text, append
                    msg["content"] = content + f"\n\n{hint_text}"
            else:
                # Other roles - append as text
                msg["content"] = content + f"\n\n{hint_text}"
        elif content is None:
            # No content yet - create it
            msg["content"] = hint_text
    
    def _estimate_request_bytes(self, messages: list[dict[str, Any]]) -> int:
        """Estimate the total request size in bytes.
        
        This is important for providers like Gemini that have byte-size limits (100MB).
        
        Args:
            messages: Conversation messages
            
        Returns:
            Estimated size in bytes
        """
        import json
        
        total_bytes = 0
        
        for msg in messages:
            # Estimate JSON overhead for message structure
            role = msg.get("role", "")
            total_bytes += len(role) + 20  # role + JSON structure
            
            content = msg.get("content")
            if isinstance(content, str):
                total_bytes += len(content.encode('utf-8'))
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        # Text content
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            total_bytes += len(text.encode('utf-8')) if isinstance(text, str) else 0
                        
                        # Image/audio inline data - check various formats
                        elif item.get("type") in ("image", "audio", "image_url"):
                            total_bytes += self._estimate_item_bytes(item)
            
            # Tool calls
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                total_bytes += len(json.dumps(tool_calls))
            
            # Multimodal content in tool responses (may have inline data or file paths)
            multimodal_content = msg.get("multimodal_content", [])
            if multimodal_content:
                for mm_item in multimodal_content:
                    if isinstance(mm_item, dict):
                        # Use _estimate_item_bytes which handles both inline data and file paths
                        total_bytes += self._estimate_item_bytes(mm_item)
        
        return total_bytes
    
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
    
    async def compact(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        force: bool = False,
        trigger_event: str | None = None,
        session_id: str = "default"
    ) -> CompactionResult:
        """Apply layered compaction to messages.
        
        Args:
            messages: Conversation messages
            current_tokens: Current token count (calculated if not provided)
            force: Force compaction even if below target threshold
            trigger_event: Optional event that triggered compaction:
                - 'user_message': New user message arrived
                - 'final_response': Agent sent final response
                Used for config-based media compaction.
            session_id: Session ID for media storage
            
        Returns:
            CompactionResult with modified messages
        """
        # Store session_id for use in _compact_multimodal_content
        self._current_session_id = session_id
        if current_tokens is None:
            current_tokens = self._estimate_messages_tokens(messages)
        
        result = CompactionResult(
            original_tokens=current_tokens,
            final_tokens=current_tokens,
            tokens_saved=0,
            modified_messages=messages.copy()
        )
        
        # Check byte size - Gemini has 100MB limit, force compaction if exceeded
        request_bytes = self._estimate_request_bytes(messages)
        bytes_exceeded = request_bytes > self.config.max_request_bytes
        
        if bytes_exceeded:
            logger.warning(
                f"Request size {request_bytes / (1024*1024):.1f}MB exceeds "
                f"{self.config.max_request_bytes / (1024*1024):.0f}MB limit - forcing compaction"
            )
            force = True  # Force compaction to reduce byte size
        
        # Also force compaction if always_compact_media is enabled
        # (need to run compact() to apply the media compaction even if under token threshold)
        always_compact_media = self.config.always_compact_media_keep_last > 0
        
        # Also force compaction if max_messages exceeded
        # (need to run compact() to apply Pre-Layer P even if under token threshold)
        max_messages_exceeded = (
            self.config.max_messages > 0 and 
            len(messages) > self.config.max_messages
        )
        
        # Check if compaction needed (unless forced or special conditions)
        if not force and not always_compact_media and not max_messages_exceeded and current_tokens <= self.config.target_tokens:
            logger.debug(
                f"No compaction needed: {current_tokens} tokens "
                f"<= {self.config.target_tokens} target"
            )
            return result
        
        # Build trigger reason for logging
        triggers = []
        if force:
            triggers.append("FORCED")
        if max_messages_exceeded:
            triggers.append(f"MSG_LIMIT({len(messages)}>{self.config.max_messages})")
        if always_compact_media:
            triggers.append("MEDIA_ALWAYS")
        if current_tokens > self.config.target_tokens:
            triggers.append(f"TOKENS({current_tokens}>{self.config.target_tokens})")
        
        logger.info(
            f"Starting compaction [{', '.join(triggers)}]: {current_tokens} tokens, "
            f"{request_bytes / (1024*1024):.1f}MB, {len(messages)} messages, "
            f"max_messages={self.config.max_messages}"
        )
        
        # Pre-Layer P: Prune by message count - runs FIRST if message limit exceeded
        # This is independent of token thresholds - too many messages waste API overhead
        if self.config.max_messages > 0 and len(result.modified_messages) > self.config.max_messages:
            await self._prune_by_message_count(result)
            if result.messages_pruned > 0:
                result.layers_applied.append("P")
                # Recalculate tokens after pruning
                result.final_tokens = self._estimate_messages_tokens(result.modified_messages)
                current_tokens = result.final_tokens
        
        # Pre-Layer M: Always compact media (keep last N) - runs regardless of token count
        # This is the most aggressive media compaction, runs first if enabled
        if self.config.always_compact_media_keep_last > 0:
            await self._compact_media_always(result)
            # Mark that always-compact media was applied (shown as "M" in UI)
            if result.media_always_compacted > 0 and "M" not in result.layers_applied:
                result.layers_applied.append("M")
        
        # Pre-Layer: Media deduplication (always run if enabled)
        # This is independent of token thresholds - duplicates waste space regardless
        if self.config.deduplicate_media:
            await self._deduplicate_media(result)
        
        # Pre-Layer: Event-triggered media compaction
        # Compact all media if configured to do so on user message or final response
        # SKIP if always_compact_media is enabled (it handles media more precisely)
        if self.config.always_compact_media_keep_last == 0:
            if trigger_event == "user_message" and self.config.compact_media_after_user_message:
                await self._compact_media_after_event(result, trigger="user_message")
            elif trigger_event == "final_response" and self.config.compact_media_after_final_response:
                await self._compact_media_after_event(result, trigger="final_response")
        
        # If byte size is the issue, compact media aggressively
        # Keep only the last 2 messages with inline media, compact all others
        if bytes_exceeded:
            await self._compact_media_for_byte_limit(result)
            # Mark that size-limit compaction was applied (shown as "B" in UI)
            if "B" not in result.layers_applied:
                result.layers_applied.append("B")
        
        # Apply layers progressively based on TOKEN thresholds
        # Layer 1/2/3 should ONLY run based on token thresholds
        # The 'force' flag is used to enter compact() even when below threshold,
        # but it should NOT force Layer 1 to run if we're just doing media compaction
        
        # Layer 1: Apply only if above threshold (or bytes exceeded which sets force)
        # Note: always_compact_media triggers compact() but should NOT trigger Layer 1
        layer1_needed = current_tokens >= self.config.layer1_threshold or bytes_exceeded
        if layer1_needed:
            await self._apply_layer1(result)
            result.layers_applied.append(1)
            
            # Check both token AND byte targets
            final_bytes = self._estimate_request_bytes(result.modified_messages)
            if (result.final_tokens <= self.config.target_tokens and 
                final_bytes <= self.config.target_request_bytes):
                return self._finalize(result)
        
        # Layer 2: Only apply if above threshold (turn-based archival)
        if result.final_tokens >= self.config.layer2_threshold:
            await self._apply_layer2(result)
            result.layers_applied.append(2)
            
            if result.final_tokens <= self.config.target_tokens:
                return self._finalize(result)
        
        # Layer 3: Only apply if above threshold (turn-based dropping)
        if result.final_tokens >= self.config.layer3_threshold:
            await self._apply_layer3(result)
            result.layers_applied.append(3)
        
        return self._finalize(result)
    
    def _compute_media_hash(self, item: dict[str, Any]) -> str | None:
        """Compute a hash for a media item to detect duplicates.
        
        Uses file path as primary identifier. If path doesn't exist, 
        falls back to inline data hash.
        
        Args:
            item: Media item dict (from multimodal_content or content list)
            
        Returns:
            Hash string or None if not hashable
        """
        # Primary: use file path
        file_path = item.get("path", "")
        if file_path:
            # Normalize path for consistent hashing
            normalized = os.path.normpath(file_path)
            return hashlib.md5(normalized.encode()).hexdigest()[:16]
        
        # Fallback: hash inline data if present
        inline_data = item.get("data")
        if inline_data and isinstance(inline_data, str):
            # Hash first 1000 chars of base64 data (enough for uniqueness)
            return hashlib.md5(inline_data[:1000].encode()).hexdigest()[:16]
        
        # Check for image_url format
        image_url = item.get("image_url", {})
        if isinstance(image_url, dict):
            url = image_url.get("url", "")
            if url.startswith("data:"):
                # Data URL - hash the data portion
                data_start = url.find(",")
                if data_start > 0:
                    data = url[data_start + 1:data_start + 1001]  # First 1000 chars
                    return hashlib.md5(data.encode()).hexdigest()[:16]
        
        return None
    
    def _get_media_filename(self, item: dict[str, Any]) -> str:
        """Extract original filename from media item.
        
        Args:
            item: Media item dict
            
        Returns:
            Filename string or 'unknown'
        """
        file_path = item.get("path", "")
        if file_path:
            return os.path.basename(file_path)
        
        # Try name field
        name = item.get("name", "")
        if name:
            return name
        
        # Try to extract from description
        desc = item.get("description", "")
        if desc:
            return desc[:50]  # Use first 50 chars of description
        
        return "unknown"
    
    async def _deduplicate_media(self, result: CompactionResult) -> None:
        """Deduplicate media items by hash - keep newest, compact older duplicates.
        
        Scans all messages for media items, groups by hash, and compacts all
        but the newest occurrence of each duplicate.
        
        Args:
            result: CompactionResult to update
        """
        if not self.config.deduplicate_media:
            return
        
        messages = result.modified_messages
        
        # Map: hash -> list of (message_idx, item_idx, item, is_multimodal_content)
        media_by_hash: dict[str, list[tuple[int, int, dict, bool]]] = {}
        
        # Scan all messages for media items
        for msg_idx, msg in enumerate(messages):
            # Check content list (user messages with images/audio)
            content = msg.get("content")
            if isinstance(content, list):
                for item_idx, item in enumerate(content):
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type in ("image", "image_url", "audio", "video"):
                        media_hash = self._compute_media_hash(item)
                        if media_hash:
                            if media_hash not in media_by_hash:
                                media_by_hash[media_hash] = []
                            media_by_hash[media_hash].append((msg_idx, item_idx, item, False))
            
            # Check multimodal_content (tool responses with files)
            mm_content = msg.get("multimodal_content")
            if mm_content and isinstance(mm_content, list):
                for item_idx, item in enumerate(mm_content):
                    if not isinstance(item, dict):
                        continue
                    media_hash = self._compute_media_hash(item)
                    if media_hash:
                        if media_hash not in media_by_hash:
                            media_by_hash[media_hash] = []
                        media_by_hash[media_hash].append((msg_idx, item_idx, item, True))
        
        # Compact duplicates (keep newest = highest message index)
        # Track items to remove (we can't modify list while iterating)
        items_to_remove: list[tuple[int, int, str, str]] = []  # (msg_idx, item_idx, hint_text, location)
        
        for media_hash, occurrences in media_by_hash.items():
            if len(occurrences) <= 1:
                continue  # No duplicates
            
            # Sort by message index (ascending) - newest is last
            occurrences.sort(key=lambda x: x[0])
            
            # Compact all except the last (newest) one
            for msg_idx, item_idx, item, is_mm_content in occurrences[:-1]:
                msg = messages[msg_idx]
                filename = self._get_media_filename(item)
                item_type = item.get("type", "media")
                inline_tokens = estimate_inline_data_tokens(item)
                file_path = item.get("path", "N/A")
                
                hint_text = (
                    f"[{item_type.title()} '{filename}' - duplicate compacted. "
                    f"Newer version exists later in conversation. "
                    f"Use recall(query=\"{file_path}\") if needed.]"
                )
                
                # Replace in appropriate content list
                if is_mm_content:
                    mm_list = msg.get("multimodal_content", [])
                    if item_idx < len(mm_list):
                        # Mark for removal and add hint to content
                        items_to_remove.append((msg_idx, item_idx, hint_text, "multimodal_content"))
                else:
                    content_list = msg.get("content", [])
                    if isinstance(content_list, list) and item_idx < len(content_list):
                        # Replace with text placeholder in content list
                        content_list[item_idx] = {"type": "text", "text": hint_text}
                
                result.media_deduplicated += 1
                result.tokens_saved += inline_tokens
                logger.info(
                    f"Deduplicated media '{filename}' (hash={media_hash[:8]}...): "
                    f"{inline_tokens:,} tokens saved"
                )
        
        # Remove items from multimodal_content (process in reverse to maintain indices)
        # Group by message index and sort by item_idx descending
        from collections import defaultdict
        removals_by_msg: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for msg_idx, item_idx, hint_text, location in items_to_remove:
            if location == "multimodal_content":
                removals_by_msg[msg_idx].append((item_idx, hint_text))
        
        for msg_idx, removals in removals_by_msg.items():
            msg = messages[msg_idx]
            mm_list = msg.get("multimodal_content", [])
            # Sort by item_idx descending to remove from end first
            for item_idx, hint_text in sorted(removals, key=lambda x: x[0], reverse=True):
                if item_idx < len(mm_list):
                    del mm_list[item_idx]
                    self._add_media_hint_to_content(msg, hint_text)
        
        if result.media_deduplicated > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            logger.info(f"Media deduplication: {result.media_deduplicated} duplicates compacted")
    
    def _store_inline_media(
        self,
        item: dict[str, Any],
        media_type: str,
        session_id: str
    ) -> str | None:
        """Store inline base64 media to disk before compaction.
        
        Args:
            item: Media item with various formats:
                - source.data (Anthropic/Gemini format)
                - image_url.url (OpenAI format)
                - inline_data.data (Gemini native format)
                - audio_url (data URL format)
            media_type: MIME type (e.g., "audio/flac", "image/png")
            session_id: Session ID for organization
            
        Returns:
            File path if stored successfully, None if media_store not available or failed
        """
        if not self.media_store or not self.config.store_media_before_compaction:
            return None
        
        try:
            # Extract base64 data from item - check all possible formats
            base64_data = None
            source_name = item.get("name")
            
            # Format 1: source.data (Anthropic/Gemini format)
            source = item.get("source", {})
            if isinstance(source, dict):
                base64_data = source.get("data")
                if base64_data and isinstance(base64_data, bytes):
                    # Convert bytes to base64 string
                    import base64
                    base64_data = base64.b64encode(base64_data).decode("utf-8")
                # Get media_type from source if available
                if not media_type or media_type in ("audio", "image"):
                    media_type = source.get("media_type", media_type)
            
            # Format 2: image_url.url (OpenAI format with data URL)
            if not base64_data:
                image_url = item.get("image_url", {})
                if isinstance(image_url, dict):
                    url = image_url.get("url", "")
                    if isinstance(url, str) and ";base64," in url:
                        # Extract base64 part and mime type from data URL
                        parts = url.split(";base64,", 1)
                        base64_data = parts[1]
                        if parts[0].startswith("data:"):
                            media_type = parts[0][5:]  # Extract mime type
            
            # Format 3: inline_data.data (Gemini native format)
            if not base64_data:
                inline_data = item.get("inline_data", {})
                if isinstance(inline_data, dict):
                    data = inline_data.get("data")
                    if data:
                        if isinstance(data, bytes):
                            import base64
                            base64_data = base64.b64encode(data).decode("utf-8")
                        else:
                            base64_data = data
                        # Get mime_type from inline_data
                        if inline_data.get("mime_type"):
                            media_type = inline_data["mime_type"]
            
            # Format 4: audio_url (data URL for audio)
            if not base64_data:
                audio_url = item.get("audio_url", "")
                if isinstance(audio_url, str) and ";base64," in audio_url:
                    parts = audio_url.split(";base64,", 1)
                    base64_data = parts[1]
                    if parts[0].startswith("data:"):
                        media_type = parts[0][5:]  # Extract mime type
            
            if not base64_data:
                logger.debug(f"No base64 data found in item with keys: {list(item.keys())}")
                return None
            
            # Store the media
            stored_path = self.media_store.store(
                data=base64_data,
                media_type=media_type,
                session_id=session_id,
                source_name=source_name
            )
            
            return stored_path
            
        except Exception as e:
            logger.warning(f"Failed to store inline media: {e}")
            return None
    
    async def _compact_media_for_byte_limit(
        self,
        result: CompactionResult,
        keep_last_n: int = 2
    ) -> None:
        """Aggressively compact media to reduce request byte size.
        
        This is called when the request size exceeds max_request_bytes (e.g., Gemini's 100MB limit).
        Compacts ALL media except in the last N messages.
        
        Args:
            result: CompactionResult to update
            keep_last_n: Number of recent messages to keep media for
        """
        messages = result.modified_messages
        if not messages:
            return
        
        compacted_count = 0
        bytes_saved = 0
        
        # Protect last N messages
        protected_indices = set(range(max(0, len(messages) - keep_last_n), len(messages)))
        
        for msg_idx, msg in enumerate(messages):
            if msg_idx in protected_indices:
                continue  # Don't compact recent messages
            
            # Process content list
            content = msg.get("content")
            if isinstance(content, list):
                for item_idx, item in enumerate(content):
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type not in ("image", "image_url", "audio", "video"):
                        continue
                    
                    # Estimate bytes for this item
                    item_bytes = self._estimate_item_bytes(item)
                    if item_bytes < 10000:  # Skip small items (<10KB)
                        continue
                    
                    filename = self._get_media_filename(item)
                    inline_tokens = estimate_inline_data_tokens(item)
                    file_path = item.get("path", "")
                    
                    # Replace with text placeholder
                    placeholder = {
                        "type": "text",
                        "text": (
                            f"[{item_type.title()} '{filename}' removed to reduce request size. "
                            f"Saved {item_bytes / 1024:.0f}KB."
                            + (f" Use recall(query=\"{file_path}\") to reload.]" if file_path else "]")
                        )
                    }
                    content[item_idx] = placeholder
                    compacted_count += 1
                    bytes_saved += item_bytes
                    result.tokens_saved += inline_tokens
            
            # Process multimodal_content
            mm_content = msg.get("multimodal_content")
            if mm_content and isinstance(mm_content, list):
                items_to_remove: list[tuple[int, str, int]] = []  # (item_idx, hint_text, bytes_saved)
                for item_idx, item in enumerate(mm_content):
                    if not isinstance(item, dict):
                        continue
                    
                    # Estimate bytes for this item
                    item_bytes = self._estimate_item_bytes(item)
                    if item_bytes < 10000:  # Skip small items (<10KB)
                        continue
                    
                    item_type = item.get("type", "media")
                    filename = self._get_media_filename(item)
                    inline_tokens = estimate_inline_data_tokens(item)
                    file_path = item.get("path", "")
                    
                    hint_text = (
                        f"[{item_type.title()} '{filename}' removed to reduce request size. "
                        f"Saved {item_bytes / 1024:.0f}KB."
                        + (f" Use recall(query=\"{file_path}\") to reload.]" if file_path else "]")
                    )
                    
                    items_to_remove.append((item_idx, hint_text, item_bytes))
                    compacted_count += 1
                    bytes_saved += item_bytes
                    result.tokens_saved += inline_tokens
                
                # Remove items in reverse order to maintain indices
                for item_idx, hint_text, _ in sorted(items_to_remove, key=lambda x: x[0], reverse=True):
                    del mm_content[item_idx]
                    self._add_media_hint_to_content(msg, hint_text)
        
        if compacted_count > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            result.media_compacted_after_event += compacted_count
            result.media_bytes_saved += bytes_saved  # Track bytes saved for UI
            logger.warning(
                f"Byte limit compaction: {compacted_count} media items compacted, "
                f"saved {bytes_saved / (1024*1024):.1f}MB"
            )
    
    def _estimate_item_bytes(self, item: dict) -> int:
        """Estimate bytes for a single media item.
        
        Checks for inline data first, then falls back to file size if path exists.
        Returns base64-encoded size estimate (file_size * 4/3) for path-based items.
        Returns 0 for already-compacted items (they have no data).
        """
        # Skip already compacted items - they have no data
        if item.get("compacted"):
            return 0
        
        # Check various inline data formats first
        source = item.get("source", {})
        if isinstance(source, dict):
            data = source.get("data", "")
            if data:
                return len(data) if isinstance(data, str) else len(data)
        
        image_url = item.get("image_url", {})
        if isinstance(image_url, dict):
            url = image_url.get("url", "")
            if ";base64," in url:
                return len(url.split(";base64,", 1)[1])
        
        audio_url = item.get("audio_url", "")
        if isinstance(audio_url, str) and ";base64," in audio_url:
            return len(audio_url.split(";base64,", 1)[1])
        
        # Check various data field names
        for field_name in ("data", "inline_data", "content"):
            field_data = item.get(field_name, "")
            if field_data:
                return len(field_data) if isinstance(field_data, str) else len(field_data)
        
        # Fallback: check file path and get actual file size
        # This is important for multimodal_content items that reference files
        file_path = item.get("path", "")
        if file_path and os.path.isfile(file_path):
            try:
                file_size = os.path.getsize(file_path)
                # Base64 encoding increases size by ~33%
                return int(file_size * 4 / 3)
            except (OSError, IOError):
                pass
        
        return 0
    
    async def _compact_media_after_event(
        self,
        result: CompactionResult,
        trigger: str = "user_message"
    ) -> None:
        """Compact all media items except those in the most recent message.
        
        This is triggered by config options:
        - compact_media_after_user_message: When a new user message arrives
        - compact_media_after_final_response: When agent sends final response
        
        Args:
            result: CompactionResult to update
            trigger: What triggered this compaction ('user_message' or 'final_response')
        """
        messages = result.modified_messages
        if not messages:
            return
        
        # Find the last message index to protect (don't compact its media)
        last_msg_idx = len(messages) - 1
        
        # Compact all media in older messages
        for msg_idx in range(last_msg_idx):  # Exclude last message
            msg = messages[msg_idx]
            
            # Process content list
            content = msg.get("content")
            if isinstance(content, list):
                for item_idx, item in enumerate(content):
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type not in ("image", "image_url", "audio", "video"):
                        continue
                    if item.get("compacted"):
                        continue  # Already compacted
                    
                    filename = self._get_media_filename(item)
                    inline_tokens = estimate_inline_data_tokens(item)
                    item_bytes = self._estimate_item_bytes(item)
                    file_path = item.get("path", "")
                    
                    # If no file_path, try to store inline data before removing
                    if not file_path:
                        source = item.get("source", {})
                        media_type = source.get("media_type", item_type) if isinstance(source, dict) else item_type
                        stored_path = self._store_inline_media(item, str(media_type), self._current_session_id)
                        if stored_path:
                            file_path = stored_path
                    
                    # Replace with text placeholder
                    placeholder = {
                        "type": "text",
                        "text": (
                            f"[{item_type.title()} removed after {trigger}."
                            + (f" Use recall(query=\"{file_path}\") to reload.]" if file_path else "]")
                        )
                    }
                    content[item_idx] = placeholder
                    result.media_compacted_after_event += 1
                    result.tokens_saved += inline_tokens
                    result.media_bytes_saved += item_bytes
            
            # Process multimodal_content
            mm_content = msg.get("multimodal_content")
            if mm_content and isinstance(mm_content, list):
                items_to_remove: list[tuple[int, str, int, int]] = []  # (item_idx, hint_text, tokens, bytes)
                for item_idx, item in enumerate(mm_content):
                    if not isinstance(item, dict):
                        continue
                    
                    item_type = item.get("type", "media")
                    filename = self._get_media_filename(item)
                    inline_tokens = estimate_inline_data_tokens(item)
                    item_bytes = self._estimate_item_bytes(item)
                    file_path = item.get("path", "")
                    
                    hint_text = (
                        f"[{item_type.title()} '{filename}' removed after {trigger}."
                        + (f" Use recall(query=\"{file_path}\") to reload.]" if file_path else "]")
                    )
                    
                    items_to_remove.append((item_idx, hint_text, inline_tokens, item_bytes))
                
                # Remove items in reverse order to maintain indices
                for item_idx, hint_text, inline_tokens, item_bytes in sorted(items_to_remove, key=lambda x: x[0], reverse=True):
                    del mm_content[item_idx]
                    self._add_media_hint_to_content(msg, hint_text)
                    result.media_compacted_after_event += 1
                    result.tokens_saved += inline_tokens
                    result.media_bytes_saved += item_bytes
        
        if result.media_compacted_after_event > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            logger.info(
                f"Media compaction after {trigger}: "
                f"{result.media_compacted_after_event} items compacted"
            )
    
    async def _compact_media_always(
        self,
        result: CompactionResult
    ) -> None:
        """Always compact media items, keeping only the last N messages with media.
        
        This runs regardless of token count and is controlled by
        config.always_compact_media_keep_last (0 = disabled).
        
        Unlike _compact_media_after_event, this keeps the last N messages that 
        CONTAIN media, not just the last N messages overall.
        
        Args:
            result: CompactionResult to update
        """
        messages = result.modified_messages
        if not messages:
            logger.debug("_compact_media_always: no messages")
            return
        
        keep_count = self.config.always_compact_media_keep_last
        if keep_count <= 0:
            logger.debug(f"_compact_media_always: disabled (keep_count={keep_count})")
            return
        
        logger.info(f"_compact_media_always: scanning {len(messages)} messages, keep_last={keep_count}")
        
        # Find all message indices that have media content
        messages_with_media: list[int] = []
        for msg_idx, msg in enumerate(messages):
            has_media = False
            
            # Check content list for media items
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") in ("image", "image_url", "audio", "video"):
                        # For always-compact, count ALL media (even already compacted)
                        # We want to manage which messages keep their media references
                        has_media = True
                        break
            
            # Check multimodal_content
            if not has_media:
                mm_content = msg.get("multimodal_content")
                if mm_content and isinstance(mm_content, list):
                    for item in mm_content:
                        if isinstance(item, dict) and item.get("type") in ("image", "audio", "video"):
                            has_media = True
                            break
            
            if has_media:
                messages_with_media.append(msg_idx)
        
        logger.info(f"_compact_media_always: found {len(messages_with_media)} messages with media")
        
        if not messages_with_media:
            return
        
        # Determine which message indices to protect (keep last N with media)
        protected_indices = set(messages_with_media[-keep_count:])
        
        # Compact media in all non-protected messages
        compacted_count = 0
        for msg_idx in messages_with_media:
            if msg_idx in protected_indices:
                continue
            
            msg = messages[msg_idx]
            
            # Process content list
            content = msg.get("content")
            if isinstance(content, list):
                for item_idx, item in enumerate(content):
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type not in ("image", "image_url", "audio", "video"):
                        continue
                    
                    filename = self._get_media_filename(item)
                    inline_tokens = estimate_inline_data_tokens(item)
                    item_bytes = self._estimate_item_bytes(item)
                    file_path = item.get("path", "")
                    
                    # Store inline data before removing if configured
                    if not file_path and self.config.store_media_before_compaction:
                        source = item.get("source", {})
                        media_type = source.get("media_type", item_type) if isinstance(source, dict) else item_type
                        stored_path = self._store_inline_media(item, str(media_type), self._current_session_id)
                        if stored_path:
                            file_path = stored_path
                    
                    # Replace with text placeholder
                    placeholder = {
                        "type": "text",
                        "text": (
                            f"[{item_type.title()} '{filename}' compacted."
                            + (f" Use recall(query=\"{file_path}\") to reload.]" if file_path else "]")
                        )
                    }
                    content[item_idx] = placeholder
                    compacted_count += 1
                    result.tokens_saved += inline_tokens
                    result.media_bytes_saved += item_bytes
            
            # Process multimodal_content
            mm_content = msg.get("multimodal_content")
            if mm_content and isinstance(mm_content, list):
                items_to_remove: list[tuple[int, str, int, int]] = []
                for item_idx, item in enumerate(mm_content):
                    if not isinstance(item, dict):
                        continue
                    
                    item_type = item.get("type", "media")
                    if item_type not in ("image", "audio", "video"):
                        continue
                    
                    filename = self._get_media_filename(item)
                    inline_tokens = estimate_inline_data_tokens(item)
                    item_bytes = self._estimate_item_bytes(item)
                    file_path = item.get("path", "")
                    
                    hint_text = (
                        f"[{item_type.title()} '{filename}' compacted."
                        + (f" Use recall(query=\"{file_path}\") to reload.]" if file_path else "]")
                    )
                    
                    items_to_remove.append((item_idx, hint_text, inline_tokens, item_bytes))
                
                # Remove items in reverse order to maintain indices
                for item_idx, hint_text, inline_tokens, item_bytes in sorted(items_to_remove, key=lambda x: x[0], reverse=True):
                    del mm_content[item_idx]
                    self._add_media_hint_to_content(msg, hint_text)
                    compacted_count += 1
                    result.tokens_saved += inline_tokens
                    result.media_bytes_saved += item_bytes
        
        result.media_always_compacted = compacted_count
        
        if compacted_count > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            logger.info(
                f"Always-compact media: {compacted_count} items compacted, "
                f"kept last {keep_count} messages with media "
                f"({len(protected_indices)} protected)"
            )

    async def _compact_multimodal_content(
        self,
        content: list,
        result: CompactionResult,
        session_id: str = "default",
        preserve_media: bool = False
    ) -> list:
        """Compact multimodal content by replacing large items with variables/references.
        
        Handles:
        - text_file items: Replace large content with $VAR_N references
        - audio items: Replace large base64 data with [Audio removed] placeholder
        - image items: Replace large base64 data with [Image removed] placeholder
        
        Args:
            content: Multimodal content list (text, image, text_file, audio, etc.)
            result: CompactionResult to update tokens_saved/variables_created
            session_id: Session ID for media storage
            preserve_media: If True, skip audio/image/video compaction (preserve media in last user msg)
            
        Returns:
            Compacted content list with large items replaced
        """
        compacted = []
        
        for item in content:
            if not isinstance(item, dict):
                compacted.append(item)
                continue
                
            item_type = item.get("type", "")
            
            # Compact text_file items - replace with variable reference
            if item_type == "text_file":
                file_content = item.get("content", "")
                file_name = item.get("name") or "file"
                token_count = estimate_content_tokens(file_content)
                
                if token_count >= self.config.variable_min_size:
                    # Create variable for file content
                    var_name, summary = await self.variable_manager.create_variable(
                        content=file_content,
                        content_type="file",
                        source=file_name
                    )
                    
                    if var_name:  # Variable was created
                        # Replace text_file with text containing variable reference
                        compacted.append({
                            "type": "text",
                            "text": f"[File: {file_name}] → {var_name} [{summary}]"
                        })
                        result.variables_created += 1
                        result.tokens_saved += token_count - estimate_content_tokens(f"{var_name} [{summary}]")
                        logger.debug(
                            f"Compacted text_file '{file_name}' ({token_count} tokens) → {var_name}"
                        )
                        continue
            
            # Compact audio items - remove large base64 inline data
            # If media_store is available, save to disk first for potential restoration
            # Skip if preserve_media is True (last user message)
            elif item_type == "audio":
                if preserve_media:
                    compacted.append(item)
                    continue
                    
                inline_tokens = estimate_inline_data_tokens(item)
                if inline_tokens >= self.config.tool_result_min_size:
                    # Get audio metadata if available
                    source = item.get("source", {})
                    media_type = source.get("media_type", "audio") if isinstance(source, dict) else "audio"
                    
                    # Estimate bytes for tracking
                    item_bytes = self._estimate_item_bytes(item)
                    
                    # Try to store before removing
                    stored_path = self._store_inline_media(item, media_type, session_id)
                    
                    # Create placeholder
                    if stored_path:
                        placeholder_text = f"[Audio removed. Use recall(query=\"{stored_path}\") to reload.]"
                    else:
                        placeholder_text = "[Audio removed - not recoverable. Use store_fact to save key information before compaction.]"
                    
                    compacted.append({
                        "type": "text",
                        "text": placeholder_text
                    })
                    result.tokens_saved += inline_tokens
                    result.media_compacted_after_event += 1
                    result.media_bytes_saved += item_bytes
                    logger.debug(f"Removed audio inline data: {inline_tokens:,} tokens, {item_bytes / 1024:.0f}KB saved")
                    continue
            
            # Compact image items - remove large base64 inline data  
            # If media_store is available, save to disk first for potential restoration
            # Skip if preserve_media is True (last user message)
            elif item_type in ("image", "image_url"):
                if preserve_media:
                    compacted.append(item)
                    continue
                    
                inline_tokens = estimate_inline_data_tokens(item)
                if inline_tokens >= self.config.tool_result_min_size:
                    # Get image metadata if available
                    source = item.get("source", {})
                    image_url = item.get("image_url", {})
                    media_type = "image"
                    if isinstance(source, dict):
                        media_type = source.get("media_type", "image")
                    elif isinstance(image_url, dict):
                        url = image_url.get("url", "")
                        if "data:" in url and ";" in url:
                            media_type = url.split(";")[0].replace("data:", "")
                    
                    # Estimate bytes for tracking
                    item_bytes = self._estimate_item_bytes(item)
                    
                    # Try to store before removing
                    stored_path = self._store_inline_media(item, media_type, session_id)
                    
                    # Create placeholder
                    if stored_path:
                        placeholder_text = f"[Image removed. Use recall(query=\"{stored_path}\") to reload.]"
                    else:
                        placeholder_text = "[Image removed - not recoverable. Use store_fact to save key observations before compaction.]"
                    
                    compacted.append({
                        "type": "text", 
                        "text": placeholder_text
                    })
                    result.tokens_saved += inline_tokens
                    result.media_compacted_after_event += 1
                    result.media_bytes_saved += item_bytes
                    logger.debug(f"Removed image inline data: {inline_tokens:,} tokens, {item_bytes / 1024:.0f}KB saved")
                    continue
                
            # Keep item as-is (including small items and non-compactable types)
            compacted.append(item)
        
        return compacted
    
    async def _compact_multimodal_content_items(
        self,
        mm_content: list,
        result: CompactionResult,
        msg: dict[str, Any] | None = None
    ) -> tuple[list, int]:
        """Compact multimodal_content items (file paths) by removing from list and adding hint to content.
        
        This handles MultimodalToolContent objects that reference files which will be
        base64-encoded at LLM call time. We compact by:
        1. Removing the item from multimodal_content
        2. Adding a text hint to the message content explaining how to restore
        
        Args:
            mm_content: List of MultimodalToolContent dicts with path, type, mime_type
            result: CompactionResult for tracking
            msg: Message dict to add hints to (if None, hints are not added)
            
        Returns:
            Tuple of (compacted list with large items removed, tokens saved)
        """
        compacted = []
        tokens_saved = 0
        hints_to_add: list[str] = []
        
        for item in mm_content:
            if not isinstance(item, dict):
                compacted.append(item)
                continue
            
            item_type = item.get("type", "")
            file_path = item.get("path", "")
            
            if not file_path:
                compacted.append(item)
                continue
            
            # Estimate tokens from file size
            inline_tokens = estimate_inline_data_tokens(item)
            
            if inline_tokens >= self.config.tool_result_min_size:
                # Compact large audio/image/video files
                # Estimate bytes for tracking (base64 encoded size)
                item_bytes = self._estimate_item_bytes(item)
                
                hint_text = (
                    f"[{item_type.title()} compacted. "
                    f"Use recall(query=\"{file_path}\") to reload.]"
                )
                hints_to_add.append(hint_text)
                tokens_saved += inline_tokens
                
                # Track media compaction for UI
                result.media_compacted_after_event += 1
                result.media_bytes_saved += item_bytes
                
                logger.info(f"Compacted {item_type} file: {inline_tokens:,} tokens, {item_bytes / 1024:.0f}KB saved (path: {file_path})")
                # Don't append to compacted - item is removed
                    
            else:
                # Small enough to keep
                compacted.append(item)
        
        # Add hints to message content
        if msg is not None:
            for hint in hints_to_add:
                self._add_media_hint_to_content(msg, hint)
        
        return compacted, tokens_saved
    
    async def _apply_layer1(self, result: CompactionResult) -> None:
        """Layer 1: Reversible compaction.
        
        - Store tool outputs with references (auto-archives large results > max_size)
        - Create variables for large content blocks
        - Compact text_file items in multimodal content
        """
        logger.debug("Applying Layer 1: Reversible compaction")
        
        messages = result.modified_messages
        
        # Process messages in reverse (newer first, but skip last N tool results UNLESS too large)
        tool_results_seen = 0
        assistant_messages_seen = 0
        
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            
            # Process multimodal_content from ANY message (file paths that will be base64-encoded)
            # This is crucial for tool responses with audio/image files
            mm_content = msg.get("multimodal_content")
            if mm_content and isinstance(mm_content, list):
                compacted_mm, mm_tokens_saved = await self._compact_multimodal_content_items(mm_content, result, msg)
                if mm_tokens_saved > 0:
                    messages[i] = {**msg, "multimodal_content": compacted_mm}
                    result.tokens_saved += mm_tokens_saved
            
            # Process tool results
            if msg.get("role") == "tool":
                tool_results_seen += 1
                
                content = msg.get("content", "")
                # Handle multimodal content - compact text_file items
                if isinstance(content, list):
                    compacted_content = await self._compact_multimodal_content(
                        content, result, session_id=self._current_session_id
                    )
                    if compacted_content != content:
                        messages[i] = {**msg, "content": compacted_content}
                    continue
                token_count = estimate_content_tokens(content)
                
                # Always archive if exceeds max size (even if in last N)
                # CRITICAL: Archive very large results (>2x max_inline_size) even if it's the very last one
                # This prevents huge tool results from breaking LLM calls
                is_extremely_large = token_count >= (self.config.tool_result_max_inline_size * 2)
                is_too_large = token_count >= self.config.tool_result_max_inline_size
                is_old_enough = tool_results_seen > self.config.tool_result_keep_last
                should_archive = token_count >= self.config.tool_result_min_size and (
                    is_extremely_large or is_too_large or is_old_enough
                )
                
                if should_archive:
                    # Store and replace with reference
                    tool_name = msg.get("name", "unknown")
                    tool_call_id = msg.get("tool_call_id", "")
                    
                    # Wrap sync SQLite operation in thread pool
                    reference = await asyncio.to_thread(
                        self.tool_store.store_and_reference,
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
                assistant_messages_seen += 1
                
                # Skip recent assistant messages - they are still relevant to the conversation
                if assistant_messages_seen <= self.config.assistant_keep_last:
                    continue
                
                content = msg.get("content")
                if content and isinstance(content, str):
                    token_count = estimate_content_tokens(content)
                    
                    if token_count >= self.config.variable_min_size:
                        var_name, summary = await self.variable_manager.create_variable(content)
                        if var_name:  # Non-empty var_name means variable was created
                            var_ref = f"{var_name} [{summary}]"
                            messages[i] = {**msg, "content": var_ref}
                            result.variables_created += 1
                            result.tokens_saved += (
                                token_count - estimate_content_tokens(var_ref)
                            )
            
            # Process user messages with multimodal content
            # Only skip the LAST user message for audio/image/video media preservation
            # text_file items should always be processed (they are code/text)
            elif msg.get("role") == "user":
                # Find if this is the last user message
                is_last_user_msg = all(
                    messages[j].get("role") != "user" 
                    for j in range(i + 1, len(messages))
                )
                
                content = msg.get("content")
                if content and isinstance(content, list):
                    # For the last user message, only compact text_file items
                    # (preserve audio/image/video inline data)
                    compacted_content = await self._compact_multimodal_content(
                        content, result, session_id=self._current_session_id,
                        preserve_media=is_last_user_msg  # Preserve media in last user msg
                    )
                    if compacted_content != content:
                        messages[i] = {**msg, "content": compacted_content}
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 1 complete: stored {result.tool_results_stored} tool results, "
            f"created {result.variables_created} variables, "
            f"saved {result.tokens_saved} tokens"
        )
    
    async def _apply_layer2(self, result: CompactionResult) -> None:
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
            original_role = msg.get("role", "system")
            # Wrap sync SQLite operation in thread pool
            archive_id = await asyncio.to_thread(self.archival_memory.store, msg)
            
            # Create compact reference as JSON (preserves structure, valid for tool messages)
            summary = self.archival_memory._generate_summary(msg)
            if len(summary) > self.config.max_summary_tokens * 4:  # ~4 chars per token
                summary = summary[:self.config.max_summary_tokens * 4] + "..."
            
            import json
            # Preserve original role to maintain message structure (important for Gemini)
            archived_msg: dict[str, Any] = {
                "role": original_role,
                "content": json.dumps({
                    "type": "archived_ref",
                    "ref_id": archive_id,
                    "summary": summary
                })
            }
            
            # CRITICAL: Preserve tool_calls for assistant messages!
            # Gemini requires tool_calls (with thought_signatures) for proper function call handling.
            # Without tool_calls, the message structure becomes invalid for the LLM.
            if original_role == "assistant" and msg.get("tool_calls"):
                archived_msg["tool_calls"] = msg["tool_calls"]
            
            # Preserve tool_call_id and name for tool messages (required for pairing and conversion)
            if original_role == "tool":
                if msg.get("tool_call_id"):
                    archived_msg["tool_call_id"] = msg["tool_call_id"]
                if msg.get("name"):
                    archived_msg["name"] = msg["name"]
            
            messages[i] = archived_msg
            result.messages_archived += 1
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 2 complete: archived {result.messages_archived} messages "
            f"(including tool_call pairs)"
        )
        
        # Cleanup unreferenced variables after archiving messages
        removed = await self.variable_manager.cleanup_unused_variables(messages)
        if removed > 0:
            logger.debug(f"Cleaned up {removed} unreferenced variables")

    async def _apply_layer3(self, result: CompactionResult) -> None:
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
        import json
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if isinstance(content, str):
                # Check for JSON archived_ref format
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and parsed.get("type") == "archived_ref":
                        # Shorten to minimal reference
                        messages[i] = {**msg, "content": json.dumps({
                            "type": "archived_ref",
                            "ref_id": parsed.get("ref_id")
                        })}
                except (json.JSONDecodeError, TypeError):
                    pass  # Not JSON, skip
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 3 complete: dropped {result.messages_dropped} messages "
            f"(including tool_call pairs)"
        )
        
        # Ensure valid message sequence after dropping
        extra_dropped = self._ensure_valid_message_sequence(messages, "Layer 3")
        result.messages_dropped += extra_dropped
        
        # Note: max_messages limit is now handled by Pre-Layer P at the start of compact()
        # This ensures message count is limited even when token thresholds aren't reached
        
        # Cleanup unreferenced variables after dropping messages
        removed = await self.variable_manager.cleanup_unused_variables(messages)
        if removed > 0:
            logger.debug(f"Cleaned up {removed} unreferenced variables")

    async def _prune_by_message_count(self, result: CompactionResult) -> None:
        """Pre-Layer P: Prune oldest messages to enforce max_messages limit.
        
        This runs BEFORE token-based layers to prevent excessive message counts
        that waste API overhead even when token count is low.
        
        Counts ALL messages (user, assistant, tool calls, tool results).
        System messages are kept if keep_system_messages is True.
        Tool call/result pairs are kept together to maintain conversation integrity.
        """
        messages = result.modified_messages
        max_msgs = self.config.max_messages
        
        if len(messages) <= max_msgs:
            return
        
        excess = len(messages) - max_msgs
        logger.info(
            f"Pre-Layer P: {len(messages)} messages exceeds limit of {max_msgs}, "
            f"pruning ~{excess} oldest messages"
        )
        
        # Build tool_call mapping to keep pairs together
        tool_map = self._build_tool_call_map(messages)
        
        # Find system message indices to protect
        system_indices = set()
        if self.config.keep_system_messages:
            system_indices = {
                i for i, msg in enumerate(messages) if msg.get("role") == "system"
            }
        
        # Find user message indices - we must keep at least one!
        user_indices = [
            i for i, msg in enumerate(messages) if msg.get("role") == "user"
        ]
        last_user_idx = user_indices[-1] if user_indices else None
        
        # Collect indices to remove (oldest first, respecting tool call pairs)
        indices_to_remove: set[int] = set()
        
        # Start from beginning (oldest) and collect messages until we have enough
        i = 0
        while len(indices_to_remove) < excess and i < len(messages):
            # Skip system messages
            if i in system_indices:
                i += 1
                continue
            
            # Skip if already marked for removal
            if i in indices_to_remove:
                i += 1
                continue
            
            # CRITICAL: Never remove the LAST user message - this would break API calls
            if i == last_user_idx:
                logger.debug(f"Pre-Layer P: Protecting last user message at index {i}")
                i += 1
                continue
            
            msg = messages[i]
            role = msg.get("role")
            
            # Add this message
            indices_to_remove.add(i)
            
            # If tool call/result, add related messages
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id")
                    if tc_id and tc_id in tool_map:
                        indices_to_remove.update(tool_map[tc_id])
            elif role == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id and tc_id in tool_map:
                    indices_to_remove.update(tool_map[tc_id])
            
            i += 1
        
        # Remove in reverse order to preserve indices
        pruned_count = len(indices_to_remove)
        for idx in sorted(indices_to_remove, reverse=True):
            del messages[idx]
        
        # Ensure valid message sequence after pruning
        # Note: _ensure_valid_message_sequence rebuilds tool_map internally at each iteration
        extra_pruned = self._ensure_valid_message_sequence(messages, "Pre-Layer P")
        pruned_count += extra_pruned
        
        result.messages_pruned = pruned_count
        result.final_tokens = self._estimate_messages_tokens(messages)
        
        logger.info(
            f"Pre-Layer P: pruned {pruned_count} messages, "
            f"now {len(messages)} messages, {result.final_tokens:,} tokens"
        )

    def _ensure_valid_message_sequence(
        self, 
        messages: list[dict[str, Any]], 
        caller: str
    ) -> int:
        """Ensure first non-system message is 'user' to satisfy Gemini requirements.
        
        Gemini requires: user -> assistant (with tool_calls) -> tool responses
        If first non-system msg is assistant/tool, we remove until we hit a user msg.
        
        CRITICAL: This function NEVER removes all user messages. At least one user
        message must remain for a valid API request. If only one user message is
        left and it's not at the start, we add a minimal fallback user message.
        
        Note: This method rebuilds tool_map at each iteration since indices change after deletions.
        
        Args:
            messages: List of messages (modified in place)
            caller: Name of calling function for logging
            
        Returns:
            Number of additional messages removed
        """
        extra_removed = 0
        
        while messages:
            # CRITICAL: Rebuild tool_map at each iteration because indices change after deletions
            tool_map = self._build_tool_call_map(messages)
            
            first_non_system_idx = None
            for i, msg in enumerate(messages):
                if msg.get("role") != "system":
                    first_non_system_idx = i
                    break
            
            if first_non_system_idx is None:
                break  # Only system messages left
            
            first_msg = messages[first_non_system_idx]
            if first_msg.get("role") == "user":
                break  # Good - first non-system message is user
            
            # Check if we would remove ALL user messages by continuing
            # Count remaining user messages
            user_message_count = sum(1 for msg in messages if msg.get("role") == "user")
            
            if user_message_count == 0:
                # CRITICAL: No user messages left at all - add a fallback
                logger.warning(
                    f"{caller}: No user messages remaining after pruning! "
                    f"Adding fallback user message to prevent empty contents error."
                )
                # Insert a minimal user message at the appropriate position
                fallback_msg = {
                    "role": "user",
                    "content": "Continue with the task."
                }
                # Insert after system messages
                insert_idx = first_non_system_idx
                messages.insert(insert_idx, fallback_msg)
                break
            
            # First non-system message is not user - need to remove it and related tool messages
            role = first_msg.get("role")
            indices_to_remove: set[int] = {first_non_system_idx}
            
            if role == "assistant" and first_msg.get("tool_calls"):
                # Remove this assistant message AND all its tool responses
                for tc in first_msg.get("tool_calls", []):
                    tc_id = tc.get("id")
                    if tc_id and tc_id in tool_map:
                        indices_to_remove.update(tool_map[tc_id])
            elif role == "tool":
                # Remove this tool response AND its parent assistant message
                tc_id = first_msg.get("tool_call_id")
                if tc_id and tc_id in tool_map:
                    indices_to_remove.update(tool_map[tc_id])
            
            # Remove these messages
            for idx in sorted(indices_to_remove, reverse=True):
                if idx < len(messages):
                    del messages[idx]
                    extra_removed += 1
            
            logger.debug(
                f"{caller}: removed {len(indices_to_remove)} more messages "
                f"to ensure first non-system message is 'user'"
            )
        
        # Final safety check: ensure at least one user message exists
        user_message_count = sum(1 for msg in messages if msg.get("role") == "user")
        if user_message_count == 0:
            logger.warning(
                f"{caller}: Final check - no user messages! Adding fallback."
            )
            # Find position after system messages
            insert_idx = 0
            for i, msg in enumerate(messages):
                if msg.get("role") != "system":
                    insert_idx = i
                    break
            else:
                insert_idx = len(messages)
            
            fallback_msg = {
                "role": "user",
                "content": "Continue with the task."
            }
            messages.insert(insert_idx, fallback_msg)
        
        return extra_removed
        
        return extra_removed

    def _estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens in messages, aligned with LLM token estimation.

        Uses the same estimation path as context_summarizer to avoid drift,
        while skipping media items marked as compacted since they won't be
        encoded at LLM call time.
        """
        sanitized_messages: list[dict[str, Any]] = []

        for msg in messages:
            if not isinstance(msg, dict):
                sanitized_messages.append(msg)
                continue

            msg_copy = dict(msg)

            # Remove compacted multimodal_content items (they are placeholders only)
            mm_content = msg_copy.get("multimodal_content")
            if isinstance(mm_content, list):
                msg_copy["multimodal_content"] = [
                    item
                    for item in mm_content
                    if not (isinstance(item, dict) and item.get("compacted"))
                ]

            # Remove compacted content parts if any were inserted as placeholders
            content = msg_copy.get("content")
            if isinstance(content, list):
                msg_copy["content"] = [
                    part
                    for part in content
                    if not (isinstance(part, dict) and part.get("compacted"))
                ]

            sanitized_messages.append(msg_copy)

        return estimate_token_count(sanitized_messages)
    
    def _finalize(self, result: CompactionResult) -> CompactionResult:
        """Finalize compaction result."""
        result.tokens_saved = result.original_tokens - result.final_tokens

        # THE INVARIANT (utils/reasoning_artifacts.py): provider reasoning
        # artifacts (OpenAI encrypted reasoning items, Gemini thought
        # signatures) are integrity-protected over the EXACT history that
        # produced them. If this compaction pass mutated the history in ANY
        # way — tool results swapped for refs, content replaced by variables,
        # media evicted, messages archived/dropped/pruned — those artifacts
        # are stale and will fail provider verification on a later turn
        # (HTTP 400 "encrypted content … could not be verified", deep into a
        # run). Invalidate them HERE, at the mutation site, so the chain
        # resets deterministically instead of failing reactively.
        mutated = (
            result.tool_results_stored
            + result.variables_created
            + result.messages_archived
            + result.messages_dropped
            + result.media_deduplicated
            + result.media_compacted_after_event
            + result.media_always_compacted
            + result.messages_pruned
        ) > 0 or result.media_bytes_saved > 0
        if mutated:
            invalidated = invalidate_reasoning_artifacts(result.modified_messages)
            if invalidated:
                logger.info(
                    f"Compaction mutated history -> invalidated reasoning "
                    f"artifacts on {invalidated} message(s)"
                )

        logger.info(
            f"Compaction complete: {result.original_tokens} -> {result.final_tokens} tokens "
            f"({result.reduction_percent:.1f}% reduction), "
            f"layers applied: {result.layers_applied}"
        )

        return result
    
    async def get_restoration_context(self) -> str:
        """Generate context section explaining how to restore information.
        
        This is added to the system prompt so the LLM knows how to access
        stored/archived information.
        
        Returns:
            System prompt section
        """
        sections = []
        
        # Tool result references - wrap sync SQLite operation
        tool_stats = await asyncio.to_thread(self.tool_store.get_stats)
        if tool_stats["total_entries"] > 0:
            sections.append(
                "## Tool Results\n"
                f"There are {tool_stats['total_entries']} stored tool results. "
                "When you see a JSON reference with `type: tool_result_ref`, "
                "you can retrieve the full result using the `get_tool_result` tool "
                "with the ref_id."
            )
        
        # Variables
        var_section = self.variable_manager.to_system_prompt_section()
        if var_section:
            sections.append(var_section)
        
        # Archival memory - wrap sync SQLite operation
        archive_stats = await asyncio.to_thread(self.archival_memory.get_stats)
        if archive_stats["total_messages"] > 0:
            sections.append(
                "## Conversation Archive\n"
                f"There are {archive_stats['total_messages']} archived messages "
                f"({archive_stats['total_tokens']} tokens). "
                "When you see a JSON reference with `type: archived_ref`, the full message "
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
