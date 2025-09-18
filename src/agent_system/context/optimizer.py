"""Token usage optimization and message compression utilities."""

import logging
import re
import json
from typing import List, Dict, Any
from ..llm.clients import ChatMessage
from ..mcp.status import publish_status, PHASE_START, PHASE_PROGRESS, PHASE_END, PHASE_ERROR

logger = logging.getLogger(__name__)


class TokenOptimizer:
    """Optimizes token usage through message compression and efficient formatting."""
    
    def __init__(self):
        self.compression_stats = {
            "messages_processed": 0,
            "tokens_saved": 0,
            "compression_ratio": 0.0
        }
    
    async def optimize_messages(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """Apply various optimization techniques to reduce token count."""
        if not messages:
            return messages
        
        # Publish start status event
        await publish_status(
            server="token-optimizer",
            message=f"🔧 Starting token optimization for {len(messages)} messages",
            phase=PHASE_START,
            meta={"message_count": len(messages)}
        )
            
        logger.debug("🔧 Starting token optimization for %d messages...", len(messages))
        
        optimized = []
        total_original_tokens = 0
        total_optimized_tokens = 0
        
        try:
            for i, msg in enumerate(messages):
                original_tokens = self._estimate_message_tokens(msg)
                optimized_msg = self._optimize_message(msg)
                optimized_tokens = self._estimate_message_tokens(optimized_msg)
                
                optimized.append(optimized_msg)
                total_original_tokens += original_tokens
                total_optimized_tokens += optimized_tokens
                
                # Log progress for large batches
                if len(messages) > 10 and (i + 1) % 10 == 0:
                    logger.debug("   📊 Processed %d/%d messages...", i + 1, len(messages))
                    await publish_status(
                        server="token-optimizer",
                        message=f"📊 Processed {i + 1}/{len(messages)} messages",
                        phase=PHASE_PROGRESS,
                        meta={"processed": i + 1, "total": len(messages)}
                    )
        except Exception as e:
            await publish_status(
                server="token-optimizer",
                message=f"❌ Token optimization failed: {str(e)}",
                phase=PHASE_ERROR,
                level="error",
                meta={"error": str(e), "processed": len(optimized)}
            )
            logger.error("Token optimization failed: %s", e)
            # Return original messages as fallback
            return messages
        
        # Update stats
        self.compression_stats["messages_processed"] += len(messages)
        tokens_saved = total_original_tokens - total_optimized_tokens
        self.compression_stats["tokens_saved"] += tokens_saved
        
        if total_original_tokens > 0:
            ratio = (total_optimized_tokens / total_original_tokens) * 100
            self.compression_stats["compression_ratio"] = ratio
            
            logger.debug("✅ Token optimization complete:")
            logger.debug("   🪙 Tokens: %d → %d (saved %d tokens)", 
                       total_original_tokens, total_optimized_tokens, tokens_saved)
            if tokens_saved > 0:
                logger.debug("   📉 Compression: %.1f%% (%.1f%% reduction)", 
                           ratio, 100 - ratio)
            else:
                logger.debug("   📊 No optimization opportunities found")
            
            # Publish completion status event
            await publish_status(
                server="token-optimizer",
                message=f"✅ Optimization complete: {total_original_tokens:,}→{total_optimized_tokens:,} tokens (saved {tokens_saved:,})",
                phase=PHASE_END,
                meta={
                    "original_tokens": total_original_tokens,
                    "optimized_tokens": total_optimized_tokens,
                    "tokens_saved": tokens_saved,
                    "compression_ratio": ratio,
                    "messages_processed": len(messages)
                }
            )
        
        return optimized
    
    def _optimize_message(self, msg: ChatMessage) -> ChatMessage:
        """Optimize a single message for token efficiency."""
        # Create a copy to avoid modifying the original
        optimized_content = self._optimize_content(str(msg.content or ""))
        
        # Handle tool calls optimization
        optimized_tool_calls = None
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            optimized_tool_calls = self._optimize_tool_calls(msg.tool_calls)
        
        # Create optimized message
        optimized_msg = ChatMessage(
            role=msg.role,
            content=optimized_content if optimized_content else None,
            tool_calls=optimized_tool_calls,
            tool_call_id=getattr(msg, 'tool_call_id', None),
            name=getattr(msg, 'name', None)
        )
        
        return optimized_msg
    
    def _optimize_content(self, content: str) -> str:
        """Optimize text content for token efficiency."""
        if not content:
            return content
        
        # Remove excessive whitespace
        content = re.sub(r'\s+', ' ', content.strip())
        
        # Remove redundant line breaks in code blocks
        content = re.sub(r'\n\s*\n\s*\n+', '\n\n', content)
        
        # Compress common verbose patterns
        content = self._compress_verbose_patterns(content)
        
        # Optimize JSON formatting in content
        content = self._optimize_json_in_text(content)
        
        return content
    
    def _compress_verbose_patterns(self, content: str) -> str:
        """Compress common verbose patterns in text."""
        patterns = [
            # Compress multiple periods
            (r'\.{3,}', '...'),
            # Compress multiple dashes
            (r'-{4,}', '----'),
            # Compress excessive punctuation
            (r'[!]{2,}', '!!'),
            (r'[?]{2,}', '??'),
            # Compress log timestamps to shorter format
            (r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}', '[TIMESTAMP]'),
            # Compress long file paths
            (r'[a-zA-Z]:\\[^\\]+\\[^\\]+\\[^\\]+\\[^\\]+\\[^\\]+', '[LONG_PATH]'),
            # Compress repeated "the" or "a" articles
            (r'\b(the|a)\s+(the|a)\s+', r'\1 '),
        ]
        
        for pattern, replacement in patterns:
            content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
        
        return content
    
    def _optimize_json_in_text(self, content: str) -> str:
        """Optimize JSON formatting within text content."""
        # Find JSON-like structures and compress them
        json_pattern = r'\{[^{}]*\}'
        
        def compress_json(match):
            json_str = match.group(0)
            try:
                # Parse and reformat JSON compactly
                json_obj = json.loads(json_str)
                return json.dumps(json_obj, separators=(',', ':'))
            except json.JSONDecodeError:
                # Not valid JSON, return as-is
                return json_str
        
        return re.sub(json_pattern, compress_json, content)
    
    def _optimize_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Optimize tool calls for token efficiency."""
        optimized = []
        
        for tc in tool_calls:
            if not isinstance(tc, dict):
                optimized.append(tc)
                continue
            
            optimized_tc = tc.copy()
            
            # Optimize function arguments
            if "function" in tc and "arguments" in tc["function"]:
                args = tc["function"]["arguments"]
                if isinstance(args, str):
                    try:
                        # Parse, optimize, and reformat arguments
                        args_obj = json.loads(args)
                        optimized_args = self._optimize_tool_arguments(args_obj)
                        optimized_tc["function"]["arguments"] = json.dumps(optimized_args, separators=(',', ':'))
                    except json.JSONDecodeError:
                        # Keep original if not valid JSON
                        pass
                elif isinstance(args, dict):
                    optimized_tc["function"]["arguments"] = self._optimize_tool_arguments(args)
            
            optimized.append(optimized_tc)
        
        return optimized
    
    def _optimize_tool_arguments(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Optimize tool arguments for token efficiency."""
        optimized = {}
        
        for key, value in args.items():
            if isinstance(value, str):
                # Optimize string values
                optimized[key] = self._optimize_content(value)
            elif isinstance(value, list):
                # Optimize list elements
                optimized[key] = [
                    self._optimize_content(item) if isinstance(item, str) else item
                    for item in value
                ]
            elif isinstance(value, dict):
                # Recursively optimize nested dictionaries
                optimized[key] = self._optimize_tool_arguments(value)
            else:
                optimized[key] = value
        
        return optimized
    
    def _estimate_message_tokens(self, msg: ChatMessage) -> int:
        """Estimate token count for a single message."""
        total_chars = 0
        
        # Count content
        if msg.content:
            total_chars += len(str(msg.content))
        
        # Count tool calls
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                total_chars += len(str(tc))
        
        # Add overhead for message structure
        total_chars += 50  # Role, metadata, etc.
        
        # Convert to tokens (conservative estimate)
        return int(total_chars / 3.5)
    
    def get_compression_stats(self) -> Dict[str, Any]:
        """Get current compression statistics."""
        return self.compression_stats.copy()
    
    def reset_stats(self):
        """Reset compression statistics."""
        self.compression_stats = {
            "messages_processed": 0,
            "tokens_saved": 0,
            "compression_ratio": 0.0
        }
    
    def compress_tool_results(self, messages: List[ChatMessage], max_result_length: int = 2000) -> List[ChatMessage]:
        """Specifically compress long tool results that often cause token bloat."""
        tool_results_found = 0
        tool_results_compressed = 0
        total_chars_saved = 0
        
        compressed = []
        
        for msg in messages:
            if getattr(msg, 'role', None) == 'tool' and msg.content:
                tool_results_found += 1
                content = str(msg.content)
                if len(content) > max_result_length:
                    # Truncate with summary
                    truncated = content[:max_result_length]
                    summary = f"\n\n[TRUNCATED: Original length {len(content)} chars, showing first {max_result_length} chars]"
                    compressed_content = truncated + summary
                    
                    compressed_msg = ChatMessage(
                        role=msg.role,
                        content=compressed_content,
                        tool_call_id=getattr(msg, 'tool_call_id', None),
                        name=getattr(msg, 'name', None)
                    )
                    compressed.append(compressed_msg)
                    
                    chars_saved = len(content) - len(compressed_content)
                    total_chars_saved += chars_saved
                    tool_results_compressed += 1
                    
                    logger.debug("📦 Compressed tool result: %d → %d chars (saved %d)", 
                               len(content), len(compressed_content), chars_saved)
                else:
                    compressed.append(msg)
            else:
                compressed.append(msg)
        
        if tool_results_found > 0:
            logger.debug("🛠️  Tool result compression complete:")
            logger.debug("   📊 Tool results found: %d", tool_results_found)
            logger.debug("   📦 Results compressed: %d", tool_results_compressed)
            if total_chars_saved > 0:
                logger.debug("   💾 Characters saved: %d", total_chars_saved)
        
        return compressed