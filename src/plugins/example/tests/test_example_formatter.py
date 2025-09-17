"""Tests for the example plugin formatter tool."""

from __future__ import annotations

import pytest

from plugins.example.server import ExampleServer


class TestFormatter:
    """Test the text formatter tool functionality."""
    
    @pytest.fixture
    def server(self):
        """Create server instance for testing."""
        return ExampleServer(name="test", config={"max_text_length": 100})
    
    @pytest.fixture
    def limited_server(self):
        """Create server with limited text length."""
        return ExampleServer(name="test", config={"max_text_length": 10})
    
    async def test_example_formatter_uppercase(self, server):
        """Test uppercase formatting."""
        result = await server.call("test_formatter", {
            "text": "hello world",
            "format": "uppercase"
        })
        
        assert result["original"] == "hello world"
        assert result["format"] == "uppercase"
        assert result["formatted"] == "HELLO WORLD"
        assert result["length"] == 11
    
    async def test_example_formatter_lowercase(self, server):
        """Test lowercase formatting."""
        result = await server.call("test_formatter", {
            "text": "HELLO WORLD",
            "format": "lowercase"
        })
        
        assert result["original"] == "HELLO WORLD"
        assert result["format"] == "lowercase"
        assert result["formatted"] == "hello world"
        assert result["length"] == 11
    
    async def test_example_formatter_title(self, server):
        """Test title case formatting."""
        result = await server.call("test_formatter", {
            "text": "hello world",
            "format": "title"
        })
        
        assert result["original"] == "hello world"
        assert result["format"] == "title"
        assert result["formatted"] == "Hello World"
        assert result["length"] == 11
    
    async def test_example_formatter_reverse(self, server):
        """Test text reversal."""
        result = await server.call("test_formatter", {
            "text": "hello",
            "format": "reverse"
        })
        
        assert result["original"] == "hello"
        assert result["format"] == "reverse"
        assert result["formatted"] == "olleh"
        assert result["length"] == 5
    
    async def test_example_formatter_empty_string(self, server):
        """Test formatting empty string."""
        result = await server.call("test_formatter", {
            "text": "",
            "format": "uppercase"
        })
        
        assert result["original"] == ""
        assert result["formatted"] == ""
        assert result["length"] == 0
    
    async def test_example_formatter_special_characters(self, server):
        """Test formatting with special characters."""
        text = "hello, world! 123 @#$"
        result = await server.call("test_formatter", {
            "text": text,
            "format": "reverse"
        })
        
        assert result["formatted"] == "$#@ 321 !dlrow ,olleh"
    
    async def test_example_formatter_unicode(self, server):
        """Test formatting with unicode characters."""
        text = "café naïve résumé"
        result = await server.call("test_formatter", {
            "text": text,
            "format": "uppercase"
        })
        
        assert result["formatted"] == "CAFÉ NAÏVE RÉSUMÉ"
    
    async def test_example_formatter_invalid_format(self, server):
        """Test invalid format type error."""
        with pytest.raises(ValueError, match="Invalid format 'invalid'"):
            await server.call("test_formatter", {
                "text": "hello",
                "format": "invalid"
            })
    
    async def test_example_formatter_missing_parameters(self, server):
        """Test missing parameters error."""
        with pytest.raises(ValueError, match="Missing required parameters"):
            await server.call("test_formatter", {
                "text": "hello"
            })
    
    async def test_example_formatter_non_string_text(self, server):
        """Test non-string text parameter error."""
        with pytest.raises(TypeError, match="Text parameter must be a string"):
            await server.call("test_formatter", {
                "text": 123,
                "format": "uppercase"
            })
    
    async def test_example_formatter_text_too_long(self, limited_server):
        """Test text length limit enforcement."""
        long_text = "a" * 20  # Exceeds limit of 10
        
        with pytest.raises(ValueError, match="Text length 20 exceeds maximum 10"):
            await limited_server.call("test_formatter", {
                "text": long_text,
                "format": "uppercase"
            })
    
    async def test_example_formatter_text_at_limit(self, limited_server):
        """Test text exactly at length limit."""
        text = "a" * 10  # Exactly at limit
        
        result = await limited_server.call("test_formatter", {
            "text": text,
            "format": "uppercase"
        })
        
        assert result["formatted"] == "A" * 10
        assert result["length"] == 10
    
    async def test_example_formatter_whitespace_handling(self, server):
        """Test formatting with various whitespace."""
        text = "  hello  world  "
        result = await server.call("test_formatter", {
            "text": text,
            "format": "title"
        })
        
        assert result["formatted"] == "  Hello  World  "
    
    async def test_example_formatter_multiline_text(self, server):
        """Test formatting multiline text."""
        text = "line1\nline2\nline3"
        result = await server.call("test_formatter", {
            "text": text,
            "format": "reverse"
        })
        
        assert result["formatted"] == "3enil\n2enil\n1enil"
    
    async def test_example_formatter_all_formats_consistency(self, server):
        """Test that all formats maintain text length."""
        text = "Hello, World! 123"
        formats = ["uppercase", "lowercase", "title", "reverse"]
        
        for format_type in formats:
            result = await server.call("test_formatter", {
                "text": text,
                "format": format_type
            })
            
            assert result["length"] == len(text)
            assert len(result["formatted"]) == len(text)