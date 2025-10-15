"""
Message Validator Plugin - Schema-Based Hook Plugin.

This plugin demonstrates schema-based hook implementation for message validation.
Hook definitions are loaded from schema.yaml.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- message_validator: Main validation hook (pre_llm_call)
- message_structure_validator: Structure validation hook (pre_llm_call)

**Features:**
- Validates message structure and role sequences
- Sanitizes message content
- Enforces message format rules
- Ensures alternating user/assistant pattern
- Configurable validation strictness
"""

from pathlib import Path
from typing import Any
import logging
import re
from dataclasses import replace

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)


class MessageValidatorPlugin(SchemaBasedPluginHook):
    """
    Schema-based hook plugin for message validation.
    
    Hook definitions and configuration are loaded from schema.yaml.
    """
    
    def __init__(self, plugin_dir: Path | str):
        """Initialize the message validator plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
        """
        super().__init__(plugin_dir)
        logger.info("MessageValidatorPlugin initialized with schema-based hooks")
    
    # Handler for 'message_validator' hook (referenced in schema.yaml as 'validate_messages')
    async def validate_messages(self, context: HookContext) -> HookResult:
        """
        Main validation hook - validates and sanitizes messages.
        
        Performs:
        1. Content sanitization
        2. Length validation
        3. Role validation
        4. Sequence validation (alternating pattern)
        """
        try:
            messages = context.messages or []
            
            if not messages:
                return HookResult(success=True, modified=False, context=context)
            
            # Get config
            config = self.get_config()
            strict_mode = config.get('strict_mode', False)
            max_length = config.get('max_message_length', 100000)
            sanitize = config.get('sanitize_content', True)
            enforce_alternating = config.get('enforce_alternating', True)
            allowed_roles = config.get('allowed_roles', ['system', 'user', 'assistant', 'tool'])
            
            validated_messages = []
            errors = []
            
            for i, msg in enumerate(messages):
                # Handle both dict and ChatMessage objects
                if isinstance(msg, ChatMessage):
                    role = msg.role
                    content = msg.content
                elif isinstance(msg, dict):
                    role = msg.get('role', '')
                    content = msg.get('content', '')
                else:
                    error = f"Message {i} is neither ChatMessage nor dict: {type(msg)}"
                    if strict_mode:
                        return HookResult(success=False, modified=False, context=context, error=error)
                    errors.append(error)
                    continue
                
                # Validate role
                if role not in allowed_roles:
                    error = f"Invalid role '{role}' at index {i}"
                    if strict_mode:
                        return HookResult(success=False, modified=False, context=context, error=error)
                    errors.append(error)
                    continue
                
                # Validate content length
                content_str = str(content) if content is not None else ''
                if len(content_str) > max_length:
                    error = f"Message {i} exceeds max length ({len(content_str)} > {max_length})"
                    if strict_mode:
                        return HookResult(success=False, modified=False, context=context, error=error)
                    # Truncate in non-strict mode
                    content_str = content_str[:max_length] + '... [truncated]'
                    errors.append(error)
                
                # Sanitize content (only for string content)
                if sanitize and isinstance(content_str, str):
                    content_str = self._sanitize_content(content_str)
                
                # Create validated message
                if isinstance(msg, ChatMessage):
                    # Update ChatMessage with sanitized content
                    validated_msg = msg.model_copy(update={'content': content_str})
                else:
                    # Dict message
                    validated_msg = {**msg, 'content': content_str}
                
                validated_messages.append(validated_msg)
            
            # Check alternating pattern
            if enforce_alternating:
                validated_messages = self._enforce_alternating_pattern(validated_messages)
            
            # Create modified context using dataclasses.replace
            modified_context = replace(context, messages=validated_messages)
            
            modified = len(errors) > 0
            
            if errors:
                logger.warning(f"Message validation issues: {'; '.join(errors)}")
            
            return HookResult(
                success=True,
                modified=modified,
                context=modified_context,
                metadata={
                    'validation_errors': errors,
                    'messages_validated': len(validated_messages)
                }
            )
            
        except Exception as e:
            logger.exception(f"Error in message validation: {e}")
            return HookResult(success=False, modified=False, context=context, error=str(e))
    
    # Handler for 'message_structure_validator' hook (referenced in schema.yaml as 'validate_structure')
    async def validate_structure(self, context: HookContext) -> HookResult:
        """
        Validates message structure - runs before main validator.
        
        Checks:
        - Messages have required fields
        - Content is not None
        - Role is present
        """
        try:
            messages = context.messages or []
            errors = []
            
            for i, msg in enumerate(messages):
                # Handle both ChatMessage and dict
                if isinstance(msg, ChatMessage):
                    # ChatMessage is always valid structurally (Pydantic enforces it)
                    if msg.role is None:
                        errors.append(f"Message {i} has None role")
                    if msg.content is None:
                        errors.append(f"Message {i} has None content")
                elif isinstance(msg, dict):
                    if 'role' not in msg:
                        errors.append(f"Message {i} missing 'role' field")
                    
                    if 'content' not in msg:
                        errors.append(f"Message {i} missing 'content' field")
                    elif msg['content'] is None:
                        errors.append(f"Message {i} has None content")
                else:
                    errors.append(f"Message {i} is neither ChatMessage nor dict: {type(msg).__name__}")
            
            if errors:
                config = self.get_config()
                strict = config.get('strict_mode', False)
                if strict:
                    return HookResult(
                        success=False,
                        modified=False,
                        context=context,
                        error=f"Structure validation failed: {'; '.join(errors)}"
                    )
                logger.warning(f"Message structure issues (non-strict): {'; '.join(errors)}")
            
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'structure_errors': errors}
            )
            
        except Exception as e:
            logger.exception(f"Error in structure validation: {e}")
            return HookResult(success=False, modified=False, context=context, error=str(e))
    
    def _sanitize_content(self, content: str) -> str:
        """Sanitize message content to remove potentially dangerous patterns."""
        # Remove control characters except newlines and tabs
        content = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', '', content)
        
        # Remove potential script injection patterns
        dangerous_patterns = [
            r'<script[^>]*>.*?</script>',
            r'javascript:',
            r'on\w+\s*=',
        ]
        
        for pattern in dangerous_patterns:
            content = re.sub(pattern, '[removed]', content, flags=re.IGNORECASE | re.DOTALL)
        
        return content
    
    def _enforce_alternating_pattern(self, messages: list[Any]) -> list[Any]:
        """Ensure user/assistant messages alternate (system messages are ignored).
        
        Works with both ChatMessage objects and dicts.
        """
        result = []
        last_role = None
        
        for msg in messages:
            # Get role from ChatMessage or dict
            if isinstance(msg, ChatMessage):
                role = msg.role
            elif isinstance(msg, dict):
                role = msg.get('role', '')
            else:
                # Unknown type, keep it
                result.append(msg)
                continue
            
            # System and tool messages don't break the pattern
            if role in ('system', 'tool'):
                result.append(msg)
                continue
            
            # Skip duplicate consecutive user/assistant roles
            if role == last_role:
                logger.warning(f"Skipping duplicate {role} message to enforce alternating pattern")
                continue
            
            result.append(msg)
            last_role = role
        
        return result
