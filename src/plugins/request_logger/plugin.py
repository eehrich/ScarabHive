"""Request Logger Plugin - Example hooks-only plugin.

This plugin demonstrates how to create a hooks-only plugin that logs
agent requests and responses without providing any MCP tools or web endpoints.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

from agent_system.hooks import PluginHook, HookContext, HookResult

logger = logging.getLogger(__name__)


class RequestLoggerPlugin(PluginHook):
    """Example plugin that logs agent lifecycle events using hooks.
    
    This plugin demonstrates:
    - Hooks-only plugin (no MCP tools or web endpoints)
    - Multiple hooks (pre_llm, post_llm, session_start, session_end)
    - Hook ordering (log_post_llm comes after log_pre_llm)
    - Passing data between hooks using context metadata
    - Timing measurements
    
    Note: This plugin does NOT inherit from MCPServer because it provides
    no tools. Hooks-only plugins should only inherit from PluginHook.
    """
    
    def __init__(self, name: str, config: Dict[str, Any] = None):
        """Initialize the request logger plugin.
        
        Args:
            name: Plugin name
            config: Plugin configuration dictionary
        """
        super().__init__(name, config or {})
        self.request_count = 0
        self.session_data: Dict[str, Any] = {}
    
    # Hook implementations
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """Log LLM request before execution."""
        try:
            self.request_count += 1
            request_num = self.request_count
            
            # Store start time in metadata for timing calculation
            if context.metadata is None:
                context.metadata = {}
            context.metadata['request_logger_start_time'] = time.time()
            context.metadata['request_logger_number'] = request_num
            
            # Log request info
            msg_count = len(context.messages) if context.messages else 0
            logger.info(
                f"[RequestLogger] Request #{request_num} - Pre-LLM Call: "
                f"session={context.session_id}, messages={msg_count}, "
                f"agent={context.agent.name if context.agent else 'unknown'}"
            )
            
            # Log message summary
            if context.messages and len(context.messages) > 0:
                last_msg = context.messages[-1]
                role = last_msg.get('role', 'unknown')
                content_preview = str(last_msg.get('content', ''))[:100]
                logger.debug(f"[RequestLogger] Last message: role={role}, content={content_preview}...")
            
            return HookResult(
                success=True,
                modified=True,  # We modified metadata
                context=context,
                metadata={'logged': True, 'request_number': request_num}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in pre_llm hook: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        """Log LLM response after execution."""
        try:
            # Calculate timing if available
            start_time = context.metadata.get('request_logger_start_time') if context.metadata else None
            request_num = context.metadata.get('request_logger_number', '?') if context.metadata else '?'
            
            duration_ms = None
            if start_time:
                duration_ms = (time.time() - start_time) * 1000
            
            # Log response info
            response_content = ""
            if context.llm_response:
                if isinstance(context.llm_response, dict):
                    response_content = str(context.llm_response.get('content', ''))[:100]
                else:
                    response_content = str(context.llm_response)[:100]
            
            logger.info(
                f"[RequestLogger] Request #{request_num} - Post-LLM Call: "
                f"duration={duration_ms:.2f}ms, response_preview={response_content}..."
                if duration_ms else
                f"[RequestLogger] Request #{request_num} - Post-LLM Call: response_preview={response_content}..."
            )
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'duration_ms': duration_ms}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in post_llm hook: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def on_session_start(self, context: HookContext) -> HookResult:
        """Log session start event."""
        try:
            session_id = context.session_id or 'unknown'
            agent_name = context.agent.name if context.agent else 'unknown'
            
            logger.info(
                f"[RequestLogger] Session Started: "
                f"session_id={session_id}, agent={agent_name}"
            )
            
            # Initialize session tracking
            self.session_data[session_id] = {
                'start_time': time.time(),
                'agent': agent_name,
                'request_count': 0
            }
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in session_start hook: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def on_session_end(self, context: HookContext) -> HookResult:
        """Log session end event with summary."""
        try:
            session_id = context.session_id or 'unknown'
            session_info = self.session_data.get(session_id, {})
            
            start_time = session_info.get('start_time')
            duration_s = (time.time() - start_time) if start_time else None
            
            logger.info(
                f"[RequestLogger] Session Ended: "
                f"session_id={session_id}, "
                f"duration={duration_s:.2f}s, "
                f"total_requests={self.request_count}"
                if duration_s else
                f"[RequestLogger] Session Ended: session_id={session_id}"
            )
            
            # Clean up session data
            if session_id in self.session_data:
                del self.session_data[session_id]
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'duration_s': duration_s}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in session_end hook: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    # Unused hooks (return unmodified context)
    async def on_pre_tool_call(self, context: HookContext) -> HookResult:
        """Not used by this plugin."""
        return HookResult(success=True, modified=False, context=context)
    
    async def on_post_tool_call(self, context: HookContext) -> HookResult:
        """Not used by this plugin."""
        return HookResult(success=True, modified=False, context=context)
    
    async def on_format_output(self, context: HookContext) -> HookResult:
        """Not used by this plugin."""
        return HookResult(success=True, modified=False, context=context)


def PLUGIN_FACTORY(name: str, config: Dict[str, Any], ssl_verify: bool = True) -> RequestLoggerPlugin:
    """Factory function to create RequestLogger plugin instances.
    
    Args:
        name: Plugin name (from discovery)
        config: Plugin configuration dictionary
        ssl_verify: SSL verification flag (unused by this plugin)
    
    Returns:
        Configured RequestLoggerPlugin instance
    """
    return RequestLoggerPlugin(name, config)
