"""Layered Compaction Strategy - Progressive context compression.

This module implements a multi-stage compaction strategy that applies
increasingly aggressive compression techniques based on token budget.

Layers (in order of application):
1. **Reversible Compaction** - Operations that can be fully undone:
   - Store tool outputs with references
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
import base64
import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field, fields
from typing import Any, Iterator, NamedTuple

from agent_system.utils.multimodal_tool_content import extract_inline_media
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
    
    # Message archival settings
    archive_after_turns: int = 10    # Archive messages older than N turns
    keep_system_messages: bool = True  # Never archive system messages
    
    # Irreversible settings
    drop_after_turns: int = 50       # Drop messages older than N turns
    max_summary_tokens: int = 100    # Max tokens for archived summaries
    
    # Hard message limit - drops oldest messages if exceeded (Pre-Layer P)
    # Counts ALL messages including tool calls/results, not just user messages
    # Set to 0 to disable
    max_messages: int = 0            # 0 = disabled, e.g., 200 = keep max 200 messages

    # How far BELOW max_messages a prune goes. Without it a prune trims to
    # exactly the limit, the next step is over it again, and the front of the
    # conversation is rewritten every single step — a prompt-cache break per
    # step for one message of savings. With headroom the same break buys
    # roughly `headroom` quiet steps.
    max_messages_headroom: int = 50
    
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


#: Config keys the plugin consumes that are NOT CompactionConfig fields.
#: Anything a config file sets must be one or the other — see
#: ``unknown_config_keys``, which is what turns a typo into a log line instead
#: of silence.
PLUGIN_LEVEL_KEYS = frozenset({
    "session_ttl_seconds",
    "max_tracked_sessions",
    "min_time_between_compactions",
    "enable_semantic_search",
    "core_memory_max_tokens",
    "storage_path",
})


def _coerce(value: Any, type_name: str, field_name: str) -> Any:
    """YAML already yields ints and bools; this only catches the odd string.

    A value that cannot be coerced is passed through UNCHANGED rather than
    dropped: a wrong type is the operator's to see, and silently substituting
    a default here would be the same disappearing act this module exists to
    stop.
    """
    try:
        if type_name == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if type_name == "int":
            return int(value)
        if type_name == "float":
            return float(value)
    except (TypeError, ValueError):
        logger.warning(
            "[ContextEngineer] config '%s' = %r is not a %s; using it as-is",
            field_name, value, type_name)
    return value


def compaction_config_from(values: dict[str, Any]) -> CompactionConfig:
    """Build the config by FIELD NAME. The dataclass IS the schema.

    Every setting used to be hand-written in three separate lists (read it in
    server.py, copy it onto the hook, name it again when constructing this
    object) and a key missing from any of them was dropped without a word.
    Measured on the shipped config: 5 of 25 settings never arrived — among
    them a request-size guard an operator had deliberately lowered to 29 MB
    to stay under a provider cap, running at 90 MB.
    """
    typed = {f.name: f.type for f in fields(CompactionConfig)}
    return CompactionConfig(**{
        name: _coerce(values[name], typed[name], name)
        for name in typed if name in values
    })


def unknown_config_keys(values: Any) -> list[str]:
    """Keys that reach neither the compaction config nor the plugin itself.

    This is the half a generic mapping cannot do on its own: mapping by field
    name makes a correctly-named key arrive, but a MISSPELLED one still lands
    nowhere. Shipped example: `semantic_search`, where the code reads
    `enable_semantic_search` — set to true for months, off the whole time.
    """
    known = {f.name for f in fields(CompactionConfig)} | PLUGIN_LEVEL_KEYS
    return sorted(set(values) - known)


@dataclass
class CompactionResult:
    """Result of a compaction operation."""
    
    original_tokens: int
    final_tokens: int
    tokens_saved: int
    
    # What was done
    tool_results_stored: int = 0
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


#: Marker of the JSON placeholder that replaces an archived message in place.
ARCHIVED_REF_TYPE = "archived_ref"

#: Key the retrieval tools put in their own answers so compaction can recognise
#: them. Deliberately NOT a tool-name match: the tool names are built from the
#: server name in plugins.yaml, so renaming the server there would silently
#: switch the exemption off and bring the retrieval loop back. The answer
#: identifying itself survives any renaming.
RETRIEVAL_MARKER = "retrieval_result"


def _is_retrieval_result(message: dict[str, Any]) -> bool:
    """Whether a TOOL message is an answer from the context-retrieval tools.

    Their output is content the agent just pulled OUT of storage; putting it
    straight back is a loop with no exit (see the call site).

    The role check is load-bearing, not decoration: this predicate now also
    decides what is exempt from archiving, and a user who pastes a retrieval
    answer back into the chat to ask about it writes a REAL message that merely
    looks like one. Without the check that message would be deleted and never
    stored.
    """
    if message.get("role") != "tool":
        return False
    content = message.get("content")
    if not isinstance(content, str) or RETRIEVAL_MARKER not in content:
        return False
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get(RETRIEVAL_MARKER) is True


#: Marker of the placeholder Layer 1 leaves where a tool result was externalised.
TOOL_RESULT_REF_TYPE = "tool_result_ref"

#: Marker of the single breadcrumb Pre-Layer P leaves after removing messages.
#: Without it a bulk removal is invisible to the agent, which is the "kein
#: stiller Drift" invariant applied to the context: a self-healing step nobody
#: can see is indistinguishable from one that never happened.
PRUNE_NOTICE_TYPE = "pruned_notice"

#: Placeholder types whose body already lives in a store. Dropping one costs
#: its address only — the content stays reachable through the retrieval tools.
_PLACEHOLDER_TYPES = (ARCHIVED_REF_TYPE, TOOL_RESULT_REF_TYPE, PRUNE_NOTICE_TYPE)

#: The role each placeholder is WRITTEN on. The agent can see these JSON blobs
#: in its own context and reproduce one in an answer; on shape alone such an
#: echo counts as a placeholder, gets ranked as free to drop AND excluded from
#: archiving — deleted with no copy anywhere. The role is what separates the
#: real thing from a quotation of it.
#:
#: ``archived_ref`` is absent on purpose: Layer 2 preserves the ORIGINAL role of
#: whatever it archived, so every role is legitimate there and nothing can tell
#: an echo apart. That residue is accepted — an echoed archive pointer is an
#: address whose target is still in the archive, so the loss is the quotation.
_PLACEHOLDER_ROLES = {
    TOOL_RESULT_REF_TYPE: ("tool",),
    PRUNE_NOTICE_TYPE: ("system",),
}

#: Media item types that can appear in a message's ``content`` list.
_MEDIA_TYPES = ("image", "image_url", "audio", "video")

#: What a hint CALLS each of them. Not ``type.title()``: that spells
#: ``image_url`` as "Image_Url", a word the model has never seen, and it raises
#: outright on an item whose ``type`` is not a string — which
#: ``multimodal_content`` does not promise.
_MEDIA_SUBJECTS = {"image": "Image", "image_url": "Image",
                   "audio": "Audio", "video": "Video"}

#: The session every caller lands in that brought no session id of its own.
#: Named rather than spelled out four times, because it is the one value whose
#: appearance means "someone forgot" and not "someone chose".
_SHARED_SESSION_ID = "default"


class _MediaPick(NamedTuple):
    """One media item chosen for eviction, plus the words for its hint.

    Everything that distinguishes the eviction reasons lives in ``subject`` and
    ``reason``; the mechanics below them are identical for all of them.
    """

    msg_idx: int
    item_idx: int
    item: dict[str, Any]
    in_mm: bool
    subject: str
    reason: str


#: Above this many messages in one prune, the archive is written WITHOUT the
#: vector index (see _archive_pruned). Measured: ~17 ms per message of embedding
#: against 0.02 ms for the row itself, and the largest of 1000 production prunes
#: was 11 messages — so this only ever trips on a runaway loop.
_SEMANTIC_INDEX_MAX_BATCH = 200


def _ref_type(message: dict[str, Any]) -> str | None:
    """The placeholder type of a message, or None if it carries real content.

    Cheap substring test before the JSON parse: this runs over every message on
    every compaction, and the vast majority are ordinary text.
    """
    content = message.get("content")
    if not isinstance(content, str):
        return None
    # Deliberately NOT windowed to the first N characters, matching
    # is_compaction_system_message in the agent core: a window couples this to
    # JSON key order, and one `sort_keys=True` in a producer would push "type"
    # past it. This predicate decides protection, ranking and archive exclusion
    # — losing it silently would stack breadcrumbs and mis-rank candidates.
    if not any(t in content for t in _PLACEHOLDER_TYPES):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    ref_type = data.get("type")
    if ref_type not in _PLACEHOLDER_TYPES:
        return None
    allowed = _PLACEHOLDER_ROLES.get(ref_type)
    if allowed is not None and message.get("role") not in allowed:
        return None  # an echo of a placeholder, not a placeholder
    return ref_type


def _is_prune_notice(message: dict[str, Any]) -> bool:
    """The breadcrumb. Role is already part of ``_ref_type``'s answer."""
    return _ref_type(message) == PRUNE_NOTICE_TYPE


def _is_archive_pointer(message: dict[str, Any]) -> bool:
    """Whether a message is ALREADY the placeholder left by a previous archival."""
    return _ref_type(message) == ARCHIVED_REF_TYPE


class LayeredCompactionStrategy:
    """Implements progressive context compaction.
    
    This strategy applies compression techniques in layers, starting with
    fully reversible operations and progressively moving to more aggressive
    techniques only if needed to reach the target token budget.
    
    Usage:
        strategy = LayeredCompactionStrategy(
            tool_store, core_memory, archival_memory, config
        )

        result = strategy.compact(messages, current_tokens)

        # Use result.modified_messages as the new conversation history
    """

    def __init__(
        self,
        tool_store: ToolResultStore,
        core_memory: CoreMemory,
        archival_memory: ArchivalMemory,
        config: CompactionConfig | None = None,
        media_store: MediaStore | None = None
    ):
        """Initialize the compaction strategy.

        Args:
            tool_store: Store for tool results
            core_memory: Core memory for important facts
            archival_memory: Archive for old messages
            config: Compaction configuration
            media_store: Optional store for inline media before compaction
        """
        self.tool_store = tool_store
        self.core_memory = core_memory
        self.archival_memory = archival_memory
        self.config = config or CompactionConfig()
        self.media_store = media_store
        #: Set per run by ``compact``; pre-set so a directly called layer
        #: cannot fail on a missing attribute instead of storing the media.
        #: It is a SHARED pot, not a private one — ``_store_inline_media`` says
        #: so out loud the first time anything lands in it.
        self._current_session_id = _SHARED_SESSION_ID
        self._warned_shared_session = False
    
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
            hint_text: Hint text to add (e.g., "[Audio removed. read(ref=...)]")
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
                        # APPEND, never assign: one tool result routinely carries
                        # several attachments, and each gets its own hint through
                        # here. Assigning let the last one silently delete every
                        # earlier hint, so a removed attachment left no trace at
                        # all — the plain-text branch below always appended.
                        previous = data.get("_media_compacted")
                        data["_media_compacted"] = (
                            f"{previous}\n{hint_text}" if previous else hint_text)
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
                        
                        # Inline media - check various formats
                        elif item.get("type") in _MEDIA_TYPES:
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

    def _is_protected(self, msg: dict[str, Any]) -> bool:
        """Messages no layer may remove or archive. One rule, every layer.

        Each layer used to spell out its own version of this and they drifted:
        Layer 3 checked only the role, so with keep_system_messages off it
        dropped the prune breadcrumb — which is protected on its TYPE for a
        reason. That breadcrumb is the only thing telling the agent something
        left the view at all, and it carries the running total; dropping it
        resets the total to zero on the next prune, so the count silently
        restarts and the agent is told 12 messages went when it was 40.
        """
        if msg.get("role") == "system" and self.config.keep_system_messages:
            return True
        return _is_prune_notice(msg)

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
        """Identity of a media item, for duplicate detection.

        A path identifies a file; everything else is identified by its actual
        payload, pulled out by the ONE shared extractor
        (`extract_inline_media`) that also feeds `_store_inline_media`.

        This used to be a fourth hand-written format reader and recognised
        exactly one of the five wire shapes — the OpenAI data URL. Anthropic
        `source.data`, Gemini `inline_data`, `audio_url` and `video_url` all
        hashed to None, so deduplication silently never fired for them: the
        same 40 MB image twice in a conversation stayed twice in the request.

        It also hashed only the first 1000 base64 characters. Two different
        videos sharing a prefix (container header plus metadata) compared
        equal, and one of them was dropped as a duplicate. Hashing the full
        payload costs a hash over bytes we already hold in memory.

        Args:
            item: Media item dict (from multimodal_content or content list)

        Returns:
            Hash string or None if the item carries neither path nor payload
        """
        file_path = item.get("path", "")
        if file_path:
            normalized = os.path.normpath(file_path)
            return hashlib.md5(normalized.encode()).hexdigest()[:16]

        try:
            raw_bytes, _, _ = extract_inline_media(item)
        except ValueError:
            # The extractor deliberately raises on a corrupt payload rather
            # than pretending there is no media. Identity is a different
            # question: an item we cannot decode is simply not provably equal
            # to anything, so it must not be merged with another one — and it
            # must not take the whole compaction down either.
            logger.debug("Media item has an undecodable payload, not hashable")
            return None

        if raw_bytes:
            return hashlib.md5(raw_bytes).hexdigest()[:16]

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
    
    def _iter_media(
        self,
        messages: list[dict[str, Any]],
        *,
        mm_types: tuple[str, ...] | None = None,
    ) -> Iterator[tuple[int, int, dict[str, Any], bool]]:
        """Every media item in the conversation — both storage places, one walk.

        Yields ``(msg_idx, item_idx, item, in_mm)``. ``in_mm`` is not decoration:
        it decides HOW the item leaves later, and the two ways are genuinely
        different (see ``_evict_media``).

        ``mm_types=None`` means every dict in ``multimodal_content`` counts.
        That is what three of the four selections want, because a tool
        attachment carries no dependable ``type`` — only the always-compact rule
        narrows it, and it says so.
        """
        for msg_idx, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list):
                for item_idx, item in enumerate(content):
                    if isinstance(item, dict) and item.get("type", "") in _MEDIA_TYPES:
                        yield msg_idx, item_idx, item, False

            mm_content = msg.get("multimodal_content")
            if isinstance(mm_content, list):
                for item_idx, item in enumerate(mm_content):
                    if not isinstance(item, dict):
                        continue
                    if mm_types is None or item.get("type", "") in mm_types:
                        yield msg_idx, item_idx, item, True

    def _media_subject(self, item: dict[str, Any], with_filename: bool = True) -> str:
        """What a hint calls the item it replaces (see ``_MEDIA_SUBJECTS``)."""
        subject = _MEDIA_SUBJECTS.get(str(item.get("type", "")), "Media")
        if with_filename:
            return f"{subject} '{self._get_media_filename(item)}'"
        return subject

    def _media_hint(
        self,
        subject: str,
        reason: str,
        path: str,
        *,
        unrecoverable: str = "",
    ) -> str:
        """The ONE place a model-facing media hint is written.

        The path stays in DOUBLE QUOTES because that is literally what the model
        copies into ``read(ref="…")``. Every eviction path used to spell this
        out for itself, which is how eleven of these hints kept pointing at
        ``recall(query=…)`` long after that tool was deleted — a dead
        instruction handed over at the exact moment the bytes left the context.
        """
        if path:
            return f'[{subject} {reason} read(ref="{path}") loads it again.]'
        return unrecoverable or f"[{subject} {reason}]"

    async def _evict_media(
        self,
        result: CompactionResult,
        picks: list[_MediaPick],
        *,
        store: bool = False,
        count_bytes: bool = True,
    ) -> int:
        """Throw the picked media out and leave exactly one hint per item.

        The ONE eviction. Everything above it only decides WHICH items go. The
        asymmetry between the two storage places is real and lives here:

        * ``content`` item → REPLACED in place by a ``{"type": "text"}`` item,
          because a content list is positional and the model reads it in order.
        * ``multimodal_content`` item → DELETED, hint appended to the content,
          because that list is re-encoded verbatim at call time and has no slot
          for prose.

        ``store`` saves an inline payload to disk first, but only for content
        items — a ``multimodal_content`` item already IS a file reference. An
        item that ends up with no address gets a hint WITHOUT a ``read(...)``
        suffix: dedup used to substitute the literal "N/A" there, handing the
        model ``read(ref="N/A")`` — the dead instruction ``_media_hint`` exists
        to prevent, and ``read(ref="")`` for an item carrying an empty path.

        Returns the number of items evicted; ``tokens_saved`` and (unless
        ``count_bytes`` is off) ``media_bytes_saved`` are booked here. The
        per-reason counter stays with the caller — one of them assigns where the
        others add, and that difference is load-bearing in ``_finalize``.
        """
        messages = result.modified_messages
        mm_removals: dict[int, list[tuple[int, str]]] = defaultdict(list)
        evicted = 0

        for msg_idx, item_idx, item, in_mm, subject, reason in picks:
            path = item.get("path", "")
            if store and not in_mm and not path:
                source = item.get("source", {})
                item_type = item.get("type", "")
                media_type = (source.get("media_type", item_type)
                              if isinstance(source, dict) else item_type)
                # Off the event loop: this base64-decodes and writes a file, and
                # the byte-limit caller arrives with dozens of items at 90 MB.
                path = await asyncio.to_thread(
                    self._store_inline_media,
                    item, str(media_type), self._current_session_id) or path

            hint = self._media_hint(subject, reason, path)

            if in_mm:
                mm_removals[msg_idx].append((item_idx, hint))
            else:
                messages[msg_idx]["content"][item_idx] = {"type": "text", "text": hint}

            evicted += 1
            result.tokens_saved += estimate_inline_data_tokens(item)
            if count_bytes:
                result.media_bytes_saved += self._estimate_item_bytes(item)
            logger.debug("Evicted media: %s", hint)

        # Delete from the tail forwards so the lower indices stay valid.
        for msg_idx, removals in mm_removals.items():
            msg = messages[msg_idx]
            mm_list = msg.get("multimodal_content", [])
            for item_idx, hint in sorted(removals, key=lambda x: x[0], reverse=True):
                # Every index came from enumerating THIS list, nothing shrinks it
                # between selection and here, and the descending order keeps the
                # lower ones valid — so a miss is impossible, not merely unlikely.
                # It used to be skipped silently, which would have left three
                # counters claiming an eviction that never happened.
                assert item_idx < len(mm_list), (
                    f"multimodal_content shrank under the eviction: index "
                    f"{item_idx} of {len(mm_list)} in message {msg_idx}")
                del mm_list[item_idx]
                self._add_media_hint_to_content(msg, hint)

        return evicted

    async def _deduplicate_media(self, result: CompactionResult) -> None:
        """Selection: the same bytes twice — every occurrence but the newest goes.

        Nothing is written to disk here, and that is deliberate: the payload is
        still in the conversation a few messages further down.
        """
        if not self.config.deduplicate_media:
            return

        by_hash: dict[str, list[tuple[int, int, dict[str, Any], bool]]] = defaultdict(list)
        for found in self._iter_media(result.modified_messages):
            media_hash = self._compute_media_hash(found[2])
            if media_hash:
                by_hash[media_hash].append(found)

        picks = [
            _MediaPick(msg_idx, item_idx, item, in_mm, self._media_subject(item),
                       "- duplicate compacted. Newer version exists later in "
                       "conversation.")
            for occurrences in by_hash.values() if len(occurrences) > 1
            # Ascending by message index, so the LAST one is the newest.
            for msg_idx, item_idx, item, in_mm in sorted(occurrences,
                                                         key=lambda o: o[0])[:-1]
        ]

        result.media_deduplicated += await self._evict_media(
            result, picks, count_bytes=False)

        if result.media_deduplicated > 0:
            result.final_tokens = self._estimate_messages_tokens(result.modified_messages)
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

        # media_store_max_files is a PER-SESSION quota, so everyone who arrives
        # without a session id shares one pot and evicts each other's files.
        # Once per strategy is enough to find the caller; per item would be
        # dozens of lines in a single byte-limit compaction.
        if session_id == _SHARED_SESSION_ID and not self._warned_shared_session:
            self._warned_shared_session = True
            logger.warning(
                "Storing media under the shared '%s' session — no session id "
                "reached the compaction, so this run shares one per-session "
                "file quota with every other such caller", _SHARED_SESSION_ID)

        try:
            # ONE extractor for every wire shape, shared with media_ops:
            # agent_system.utils.multimodal_tool_content.extract_inline_media.
            # This used to be four hand-written format branches here, and they
            # were one short — `video_url` was missing, so inline video was
            # evicted without ever being stored and could not be restored.
            raw_bytes, mime, source_name = extract_inline_media(item)
            if raw_bytes is None:
                # Kept for the diagnostic, not for the outcome: without it the
                # encode below raises and the outer handler returns None too.
                # A mutation removing this stays green for exactly that reason.
                logger.debug(f"No inline data found in item with keys: {list(item.keys())}")
                return None

            # Raw bytes straight through: media_store.store() takes
            # `str | bytes` and b64decodes a string right back. Encoding
            # here would cost an encode, a decode and a 1.33x copy per
            # item — on the byte-limit path that is dozens of items at 90 MB.
            media_type = mime or media_type
            source_name = source_name or item.get("name")

            # Store the media
            stored_path = self.media_store.store(
                data=raw_bytes,
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

        # Selection: everything outside the last N messages, but only items big
        # enough to be part of the problem — a 2 KB thumbnail never blew a
        # 90 MB request, and evicting it costs a hint that is nearly as long.
        cutoff = max(0, len(messages) - keep_last_n)
        picks = []
        for msg_idx, item_idx, item, in_mm in self._iter_media(messages):
            if msg_idx >= cutoff:
                continue
            item_bytes = self._estimate_item_bytes(item)
            if item_bytes < 10000:
                continue
            picks.append(_MediaPick(
                msg_idx, item_idx, item, in_mm, self._media_subject(item),
                f"removed to reduce request size. Saved {item_bytes / 1024:.0f}KB."))

        bytes_before = result.media_bytes_saved
        # store=True like every other eviction: this one fires at 90 MB, i.e.
        # when the payload is at its largest, and used to drop inline data with
        # no copy on disk and a hint that could name no address.
        compacted_count = await self._evict_media(result, picks, store=True)

        if compacted_count > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            result.media_compacted_after_event += compacted_count
            bytes_saved = result.media_bytes_saved - bytes_before
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

        # Selection: everything but the newest message.
        picks = []
        for msg_idx, item_idx, item, in_mm in self._iter_media(messages):
            if msg_idx >= len(messages) - 1:
                continue
            if item.get("compacted") and not in_mm:
                continue  # already a placeholder
            picks.append(_MediaPick(
                msg_idx, item_idx, item, in_mm,
                # The content variant names no file and never has — its hint
                # sits in the reading order right where the item was, so the
                # position already says which one went.
                self._media_subject(item, with_filename=in_mm),
                f"removed after {trigger}."))

        result.media_compacted_after_event += await self._evict_media(
            result, picks, store=True)

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
        keep_count = self.config.always_compact_media_keep_last
        if not messages or keep_count <= 0:
            return

        # Selection: the last N messages that CARRY media keep theirs, the rest
        # lose it. multimodal_content is narrowed to real media here, unlike
        # everywhere else — this is the only rule that lets an item decide
        # whether a whole MESSAGE counts as recent, so a stray attachment must
        # not spend one of the N slots.
        found = list(self._iter_media(messages, mm_types=("image", "audio", "video")))
        with_media = sorted({msg_idx for msg_idx, _, _, _ in found})
        if not with_media:
            return

        protected = set(with_media[-keep_count:])
        picks = [
            _MediaPick(msg_idx, item_idx, item, in_mm,
                       self._media_subject(item), "compacted.")
            for msg_idx, item_idx, item, in_mm in found
            if msg_idx not in protected
        ]

        compacted_count = await self._evict_media(result, picks, store=True)
        result.media_always_compacted = compacted_count

        if compacted_count > 0:
            result.final_tokens = self._estimate_messages_tokens(messages)
            logger.info(
                f"Always-compact media: {compacted_count} items compacted, "
                f"kept last {keep_count} messages with media "
                f"({len(protected)} protected)"
            )

    async def _compact_multimodal_content(
        self,
        content: list,
        result: CompactionResult,
        session_id: str = "default",
        preserve_media: bool = False
    ) -> list:
        """Compact multimodal content by replacing large items with references.

        Handles:
        - text_file items: Move large content to the tool-result store
        - audio items: Replace large base64 data with [Audio removed] placeholder
        - image items: Replace large base64 data with [Image removed] placeholder

        Args:
            content: Multimodal content list (text, image, text_file, audio, etc.)
            result: CompactionResult to update tokens_saved
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

            # An attached text file goes to the tool-result store. It used to
            # become a $VAR, which is gone; the store holds the same thing
            # (content, ref, summary) and its refs already work with list and
            # read, so this is one store instead of two rather than a new path.
            if item_type == "text_file":
                file_content = item.get("content", "")
                file_name = item.get("name") or "file"
                token_count = estimate_content_tokens(file_content)

                if token_count >= self.config.tool_result_min_size and file_content:
                    # Content-derived key, so re-attaching the same file lands
                    # on the same row instead of filling the store with copies.
                    file_key = (
                        f"file_{file_name}_"
                        f"{hashlib.sha256(file_content.encode()).hexdigest()[:16]}"
                    )
                    reference = await asyncio.to_thread(
                        self.tool_store.store_and_reference,
                        tool_call_id=file_key,
                        tool_name=file_name,
                        content=file_content,
                        session_id=session_id,
                        summary=" ".join(file_content.split())[:200],
                    )
                    ref_id = json.loads(reference).get("ref_id", "")
                    hint = (
                        f'[File "{file_name}" stored, {token_count} tokens. '
                        f'read(ref="{ref_id}", find="...") for the parts you need.]'
                    )
                    compacted.append({"type": "text", "text": hint})
                    result.tool_results_stored += 1
                    result.tokens_saved += token_count - estimate_content_tokens(hint)
                    logger.debug(
                        f"Stored text_file '{file_name}' ({token_count} tokens) → {ref_id}"
                    )
                    continue

            # Inline audio/image payloads: save to disk first, so the placeholder
            # can name an address instead of an apology. Skipped entirely when
            # preserve_media is set (the last user message keeps its media).
            elif item_type in _MEDIA_TYPES:
                if preserve_media:
                    compacted.append(item)
                    continue

                inline_tokens = estimate_inline_data_tokens(item)
                if inline_tokens >= self.config.tool_result_min_size:
                    subject = self._media_subject(item, with_filename=False)
                    # Only steers the stored file's extension — _store_inline_media
                    # re-derives the type from the payload it actually finds.
                    source = item.get("source", {})
                    media_type = (source.get("media_type", subject.lower())
                                  if isinstance(source, dict) else subject.lower())

                    item_bytes = self._estimate_item_bytes(item)
                    stored_path = self._store_inline_media(item, media_type, session_id)

                    compacted.append({"type": "text", "text": self._media_hint(
                        subject, "removed.", stored_path or "",
                        # The advice used to be "use store_fact BEFORE
                        # compaction" — read at the one moment compaction has
                        # already happened. What is left to do is ask again.
                        unrecoverable=(
                            f"[{subject} removed - not recoverable. Ask for it "
                            f"again if you still need it.]"),
                    )})
                    result.tokens_saved += inline_tokens
                    result.media_compacted_after_event += 1
                    result.media_bytes_saved += item_bytes
                    logger.debug(
                        f"Removed {subject.lower()} inline data: {inline_tokens:,} "
                        f"tokens, {item_bytes / 1024:.0f}KB saved")
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
                
                hints_to_add.append(self._media_hint(
                    self._media_subject(item, with_filename=False),
                    "compacted.", file_path))
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
        - Move attached text files to the tool-result store
        - Evict inline media, storing it to disk first
        """
        logger.debug("Applying Layer 1: Reversible compaction")

        messages = result.modified_messages

        # Process messages in reverse (newer first, but skip last N tool results UNLESS too large)
        tool_results_seen = 0

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

                # Already a pointer — re-archiving it stores a pointer to a
                # pointer and gains nothing, the content is long gone from
                # this message. Layer 2 has this exact guard (see the comment
                # there: chains 20 levels deep, 637 of 1286 entries nothing
                # but pointers); Layer 1 runs BEFORE Layer 2 and never had it,
                # so a small-enough placeholder could loop back through here
                # on a later turn and get "archived" again.
                if _ref_type(msg) is not None:
                    continue

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

                # A retrieval tool's OWN answer must stay in the conversation.
                # Storing it away replaces the content the agent just fetched
                # with a reference to it — the agent asks again, that answer is
                # stored too, and retrieval can never complete. Observed live:
                # 25 read results externalised in one turn, the first of which
                # already contained the answer the agent then reported missing.
                if should_archive and _is_retrieval_result(msg):
                    logger.debug(
                        "Keeping retrieval result %s inline (externalising it "
                        "would undo the retrieval)", msg.get("name"))
                    should_archive = False

                if should_archive:
                    # Store and replace with reference
                    tool_name = msg.get("name", "unknown")
                    tool_call_id = msg.get("tool_call_id", "")

                    # A bare ref+token_count gives the model nothing to decide
                    # what to find= for — it can only page blindly. A cheap
                    # preview (no LLM call) is enough to point it at find=.
                    preview = " ".join(content.split())[:200]

                    # Wrap sync SQLite operation in thread pool
                    reference = await asyncio.to_thread(
                        self.tool_store.store_and_reference,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        content=content,
                        summary=preview,
                    )
                    
                    messages[i] = {**msg, "content": reference}
                    result.tool_results_stored += 1
                    result.tokens_saved += token_count - estimate_content_tokens(reference)
                    
                    if is_too_large:
                        logger.debug(
                            f"Auto-archived large tool result '{tool_name}' "
                            f"({token_count} tokens, exceeds max_inline_size)"
                        )
            
            # Long assistant messages used to be replaced by a $VAR reference
            # here. Measured over 1000 production compactions that fired 9
            # times against 3730 tool results, while rewriting an old message
            # broke the provider prompt cache from that point on every time.
            # Layer 2 archives them by age instead, which is what actually ran.

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

            # Already a pointer — archiving it again stores a pointer to a
            # pointer and gains nothing, because the content is long gone from
            # this message. Measured before this guard: chains 20 levels deep,
            # 637 of 1286 entries in one session being nothing but pointers, and
            # the summaries degrading to `User: {"type": "archived_ref"...}`. An
            # agent following such a ref never reaches the text, which is what
            # made retrieval look broken.
            #
            # The guard covers EVERY placeholder type, not just archive refs.
            # Layer 1 runs immediately before this and turns old tool results
            # into `tool_result_ref` — those were then archived here, producing
            # `archived_ref -> tool_result_ref`, and the chain resolver only
            # follows archive refs, so the walk ended on the pointer and handed
            # the agent JSON instead of the text. Same defect, other entrance.
            if _ref_type(msg) is not None:
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
        
        # Drop placeholders that entered through the tool-pair expansion above:
        # that branch adds indices directly, so the per-message guard never sees
        # them. Measured after guarding only the loop, 13 of 57 entries in a live
        # session were still pointers — all of them halves of a tool pair.
        indices_to_archive = {i for i in indices_to_archive
                              if _ref_type(messages[i]) is None}

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

    async def _apply_layer3(self, result: CompactionResult) -> None:
        """Layer 3: last-resort compaction — messages leave the conversation.

        "Irreversible" used to be literal: this layer deleted outright, while
        Pre-Layer P right next to it wrote everything to the archive first. Same
        operation, two different answers to "is the content gone afterwards".
        It goes through the same archive-then-delete path now, so what leaves
        the view here is still reachable through the retrieval tools.
        """
        logger.debug("Applying Layer 3: dropping old messages")
        
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

            if self._is_protected(msg):
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
        
        result.messages_dropped = await self._archive_then_remove(
            messages, indices_to_remove, "Layer 3"
        )

        # The summaries on the remaining archive references STAY. This pass used
        # to strip them down to a bare ref_id, which was consistent while there
        # was no way to follow a reference: a pointer you cannot dereference is
        # just ballast, so the label was worth nothing. With the retrieval tools
        # in place the trade inverts — the summary IS the catalogue entry the
        # agent reads to decide whether a ref is worth fetching, and an
        # unlabelled address is the one form it can never act on. If the space
        # is genuinely needed, dropping the whole placeholder is the better
        # move: the content stays findable through list(section='history').
        
        result.final_tokens = self._estimate_messages_tokens(messages)
        logger.debug(
            f"Layer 3 complete: dropped {result.messages_dropped} messages "
            f"(including tool_call pairs)"
        )

        # Note: max_messages limit is now handled by Pre-Layer P at the start of compact()
        # This ensures message count is limited even when token thresholds aren't reached

    async def _prune_by_message_count(self, result: CompactionResult) -> None:
        """Pre-Layer P: Prune oldest messages to enforce max_messages limit.

        Runs BEFORE the token-based layers to prevent excessive message counts
        that waste API overhead even when the token count is low. Measured over
        the last 1000 production compactions, this is the layer that actually
        does the work: it fired 476 times against Layer 2's 115 and Layer 3's
        zero — the turn-based layers barely apply because a "turn" is a USER
        message, and an agent run is one user message with hundreds of steps.

        It therefore also owns the recoverability of what it removes. Everything
        that leaves the view is written to the archive FIRST, so the agent can
        find it again through the retrieval tools; before that, this was the one
        place in the system that destroyed content outright.

        System messages are kept if keep_system_messages is True. The first AND
        last user messages are protected. Tool call/result pairs move together.
        """
        messages = result.modified_messages
        max_msgs = self.config.max_messages

        if len(messages) <= max_msgs:
            return

        # Prune down to the low-water mark, not back to the limit. Trimming to
        # exactly max_messages means the next step is over it again and prunes
        # again, and every prune rewrites the front of the conversation — so
        # the provider prompt cache was being thrown away on EVERY step once a
        # session reached the limit. Going deeper once buys `headroom` quiet
        # steps for the same single cache break.
        #
        # Capped at half the limit. Unclamped, any max_messages below the
        # headroom (default 50) drove the target to 1 — max(1, 40 - 50) — so
        # the FIRST prune collapsed the conversation to the protected minimum
        # instead of trimming 50 messages. The cap also keeps excess small
        # enough that _select_prune_candidates' window (2 * excess) stays in
        # the old head instead of reaching into the working tail.
        headroom = min(max(0, self.config.max_messages_headroom), max_msgs // 2)
        target = max(1, max_msgs - headroom)
        excess = len(messages) - target

        # The breadcrumb below is itself a message. Removing exactly `excess`
        # and then adding it would land one over the limit and re-trigger on
        # every following call, so pay for it here — but only when there is not
        # already one in the list to replace.
        if not any(_is_prune_notice(m) for m in messages):
            excess += 1

        logger.info(
            f"Pre-Layer P: {len(messages)} messages exceeds limit of {max_msgs}, "
            f"pruning ~{excess} oldest down to {target}"
        )

        # Build tool_call mapping to keep pairs together
        tool_map = self._build_tool_call_map(messages)

        protected = {i for i, msg in enumerate(messages)
                     if self._is_protected(msg)}

        user_indices = [
            i for i, msg in enumerate(messages) if msg.get("role") == "user"
        ]
        if user_indices:
            # The LAST user message must survive or the API call is invalid.
            # The FIRST one is the task everything else refers to: in a single
            # agent run they are the same message, but in a continued session
            # the task sits at the front and was the very first thing to go.
            protected.add(user_indices[-1])
            protected.add(user_indices[0])

        indices_to_remove = self._select_prune_candidates(
            messages, tool_map, protected, excess
        )

        pruned_count = await self._archive_then_remove(
            messages, indices_to_remove, "Pre-Layer P"
        )

        result.messages_pruned = pruned_count
        result.final_tokens = self._estimate_messages_tokens(messages)

        if len(messages) > max_msgs:
            # Everything left is protected (a long leading system block, or the
            # only user message). Saying so once per call beats an INFO line
            # that reads like work was done while the list never shrinks.
            logger.warning(
                f"Pre-Layer P: still {len(messages)} messages over the limit of "
                f"{max_msgs} after pruning {pruned_count} — the remainder is "
                f"protected (system messages, first/last user message)"
            )

        logger.info(
            f"Pre-Layer P: pruned {pruned_count} messages, "
            f"now {len(messages)} messages, {result.final_tokens:,} tokens"
        )

    def _select_prune_candidates(
        self,
        messages: list[dict[str, Any]],
        tool_map: dict[str, list[int]],
        protected: set[int],
        excess: int
    ) -> set[int]:
        """Pick which indices to remove: oldest first, cheapest-to-lose first.

        Age stays the primary criterion — the tail is the agent's working set
        and must not be touched. But among the oldest candidates, a placeholder
        is strictly cheaper to drop than real content: its body already sits in
        a store, so losing it costs an address, while real content costs an
        archive write and a retrieval turn to get back.

        The candidate window is deliberately narrow (twice what we need). A
        global sort by cheapness would reach into the recent tail and strip the
        pointers the agent is actively working with.
        """
        candidates = [i for i in range(len(messages)) if i not in protected]
        window = candidates[:max(2 * excess, 20)]

        def cost(i: int) -> tuple[int, int]:
            # 0/1: placeholder (body is stored elsewhere), 2: real content.
            ref = _ref_type(messages[i])
            rank = 0 if ref == TOOL_RESULT_REF_TYPE else 1 if ref else 2
            return (rank, i)

        groups = self._tool_call_groups(messages, tool_map)

        indices_to_remove: set[int] = set()
        for i in sorted(window, key=cost):
            if len(indices_to_remove) >= excess:
                break
            if i in indices_to_remove:
                continue
            # Whole group at once. Selecting one member and cascading from it is
            # NOT enough: reaching a tool result first pulls in its assistant,
            # and the assistant's OTHER results are then never expanded, because
            # the loop skips an index it already holds. The old code got away
            # with a per-index cascade only because it walked strictly ascending
            # and so always met the assistant before its results — the cost sort
            # inverts exactly that order, and preferentially so.
            group = groups.get(i, (i,))
            # Today `protected` holds only system/user indices and a group only
            # assistant/tool indices, so this never fires. It is here because
            # the guarantee currently rests on that coincidence: one new rule in
            # `protected` and the closure would silently evict a protected
            # message. Skipping the whole group keeps BOTH invariants — nothing
            # protected leaves, and no pair is split.
            if any(j in protected for j in group):
                continue
            indices_to_remove.update(group)

        return indices_to_remove

    @staticmethod
    def _tool_call_groups(
        messages: list[dict[str, Any]],
        tool_map: dict[str, list[int]]
    ) -> dict[int, frozenset[int]]:
        """Map each index to the tool-call unit it belongs to.

        An assistant with parallel tool_calls plus ALL of its results is one
        indivisible unit: half a pair is a 400 from every provider, and the
        broken list is then persisted and re-sent on every following step.

        Indices with no tool involvement are absent — callers treat that as a
        group of one. tool_map holds only assistant/tool indices, so a group can
        never contain a protected system or user message.
        """
        # Union-find, NOT one set per assistant. A per-assistant dict is
        # last-writer-wins, and two assistants that share a tool_call id produce
        # overlapping units: the second write leaves the first unit's exclusive
        # members pointing at a stale set, and removing the shared part strands
        # them. Reproduced with a two-call assistant and a later one-call
        # assistant reusing an id — the result was a tool message with no
        # tool_call. Colliding ids are not hypothetical: the Gemini batch client
        # mints `call_{name}_{index}`, which repeats across assistant turns.
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_a] = root_b

        for i, msg in enumerate(messages):
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            find(i)  # an assistant with tool_calls is always its own component
            linked = False
            for tc in msg.get("tool_calls", []):
                tc_id = tc.get("id")
                for j in tool_map.get(tc_id, ()) if tc_id else ():
                    union(i, j)
                    linked = True
            if not linked:
                # tool_calls without usable ids: nothing links the results to
                # this message, so both halves would be free to move apart.
                # Absorb the contiguous run of tool messages that follows —
                # the shape every provider emits — instead of splitting a pair.
                for j in range(i + 1, len(messages)):
                    if messages[j].get("role") != "tool":
                        break
                    union(i, j)

        components: dict[int, set[int]] = {}
        for index in list(parent):
            components.setdefault(find(index), set()).add(index)

        groups: dict[int, frozenset[int]] = {}
        for members in components.values():
            unit = frozenset(members)
            for member in members:
                groups[member] = unit
        return groups

    async def _archive_then_remove(
        self,
        messages: list[dict[str, Any]],
        indices_to_remove: set[int],
        caller: str,
    ) -> int:
        """Store the selected messages, then delete them. Returns how many went.

        The ONLY way a message may leave the conversation. Both callers used to
        carry their own copy of this sequence and they disagreed on the one
        thing that matters: Pre-Layer P archived first, Layer 3 deleted outright.
        A single path means a new layer cannot get that wrong by omission.

        Zero means nothing was removed — the archive write failed and the
        messages are still in the list. That order is deliberate: the write is
        all-or-nothing (one malformed message aborts the batch), so deleting
        anyway would destroy the whole batch while the breadcrumb promises they
        can be looked up. An over-long context is a cost; destroyed content is
        not recoverable, and the remaining layers still run.
        """
        if not indices_to_remove:
            return 0

        snapshot = list(messages)
        selected = [messages[i] for i in sorted(indices_to_remove)]

        # Placeholders are skipped inside: their body is already in a store, and
        # archiving a pointer would only produce a pointer to a pointer.
        if not await self._archive_pruned(selected):
            logger.warning(
                f"{caller}: skipping the removal of {len(selected)} messages — "
                f"the archive write failed and dropping them would destroy them"
            )
            return 0

        for idx in sorted(indices_to_remove, reverse=True):
            del messages[idx]

        # Note: _ensure_valid_message_sequence rebuilds tool_map internally at
        # each iteration, since indices move as it deletes.
        extra = self._ensure_valid_message_sequence(messages, caller)

        # That pass deletes on its own account, so ask the list what actually
        # went rather than trusting the selection. Identity, not equality:
        # duplicate contents are common and nothing here rewrites a message.
        survivors = {id(m) for m in messages}
        removed = [m for m in snapshot if id(m) not in survivors]
        if extra:
            # These are already gone — the sequence fix deletes to make the
            # request valid at all, and that cannot be undone. Archive what we
            # can and say so if it fails.
            already = {id(m) for m in selected}
            if not await self._archive_pruned(
                [m for m in removed if id(m) not in already]
            ):
                logger.error(
                    f"{caller}: {extra} messages removed by the sequence fix "
                    f"could not be archived and are lost"
                )

        self._leave_prune_notice(messages, len(removed))
        return len(removed)

    async def _archive_pruned(self, removed: list[dict[str, Any]]) -> bool:
        """Write pruned messages to the archive. True when they are safe to drop.

        Placeholders are skipped — their body is already stored and archiving
        one would store a pointer, not content.

        Never raises: bookkeeping must not sink the compaction it belongs to.
        But it does REPORT, and the caller must not delete on False: the write
        is all-or-nothing (one malformed message aborts the batch), so a
        swallowed failure would delete every message of that prune while the
        breadcrumb still promises they can be looked up. Keeping an over-long
        context is recoverable; destroying the content is not — and the
        token-based layers remain as the backstop.
        """
        # Retrieval answers are exempt for the same reason Layer 1 exempts them
        # (see RETRIEVAL_MARKER): their body is content the agent just pulled OUT
        # of storage, so putting it back is a loop with no exit. Measured live
        # before this guard: the archive filled with the agent's own list()
        # answers, the next search ranked those above the original message, and
        # the agent followed refs to its own earlier replies until it gave up.
        # Removing them from the conversation is still fine — they are
        # re-derivable by asking again.
        payload = [m for m in removed
                   if _ref_type(m) is None and not _is_retrieval_result(m)]
        if not payload:
            return True

        # Embedding is the whole cost of a large batch: 4682 messages take 0.08 s
        # as rows and 80 s with the vector index. In steady state that never
        # matters (the largest of 1000 production prunes was 11 messages), but a
        # model that emits a runaway tool_call batch produces exactly the huge
        # first prune where a stall would hurt most. Beyond the cap the rows and
        # the FTS index still go in — the content stays listable and findable by
        # keyword, only vector similarity misses it — and the log says so.
        index_semantic = len(payload) <= _SEMANTIC_INDEX_MAX_BATCH
        if not index_semantic:
            logger.warning(
                f"Pre-Layer P: {len(payload)} messages exceed the semantic-index "
                f"batch cap of {_SEMANTIC_INDEX_MAX_BATCH}; archiving them "
                f"without vector indexing (list and keyword search still find them)"
            )

        try:
            await asyncio.to_thread(
                self.archival_memory.store_many, payload, None, index_semantic
            )
            return True
        except Exception as e:  # noqa: BLE001 - see docstring
            logger.error(
                f"Pre-Layer P: archiving {len(payload)} pruned messages failed, "
                f"keeping them in the conversation instead of destroying them: {e}"
            )
            return False

    def _leave_prune_notice(
        self, messages: list[dict[str, Any]], removed: int
    ) -> None:
        """Leave exactly ONE breadcrumb saying that older turns left the view.

        Constant cost regardless of how much went, and it is the only thing
        telling the agent there is something to look up at all — without it a
        bulk removal is indistinguishable from a conversation that never had
        those turns.
        """
        if removed <= 0:
            return

        # Replace, never stack. ALL existing notices go, not just the first:
        # a second one can arrive through a merged or restored history, and
        # removing only one leaves a permanent pair with disagreeing totals.
        # The carried total is the MAXIMUM of the notices found, not their sum.
        # Duplicates only ever arise from one lineage — a merged or restored
        # history where the same running count appears twice at different ages —
        # so summing would double-count the shared part of a counter whose only
        # job is not to drift.
        carried = 0
        for i in range(len(messages) - 1, -1, -1):
            if not _is_prune_notice(messages[i]):
                continue
            try:
                previous = json.loads(messages[i].get("content") or "{}")
                carried = max(carried, int(previous.get("total_removed") or 0))
            except (ValueError, TypeError):
                pass
            del messages[i]
        total = removed + carried

        insert_at = len(messages)
        for i, msg in enumerate(messages):
            if msg.get("role") != "system":
                insert_at = i
                break

        messages.insert(insert_at, {
            "role": "system",
            "content": json.dumps({
                "type": PRUNE_NOTICE_TYPE,
                "total_removed": total,
                # No promise that all N sit in the history archive: the count
                # includes placeholders whose bodies live in the tool-result
                # store instead. "Retrievable" is true for all of them.
                # Every call named here must WORK. This said "section='history'
                # or 'all'", and list(section='all') without a filter is an
                # error by design — so the one message telling the agent where
                # its history went also handed it a call that fails.
                "hint": (
                    f"{total} earlier messages of this conversation were moved "
                    f"out of view to keep it within limits. They are stored, "
                    f"not gone: list(section='history') shows the most recent "
                    f"of them, list(filter='...') searches everything, and "
                    f"read(ref=...) fetches one."
                )
            })
        })

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
        # way — tool results swapped for refs, attached files stored away,
        # media evicted, messages archived/dropped/pruned — those artifacts
        # are stale and will fail provider verification on a later turn
        # (HTTP 400 "encrypted content … could not be verified", deep into a
        # run). Invalidate them HERE, at the mutation site, so the chain
        # resets deterministically instead of failing reactively.
        mutated = (
            result.tool_results_stored
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

        BYTE-STABIL HALTEN. Der Block wird direkt hinter dem System-Prompt
        eingefuegt (hooks.py), steht also VOR der gesamten Konversation:
        jede Aenderung an ihm entwertet den Provider-Prompt-Cache fuer ALLES
        dahinter. Zaehler wie "There are 47 stored tool results" aendern sich
        bei jedem ausgelagerten Tool-Ergebnis -- gemessen an einem
        Multi-Turn-Lauf: Praefix-Bruch bei Message 50 von 197, Cache-Quote
        8-13 % statt 50-65 %. Die Zahlen sind fuer das Modell auch nicht
        handlungsleitend: es reagiert auf die Referenz IM Text, nicht auf
        eine Gesamtzahl. Also nur konstante Beschreibungen hier, nichts,
        was sich pro Turn bewegt.

        Die Variablen-Sektion war genau so ein Verstoss: sie listete JEDE
        angelegte Variable mit Namen und Zusammenfassung, aenderte sich also
        bei jeder neuen -- und entwertete den Cache fuer die ganze
        Konversation dahinter. Sie ist mit der $VAR-Ersetzung weg.

        Returns:
            System prompt section
        """
        sections = []

        # Tool result references - wrap sync SQLite operation
        tool_stats = await asyncio.to_thread(self.tool_store.get_stats)
        if tool_stats["total_entries"] > 0:
            sections.append(
                "## Tool Results\n"
                "Some tool results have been stored externally. When you see a JSON "
                "reference with `type: tool_result_ref`, use list(section='tool_results') "
                "to see what it contains, then read(ref=ref_id, find=\"...\") for just "
                "the matching part, or read(ref=ref_id) to page through it."
            )
        
        # Archival memory - wrap sync SQLite operation
        archive_stats = await asyncio.to_thread(self.archival_memory.get_stats)
        if archive_stats["total_messages"] > 0:
            sections.append(
                "## Conversation Archive\n"
                "Older messages have been archived. When you see a JSON reference "
                "with `type: archived_ref`, read(ref=ref_id) returns that message; "
                "list(section='history') shows what else is back there."
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
