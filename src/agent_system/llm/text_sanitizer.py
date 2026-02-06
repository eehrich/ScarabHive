"""Text sanitization utilities for LLM-safe content processing."""

import re
import unicodedata
import logging
from typing import Optional, Union

logger = logging.getLogger(__name__)


def sanitize_for_llm(text: Optional[Union[str, bytes]]) -> str:
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
        # Allow most Unicode scripts that are commonly used in text content
        safe_chars = []
        for char in text:
            code = ord(char)
            if (
                (32 <= code <= 126) or      # ASCII printable
                (160 <= code <= 255) or     # Latin-1 Supplement (ä, ö, ü, etc.)
                (0x100 <= code <= 0x17F) or # Latin Extended-A (č, š, ž, ť, ň, ď, ľ, ą, ę, etc.)
                (0x180 <= code <= 0x24F) or # Latin Extended-B (additional European chars)
                (0x1E00 <= code <= 0x1EFF) or # Latin Extended Additional (Vietnamese, etc.)
                (0x0370 <= code <= 0x03FF) or # Greek and Coptic
                (0x0400 <= code <= 0x04FF) or # Cyrillic
                (0x0500 <= code <= 0x052F) or # Cyrillic Supplement
                (0x0590 <= code <= 0x05FF) or # Hebrew
                (0x0600 <= code <= 0x06FF) or # Arabic
                (0x0750 <= code <= 0x077F) or # Arabic Supplement
                (0x0900 <= code <= 0x097F) or # Devanagari (Hindi, Sanskrit, etc.)
                (0x0980 <= code <= 0x09FF) or # Bengali
                (0x0A00 <= code <= 0x0A7F) or # Gurmukhi (Punjabi)
                (0x0A80 <= code <= 0x0AFF) or # Gujarati
                (0x0B00 <= code <= 0x0B7F) or # Oriya
                (0x0B80 <= code <= 0x0BFF) or # Tamil
                (0x0C00 <= code <= 0x0C7F) or # Telugu
                (0x0C80 <= code <= 0x0CFF) or # Kannada
                (0x0D00 <= code <= 0x0D7F) or # Malayalam
                (0x0E00 <= code <= 0x0E7F) or # Thai
                (0x0E80 <= code <= 0x0EFF) or # Lao
                (0x1000 <= code <= 0x109F) or # Myanmar (Burmese)
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

        # Note: We intentionally do NOT collapse whitespace here, as it can destroy
        # important formatting in code, YAML, structured data, etc. that tools return.
        # Leading/trailing spaces and line structure must be preserved.

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
