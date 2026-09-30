"""
Message Validator Plugin - Schema-Based Hook Plugin.

Comprehensive message validation and repair before LLM calls.
Hook definitions are loaded from schema.yaml.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- validate_messages: checks and repairs the history (pre_llm_call)
- validate_structure: logs messages without a role (pre_llm_call)

Repairs structure only (tool-call pairing, tool names, message order);
content is never rewritten, except that merged assistant turns are joined.
"""

from pathlib import Path
from typing import Any, List, Dict, Optional, Set
import logging
import re
from dataclasses import replace, dataclass

from agent_system.hooks import (
    SchemaBasedPluginHook,
    HookContext,
    HookResult,
)
from agent_system.llm.message_roles import INSTRUCTION_ROLES, is_input
from agent_system.llm.models import ChatMessage
from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts

logger = logging.getLogger(__name__)

# Pattern for valid OpenAI tool names (alphanumeric, underscore, hyphen)
OPENAI_TOOL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

# Check and repair passes per call; one repair can expose another issue.
MAX_REPAIR_PASSES = 3

# Issues that are logged, never repaired.
REPORT_ONLY_ISSUES = frozenset({
    "invalid_tool_response_json", "tool_response_too_large", "tool_response_large",
})

INTERRUPTED_PLACEHOLDER = "Tool execution was interrupted"


def _tool_call_id(tool_call: Any) -> Optional[str]:
    """The id of a tool call (dict, or an object with ``id``)."""
    if isinstance(tool_call, dict):
        return tool_call.get("id")
    return getattr(tool_call, "id", None)


def _tool_call_id_of(msg: ChatMessage) -> Optional[str]:
    """The ``tool_call_id`` a tool message answers."""
    return getattr(msg, "tool_call_id", None)


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

    def __init__(self, config: Dict[str, Any] = None):
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
        repaired_messages = messages
        repair_summary = "No issues found"

        # Check and repair until nothing changes: a repair can expose an
        # issue (a removal lets two assistant turns meet). Fixing it in the
        # same call breaks the prompt cache once instead of on two calls.
        for pass_no in range(MAX_REPAIR_PASSES):
            found = self._run_checks(repaired_messages)
            if pass_no:
                # Report-only issues were counted in the first pass already.
                found = [i for i in found if i.type not in REPORT_ONLY_ISSUES]
            issues.extend(found)
            if not any(i.type not in REPORT_ONLY_ISSUES for i in found):
                break
            before = repaired_messages
            repaired_messages = self._apply_repairs(before, found)
            if len(before) == len(repaired_messages) and all(
                    a is b for a, b in zip(before, repaired_messages)):
                break

        if issues:
            repair_summary = self._generate_repair_summary(issues)
            self._log_validation_issues(issues, context, repair_summary)

        is_valid = not any(issue.severity == "error" for issue in issues)

        return ValidationResult(
            is_valid=is_valid,
            issues=issues,
            repaired_messages=repaired_messages,
            repair_summary=repair_summary
        )

    def _run_checks(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        issues.extend(self._check_tool_call_consistency(messages))
        issues.extend(self._check_tool_names(messages))
        issues.extend(self._check_tool_response_json(messages))
        issues.extend(self._check_tool_response_size(messages))
        issues.extend(self._check_content_structure(messages))
        issues.extend(self._check_message_sequence(messages))
        return issues

    def _check_tool_call_consistency(self, messages: List[ChatMessage]) -> List[ValidationIssue]:
        """Check for orphaned tool calls and missing tool responses.

        Pairing is per block: a tool answer counts only for the calls of the
        latest assistant message with tool calls. A new one closes the block,
        and what is still unanswered there is orphaned -- providers want the
        answers right after their calls, and a call id may repeat across turns.
        """
        issues = []
        # tool_call_id -> [(message_index, tool_index)] still unanswered in the open block
        pending_tool_calls: Dict[Any, List[tuple]] = {}

        def close_block():
            for tool_call_id, calls in pending_tool_calls.items():
                for msg_idx, tool_idx in calls:
                    logger.warning(
                        f"Orphaned tool_call detected: id={tool_call_id}, msg_idx={msg_idx}"
                    )
                    issues.append(ValidationIssue(
                        type="orphaned_tool_call",
                        severity="error",
                        message_index=msg_idx,
                        description=f"Assistant tool call '{tool_call_id}' has no corresponding tool response",
                        details={"tool_call_id": tool_call_id, "tool_index": tool_idx}
                    ))
            pending_tool_calls.clear()

        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                close_block()
                # A call without an id can never be answered: pending under None.
                # A call whose name cannot be made valid is dropped by the
                # tool-name repair; it takes no answer, so a duplicate id's
                # answer goes to the call that stays.
                for tool_idx, tool_call in enumerate(msg.tool_calls):
                    if self._name_is_unusable(tool_call):
                        continue
                    pending_tool_calls.setdefault(_tool_call_id(tool_call), []).append((i, tool_idx))

            elif msg.role == "tool":
                # Check if this tool response has a matching call
                tool_call_id = getattr(msg, 'tool_call_id', None)
                if not tool_call_id and hasattr(msg, 'model_dump'):
                    # Try extracting from dict representation
                    msg_dict = msg.model_dump()
                    tool_call_id = msg_dict.get('tool_call_id')

                if tool_call_id:
                    if pending_tool_calls.get(tool_call_id):
                        # Found matching tool call
                        pending_tool_calls[tool_call_id].pop(0)
                        if not pending_tool_calls[tool_call_id]:
                            del pending_tool_calls[tool_call_id]
                    else:
                        # Orphaned tool response
                        issues.append(ValidationIssue(
                            type="orphaned_tool_response",
                            severity="warning",
                            message_index=i,
                            description=f"Tool response with id '{tool_call_id}' has no open assistant tool call",
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

        # OpenAI API requires: "An assistant message with 'tool_calls' must be
        # followed by tool messages responding to each 'tool_call_id'". Calls
        # left unanswered (a cancelled run, a blocked call) are removed.
        close_block()
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

            # Check for empty assistant messages (None, empty string, or whitespace-only)
            if msg.role == "assistant" and not msg.tool_calls:
                if content is None or (isinstance(content, str) and not content.strip()):
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

        # Check that the conversation opens on something the model is asked to
        # act on. Gemini requires: user -> assistant (with tool_calls) -> tool
        # responses. A developer NOTE is an instruction, not the start of the
        # conversation -- counting it as one made the repair below delete it.
        # A developer WAKE is the opposite: it is a woken run's whole task, and
        # every route that needs a user turn first lowers it to one. `is_input`
        # tells the two apart, the same question compaction's copy of this rule
        # asks (context_engineer/compaction._ensure_valid_message_sequence).
        for i, msg in enumerate(messages):
            if msg.role not in INSTRUCTION_ROLES or is_input(msg):
                if not is_input(msg):
                    issues.append(ValidationIssue(
                        type="invalid_first_message",
                        severity="error",
                        message_index=i,
                        description=f"First non-system message must be 'user', got '{msg.role}'",
                        details={"actual_role": msg.role}
                    ))
                break  # Only check first non-system message

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

        # Check for non-tool messages interleaved between assistant(tool_calls) and
        # their tool responses.  The OpenAI/DeepSeek API requires that an assistant
        # message containing tool_calls is *immediately* followed by all corresponding
        # tool-role messages before any other role appears.
        # Example of broken sequence:
        #   assistant(tool_calls=[id1]) -> user("loop warning") -> tool(id1)
        # Repair: move the interleaved message(s) to after the tool responses.
        # Only a message an answer of the same block still follows counts;
        # after a call that is never answered there is nothing to move.
        pending: Dict[Any, int] = {}             # open block: call id -> unanswered count
        block_start: Optional[int] = None        # index of assistant msg that opened the block
        waiting: List[int] = []                  # non-tool messages seen inside the open block
        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                pending = {}
                for tc in msg.tool_calls:
                    tc_id = _tool_call_id(tc)
                    pending[tc_id] = pending.get(tc_id, 0) + 1
                block_start = i
                waiting = []
            elif msg.role == "tool":
                tc_id = _tool_call_id_of(msg)
                if tc_id and pending.get(tc_id):
                    pending[tc_id] -= 1
                    for w in waiting:
                        issues.append(ValidationIssue(
                            type="interleaved_message_in_tool_block",
                            severity="error",
                            message_index=w,
                            description=(
                                f"A '{messages[w].role}' message at index {w} appears between an assistant "
                                f"tool_calls message (index {block_start}) and its tool "
                                f"responses. This breaks the OpenAI message protocol."
                            ),
                            details={
                                "interleaved_role": messages[w].role,
                                "assistant_index": block_start,
                            }
                        ))
                    waiting = []
            elif any(pending.values()):
                waiting.append(i)

        return issues

    def _apply_repairs(self, messages: List[ChatMessage], issues: List[ValidationIssue]) -> List[ChatMessage]:
        """Apply automatic repairs to fix validation issues.

        Issues point at indices of ``messages``, but every phase below moves,
        replaces or removes messages. So each phase finds its messages by
        identity: indices reused after an earlier phase hit the wrong message
        (stripped a valid tool call, kept tool responses of a removed call).
        Messages are replaced, never changed in place.
        """
        repaired = messages.copy()
        # id(original message) -> the message that replaced it
        current: Dict[int, ChatMessage] = {}

        def cur(msg: ChatMessage) -> ChatMessage:
            return current.get(id(msg), msg)

        def put(original: ChatMessage, new: ChatMessage) -> None:
            live = cur(original)
            pos = next(i for i, m in enumerate(repaired) if m is live)
            repaired[pos] = new
            current[id(original)] = new

        unanswered: Dict[int, Set[int]] = {}       # message index -> indices of calls to drop
        remove: Dict[int, ChatMessage] = {}        # id(original) -> original
        relocate: List[int] = []                   # indices of interleaved messages
        merges: List[tuple] = []                   # (first index, second index)

        for issue in issues:
            idx = issue.message_index
            if issue.type == "orphaned_tool_call":
                # Drop the unanswered calls instead of adding fake responses:
                # this avoids sending fake tool execution results to the LLM.
                unanswered.setdefault(idx, set()).add(issue.details.get("tool_index"))

            elif issue.type in ("orphaned_tool_response", "missing_tool_call_id"):
                remove[id(messages[idx])] = messages[idx]

            elif issue.type == "empty_assistant_message":
                # Remove empty assistant messages - they serve no purpose and can
                # confuse the LLM (especially when followed by user "Continue" messages)
                remove[id(messages[idx])] = messages[idx]

            # Note: invalid_first_message is handled by the loop after all removals
            # (see "Ensure first non-system message is 'user'" section below)

            elif issue.type == "interleaved_message_in_tool_block":
                relocate.append(idx)

            elif issue.type == "consecutive_assistant_messages":
                merges.append((idx, issue.details.get("next_index", idx + 1)))

            elif issue.type == "invalid_tool_name":
                original = messages[idx]
                sanitized_name = self._sanitize_tool_name(issue.details.get("tool_name", ""))
                if not (sanitized_name and OPENAI_TOOL_NAME_PATTERN.match(sanitized_name)):
                    # Nothing usable left: drop this call like an unanswered
                    # one; its answer is orphaned then and goes in the next pass.
                    unanswered.setdefault(idx, set()).add(issue.details.get("tool_index"))
                    continue
                msg = cur(original)
                tool_idx = issue.details.get("tool_index", 0)
                if msg.tool_calls and tool_idx < len(msg.tool_calls):
                    tool_calls = list(msg.tool_calls)
                    tool_call = tool_calls[tool_idx]
                    if isinstance(tool_call, dict) and isinstance(tool_call.get("function"), dict):
                        tool_calls[tool_idx] = {
                            **tool_call,
                            "function": {**tool_call["function"], "name": sanitized_name},
                        }
                        put(original, msg.model_copy(update={"tool_calls": tool_calls}))

        # FIRST: drop unanswered tool calls. Answered calls of the same message
        # stay -- stripping them all orphaned their responses. No placeholder
        # text yet: a merge below may still give the message content.
        for idx, missing in unanswered.items():
            original = messages[idx]
            msg = cur(original)
            if msg.role != "assistant" or not msg.tool_calls:
                continue
            kept = [tc for k, tc in enumerate(msg.tool_calls) if k not in missing]
            # rd_orphaned: the kept reasoning_details signed the very
            # tool_calls we just removed — chain-verified providers
            # (reasoning_details_mode=keep_all) must reset instead of
            # replaying a signature over mutated content; Gemini
            # (keep_last) ignores the flag and keeps its signature.
            # model_copy keeps the remaining fields (reasoning_content,
            # reasoning_details, thinking_blocks, timestamp, ...).
            put(original, msg.model_copy(update={
                "tool_calls": kept or None,
                "rd_orphaned": True,
            }))

        # NEXT: relocate interleaved messages out of tool-call blocks, right
        # after the tool responses that follow them. Reverse order keeps
        # several interleaved messages of one block in their order.
        for idx in sorted(relocate, reverse=True):
            live = cur(messages[idx])
            pos = next((i for i, m in enumerate(repaired) if m is live), None)
            if pos is None:
                continue
            msg = repaired.pop(pos)
            insert_at = pos
            while insert_at < len(repaired) and repaired[insert_at].role == "tool":
                insert_at += 1
            repaired.insert(insert_at, msg)
            logger.warning(
                f"Relocated interleaved '{msg.role}' message from index {pos} "
                f"to after tool responses at index {insert_at}"
            )

        # NEXT: merge consecutive assistant messages. Reverse order: in a run
        # a1, a2, a3 the pair (a2, a3) merges first, then a1 takes in the result.
        for first_idx, second_idx in sorted(merges, reverse=True):
            if not (0 <= first_idx < len(messages) and 0 <= second_idx < len(messages)):
                continue
            first_orig, second_orig = messages[first_idx], messages[second_idx]
            first_msg, second_msg = cur(first_orig), cur(second_orig)

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

            # Merge reasoning_details in original order (provider-side
            # thinking blocks that must round-trip, e.g. Gemini
            # thought_signature — dropping them causes
            # MALFORMED_FUNCTION_CALL on the next turn).
            merged_rd = (list(getattr(first_msg, 'reasoning_details', None) or [])
                         + list(getattr(second_msg, 'reasoning_details', None) or []))

            # Anthropic thinking blocks: keep only the NEWER turn's
            # blocks. Anthropic validates the block SEQUENCE of the last
            # assistant turn verbatim ("never filter, dedupe or reorder",
            # see llm/models.py) — concatenating two turns' blocks would
            # send a sequence the model never produced in one turn, which
            # is exactly the 400 this preservation is meant to avoid.
            # Model-bound signatures make cross-model concatenation wrong
            # for the same reason.
            first_tb = getattr(first_msg, 'thinking_blocks', None)
            second_tb = getattr(second_msg, 'thinking_blocks', None)
            first_tm = getattr(first_msg, 'thinking_model', None)
            second_tm = getattr(second_msg, 'thinking_model', None)
            if second_tb:
                merged_tb, merged_tm = second_tb, second_tm
            else:
                merged_tb, merged_tm = first_tb or None, first_tm

            # model_copy keeps every remaining field (name, timestamp,
            # injected_by, ...) — the previous full reconstruction here
            # silently dropped reasoning_details/thinking_blocks.
            put(first_orig, first_msg.model_copy(update={
                "content": merged_content if merged_content else None,
                "tool_calls": merged_tool_calls if merged_tool_calls else None,
                "reasoning_content": merged_reasoning if merged_reasoning else None,
                "multimodal_content": merged_multimodal if merged_multimodal else None,
                "content_format": first_msg.content_format or second_msg.content_format,
                "reasoning_details": merged_rd if merged_rd else None,
                # A broken chain on either side stays broken in the merge.
                "rd_orphaned": (getattr(first_msg, 'rd_orphaned', None)
                                or getattr(second_msg, 'rd_orphaned', None)),
                # Two producers in one merged list match no model, so
                # strip_foreign_reasoning_artifacts would reset them. The
                # agent loop strips before this hook runs, so today the
                # merge only ever sees one producer.
                "reasoning_model": "|".join(dict.fromkeys(
                    m.reasoning_model for m in (first_msg, second_msg)
                    if getattr(m, 'reasoning_details', None)
                    and getattr(m, 'reasoning_model', None))) or None,
                "thinking_blocks": merged_tb,
                "thinking_model": merged_tm if merged_tb else None,
                # The later turn's backend is the one holding the cache.
                "served_by": (getattr(second_msg, 'served_by', None)
                              or getattr(first_msg, 'served_by', None)),
            }))

            # The second message now lives in the first. The first is kept
            # even when it was empty: removing it dropped the merged content
            # and tool calls with it.
            remove.pop(id(first_orig), None)
            remove[id(second_orig)] = second_orig
            logger.debug(f"Merged consecutive assistant messages at indices {first_idx} and {second_idx}")

        # Remove problematic messages (tool answers, empty or merged-away
        # assistant turns -- none of them carries calls of its own).
        drop = {id(cur(m)) for m in remove.values()}
        repaired = [m for m in repaired if id(m) not in drop]

        # A turn whose every call was dropped and that got no text from a
        # merge still has to say something.
        for idx in unanswered:
            msg = cur(messages[idx])
            if (msg.role == "assistant" and not msg.content and not msg.tool_calls
                    and any(m is msg for m in repaired)):
                put(messages[idx], msg.model_copy(update={"content": INTERRUPTED_PLACEHOLDER}))

        # Ensure the first non-instruction message is 'user' (loop until valid
        # or empty). This handles cascading removals where removing the first
        # bad message exposes another. Instructions (system AND developer) are
        # skipped, not removed: this loop POPS whatever it finds, so counting a
        # developer note as the first message deleted it without a word.
        max_iterations = 100  # Safety limit
        for _ in range(max_iterations):
            first_non_system_idx = None
            for i, msg in enumerate(repaired):
                if msg.role not in INSTRUCTION_ROLES or is_input(msg):
                    first_non_system_idx = i
                    break
            
            if first_non_system_idx is None:
                break  # Only system messages left
            
            first_msg = repaired[first_non_system_idx]
            if is_input(first_msg):
                # Valid. `is_input`, not the bare role: asking for `user` read a
                # woken run's wake as missing, and this loop POPS -- it deleted
                # the run's own assistant and tool messages one by one looking
                # for a user turn that a woken run need not have.
                break
            
            # Need to remove this message and related tool messages
            indices_to_remove: Set[int] = {first_non_system_idx}
            
            if first_msg.role == "assistant" and first_msg.tool_calls:
                # Find and remove matching tool responses
                tool_call_ids = set()
                for tc in first_msg.tool_calls:
                    tc_id = getattr(tc, 'id', None) or (tc.get('id') if isinstance(tc, dict) else None)
                    if tc_id:
                        tool_call_ids.add(tc_id)
                # Only in its own block: a call id may repeat in later turns.
                for j in range(first_non_system_idx + 1, len(repaired)):
                    other_msg = repaired[j]
                    if other_msg.role == "assistant" and other_msg.tool_calls:
                        break
                    if other_msg.role == "tool":
                        other_tc_id = getattr(other_msg, 'tool_call_id', None)
                        if not other_tc_id and hasattr(other_msg, 'model_dump'):
                            other_tc_id = other_msg.model_dump().get('tool_call_id')
                        if other_tc_id in tool_call_ids:
                            indices_to_remove.add(j)
            elif first_msg.role == "tool":
                # Tool message without assistant - just remove it
                pass
            
            # Remove in reverse order
            for idx in sorted(indices_to_remove, reverse=True):
                if idx < len(repaired):
                    repaired.pop(idx)
            
            logger.debug(
                f"Removed {len(indices_to_remove)} messages to ensure first "
                f"non-system message is 'user'"
            )

        return repaired

    def _name_is_unusable(self, tool_call: Any) -> bool:
        """Whether the call has a name that no rewrite makes valid."""
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not name or OPENAI_TOOL_NAME_PATTERN.match(name):
            return False
        sanitized = self._sanitize_tool_name(name)
        return not (sanitized and OPENAI_TOOL_NAME_PATTERN.match(sanitized))

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
                f"Message validation found {len(issues)} issues in {context}: {repair_summary}"
            )


class MessageValidatorPlugin(SchemaBasedPluginHook):
    """
    Schema-based hook plugin for message validation.

    Hook definitions and configuration are loaded from schema.yaml.
    """

    def __init__(self, plugin_dir: Path | str, server_config: Any = None):
        """Initialize the message validator plugin.

        Args:
            plugin_dir: Directory containing schema.yaml
            server_config: tool server configuration (contains config from plugins.yaml)
        """
        super().__init__(plugin_dir)

        # Get config from schema defaults
        config = self.get_config()
        
        # Merge with server_config.config if provided (overrides schema defaults)
        if server_config and hasattr(server_config, 'config') and server_config.config:
            config.update(server_config.config)
        
        # Initialize internal validator with merged config
        self.validator = InternalMessageValidator(config=config)

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

            # Only a real change counts. Repairs replace messages instead of
            # changing them in place, so the comparison above sees every one.
            # An issue without a repair (a large or JSON-looking tool result)
            # used to count too: then every call reported a change, stripped
            # reasoning artifacts and had the loop save the history again.
            actually_modified = modified

            # THE INVARIANT (utils/reasoning_artifacts.py): repairs reorder,
            # drop or rewrite messages mid-history — provider reasoning chains
            # (OpenAI encrypted items) over the repaired span become
            # unverifiable. Invalidate at the mutation site -- from the first
            # changed message on: turns before it came from an unchanged
            # history and still verify, and stripping them moved the cache
            # break to the front (measured in context_engineer/compaction.py).
            # Repairs replace messages, so identity finds the first change.
            if actually_modified:
                repaired = result.repaired_messages
                start = next((i for i, (before, after) in enumerate(zip(chat_messages, repaired))
                              if before is not after), min(len(chat_messages), len(repaired)))
                invalidate_reasoning_artifacts(repaired, start=start)

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

            errors = []

            for i, msg in enumerate(messages):
                # Check basic structure
                if isinstance(msg, ChatMessage):
                    if not hasattr(msg, 'role') or not msg.role:
                        errors.append(f"Message {i} missing role")
                elif isinstance(msg, dict):
                    if 'role' not in msg or not msg['role']:
                        errors.append(f"Message {i} missing role")

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
