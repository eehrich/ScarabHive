"""Message Debugger Plugin - Hooks implementation.

Captures and stores LLM conversation messages for debugging and inspection.
Provides detailed analysis including token counts, tool calls, and message flow.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent_system.hooks import SchemaBasedPluginHook, HookContext, HookResult
from agent_system.llm.token_utils import estimate_token_count
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)


class MessageDebuggerPlugin(SchemaBasedPluginHook):
    """Schema-based plugin for capturing and debugging LLM messages.
    
    Hooks into pre_llm_call to capture message state before each LLM interaction.
    Stores detailed information including token estimates, tool calls, and timing.
    
    Configuration is loaded from schema.yaml.
    """
    
    def __init__(self, plugin_dir: Path | str, message_history: List[Dict[str, Any]] | None = None):
        """Initialize the message debugger plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
            message_history: Optional list to track message snapshots for web UI
        """
        super().__init__(plugin_dir)
        
        # Shared history for web UI
        self.message_history = message_history if message_history is not None else []
        
        # Load config from schema.yaml
        config = self.get_config()
        self.max_history = int(config.get('max_history_entries', 100))
        self.capture_enabled = bool(config.get('capture_enabled', True))
        self.capture_pre_llm = bool(config.get('capture_pre_llm', True))
        self.capture_post_llm = bool(config.get('capture_post_llm', True))
        self.include_tool_calls = bool(config.get('include_tool_calls', True))
        self.include_token_estimates = bool(config.get('include_token_estimates', True))
        self.auto_cleanup_threshold = int(config.get('auto_cleanup_threshold', 150))
        
        logger.info(
            f"MessageDebuggerPlugin initialized: max_history={self.max_history}, "
            f"capture_enabled={self.capture_enabled}, pre={self.capture_pre_llm}, "
            f"post={self.capture_post_llm}, include_tool_calls={self.include_tool_calls}"
        )
    
    async def debugger_capture_pre_llm(self, context: HookContext) -> HookResult:
        """Capture messages before LLM call (input snapshot).
        
        Handler for 'debugger_capture_pre_llm' hook defined in schema.yaml.
        Runs last in pre_llm_call chain to capture final state before LLM.
        
        Args:
            context: Hook context with messages and metadata
            
        Returns:
            HookResult with unmodified context (read-only hook)
        """
        if not self.capture_enabled or not self.capture_pre_llm:
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'reason': 'capture_disabled'}
            )
        
        return await self._capture_snapshot(context, snapshot_type='pre_llm')
    
    async def debugger_capture_post_llm(self, context: HookContext) -> HookResult:
        """Capture LLM response after LLM call (output snapshot).
        
        Handler for 'debugger_capture_post_llm' hook defined in schema.yaml.
        Runs first in post_llm_call chain to capture raw LLM response.
        
        Args:
            context: Hook context with LLM response and metadata
            
        Returns:
            HookResult with unmodified context (read-only hook)
        """
        if not self.capture_enabled or not self.capture_post_llm:
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'reason': 'capture_disabled'}
            )
        
        return await self._capture_snapshot(context, snapshot_type='post_llm')
    
    async def _capture_snapshot(self, context: HookContext, snapshot_type: str) -> HookResult:
        """Internal method to capture a message snapshot.
        
        Args:
            context: Hook context with messages/response
            snapshot_type: 'pre_llm' or 'post_llm'
            
        Returns:
            HookResult with unmodified context
        """
        try:
            messages = context.messages or []
            
            if not messages:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_messages', 'snapshot_type': snapshot_type}
                )
            
            # Convert messages to serializable format
            message_data = []
            total_tokens = 0
            
            for idx, msg in enumerate(messages):
                # Ensure we have a ChatMessage object
                if not isinstance(msg, ChatMessage):
                    try:
                        msg = ChatMessage(**msg)
                    except (TypeError, ValueError) as e:
                        logger.warning(f"Failed to convert message {idx} to ChatMessage: {e}")
                        continue
                
                # Calculate token estimate if enabled
                msg_tokens = 0
                if self.include_token_estimates:
                    msg_tokens = estimate_token_count([msg])
                    total_tokens += msg_tokens
                
                # Build message info
                msg_info = {
                    'index': idx,
                    'role': msg.role,
                    'content': str(msg.content) if msg.content else None,
                    'content_length': len(str(msg.content)) if msg.content else 0,
                    'estimated_tokens': msg_tokens if self.include_token_estimates else None,
                }
                
                # Add tool call information if enabled
                if self.include_tool_calls:
                    if hasattr(msg, 'tool_calls') and msg.tool_calls:
                        tool_calls_info = []
                        for tc in msg.tool_calls:
                            tool_calls_info.append({
                                'id': tc.get('id'),
                                'type': tc.get('type'),
                                'function': {
                                    'name': tc.get('function', {}).get('name'),
                                    'arguments': tc.get('function', {}).get('arguments'),
                                }
                            })
                        msg_info['tool_calls'] = tool_calls_info
                        msg_info['tool_call_count'] = len(tool_calls_info)
                    else:
                        msg_info['tool_calls'] = None
                        msg_info['tool_call_count'] = 0
                    
                    # Check if this is a tool result
                    if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
                        msg_info['tool_call_id'] = msg.tool_call_id
                        msg_info['is_tool_result'] = True
                    else:
                        msg_info['tool_call_id'] = None
                        msg_info['is_tool_result'] = False
                
                message_data.append(msg_info)
            
            # Create snapshot entry
            snapshot = {
                'timestamp': datetime.now().isoformat(),
                'snapshot_type': snapshot_type,
                'agent_name': context.agent_name,
                'request_id': context.request_id,
                'session_id': context.session_id,
                'message_count': len(message_data),
                'total_estimated_tokens': total_tokens if self.include_token_estimates else None,
                'context_window': getattr(context.llm, 'context_window', None) if hasattr(context, 'llm') and context.llm else None,
                'messages': message_data,
            }
            
            # Add LLM response info for post_llm snapshots
            if snapshot_type == 'post_llm' and hasattr(context, 'llm_response') and context.llm_response:
                snapshot['llm_response'] = {
                    'model': context.llm_response.get('model'),
                    'usage': context.llm_response.get('usage'),
                    'finish_reason': context.llm_response.get('finish_reason'),
                }
            
            # Add to history
            self.message_history.append(snapshot)
            
            # Auto-cleanup if threshold exceeded
            if len(self.message_history) > self.auto_cleanup_threshold:
                # Remove oldest entries to get back to max_history
                remove_count = len(self.message_history) - self.max_history
                if remove_count > 0:
                    del self.message_history[:remove_count]
                    logger.debug(f"Auto-cleanup: removed {remove_count} old message snapshots")
            
            logger.debug(
                f"Captured {snapshot_type} snapshot: agent={context.agent_name}, "
                f"messages={len(message_data)}, tokens={total_tokens}, "
                f"history_size={len(self.message_history)}"
            )
            
            # Return unmodified context (read-only hook)
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={
                    'captured': True,
                    'snapshot_type': snapshot_type,
                    'message_count': len(message_data),
                    'total_tokens': total_tokens,
                    'history_size': len(self.message_history)
                }
            )
            
        except Exception as e:
            logger.exception(f"Failed to capture {snapshot_type} snapshot: {e}")
            # Don't fail the LLM call on capture errors
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'error': str(e), 'snapshot_type': snapshot_type}
            )
