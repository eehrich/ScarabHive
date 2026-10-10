"""Extended tests for context_engineer plugin - byte tracking and media compaction."""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins.context_engineer.compaction import (
    CompactionConfig,
    LayeredCompactionStrategy,
)
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.media_store import MediaStore
from plugins.context_engineer.tool_result_store import ToolResultStore
from plugins.context_engineer.archival_memory import ArchivalMemory


class TestMediaEvictionBooksWhatItRemoved:
    """The shared eviction counts what it removes -- and only that.

    Both assertions here were unchecked as long as each selection rule had
    its own eviction: the existing byte tests are already made green by
    Layer 1, which fills the counter by another route. A mutation in the
    media eviction itself (not counting up the bytes, reversing the
    selection) therefore stayed invisible -- and ``media_bytes_saved``
    decides in ``_finalize`` whether reasoning artefacts are dropped. A
    counter that is too small later means HTTP 400.

    The two payloads are DELIBERATELY of different size: so the byte count
    alone says WHICH of the two was evicted.
    """

    OLD_PAYLOAD = 3000
    NEW_PAYLOAD = 5000

    @pytest.fixture
    def strategy(self, tmp_path):
        """Only the media eviction runs: the token layers are raised out of
        reach, otherwise Layer 1 colours the counters too.

        WITH ``media_store``: without it ``_store_inline_media`` bails out
        immediately, and the ``store=True`` branch -- the only one in which
        data can be lost -- never ran in this class."""
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, target_tokens=10**9,
                deduplicate_media=False,
                always_compact_media_keep_last=1,
            ),
            media_store=MediaStore(tmp_path / "media"),
        )

    def _conversation(self) -> list[dict]:
        def audio(size: int) -> dict:
            return {"type": "audio",
                    "source": {"type": "base64", "media_type": "audio/wav",
                               "data": "A" * size}}
        return [
            {"role": "user", "content": [{"type": "text", "text": "old"},
                                         audio(self.OLD_PAYLOAD)]},
            {"role": "assistant", "content": "understood"},
            {"role": "user", "content": [{"type": "text", "text": "new"},
                                         audio(self.NEW_PAYLOAD)]},
        ]

    @pytest.mark.asyncio
    async def test_the_byte_counter_matches_the_item_that_actually_left(self, strategy):
        result = await strategy.compact(self._conversation(), current_tokens=100)

        assert result.media_always_compacted == 1, (
            "nothing was compacted -- the test would check nothing")
        assert result.media_bytes_saved == self.OLD_PAYLOAD, (
            "the byte counter matches neither payload "
            f"(old={self.OLD_PAYLOAD}, new={self.NEW_PAYLOAD})")

    @pytest.mark.asyncio
    async def test_the_newest_message_keeps_its_media(self, strategy):
        """The direction of the selection. Reversed, the number of hints
        stays the same -- only the wrong one is gone, and the agent no longer
        sees the file that is being talked about right now."""
        result = await strategy.compact(self._conversation(), current_tokens=100)

        kinds = [[part.get("type") for part in msg["content"]]
                 for msg in result.modified_messages
                 if isinstance(msg.get("content"), list)]
        assert kinds[0] == ["text", "text"], (
            f"the older message holds on to its audio: {kinds[0]}")
        assert kinds[1] == ["text", "audio"], (
            f"the newest message lost its audio: {kinds[1]}")

    @pytest.mark.asyncio
    async def test_the_payload_reaches_the_disk_before_it_leaves_the_context(
            self, strategy, tmp_path):
        """The ``store=True`` branch, which never ran without ``media_store``.

        Inline data is the one thing compaction removes that nobody can
        derive again. If the copy is missing, the hint is an address into the
        void -- exactly the case ``_media_hint`` is meant to prevent."""
        result = await strategy.compact(self._conversation(), current_tokens=100,
                                        session_id="s")

        assert result.media_always_compacted == 1, "nothing compacted"
        stored = [p for p in (tmp_path / "media").rglob("*")
                  if p.is_file() and p.suffix != ".json"]
        assert len(stored) == 1, (
            f"the payload is nowhere on disk: {stored}")
        # The store decodes the base64 before writing -- so the round trip
        # is compared, not the raw text.
        import base64
        assert base64.b64encode(stored[0].read_bytes()).decode() == \
            "A" * self.OLD_PAYLOAD, (
                "the stored file is not the removed payload")


class TestNoHintPointsIntoTheVoid:
    """Every ``read(ref=...)`` order in a hint must be resolvable.

    The eviction during deduplication put the address ``"N/A"`` in for items
    without a path and appended the order anyway -- ``read(ref="N/A")``, a
    call that cannot succeed. With ``"path": ""`` that became
    ``read(ref="")``, an explicit error path. So what is checked is not the
    wording but whether the named address exists.
    """

    @staticmethod
    def _refs(messages) -> list[str]:
        import re
        return [ref
                for msg in messages
                if isinstance(msg.get("content"), list)
                for part in msg["content"]
                if isinstance(part, dict) and part.get("type") == "text"
                for ref in re.findall(r'read\(ref="([^"]*)"', part.get("text", ""))]

    @staticmethod
    def _hints(messages) -> list[str]:
        return [part["text"]
                for msg in messages
                if isinstance(msg.get("content"), list)
                for part in msg["content"]
                if isinstance(part, dict) and part.get("type") == "text"
                and part["text"].startswith("[")]

    def _strategy(self, tmp_path, **cfg):
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, target_tokens=10**9, **cfg),
            media_store=MediaStore(tmp_path / "media"),
        )

    @pytest.mark.asyncio
    async def test_a_duplicate_without_a_path_gets_no_restore_order(self, tmp_path):
        """Deduplication puts nothing on disk -- deliberately, the payload
        still stands further down in the conversation. So the hint must not
        name an address either."""
        strategy = self._strategy(tmp_path, deduplicate_media=True,
                                  always_compact_media_keep_last=0)
        same = {"type": "image", "data": "Z" * 5000}
        messages = [
            {"role": "user", "content": [dict(same)]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [dict(same, path="")]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [dict(same)]},
        ]

        result = await strategy.compact(messages, current_tokens=100, force=True,
                                        session_id="s")

        assert result.media_deduplicated == 2, (
            f"nothing deduplicated -- the test would check nothing "
            f"({result.media_deduplicated})")
        hints = self._hints(result.modified_messages)
        assert len(hints) == 2, f"no hint left behind: {hints}"
        assert self._refs(result.modified_messages) == [], (
            f"the hint names an address that does not exist: {hints}")

    @pytest.mark.asyncio
    async def test_the_byte_limit_layer_leaves_a_reachable_address(self, tmp_path):
        """The most aggressive layer -- it fires at 90 MB, i.e. when the
        payload is largest -- was the only one that did not pass on
        ``store=True``: inline data gone, no copy, no address."""
        strategy = self._strategy(tmp_path, max_request_bytes=50000,
                                  deduplicate_media=False,
                                  always_compact_media_keep_last=0)
        messages = [
            {"role": "user", "content": [
                {"type": "audio",
                 "source": {"type": "base64", "media_type": "audio/wav",
                            "data": "QQ==" * 30000}}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "and?"},
        ]

        result = await strategy.compact(messages, current_tokens=100,
                                        session_id="s")

        assert "B" in result.layers_applied, (
            f"the byte layer did not run at all: {result.layers_applied}")
        refs = self._refs(result.modified_messages)
        assert len(refs) == 1, (
            f"no restorable hint: "
            f"{self._hints(result.modified_messages)}")
        assert Path(refs[0]).is_file(), (
            f"the hint points at a file that does not exist: {refs[0]}")


class TestEveryEvictedAttachmentLeavesATrace:
    """A tool result normally carries SEVERAL attachments.

    With JSON content every hint wrote into the same key
    ``_media_compacted`` and overwrote the previous one: of two attachments
    one survived, the other vanished without a trace from the request. With
    plain text both were always appended -- the same message, two results.
    """

    def _strategy(self, tmp_path):
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, target_tokens=10**9,
                deduplicate_media=False,
                compact_media_after_user_message=True),
            media_store=MediaStore(tmp_path / "media"),
        )

    @staticmethod
    def _messages(content):
        return [
            {"role": "tool", "tool_call_id": "c1", "name": "grab",
             "content": content,
             "multimodal_content": [
                 {"type": "audio", "path": "/x/one.wav"},
                 {"type": "image", "path": "/x/two.png"},
             ]},
            {"role": "user", "content": "and?"},
        ]

    @pytest.mark.parametrize("content", ['{"ok": true}', "fetched two files"],
                             ids=["json", "plain"])
    @pytest.mark.asyncio
    async def test_both_attachments_are_still_named_after_eviction(
            self, tmp_path, content):
        result = await self._strategy(tmp_path).compact(
            self._messages(content), current_tokens=100, force=True,
            trigger_event="user_message", session_id="s")

        tool_msg = result.modified_messages[0]
        assert result.media_compacted_after_event == 2, (
            "not both attachments were evicted -- the test checks nothing")
        assert tool_msg["multimodal_content"] == []
        assert "one.wav" in tool_msg["content"], (
            f"the first attachment vanished without a trace: {tool_msg['content']}")
        assert "two.png" in tool_msg["content"], (
            f"the second attachment vanished without a trace: {tool_msg['content']}")


class TestVideoIsAMediumLikeTheOthers:
    """``_MEDIA_TYPES`` lists ``video``, yet two places did not count it.

    Both times a hard-coded copy of the type list: the byte estimate saw
    inline video as 0 bytes (the 90 MB emergency layer never fires, the
    provider rejects), and Layer 1 left the full payload in place.
    """

    def _strategy(self, tmp_path, **cfg):
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db"),
            config=CompactionConfig(**cfg),
            media_store=MediaStore(tmp_path / "media"),
        )

    @staticmethod
    def _inline(kind: str, mime: str, size: int) -> dict:
        return {"type": kind,
                "source": {"type": "base64", "media_type": mime,
                           "data": "V" * size}}

    def test_inline_video_weighs_the_same_as_inline_image(self, tmp_path):
        """Differential instead of against a number: the same payload, once
        as an image, once as a video -- the estimate must not depend on it."""
        strategy = self._strategy(tmp_path)
        as_video = strategy._estimate_request_bytes(
            [{"role": "user", "content": [self._inline("video", "video/mp4", 100000)]}])
        as_image = strategy._estimate_request_bytes(
            [{"role": "user", "content": [self._inline("image", "image/png", 100000)]}])

        assert as_video == as_image, (
            f"inline video does not count towards the byte limit "
            f"(video={as_video}, image={as_image})")

    @pytest.mark.asyncio
    async def test_layer1_evicts_inline_video(self, tmp_path):
        strategy = self._strategy(tmp_path, tool_result_min_size=100,
                                  layer1_threshold=1, deduplicate_media=False,
                                  always_compact_media_keep_last=0)
        messages = [
            {"role": "user", "content": [self._inline("video", "video/mp4", 80000)]},
            {"role": "user", "content": "what was that?"},
        ]

        result = await strategy.compact(messages, current_tokens=10**6,
                                        session_id="s")

        kinds = [p.get("type") for p in result.modified_messages[0]["content"]]
        assert kinds == ["text"], (
            f"Layer 1 keeps the full video payload: {kinds}")


class TestTheSubjectSurvivesAnUnexpectedType:
    """``multimodal_content`` does not promise a ``type`` string.

    The subject came from ``item["type"].title()``: with a non-string the
    whole compaction blew up with ``AttributeError``, and ``image_url``
    became "Image_Url" -- a word the model can do nothing with.
    """

    @pytest.mark.asyncio
    async def test_a_non_string_type_does_not_kill_the_compaction(self, tmp_path):
        strategy = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9, target_tokens=10**9,
                deduplicate_media=False,
                compact_media_after_user_message=True),
            media_store=MediaStore(tmp_path / "media"),
        )
        messages = [
            {"role": "tool", "tool_call_id": "c", "name": "g", "content": "{}",
             "multimodal_content": [{"type": {"weird": 1}, "path": "/x/a.bin"}]},
            {"role": "user", "content": "and?"},
        ]

        result = await strategy.compact(messages, current_tokens=100, force=True,
                                        trigger_event="user_message",
                                        session_id="s")

        assert result.media_compacted_after_event == 1, (
            "the attachment was left in place -- the test checks nothing")
        assert "a.bin" in result.modified_messages[0]["content"]


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
            core_memory,
            archival_memory,
            config
        )
        
        return {
            "strategy": strategy,
            "tool_store": tool_store,
            "core_memory": core_memory,
            "archival_memory": archival_memory,
            "config": config
        }
    
    @pytest.mark.asyncio
    async def test_media_bytes_saved_for_multimodal_content(self, strategy_components, tmp_path):
        """Test that media_bytes_saved is tracked when compacting multimodal_content."""
        strategy = strategy_components["strategy"]
        # Byte accounting is under test, not recency: the only tool result is
        # also the newest, whose media Layer 1 keeps by default.
        strategy.config.tool_result_keep_last = 0

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
        
        # This class's fixture leaves max_request_bytes at the 90MB default;
        # this test exercises the byte-limit trigger, so cap it below the 25MB
        # payload (the sibling byte-threshold fixture sets 18MB the same way).
        strategy.config.max_request_bytes = 18 * 1024 * 1024  # 18MB
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
