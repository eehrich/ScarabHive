"""Test text sanitization utilities."""

import pytest
from agent_system.utils.text_sanitizer import sanitize_for_llm, sanitize_json_content


class TestTextSanitizer:
    """Test the text sanitization functions."""

    def test_sanitize_for_llm_basic(self):
        """Test basic text sanitization."""
        # Normal text should pass through
        assert sanitize_for_llm("Hello world") == "Hello world"
        
        # Empty/None handling
        assert sanitize_for_llm("") == ""
        assert sanitize_for_llm(None) == ""
        
        # Numbers should be converted to string
        assert sanitize_for_llm(123) == "123"

    def test_sanitize_for_llm_encoding_issues(self):
        """Test sanitization of encoding problems."""
        # Null bytes should be removed
        text_with_nulls = "Hello\x00world"
        result = sanitize_for_llm(text_with_nulls)
        assert "\x00" not in result
        assert "Hello" in result and "world" in result
        
        # Control characters should be removed (except \t, \n, \r)
        text_with_controls = "Hello\x01\x02\x08world\x0B\x0C\x0E\x1F"
        result = sanitize_for_llm(text_with_controls)
        assert result == "Helloworld"
        
        # Tab, newline, carriage return should be preserved
        text_with_whitespace = "Hello\tworld\ntest\rmiddle"
        result = sanitize_for_llm(text_with_whitespace)
        assert "\t" in result
        assert "\n" in result
        assert "\r" in result

    def test_sanitize_for_llm_unicode_issues(self):
        """Test sanitization of problematic Unicode characters."""
        # Zero-width characters should be removed
        text_with_zwsp = "Hello\u200Bworld\u200C\u200D\u200E\u200F"
        result = sanitize_for_llm(text_with_zwsp)
        assert "Helloworld" in result
        assert "\u200B" not in result
        
        # Directional formatting should be removed
        text_with_direction = "Hello\u202Aworld\u202E"
        result = sanitize_for_llm(text_with_direction)
        assert "Helloworld" in result
        assert "\u202A" not in result

    def test_sanitize_for_llm_foreign_characters(self):
        """Test sanitization preserves safe foreign characters."""
        # CJK characters should be preserved
        cjk_text = "你好世界"
        result = sanitize_for_llm(cjk_text)
        assert result == "你好世界"
        
        # Japanese characters should be preserved
        japanese_text = "こんにちは世界"
        result = sanitize_for_llm(japanese_text)
        assert result == "こんにちは世界"
        
        # Korean characters should be preserved
        korean_text = "안녕하세요"
        result = sanitize_for_llm(korean_text)
        assert result == "안녕하세요"

    def test_sanitize_for_llm_whitespace_cleanup(self):
        """Test whitespace normalization."""
        # Multiple spaces should be collapsed
        text_with_spaces = "Hello    world   test"
        result = sanitize_for_llm(text_with_spaces)
        assert result == "Hello world test"
        
        # Leading/trailing whitespace should be stripped
        text_with_padding = "   Hello world   "
        result = sanitize_for_llm(text_with_padding)
        assert result == "Hello world"

    def test_sanitize_json_content(self):
        """Test JSON-specific sanitization."""
        # Basic sanitization should work
        assert sanitize_json_content("Hello world") == "Hello world"
        
        # Control characters should be removed during LLM sanitization
        text_with_controls = "Hello\bworld\f"
        result = sanitize_json_content(text_with_controls)
        assert result == "Helloworld"  # Control chars removed by LLM sanitization

    def test_sanitize_for_llm_with_web_scraper_like_data(self):
        """Test with data similar to what web scraper might encounter."""
        # Mixed encoding issues
        problematic_text = "Café\x00 naïve\u200B résumé\x01 piñata\u202A"
        result = sanitize_for_llm(problematic_text)
        
        # Should preserve valid characters and remove problematic ones
        assert "Café" in result
        assert "naïve" in result  
        assert "résumé" in result
        assert "piñata" in result
        assert "\x00" not in result
        assert "\u200B" not in result
        assert "\x01" not in result
        assert "\u202A" not in result

    def test_sanitize_for_llm_error_handling(self):
        """Test error handling in sanitization."""
        # Should not crash on extreme cases
        result = sanitize_for_llm("\x00" * 1000)
        assert result == ""
        
        # Should handle bytes input
        bytes_input = b"Hello world"
        result = sanitize_for_llm(bytes_input)
        assert result == "Hello world"
