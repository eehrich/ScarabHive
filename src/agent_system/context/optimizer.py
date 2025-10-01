"""Token usage optimization and message compression utilities."""

import logging
import re
import json
from typing import List, Dict, Any
from ..llm.models import ChatMessage
from ..mcp.status import StatusScope, status_bus

logger = logging.getLogger(__name__)


class TokenOptimizer:
    """Optimizes token usage through message compression and efficient formatting."""
    
    def __init__(self):
        self.compression_stats = {
            "messages_processed": 0,
            "tokens_saved": 0,
            "compression_ratio": 0.0
        }
        # Guarding against repeated immediate optimization calls that cause log storms
        self._last_run_time = 0.0
        self._last_tokens_saved = 0
        self._last_input_fingerprint = None
        # Cooldown in seconds between optimization attempts when no savings were observed
        self._cooldown_seconds = 1.0
    
    async def optimize_messages(self, messages: List[ChatMessage], request_id: str = None) -> List[ChatMessage]:
        """Apply various optimization techniques to reduce token count."""
        if not messages:
            return messages

        # Quick fingerprint of input to detect repeated identical inputs
        try:
            fingerprint_parts = []
            for m in messages:
                fingerprint_parts.append(str(len(str(m.content or ""))))
                if hasattr(m, 'tool_calls') and m.tool_calls:
                    fingerprint_parts.append(str(len(m.tool_calls)))
            input_fingerprint = "|".join(fingerprint_parts)
        except Exception:
            input_fingerprint = None

        # If we recently ran optimization on the same input and it saved nothing,
        # skip re-running until cooldown expires to avoid repeated log/status events.
        import time
        now = time.time()
        if (input_fingerprint is not None and
                input_fingerprint == self._last_input_fingerprint and
                self._last_tokens_saved <= 0 and
                (now - self._last_run_time) < self._cooldown_seconds):
            logger.debug("⏱️ Skipping token optimization due to cooldown and no prior savings")
            return messages
        
        # Use StatusScope for automatic START/END status management
        async with StatusScope(status_bus, "token-optimizer", request_id) as status:
            await status.progress(f"🔧 Starting token optimization for {len(messages)} messages", 
                                meta={"message_count": len(messages)})
                
            logger.debug("🔧 Starting token optimization for %d messages...", len(messages))
            
            optimized = []
            total_original_tokens = 0
            total_optimized_tokens = 0
            
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
                    await status.progress(f"📊 Processed {i + 1}/{len(messages)} messages",
                                        meta={"processed": i + 1, "total": len(messages)})
        
            # Compute token delta and decide whether to accept the optimized batch.
            tokens_saved = total_original_tokens - total_optimized_tokens

            # If the optimization resulted in negative savings (worse overall), avoid
            # applying and emit an error. Zero savings is neutral and should be treated as a completed run.
            if tokens_saved < 0:
                # Record last run metadata (no savings)
                try:
                    self._last_run_time = time.time()
                    self._last_tokens_saved = int(tokens_saved)
                    self._last_input_fingerprint = input_fingerprint
                except Exception:
                    pass

                # Update message processed counter but do not add negative savings
                self.compression_stats["messages_processed"] += len(messages)

                # Compression ratio reflects no improvement (100% remaining)
                ratio = 100.0 if total_original_tokens > 0 else 0.0
                self.compression_stats["compression_ratio"] = ratio

                # Log that optimization would not help overall
                logger.debug("⚠️  Token optimization produced no net savings: %d → %d (saved %d)",
                             total_original_tokens, total_optimized_tokens, tokens_saved)

                # Report error for negative savings
                meta = {
                    "original_tokens": total_original_tokens,
                    "optimized_tokens": total_optimized_tokens,
                    "tokens_saved": tokens_saved,
                    "compression_ratio": ratio,
                    "messages_processed": len(messages),
                }
                await status.error(f"Optimization degraded token usage: {total_original_tokens:,}→{total_optimized_tokens:,} tokens (saved {tokens_saved:,})", 
                                 meta=meta)

                # Return original messages to keep behavior safe and non-regressive
                return messages

            # Otherwise we have net zero or positive savings; update stats and accept optimized batch
            self.compression_stats["messages_processed"] += len(messages)
            # Only accumulate positive tokens_saved into aggregate stats
            if tokens_saved > 0:
                self.compression_stats["tokens_saved"] += int(tokens_saved)

            # Record last run metadata
            try:
                self._last_run_time = time.time()
                self._last_tokens_saved = int(tokens_saved)
                self._last_input_fingerprint = input_fingerprint
            except Exception:
                pass

            # Compute a safe compression ratio (avoid division by zero)
            if total_original_tokens > 0:
                ratio = (total_optimized_tokens / total_original_tokens) * 100
            else:
                ratio = 0.0
            self.compression_stats["compression_ratio"] = ratio

            # Log summary information
            if total_original_tokens > 0:
                logger.debug("✅ Token optimization complete:")
                logger.debug("   🪙 Tokens: %d → %d (saved %d tokens)", 
                           total_original_tokens, total_optimized_tokens, tokens_saved)
                if tokens_saved > 0:
                    logger.debug("   📉 Compression: %.1f%% (%.1f%% reduction)", 
                               ratio, 100 - ratio)
                else:
                    logger.debug("   📊 No optimization opportunities found")
            else:
                logger.debug("⚪ Token optimization complete: no tokenized content found to analyze")

            # Publish completion status using explicit end to include metadata
            meta = {
                "original_tokens": total_original_tokens,
                "optimized_tokens": total_optimized_tokens,
                "tokens_saved": tokens_saved,
                "compression_ratio": ratio,
                "messages_processed": len(messages),
            }
            await status.end(f"complete: {total_original_tokens:,}→{total_optimized_tokens:,} tokens (saved {tokens_saved:,})", 
                           meta=meta)
            
            return optimized
    
    def _optimize_message(self, msg: ChatMessage) -> ChatMessage:
        """Optimize a single message for token efficiency."""
        # Estimate original tokens
        original_tokens = self._estimate_message_tokens(msg)
        
        # Create a copy to avoid modifying the original
        optimized_content = self._optimize_content(str(msg.content or ""))
        
        # Handle tool calls optimization
        optimized_tool_calls = None
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            # Never attempt semantic compression of tool call JSON itself – just whitespace normalization
            logger.debug("🔧 Preserving tool_calls structure (%d calls)", len(msg.tool_calls))
            optimized_tool_calls = self._optimize_tool_calls(msg.tool_calls)
        
        # Create potential optimized message
        optimized_msg = ChatMessage(
            role=msg.role,
            content=optimized_content if optimized_content else None,
            tool_calls=optimized_tool_calls,
            tool_call_id=getattr(msg, 'tool_call_id', None),
            name=getattr(msg, 'name', None)
        )
        
        # Check if optimization actually helps
        optimized_tokens = self._estimate_message_tokens(optimized_msg)

        # Guard: If assistant message has tool_calls ensure none were lost
        if getattr(msg, 'role', None) == 'assistant' and getattr(msg, 'tool_calls', None):
            orig_ids = [tc.get('id') for tc in msg.tool_calls if isinstance(tc, dict)]
            new_ids = [tc.get('id') for tc in (optimized_msg.tool_calls or []) if isinstance(tc, dict)]
            if set(orig_ids) != set(new_ids):
                logger.warning("⚠️  Optimization attempted to alter tool_call ids (%s -> %s); reverting message untouched", orig_ids, new_ids)
                return msg  # Structural safety override
            # Also ensure tool_call_id on tool messages is preserved
        if getattr(msg, 'role', None) == 'tool':
            if getattr(msg, 'tool_call_id', None) != getattr(optimized_msg, 'tool_call_id', None):
                logger.warning("⚠️  Optimization altered tool_call_id on tool message; reverting")
                return msg
        
        # Only return optimized version if it's actually better
        if optimized_tokens < original_tokens:
            logger.debug("✅ Message optimization successful: %d → %d tokens", 
                        original_tokens, optimized_tokens)
            return optimized_msg
        else:
            logger.debug("⚠️  Message optimization skipped: would increase tokens %d → %d", 
                        original_tokens, optimized_tokens)
            return msg
    
    def _optimize_content(self, content: str) -> str:
        """Optimize text content for token efficiency."""
        if not content:
            return content
        # If content is only whitespace, return empty string
        if not content.strip():
            return ""

        # Check if content looks like Python code (has indented blocks)
        if self._is_python_code(content):
            logger.debug("🐍 Detected Python code - preserving indentation")
            return self._optimize_python_code(content)

        # Normalize newlines and line endings
        text = content.replace('\r\n', '\n').replace('\r', '\n')

        # Split into lines, trim each line, remove leading/trailing empty lines
        lines = [ln.strip() for ln in text.split('\n')]
        # Remove leading/trailing empty lines
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()

        # Remove empty lines entirely to produce compact paragraphs
        normalized_lines = [re.sub(r"\s+", " ", ln) for ln in lines if ln != ""]
        content = "\n".join(normalized_lines)

        # If content is short, skip heavy optimization
        if self._is_content_already_optimized(content):
            return content

        # Remove verbose assistant phrases and compress common patterns
        content = self._remove_verbose_patterns(content)
        content = self._compress_verbose_patterns(content)

        # Optimize any JSON fragments embedded within the text
        content = self._optimize_json_in_text(content)

        return content

    def _is_python_code(self, content: str) -> bool:
        """Check if content appears to be Python code with indentation."""
        # Look for Python keywords followed by indented blocks
        python_indicators = [
            r'\bdef\s+\w+\s*\(',          # function definitions
            r'\bclass\s+\w+\s*\(',        # class definitions  
            r'\bif\s+.*:',                # if statements
            r'\bwhile\s+.*:',             # while loops
            r'\bfor\s+.*:',               # for loops
            r'\btry\s*:',                 # try blocks
            r'\bexcept\s*.*:',            # except blocks
            r'\bwith\s+.*:',              # context managers
            r'\bprint\s*\(',              # print statements
        ]
        
        # Check if we have Python keywords followed by indented lines
        lines = content.split('\n')
        has_python_keywords = any(re.search(pattern, content, re.MULTILINE) for pattern in python_indicators)
        has_indented_lines = any(line.startswith(('    ', '\t')) for line in lines)
        
        # Special case: if we have multiple Python indicators, treat as Python even without indentation
        python_keyword_count = sum(1 for pattern in python_indicators if re.search(pattern, content, re.MULTILINE))
        
        # Also check for Python-specific patterns that strongly suggest code
        strong_python_indicators = [
            r'=\s*\[.*\]',                    # list assignments
            r'=\s*\{.*\}',                    # dict assignments  
            r'\w+\s*\(\s*\w+\s*\)',          # function calls
            r'f".*\{.*\}"',                   # f-strings
            r"f'.*\{.*\}'",                   # f-strings with single quotes
        ]
        has_strong_indicators = any(re.search(pattern, content, re.MULTILINE) for pattern in strong_python_indicators)
        
        return (has_python_keywords and has_indented_lines) or \
               (python_keyword_count >= 2) or \
               (has_python_keywords and has_strong_indicators)

    def _optimize_python_code(self, content: str) -> str:
        """Optimize Python code while preserving indentation."""
        # Normalize newlines and line endings
        text = content.replace('\r\n', '\n').replace('\r', '\n')
        
        lines = text.split('\n')
        
        # Remove leading and trailing empty lines
        while lines and lines[0].strip() == "":
            lines.pop(0)
        while lines and lines[-1].strip() == "":
            lines.pop()
        
        # For Python code, preserve indentation but clean up trailing whitespace
        cleaned_lines = []
        for line in lines:
            # Remove trailing whitespace but preserve leading indentation
            cleaned_line = line.rstrip()
            
            # Only include non-empty lines or lines that are just whitespace in certain contexts
            if cleaned_line or (cleaned_lines and not cleaned_lines[-1].strip()):
                # Don't add multiple consecutive empty lines
                if not (cleaned_line == "" and cleaned_lines and cleaned_lines[-1] == ""):
                    cleaned_lines.append(cleaned_line)
        
        return '\n'.join(cleaned_lines)

    def _is_content_already_optimized(self, content: str) -> bool:
        """Detect if content is already well-optimized and should be left alone."""
        # Short content should still be considered optimized for tiny values
        if len(content) < 10:
            # Short content doesn't benefit much from optimization
            return True
        
        # Check for signs of already-optimized content
        signs_of_optimization = [
            # Very compact JSON (high ratio of structural chars)
            (content.count('{') + content.count('}') + content.count('[') + content.count(']')) / len(content) > 0.1,
            # Already minimal whitespace (very few consecutive spaces)
            content.count('  ') < 3,  # Very few double spaces
            # Compact data formats (like base64, hex, etc.)
            bool(re.search(r'[a-zA-Z0-9+/]{20,}=*$', content, re.MULTILINE)),  # Base64-like (loose)
            bool(re.search(r'^[0-9a-fA-F]{32,}$', content, re.MULTILINE)),     # Hex-like
            # API response patterns (often already optimized)
            '"id":' in content and '"type":' in content,
            # Log entries that are already timestamped compactly
            bool(re.search(r'\[\d{2}:\d{2}:\d{2}\]', content)),
        ]
        
        # Check for signs of verbose content that should be optimized
        signs_of_verbosity = [
            # Repeated articles that can be compressed
            bool(re.search(r'\b(the|a)\s+(the|a)\b', content)),
            # Excessive dots
            bool(re.search(r'\.{4,}', content)),
            # Common verbose phrases
            'I understand that' in content,
            'Let me think about' in content,
            'I want to help' in content,
            # Multiple consecutive spaces (poor formatting)
            '   ' in content,
        ]
        
        # Quick checks: compact JSON-like content and base64/full-data blocks
        stripped = content.strip()
        if (stripped.startswith('{') and stripped.endswith('}')) or (stripped.startswith('[') and stripped.endswith(']')):
            if '"id":' in content or '"type":' in content:
                return True

        # If the content looks like a single base64 block (possibly padded/newlines), treat as optimized
        # Remove whitespace/newlines to test for continuous base64 sequence
        compact = re.sub(r'\s+', '', content)
        # Full-match base64 (allow padding = or == at the end)
        if len(compact) >= 24 and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', compact):
            return True

        # If we see clear signs of verbosity, don't skip optimization
        if sum(signs_of_verbosity) >= 2:
            return False

        # If multiple signs suggest already optimized, skip aggressive processing
        return sum(signs_of_optimization) >= 3  # Increased threshold
    
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

    # Backwards-compatible helper methods expected by tests
    def _compress_json(self, json_text: str) -> str:
        """Compress JSON string to its most compact representation if valid."""
        if not json_text or not isinstance(json_text, str):
            return json_text
        try:
            obj = json.loads(json_text)
            return json.dumps(obj, separators=(',', ':'))
        except Exception:
            return json_text

    def _remove_verbose_patterns(self, content: str) -> str:
        """Wrapper that removes very verbose assistant-style phrases."""
        if not content:
            return content
        # Remove common assistant filler phrases
        patterns = [
            r'I understand that you want me to\s*',
            r'Let me think about this carefully\.?\s*',
            r'I need to be very careful here\.?\s*',
            r'As an AI assistant,?\s*',
        ]
        result = content
        for p in patterns:
            result = re.sub(p, '', result, flags=re.IGNORECASE)
        return result.strip()

    def _truncate_large_content(self, content: str, max_length: int = 1000) -> str:
        """Truncate large text content with an informative suffix."""
        if not content or len(content) <= max_length:
            return content
        suffix = f"...\n[TRUNCATED: original {len(content)} chars]"
        return content[:max_length - len(suffix)] + suffix

    
    def _optimize_json_in_text(self, content: str) -> str:
        """Optimize JSON formatting within text content, only if it actually helps."""
        # Find JSON-like structures and compress them
        json_pattern = r'\{[^{}]*\}'
        
        def compress_json(match):
            json_str = match.group(0)
            try:
                # Parse and reformat JSON compactly
                json_obj = json.loads(json_str)
                compressed = json.dumps(json_obj, separators=(',', ':'))
                
                # Only use compressed version if it's actually shorter
                if len(compressed) < len(json_str):
                    return compressed
                else:
                    return json_str
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
        """Estimate token count for a single message using improved methodology."""
        total_tokens = 0
        
        # Count content tokens
        if msg.content:
            total_tokens += self._estimate_text_tokens(str(msg.content))
        
        # Count tool calls tokens
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                total_tokens += self._estimate_text_tokens(str(tc))
        
        # Add overhead for message structure (role, metadata, JSON formatting)
        # This is more realistic than the fixed 50 chars
        overhead_tokens = 10  # Reduced from ~14 tokens (50/3.5) to more realistic 10
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            overhead_tokens += 5  # Additional overhead for tool calls
        if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
            overhead_tokens += 3  # Additional overhead for tool responses
            
        return total_tokens + overhead_tokens
    
    def _estimate_text_tokens(self, text: str) -> int:
        """Improved token estimation that considers word boundaries and patterns."""
        if not text:
            return 0
        
        # Basic tokenization patterns
        import re
        
        # Split on word boundaries, keeping punctuation separate
        tokens = re.findall(r'\w+|[^\w\s]', text)
        
        # Apply more realistic token counting rules
        token_count = 0
        for token in tokens:
            if len(token) <= 2:
                # Short tokens (punctuation, short words) = 1 token
                token_count += 1
            elif len(token) <= 6:
                # Medium words = 1 token
                token_count += 1
            else:
                # Long words often split into multiple tokens
                # Estimate based on common subword patterns
                token_count += max(1, len(token) // 4)
        
        # Add tokens for whitespace and formatting
        whitespace_tokens = text.count(' ') + text.count('\n') * 0.5
        
        # JSON/structured data has additional overhead
        if '{' in text and '}' in text:
            json_overhead = text.count('{') + text.count('}') + text.count('"') * 0.3
            token_count += int(json_overhead)
        
        return int(token_count + whitespace_tokens)
    
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
    
    def compress_tool_results(self, messages: List[ChatMessage], max_result_length: int = 600) -> List[ChatMessage]:
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
                    # Truncate with summary; ensure compressed content is shorter than original
                    suffix = f"\n\n[TRUNCATED: Original length {len(content)} chars, showing first {max_result_length} chars]"
                    truncated = content[: max_result_length - len(suffix)]
                    compressed_content = truncated + suffix
                    
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