"""Context Engineer MCP Server - Advanced context management tools.

Provides MCP tools for:
- list: Browse or filter what was moved out of the conversation
- read: Read one stored item by reference, bounded (also restores media)
- store_fact: Add important facts to core memory
- compact: Trigger compaction manually

Also implements pre_llm_call hook for automatic context engineering.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_system.hooks.plugin_hook import HookContext, HookResult, PluginHook
from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .compaction import RETRIEVAL_MARKER

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class ContextEngineerServer(SchemaBasedMCPServer, PluginHook):
    """Unified MCP server and hook for context engineering.

    Provides MCP tools for manual context management:
    - list: What is stored — refs and summaries, never bodies
    - read: One item by ref, bounded; a media path restores that file
    - store_fact: Add facts to persistent core memory
    - compact: Run a compaction now

    Implements pre_llm_call hook for automatic context compaction.
    """
    
    def __init__(
        self,
        name: str,
        system_config: AgentSystemConfig,
        mcp_config: MCPConfig
    ) -> None:
        """Initialize ContextEngineerServer.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        # Initialize MCP server
        SchemaBasedMCPServer.__init__(self, name, system_config, mcp_config)
        
        # Initialize hook
        hook_config = getattr(mcp_config, "hook_config", {})
        PluginHook.__init__(self, name, config=hook_config)
        
        # Load configuration
        config_dict = mcp_config.config if hasattr(mcp_config, "config") else {}
        
        # Token thresholds
        self.layer1_threshold = int(config_dict.get("layer1_threshold", 80000))
        self.layer2_threshold = int(config_dict.get("layer2_threshold", 100000))
        self.layer3_threshold = int(config_dict.get("layer3_threshold", 120000))
        self.target_tokens = int(config_dict.get("target_tokens", 60000))
        # Cache-Hysterese: Mindestabstand zwischen prefix-brechenden
        # Kompaktionen (docs/prompt_cache_design.md par. 3.5)
        self.min_time_between_compactions = float(config_dict.get("min_time_between_compactions", 120.0))
        
        # ============================================================
        # CONFIG LOADING - WICHTIG für neue Parameter:
        # 1. Hier aus config_dict laden (plugins.yaml Werte)
        # 2. Unten zu self._hooks_impl syncen
        # 3. In schema.yaml unter 'config:' Sektion hinzufügen (für Defaults)
        # 4. In hooks.py __init__ auch laden (für get_config() Fallback)
        # ============================================================
        
        # Feature settings
        self.tool_result_min_size = int(config_dict.get("tool_result_min_size", 500))
        self.tool_result_keep_last = int(config_dict.get("tool_result_keep_last", 3))
        self.tool_result_max_inline_size = int(config_dict.get("tool_result_max_inline_size", 5000))
        self.archive_after_turns = int(config_dict.get("archive_after_turns", 10))
        self.enable_semantic_search = bool(config_dict.get("enable_semantic_search", False))
        
        # Media handling settings
        self.deduplicate_media = bool(config_dict.get("deduplicate_media", True))
        self.compact_media_after_user_message = bool(config_dict.get("compact_media_after_user_message", False))
        self.compact_media_after_final_response = bool(config_dict.get("compact_media_after_final_response", False))
        self.always_compact_media_keep_last = int(config_dict.get("always_compact_media_keep_last", 0))
        
        # Pre-Layer P: Hard message limit
        self.max_messages = int(config_dict.get("max_messages", 0))
        self.max_messages_headroom = int(config_dict.get("max_messages_headroom", 50))
        
        # Media store settings
        self.store_media_before_compaction = bool(config_dict.get("store_media_before_compaction", True))
        self.media_store_ttl_seconds = int(config_dict.get("media_store_ttl_seconds", 86400 * 7))  # 7 days
        self.media_store_max_files = int(config_dict.get("media_store_max_files", 500))
        
        # Web UI history tracking - load from persistent storage
        self.stats_history: list[dict[str, Any]] = []
        self._history_file = Path("data/context_engineer/history.json")
        self._load_history()
        
        # Import and instantiate the hook implementation
        from plugins.context_engineer.hooks import ContextEngineerPlugin
        
        plugin_dir = Path(__file__).parent
        self._hooks_impl = ContextEngineerPlugin(
            plugin_dir,
            stats_history=self.stats_history,
            history_callback=self._save_history_sync  # Sync callback, runs in thread pool
        )
        
        # Sync config to hooks implementation
        # WICHTIG: Neue Parameter hier hinzufügen! (siehe Kommentar oben)
        self._hooks_impl.layer1_threshold = self.layer1_threshold
        self._hooks_impl.layer2_threshold = self.layer2_threshold
        self._hooks_impl.layer3_threshold = self.layer3_threshold
        self._hooks_impl.target_tokens = self.target_tokens
        self._hooks_impl.min_time_between = self.min_time_between_compactions
        self._hooks_impl.tool_result_min_size = self.tool_result_min_size
        self._hooks_impl.tool_result_keep_last = self.tool_result_keep_last
        self._hooks_impl.tool_result_max_inline_size = self.tool_result_max_inline_size
        self._hooks_impl.archive_after_turns = self.archive_after_turns
        self._hooks_impl.enable_semantic_search = self.enable_semantic_search
        self._hooks_impl.deduplicate_media = self.deduplicate_media
        self._hooks_impl.compact_media_after_user_message = self.compact_media_after_user_message
        self._hooks_impl.compact_media_after_final_response = self.compact_media_after_final_response
        self._hooks_impl.always_compact_media_keep_last = self.always_compact_media_keep_last
        self._hooks_impl.store_media_before_compaction = self.store_media_before_compaction
        self._hooks_impl.media_store_ttl_seconds = self.media_store_ttl_seconds
        self._hooks_impl.media_store_max_files = self.media_store_max_files
        self._hooks_impl.max_messages = self.max_messages
        self._hooks_impl.max_messages_headroom = self.max_messages_headroom
        
        logger.info(
            f"ContextEngineerServer initialized: "
            f"thresholds=L1:{self.layer1_threshold}/L2:{self.layer2_threshold}/"
            f"L3:{self.layer3_threshold}, target={self.target_tokens}, "
            f"compact_media_after_user_message={self.compact_media_after_user_message}, "
            f"compact_media_after_final_response={self.compact_media_after_final_response}, "
            f"always_compact_media_keep_last={self.always_compact_media_keep_last}, "
            f"max_messages={self.max_messages}"
        )
    
    def _load_history_sync(self) -> list[dict[str, Any]]:
        """Load compaction history from persistent storage - sync version."""
        import json
        
        if not self._history_file.exists():
            return []
        
        try:
            with open(self._history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("events", [])
        except Exception as e:
            logger.warning(f"Failed to load history from {self._history_file}: {e}")
            return []
    
    def _load_history(self) -> None:
        """Load compaction history from persistent storage (init only)."""
        events = self._load_history_sync()
        if events:
            self.stats_history.extend(events)
            logger.info(f"Loaded {len(events)} history events from {self._history_file}")
    
    def _save_history_sync(self) -> None:
        """Save compaction history to persistent storage - sync version."""
        import json
        
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Keep only last 1000 events to prevent file from growing too large
            events_to_save = self.stats_history[-1000:] if len(self.stats_history) > 1000 else self.stats_history
            
            with open(self._history_file, 'w', encoding='utf-8') as f:
                json.dump({"events": events_to_save}, f, indent=2)
                
        except Exception as e:
            logger.warning(f"Failed to save history to {self._history_file}: {e}")
    
    # =========================================================================
    # MCP Tools Interface
    # =========================================================================
    
    async def list_tools(self) -> list:
        """List available MCP tools from schema."""
        return await super().list_tools()
    
    # =========================================================================
    # Hook Interface
    # =========================================================================
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Hook for automatic context engineering before LLM calls.
        
        Delegates to ContextEngineerPlugin for actual implementation.
        
        Args:
            context: Hook context with messages and metadata
            
        Returns:
            HookResult with engineered messages or original context
        """
        return await self._hooks_impl.engineer_context(context)
    
    # =========================================================================
    # MCP Tool Handlers
    # =========================================================================
    
    async def store_fact(self, params: dict[str, Any]) -> dict[str, Any]:
        """Store an important fact in core memory.
        
        Tool name: {{ name }}_store_fact → e.g., 'context_engineer_store_fact'
        
        Args:
            params: {
                "fact": The fact to store,
                "category": Category (decisions/preferences/facts/context/tasks),
                "importance": 0.0-1.0 importance score
            }
            
        Returns:
            {
                "success": bool,
                "fact_id": str,
                "category": str
            }
        """
        status = params.get("_status")
        
        try:
            fact = params.get("fact")
            if not fact:
                error_msg = "Fact parameter is required"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            category = params.get("category", "facts")
            importance = float(params.get("importance", 0.5))
            session_id = params.get("_session_id", "default")
            
            if status:
                await status.progress(f"Storing fact in {category} with importance {importance}")
            
            result = await self._hooks_impl._handle_store_fact(
                fact=fact,
                category=category,
                importance=importance,
                session_id=session_id
            )
            
            if status:
                await status.end(f"Stored fact: {fact[:50]}...")
            
            return {
                "status": "success",
                **result
            }
            
        except Exception as e:
            logger.exception(f"Error storing fact: {e}")
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}
    
    async def compact(self, params: dict[str, Any]) -> dict[str, Any]:
        """Manually trigger context compaction.
        
        Tool name: {{ name }}_compact → e.g., 'context_engineer_compact'
        
        When called via MCP tool, compaction ALWAYS runs (bypasses thresholds).
        Layers are applied progressively based on current token count.
        
        Returns:
            {
                "status": "success",
                "original_tokens": int,
                "final_tokens": int,
                "tokens_saved": int,
                "layers_applied": [...]
            }
        """
        status = params.get("_status")
        
        try:
            session_id = params.get("_session_id")
            agent = params.get("_agent")
            
            if not session_id or not agent:
                error_msg = "Session context not available"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            # Get current messages from the agent's LIVE messages list, not persisted session
            # This ensures we include the current assistant message (with tool_calls) that
            # triggered this compact() call. Without this, the compacted messages would not
            # include the current turn, causing orphaned tool responses.
            # Session-correct live messages (keyed by session_id). The prior
            # agent._current_messages was a shared singleton attr that could
            # return ANOTHER session's messages under concurrency, persisting
            # them into this session.
            messages = None
            if hasattr(agent, 'get_live_messages'):
                live = agent.get_live_messages(session_id)
                if isinstance(live, list):
                    messages = live.copy()
            elif hasattr(agent, '_current_messages') and isinstance(agent._current_messages, list):
                messages = agent._current_messages.copy()

            # Fallback to session tracker if live messages not available
            if not messages:
                messages = agent._session_tracker.get_session_messages(session_id)

            # Filter out system messages - we compact conversation only. The
            # ones that ARE compacted conversation stay: dropping the prune
            # breadcrumb here reset its running total on every manual compact,
            # so a session that had lost 40 messages reported the last 12.
            from agent_system.servers.agent.components.hook_integration import (
                is_compaction_system_message,
            )

            def _is_prompt_system(m: Any) -> bool:
                role = getattr(m, 'role', m.get('role') if isinstance(m, dict) else None)
                return role == 'system' and not is_compaction_system_message(m)

            messages = [m for m in messages if not _is_prompt_system(m)]
            if not messages:
                if status:
                    await status.end("No messages to compact")
                return {
                    "status": "success",
                    "original_tokens": 0,
                    "final_tokens": 0,
                    "tokens_saved": 0,
                    "message": "No messages in conversation"
                }
            
            if status:
                await status.progress(f"Compacting {len(messages)} messages...")
            
            # Create hook context with manual trigger flag
            # manual_trigger=True causes hook to bypass threshold checks
            from agent_system.hooks import HookContext, HookType
            
            hook_context = HookContext(
                hook_type=HookType.PRE_LLM_CALL,
                request_id=session_id,
                session_id=session_id,
                messages=messages,
                agent=agent,
                agent_name=agent.name if hasattr(agent, "name") else "unknown",
                llm=agent.llm if hasattr(agent, "llm") else None,
                metadata={"manual_trigger": True}
            )
            
            # Execute compaction
            result = await self._hooks_impl.engineer_context(hook_context)
            
            if not result.success:
                error_msg = result.error or "Compaction failed"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            # Store compacted messages for end-of-request persistence
            # We can't modify the request's local messages list directly, so we store
            # the compacted messages in the session tracker. The agent will use these
            # when persisting the session at end of request.
            if result.modified and result.context and result.context.messages:
                agent._session_tracker.set_compacted_messages(
                    session_id, result.context.messages
                )
            
            metadata = result.metadata or {}
            
            if status:
                tokens_saved = metadata.get("tokens_saved", 0)
                reduction = metadata.get("reduction_percent", 0)
                await status.end(f"Compacted: saved {tokens_saved} tokens ({reduction:.1f}% reduction)")
            
            return {
                "status": "success",
                "modified": result.modified,
                "original_tokens": metadata.get("original_tokens", 0),
                "final_tokens": metadata.get("final_tokens", 0),
                "tokens_saved": metadata.get("tokens_saved", 0),
                "reduction_percent": metadata.get("reduction_percent", 0),
                "layers_applied": metadata.get("layers_applied", []),
                "tool_results_stored": metadata.get("tool_results_stored", 0),
                "messages_archived": metadata.get("messages_archived", 0),
                "messages_dropped": metadata.get("messages_dropped", 0)
            }
            
        except Exception as e:
            logger.exception(f"Error in manual compaction: {e}")
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}   
         
    # ------------------------------------------------------------------
    # list / search / read — see hooks._handle_context_* for the rationale
    # ------------------------------------------------------------------

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Browse or filter what is stored: refs and summaries, never bodies.

        Tool name: {{ name }}_list
        """
        status = params.get("_status")
        needle = params.get("filter")
        try:
            result = await self._hooks_impl._handle_context_list(
                section=params.get("section"),
                # None, not 0: an omitted offset means "the tail", while 0 is a
                # real position (the very start). `or 0` collapsed the two.
                offset=None if params.get("offset") is None else int(params["offset"]),
                limit=int(params.get("limit") or 20),
                role=params.get("role"),
                filter=needle,
                session_id=params.get("_session_id", "default"),
            )
            if result.get("status") == "success":
                # Marks this answer as content just pulled OUT of storage, so
                # compaction does not put it straight back (see
                # compaction._is_retrieval_result).
                result[RETRIEVAL_MARKER] = True
            if status:
                if result.get("status") == "error":
                    await status.error(result["error"])
                elif needle:
                    kinds: dict[str, int] = {}
                    for e in result["entries"]:
                        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
                    detail = ", ".join(f"{n} {k}" for k, n in kinds.items()) or "nothing"
                    await status.end(f"'{needle}': {detail}")
                else:
                    shown = result["offset"] + result["count"]
                    more = (f", {result['total'] - shown} newer"
                            if result.get("has_more_after") else "")
                    older = (f", {result['offset']} older"
                             if result.get("has_more_before") else "")
                    await status.end(
                        f"{result['section']}: {result['count']} of "
                        f"{result['total']} (from #{result['offset']}{older}{more})")
            return result
        except Exception as e:
            logger.exception("Error in list: %s", e)
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}

    async def read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read one stored item by reference, always bounded.

        Tool name: {{ name }}_read
        """
        status = params.get("_status")
        ref = params.get("ref") or ""
        try:
            if status:
                await status.progress(f"Reading {ref}")
            result = await self._hooks_impl._handle_context_read(
                ref=ref,
                offset=int(params.get("offset") or 0),  # negative = from the end
                limit=params.get("limit"),
                find=params.get("find"),
                session_id=params.get("_session_id", "default"),
            )
            if result.get("status") == "success":
                # Marks this answer as content just pulled OUT of storage,
                # so compaction does not put it straight back (see
                # compaction._is_retrieval_result).
                result[RETRIEVAL_MARKER] = True
            if status:
                if result.get("status") == "error":
                    await status.error(result["error"])
                elif result.get("match_count") is not None:
                    await status.end(
                        f"{ref}: {result['match_count']} match(es) for '{params.get('find')}'")
                elif result.get("kind") == "media":
                    await status.end(f"{ref}: media queued for restoration")
                else:
                    total = result.get("total_chars") or 0
                    got = result.get("returned_chars") or 0
                    rest = (f", {total - (result.get('offset') or 0) - got} left"
                            if result.get("next_offset") else "")
                    await status.end(f"{ref}: {got} of {total} chars{rest}")
            return result
        except Exception as e:
            logger.exception("Error in read: %s", e)
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}
