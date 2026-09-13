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
        
        # Load configuration. ONE mapping, handed over whole — the plugin maps
        # it onto CompactionConfig by field name and warns about keys that
        # reach nothing.
        #
        # This used to be two hand-maintained lists (read every key here, then
        # copy every key onto _hooks_impl) plus a third inside hooks.py, with a
        # comment block reminding the next author to touch all four places. It
        # drifted: measured on the shipped config, 5 of 25 settings never
        # arrived — including a request-size guard deliberately lowered to
        # 29 MB for a provider cap, running at 90 MB, and `semantic_search`,
        # whose name was simply wrong with nothing to say so.
        config_dict = dict(mcp_config.config) if getattr(mcp_config, "config", None) else {}

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
        self._hooks_impl.apply_config(config_dict)
        self.config = config_dict

        cfg = self._hooks_impl
        logger.info(
            f"ContextEngineerServer initialized: "
            f"thresholds=L1:{cfg.layer1_threshold}/L2:{cfg.layer2_threshold}/"
            f"L3:{cfg.layer3_threshold}, target={cfg.target_tokens}, "
            f"max_request_bytes={cfg.max_request_bytes // (1024*1024)}MB, "
            f"compact_media_after_user_message={cfg.compact_media_after_user_message}, "
            f"compact_media_after_final_response={cfg.compact_media_after_final_response}, "
            f"always_compact_media_keep_last={cfg.always_compact_media_keep_last}, "
            f"max_messages={cfg.max_messages}"
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
            if not result.get("success"):
                if status:
                    await status.error(result["error"])
                return {"status": "error", **result}

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
            
            # manual_trigger=True bypasses the threshold gate and the hysteresis.
            # Only for a person: a /compact typed at the prompt arrives through
            # run_plugin_command, which dispatches without a request id. The
            # model calling this tool mid-run always has one — and it called it
            # "proactively" every few steps, far below the threshold, each call
            # a rewrite of the prompt and a cache break the hysteresis never saw.
            # For the model the automatic rules apply.
            by_person = not params.get("_request_id")
            from agent_system.hooks import HookContext, HookType

            # The agent's own hooks.overrides for this hook, as the registry
            # hands them to the pre-LLM call — without them a /compact that
            # came first compacted with the plugin defaults.
            hooks_cfg = getattr(getattr(agent, "agent_config", None), "hooks", None)
            override = (getattr(hooks_cfg, "overrides", None) or {}).get(
                f"{self.name}.engineer_context") or {}
            hook_config = {k: v for k, v in override.items()
                           if k not in ("enabled", "timeout", "order")} \
                if isinstance(override, dict) else {}

            hook_context = HookContext(
                hook_type=HookType.PRE_LLM_CALL,
                request_id=session_id,
                session_id=session_id,
                messages=messages,
                agent=agent,
                agent_name=agent.name if hasattr(agent, "name") else "unknown",
                llm=agent.llm if hasattr(agent, "llm") else None,
                # The system prompt is filtered out above: this reading is not
                # comparable with the pre-LLM call's and must not move the
                # hysteresis base.
                metadata={"manual_trigger": by_person, "partial_view": True},
                hook_config=hook_config,
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
            # Only when something changed: the agent rebuilds its list from a
            # staged history as [system prompt] + staged + tool messages, which
            # drops every other leading system message — a prompt rewrite of its
            # own, for a run that compacted nothing.
            if (result.modified and (result.metadata or {}).get("compacted")
                    and result.context and result.context.messages):
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
    # list / read — see hooks._handle_context_* for the rationale
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
