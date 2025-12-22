"""Context Engineer MCP Server - Advanced context management tools.

Provides MCP tools for:
- Recall: Search archived conversation history
- Store Fact: Add important facts to core memory
- Get Variable: Retrieve stored variable content
- Get Tool Result: Retrieve stored tool output
- Stats: Get context engineering statistics

Also implements pre_llm_call hook for automatic context engineering.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_system.hooks.plugin_hook import HookContext, HookResult, PluginHook
from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class ContextEngineerServer(SchemaBasedMCPServer, PluginHook):
    """Unified MCP server and hook for context engineering.
    
    Provides MCP tools for manual context management:
    - recall: Search archived conversation history
    - store_fact: Add facts to persistent core memory
    - get_variable: Retrieve content stored as variables
    - get_tool_result: Retrieve stored tool outputs
    - stats: Get context engineering statistics
    
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
        
        # Feature settings
        self.tool_result_min_size = int(config_dict.get("tool_result_min_size", 500))
        self.tool_result_keep_last = int(config_dict.get("tool_result_keep_last", 3))
        self.tool_result_max_inline_size = int(config_dict.get("tool_result_max_inline_size", 5000))
        self.variable_min_size = int(config_dict.get("variable_min_size", 200))
        self.archive_after_turns = int(config_dict.get("archive_after_turns", 10))
        self.enable_semantic_search = bool(config_dict.get("enable_semantic_search", False))
        
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
            history_callback=self._save_history  # Callback to save after each event
        )
        
        # Sync config to hooks implementation
        self._hooks_impl.layer1_threshold = self.layer1_threshold
        self._hooks_impl.layer2_threshold = self.layer2_threshold
        self._hooks_impl.layer3_threshold = self.layer3_threshold
        self._hooks_impl.target_tokens = self.target_tokens
        self._hooks_impl.tool_result_min_size = self.tool_result_min_size
        self._hooks_impl.tool_result_keep_last = self.tool_result_keep_last
        self._hooks_impl.tool_result_max_inline_size = self.tool_result_max_inline_size
        self._hooks_impl.variable_min_size = self.variable_min_size
        self._hooks_impl.archive_after_turns = self.archive_after_turns
        self._hooks_impl.enable_semantic_search = self.enable_semantic_search
        
        logger.info(
            f"ContextEngineerServer initialized: "
            f"thresholds=L1:{self.layer1_threshold}/L2:{self.layer2_threshold}/"
            f"L3:{self.layer3_threshold}, target={self.target_tokens}"
        )
    
    def _load_history(self) -> None:
        """Load compaction history from persistent storage."""
        import json
        
        if not self._history_file.exists():
            return
        
        try:
            with open(self._history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.stats_history.extend(data.get("events", []))
                logger.info(f"Loaded {len(self.stats_history)} history events from {self._history_file}")
        except Exception as e:
            logger.warning(f"Failed to load history from {self._history_file}: {e}")
    
    def _save_history(self) -> None:
        """Save compaction history to persistent storage."""
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
    
    async def recall(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search archived conversation history.
        
        Tool name: {{ name }}_recall → e.g., 'context_engineer_recall'
        
        Args:
            params: {
                "query": Search query string,
                "limit": Max results (default 5)
            }
            
        Returns:
            {
                "results": [...],
                "total_found": int
            }
        """
        status = params.get("_status")
        
        try:
            query = params.get("query")
            if not query:
                error_msg = "Query parameter is required"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            limit = int(params.get("limit", 5))
            session_id = params.get("_session_id", "default")
            
            if status:
                await status.progress(f"Searching archived context for: {query}")
            
            result = await self._hooks_impl._handle_recall(
                query=query,
                limit=limit,
                session_id=session_id
            )
            
            if status:
                await status.end(f"Found {result['total_found']} relevant messages")
            
            return {
                "status": "success",
                **result
            }
            
        except Exception as e:
            logger.exception(f"Error in recall: {e}")
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}
    
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
    
    async def get_variable(self, params: dict[str, Any]) -> dict[str, Any]:
        """Retrieve content stored as a variable with pagination support.
        
        Tool name: {{ name }}_get_variable → e.g., 'context_engineer_get_variable'
        
        Args:
            params: {
                "variable_name": Variable name (e.g., $VAR_1),
                "mode": "preview" | "chunk" | "search" | "full" (default: preview),
                "offset": Start position for chunk mode (default: 0),
                "limit": Max chars for chunk mode (default: 1000),
                "search": Search query for search mode,
                "context_chars": Context around search matches (default: 150)
            }
            
        Returns:
            Mode-dependent response with content/matches and metadata
        """
        status = params.get("_status")
        
        try:
            variable_name = params.get("variable_name")
            if not variable_name:
                error_msg = "Variable name parameter is required"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            session_id = params.get("_session_id", "default")
            mode = params.get("mode", "preview")
            
            if status:
                await status.progress(f"Retrieving variable {variable_name} (mode={mode})")
            
            result = await self._hooks_impl._handle_get_variable(
                variable_name=variable_name,
                session_id=session_id,
                mode=mode,
                offset=params.get("offset", 0),
                limit=params.get("limit", 1000),
                search=params.get("search"),
                context_chars=params.get("context_chars", 150)
            )
            
            if result.get("found"):
                chars_info = f"{result.get('returned_chars', 0)}/{result.get('total_chars', 0)} chars"
                if status:
                    await status.end(f"Retrieved {variable_name} ({chars_info})")
            else:
                if status:
                    await status.error(f"Variable {variable_name} not found")
            
            return {
                "status": "success" if result.get("found") else "not_found",
                **result
            }
            
        except Exception as e:
            logger.exception(f"Error getting variable: {e}")
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}
    
    async def get_tool_result(self, params: dict[str, Any]) -> dict[str, Any]:
        """Retrieve a stored tool result with pagination support.
        
        Tool name: {{ name }}_get_tool_result → e.g., 'context_engineer_get_tool_result'
        
        Args:
            params: {
                "reference": Reference ID or content hash,
                "mode": "preview" | "chunk" | "search" | "full" (default: preview),
                "offset": Start position for chunk mode (default: 0),
                "limit": Max chars for chunk mode (default: 1000),
                "search": Search query for search mode,
                "context_chars": Context around search matches (default: 150)
            }
            
        Returns:
            Mode-dependent response with content/matches and metadata
        """
        status = params.get("_status")
        
        try:
            reference = params.get("reference")
            if not reference:
                error_msg = "Reference parameter is required"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            session_id = params.get("_session_id", "default")
            mode = params.get("mode", "preview")
            
            if status:
                await status.progress(f"Retrieving tool result {reference} (mode={mode})")
            
            result = await self._hooks_impl._handle_get_tool_result(
                reference=reference,
                session_id=session_id,
                mode=mode,
                offset=params.get("offset", 0),
                limit=params.get("limit", 1000),
                search=params.get("search"),
                context_chars=params.get("context_chars", 150)
            )
            
            if result.get("found"):
                # Format status message based on mode
                if result.get("mode") == "chunk":
                    offset = result.get("offset", 0)
                    returned = result.get("returned_chars", 0)
                    total = result.get("total_chars", 0)
                    chars_info = f"offset {offset}-{offset+returned}/{total} chars"
                else:
                    chars_info = f"{result.get('returned_chars', 0)}/{result.get('total_chars', 0)} chars"
                
                if status:
                    await status.end(f"Retrieved tool result ({chars_info})")
            else:
                if status:
                    await status.error(f"Tool result '{reference}' not found")
            
            return {
                "status": "success" if result.get("found") else "not_found",
                **result
            }
            
        except Exception as e:
            logger.exception(f"Error getting tool result: {e}")
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}
    
    async def stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get context engineering statistics.
        
        Tool name: {{ name }}_stats → e.g., 'context_engineer_stats'
        
        Returns:
            {
                "tool_results": {...},
                "variables": {...},
                "core_memory": {...},
                "archival_memory": {...}
            }
        """
        status = params.get("_status")
        
        try:
            session_id = params.get("_session_id", "default")
            
            if status:
                await status.progress("Gathering context engineering statistics")
            
            result = await self._hooks_impl._handle_stats(session_id=session_id)
            
            if status:
                tool_count = result.get("tool_results", {}).get("total_entries", 0)
                var_count = result.get("variables", {}).get("total_variables", 0)
                fact_count = result.get("core_memory", {}).get("facts", 0)
                archive_count = result.get("archival_memory", {}).get("total_messages", 0)
                
                await status.end(
                    f"Stats: {tool_count} tool results, {var_count} variables, "
                    f"{fact_count} facts, {archive_count} archived messages"
                )
            
            return {
                "status": "success",
                **result
            }
            
        except Exception as e:
            logger.exception(f"Error getting stats: {e}")
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
            
            # Get current messages
            messages = agent._session_tracker.get_session_messages(session_id)
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
                "variables_created": metadata.get("variables_created", 0),
                "messages_archived": metadata.get("messages_archived", 0),
                "messages_dropped": metadata.get("messages_dropped", 0)
            }
            
        except Exception as e:
            logger.exception(f"Error in manual compaction: {e}")
            if status:
                await status.error(str(e))
            return {"status": "error", "error": str(e)}
