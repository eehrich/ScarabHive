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
from agent_system.llm.token_utils import estimate_content_tokens
from agent_system.mcp.status import StatusScope, status_bus

from .archival_memory import ArchivalMemory
from .compaction import CompactionConfig, LayeredCompactionStrategy
from .core_memory import CoreMemory
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
        
        # Session tracking
        self._last_compaction_time: dict[str, float] = {}
        self._session_components: dict[str, dict[str, Any]] = {}
        
        # Load config
        config = self.get_config()
        
        # Token thresholds
        self.layer1_threshold = int(config.get("layer1_threshold", 80000))
        self.layer2_threshold = int(config.get("layer2_threshold", 100000))
        self.layer3_threshold = int(config.get("layer3_threshold", 120000))
        self.target_tokens = int(config.get("target_tokens", 60000))
        
        # Tool result settings
        self.tool_result_min_size = int(config.get("tool_result_min_size", 500))
        self.tool_result_keep_last = int(config.get("tool_result_keep_last", 3))
        self.tool_result_max_inline_size = int(config.get("tool_result_max_inline_size", 5000))
        
        # Variable settings
        self.variable_min_size = int(config.get("variable_min_size", 200))
        
        # Message settings
        self.archive_after_turns = int(config.get("archive_after_turns", 10))
        self.drop_after_turns = int(config.get("drop_after_turns", 50))
        self.keep_system_messages = bool(config.get("keep_system_messages", True))
        
        # Rate limiting
        self.min_time_between = float(config.get("min_time_between_compactions", 120.0))
        
        # Optional features
        self.enable_semantic_search = bool(config.get("enable_semantic_search", False))
        
        # Storage paths (will be session-specific)
        self._storage_base = Path(config.get("storage_path", "data/context_engineer"))
        
        logger.info(
            f"ContextEngineerPlugin initialized: "
            f"thresholds=L1:{self.layer1_threshold}/L2:{self.layer2_threshold}/"
            f"L3:{self.layer3_threshold}, target={self.target_tokens}, "
            f"min_time_between={self.min_time_between}s"
        )
    
    def _get_session_components(self, session_id: str) -> dict[str, Any]:
        """Get or create session-scoped components.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Dict with tool_store, variable_manager, core_memory, archival_memory
        """
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
            
            # Create compaction config
            compaction_config = CompactionConfig(
                layer1_threshold=self.layer1_threshold,
                layer2_threshold=self.layer2_threshold,
                layer3_threshold=self.layer3_threshold,
                target_tokens=self.target_tokens,
                tool_result_min_size=self.tool_result_min_size,
                tool_result_keep_last=self.tool_result_keep_last,
                tool_result_max_inline_size=self.tool_result_max_inline_size,
                variable_min_size=self.variable_min_size,
                archive_after_turns=self.archive_after_turns,
                drop_after_turns=self.drop_after_turns,
                keep_system_messages=self.keep_system_messages
            )
            
            # Create strategy
            strategy = LayeredCompactionStrategy(
                tool_store=tool_store,
                variable_manager=variable_manager,
                core_memory=core_memory,
                archival_memory=archival_memory,
                config=compaction_config
            )
            
            self._session_components[session_id] = {
                "tool_store": tool_store,
                "variable_manager": variable_manager,
                "core_memory": core_memory,
                "archival_memory": archival_memory,
                "strategy": strategy
            }
            
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
            
            # Get actual or estimated token usage (prefer actual from usage_tracker)
            current_tokens = self._get_actual_or_estimated_tokens(context, messages_as_dicts)
            
            # Check if compaction needed
            is_manual = context.metadata.get("manual_trigger", False) if context.metadata else False
            
            if not is_manual and current_tokens < self.layer1_threshold:
                logger.debug(
                    f"[ContextEngineer] Session {session_id}: "
                    f"{current_tokens} tokens < {self.layer1_threshold} threshold, skipping"
                )
                
                # Below threshold - no compaction needed, return unchanged
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={
                        "reason": "below_threshold",
                        "current_tokens": current_tokens,
                        "threshold": self.layer1_threshold
                    }
                )
            
            # Rate limiting
            current_time = time.monotonic()
            last_compaction = self._last_compaction_time.get(session_id)
            
            if not is_manual and last_compaction:
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
            
            # Get session components
            components = self._get_session_components(session_id)
            strategy: LayeredCompactionStrategy = components["strategy"]
            
            # When manually triggered (via MCP tool), always bypass threshold checks
            # Hook execution respects thresholds (force=False)
            force = is_manual
            
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
                
                result = strategy.compact(messages_as_dicts, current_tokens, force=force)
            
            # Update rate limit tracker
            self._last_compaction_time[session_id] = current_time
            
            # Track in history for web UI
            if self.stats_history is not None:
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
                    "messages_dropped": result.messages_dropped
                })
                
                # Save history to disk
                if self.history_callback is not None:
                    self.history_callback()
            
            # Convert modified messages to ChatMessage objects
            modified_messages = result.modified_messages
            new_messages = [
                ChatMessage(**msg) if isinstance(msg, dict) else msg
                for msg in modified_messages
            ]
            
            # Inject restoration context (info about how to retrieve stored data)
            strategy: LayeredCompactionStrategy = components["strategy"]
            restoration_context = strategy.get_restoration_context()
            
            if restoration_context:
                # Prepend restoration context as system message
                restoration_msg = ChatMessage(
                    role="system",
                    content=restoration_context
                )
                new_messages.insert(0, restoration_msg)
            
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
                    "messages_dropped": result.messages_dropped
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
    
    def _estimate_total_tokens(self, messages: list[dict[str, Any]]) -> int:
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
            
            # Overhead for message structure
            total += 4
            
            # Tool calls
            if "tool_calls" in msg:
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    total += estimate_content_tokens(func.get("name", ""))
                    total += estimate_content_tokens(func.get("arguments", ""))
        
        return total
    
    def _get_actual_or_estimated_tokens(self, context: HookContext, messages: list[dict[str, Any]]) -> int:
        """Get actual token count from last LLM response or estimate from messages.

        Uses the MAXIMUM of:
        1. Actual prompt_tokens from last LLM response (via context_usage_tracker)
        2. Estimated tokens from current messages

        This ensures we trigger compaction if either metric exceeds threshold,
        preventing context overflow.

        Args:
            context: Hook context with session_id
            messages: Current message list

        Returns:
            Maximum of actual or estimated token count
        """
        estimated_tokens = self._estimate_total_tokens(messages)
        actual_tokens = 0

        # Try to get actual tokens from context_usage_tracker's latest snapshot
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

                        # Get latest snapshot for this session
                        if tracker._latest_snapshot and tracker._latest_snapshot.session_id == context.session_id:
                            actual_tokens = tracker._latest_snapshot.prompt_tokens
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
            "get_variable": self._handle_get_variable,
            "get_tool_result": self._handle_get_tool_result,
            "stats": self._handle_stats
        }
    
    async def _handle_recall(
        self,
        query: str,
        limit: int = 5,
        session_id: str = "default"
    ) -> dict[str, Any]:
        """Handle recall tool - search archived messages.
        
        Args:
            query: Search query
            limit: Max results
            session_id: Session ID
            
        Returns:
            Search results
        """
        components = self._get_session_components(session_id)
        archival: ArchivalMemory = components["archival_memory"]
        
        results = archival.search(query, limit=limit)
        
        return {
            "results": [
                {
                    "id": r.id,
                    "role": r.role,
                    "summary": r.summary,
                    "content_preview": r.content[:500] + "..." if len(r.content) > 500 else r.content,
                    "timestamp": r.timestamp.isoformat()
                }
                for r in results
            ],
            "total_found": len(results)
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
        
        fact_id = core_memory.add_fact(fact, category=category, importance=importance)
        
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
        
        return {
            "tool_results": tool_store.get_stats(),
            "variables": variable_manager.get_stats(),
            "core_memory": {
                "facts": len(core_memory.facts),
                "facts_count": len(core_memory.facts),  # Added for UI compatibility
                "categories": core_memory.get_categories(),
                "token_usage": core_memory.get_token_usage()
            },
            "archival_memory": archival.get_stats()
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
