"""Text sanitization utilities for LLM-safe content processing."""

import re
import unicodedata
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def sanitize_for_llm(text: Optional[str]) -> str:
    """
    Sanitize text content to ensure it's safe for LLM consumption.
    
    This function removes problematic characters that can cause issues
    with LLM APIs, particularly OpenAI, when processing international
    or binary content from web scraping and other sources.
    
    Args:
        text: Input text to sanitize, can be None
        
    Returns:
        Clean, LLM-safe text string
    """
    if not text:
        return ""
    
    if not isinstance(text, str):
        # Convert to string if needed, handling bytes properly
        if isinstance(text, bytes):
            try:
                text = text.decode('utf-8', errors='ignore')
            except Exception as e:
                logger.warning("Failed to decode bytes to UTF-8, returning empty string: %s", e)
                return ""
        else:
            text = str(text)
    
    try:
        # Step 1: Ensure proper UTF-8 encoding
        # Re-encode to clean up any encoding issues
        text_bytes = text.encode('utf-8', errors='ignore')
        text = text_bytes.decode('utf-8', errors='ignore')
        
        # Step 2: Remove null bytes and problematic control characters
        text = re.sub(r'\x00', '', text)  # null bytes
        text = re.sub(r'[\x01-\x08\x0B\x0C\x0E-\x1F]', '', text)  # control chars except \t, \n, \r
        
        # Step 3: Remove zero-width and directional formatting characters
        text = re.sub(r'[\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF]', '', text)
        
        # Step 4: Normalize Unicode to remove problematic combining characters
        text = unicodedata.normalize('NFKC', text)
        
        # Step 5: Keep only safe character ranges
        # Allow: ASCII printable, basic Latin extended, common symbols, basic CJK
        safe_chars = []
        for char in text:
            code = ord(char)
            if (
                (32 <= code <= 126) or      # ASCII printable
                (160 <= code <= 255) or     # Latin-1 Supplement
                (code in [9, 10, 13]) or    # Tab, newline, carriage return
                (0x2000 <= code <= 0x206F and code not in range(0x200B, 0x200F+1) and 
                 code not in range(0x202A, 0x202E+1) and code not in range(0x2060, 0x2069+1)) or  # General punctuation (safe subset)
                (0x4E00 <= code <= 0x9FFF) or  # CJK Unified Ideographs
                (0x3040 <= code <= 0x309F) or  # Hiragana
                (0x30A0 <= code <= 0x30FF) or  # Katakana
                (0xAC00 <= code <= 0xD7AF)     # Hangul
            ):
                safe_chars.append(char)
        
        text = ''.join(safe_chars)
        
        # Step 6: Clean up excessive whitespace
        text = re.sub(r'[ ]+', ' ', text)  # Multiple spaces to single space (but preserve tabs, newlines)
        text = text.strip()
        
        return text
        
    except Exception as e:
        # Fallback: return empty string if sanitization fails completely
        logger.warning("Text sanitization failed completely, returning empty string: %s", e)
        return ""


def sanitize_json_content(content: str) -> str:
    """
    Sanitize content that will be JSON-encoded for LLM consumption.
    
    Args:
        content: Content to sanitize
        
    Returns:
        JSON-safe, LLM-safe content
    """
    # First apply general LLM sanitization
    content = sanitize_for_llm(content)
    
    # Note: We don't need to escape JSON control characters here since
    # json.dumps() will handle proper escaping. We just need to ensure
    # the content is LLM-safe before JSON encoding.
    
    return content
