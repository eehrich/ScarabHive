"""Token estimation utilities for LLM interactions."""

import re
from typing import List, Union, Any
from .models import ChatMessage


# Constants for multimodal token estimation
TOKENS_PER_IMAGE = 1000  # Approximate tokens for an embedded image
TOKENS_PER_AUDIO_SECOND = 25  # Approximate tokens per second of audio


def extract_text_from_content(content: Union[str, List[Any], Any]) -> str:
    """Extract text content from potentially multimodal message content.
    
    Handles:
    - Plain string content (most common)
    - List of content items (multimodal: text, image, audio, etc.)
    - Dict content items
    
    Args:
        content: Message content (str, list, or other)
        
    Returns:
        Extracted text as string
    """
    if content is None:
        return ""
    
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get('type', '')
                if item_type == 'text':
                    text_parts.append(item.get('text', ''))
                elif item_type == 'text_file':
                    # Include text file content for token counting
                    text_parts.append(item.get('content', ''))
                # For image/audio, we don't extract text but they contribute to tokens
            elif hasattr(item, 'type'):
                # Pydantic model (TextContent, ImageContent, etc.)
                item_type = getattr(item, 'type', '')
                if item_type == 'text':
                    text_parts.append(getattr(item, 'text', ''))
                elif item_type == 'text_file':
                    # Include text file content for token counting
                    text_parts.append(getattr(item, 'content', ''))
        return ' '.join(text_parts)
    
    # Fallback for other types
    return str(content)


def count_multimodal_items(content: Union[str, List[Any], Any]) -> dict:
    """Count multimodal items in content.
    
    Args:
        content: Message content
        
    Returns:
        Dict with counts: {'images': N, 'audio': N, 'video': N}
    """
    counts = {'images': 0, 'audio': 0, 'video': 0}
    
    if not isinstance(content, list):
        return counts
    
    for item in content:
        if isinstance(item, dict):
            item_type = item.get('type', '')
        elif hasattr(item, 'type'):
            item_type = getattr(item, 'type', '')
        else:
            continue
            
        if item_type in ('image', 'image_url'):
            counts['images'] += 1
        elif item_type == 'audio':
            counts['audio'] += 1
        elif item_type == 'video':
            counts['video'] += 1
    
    return counts


def estimate_token_count(messages: List[ChatMessage]) -> int:
    """Enhanced token count estimation with improved accuracy for different content types.

    Supports multimodal content (images, audio, video) in addition to text.

    Args:
        messages: List of chat messages to estimate tokens for

    Returns:
        Estimated total token count
    """
    total_tokens = 0

    for msg in messages:
        msg_tokens = 0

        # Base overhead for message structure (role, formatting, etc.)
        msg_tokens += 4  # Base message overhead

        # Count content tokens with content-type aware ratios
        if msg.content:
            # Extract text content (handles multimodal lists)
            text_content = extract_text_from_content(msg.content)
            msg_tokens += estimate_content_tokens(text_content)
            
            # Add tokens for multimodal items (images, audio, video)
            multimodal_counts = count_multimodal_items(msg.content)
            msg_tokens += multimodal_counts['images'] * TOKENS_PER_IMAGE
            # Audio tokens estimated at ~25 tokens/second, assume ~10 seconds average
            msg_tokens += multimodal_counts['audio'] * (TOKENS_PER_AUDIO_SECOND * 10)
            # Video similar to audio but larger
            msg_tokens += multimodal_counts['video'] * (TOKENS_PER_AUDIO_SECOND * 30)

        # Count tool calls with detailed breakdown
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                # Tool call overhead (id, type, function wrapper)
                msg_tokens += 10

                func = tc.get("function", {})
                func_name = func.get("name", "")
                if func_name:  # Only count if name exists
                    msg_tokens += len(func_name) // 4  # Function names are typically short

                # Tool arguments - often JSON, handle differently
                args_str = str(func.get("arguments", ""))
                if args_str:
                    msg_tokens += estimate_json_tokens(args_str)

        # Count tool results (these can be the biggest consumers)
        if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
            # Tool call ID overhead
            msg_tokens += 8
            # Tool result content - extract text for multimodal
            content = extract_text_from_content(msg.content) if msg.content else ""
            if content:
                msg_tokens += estimate_tool_result_tokens(content)

        total_tokens += msg_tokens

    return total_tokens


def estimate_content_tokens(content: str) -> int:
    """Estimate tokens for message content using word-based ratios.

    Args:
        content: Text content to estimate

    Returns:
        Estimated token count
    """
    if not content:
        return 0

    # Word-based estimation (more accurate than character-based)
    words = len(content.split())

    # Detect content type for better estimation
    # Ratios calibrated for OpenAI cl100k_base tokenizer (GPT-4/5)
    if is_code_content(content):
        # Code: higher token density due to symbols, operators, keywords
        # Ratio: ~1.5 tokens per word (empirically measured)
        return int(words * 1.5)
    elif is_structured_data(content):
        # JSON/XML: compact structure, many punctuation tokens
        # Ratio: ~1.8 tokens per word (empirically measured)
        return int(words * 1.8)
    else:
        # Natural language: standard ratio
        # Ratio: ~1.3 tokens per word for cl100k_base (was 0.75, too low)
        return int(words * 1.3)


def estimate_json_tokens(json_str: str) -> int:
    """Estimate tokens for JSON content using word and structure analysis.

    Args:
        json_str: JSON string to estimate

    Returns:
        Estimated token count
    """
    if not json_str:
        return 0

    # Remove JSON structural characters to count actual content words
    content_only = re.sub(r'[{}\[\]":,]', ' ', json_str)
    words = len(content_only.split())

    # Count structural tokens (each structural char is usually a token)
    structural_chars = json_str.count('{') + json_str.count('}') + \
                      json_str.count('[') + json_str.count(']') + \
                      json_str.count('"') + json_str.count(':') + \
                      json_str.count(',')

    # JSON tokens = structural tokens + content words * ratio
    # Increased ratio from 0.8 to 1.3 for cl100k_base accuracy
    return structural_chars + int(words * 1.3)


def estimate_tool_result_tokens(content: str) -> int:
    """Estimate tokens for tool results using content-aware word counting.

    Args:
        content: Tool result content to estimate

    Returns:
        Estimated token count
    """
    if not content:
        return 0

    # Tool results can be JSON, plain text, HTML, etc.
    if content.strip().startswith('{') or content.strip().startswith('['):
        # Likely JSON response
        return estimate_json_tokens(content)
    elif '<' in content and '>' in content:
        # Likely HTML/XML - high token density due to tags
        words = len(content.split())
        return int(words * 1.7)  # HTML has many tag tokens (increased from 1.4)
    elif is_code_content(content):
        # Code output
        words = len(content.split())
        return int(words * 1.5)  # Increased from 1.2
    else:
        # Plain text tool results
        words = len(content.split())
        return int(words * 1.3)  # Increased from 0.75 for cl100k_base


def is_code_content(content: str) -> bool:
    """Detect if content is likely code.

    Args:
        content: Text content to check

    Returns:
        True if content appears to be code
    """
    code_indicators = [
        'def ', 'function ', 'class ', 'import ', 'from ',
        '=>', '&&', '||', '{}', '[]', '()', 'const ', 'let ', 'var ',
        'if (', 'for (', 'while (', 'switch (', 'catch (', 'try {'
    ]

    # Count code-like patterns
    code_score = sum(1 for indicator in code_indicators if indicator in content)

    # Also check character density of symbols common in code
    symbol_chars = sum(1 for c in content if c in '{}[]();=+-*/<>!')
    symbol_ratio = symbol_chars / len(content) if content else 0

    return code_score >= 2 or symbol_ratio > 0.15


def is_structured_data(content: str) -> bool:
    """Detect if content is structured data like JSON, XML, YAML.

    Args:
        content: Text content to check

    Returns:
        True if content appears to be structured data
    """
    content = content.strip()
    return (
        (content.startswith('{') and content.endswith('}')) or
        (content.startswith('[') and content.endswith(']')) or
        content.startswith('<') and content.endswith('>') or
        '\n- ' in content  # YAML-like lists
    )
