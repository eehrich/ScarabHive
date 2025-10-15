"""LLM Message Validator Plugin - Hook implementation.

Validates and repairs message sequences before LLM calls using existing
MessageValidator from agent_system.llm.message_validator.
"""
from __future__ import annotations

import logging
from pathlib import Path

from agent_system.hooks import HookContext, HookResult
from agent_system.hooks.schema_based import SchemaBasedPluginHook
from agent_system.llm.message_validator import MessageValidator, ValidationResult

logger = logging.getLogger(__name__)


class MessageValidatorPlugin(SchemaBasedPluginHook):
    """Hook plugin for message validation before LLM calls."""
    
    def __init__(self, plugin_dir: Path):
        """Initialize the message validator plugin.
        
        Args:
            plugin_dir: Directory containing plugin configuration files
        """
        super().__init__(plugin_dir)
        
        # Get config from schema
        config = self.config or {}
        
        # Extract log_level (config might be nested dict)
        log_level = config.get('log_level', 'warning')
        if isinstance(log_level, dict):
            log_level = log_level.get('default', 'warning')
        
        # Initialize validator with config
        self.validator = MessageValidator(log_level=log_level)
        
        # Store configuration options
        repair_mode = config.get('repair_mode', 'auto')
        if isinstance(repair_mode, dict):
            repair_mode = repair_mode.get('default', 'auto')
        self.repair_mode = repair_mode
        
        # Extract boolean configs with proper handling
        def get_bool_config(key: str, default: bool = True) -> bool:
            val = config.get(key, default)
            if isinstance(val, dict):
                val = val.get('default', default)
            return bool(val)
        
        self.enable_tool_call_check = get_bool_config('enable_tool_call_check', True)
        self.enable_tool_name_check = get_bool_config('enable_tool_name_check', True)
        self.enable_content_check = get_bool_config('enable_content_check', True)
        self.enable_sequence_check = get_bool_config('enable_sequence_check', True)
        self.remove_orphaned_tool_responses = get_bool_config('remove_orphaned_tool_responses', True)
        self.fix_tool_name_format = get_bool_config('fix_tool_name_format', True)
        self.normalize_content = get_bool_config('normalize_content', True)
        
        logger.debug(
            f"MessageValidatorPlugin initialized: repair_mode={self.repair_mode}, "
            f"log_level={log_level}"
        )
    
    async def validate_messages(self, context: HookContext) -> HookResult:
        """Validate messages before LLM call.
        
        Hook implementation that validates message sequences and repairs issues.
        Called automatically before each LLM call.
        
        Args:
            context: Hook context with messages to validate
            
        Returns:
            HookResult with validated/repaired messages
        """
        try:
            messages = context.messages
            if not messages:
                return HookResult(
                    success=True,
                    modified=False,
                    context=context,
                    metadata={'reason': 'no_messages'}
                )
            
            # Convert dict messages to ChatMessage objects if needed
            from agent_system.llm.models import ChatMessage
            chat_messages = []
            for msg in messages:
                if isinstance(msg, dict):
                    chat_messages.append(ChatMessage(**msg))
                else:
                    chat_messages.append(msg)
            
            # Run validation
            validation_context = f"{context.agent_name or 'unknown'}_{context.hook_type.value}"
            result: ValidationResult = self.validator.validate_and_repair(
                chat_messages,
                context=validation_context
            )
            
            # Check repair mode
            if self.repair_mode == 'strict' and not result.is_valid:
                error_msg = f"Message validation failed: {result.repair_summary}"
                logger.error(error_msg)
                return HookResult(
                    success=False,
                    modified=False,
                    context=context,
                    error=error_msg,
                    metadata={
                        'validation_failed': True,
                        'issues': [
                            {
                                'type': issue.type,
                                'severity': issue.severity,
                                'index': issue.message_index,
                                'description': issue.description
                            }
                            for issue in result.issues
                        ]
                    }
                )
            
            # Auto-repair mode: use repaired messages
            modified = len(result.issues) > 0
            
            if modified:
                # Convert back to dict format for consistency
                repaired_dicts = []
                for msg in result.repaired_messages:
                    if hasattr(msg, 'model_dump'):
                        repaired_dicts.append(msg.model_dump(exclude_none=True))
                    else:
                        repaired_dicts.append(msg)
                
                # Update context with repaired messages
                context.messages = repaired_dicts
                
                logger.info(
                    f"Message validation repaired {len(result.issues)} issues: "
                    f"{result.repair_summary}"
                )
            
            return HookResult(
                success=True,
                modified=modified,
                context=context,
                metadata={
                    'issues_found': len(result.issues),
                    'issues_repaired': len(result.issues) if modified else 0,
                    'repair_summary': result.repair_summary,
                    'validation_context': validation_context
                }
            )
            
        except Exception as e:
            logger.exception(f"Error in message validation hook: {e}")
            return HookResult(
                success=False,
                modified=False,
                context=context,
                error=str(e)
            )
