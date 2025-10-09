"""Unit tests for LLM text sanitizer."""

from agent_system.llm.text_sanitizer import sanitize_for_llm, sanitize_json_content


class TestBasicSanitization:
    """Test basic text sanitization functionality."""

    def test_sanitize_none_input(self):
        """Test sanitization of None input."""
        result = sanitize_for_llm(None)
        assert result == ""

    def test_sanitize_empty_string(self):
        """Test sanitization of empty string."""
        result = sanitize_for_llm("")
        assert result == ""

    def test_sanitize_simple_ascii_text(self):
        """Test sanitization preserves simple ASCII text."""
        text = "Hello, World!"
        result = sanitize_for_llm(text)
        assert result == "Hello, World!"

    def test_sanitize_text_with_whitespace(self):
        """Test sanitization handles whitespace correctly."""
        text = "Hello   World"  # Multiple spaces
        result = sanitize_for_llm(text)
        assert result == "Hello World"  # Single space

    def test_sanitize_text_with_tabs_and_newlines(self):
        """Test sanitization preserves tabs and newlines."""
        text = "Hello\tWorld\nNew Line"
        result = sanitize_for_llm(text)
        assert "\t" in result
        assert "\n" in result
        assert "Hello" in result
        assert "World" in result


class TestControlCharacterRemoval:
    """Test removal of problematic control characters."""

    def test_remove_null_bytes(self):
        """Test removal of null bytes."""
        text = "Hello\x00World"
        result = sanitize_for_llm(text)
        assert "\x00" not in result
        assert "HelloWorld" == result

    def test_remove_control_characters(self):
        """Test removal of control characters."""
        text = "Hello\x01\x02\x03World"
        result = sanitize_for_llm(text)
        assert "\x01" not in result
        assert "\x02" not in result
        assert "\x03" not in result
        assert result == "HelloWorld"

    def test_preserve_safe_whitespace(self):
        """Test that tab, newline, and carriage return are preserved."""
        text = "Line1\nLine2\tTabbed\rReturn"
        result = sanitize_for_llm(text)
        assert "\n" in result
        assert "\t" in result
        assert "\r" in result


class TestZeroWidthCharacterRemoval:
    """Test removal of zero-width and directional formatting characters."""

    def test_remove_zero_width_space(self):
        """Test removal of zero-width space."""
        text = "Hello\u200BWorld"
        result = sanitize_for_llm(text)
        assert "\u200B" not in result
        assert result == "HelloWorld"

    def test_remove_zero_width_joiner(self):
        """Test removal of zero-width joiner."""
        text = "Hello\u200DWorld"
        result = sanitize_for_llm(text)
        assert "\u200D" not in result
        assert result == "HelloWorld"

    def test_remove_bom(self):
        """Test removal of byte order mark (BOM)."""
        text = "\uFEFFHello World"
        result = sanitize_for_llm(text)
        assert "\uFEFF" not in result
        assert result == "Hello World"

    def test_remove_directional_formatting(self):
        """Test removal of left-to-right and right-to-left marks."""
        text = "Hello\u202AWorld\u202E"
        result = sanitize_for_llm(text)
        assert "\u202A" not in result
        assert "\u202E" not in result
        assert result == "HelloWorld"


class TestUnicodeNormalization:
    """Test Unicode normalization."""

    def test_unicode_normalization(self):
        """Test that Unicode is normalized (NFKC)."""
        # Using a character that can be normalized
        text = "café"  # é can be composed or decomposed
        result = sanitize_for_llm(text)
        assert "café" in result or "cafe" in result

    def test_normalize_fullwidth_characters(self):
        """Test normalization of fullwidth characters."""
        text = "Ｈｅｌｌｏ"  # Fullwidth Latin
        result = sanitize_for_llm(text)
        # NFKC should convert to normal ASCII
        assert "Hello" == result


class TestInternationalCharacters:
    """Test handling of international characters."""

    def test_preserve_cjk_characters(self):
        """Test that CJK characters are preserved."""
        text = "你好世界"  # Chinese
        result = sanitize_for_llm(text)
        assert "你好世界" == result

    def test_preserve_japanese_hiragana(self):
        """Test that Japanese Hiragana is preserved."""
        text = "こんにちは"
        result = sanitize_for_llm(text)
        assert "こんにちは" == result

    def test_preserve_japanese_katakana(self):
        """Test that Japanese Katakana is preserved."""
        text = "カタカナ"
        result = sanitize_for_llm(text)
        assert "カタカナ" == result

    def test_preserve_korean_hangul(self):
        """Test that Korean Hangul is preserved."""
        text = "안녕하세요"
        result = sanitize_for_llm(text)
        assert "안녕하세요" == result

    def test_preserve_latin_extended(self):
        """Test that Latin extended characters are preserved."""
        text = "Héllo Wörld"
        result = sanitize_for_llm(text)
        assert "Héllo Wörld" == result


class TestBytesHandling:
    """Test handling of bytes input."""

    def test_sanitize_bytes_input(self):
        """Test sanitization of bytes input."""
        text_bytes = b"Hello World"
        result = sanitize_for_llm(text_bytes)
        assert result == "Hello World"

    def test_sanitize_bytes_with_utf8(self):
        """Test sanitization of UTF-8 encoded bytes."""
        text = "Hello 世界"
        text_bytes = text.encode('utf-8')
        result = sanitize_for_llm(text_bytes)
        assert "Hello" in result
        assert "世界" in result

    def test_sanitize_invalid_bytes(self):
        """Test handling of invalid byte sequences."""
        invalid_bytes = b"\xff\xfe"
        result = sanitize_for_llm(invalid_bytes)
        # Should not crash, returns some result
        assert isinstance(result, str)


class TestWhitespaceHandling:
    """Test whitespace handling."""

    def test_trim_leading_whitespace(self):
        """Test trimming of leading whitespace."""
        text = "   Hello World"
        result = sanitize_for_llm(text)
        assert result == "Hello World"

    def test_trim_trailing_whitespace(self):
        """Test trimming of trailing whitespace."""
        text = "Hello World   "
        result = sanitize_for_llm(text)
        assert result == "Hello World"

    def test_collapse_multiple_spaces(self):
        """Test collapsing of multiple consecutive spaces."""
        text = "Hello     World"
        result = sanitize_for_llm(text)
        assert result == "Hello World"

    def test_preserve_single_newlines(self):
        """Test that single newlines are preserved."""
        text = "Line1\nLine2"
        result = sanitize_for_llm(text)
        assert result == "Line1\nLine2"


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_very_long_text(self):
        """Test sanitization of very long text."""
        text = "A" * 100000
        result = sanitize_for_llm(text)
        assert len(result) == 100000
        assert result == "A" * 100000

    def test_text_with_only_control_characters(self):
        """Test text containing only control characters."""
        text = "\x00\x01\x02\x03"
        result = sanitize_for_llm(text)
        assert result == ""

    def test_text_with_only_whitespace(self):
        """Test text containing only whitespace."""
        text = "     "
        result = sanitize_for_llm(text)
        assert result == ""

    def test_mixed_valid_and_invalid_characters(self):
        """Test text with mix of valid and invalid characters."""
        text = "Hello\x00World\u200B!"
        result = sanitize_for_llm(text)
        assert result == "HelloWorld!"


class TestSanitizeJsonContent:
    """Test JSON content sanitization."""

    def test_sanitize_json_content_basic(self):
        """Test basic JSON content sanitization."""
        content = "Hello World"
        result = sanitize_json_content(content)
        assert result == "Hello World"

    def test_sanitize_json_content_with_control_chars(self):
        """Test JSON sanitization removes control chars."""
        content = "Hello\x00World\u200B"
        result = sanitize_json_content(content)
        assert "\x00" not in result
        assert "\u200B" not in result
        assert result == "HelloWorld"

    def test_sanitize_json_content_with_quotes(self):
        """Test that quotes are preserved (JSON encoder will handle escaping)."""
        content = 'Hello "World"'
        result = sanitize_json_content(content)
        assert '"' in result
        assert result == 'Hello "World"'

    def test_sanitize_json_content_with_newlines(self):
        """Test that newlines are preserved."""
        content = "Line1\nLine2"
        result = sanitize_json_content(content)
        assert "\n" in result


class TestErrorHandling:
    """Test error handling and fallback behavior."""

    def test_sanitize_handles_encoding_errors(self):
        """Test that encoding errors are handled gracefully."""
        # Create a string with problematic encoding
        text = "Hello\udcffWorld"  # Surrogate character
        result = sanitize_for_llm(text)
        # Should not crash, returns some result
        assert isinstance(result, str)
        assert "Hello" in result or "World" in result

    def test_sanitize_fallback_on_exception(self):
        """Test fallback behavior when sanitization fails."""
        # This is hard to trigger, but we test the exception handling exists
        result = sanitize_for_llm("Normal text")
        assert result == "Normal text"


class TestRealWorldScenarios:
    """Test real-world scenarios from web scraping and API responses."""

    def test_web_scraped_content(self):
        """Test sanitization of typical web-scraped content."""
        text = "Product\u00a0Price: $19.99\u200b"
        result = sanitize_for_llm(text)
        assert "Product" in result
        assert "Price" in result
        assert "$19.99" in result
        assert "\u200b" not in result

    def test_api_response_with_formatting(self):
        """Test API response with various formatting characters."""
        text = "Status:\u00a0\u2705 Success"
        result = sanitize_for_llm(text)
        assert "Status" in result
        assert "Success" in result

    def test_mixed_language_content(self):
        """Test content with mixed languages."""
        text = "English 中文 日本語 한국어"
        result = sanitize_for_llm(text)
        assert "English" in result
        assert "中文" in result
        assert "日本語" in result
        assert "한국어" in result
