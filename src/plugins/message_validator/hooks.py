"""
Message Validator Plugin - Schema-Based Hook Plugin.

Comprehensive message validation and repair before LLM calls.
Hook definitions are loaded from schema.yaml.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- validate_messages: Main validation hook (prPI compliance (tool call consistency, tool name pae_llm_call)
- validate_structure: Structure validation hook (pre_llm_call)

**Features:**
- OpenAI Atterns)
- Validates message structure and role sequences
- Sanitizes message content
- Enforces message format rules
- Auto-repairs common issues (orphaned tool calls, missing tool responses)
- Cascading removal (removes tool responses when removing assistant with tool_calls)
- Configurable validation strictness
"""

from pathlib import Path
from typing import Any, List, Dict, Set
import logging
import re
from dataclasses import replace, dataclass

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)

# Pattern for valid OpenAI tool names (alphanumeric, underscore, hyphen)
OPENAI_TOOL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


@dataclass
class ValidationIssue:
    """Represents a validation issue found in message history."""
    type: str  # "orphaned_tool_call", "missing_assistant", "malformed_content", etc.
    severity: str  # "error", "warning", "info"
    message_index: int
    description: str
    details: Dict[str, Any]


@dataclass
class ValidationResult:
    """Result of message validation with issues and repaired messages."""
    is_valid: bool
    issues: List[ValidationIssue]
    repaired_messages: List[ChatMessage]
    repair_summary: str


class InternalMessageValidator:
    """Internal validator for message sequences - contains all validation logic."""

    def __init__(self, log_level: str = "warning", config: Dict[str, Any] = None):
        self.log_level = log_level.lower()
        self.config = config or {}

    def validate_and_repair(
        self,
        messages: List[ChatMessage],
        context: str = "unknown"
    ) -> ValidationResult:
        """
        Validate message sequence and apply automatic repairs.

        Args:
            messages: List of chat messages to validate
            context: Context identifier for logging

        Returns:
            ValidationResult with issues found and repaired messages
        """
        if not messages:
            return ValidationResult(
                is_valid=True,
                issues=[],
                repaired_messages=[],
                repair_summary="Empty message list"
            )

        issues: List[ValidationIssue] = []

        # Run all validation checks
        issues.extend(self._check_tool_call_consistency(messages))
        issues.extend(self._check_tool_names(messages))
        issues.extend(self._check_tool_response_json(messages))
        # NOTE: Removed _check_tool_response_null_values - null values are legitimate
        issues.extend(self._check_tool_response_size(messages))  # Prevent oversized responses
        issues.extend(self._check_content_structure(messages))
        issues.extend(self._check_message_sequence(messages))

        # Apply repairs if issues found
        repaired_messages = messages
        repair_summary = "No issues found"

        if issues:
            repaired_messages = self._apply_repairs(messages, issues)
            repair_summary = self._generate_repair_summary(issues)
            self._log_validation_issues(issues, context, repair_summary)

        is_valid = not any(issue.severity == "error" for issue in issues)

        return ValidationResult(
            is_valid=is_valid,
            issues=issues,
            repaired_messages=repaired_messages,
            repair_summary=repair_summary
        )

    def _check_tool_call_consistency(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for orphaned tool calls and missing tool responses."""
        issues = []
        pending_tool_calls: Dict[str, int] = {}  # tool_call_id -> message_index

        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                # Register tool calls that need responses
                for tool_call in msg.tool_calls:
                    if hasattr(tool_call, 'id'):
                        pending_tool_calls[tool_call.id] = i
                    elif isinstance(tool_call, dict) and 'id' in tool_call:
                        pending_tool_calls[tool_call['id']] = i

            elif msg.role == "tool":
                # Check if this tool response has a matching call
                tool_call_id = getattr(msg, 'tool_call_id', None)
                if not tool_call_id and hasattr(msg, 'model_dump'):
                    # Try extracting from dict representation
                    msg_dict = msg.model_dump()
                    tool_call_id = msg_dict.get('tool_call_id')

                if tool_call_id:
                    if tool_call_id in pending_tool_calls:
                        # Found matching tool call
                        del pending_tool_calls[tool_call_id]
                    else:
                        # Orphaned tool response
                        issues.append(ValidationIssue(
                            type="orphaned_tool_response",
                            severity="warning",
                            message_index=i,
                            description=f"Tool response with id '{tool_call_id}' has no matching assistant tool call",
                            details={"tool_call_id": tool_call_id}
                        ))
                else:
                    # Tool message without tool_call_id
                    issues.append(ValidationIssue(
                        type="missing_tool_call_id",
                        severity="error",
                        message_index=i,
                        description="Tool message missing tool_call_id",
                        details={"content_preview": str(msg.content)[:100]}
                    ))

        # Report any remaining pending tool calls (orphaned)
        # CRITICAL FIX: ALL pending tool_calls without responses are orphaned!
        # OpenAI API requires: "An assistant message with 'tool_calls' must be followed
        # by tool messages responding to each 'tool_call_id'"
        #
        # This happens when:
        # 1. Previous LLM call made tool_calls but they were interrupted (cancelled)
        # 2. Conversation history was persisted with orphaned tool_calls
        # 3. New LLM call tries to use this invalid history
        #
        # We MUST remove the tool_calls from the assistant message to make history valid.

        for tool_call_id, msg_idx in pending_tool_calls.items():
            logger.warning(
                f"Orphaned tool_call detected: id={tool_call_id}, msg_idx={msg_idx}"
            )
            issues.append(ValidationIssue(
                type="orphaned_tool_call",
                severity="error",
                message_index=msg_idx,
                description=f"Assistant tool call '{tool_call_id}' has no corresponding tool response",
                details={"tool_call_id": tool_call_id}
            ))

        return issues

    def _check_tool_names(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check that tool names comply with OpenAI's naming requirements."""
        issues = []

        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                for tool_idx, tool_call in enumerate(msg.tool_calls):
                    # Extract tool name from various formats
                    tool_name = None
                    if hasattr(tool_call, 'function') and hasattr(tool_call.function, 'name'):
                        tool_name = tool_call.function.name
                    elif isinstance(tool_call, dict):
                        if 'function' in tool_call and isinstance(tool_call['function'], dict):
                            tool_name = tool_call['function'].get('name')

                    if tool_name and not OPENAI_TOOL_NAME_PATTERN.match(tool_name):
                        issues.append(ValidationIssue(
                            type="invalid_tool_name",
                            severity="error",
                            message_index=i,
                            description=f"Tool name '{tool_name}' does not match OpenAI pattern ^[a-zA-Z0-9_-]+$",
                            details={
                                "tool_name": tool_name,
                                "tool_index": tool_idx,
                                "pattern": "^[a-zA-Z0-9_-]+$"
                            }
                        ))

        return issues

    def _check_tool_response_json(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check that tool responses are well-formed.
        
        Note: Tool response content can be:
        - A JSON object (most common)
        - A JSON string (e.g., paginated JSON content from writer_content)
        - A plain string (descriptive text)
        
        We only flag truly malformed content, not valid strings.
        """
        issues = []

        for i, msg in enumerate(messages):
            if msg.role == "tool":
                content = msg.content
                if not content:
                    continue
                
                # Tool response content can be:
                # 1. Valid JSON object/array -> OK
                # 2. Valid JSON string -> OK (e.g., paginated JSON as string)
                # 3. Plain string that's not JSON -> OK (descriptive text)
                # 4. Malformed (e.g., truncated JSON, encoding issues) -> Warning
                
                # We only check for obvious malformation patterns, not JSON validity
                # because string content is perfectly valid for tool responses
                
                # Check for common malformation indicators
                content_str = str(content)
                
                # Check for truncated JSON (starts with { or [ but doesn't close)
                if content_str.strip().startswith('{') or content_str.strip().startswith('['):
                    # Looks like JSON - check if it's valid
                    import json
                    try:
                        json.loads(content_str)
                        # Valid JSON - OK
                    except json.JSONDecodeError as e:
                        # Only warn if it LOOKS like JSON but is malformed
                        # (truncated, missing quotes, etc.)
                        issues.append(ValidationIssue(
                            type="invalid_tool_response_json",
                            severity="warning",
                            message_index=i,
                            description=f"Tool response appears to be malformed JSON: {str(e)}",
                            details={
                                "tool_call_id": getattr(msg, 'tool_call_id', None),
                                "error": str(e),
                                "content_preview": content_str[:100]
                            }
                        ))
                # Non-JSON string content is perfectly valid - no issue

        return issues

    # NOTE: _check_tool_response_null_values was removed - null values are legitimate JSON values

    def _check_tool_response_size(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for oversized tool responses that may cause LLM issues.
        
        Very large tool responses (>50KB) can cause:
        - Token limit issues
        - Parsing errors
        - MALFORMED_FUNCTION_CALL errors with some LLMs
        """
        issues = []
        
        # Get size limits from config (in KB, convert to bytes)
        MAX_SIZE_BYTES = self.config.get("max_tool_response_size_kb", 50) * 1024
        WARN_SIZE_BYTES = self.config.get("warn_tool_response_size_kb", 20) * 1024

        for i, msg in enumerate(messages):
            if msg.role == "tool":
                content = msg.content
                if not content:
                    continue
                
                content_size = len(content.encode('utf-8'))
                
                if content_size > MAX_SIZE_BYTES:
                    tool_name = getattr(msg, 'name', 'unknown')
                    issues.append(ValidationIssue(
                        type="tool_response_too_large",
                        severity="error",
                        message_index=i,
                        description=f"Tool response from '{tool_name}' is {content_size/1024:.1f}KB (>{MAX_SIZE_BYTES/1024}KB) - may cause LLM errors",
                        details={
                            "tool_name": tool_name,
                            "size_bytes": content_size,
                            "size_kb": round(content_size/1024, 1),
                            "tool_call_id": getattr(msg, 'tool_call_id', None)
                        }
                    ))
                elif content_size > WARN_SIZE_BYTES:
                    tool_name = getattr(msg, 'name', 'unknown')
                    issues.append(ValidationIssue(
                        type="tool_response_large",
                        severity="warning",
                        message_index=i,
                        description=f"Tool response from '{tool_name}' is large ({content_size/1024:.1f}KB) - consider pagination",
                        details={
                            "tool_name": tool_name,
                            "size_bytes": content_size,
                            "size_kb": round(content_size/1024, 1),
                            "tool_call_id": getattr(msg, 'tool_call_id', None)
                        }
                    ))

        return issues

    def _check_content_structure(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for malformed or problematic content structures."""
        issues = []

        for i, msg in enumerate(messages):
            content = msg.content

            if content is None and not msg.tool_calls and msg.role == "assistant":
                issues.append(ValidationIssue(
                    type="empty_assistant_message",
                    severity="warning",
                    message_index=i,
                    description="Assistant message with no content and no tool calls",
                    details={"role": msg.role}
                ))

        return issues

    def _check_message_sequence(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for problematic message sequences."""
        issues = []

        # Check for consecutive assistant messages
        for i in range(len(messages) - 1):
            if messages[i].role == "assistant" and messages[i + 1].role == "assistant":
                issues.append(ValidationIssue(
                    type="consecutive_assistant_messages",
                    severity="warning",
                    message_index=i,
                    description="Two consecutive assistant messages found",
                    details={"next_index": i + 1}
                ))

        return issues

    def _apply_repairs(self, messages: List[ChatMessage], issues: List[ValidationIssue]) -> List[ChatMessage]:
        """Apply automatic repairs to fix validation issues."""
        repaired = messages.copy()

        # Collect indices of messages to remove
        remove_indices: Set[int] = set()

        # Track which assistant messages need their tool_calls removed
        messages_to_strip_tool_calls: Set[int] = set()

        # Track consecutive assistant messages to merge (first_idx -> second_idx)
        consecutive_assistant_merges: Dict[int, int] = {}

        for issue in issues:
            if issue.type == "orphaned_tool_call":
                # Remove tool_calls from assistant message instead of adding fake responses
                # This is cleaner and avoids sending fake tool execution results to LLM
                msg_idx = issue.message_index
                messages_to_strip_tool_calls.add(msg_idx)

            elif issue.type == "orphaned_tool_response":
                remove_indices.add(issue.message_index)

            elif issue.type == "missing_tool_call_id":
                remove_indices.add(issue.message_index)

            elif issue.type == "empty_assistant_message":
                # Remove empty assistant messages - they serve no purpose and can 
                # confuse the LLM (especially when followed by user "Continue" messages)
                remove_indices.add(issue.message_index)

            elif issue.type == "consecutive_assistant_messages":
                # Merge second assistant message into first
                first_idx = issue.message_index
                second_idx = issue.details.get("next_index", first_idx + 1)
                consecutive_assistant_merges[first_idx] = second_idx

            elif issue.type == "invalid_tool_name":
                # Try to repair invalid tool names
                msg_idx = issue.message_index
                if 0 <= msg_idx < len(repaired):
                    msg = repaired[msg_idx]
                    original_name = issue.details.get("tool_name", "")
                    sanitized_name = self._sanitize_tool_name(original_name)

                    if sanitized_name and OPENAI_TOOL_NAME_PATTERN.match(sanitized_name):
                        # Apply repair
                        if msg.role == "assistant" and msg.tool_calls:
                            tool_idx = issue.details.get("tool_index", 0)
                            if tool_idx < len(msg.tool_calls):
                                tool_call = msg.tool_calls[tool_idx]
                                if hasattr(tool_call, 'function'):
                                    tool_call.function.name = sanitized_name
                    else:
                        remove_indices.add(msg_idx)

        # FIRST: Merge consecutive assistant messages (before any removals)
        # Process in reverse order to handle multiple consecutive pairs correctly
        for first_idx in sorted(consecutive_assistant_merges.keys(), reverse=True):
            second_idx = consecutive_assistant_merges[first_idx]
            if 0 <= first_idx < len(repaired) and 0 <= second_idx < len(repaired):
                first_msg = repaired[first_idx]
                second_msg = repaired[second_idx]
                
                # Merge content: combine both contents with separator
                first_content = first_msg.content or ""
                second_content = second_msg.content or ""
                merged_content = first_content
                if second_content:
                    if merged_content:
                        merged_content = f"{merged_content}\n\n{second_content}"
                    else:
                        merged_content = second_content
                
                # Merge tool_calls: combine both lists
                merged_tool_calls = []
                if first_msg.tool_calls:
                    merged_tool_calls.extend(first_msg.tool_calls)
                if second_msg.tool_calls:
                    merged_tool_calls.extend(second_msg.tool_calls)
                
                # Merge reasoning_content (important for Gemini "thinking")
                first_reasoning = getattr(first_msg, 'reasoning_content', None) or ""
                second_reasoning = getattr(second_msg, 'reasoning_content', None) or ""
                merged_reasoning = first_reasoning
                if second_reasoning:
                    if merged_reasoning:
                        merged_reasoning = f"{merged_reasoning}\n\n{second_reasoning}"
                    else:
                        merged_reasoning = second_reasoning
                
                # Merge multimodal_content: combine both lists
                merged_multimodal = []
                if getattr(first_msg, 'multimodal_content', None):
                    merged_multimodal.extend(first_msg.multimodal_content)
                if getattr(second_msg, 'multimodal_content', None):
                    merged_multimodal.extend(second_msg.multimodal_content)
                
                # Create merged message preserving all fields
                repaired[first_idx] = ChatMessage(
                    role="assistant",
                    content=merged_content if merged_content else None,
                    tool_calls=merged_tool_calls if merged_tool_calls else None,
                    name=first_msg.name if hasattr(first_msg, 'name') else None,
                    timestamp=first_msg.timestamp if hasattr(first_msg, 'timestamp') else None,
                    reasoning_content=merged_reasoning if merged_reasoning else None,
                    multimodal_content=merged_multimodal if merged_multimodal else None,
                    content_format=first_msg.content_format or second_msg.content_format
                )
                
                # Mark second message for removal
                remove_indices.add(second_idx)
                logger.debug(f"Merged consecutive assistant messages at indices {first_idx} and {second_idx}")

        # Remove problematic messages (in reverse order to preserve indices)
        for idx in sorted(remove_indices, reverse=True):
            if 0 <= idx < len(repaired):
                repaired.pop(idx)

        # Strip tool_calls from assistant messages with orphaned calls
        # We do this AFTER removals to work with adjusted indices
        for msg_idx in messages_to_strip_tool_calls:
            # Adjust index after removals
            adjusted_idx = msg_idx
            for removed_idx in sorted(remove_indices):
                if removed_idx < msg_idx:
                    adjusted_idx -= 1

            if 0 <= adjusted_idx < len(repaired):
                msg = repaired[adjusted_idx]
                if msg.role == "assistant" and msg.tool_calls:
                    # Create a new message without tool_calls
                    # Preserve content if it exists
                    new_content = msg.content if msg.content else "Tool execution was interrupted"

                    # Create new ChatMessage without tool_calls
                    repaired[adjusted_idx] = ChatMessage(
                        role="assistant",
                        content=new_content,
                        name=msg.name if hasattr(msg, 'name') else None
                    )

        return repaired


    def _sanitize_tool_name(self, name: str) -> str:
        """Attempt to sanitize an invalid tool name."""
        if not name:
            return ""

        sanitized = name.replace("/", "__")
        sanitized = sanitized.replace(".", "_")
        sanitized = sanitized.replace(" ", "_")
        sanitized = re.sub(r'[^a-zA-Z0-9_-]', '', sanitized)
        sanitized = re.sub(r'_{3,}', '__', sanitized)
        sanitized = sanitized.strip('_')

        return sanitized

    def _generate_repair_summary(self, issues: List[ValidationIssue]) -> str:
        """Generate human-readable repair summary."""
        if not issues:
            return "No repairs needed"

        error_count = len([i for i in issues if i.severity == "error"])
        warning_count = len([i for i in issues if i.severity == "warning"])

        issue_types = {}
        for issue in issues:
            issue_types[issue.type] = issue_types.get(issue.type, 0) + 1

        type_summary = ", ".join([f"{count} {issue_type}" for issue_type, count in issue_types.items()])

        parts = []
        if error_count:
            parts.append(f"{error_count} errors")
        if warning_count:
            parts.append(f"{warning_count} warnings")

        return f"Found {', '.join(parts)}: {type_summary}"

    def _log_validation_issues(self, issues: List[ValidationIssue], context: str, repair_summary: str):
        """Log validation issues with appropriate detail level."""
        if not issues:
            return

        error_issues = [i for i in issues if i.severity == "error"]
        warning_issues = [i for i in issues if i.severity == "warning"]

        if error_issues or warning_issues:
            logger.warning(
                f"Message validation auto-repaired {len(issues)} issues in {context}: {repair_summary}"
            )


class MessageValidatorPlugin(SchemaBasedPluginHook):
    """
    Schema-based hook plugin for message validation.

    Hook definitions and configuration are loaded from schema.yaml.
    """

    def __init__(self, plugin_dir: Path | str, mcp_config: Any = None):
        """Initialize the message validator plugin.

        Args:
            plugin_dir: Directory containing schema.yaml
            mcp_config: MCP configuration (contains config from plugins.yaml)
        """
        super().__init__(plugin_dir)

        # Get config from schema defaults
        config = self.get_config()
        
        # Merge with mcp_config.config if provided (overrides schema defaults)
        if mcp_config and hasattr(mcp_config, 'config') and mcp_config.config:
            config.update(mcp_config.config)
        
        log_level = config.get('log_level', 'warning')

        # Initialize internal validator with merged config
        self.validator = InternalMessageValidator(log_level=log_level, config=config)

    async def validate_messages(self, context: HookContext) -> HookResult:
        """
        Main validation hook - validates and repairs messages.

        Performs:
        1. Tool call consistency checking (orphaned calls/responses)
        2. Tool name validation (OpenAI API compliance)
        3. Content structure validation
        4. Message sequence validation
        5. Auto-repairs with cascading removal
        """
        try:
            messages = context.messages or []

            if not messages:
                return HookResult(success=True, modified=False, context=context)

            # Convert dict messages to ChatMessage objects if needed
            chat_messages = []
            for msg in messages:
                if isinstance(msg, dict):
                    chat_messages.append(ChatMessage(**msg))
                else:
                    chat_messages.append(msg)

            # Run validation and repair
            result = self.validator.validate_and_repair(chat_messages, context="message_validator")

            # Update context with repaired messages (keep as ChatMessage objects!)
            modified_context = replace(context, messages=result.repaired_messages)

            # Detect if messages were actually modified
            # Note: We can't use identity comparison (is) because hooks receive deep-copied contexts,
            # so we need to compare content/structure instead

            if len(result.repaired_messages) != len(chat_messages):
                # Different lengths = definitely modified
                modified = True
            else:
                # Same length - compare message content and structure
                # Deep comparison of all message fields
                modified = False
                for orig_msg, repaired_msg in zip(chat_messages, result.repaired_messages):
                    # Compare all relevant fields
                    if (orig_msg.role != repaired_msg.role or
                        orig_msg.content != repaired_msg.content or
                        orig_msg.tool_calls != repaired_msg.tool_calls or
                        orig_msg.tool_call_id != repaired_msg.tool_call_id or
                        orig_msg.name != repaired_msg.name):
                        modified = True
                        break

            # Log when messages were repaired
            if modified and result.issues:
                logger.warning(
                    f"Hook 'message_validator.validate_messages' repaired {len(result.issues)} issues: "
                    f"{result.repair_summary}"
                )

            # CRITICAL: We must set modified=True if validator made ANY repairs,
            # otherwise the hook registry will ignore our repaired messages!
            # The validator always returns repaired messages (even if identical),
            # so we need to explicitly signal when repairs were made.
            actually_modified = modified or bool(result.issues)

            return HookResult(
                success=True,
                modified=actually_modified,
                context=modified_context,
                metadata={
                    'validation_result': result.repair_summary,
                    'issues_count': len(result.issues)
                }
            )
        except Exception as e:
            logger.exception(f"Error in message validation: {e}")
            return HookResult(success=False, modified=False, context=context, error=str(e))

    async def validate_structure(self, context: HookContext) -> HookResult:
        """
        Validates message structure - runs before main validator.

        Checks:
        - Messages have required fields
        - Basic structure integrity
        """
        try:
            messages = context.messages or []

            if not messages:
                return HookResult(success=True, modified=False, context=context)

            # Get config
            config = self.get_config()
            strict_mode = config.get('strict_mode', False)

            errors = []

            for i, msg in enumerate(messages):
                # Check basic structure
                if isinstance(msg, ChatMessage):
                    if not hasattr(msg, 'role') or not msg.role:
                        error = f"Message {i} missing role"
                        if strict_mode:
                            return HookResult(success=False, modified=False, context=context, error=error)
                        errors.append(error)
                elif isinstance(msg, dict):
                    if 'role' not in msg or not msg['role']:
                        error = f"Message {i} missing role"
                        if strict_mode:
                            return HookResult(success=False, modified=False, context=context, error=error)
                        errors.append(error)

            if errors:
                logger.warning(f"Structure validation issues: {'; '.join(errors)}")

            return HookResult(
                success=True,
                modified=False,
                context=context,
                metadata={'structure_errors': errors}
            )

        except Exception as e:
            logger.exception(f"Error in structure validation: {e}")
            return HookResult(success=False, modified=False, context=context, error=str(e))
