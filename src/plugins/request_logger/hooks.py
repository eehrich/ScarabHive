"""Request Logger Plugin - Schema-based hooks plugin.

This plugin demonstrates how to create a schema-based hooks-only plugin
that logs agent requests and responses.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult

logger = logging.getLogger(__name__)


class RequestLoggerPlugin(SchemaBasedPluginHook):
    """Example schema-based plugin that logs agent lifecycle events.
    
    This plugin demonstrates:
    - Schema-based hook plugin (hooks defined in schema.yaml)
    - Multiple hooks (log_pre_llm, log_post_llm, log_session_start, log_session_end)
    - Hook ordering (log_post_llm comes after log_pre_llm)
    - Passing data between hooks using context metadata
    - Timing measurements
    - Configuration from schema.yaml
    
    Hook definitions are loaded from schema.yaml.
    Handler methods match hook names exactly.
    """
    
    def __init__(self, plugin_dir: Path | str, mcp_config: Any = None):
        """Initialize the request logger plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
            mcp_config: MCP configuration (contains config from plugins.yaml)
        """
        super().__init__(plugin_dir)
        self.request_count = 0
        self.session_data: Dict[str, Any] = {}
        
        # Get configuration from schema defaults
        config = self.get_config()
        
        # Merge with mcp_config.config if provided (overrides schema defaults)
        if mcp_config and hasattr(mcp_config, 'config') and mcp_config.config:
            config.update(mcp_config.config)
        self.log_level = str(config.get('log_level', 'INFO'))
        self.log_message_content = bool(config.get('log_message_content', True))
        self.log_timing = bool(config.get('log_timing', True))
        self.max_content_preview = int(config.get('max_content_preview', 100))
    
    # Hook handler methods - names must match hook names in schema.yaml
    
    async def log_pre_llm(self, context: HookContext) -> HookResult:
        """Log LLM request before execution.
        
        Handler for 'log_pre_llm' hook defined in schema.yaml.
        """
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
                f"agent={context.agent_name or 'unknown'}"
            )
            
            # Log message summary if enabled
            if self.log_message_content and context.messages and len(context.messages) > 0:
                last_msg = context.messages[-1]
                role = last_msg.role
                content = str(last_msg.content)
                content_preview = content[:self.max_content_preview]
                if len(content) > self.max_content_preview:
                    content_preview += "..."
                logger.debug(f"[RequestLogger] Last message: role={role}, content={content_preview}")
            
            # IMPORTANT: Return modified=False because we only modified metadata, not messages.
            # Returning modified=True would cause our input context (with potentially old messages)
            # to replace the current_context, overwriting any message modifications from earlier hooks.
            return HookResult(
                success=True,
                modified=False,  # Only metadata was modified, not messages
                context=context,
                metadata={'logged': True, 'request_number': request_num}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_pre_llm: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def log_post_llm(self, context: HookContext) -> HookResult:
        """Log LLM response after execution.
        
        Handler for 'log_post_llm' hook defined in schema.yaml.
        """
        try:
            # Calculate timing if available and enabled
            start_time = context.metadata.get('request_logger_start_time') if context.metadata else None
            request_num = context.metadata.get('request_logger_number', '?') if context.metadata else '?'
            
            duration_ms = None
            if self.log_timing and start_time:
                duration_ms = (time.time() - start_time) * 1000
            
            # Log response info
            log_msg = f"[RequestLogger] Request #{request_num} - Post-LLM Call"
            
            if duration_ms is not None:
                log_msg += f": duration={duration_ms:.2f}ms"
            
            if self.log_message_content and context.llm_response:
                if isinstance(context.llm_response, dict):
                    response_content = str(context.llm_response.get('content', ''))
                else:
                    response_content = str(context.llm_response)
                
                content_preview = response_content[:self.max_content_preview]
                if len(response_content) > self.max_content_preview:
                    content_preview += "..."
                log_msg += f", response_preview={content_preview}"
            
            logger.info(log_msg)
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'duration_ms': duration_ms}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_post_llm: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def log_session_start(self, context: HookContext) -> HookResult:
        """Log session start event.
        
        Handler for 'log_session_start' hook defined in schema.yaml.
        """
        try:
            session_id = context.session_id or 'unknown'
            agent_name = context.agent_name or 'unknown'
            
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
            logger.error(f"[RequestLogger] Error in log_session_start: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
    
    async def log_session_end(self, context: HookContext) -> HookResult:
        """Log session end event with summary.
        
        Handler for 'log_session_end' hook defined in schema.yaml.
        """
        try:
            session_id = context.session_id or 'unknown'
            session_info = self.session_data.get(session_id, {})
            
            log_msg = f"[RequestLogger] Session Ended: session_id={session_id}"
            
            if self.log_timing:
                start_time = session_info.get('start_time')
                if start_time:
                    duration_s = time.time() - start_time
                    log_msg += f", duration={duration_s:.2f}s"
            
            log_msg += f", total_requests={self.request_count}"
            
            logger.info(log_msg)
            
            # Clean up session data
            if session_id in self.session_data:
                duration_s = time.time() - session_info['start_time'] if session_info.get('start_time') else None
                del self.session_data[session_id]
            else:
                duration_s = None
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'logged': True, 'duration_s': duration_s}
            )
            
        except Exception as e:
            logger.error(f"[RequestLogger] Error in log_session_end: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
