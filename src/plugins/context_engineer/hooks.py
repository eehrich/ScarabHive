"""Context Engineer Plugin - Hook implementations.

This module implements the main hook for context engineering, which applies
layered compaction strategies to optimize context usage.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from agent_system.hooks import HookContext, HookResult, SchemaBasedPluginHook
from agent_system.llm.models import ChatMessage
from agent_system.mcp.status import StatusScope, status_bus

from .archival_memory import ArchivalMemory
from .compaction import CompactionConfig, LayeredCompactionStrategy
from .core_memory import CoreMemory
from .media_store import MediaStore
from .tool_result_store import ToolResultStore
from .variable_manager import VariableManager

logger = logging.getLogger(__name__)


class ContextEngineerPlugin(SchemaBasedPluginHook):
    """Schema-based plugin for advanced context engineering.
    
    Implements layered compaction strategy:
    1. Reversible: Store tool results, create variables
    2. Semi-Reversible: Archive old messages with summaries
    3. Irreversible: Drop very old messages
    
    All stored information can be retrieved via MCP tools.
    
    Configuration is loaded from schema.yaml.
    """
    
    def __init__(
        self,
        plugin_dir: Path | str,
        stats_history: list[dict[str, Any]] | None = None,
        history_callback: Any = None
    ):
        """Initialize the context engineer plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
            stats_history: Optional list for web UI tracking
            history_callback: Optional callback to invoke after adding history events
        """
        super().__init__(plugin_dir)
        
        self.plugin_dir = Path(plugin_dir)
        self.stats_history = stats_history
        self.history_callback = history_callback
        
        # Session tracking (with TTL to prevent memory leak)
        self._last_compaction_time: dict[str, float] = {}
        self._session_components: dict[str, dict[str, Any]] = {}
        
        # Load config from schema (will be overridden by server.py sync)
        config = self.get_config()
        
        # Memory management settings
        self._session_ttl_seconds = int(config.get("session_ttl_seconds", 7200))
        self._max_tracked_sessions = int(config.get("max_tracked_sessions", 100))
        
        # Token thresholds
        self.layer1_threshold = int(config.get("layer1_threshold", 80000))
        self.layer2_threshold = int(config.get("layer2_threshold", 100000))
        self.layer3_threshold = int(config.get("layer3_threshold", 120000))
        self.target_tokens = int(config.get("target_tokens", 60000))
        
        # Byte size limits (Gemini has 100MB limit)
        self.max_request_bytes = int(config.get("max_request_bytes", 90 * 1024 * 1024))  # 90 MB
        self.target_request_bytes = int(config.get("target_request_bytes", 70 * 1024 * 1024))  # 70 MB
        
        # Tool result settings
        self.tool_result_min_size = int(config.get("tool_result_min_size", 500))
        self.tool_result_keep_last = int(config.get("tool_result_keep_last", 3))
        self.tool_result_max_inline_size = int(config.get("tool_result_max_inline_size", 5000))
        
        # Variable settings
        self.variable_min_size = int(config.get("variable_min_size", 200))
        self.assistant_keep_last = int(config.get("assistant_keep_last", 3))
        
        # Message settings
        self.archive_after_turns = int(config.get("archive_after_turns", 10))
        self.drop_after_turns = int(config.get("drop_after_turns", 50))
        self.keep_system_messages = bool(config.get("keep_system_messages", True))
        self.max_messages = int(config.get("max_messages", 0))  # 0 = disabled
        
        # Rate limiting
        self.min_time_between = float(config.get("min_time_between_compactions", 120.0))
        
        # Optional features
        self.enable_semantic_search = bool(config.get("enable_semantic_search", False))
        
        # Media handling settings
        self.deduplicate_media = bool(config.get("deduplicate_media", True))
        self.compact_media_after_user_message = bool(config.get("compact_media_after_user_message", False))
        self.compact_media_after_final_response = bool(config.get("compact_media_after_final_response", False))
        self.always_compact_media_keep_last = int(config.get("always_compact_media_keep_last", 0))
        
        # Media store settings (for storing inline base64 before compaction)
        self.store_media_before_compaction = bool(config.get("store_media_before_compaction", True))
        self.media_store_ttl_seconds = int(config.get("media_store_ttl_seconds", 86400 * 7))  # 7 days
        self.media_store_max_files = int(config.get("media_store_max_files", 500))
        
        # Storage paths (will be session-specific)
        self._storage_base = Path(config.get("storage_path", "data/context_engineer"))
        
        logger.info(
            f"ContextEngineerPlugin initialized: "
            f"thresholds=L1:{self.layer1_threshold}/L2:{self.layer2_threshold}/"
            f"L3:{self.layer3_threshold}, target={self.target_tokens}, "
            f"min_time_between={self.min_time_between}s, "
            f"deduplicate_media={self.deduplicate_media}, "
            f"compact_media_after_user_message={self.compact_media_after_user_message}, "
            f"compact_media_after_final_response={self.compact_media_after_final_response}, "
            f"always_compact_media_keep_last={self.always_compact_media_keep_last}, "
            f"max_messages={self.max_messages}"
        )
    
    def _cleanup_expired_sessions(self) -> None:
        """Remove expired session components based on TTL and max count."""
        current_time = time.time()
        
        # First: TTL-based cleanup
        expired = [
            sid for sid, components in self._session_components.items()
            if (current_time - components.get("last_accessed", 0)) > self._session_ttl_seconds
        ]
        for sid in expired:
            self.cleanup_session(sid)
        
        # Second: LRU eviction if still over limit
        if len(self._session_components) > self._max_tracked_sessions:
            # Sort by last_accessed, evict oldest
            sorted_sessions = sorted(
                self._session_components.items(),
                key=lambda x: x[1].get("last_accessed", 0)
            )
            evict_count = len(self._session_components) - self._max_tracked_sessions
            for sid, _ in sorted_sessions[:evict_count]:
                self.cleanup_session(sid)
        
        # Also cleanup _last_compaction_time
        stale_compaction = [
            sid for sid, ts in self._last_compaction_time.items()
            if (current_time - ts) > self._session_ttl_seconds
        ]
        for sid in stale_compaction:
            del self._last_compaction_time[sid]
    
    def _get_session_components(self, session_id: str) -> dict[str, Any]:
        """Get or create session-scoped components.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dict with tool_store, variable_manager, core_memory, archival_memory
        """
        if session_id in self._session_components:
            # Update last accessed time
            self._session_components[session_id]["last_accessed"] = time.time()
            return self._session_components[session_id]
        
        if session_id not in self._session_components:
            # Create session-specific storage paths
            session_path = self._storage_base / session_id
            session_path.mkdir(parents=True, exist_ok=True)
            
            # Initialize components
            tool_store = ToolResultStore(session_path / "tool_results.db")
            variable_manager = VariableManager(
                min_content_tokens=self.variable_min_size,
                storage_path=session_path / "variables.json"
            )
            core_memory = CoreMemory(storage_path=session_path / "core_memory.json")
            archival_memory = ArchivalMemory(
                session_path / "archive.db",
                session_id=session_id,
                enable_semantic_search=self.enable_semantic_search,
                vector_store_path=session_path / "vectors" if self.enable_semantic_search else None
            )
            
            # Initialize media store for inline media preservation
            media_store = None
            if self.store_media_before_compaction:
                media_store = MediaStore(
                    storage_path=session_path / "media",
                    ttl_seconds=self.media_store_ttl_seconds,
                    max_files=self.media_store_max_files
                )
            
            # Create compaction config
            compaction_config = CompactionConfig(
                layer1_threshold=self.layer1_threshold,
                layer2_threshold=self.layer2_threshold,
                layer3_threshold=self.layer3_threshold,
                target_tokens=self.target_tokens,
                max_request_bytes=self.max_request_bytes,
                target_request_bytes=self.target_request_bytes,
                tool_result_min_size=self.tool_result_min_size,
                tool_result_keep_last=self.tool_result_keep_last,
                tool_result_max_inline_size=self.tool_result_max_inline_size,
                variable_min_size=self.variable_min_size,
                assistant_keep_last=self.assistant_keep_last,
                archive_after_turns=self.archive_after_turns,
                drop_after_turns=self.drop_after_turns,
                keep_system_messages=self.keep_system_messages,
                max_messages=self.max_messages,
                deduplicate_media=self.deduplicate_media,
                compact_media_after_user_message=self.compact_media_after_user_message,
                compact_media_after_final_response=self.compact_media_after_final_response,
                always_compact_media_keep_last=self.always_compact_media_keep_last,
                store_media_before_compaction=self.store_media_before_compaction,
                media_store_ttl_seconds=self.media_store_ttl_seconds,
                media_store_max_files=self.media_store_max_files
            )
            
            logger.info(
                f"[ContextEngineer] Created CompactionConfig for session {session_id}: "
                f"max_messages={compaction_config.max_messages}, "
                f"always_compact_media_keep_last={compaction_config.always_compact_media_keep_last}"
            )
            
            # Create strategy
            strategy = LayeredCompactionStrategy(
                tool_store=tool_store,
                variable_manager=variable_manager,
                core_memory=core_memory,
                archival_memory=archival_memory,
                config=compaction_config,
                media_store=media_store
            )
            
            self._session_components[session_id] = {
                "tool_store": tool_store,
                "variable_manager": variable_manager,
                "core_memory": core_memory,
                "archival_memory": archival_memory,
                "media_store": media_store,
                "strategy": strategy,
                "last_accessed": time.time()
            }
            
            # Cleanup expired sessions periodically
            self._cleanup_expired_sessions()
            
            logger.debug(f"Created session components for {session_id}")
        
        return self._session_components[session_id]
    
    async def engineer_context(self, context: HookContext) -> HookResult:
        """Apply context engineering to messages.
        
        Handler for 'engineer_context' hook defined in schema.yaml.
        
        Args:
            context: Hook context with messages and metadata
            
        Returns:
            HookResult with engineered messages
        """
        try:
            messages = context.messages or []
            
            if not messages:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={"reason": "no_messages"}
                )
            
            session_id = context.session_id or "default"
            
            # Convert ChatMessage objects to dicts
            messages_as_dicts = []
            for msg in messages:
                if isinstance(msg, ChatMessage):
                    messages_as_dicts.append(msg.model_dump(exclude_none=True))
                else:
                    messages_as_dicts.append(msg)
            
            # Get session components (needed for token/byte estimation and compaction)
            components = self._get_session_components(session_id)
            strategy: LayeredCompactionStrategy = components["strategy"]
            
            # Get actual or estimated token usage (prefer actual from usage_tracker)
            current_tokens = self._get_actual_or_estimated_tokens(context, messages_as_dicts, strategy)
            
            # Estimate request bytes using strategy's method (for Gemini 100MB limit check)
            request_bytes = strategy._estimate_request_bytes(messages_as_dicts)
            bytes_exceeded = request_bytes > self.max_request_bytes
            
            if bytes_exceeded:
                logger.warning(
                    f"[ContextEngineer] Session {session_id}: Request size "
                    f"{request_bytes / (1024*1024):.1f}MB exceeds "
                    f"{self.max_request_bytes / (1024*1024):.0f}MB limit - forcing compaction"
                )
            
            # Check if compaction needed
            is_manual = context.metadata.get("manual_trigger", False) if context.metadata else False
            
            # Determine trigger event for media compaction BEFORE early return check
            # This is a pre_llm_call hook, so the last message is what the user just sent
            trigger_event = None
            if messages_as_dicts:
                last_msg = messages_as_dicts[-1]
                last_role = last_msg.get("role", "")
                if last_role == "user":
                    trigger_event = "user_message"
                # Note: final_response is detected in post-hooks, not here
            
            # Check if event-based media compaction should run even below threshold
            event_media_compaction_needed = False
            if trigger_event == "user_message" and self.compact_media_after_user_message:
                event_media_compaction_needed = True
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: Event-based media compaction "
                    f"triggered by user_message (compact_media_after_user_message=true)"
                )
            
            # Check if always-compact-media is enabled (must run even below threshold)
            always_compact_media_enabled = self.always_compact_media_keep_last > 0
            
            # Skip only if: not manual, below token threshold, below byte limit, 
            # AND no event-based media compaction, AND always_compact_media disabled
            if not is_manual and current_tokens < self.layer1_threshold and not bytes_exceeded and not event_media_compaction_needed and not always_compact_media_enabled:
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: "
                    f"{current_tokens} tokens < {self.layer1_threshold} threshold, "
                    f"{request_bytes / (1024*1024):.1f}MB < {self.max_request_bytes / (1024*1024):.0f}MB limit, "
                    f"no event-based media compaction needed, skipping"
                )
                
                # Below threshold - no compaction needed, return unchanged
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        "reason": "below_threshold",
                        "current_tokens": current_tokens,
                        "threshold": self.layer1_threshold,
                        "request_bytes": request_bytes,
                        "max_request_bytes": self.max_request_bytes
                    }
                )
            
            # Rate limiting - but NOT if bytes exceeded (must compact to avoid API errors!)
            # Also NOT if event-based media compaction or always_compact_media is needed
            current_time = time.monotonic()
            last_compaction = self._last_compaction_time.get(session_id)
            
            if not is_manual and not bytes_exceeded and not event_media_compaction_needed and not always_compact_media_enabled and last_compaction:
                time_since = current_time - last_compaction
                if time_since < self.min_time_between:
                    logger.info(
                        f"[ContextEngineer] Session {session_id}: Rate limited - "
                        f"{time_since:.1f}s since last compaction "
                        f"(min: {self.min_time_between}s)"
                    )
                    return HookResult(
                        success=True,
                        modified=False,
                        context=context,
                        metadata={
                            "reason": "rate_limited",
                            "time_since_last": time_since,
                            "min_time_between": self.min_time_between
                        }
                    )
            
            # Force compaction if manually triggered, byte limit exceeded, event-based media compaction needed,
            # OR always_compact_media enabled
            # Byte limit MUST be enforced to avoid API errors (Gemini 100MB limit)
            force = is_manual or bytes_exceeded or event_media_compaction_needed or always_compact_media_enabled
            
            # Apply compaction with status updates
            result = None
            async with StatusScope(
                status_bus,
                "context_engineer",
                session_id,
                start_msg=f"Engineering context: {current_tokens} tokens (target: {self.target_tokens}){' [MANUAL]' if is_manual else ''}",
                end_msg="Context engineering completed"
            ):
                import asyncio
                await asyncio.sleep(0.01)  # Allow START message to be delivered
                
                result = await strategy.compact(
                    messages_as_dicts, 
                    current_tokens, 
                    force=force,
                    trigger_event=trigger_event,
                    session_id=session_id
                )
            
            # Update rate limit tracker
            self._last_compaction_time[session_id] = current_time
            
            # Track in history for web UI - ONLY if something was actually compacted
            # Skip history entry if nothing happened to avoid noise
            something_compacted = (
                result.tokens_saved > 0 or
                result.tool_results_stored > 0 or
                result.variables_created > 0 or
                result.messages_archived > 0 or
                result.messages_dropped > 0 or
                result.messages_pruned > 0 or  # Pre-Layer P (message count limit)
                result.media_deduplicated > 0 or
                result.media_compacted_after_event > 0 or
                result.media_always_compacted > 0  # Always-compact media (Pre-Layer M)
            )
            
            # Invalidate usage tracker data for this session if something was compacted
            # This prevents subsequent hooks (e.g., context_summarizer) from using stale
            # token counts that don't reflect the optimized message list
            if something_compacted:
                self._invalidate_usage_tracker_session(context, session_id, "context_engineer_compaction")
            
            if self.stats_history is not None and something_compacted:
                self.stats_history.append({
                    "timestamp": time.time(),
                    "session_id": session_id,
                    "agent_name": context.agent_name or "unknown",
                    "original_tokens": result.original_tokens,
                    "final_tokens": result.final_tokens,
                    "tokens_saved": result.tokens_saved,
                    "reduction_percent": result.reduction_percent,
                    "layers_applied": result.layers_applied,
                    "tool_results_stored": result.tool_results_stored,
                    "variables_created": result.variables_created,
                    "messages_archived": result.messages_archived,
                    "messages_dropped": result.messages_dropped,
                    "messages_pruned": result.messages_pruned,  # Pre-Layer P
                    "media_deduplicated": result.media_deduplicated,
                    "media_compacted_after_event": result.media_compacted_after_event,
                    "media_bytes_saved": result.media_bytes_saved
                })
                
                # Save history to disk (support both sync and async callbacks)
                if self.history_callback is not None:
                    if asyncio.iscoroutinefunction(self.history_callback):
                        await self.history_callback()
                    else:
                        # Run sync callback in thread pool to avoid blocking
                        await asyncio.to_thread(self.history_callback)
            
            # Convert modified messages to ChatMessage objects
            modified_messages = result.modified_messages
            new_messages = [
                ChatMessage(**msg) if isinstance(msg, dict) else msg
                for msg in modified_messages
            ]
            
            # Inject restoration context (info about how to retrieve stored data)
            strategy: LayeredCompactionStrategy = components["strategy"]
            restoration_context = await strategy.get_restoration_context()
            
            if restoration_context:
                # Find position after last system message to insert restoration context
                # This preserves the agent's system prompt while adding our context
                insert_pos = 0
                for i, msg in enumerate(new_messages):
                    msg_role = msg.role if hasattr(msg, 'role') else msg.get('role')
                    if msg_role == 'system':
                        insert_pos = i + 1
                    else:
                        break  # Stop at first non-system message
                
                restoration_msg = ChatMessage(
                    role="system",
                    content=restoration_context
                )
                new_messages.insert(insert_pos, restoration_msg)
            
            # Build modified context with all fields
            modified_context = HookContext(
                hook_type=context.hook_type,
                request_id=context.request_id,
                session_id=context.session_id,
                agent=context.agent,
                agent_name=context.agent_name,
                messages=new_messages,
                llm_response=context.llm_response,
                tool_call=context.tool_call,
                tool_result=context.tool_result,
                output=context.output,
                metadata=context.metadata,
                step=context.step,
                llm=context.llm,
                cancellation_token=context.cancellation_token
            )
            
            logger.info(
                f"[ContextEngineer] Session {session_id}: "
                f"{result.original_tokens} -> {result.final_tokens} tokens "
                f"({result.reduction_percent:.1f}% reduction), "
                f"layers: {result.layers_applied}"
                + (f", media_dedup: {result.media_deduplicated}" if result.media_deduplicated > 0 else "")
                + (f", media_event: {result.media_compacted_after_event}" if result.media_compacted_after_event > 0 else "")
            )
            
            # NOTE: Session persistence is now handled automatically by HookIntegrationManager
            # when we return HookResult with modified=True. The _auto_sync_session_messages()
            # method filters system messages correctly (keeping archived_ref types).
            # The explicit set_compacted_messages() call below is kept for backwards compatibility
            # and as a safety net, but is no longer strictly required.
            if context.agent and hasattr(context.agent, '_session_tracker'):
                # Filter out the ORIGINAL system message (agent's system prompt) for persistence.
                # The system prompt is rebuilt each turn from config.
                # BUT keep archived_ref system messages - those are compacted conversation!
                import json
                conversation_msgs = []
                for msg in new_messages:
                    if msg.role == 'system':
                        # Check if this is an archived_ref (keep) or original system prompt (skip)
                        content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
                        if isinstance(content, str):
                            try:
                                parsed = json.loads(content)
                                if isinstance(parsed, dict) and parsed.get("type") == "archived_ref":
                                    # Keep archived references
                                    conversation_msgs.append(msg)
                                    continue
                            except (json.JSONDecodeError, TypeError):
                                pass
                        # Skip original system prompt
                        continue
                    conversation_msgs.append(msg)
                
                context.agent._session_tracker.set_compacted_messages(
                    session_id, conversation_msgs
                )
                logger.debug(
                    f"[ContextEngineer] Persisted {len(conversation_msgs)} compacted messages "
                    f"for session {session_id}"
                )
            
            # CRITICAL: modified=True signals hook registry to use our modified context
            return HookResult(
                success=True,
                modified=True,  # Always True when we made changes
                context=modified_context,
                metadata={
                    "original_tokens": result.original_tokens,
                    "final_tokens": result.final_tokens,
                    "tokens_saved": result.tokens_saved,
                    "reduction_percent": result.reduction_percent,
                    "layers_applied": result.layers_applied,
                    "tool_results_stored": result.tool_results_stored,
                    "variables_created": result.variables_created,
                    "messages_archived": result.messages_archived,
                    "messages_dropped": result.messages_dropped,
                    "messages_pruned": result.messages_pruned,
                    "media_deduplicated": result.media_deduplicated,
                    "media_compacted_after_event": result.media_compacted_after_event
                }
            )
            
        except Exception as e:
            logger.exception(f"[ContextEngineer] Error during context engineering: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    def _get_actual_or_estimated_tokens(
        self, 
        context: HookContext, 
        messages: list[dict[str, Any]],
        strategy: LayeredCompactionStrategy
    ) -> int:
        """Get actual token count from last LLM response or estimate from messages.

        Uses the MAXIMUM of:
        1. Actual prompt_tokens from last LLM response (via context_usage_tracker)
        2. Estimated tokens from current messages (via strategy)

        This ensures we trigger compaction if either metric exceeds threshold,
        preventing context overflow.

        Args:
            context: Hook context with session_id
            messages: Current message list
            strategy: Compaction strategy for token estimation

        Returns:
            Maximum of actual or estimated token count
        """
        estimated_tokens = strategy._estimate_messages_tokens(messages)

        # Include tool definition tokens in estimation (they consume context window)
        if context.agent and hasattr(context.agent, '_current_tools_schema'):
            tools_schema = context.agent._current_tools_schema
            if tools_schema and isinstance(tools_schema, list):
                from agent_system.llm.token_utils import estimate_tools_token_count
                tool_tokens = estimate_tools_token_count(tools_schema)
                estimated_tokens += tool_tokens
                logger.debug(
                    f"[ContextEngineer] Added {tool_tokens} tool definition tokens "
                    f"({len(tools_schema)} tools)"
                )

        actual_tokens = 0

        # Try to get actual tokens from context_usage_tracker's latest snapshot FOR THIS SESSION
        # This uses the previous LLM call's token count as baseline - if it was already high,
        # the next call will be at least as large (probably larger with new messages)
        try:
            # Access the plugin registry via agent's system_config
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'mcp_registry') and system_config.mcp_registry:
                    registry = system_config.mcp_registry

                    # Get context_usage_tracker plugin
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        tracker = usage_tracker_plugin.tracker

                        # Get latest snapshot FOR THIS SESSION (not global _latest_snapshot!)
                        # This filters by session_id to avoid interference from sub-agents
                        latest = tracker.get_latest(session_id=context.session_id)
                        if latest:
                            # Check if data is stale (context was optimized since last LLM call)
                            # Stale data doesn't reflect current message list, so ignore it
                            if latest.get('is_stale'):
                                logger.debug(
                                    f"[ContextEngineer] Ignoring stale usage_tracker data for session "
                                    f"{context.session_id} (context was already optimized)"
                                )
                            else:
                                actual_tokens = latest.get('prompt_tokens', 0)
                                logger.debug(
                                    f"[ContextEngineer] Got actual tokens from usage_tracker: {actual_tokens} "
                                    f"(estimated: {estimated_tokens})"
                                )
        except Exception as e:
            logger.debug(f"[ContextEngineer] Could not get actual tokens from usage_tracker: {e}")

        # Return the MAXIMUM to ensure we trigger on either metric
        max_tokens = max(actual_tokens, estimated_tokens)

        if actual_tokens > 0 and estimated_tokens > 0:
            logger.debug(
                f"[ContextEngineer] Session {context.session_id}: Using max tokens - "
                f"actual={actual_tokens}, estimated={estimated_tokens}, using={max_tokens}"
            )

        return max_tokens
    
    def _invalidate_usage_tracker_session(
        self, 
        context: HookContext, 
        session_id: str, 
        reason: str
    ) -> None:
        """Mark usage tracker data as stale after context optimization.
        
        This prevents subsequent hooks from using outdated token counts
        that don't reflect the optimized message list.
        
        Args:
            context: Hook context with agent reference
            session_id: Session to invalidate
            reason: Reason for invalidation (for logging)
        """
        try:
            if context.agent and hasattr(context.agent, 'system_config'):
                system_config = context.agent.system_config
                if hasattr(system_config, 'mcp_registry') and system_config.mcp_registry:
                    registry = system_config.mcp_registry
                    usage_tracker_plugin = registry.get_server('context_usage_tracker')
                    if usage_tracker_plugin and hasattr(usage_tracker_plugin, 'tracker'):
                        usage_tracker_plugin.tracker.invalidate_session(session_id, reason)
        except Exception as e:
            logger.debug(f"[ContextEngineer] Could not invalidate usage_tracker session: {e}")
    
    # === MCP Tool Handlers ===
    # These are called by the MCP server when tools are invoked
    
    def get_tool_handlers(self) -> dict[str, Any]:
        """Get tool handler functions for MCP server.
        
        Returns:
            Dict mapping tool names to handler functions
        """
        return {
            "recall": self._handle_recall,
            "store_fact": self._handle_store_fact,
            # Legacy handlers kept for backward compatibility but no longer exposed as tools
            "get_variable": self._handle_get_variable,
            "get_tool_result": self._handle_get_tool_result,
            "stats": self._handle_stats,
            "restore_multimodal": self._handle_restore_multimodal
        }
    
    def _detect_recall_type(self, query: str) -> str:
        """Detect what type of recall is needed based on query pattern.
        
        Returns one of: 'variable', 'tool_result', 'media', 'archive'
        """
        import re
        from pathlib import Path
        
        query_stripped = query.strip()
        
        # Pattern 1: Variable reference ($VAR_N or VAR_N)
        if re.match(r'^\$?VAR_\d+$', query_stripped, re.IGNORECASE):
            return 'variable'
        
        # Pattern 2: Tool result reference (TR_xxx, $TR_xxx, or call_xxx).
        # Accept the leading "$" too — agents often conflate TR_ refs with
        # variable syntax (e.g. "$TR_BFCDDCA04A") and would otherwise get
        # misrouted into the variable handler.
        if (re.match(r'^\$?TR_[a-zA-Z0-9_]+$', query_stripped)
                or re.match(r'^call_[a-zA-Z0-9_]+$', query_stripped)):
            return 'tool_result'
        
        # Pattern 3: Looks like a hex hash (8+ hex chars)
        if re.match(r'^[a-f0-9]{8,}$', query_stripped, re.IGNORECASE):
            return 'tool_result'
        
        # Pattern 4: File path with media extension
        media_extensions = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac',
                          '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp',
                          '.mp4', '.webm', '.avi', '.mov'}
        
        # Check if it looks like a path
        if '/' in query_stripped or '\\' in query_stripped or query_stripped.startswith('data/'):
            suffix = Path(query_stripped).suffix.lower()
            if suffix in media_extensions:
                return 'media'
        
        # Default: archive search
        return 'archive'
    
    async def _handle_recall(
        self,
        query: str,
        mode: str = "auto",
        limit: int = 5,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle unified recall tool - auto-detects and retrieves any stored content.
        
        Supports:
        - Archived messages (semantic/text search)
        - Variables ($VAR_N)
        - Tool results (TR_xxx or hash)
        - Media files (file paths)
        
        Args:
            query: Search query, variable name, reference, or file path
            mode: Force specific mode or 'auto' to detect
            limit: Max results for archive search
            session_id: Session ID
            
        Returns:
            Retrieved content based on detected/specified mode
        """
        
        # Determine recall type
        if mode == "auto":
            recall_type = self._detect_recall_type(query)
        else:
            recall_type = mode
        
        # Handle each type
        if recall_type == 'variable':
            # Normalize variable name
            var_name = query.strip().upper()
            if not var_name.startswith('$'):
                var_name = '$' + var_name
            
            return await self._handle_get_variable(
                variable_name=var_name,
                session_id=session_id,
                mode="preview"
            )
        
        elif recall_type == 'tool_result':
            # Explicit TR_xxx lookup → return the FULL stored result. The
            # 500-char preview default was for archive browsing; when the
            # agent has a concrete reference it wants the data back, not a
            # teaser (otherwise it loops calling the original tool again).
            ref = query.strip()
            if ref.startswith("$"):
                ref = ref[1:]
            return await self._handle_get_tool_result(
                reference=ref,
                session_id=session_id,
                mode="full",
            )
        
        elif recall_type == 'media':
            return await self._handle_restore_multimodal(
                path=query.strip(),
                session_id=session_id
            )
        
        elif recall_type == 'core_memory':
            # Search only core memory facts
            components = self._get_session_components(session_id)
            core_memory: CoreMemory = components["core_memory"]
            query_lower = query.lower()
            matching_facts = [
                {
                    "content": f.content,
                    "category": f.category,
                    "importance": f.importance,
                    "created_at": f.created_at.isoformat()
                }
                for f in core_memory.facts
                if query_lower in f.content.lower()
            ]
            return {
                "recall_type": "core_memory",
                "query": query,
                "core_memory_facts": matching_facts,
                "core_memory_total": len(matching_facts),
                "hint": "No matching facts found. Facts are stored via store_fact tool." if not matching_facts else None
            }
        
        else:  # archive search + core memory fallback
            components = self._get_session_components(session_id)
            archival: ArchivalMemory = components["archival_memory"]
            core_memory: CoreMemory = components["core_memory"]
            
            results = archival.search(query, limit=limit)
            
            # Also search core memory facts (store_fact targets)
            query_lower = query.lower()
            matching_facts = [
                {
                    "content": f.content,
                    "category": f.category,
                    "importance": f.importance,
                    "created_at": f.created_at.isoformat()
                }
                for f in core_memory.facts
                if query_lower in f.content.lower()
            ]
            
            archive_results = [
                {
                    "id": r.id,
                    "role": r.role,
                    "summary": r.summary,
                    "content_preview": r.content[:500] + "..." if len(r.content) > 500 else r.content,
                    "timestamp": r.timestamp.isoformat()
                }
                for r in results
            ]
            
            return {
                "recall_type": "archive",
                "query": query,
                "results": archive_results,
                "total_found": len(archive_results),
                "core_memory_facts": matching_facts if matching_facts else None,
                "core_memory_total": len(matching_facts) if matching_facts else 0,
                "hint": "Use recall(query='$VAR_N') for variables, recall(query='TR_xxx') for tool results, or recall(query='/path/to/file.wav') for media" if not archive_results and not matching_facts else None
            }
    
    async def _handle_store_fact(
        self,
        fact: str,
        category: str = "facts",
        importance: float = 0.5,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle store_fact tool - add to core memory.
        
        Args:
            fact: The fact to store
            category: Category for organization
            importance: Importance score
            session_id: Session ID
            
        Returns:
            Result with fact ID
        """
        components = self._get_session_components(session_id)
        core_memory: CoreMemory = components["core_memory"]
        
        fact_id = await core_memory.add_fact(fact, category=category, importance=importance)
        
        return {
            "success": True,
            "fact_id": fact_id,
            "category": category,
            "importance": importance,
            "total_facts": len(core_memory.facts)
        }
    
    async def _handle_get_variable(
        self,
        variable_name: str,
        session_id: str = "default",
        mode: str = "preview",
        offset: int = 0,
        limit: int = 1000,
        search: str | None = None,
        context_chars: int = 150
    ) -> dict[str, Any]:
        """Handle get_variable tool - retrieve stored variable with pagination.
        
        Args:
            variable_name: Variable name (e.g., $VAR_1)
            session_id: Session ID
            mode: Retrieval mode (preview, chunk, search, full)
            offset: Start position for chunk mode
            limit: Max chars for chunk mode (max 10000 per request)
            search: Search query for search mode
            context_chars: Context around search matches
            
        Returns:
            Variable content (possibly truncated) or error
        """
        # Validate pagination limit
        max_limit = 5000
        if limit > max_limit:
            return {
                "found": False,
                "variable_name": variable_name,
                "error": f"Limit {limit} exceeds maximum allowed {max_limit}. Use multiple requests with offset to retrieve large content."
            }
        
        components = self._get_session_components(session_id)
        variable_manager: VariableManager = components["variable_manager"]
        
        entry = variable_manager.get_variable(variable_name)
        
        if not entry:
            return {
                "found": False,
                "variable_name": variable_name,
                "error": f"Variable {variable_name} not found"
            }
        
        content = entry.content
        total_chars = len(content)
        
        # Apply mode-specific content extraction
        if mode == "preview":
            # Return first ~500 chars with truncation indicator
            preview_limit = 500
            extracted = content[:preview_limit]
            truncated = total_chars > preview_limit
            return {
                "found": True,
                "variable_name": variable_name,
                "mode": "preview",
                "content": extracted,
                "truncated": truncated,
                "total_chars": total_chars,
                "returned_chars": len(extracted),
                "content_type": entry.content_type,
                "hint": "Use mode='chunk' with offset/limit or mode='search' to access more content" if truncated else None
            }
        
        elif mode == "chunk":
            # Paginated access
            extracted = content[offset:offset + limit]
            has_more = (offset + limit) < total_chars
            return {
                "found": True,
                "variable_name": variable_name,
                "mode": "chunk",
                "content": extracted,
                "offset": offset,
                "limit": limit,
                "returned_chars": len(extracted),
                "total_chars": total_chars,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
                "content_type": entry.content_type
            }
        
        elif mode == "search":
            # Search within content
            if not search:
                return {
                    "found": True,
                    "variable_name": variable_name,
                    "mode": "search",
                    "error": "search parameter required for mode='search'"
                }
            
            matches = []
            search_lower = search.lower()
            content_lower = content.lower()
            pos = 0
            
            while len(matches) < 10:  # Limit to 10 matches
                idx = content_lower.find(search_lower, pos)
                if idx == -1:
                    break
                
                # Extract context around match
                start = max(0, idx - context_chars)
                end = min(total_chars, idx + len(search) + context_chars)
                snippet = content[start:end]
                
                # Add ellipsis indicators
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < total_chars else ""
                
                matches.append({
                    "position": idx,
                    "snippet": f"{prefix}{snippet}{suffix}"
                })
                pos = idx + 1
            
            return {
                "found": True,
                "variable_name": variable_name,
                "mode": "search",
                "query": search,
                "match_count": len(matches),
                "matches": matches,
                "total_chars": total_chars,
                "content_type": entry.content_type,
                "hint": "Use mode='chunk' with offset near match position for more context" if matches else None
            }
        
        else:  # mode == "full"
            # Return everything (use sparingly!)
            return {
                "found": True,
                "variable_name": variable_name,
                "mode": "full",
                "content": content,
                "total_chars": total_chars,
                "content_type": entry.content_type,
                "token_count": entry.token_count,
                "created_at": entry.created_at.isoformat(),
                "warning": "Full content returned - consider using preview/chunk/search to save tokens"
            }
    
    async def _handle_get_tool_result(
        self,
        reference: str,
        session_id: str = "default",
        mode: str = "preview",
        offset: int = 0,
        limit: int = 1000,
        search: str | None = None,
        context_chars: int = 150
    ) -> dict[str, Any]:
        """Handle get_tool_result tool - retrieve stored tool output with pagination.
        
        Args:
            reference: Reference ID or hash
            session_id: Session ID
            mode: Retrieval mode (preview, chunk, search, full)
            offset: Start position for chunk mode
            limit: Max chars for chunk mode (max 10000 per request)
            search: Search query for search mode
            context_chars: Context around search matches
            
        Returns:
            Tool result content (possibly truncated) or error
        """
        # Validate pagination limit
        max_limit = 5000
        if limit > max_limit:
            return {
                "found": False,
                "reference": reference,
                "error": f"Limit {limit} exceeds maximum allowed {max_limit}. Use multiple requests with offset to retrieve large content."
            }
        
        components = self._get_session_components(session_id)
        tool_store: ToolResultStore = components["tool_store"]
        
        # Try by ID first, then by hash
        entry = tool_store.retrieve(reference)
        if not entry:
            entry = tool_store.retrieve_by_hash(reference)
        
        if not entry:
            return {
                "found": False,
                "reference": reference,
                "error": f"Tool result with reference '{reference}' not found"
            }
        
        content = entry.content
        total_chars = len(content)
        
        # Apply mode-specific content extraction
        if mode == "preview":
            preview_limit = 500
            extracted = content[:preview_limit]
            truncated = total_chars > preview_limit
            return {
                "found": True,
                "reference": reference,
                "tool_name": entry.tool_name,
                "mode": "preview",
                "content": extracted,
                "truncated": truncated,
                "total_chars": total_chars,
                "returned_chars": len(extracted),
                "hint": "Use mode='chunk' with offset/limit or mode='search' to access more content" if truncated else None
            }
        
        elif mode == "chunk":
            extracted = content[offset:offset + limit]
            has_more = (offset + limit) < total_chars
            return {
                "found": True,
                "reference": reference,
                "tool_name": entry.tool_name,
                "mode": "chunk",
                "content": extracted,
                "offset": offset,
                "limit": limit,
                "returned_chars": len(extracted),
                "total_chars": total_chars,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None
            }
        
        elif mode == "search":
            if not search:
                return {
                    "found": True,
                    "reference": reference,
                    "tool_name": entry.tool_name,
                    "mode": "search",
                    "error": "search parameter required for mode='search'"
                }
            
            matches = []
            search_lower = search.lower()
            content_lower = content.lower()
            pos = 0
            
            while len(matches) < 10:
                idx = content_lower.find(search_lower, pos)
                if idx == -1:
                    break
                
                start = max(0, idx - context_chars)
                end = min(total_chars, idx + len(search) + context_chars)
                snippet = content[start:end]
                
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < total_chars else ""
                
                matches.append({
                    "position": idx,
                    "snippet": f"{prefix}{snippet}{suffix}"
                })
                pos = idx + 1
            
            return {
                "found": True,
                "reference": reference,
                "tool_name": entry.tool_name,
                "mode": "search",
                "query": search,
                "match_count": len(matches),
                "matches": matches,
                "total_chars": total_chars,
                "hint": "Use mode='chunk' with offset near match position for more context" if matches else None
            }
        
        else:  # mode == "full"
            return {
                "found": True,
                "reference": reference,
                "tool_name": entry.tool_name,
                "mode": "full",
                "content": content,
                "total_chars": total_chars,
                "token_count": entry.token_count,
                "stored_at": entry.timestamp.isoformat(),
                "warning": "Full content returned - consider using preview/chunk/search to save tokens"
            }
    
    async def _handle_restore_multimodal(
        self,
        path: str,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle restore_multimodal tool - reload compacted audio/image/video.
        
        This tool allows the LLM to restore previously compacted multimodal
        content back into the conversation. The file will be marked for
        re-injection on the next LLM call.
        
        Args:
            path: Full file path of the multimodal content to restore
            session_id: Session ID
            
        Returns:
            Status and file info, or error if file not found
        """
        from pathlib import Path
        
        file_path = Path(path)
        
        # Validate file exists
        if not file_path.exists():
            return {
                "status": "error",
                "error": f"File not found: {path}",
                "hint": "The file may have been moved, deleted, or the path is incorrect."
            }
        
        # Get file info
        stat = file_path.stat()
        size_bytes = stat.st_size
        size_mb = size_bytes / (1024 * 1024)
        
        # Estimate token cost
        # Base64 encoding adds ~33% overhead, then ~4 chars per token
        estimated_tokens = int(size_bytes * 0.33)
        
        # Determine type from extension
        suffix = file_path.suffix.lower()
        if suffix in ('.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac'):
            content_type = "audio"
            mime_type = {
                '.wav': 'audio/wav',
                '.mp3': 'audio/mpeg',
                '.ogg': 'audio/ogg',
                '.flac': 'audio/flac',
                '.m4a': 'audio/mp4',
                '.aac': 'audio/aac'
            }.get(suffix, 'audio/wav')
        elif suffix in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'):
            content_type = "image"
            mime_type = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp'
            }.get(suffix, 'image/png')
        elif suffix in ('.mp4', '.webm', '.avi', '.mov'):
            content_type = "video"
            mime_type = {
                '.mp4': 'video/mp4',
                '.webm': 'video/webm',
                '.avi': 'video/avi',
                '.mov': 'video/quicktime'
            }.get(suffix, 'video/mp4')
        else:
            return {
                "status": "error",
                "error": f"Unsupported file type: {suffix}",
                "hint": "Supported types: audio (wav, mp3, ogg, flac, m4a, aac), image (png, jpg, gif, webp, bmp), video (mp4, webm, avi, mov)"
            }
        
        # Return multimodal content for injection
        # The _multimodal_content key will be picked up by tool execution
        return {
            "status": "success",
            "message": f"File will be loaded in the next response. Estimated cost: ~{estimated_tokens:,} tokens",
            "file_info": {
                "path": str(file_path),
                "name": file_path.name,
                "type": content_type,
                "mime_type": mime_type,
                "size_mb": round(size_mb, 2),
                "estimated_tokens": estimated_tokens
            },
            "_multimodal_content": [{
                "type": content_type,
                "path": str(file_path),
                "mime_type": mime_type,
                "description": f"Restored {content_type}: {file_path.name}"
            }]
        }
    
    async def _handle_stats(
        self,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle stats tool - get context engineering statistics.
        
        Args:
            session_id: Session ID
            
        Returns:
            Statistics about stored data
        """
        components = self._get_session_components(session_id)
        
        tool_store: ToolResultStore = components["tool_store"]
        variable_manager: VariableManager = components["variable_manager"]
        core_memory: CoreMemory = components["core_memory"]
        archival: ArchivalMemory = components["archival_memory"]
        
        # Aggregate media compaction stats from history for this session
        media_deduplicated = 0
        media_compacted = 0
        if self.stats_history:
            for event in self.stats_history:
                if event.get("session_id") == session_id:
                    media_deduplicated += event.get("media_deduplicated", 0)
                    media_compacted += event.get("media_compacted_after_event", 0)
        
        return {
            "tool_results": tool_store.get_stats(),
            "variables": variable_manager.get_stats(),
            "core_memory": {
                "facts": len(core_memory.facts),
                "facts_count": len(core_memory.facts),  # Added for UI compatibility
                "categories": core_memory.get_categories(),
                "token_usage": core_memory.get_token_usage()
            },
            "archival_memory": archival.get_stats(),
            "media_deduplicated": media_deduplicated,
            "media_compacted": media_compacted
        }
    
    def cleanup_session(self, session_id: str) -> None:
        """Clean up session resources.
        
        Args:
            session_id: Session to clean up
        """
        if session_id in self._session_components:
            components = self._session_components[session_id]
            
            # Close database connections
            if "archival_memory" in components:
                components["archival_memory"].close()
            if "tool_store" in components:
                components["tool_store"].close()
            
            del self._session_components[session_id]
            
            logger.debug(f"Cleaned up session components for {session_id}")
