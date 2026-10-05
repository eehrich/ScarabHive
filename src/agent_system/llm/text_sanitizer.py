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

        # Step 3: Remove zero-width and directional formatting characters, and
        # the invisible tag characters that hide text in scraped content.
        text = re.sub(r'[\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF\U000E0000-\U000E007F]', '', text)

        # Step 4: NFC composes what was decomposed. Not NFKC: that rewrites
        # meaning (x² -> x2, full-width letters, ligatures).
        text = unicodedata.normalize('NFC', text)

        # Step 5: DEL, the C1 controls and the noncharacters U+FFFE/U+FFFF.
        # Everything else stays: an allow-list of scripts used to drop
        # currency signs, arrows, maths, emoji and CJK punctuation from the
        # user's own words.
        text = re.sub(r'[\x7F-\x9F\uFFFE\uFFFF]', '', text)

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
