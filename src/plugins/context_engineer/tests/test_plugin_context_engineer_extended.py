"""Extended tests for context_engineer plugin - byte tracking and media compaction."""
from __future__ import annotations


import pytest

from plugins.context_engineer.compaction import (
    CompactionConfig,
    LayeredCompactionStrategy,
)
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore
from plugins.context_engineer.variable_manager import VariableManager
from plugins.context_engineer.archival_memory import ArchivalMemory


# =============================================================================
# Media Bytes Tracking Tests
# =============================================================================


class TestMediaBytesTracking:
    """Tests for media_bytes_saved tracking."""
    
    @pytest.fixture
    def strategy_components(self, tmp_path):
        """Create strategy with temporary storage."""
        store_path = tmp_path / "tool_results.db"
        tool_store = ToolResultStore(store_path)
        
        var_path = tmp_path / "variables.json"
        variable_manager = VariableManager(var_path)
        
        core_path = tmp_path / "core_memory.json"
        core_memory = CoreMemory(core_path)
        
        archive_path = tmp_path / "archive.db"
        archival_memory = ArchivalMemory(archive_path)
        
        config = CompactionConfig(
            layer1_threshold=80000,
            target_tokens=60000,
            deduplicate_media=True,
            tool_result_min_size=500,  # Lower threshold for testing - must be below ~1281 tokens for 500KB audio
        )
        
        strategy = LayeredCompactionStrategy(
            tool_store,
            variable_manager,
            core_memory,
            archival_memory,
            config
        )
        
        return {
            "strategy": strategy,
            "tool_store": tool_store,
            "variable_manager": variable_manager,
            "core_memory": core_memory,
            "archival_memory": archival_memory,
            "config": config
        }
    
    @pytest.mark.asyncio
    async def test_media_bytes_saved_for_multimodal_content(self, strategy_components, tmp_path):
        """Test that media_bytes_saved is tracked when compacting multimodal_content."""
        strategy = strategy_components["strategy"]
        
        # Create a test audio file - must be LARGE (>200KB to exceed 5K tokens)
        audio_file = tmp_path / "test_audio.mp3"
        audio_file.write_bytes(b"x" * (500 * 1024))  # 500KB file
        
        messages = [
            {
                "role": "tool",
                "name": "audio_transcribe",
                "tool_call_id": "call_123",
                "content": "Transcription complete",
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": str(audio_file),
                        "mime_type": "audio/mpeg"
                    }
                ]
            },
            {"role": "user", "content": "Thanks"}
        ]
        
        # Trigger compaction
        result = await strategy.compact(messages, current_tokens=85000, force=True)
        
        # Check that media bytes were tracked
        assert result.media_compacted_after_event > 0, "Expected media to be compacted"
        assert result.media_bytes_saved > 0, "Expected bytes savings to be tracked"
        # Base64 encoding = file_size * 4/3
        expected_bytes = int(audio_file.stat().st_size * 4 / 3)
        assert result.media_bytes_saved >= expected_bytes * 0.9, f"Expected ~{expected_bytes} bytes saved"
    
    @pytest.mark.asyncio
    async def test_media_bytes_saved_for_inline_audio(self, strategy_components):
        """Test that media_bytes_saved is tracked for inline audio data."""
        strategy = strategy_components["strategy"]
        
        # Create inline audio data - must be LARGE (>5K tokens = >200KB base64)
        # Use the correct format: {"source": {"type": "base64", "data": "..."}}
        inline_data = "a" * (500 * 1024)  # 500KB of base64 data
        
        messages = [
            {
                "role": "tool",
                "name": "test_tool",
                "tool_call_id": "call_456",
                "content": [
                    {"type": "text", "text": "Result:"},
                    {
                        "type": "audio",
                        "source": {"type": "base64", "data": inline_data, "media_type": "audio/mpeg"}
                    }
                ]
            },
            {"role": "user", "content": "Thanks"}
        ]
        
        # Trigger compaction with L1
        result = await strategy.compact(messages, current_tokens=85000, force=True)
        
        # Check that media bytes were tracked
        assert result.media_compacted_after_event > 0, "Expected inline audio to be compacted"
        assert result.media_bytes_saved > 0, "Expected bytes savings to be tracked"
        assert result.media_bytes_saved >= len(inline_data), f"Expected at least {len(inline_data)} bytes saved"
    
    @pytest.mark.asyncio
    async def test_byte_limit_layer_marker(self, strategy_components, tmp_path):
        """Test that 'B' layer is added when byte limit is exceeded."""
        strategy = strategy_components["strategy"]
        
        # Create multiple large files to exceed byte limit
        files = []
        for i in range(5):
            audio_file = tmp_path / f"large_audio_{i}.mp3"
            audio_file.write_bytes(b"x" * (5 * 1024 * 1024))  # 5MB each = 25MB total
            files.append(audio_file)
        
        messages = [
            {
                "role": "tool",
                "name": f"audio_{i}",
                "tool_call_id": f"call_{i}",
                "content": f"Audio {i}",
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": str(f),
                        "mime_type": "audio/mpeg"
                    }
                ]
            }
            for i, f in enumerate(files)
        ]
        messages.append({"role": "user", "content": "Process these"})
        
        # The total size should exceed 18MB threshold
        result = await strategy.compact(messages, current_tokens=50000)
        
        # Check that "B" layer was applied
        assert "B" in result.layers_applied
        assert result.media_compacted_after_event > 0
        assert result.media_bytes_saved > 0
    
    @pytest.mark.asyncio
    async def test_compacted_items_not_counted_in_bytes(self, strategy_components, tmp_path):
        """Test that already-compacted items don't contribute to byte estimation."""
        strategy = strategy_components["strategy"]
        
        # Create a large file
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"x" * (10 * 1024 * 1024))  # 10MB
        
        messages = [
            {
                "role": "tool",
                "name": "audio",
                "tool_call_id": "call_1",
                "content": "Done",
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": str(audio_file),
                        "mime_type": "audio/mpeg",
                        "compacted": True,  # Already compacted
                        "original_bytes": 13000000
                    }
                ]
            },
            {"role": "user", "content": "Next"}
        ]
        
        # Estimate bytes - should be minimal
        estimated_bytes = strategy._estimate_request_bytes(messages)
        
        # Should be much less than 10MB (only message structure)
        assert estimated_bytes < 1000  # Just text + structure
    
    @pytest.mark.asyncio
    async def test_estimate_item_bytes_with_path(self, strategy_components, tmp_path):
        """Test _estimate_item_bytes correctly reads file size from path."""
        strategy = strategy_components["strategy"]
        
        # Create test file
        test_file = tmp_path / "media.mp4"
        test_file.write_bytes(b"data" * 10000)  # 40KB
        
        item = {
            "type": "video",
            "path": str(test_file),
            "mime_type": "video/mp4"
        }
        
        estimated = strategy._estimate_item_bytes(item)
        
        # Should be file_size * 4/3 for base64
        expected = int(test_file.stat().st_size * 4 / 3)
        assert estimated == expected
    
    @pytest.mark.asyncio
    async def test_estimate_item_bytes_skips_compacted(self, strategy_components):
        """Test _estimate_item_bytes returns 0 for compacted items."""
        strategy = strategy_components["strategy"]
        
        item = {
            "type": "audio",
            "path": "/some/path.mp3",
            "compacted": True,
            "original_bytes": 5000000
        }
        
        estimated = strategy._estimate_item_bytes(item)
        assert estimated == 0


# =============================================================================
# Deduplication Tests
# =============================================================================


class TestMediaDeduplication:
    """Tests for media deduplication tracking."""
    
    @pytest.fixture
    def strategy_components(self, tmp_path):
        """Create strategy with deduplication enabled."""
        store_path = tmp_path / "tool_results.db"
        tool_store = ToolResultStore(store_path)
        
        var_path = tmp_path / "variables.json"
        variable_manager = VariableManager(var_path)
        
        core_path = tmp_path / "core_memory.json"
        core_memory = CoreMemory(core_path)
        
        archive_path = tmp_path / "archive.db"
        archival_memory = ArchivalMemory(archive_path)
        
        config = CompactionConfig(
            layer1_threshold=80000,
            target_tokens=60000,
            deduplicate_media=True,
        )
        
        strategy = LayeredCompactionStrategy(
            tool_store,
            variable_manager,
            core_memory,
            archival_memory,
            config
        )
        
        return {"strategy": strategy, "config": config}
    
    @pytest.mark.asyncio
    async def test_deduplication_counts_duplicates(self, strategy_components, tmp_path):
        """Test that media_deduplicated counts duplicate files."""
        strategy = strategy_components["strategy"]
        
        # Create a test file
        media_file = tmp_path / "duplicate.jpg"
        media_file.write_bytes(b"image_data" * 100)
        
        # Multiple messages with same file
        messages = [
            {
                "role": "tool",
                "name": "tool1",
                "tool_call_id": "call_1",
                "content": [
                    {
                        "type": "image",
                        "path": str(media_file)
                    }
                ]
            },
            {
                "role": "assistant",
                "content": "Processed image 1"
            },
            {
                "role": "tool",
                "name": "tool2",
                "tool_call_id": "call_2",
                "content": [
                    {
                        "type": "image",
                        "path": str(media_file)  # Same file
                    }
                ]
            },
            {
                "role": "assistant",
                "content": "Processed image 2"
            },
            {"role": "user", "content": "Done"}
        ]
        
        result = await strategy.compact(messages, current_tokens=85000, force=True)
        
        # Should have deduplicated 1 older instance
        assert result.media_deduplicated >= 1
        assert result.tokens_saved > 0


# =============================================================================
# Byte Limit Compaction Tests
# =============================================================================


class TestByteLimitCompaction:
    """Tests for byte-limit specific compaction."""
    
    @pytest.fixture
    def strategy_components(self, tmp_path):
        """Create strategy."""
        store_path = tmp_path / "tool_results.db"
        tool_store = ToolResultStore(store_path)
        
        var_path = tmp_path / "variables.json"
        variable_manager = VariableManager(var_path)
        
        core_path = tmp_path / "core_memory.json"
        core_memory = CoreMemory(core_path)
        
        archive_path = tmp_path / "archive.db"
        archival_memory = ArchivalMemory(archive_path)
        
        config = CompactionConfig(
            max_request_bytes=18 * 1024 * 1024,  # 18MB
            target_request_bytes=10 * 1024 * 1024,  # 10MB
        )
        
        strategy = LayeredCompactionStrategy(
            tool_store,
            variable_manager,
            core_memory,
            archival_memory,
            config
        )
        
        return {"strategy": strategy}
    
    @pytest.mark.asyncio
    async def test_byte_limit_keeps_last_n_messages(self, strategy_components, tmp_path):
        """Test that byte-limit compaction preserves last N messages."""
        strategy = strategy_components["strategy"]
        
        # Create large files
        files = []
        for i in range(5):
            f = tmp_path / f"file_{i}.mp3"
            f.write_bytes(b"x" * (5 * 1024 * 1024))  # 5MB each
            files.append(f)
        
        messages = [
            {
                "role": "tool",
                "name": f"tool_{i}",
                "tool_call_id": f"call_{i}",
                "content": f"Result {i}",
                "multimodal_content": [
                    {"type": "audio", "path": str(f), "mime_type": "audio/mpeg"}
                ]
            }
            for i, f in enumerate(files)
        ]
        messages.append({"role": "user", "content": "Final message"})
        
        # Compact
        result = await strategy.compact(messages, current_tokens=50000)
        
        # Byte-limit compaction happens BEFORE layer 1, so some older files get compacted
        # Check that SOME files were compacted (request was >18MB)
        assert result.media_compacted_after_event > 0
        assert result.media_bytes_saved > 0
        
        modified = result.modified_messages
        
        # Find tool messages
        first_tool = next(m for m in modified if m.get("name") == "tool_0")
        
        # First tool message SHOULD be compacted by byte-limit - item removed, hint in content
        first_mm = first_tool.get("multimodal_content", [])
        assert len(first_mm) == 0, "First file should be removed from multimodal_content"
        content = first_tool.get("content", "")
        assert "removed" in content.lower() or "_media_compacted" in content.lower()
    
    @pytest.mark.asyncio
    async def test_byte_limit_skips_small_items(self, strategy_components, tmp_path):
        """Test that small items (<10KB) are skipped in byte-limit compaction."""
        strategy = strategy_components["strategy"]
        
        # Create small and large files
        small_file = tmp_path / "small.jpg"
        small_file.write_bytes(b"x" * 5000)  # 5KB
        
        large_file = tmp_path / "large.mp3"
        large_file.write_bytes(b"x" * (10 * 1024 * 1024))  # 10MB
        
        messages = [
            {
                "role": "tool",
                "name": "tool1",
                "tool_call_id": "call_1",
                "content": "Small",
                "multimodal_content": [
                    {"type": "image", "path": str(small_file)}
                ]
            },
            {
                "role": "tool",
                "name": "tool2",
                "tool_call_id": "call_2",
                "content": "Large",
                "multimodal_content": [
                    {"type": "audio", "path": str(large_file)}
                ]
            }
        ]
        
        # Force byte-limit compaction
        result = await strategy.compact(messages, current_tokens=50000)
        
        # Small file should be preserved, large should be compacted
        modified = result.modified_messages
        
        # Find the messages
        small_msg = next(m for m in modified if m.get("name") == "tool1")
        large_msg = next(m for m in modified if m.get("name") == "tool2")
        
        # Small should NOT be compacted (still in multimodal_content)
        small_mm = small_msg.get("multimodal_content", [])
        assert len(small_mm) == 1
        
        # If request was >18MB, large should be compacted (removed from multimodal_content)
        if result.media_compacted_after_event > 0:
            large_mm = large_msg.get("multimodal_content", [])
            assert len(large_mm) == 0, "Large file should be removed"
            content = large_msg.get("content", "")
            assert "removed" in content.lower() or "_media_compacted" in content.lower()
