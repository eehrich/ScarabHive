"""Token estimation utilities for LLM interactions."""

import re
from typing import List
from .models import ChatMessage


def estimate_token_count(messages: List[ChatMessage]) -> int:
    """Enhanced token count estimation with improved accuracy for different content types.
    
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
            content = str(msg.content)
            msg_tokens += estimate_content_tokens(content)

        # Count tool calls with detailed breakdown
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                # Tool call overhead (id, type, function wrapper)
                msg_tokens += 10

                func = tc.get("function", {})
                func_name = func.get("name", "")
                msg_tokens += len(func_name) // 4  # Function names are typically short

                # Tool arguments - often JSON, handle differently
                args_str = str(func.get("arguments", ""))
                if args_str:
                    msg_tokens += estimate_json_tokens(args_str)

        # Count tool results (these can be the biggest consumers)
        if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
            # Tool call ID overhead
            msg_tokens += 8
            # Tool result content
            content = str(msg.content or "")
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
    if is_code_content(content):
        # Code: higher token density due to symbols, operators, keywords
        # Ratio: ~1.2 tokens per word
        return int(words * 1.2)
    elif is_structured_data(content):
        # JSON/XML: compact structure, many punctuation tokens
        # Ratio: ~1.1 tokens per word
        return int(words * 1.1)
    else:
        # Natural language: standard ratio
        # Ratio: ~0.75 tokens per word (standard for English)
        return int(words * 0.75)


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
    return structural_chars + int(words * 0.8)


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
        return int(words * 1.4)  # HTML has many tag tokens
    elif is_code_content(content):
        # Code output
        words = len(content.split())
        return int(words * 1.2)
    else:
        # Plain text tool results
        words = len(content.split())
        return int(words * 0.75)


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
