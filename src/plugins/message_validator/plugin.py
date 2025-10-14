"""
Message Validator Plugin - Reference Hook Plugin Implementation.

This plugin demonstrates how to implement a hooks-only plugin that validates
and sanitizes messages before LLM calls to ensure safety, proper formatting,
and compliance with content policies.

**Plugin Type:** Hook-only (inherits only from PluginHook, NOT MCPServer)

**Hooks Implemented:**
- PRE_LLM_CALL: Validates and sanitizes messages before sending to LLM

**Features:**
- Validates message structure (role, content presence)
- Sanitizes potentially harmful content
- Ensures proper message ordering
- Validates content length limits
- Checks for empty or malformed messages
- Adds validation metadata to hook results
"""

import re
from typing import Any

from agent_system.hooks.plugin_hook import (
    HookContext,
    HookResult,
    PluginHook,
)


class MessageValidatorPlugin(PluginHook):
    """
    Hook plugin that validates and sanitizes messages before LLM calls.
    
    This is a **hook-only** plugin - it does NOT inherit from MCPServer
    because it provides no tools, only lifecycle hooks.
    
    Configuration options:
    - strict_mode: Reject invalid messages vs auto-fix (default: false)
    - max_message_length: Maximum allowed message length (default: 100000)
    - allow_empty_messages: Allow messages with empty content (default: false)
    - sanitize_content: Remove potentially harmful content (default: true)
    - valid_roles: List of valid message roles (default: ['system', 'user', 'assistant', 'function', 'tool'])
    - enforce_alternating: Require alternating user/assistant messages (default: false)
    """
    
    # Default valid roles
    DEFAULT_VALID_ROLES = ['system', 'user', 'assistant', 'function', 'tool']
    
    # Patterns for potentially harmful content
    HARMFUL_PATTERNS = [
        r'<script[^>]*>.*?</script>',  # Script injection
        r'javascript:',                 # JavaScript protocol
        r'on\w+\s*=',                  # Event handlers
    ]
    
    def __init__(self, name: str, config: dict[str, Any] | None = None):
        """
        Initialize the message validator plugin.
        
        Args:
            name: Plugin instance name
            config: Plugin configuration dictionary
        """
        super().__init__(name, config)
        
        # Configuration with defaults
        self.strict_mode = self.config.get('strict_mode', False)
        self.max_message_length = self.config.get('max_message_length', 100000)
        self.allow_empty_messages = self.config.get('allow_empty_messages', False)
        self.sanitize_content = self.config.get('sanitize_content', True)
        self.valid_roles = self.config.get('valid_roles', self.DEFAULT_VALID_ROLES)
        self.enforce_alternating = self.config.get('enforce_alternating', False)
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Validate and sanitize messages before LLM call.
        
        This hook:
        1. Validates message structure (role, content)
        2. Validates message roles are recognized
        3. Sanitizes potentially harmful content
        4. Checks message length limits
        5. Optionally enforces alternating user/assistant pattern
        
        Args:
            context: Hook context with messages and metadata
            
        Returns:
            HookResult with validated/sanitized messages and validation stats
        """
        messages = context.messages or []
        
        if not messages:
            if self.strict_mode:
                return HookResult(
                    success=False,
                    modified=False,
                    context=context,
                    error="No messages provided",
                    metadata={'validation': 'failed', 'reason': 'empty_messages'}
                )
            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'validation': 'skipped', 'reason': 'no_messages'}
            )
        
        # Convert messages to dicts if they aren't already
        message_dicts = [msg if isinstance(msg, dict) else msg.model_dump() for msg in messages]
        
        # Validation statistics
        issues_found = []
        messages_fixed = 0
        
        # Validate and sanitize each message
        validated_messages = []
        
        for i, msg in enumerate(message_dicts):
            try:
                validated_msg, msg_issues = self._validate_message(msg, i)
                
                if msg_issues:
                    issues_found.extend(msg_issues)
                    if validated_msg != msg:
                        messages_fixed += 1
                
                validated_messages.append(validated_msg)
                
            except ValueError as e:
                # Critical validation error
                if self.strict_mode:
                    return HookResult(
                        success=False,
                        modified=False,
                        context=context,
                        error=f"Message validation failed at index {i}: {e}",
                        metadata={'validation': 'failed', 'issues': issues_found}
                    )
                # In non-strict mode, skip invalid message
                issues_found.append(f"Message {i} skipped: {e}")
        
        # Validate message sequence
        if self.enforce_alternating:
            sequence_issues = self._validate_sequence(validated_messages)
            if sequence_issues:
                if self.strict_mode:
                    return HookResult(
                        success=False,
                        modified=False,
                        context=context,
                        error="Message sequence validation failed",
                        metadata={'validation': 'failed', 'issues': sequence_issues}
                    )
                issues_found.extend(sequence_issues)
        
        # Create modified context
        modified_context = HookContext(
            hook_type=context.hook_type,
            request_id=context.request_id,
            session_id=context.session_id,
            agent=context.agent,
            agent_name=context.agent_name,
            messages=validated_messages,
            llm_response=context.llm_response,
            tool_call=context.tool_call,
            tool_result=context.tool_result,
            output=context.output,
            metadata=context.metadata,
            step=context.step,
            llm=context.llm
        )
        
        return HookResult(
            success=True,
            modified=(messages_fixed > 0),
            context=modified_context,
            metadata={
                'validation': {
                    'status': 'passed' if not issues_found else 'passed_with_fixes',
                    'total_messages': len(message_dicts),
                    'validated_messages': len(validated_messages),
                    'messages_fixed': messages_fixed,
                    'issues_found': len(issues_found),
                    'issues': issues_found if issues_found else None
                }
            }
        )
    
    def _validate_message(self, msg: dict, index: int) -> tuple[dict, list[str]]:
        """
        Validate and sanitize a single message.
        
        Args:
            msg: Message dictionary
            index: Message index in conversation
            
        Returns:
            Tuple of (validated_message, list_of_issues)
            
        Raises:
            ValueError: If message is invalid and cannot be fixed
        """
        issues = []
        validated = msg.copy()
        
        # Check for required fields
        if 'role' not in validated:
            if self.strict_mode:
                raise ValueError("Missing required field 'role'")
            validated['role'] = 'user'  # Default to user
            issues.append(f"Message {index}: Added missing 'role' field (defaulted to 'user')")
        
        if 'content' not in validated:
            if self.strict_mode:
                raise ValueError("Missing required field 'content'")
            validated['content'] = ''
            issues.append(f"Message {index}: Added missing 'content' field (empty string)")
        
        # Validate role
        role = validated['role']
        if role not in self.valid_roles:
            if self.strict_mode:
                raise ValueError(f"Invalid role '{role}'. Valid roles: {self.valid_roles}")
            validated['role'] = 'user'
            issues.append(f"Message {index}: Invalid role '{role}' changed to 'user'")
        
        # Validate content
        content = validated['content']
        
        # Handle multimodal content (list of content parts)
        if isinstance(content, list):
            # Just check it's not empty if we don't allow empty
            if not content and not self.allow_empty_messages:
                if self.strict_mode:
                    raise ValueError("Empty multimodal content not allowed")
                issues.append(f"Message {index}: Empty multimodal content")
            return validated, issues
        
        # String content validation
        if not isinstance(content, str):
            if self.strict_mode:
                raise ValueError(f"Content must be string or list, got {type(content)}")
            validated['content'] = str(content)
            issues.append(f"Message {index}: Converted content to string")
            content = validated['content']
        
        # Check empty content
        if not content.strip() and not self.allow_empty_messages:
            if self.strict_mode:
                raise ValueError("Empty content not allowed")
            issues.append(f"Message {index}: Empty content")
        
        # Check length
        if len(content) > self.max_message_length:
            if self.strict_mode:
                raise ValueError(f"Content length {len(content)} exceeds maximum {self.max_message_length}")
            validated['content'] = content[:self.max_message_length] + "\n[... content truncated by validator ...]"
            issues.append(f"Message {index}: Content truncated from {len(content)} to {self.max_message_length} chars")
        
        # Sanitize content
        if self.sanitize_content and isinstance(validated['content'], str):
            original_content = validated['content']
            sanitized_content = self._sanitize_content(original_content)
            
            if sanitized_content != original_content:
                validated['content'] = sanitized_content
                issues.append(f"Message {index}: Potentially harmful content sanitized")
        
        return validated, issues
    
    def _sanitize_content(self, content: str) -> str:
        """
        Sanitize potentially harmful content.
        
        Args:
            content: Message content string
            
        Returns:
            Sanitized content
        """
        sanitized = content
        
        # Remove harmful patterns
        for pattern in self.HARMFUL_PATTERNS:
            sanitized = re.sub(pattern, '[removed]', sanitized, flags=re.IGNORECASE | re.DOTALL)
        
        return sanitized
    
    def _validate_sequence(self, messages: list[dict]) -> list[str]:
        """
        Validate message sequence (e.g., alternating user/assistant).
        
        Args:
            messages: List of validated messages
            
        Returns:
            List of sequence validation issues
        """
        if not self.enforce_alternating:
            return []
        
        issues = []
        prev_role = None
        
        # Filter to only user and assistant messages for alternation check
        conversation_messages = [
            (i, msg) for i, msg in enumerate(messages)
            if msg.get('role') in ('user', 'assistant')
        ]
        
        for i, msg in conversation_messages:
            role = msg.get('role')
            
            if prev_role == role:
                issues.append(f"Messages {i-1} and {i}: Non-alternating {role} messages")
            
            prev_role = role
        
        return issues
