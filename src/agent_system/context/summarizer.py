"""Intelligent conversation summarization for context management."""

import logging
from typing import List
from ..llm.clients import ChatMessage
from .config import ContextConfig
from ..mcp.status import (
    status_bus,
    status_scope,
)

logger = logging.getLogger(__name__)


class ConversationSummarizer:
    """Summarizes conversation history to fit within context windows."""
    
    def __init__(self, llm_client=None):
        """Initialize the conversation summarizer.
        
        Args:
            llm_client: Optional LLM client. If None, summarization will fall back to simple text extraction.
        """
        self.llm_client = llm_client
    
    async def summarize_conversation(self, messages: List[ChatMessage], config: ContextConfig) -> List[ChatMessage]:
        """Summarize older conversation while preserving recent messages.
        
        Smart summarization that:
        1. Preserves existing summaries (doesn't re-summarize summaries)
        2. Only summarizes actual conversation content
        3. Maintains tool call/result integrity
        """
        if len(messages) <= config.preserve_recent_messages:
            return messages
        
        # Find existing summaries and preserve them
        existing_summaries = []
        conversation_messages = []
        
        for msg in messages:
            role = getattr(msg, 'role', None)
            content = str(msg.content or '')
            
            if role == 'system' and '[CONVERSATION SUMMARY]' in content:
                # This is an existing summary - preserve it
                existing_summaries.append(msg)
                logger.debug("🔒 Found existing summary to preserve (length: %d chars)", len(content))
            else:
                # This is actual conversation content
                conversation_messages.append(msg)
        
        # If no new conversation content to summarize, return as-is
        if len(conversation_messages) <= config.preserve_recent_messages:
            logger.debug("📝 No new content to summarize after excluding existing summaries")
            return messages
        
        # Split conversation messages into parts to summarize and parts to keep
        preserve_count = config.preserve_recent_messages
        to_preserve = conversation_messages[-preserve_count:]
        to_summarize = conversation_messages[:-preserve_count]
        
        # Always preserve system message if it exists (but not summaries)
        system_msg = None
        if to_summarize and getattr(to_summarize[0], 'role', None) == 'system':
            # Only preserve if it's not a summary
            first_msg = to_summarize[0]
            if '[CONVERSATION SUMMARY]' not in str(first_msg.content or ''):
                system_msg = first_msg
                to_summarize = to_summarize[1:]
        
        if not to_summarize:
            # Build result: system + existing summaries + preserved conversation
            result = []
            if system_msg:
                result.append(system_msg)
            result.extend(existing_summaries)
            result.extend(to_preserve)
            return result

        # Ensure we preserve complete tool call sequences across both to_summarize and to_preserve lists
        # This prevents orphaned tool messages that violate OpenAI API requirements
        try:
            # Combine all messages to analyze complete tool call sequences
            all_messages = to_summarize + to_preserve
            
            # Find all complete tool call sequences that need to be preserved together
            tool_call_sequences = self._find_complete_tool_call_sequences(all_messages)
            
            # Determine which messages need to be moved from to_summarize to to_preserve
            indices_to_preserve = set()
            for sequence in tool_call_sequences:
                for msg_index, msg in enumerate(to_summarize):
                    if any(msg is seq_msg for seq_msg in sequence):
                        indices_to_preserve.add(msg_index)
            
            if indices_to_preserve:
                # Extract messages to preserve in original chronological order
                messages_to_move = []
                for i in sorted(indices_to_preserve):  # Sort to maintain order
                    messages_to_move.append(to_summarize[i])

                # Remove preserved messages from to_summarize (in reverse order to maintain indices)
                for i in sorted(indices_to_preserve, reverse=True):
                    to_summarize.pop(i)

                # Add to preserve list while maintaining chronological order
                # Insert these messages before the existing to_preserve messages since they're older
                to_preserve = messages_to_move + to_preserve

                logger.debug("🔒 Preserved %d tool call sequence messages to prevent orphaned tool messages", len(messages_to_move))
        except Exception:
            logger.exception("Failed while preserving complete tool call sequences")
        
        logger.debug("🤖 Starting LLM-based conversation summarization...")
        logger.debug("   📝 Messages to summarize: %d", len(to_summarize))
        logger.debug("   💾 Messages to preserve: %d", len(to_preserve))
        
        # Create summary of older conversation with status tracking
        async with status_scope(
            status_bus, 
            "conversation-summarizer", 
            request_id=None,
            start_msg=f"📝 Starting conversation summarization: {len(to_summarize)} messages to summarize"
        ) as scope:
            
            # Create summary of older conversation
            summary_text = await self._create_summary(to_summarize, config, scope)
            
            # Build result with system message, existing summaries, new summary, and preserved messages
            result = []
            if system_msg:
                result.append(system_msg)
            
            # Add existing summaries first (chronological order)
            result.extend(existing_summaries)
            
            # Add new summary as a system message
            if summary_text:
                summary_msg = ChatMessage(
                    role="system",
                    content=f"[CONVERSATION SUMMARY] The following is a summary of earlier conversation:\n\n{summary_text}\n\n[END SUMMARY] Recent conversation continues below:"
                )
                result.append(summary_msg)
                logger.debug("✅ New conversation summary created (length: %d characters)", len(summary_text))
            else:
                logger.warning("⚠️  Summary creation failed, proceeding without new summary")
            
            # Add preserved recent messages (with tool result truncation and validation)
            truncated_preserved = self._truncate_tool_results(to_preserve, config)
            from agent_system.core.message_validator import validate_messages_before_llm
            validated_preserved = validate_messages_before_llm(truncated_preserved, context="summarizer_preserve")
            result.extend(validated_preserved)
            
            logger.debug("📊 Summarization complete:")
            logger.debug("   🗂️  Original messages: %d → Final messages: %d", 
                       len(messages), len(result))
            logger.debug("   📝 Existing summaries preserved: %d", len(existing_summaries))
            logger.debug("   📝 New summary included: %s", "Yes" if summary_text else "No")
            
            # Update status with final result
            await scope.end(
                f"✅ Summarization complete: {len(messages)}→{len(result)} messages",
                meta={
                    "original_messages": len(messages),
                    "final_messages": len(result),
                    "messages_summarized": len(to_summarize),
                    "summaries_preserved": len(existing_summaries),
                    "new_summary_created": bool(summary_text)
                }
            )
            
            return result
    
    def _truncate_tool_results(self, messages: List[ChatMessage], config: ContextConfig) -> List[ChatMessage]:
        """Truncate large tool results in messages to prevent context overflow."""
        truncated_messages = []
        max_chars = config.tool_result_preview_chars
        
        for msg in messages:
            role = getattr(msg, 'role', None)
            content = getattr(msg, 'content', '')
            
            if role == "tool" and len(str(content)) > max_chars:
                # Create a new message with truncated content
                truncated_content = str(content)[:max_chars] + f"... [truncated from {len(str(content)):,} chars]"
                truncated_msg = ChatMessage(
                    role=msg.role,
                    content=truncated_content
                )
                # Copy other attributes if they exist
                if hasattr(msg, 'tool_call_id'):
                    truncated_msg.tool_call_id = msg.tool_call_id
                if hasattr(msg, 'name'):
                    truncated_msg.name = msg.name
                    
                truncated_messages.append(truncated_msg)
                logger.debug("🔧 Truncated tool result: %s chars → %s chars", 
                           len(str(content)), len(truncated_content))
            else:
                # Keep message as-is
                truncated_messages.append(msg)
        
        return truncated_messages
    
    async def _create_summary(self, messages: List[ChatMessage], config: ContextConfig, scope) -> str:
        """Create a concise summary of the conversation messages."""
        # If no LLM client is available, use fallback method immediately
        if not self.llm_client:
            logger.warning("⚠️  No LLM client available for summarization, using fallback method")
            return await self._create_fallback_summary(messages)
        
        try:
            logger.debug("🔄 Preparing conversation for LLM summarization...")
            
            # Prepare conversation text for summarization
            conversation_text = self._format_messages_for_summary(messages, config)
            
            # Check if there's meaningful content to summarize
            if not conversation_text.strip():
                logger.debug("No meaningful content to summarize - returning empty summary")
                return ""
            
            # Create summarization prompt
            summary_prompt = self._create_summary_prompt(conversation_text, config)
            
            await scope.progress(
                f"🤖 Requesting summary from LLM ({len(conversation_text):,} chars)",
                meta={"input_chars": len(conversation_text)}
            )
            
            logger.debug("🤖 Requesting summary from LLM (input: %d chars)...", len(conversation_text))
            
            # Get summary from LLM using chat method
            summary_messages = [ChatMessage(role="user", content=summary_prompt)]
            
            # Validate message sequence before LLM call
            from agent_system.core.message_validator import validate_messages_before_llm
            summary_messages = validate_messages_before_llm(summary_messages, context="summarizer")
            
            response_content = await self.llm_client.chat(summary_messages)
            
            if response_content and response_content.strip():
                summary_length = len(response_content.strip())
                # Prevent division by zero when conversation_text is empty
                if len(conversation_text) > 0:
                    reduction_ratio = (1 - summary_length / len(conversation_text)) * 100
                    logger.debug("✅ LLM summary complete (output: %d chars, %.1f%% reduction)", 
                               summary_length, reduction_ratio)
                else:
                    reduction_ratio = 0.0
                    logger.debug("✅ LLM summary complete (output: %d chars, no reduction calculated - empty input)", 
                               summary_length)
                
                await scope.progress(
                    f"✅ LLM summary complete: {len(conversation_text):,}→{summary_length:,} chars ({reduction_ratio:.1f}% reduction)",
                    meta={
                        "input_chars": len(conversation_text),
                        "output_chars": summary_length,
                        "reduction_ratio": reduction_ratio
                    }
                )
                
                return response_content.strip()
            else:
                logger.warning("⚠️  LLM returned empty summary, using fallback method")
                return await self._create_fallback_summary(messages)
                
        except Exception as e:
            await scope.error(
                f"❌ LLM summary failed: {str(e)}, using fallback method",
                meta={"error": str(e), "fallback": "text_extraction"}
            )
            logger.error("❌ LLM summary failed: %s, using fallback method", e)
            return await self._create_fallback_summary(messages)
    
    def _format_messages_for_summary(self, messages: List[ChatMessage], config: ContextConfig) -> str:
        """Format messages into readable text for summarization."""
        formatted_parts = []
        
        for i, msg in enumerate(messages):
            role = getattr(msg, 'role', 'unknown')
            content = getattr(msg, 'content', '')
            
            # Handle different message types
            if role == "user":
                formatted_parts.append(f"User: {content}")
            elif role == "assistant":
                # Include tool calls if present
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    tool_summaries = []
                    for tc in msg.tool_calls:
                        func = tc.get("function", {})
                        func_name = func.get("name", "unknown")
                        func_args = func.get("arguments", "")
                        
                        # Include important tool parameters for better summarization
                        if func_args:
                            # Try to extract key parameters (URLs, filenames, search terms)
                            try:
                                import json
                                args_dict = json.loads(func_args) if isinstance(func_args, str) else func_args
                                key_params = []
                                for key in ['url', 'query', 'filename', 'path', 'symbol', 'topic', 'task']:
                                    if key in args_dict:
                                        key_params.append(f"{key}={args_dict[key]}")
                                if key_params:
                                    tool_summaries.append(f"called {func_name}({', '.join(key_params)})")
                                else:
                                    tool_summaries.append(f"called {func_name}")
                            except Exception:
                                tool_summaries.append(f"called {func_name}")
                        else:
                            tool_summaries.append(f"called {func_name}")
                    
                    if content:
                        formatted_parts.append(f"Assistant: {content} [Tools: {', '.join(tool_summaries)}]")
                    else:
                        formatted_parts.append(f"Assistant: [Tools: {', '.join(tool_summaries)}]")
                else:
                    formatted_parts.append(f"Assistant: {content}")
            elif role == "tool":
                # Improved tool result summarization - preserve more context
                tool_name = getattr(msg, 'name', 'unknown_tool')
                
                # Use larger preview for tool results to preserve more context
                preview_chars = min(config.tool_result_preview_chars * 3, 1000)  # Up to 1000 chars
                content_str = str(content)
                
                if len(content_str) > preview_chars:
                    # For long content, try to preserve beginning and end
                    half_chars = preview_chars // 2
                    content_preview = content_str[:half_chars] + f"...[{len(content_str)-preview_chars} chars omitted]..." + content_str[-half_chars:]
                else:
                    content_preview = content_str
                    
                formatted_parts.append(f"Tool Result ({tool_name}): {content_preview}")
            elif role == "system":
                formatted_parts.append(f"System: {content}")
        
        return "\n".join(formatted_parts)
    
    def _create_summary_prompt(self, conversation_text: str, config: ContextConfig) -> str:
        """Create the prompt for summarizing conversation."""
        return f"""Please create a concise but comprehensive summary of the following conversation. Focus on:

1. The main task or problem being addressed
2. Key decisions made and approaches taken
3. Important findings, results, or outcomes
4. Current state and next steps if mentioned
5. Any critical context needed for continuation

Keep the summary under {config.max_summary_words} words but ensure all essential information is preserved.

CONVERSATION TO SUMMARIZE:
{conversation_text}

SUMMARY:"""
    
    async def _create_fallback_summary(self, messages: List[ChatMessage]) -> str:
        """Create a simple fallback summary when LLM summarization fails."""
        user_count = sum(1 for msg in messages if getattr(msg, 'role', None) == 'user')
        assistant_count = sum(1 for msg in messages if getattr(msg, 'role', None) == 'assistant')
        tool_count = sum(1 for msg in messages if getattr(msg, 'role', None) == 'tool')
        
        # Extract key patterns
        topics = []
        tools_used = set()
        
        for msg in messages:
            content = str(getattr(msg, 'content', ''))
            
            # Look for task-related keywords
            if any(keyword in content.lower() for keyword in ['implement', 'create', 'build', 'develop']):
                topics.append("implementation work")
            if any(keyword in content.lower() for keyword in ['fix', 'debug', 'error', 'issue']):
                topics.append("debugging/fixes")
            if any(keyword in content.lower() for keyword in ['test', 'verify', 'validate']):
                topics.append("testing")
            
            # Extract tool usage
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    func = tc.get("function", {})
                    func_name = func.get("name", "")
                    if func_name:
                        tools_used.add(func_name)
        
        summary_parts = [
            f"Conversation involved {user_count} user messages, {assistant_count} assistant responses, and {tool_count} tool results."
        ]
        
        if topics:
            unique_topics = list(set(topics))
            summary_parts.append(f"Main activities: {', '.join(unique_topics)}.")
        
        if tools_used:
            summary_parts.append(f"Tools used: {', '.join(sorted(tools_used))}.")
        
        return " ".join(summary_parts)
    
    def _find_complete_tool_call_sequences(self, messages: List[ChatMessage]) -> List[List[ChatMessage]]:
        """
        Find complete tool call sequences that must be preserved together.
        
        A complete sequence includes:
        1. Assistant message with tool_calls
        2. All corresponding tool response messages
        
        Returns:
            List of sequences, where each sequence is a list of messages that must stay together
        """
        sequences = []
        tool_call_map = {}  # Maps tool_call_id to assistant message
        
        # First pass: collect all assistant messages with tool_calls and map their tool_call_ids
        for msg in messages:
            if getattr(msg, 'role', None) == 'assistant' and getattr(msg, 'tool_calls', None):
                tool_calls = getattr(msg, 'tool_calls', [])
                for tc in tool_calls:
                    tcid = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                    if tcid:
                        tool_call_map[tcid] = msg
        
        # Second pass: group tool responses with their assistant messages
        sequence_map = {}  # Maps assistant message id to its complete sequence
        
        for msg in messages:
            if getattr(msg, 'role', None) == 'tool':
                tool_call_id = getattr(msg, 'tool_call_id', None)
                if tool_call_id and tool_call_id in tool_call_map:
                    assistant_msg = tool_call_map[tool_call_id]
                    assistant_id = id(assistant_msg)  # Use object id as key
                    
                    # Initialize sequence if not exists
                    if assistant_id not in sequence_map:
                        sequence_map[assistant_id] = [assistant_msg]
                    
                    # Add tool response to sequence
                    sequence_map[assistant_id].append(msg)
        
        # Convert to list of sequences
        sequences = list(sequence_map.values())
        
        logger.debug("🔍 Found %d complete tool call sequences to preserve integrity", len(sequences))
        return sequences