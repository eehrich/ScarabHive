"""Intelligent conversation summarization for context management."""

import logging
from typing import List
from ..llm.clients import ChatMessage
from .config import ContextConfig
from ..mcp.improved_status import (
    publish_status_improved,
    StatusPhase,
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

        # Ensure we preserve assistant messages that triggered tool calls and their tool results
        # These are important for continuing the conversation and should not be summarized away.
        try:
            additional_preserve = []
            preserve_ids = set()

            # Collect candidate assistant messages with tool_calls from the to_summarize block
            for msg in to_summarize:
                if getattr(msg, 'role', None) == 'assistant' and getattr(msg, 'tool_calls', None):
                    # Avoid duplicating if already in to_preserve
                    if msg not in to_preserve and msg not in additional_preserve:
                        additional_preserve.append(msg)
                        # Collect tool call ids referenced so we can keep matching tool results
                        for tc in msg.tool_calls:
                            tcid = tc.get('id') if isinstance(tc, dict) else None
                            if tcid:
                                preserve_ids.add(tcid)

            # Find tool result messages that match collected tool_call_ids
            if preserve_ids:
                for msg in to_summarize:
                    if getattr(msg, 'role', None) == 'tool':
                        tid = getattr(msg, 'tool_call_id', None)
                        if tid and tid in preserve_ids and msg not in to_preserve and msg not in additional_preserve:
                            additional_preserve.append(msg)

            if additional_preserve:
                # Maintain original chronological order when adding to_preserve
                for p in additional_preserve:
                    if p in to_summarize:
                        to_summarize.remove(p)
                    if p not in to_preserve:
                        to_preserve.append(p)

                logger.debug("🔒 Preserved %d important assistant/tool messages during summarization", len(additional_preserve))
        except Exception:
            logger.exception("Failed while preserving assistant/tool messages before summarization")
        
        # Publish start status event
        await publish_status_improved(
            server="conversation-summarizer",
            message=f"📝 Starting conversation summarization: {len(to_summarize)} messages to summarize",
            phase=StatusPhase.START,
            meta={"messages_to_summarize": len(to_summarize), "messages_to_preserve": len(to_preserve)}
        )
        
        logger.debug("🤖 Starting LLM-based conversation summarization...")
        logger.debug("   📝 Messages to summarize: %d", len(to_summarize))
        logger.debug("   💾 Messages to preserve: %d", len(to_preserve))
        
        # Create summary of older conversation
        summary_text = await self._create_summary(to_summarize, config)
        
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
        
        # Add preserved recent messages (with tool result truncation)
        truncated_preserved = self._truncate_tool_results(to_preserve, config)
        result.extend(truncated_preserved)
        
        logger.debug("📊 Summarization complete:")
        logger.debug("   🗂️  Original messages: %d → Final messages: %d", 
                   len(messages), len(result))
        logger.debug("   📝 Existing summaries preserved: %d", len(existing_summaries))
        logger.debug("   📝 New summary included: %s", "Yes" if summary_text else "No")
        
        # Publish completion status event
        await publish_status_improved(
            server="conversation-summarizer",
            message=f"✅ Summarization complete: {len(messages)}→{len(result)} messages",
            phase=StatusPhase.END,
            meta={
                "original_messages": len(messages),
                "final_messages": len(result),
                "existing_summaries": len(existing_summaries),
                "new_summary_included": bool(summary_text),
                "summary_length": len(summary_text) if summary_text else 0
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
    
    async def _create_summary(self, messages: List[ChatMessage], config: ContextConfig) -> str:
        """Create a concise summary of the conversation messages."""
        # If no LLM client is available, use fallback method immediately
        if not self.llm_client:
            logger.warning("⚠️  No LLM client available for summarization, using fallback method")
            return await self._create_fallback_summary(messages)
        
        try:
            logger.debug("🔄 Preparing conversation for LLM summarization...")
            
            # Prepare conversation text for summarization
            conversation_text = self._format_messages_for_summary(messages, config)
            
            # Create summarization prompt
            summary_prompt = self._create_summary_prompt(conversation_text, config)
            
            await publish_status_improved(
                server="conversation-summarizer",
                message=f"🤖 Requesting summary from LLM ({len(conversation_text):,} chars)",
                phase=StatusPhase.PROGRESS,
                meta={"input_chars": len(conversation_text)}
            )
            
            logger.debug("🤖 Requesting summary from LLM (input: %d chars)...", len(conversation_text))
            
            # Get summary from LLM using chat method
            summary_messages = [ChatMessage(role="user", content=summary_prompt)]
            response_content = await self.llm_client.chat(summary_messages)
            
            if response_content and response_content.strip():
                summary_length = len(response_content.strip())
                reduction_ratio = (1 - summary_length / len(conversation_text)) * 100
                logger.debug("✅ LLM summary complete (output: %d chars, %.1f%% reduction)", 
                           summary_length, reduction_ratio)
                
                await publish_status_improved(
                    server="conversation-summarizer", 
                    message=f"✅ LLM summary complete: {len(conversation_text):,}→{summary_length:,} chars ({reduction_ratio:.1f}% reduction)",
                    phase=StatusPhase.PROGRESS,
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
            await publish_status_improved(
                server="conversation-summarizer",
                message=f"❌ LLM summary failed: {str(e)}, using fallback method",
                phase=StatusPhase.ERROR,
                level="error",
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
                        tool_summaries.append(f"called {func_name}")
                    
                    if content:
                        formatted_parts.append(f"Assistant: {content} [Tools: {', '.join(tool_summaries)}]")
                    else:
                        formatted_parts.append(f"Assistant: [Tools: {', '.join(tool_summaries)}]")
                else:
                    formatted_parts.append(f"Assistant: {content}")
            elif role == "tool":
                # Summarize tool results (they can be very long)
                tool_id = getattr(msg, 'tool_call_id', 'unknown')
                preview_chars = config.tool_result_preview_chars
                content_preview = str(content)[:preview_chars] + "..." if len(str(content)) > preview_chars else str(content)
                formatted_parts.append(f"Tool Result ({tool_id}): {content_preview}")
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