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
import json
from dataclasses import replace, dataclass

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)

# OpenAI tool name pattern requirement
OPENAI_TOOL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

# DeepSeek tool call marker patterns
DEEPSEEK_TOOL_MARKER_PATTERN = re.compile(r'<[｜|]tool[▁_]calls[▁_]begin[｜|]>')
DEEPSEEK_TOOL_CALL_PATTERN = re.compile(
    r'<[｜|]tool[▁_]call[▁_]begin[｜|]>([^<]+)<[｜|]tool[▁_]sep[▁_]?[｜|]>(\{[^}]+\})<[｜|]tool[▁_]call[▁_]end[｜|]>'
)


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
    
    def __init__(self, log_level: str = "warning"):
        self.log_level = log_level.lower()
        
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
        
        # Log message structure for debugging tool_call issues (debug level)
        logger.debug(
            f"[{context}] Validating {len(messages)} messages. "
            f"Last message: role={messages[-1].role}, "
            f"has_tool_calls={bool(messages[-1].tool_calls)}, "
            f"message_roles=[{', '.join(m.role for m in messages[-5:])}]"
        )
        
        issues: List[ValidationIssue] = []
        
        # Run all validation checks
        issues.extend(self._check_tool_call_consistency(messages))
        issues.extend(self._check_tool_names(messages))
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
        # BUT: If the last message is an assistant with tool_calls, don't mark as orphaned
        # because tool responses are expected to be added AFTER this validation (pre_llm_call hook)
        last_msg_is_tool_call = False
        if messages:
            last_msg = messages[-1]
            if last_msg.role == "assistant" and last_msg.tool_calls:
                last_msg_is_tool_call = True
        
        for tool_call_id, msg_idx in pending_tool_calls.items():
            # Only report orphaned if NOT the last message (which expects responses to follow)
            if not (last_msg_is_tool_call and msg_idx == len(messages) - 1):
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
    
    def _check_content_structure(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for malformed or problematic content structures."""
        issues = []
        
        for i, msg in enumerate(messages):
            content = msg.content
            
            # Check for DeepSeek tool markers in content (should be parsed out)
            if content and isinstance(content, str):
                if DEEPSEEK_TOOL_MARKER_PATTERN.search(content):
                    issues.append(ValidationIssue(
                        type="deepseek_tool_markers",
                        severity="error",
                        message_index=i,
                        description="DeepSeek tool markers found in message content (needs parsing)",
                        details={"content_preview": content[:200]}
                    ))
            
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
        
        # Track tool_call_ids that will be removed
        removed_tool_call_ids: Set[str] = set()
        
        for issue in issues:
            if issue.type == "deepseek_tool_markers":
                # Parse DeepSeek markers and convert to tool_calls
                msg_idx = issue.message_index
                if 0 <= msg_idx < len(repaired):
                    msg = repaired[msg_idx]
                    parsed_msg = self._parse_deepseek_markers(msg)
                    if parsed_msg:
                        repaired[msg_idx] = parsed_msg
                        logger.info(f"Parsed DeepSeek tool markers at message {msg_idx}")
                    else:
                        # Parsing failed, remove message
                        remove_indices.add(msg_idx)
                        
            elif issue.type == "orphaned_tool_call":
                # Remove assistant messages with orphaned tool calls
                msg_idx = issue.message_index
                remove_indices.add(msg_idx)
                
                # Collect tool_call_ids from this message
                if 0 <= msg_idx < len(repaired):
                    msg = repaired[msg_idx]
                    if msg.role == "assistant" and msg.tool_calls:
                        for tc in msg.tool_calls:
                            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                            if tc_id:
                                removed_tool_call_ids.add(tc_id)
                
            elif issue.type == "orphaned_tool_response":
                remove_indices.add(issue.message_index)
                
            elif issue.type == "missing_tool_call_id":
                remove_indices.add(issue.message_index)
            
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
                                logger.info(f"Repaired tool name: '{original_name}' -> '{sanitized_name}'")
                    else:
                        remove_indices.add(msg_idx)
        
        # Also remove any tool responses that belong to removed tool_calls (CASCADING REMOVAL)
        if removed_tool_call_ids:
            for i, msg in enumerate(repaired):
                if msg.role == "tool" and hasattr(msg, "tool_call_id"):
                    if msg.tool_call_id in removed_tool_call_ids:
                        remove_indices.add(i)
                        logger.debug(
                            f"Cascading removal: tool response to removed tool_call_id={msg.tool_call_id}"
                        )
        
        # Remove problematic messages (in reverse order to preserve indices)
        for idx in sorted(remove_indices, reverse=True):
            if 0 <= idx < len(repaired):
                repaired.pop(idx)
                
        return repaired
    
    def _parse_deepseek_markers(self, msg: ChatMessage) -> ChatMessage | None:
        """Parse DeepSeek tool call markers and convert to standard tool_calls format.
        
        Args:
            msg: Message potentially containing DeepSeek markers
            
        Returns:
            Modified message with parsed tool_calls, or None if parsing failed
        """
        if not msg.content or not isinstance(msg.content, str):
            return None
        
        content = msg.content
        
        # Check for DeepSeek markers
        if not DEEPSEEK_TOOL_MARKER_PATTERN.search(content):
            return None
        
        # Extract tool calls
        matches = DEEPSEEK_TOOL_CALL_PATTERN.findall(content)
        
        if not matches:
            logger.warning("Found DeepSeek markers but couldn't parse tool calls")
            return None
        
        # Convert to standard tool_calls format
        tool_calls = []
        for idx, (tool_name, args_json) in enumerate(matches):
            tool_name = tool_name.strip()
            
            try:
                # Parse JSON arguments
                args = json.loads(args_json)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse tool arguments for {tool_name}: {e}")
                continue
            
            # Create OpenAI-compatible tool call
            tool_call = {
                "id": f"call_ds_{idx:02d}_{tool_name[:10]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args)
                }
            }
            tool_calls.append(tool_call)
        
        if not tool_calls:
            return None
        
        # Extract clean content (text before tool markers)
        clean_content = DEEPSEEK_TOOL_MARKER_PATTERN.split(content)[0].strip()
        
        logger.info(
            f"Parsed {len(tool_calls)} DeepSeek tool call(s): "
            f"{[tc['function']['name'] for tc in tool_calls]}"
        )
        
        # Create new message with parsed tool_calls
        return ChatMessage(
            role=msg.role,
            content=clean_content or "",
            tool_calls=tool_calls
        )
    
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
    
    def __init__(self, plugin_dir: Path | str):
        """Initialize the message validator plugin.
        
        Args:
            plugin_dir: Directory containing schema.yaml
        """
        super().__init__(plugin_dir)
        
        # Get config
        config = self.get_config()
        log_level = config.get('log_level', 'warning')
        
        # Initialize internal validator
        self.validator = InternalMessageValidator(log_level=log_level)
        
        logger.info("MessageValidatorPlugin initialized with comprehensive validation")
    
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
            
            modified = len(result.issues) > 0
            
            return HookResult(
                success=True,
                modified=modified,
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

    async def parse_deepseek_response(self, context: HookContext) -> HookResult:
        """
        Parse DeepSeek tool call markers from LLM response (post_llm_call hook).
        
        DeepSeek returns tool calls in its native marker format in the content field:
        <｜tool▁calls▁begin｜><｜tool▁call▁begin｜>name<｜tool▁sep｜>{json}<｜tool▁call▁end｜><｜tool▁calls▁end｜>
        
        This hook converts them to OpenAI-compatible tool_calls format.
        """
        try:
            llm_response = context.metadata.get("llm_response", {})
            content = llm_response.get("content", "")
            tool_calls = llm_response.get("tool_calls", [])
            
            # Debug logging - always log to see what we receive
            logger.debug(
                f"[parse_deepseek_response] Response content preview: {content[:100] if content else 'NONE'}, "
                f"has tool_calls: {bool(tool_calls)}"
            )
            
            # Skip if no content or already has tool_calls
            if not content or not isinstance(content, str):
                logger.debug("[parse_deepseek_response] No string content to parse")
                return HookResult(success=True, modified=False, context=context)
            
            if tool_calls:
                logger.debug("[parse_deepseek_response] Already has tool_calls, skipping")
                return HookResult(success=True, modified=False, context=context)
            
            # Check for DeepSeek markers
            if not DEEPSEEK_TOOL_MARKER_PATTERN.search(content):
                logger.debug("[parse_deepseek_response] No DeepSeek markers found")
                return HookResult(success=True, modified=False, context=context)
            
            logger.info("[parse_deepseek_response] ⚠️ DETECTED DeepSeek tool markers in response!")
            logger.debug(f"[parse_deepseek_response] Full content with markers: {content}")
            
            # Extract tool calls from markers
            matches = DEEPSEEK_TOOL_CALL_PATTERN.findall(content)
            if not matches:
                logger.warning("[parse_deepseek_response] DeepSeek markers found but no parseable tool calls")
                return HookResult(success=True, modified=False, context=context)
            
            # Convert to OpenAI format
            parsed_tool_calls = []
            for idx, (tool_name, args_json) in enumerate(matches):
                try:
                    # Parse and re-serialize JSON to ensure valid format
                    args = json.loads(args_json)
                    parsed_tool_calls.append({
                        "id": f"call_ds_{idx:02d}_{tool_name.strip()[:10]}",
                        "type": "function",
                        "function": {
                            "name": tool_name.strip(),
                            "arguments": json.dumps(args)
                        }
                    })
                except json.JSONDecodeError as e:
                    logger.warning(f"[parse_deepseek_response] Failed to parse tool args for {tool_name}: {e}")
                    continue
            
            if not parsed_tool_calls:
                logger.warning("[parse_deepseek_response] No valid tool calls extracted from DeepSeek markers")
                return HookResult(success=True, modified=False, context=context)
            
            # Clean content (remove all marker text)
            clean_content = DEEPSEEK_TOOL_MARKER_PATTERN.split(content)[0].strip()
            
            # Update llm_response
            modified_response = {
                "content": clean_content or "",
                "tool_calls": parsed_tool_calls
            }
            
            logger.info(
                f"[parse_deepseek_response] ✅ Parsed {len(parsed_tool_calls)} DeepSeek tool calls: "
                f"{[tc['function']['name'] for tc in parsed_tool_calls]}"
            )
            
            # Update context with modified response
            modified_context = replace(
                context,
                metadata={**context.metadata, "llm_response": modified_response}
            )
            
            return HookResult(
                success=True,
                modified=True,
                context=modified_context,
                metadata={'parsed_deepseek_tools': len(parsed_tool_calls)}
            )
            
        except Exception as e:
            logger.exception(f"Error in DeepSeek response parsing: {e}")
            return HookResult(success=False, modified=False, context=context, error=str(e))
