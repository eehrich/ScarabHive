"""Test event-based media compaction triggers below threshold."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.hooks import HookContext
from plugins.context_engineer.hooks import ContextEngineerPlugin


class TestEventBasedMediaCompaction:
    """Test that event-based media compaction works even below token threshold."""
    
    @pytest.fixture
    def plugin_dir(self, tmp_path):
        """Create plugin directory with schema."""
        plugin_dir = tmp_path / "context_engineer"
        plugin_dir.mkdir()
        
        # Copy schema.yaml from real plugin
        import shutil
        src_dir = Path("src/plugins/context_engineer")
        shutil.copy(src_dir / "schema.yaml", plugin_dir / "schema.yaml")
        
        return plugin_dir
    
    @pytest.fixture
    def hooks_impl(self, plugin_dir, tmp_path):
        """Create ContextEngineerPlugin instance."""
        storage_path = tmp_path / "storage"
        storage_path.mkdir()
        
        impl = ContextEngineerPlugin(plugin_dir)
        
        # Override storage path
        impl._storage_base = storage_path
        
        # Configure for event-based media compaction
        impl.layer1_threshold = 60000  # High threshold to ensure we're below it
        impl.compact_media_after_user_message = True  # Enable event-based compaction
        impl.min_time_between = 0.0  # Disable rate limiting for tests
        
        return impl
    
    @pytest.mark.asyncio
    async def test_media_compaction_below_threshold_with_event(self, hooks_impl, tmp_path):
        """Test that media compaction runs below threshold when event is triggered."""
        # Create a test audio file
        audio_file = tmp_path / "test_audio.mp3"
        audio_file.write_bytes(b"x" * 1024)  # 1KB file
        
        # Messages with media (below threshold: ~500 tokens)
        messages = [
            {
                "role": "user",
                "content": "Previous message with media",
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": str(audio_file),
                        "mime_type": "audio/mpeg"
                    }
                ]
            },
            {
                "role": "assistant",
                "content": "I processed your audio"
            },
            {
                "role": "user",  # This triggers event-based media compaction
                "content": "Thanks, what did you find?"
            }
        ]
        
        # Create mock context
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="test-request",
            session_id="test-session",
            agent=None,
            agent_name="test_agent",
            messages=messages,
            llm_response=None,
            tool_call=None,
            tool_result=None,
            output=None,
            metadata={},
            step=1,
            llm=None,
            cancellation_token=None
        )
        
        # Run the hook
        result = await hooks_impl.engineer_context(context)
        
        # Verify compaction was triggered despite being below threshold
        assert result.success is True
        assert result.modified is True, "Expected media compaction to modify messages"
        
        # Check that first message's media was compacted (removed from multimodal_content)
        modified_messages = result.context.messages
        first_msg = modified_messages[0]
        
        # First message should have compacted media - item removed from multimodal_content
        if hasattr(first_msg, 'multimodal_content'):
            multimodal_content = first_msg.multimodal_content
        else:
            multimodal_content = first_msg.get('multimodal_content')
        
        # multimodal_content should be empty (item removed)
        assert not multimodal_content or len(multimodal_content) == 0, \
            "Expected multimodal_content to be empty after compaction"
        
        # Hint should be added to content
        if hasattr(first_msg, 'content'):
            content = first_msg.content
        else:
            content = first_msg.get('content')
        
        if isinstance(content, list):
            content_text = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
        else:
            content_text = str(content) if content else ""
        
        assert "removed" in content_text.lower() or "compacted" in content_text.lower(), \
            f"Expected compaction hint in content, got: {content_text}"
    
    @pytest.mark.asyncio
    async def test_no_compaction_without_event_below_threshold(self, hooks_impl, tmp_path):
        """Test that compaction is skipped below threshold without event trigger."""
        # Disable event-based compaction
        hooks_impl.compact_media_after_user_message = False
        
        audio_file = tmp_path / "test_audio.mp3"
        audio_file.write_bytes(b"x" * 1024)
        
        # Messages ending with assistant (no event trigger)
        messages = [
            {
                "role": "user",
                "content": "Message with media",
                "multimodal_content": [
                    {
                        "type": "audio",
                        "path": str(audio_file),
                        "mime_type": "audio/mpeg"
                    }
                ]
            },
            {
                "role": "assistant",  # Last message is assistant, no event
                "content": "I processed your audio"
            }
        ]
        
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="test-request",
            session_id="test-session",
            agent=None,
            agent_name="test_agent",
            messages=messages,
            llm_response=None,
            tool_call=None,
            tool_result=None,
            output=None,
            metadata={},
            step=1,
            llm=None,
            cancellation_token=None
        )
        
        result = await hooks_impl.engineer_context(context)
        
        # Should skip compaction (below threshold, no event)
        assert result.success is True
        assert result.modified is False, "Expected no compaction without event trigger"
        assert result.metadata.get("reason") == "below_threshold"
