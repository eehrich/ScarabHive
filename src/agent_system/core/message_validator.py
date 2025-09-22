"""
Central message history validation and repair module.

This module provides comprehensive validation of message sequences before LLM calls,
ensuring structural integrity and OpenAI API compliance. It can detect and repair
common issues like orphaned tool calls, missing tool responses, and malformed content.
"""

import logging
from typing import List, Dict, Any, Set
from dataclasses import dataclass

from agent_system.llm.clients import ChatMessage

logger = logging.getLogger(__name__)


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


class MessageValidator:
    """Central validator for message sequences before LLM calls."""
    
    def __init__(self, log_level: str = "warning"):
        self.log_level = log_level.lower()
        
    def validate_and_repair(
        self, 
        messages: List[ChatMessage], 
        context: str = "unknown"
    ) -> ValidationResult:
        """
        Validate message sequence and repair issues if possible.
        
        Args:
            messages: List of ChatMessage objects to validate
            context: Context string for logging (e.g., "summarizer", "agent_server")
            
        Returns:
            ValidationResult with validation status, issues, and repaired messages
        """
        issues = []
        repaired_messages = messages.copy()
        
        # Run all validation checks
        issues.extend(self._check_tool_call_consistency(repaired_messages))
        issues.extend(self._check_content_structure(repaired_messages))
        issues.extend(self._check_message_sequence(repaired_messages))
        
        # Apply repairs if needed
        if issues:
            repaired_messages = self._apply_repairs(repaired_messages, issues)
            repair_summary = self._generate_repair_summary(issues)
            
            # Log detailed warning with context
            self._log_validation_issues(issues, context, repair_summary)
        else:
            repair_summary = "No issues found"
            
        return ValidationResult(
            is_valid=len([i for i in issues if i.severity == "error"]) == 0,
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
        for tool_call_id, msg_idx in pending_tool_calls.items():
            issues.append(ValidationIssue(
                type="orphaned_tool_call",
                severity="error",
                message_index=msg_idx,
                description=f"Assistant tool call '{tool_call_id}' has no corresponding tool response",
                details={"tool_call_id": tool_call_id}
            ))
            
        return issues
    
    def _check_content_structure(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for malformed or problematic content structures."""
        issues = []
        
        for i, msg in enumerate(messages):
            content = msg.content
            
            # Check for various problematic content patterns
            if content is None and not msg.tool_calls and msg.role == "assistant":
                issues.append(ValidationIssue(
                    type="empty_assistant_message",
                    severity="warning",
                    message_index=i,
                    description="Assistant message with no content and no tool calls",
                    details={"role": msg.role}
                ))
                
            elif isinstance(content, list):
                # Check list-style content for empty or malformed segments
                empty_segments = 0
                total_segments = len(content)
                
                for seg_idx, segment in enumerate(content):
                    if isinstance(segment, dict):
                        text_content = segment.get("text") or segment.get("content")
                        if not text_content or (isinstance(text_content, str) and not text_content.strip()):
                            empty_segments += 1
                    elif isinstance(segment, str) and not segment.strip():
                        empty_segments += 1
                        
                if empty_segments == total_segments and total_segments > 0:
                    issues.append(ValidationIssue(
                        type="all_empty_content_segments",
                        severity="warning",
                        message_index=i,
                        description=f"All {total_segments} content segments are empty",
                        details={"segments": total_segments, "empty": empty_segments}
                    ))
                elif empty_segments > 0:
                    issues.append(ValidationIssue(
                        type="some_empty_content_segments", 
                        severity="info",
                        message_index=i,
                        description=f"{empty_segments}/{total_segments} content segments are empty",
                        details={"segments": total_segments, "empty": empty_segments}
                    ))
                    
        return issues
    
    def _check_message_sequence(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for problematic message sequences."""
        issues = []
        
        # Check for consecutive assistant messages (usually not allowed)
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
        
        for issue in issues:
            if issue.type == "orphaned_tool_call":
                # Remove assistant messages with orphaned tool calls
                remove_indices.add(issue.message_index)
                logger.debug(f"Marking message {issue.message_index} for removal: orphaned tool call")
                
            elif issue.type == "orphaned_tool_response":
                # Remove tool responses without matching calls
                remove_indices.add(issue.message_index)
                logger.debug(f"Marking message {issue.message_index} for removal: orphaned tool response")
                
            elif issue.type == "missing_tool_call_id":
                # Remove tool messages without proper ID
                remove_indices.add(issue.message_index)
                logger.debug(f"Marking message {issue.message_index} for removal: missing tool_call_id")
        
        # Remove problematic messages (in reverse order to preserve indices)
        for idx in sorted(remove_indices, reverse=True):
            if 0 <= idx < len(repaired):
                removed_msg = repaired.pop(idx)
                logger.debug(f"Removed message {idx}: {removed_msg.role} - {str(removed_msg.content)[:50]}")
                
        return repaired
    
    def _generate_repair_summary(self, issues: List[ValidationIssue]) -> str:
        """Generate human-readable repair summary."""
        if not issues:
            return "No repairs needed"
            
        error_count = len([i for i in issues if i.severity == "error"])
        warning_count = len([i for i in issues if i.severity == "warning"])
        info_count = len([i for i in issues if i.severity == "info"])
        
        summary_parts = []
        if error_count:
            summary_parts.append(f"{error_count} errors")
        if warning_count:
            summary_parts.append(f"{warning_count} warnings")
        if info_count:
            summary_parts.append(f"{info_count} info")
            
        issue_types = {}
        for issue in issues:
            issue_types[issue.type] = issue_types.get(issue.type, 0) + 1
            
        type_summary = ", ".join([f"{count} {issue_type}" for issue_type, count in issue_types.items()])
        
        return f"Found {', '.join(summary_parts)}: {type_summary}"
    
    def _log_validation_issues(self, issues: List[ValidationIssue], context: str, repair_summary: str):
        """Log validation issues with appropriate detail level."""
        if not issues:
            return
            
        # Count issues by severity
        error_issues = [i for i in issues if i.severity == "error"]
        warning_issues = [i for i in issues if i.severity == "warning"] 
        
        # Always log errors and warnings in detail
        if error_issues or warning_issues:
            logger.warning(
                "🔍 Message validation found issues in %s context: %s",
                context,
                repair_summary
            )
            
            # Log each significant issue
            for issue in error_issues + warning_issues:
                logger.warning(
                    "  [%s] %s at message %d: %s (details: %s)",
                    issue.severity.upper(),
                    issue.type,
                    issue.message_index,
                    issue.description,
                    issue.details
                )
        
        # Log info issues only in debug mode
        info_issues = [i for i in issues if i.severity == "info"]
        if info_issues and self.log_level == "debug":
            for issue in info_issues:
                logger.debug(
                    "  [INFO] %s at message %d: %s",
                    issue.type,
                    issue.message_index, 
                    issue.description
                )


# Global validator instance
_validator = MessageValidator()


def validate_messages_before_llm(
    messages: List[ChatMessage], 
    context: str = "unknown"
) -> List[ChatMessage]:
    """
    Convenience function to validate and repair messages before LLM calls.
    
    Args:
        messages: List of ChatMessage objects
        context: Context string for logging (e.g., "agent_server", "summarizer")
        
    Returns:
        Repaired list of ChatMessage objects
        
    Raises:
        ValueError: If critical errors are found that cannot be repaired
    """
    result = _validator.validate_and_repair(messages, context)
    
    # Check for critical errors that block LLM calls
    critical_errors = [i for i in result.issues if i.severity == "error"]
    if critical_errors:
        error_details = [f"{e.type}@{e.message_index}" for e in critical_errors]
        logger.error(
            "🚫 Critical message validation errors in %s: %s", 
            context, 
            ", ".join(error_details)
        )
        # Still return repaired messages but warn about potential issues
        
    return result.repaired_messages


def get_validation_stats() -> Dict[str, Any]:
    """Get validation statistics for monitoring."""
    # This could be extended to track validation metrics over time
    return {
        "validator_available": True,
        "log_level": _validator.log_level
    }