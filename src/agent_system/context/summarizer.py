"""Intelligent conversation summarization for context management."""

import logging
from typing import List
from ..llm.clients import ChatMessage, make_llm
from .config import ContextConfig
from ..mcp.status import publish_status, PHASE_START, PHASE_PROGRESS, PHASE_END, PHASE_ERROR

logger = logging.getLogger(__name__)


class ConversationSummarizer:
    """Summarizes conversation history to fit within context windows."""
    
    def __init__(self, llm_client=None):
        self.llm_client = llm_client or make_llm()
    
    async def summarize_conversation(self, messages: List[ChatMessage], config: ContextConfig) -> List[ChatMessage]:
        """Summarize older conversation while preserving recent messages."""
        if len(messages) <= config.preserve_recent_messages:
            return messages
        
        # Split messages into parts to summarize and parts to keep
        preserve_count = config.preserve_recent_messages
        to_preserve = messages[-preserve_count:]
        to_summarize = messages[:-preserve_count]
        
        # Always preserve system message if it exists
        system_msg = None
        if to_summarize and getattr(to_summarize[0], 'role', None) == 'system':
            system_msg = to_summarize[0]
            to_summarize = to_summarize[1:]
        
        if not to_summarize:
            result = [system_msg] + to_preserve if system_msg else to_preserve
            return result
        
        # Publish start status event
        await publish_status(
            server="conversation-summarizer",
            message=f"📝 Starting conversation summarization: {len(to_summarize)} messages to summarize",
            phase=PHASE_START,
            meta={"messages_to_summarize": len(to_summarize), "messages_to_preserve": len(to_preserve)}
        )
        
        logger.debug("🤖 Starting LLM-based conversation summarization...")
        logger.debug("   📝 Messages to summarize: %d", len(to_summarize))
        logger.debug("   💾 Messages to preserve: %d", len(to_preserve))
        
        # Create summary of older conversation
        summary_text = await self._create_summary(to_summarize, config)
        
        # Build result with summary
        result = []
        if system_msg:
            result.append(system_msg)
        
        # Add summary as a system message
        if summary_text:
            summary_msg = ChatMessage(
                role="system",
                content=f"[CONVERSATION SUMMARY] The following is a summary of earlier conversation:\n\n{summary_text}\n\n[END SUMMARY] Recent conversation continues below:"
            )
            result.append(summary_msg)
            logger.debug("✅ Conversation summary created (length: %d characters)", len(summary_text))
        else:
            logger.warning("⚠️  Summary creation failed, proceeding without summary")
        
        # Add preserved recent messages
        result.extend(to_preserve)
        
        logger.debug("📊 Summarization complete:")
        logger.debug("   🗂️  Original messages: %d → Final messages: %d", 
                   len(to_summarize) + len(to_preserve), len(result))
        logger.debug("   📝 Summary included: %s", "Yes" if summary_text else "No")
        
        # Publish completion status event
        await publish_status(
            server="conversation-summarizer",
            message=f"✅ Summarization complete: {len(to_summarize) + len(to_preserve)}→{len(result)} messages",
            phase=PHASE_END,
            meta={
                "original_messages": len(to_summarize) + len(to_preserve),
                "final_messages": len(result),
                "summary_included": bool(summary_text),
                "summary_length": len(summary_text) if summary_text else 0
            }
        )
        
        return result
    
    async def _create_summary(self, messages: List[ChatMessage], config: ContextConfig) -> str:
        """Create a concise summary of the conversation messages."""
        try:
            logger.debug("🔄 Preparing conversation for LLM summarization...")
            
            # Prepare conversation text for summarization
            conversation_text = self._format_messages_for_summary(messages, config)
            
            # Create summarization prompt
            summary_prompt = self._create_summary_prompt(conversation_text, config)
            
            await publish_status(
                server="conversation-summarizer",
                message=f"🤖 Requesting summary from LLM ({len(conversation_text):,} chars)",
                phase=PHASE_PROGRESS,
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
                
                await publish_status(
                    server="conversation-summarizer", 
                    message=f"✅ LLM summary complete: {len(conversation_text):,}→{summary_length:,} chars ({reduction_ratio:.1f}% reduction)",
                    phase=PHASE_PROGRESS,
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
            await publish_status(
                server="conversation-summarizer",
                message=f"❌ LLM summary failed: {str(e)}, using fallback method",
                phase=PHASE_ERROR,
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