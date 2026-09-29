"""Tests for MediaStore - inline media preservation."""

import base64
import pytest
from pathlib import Path

from plugins.context_engineer.media_store import MediaStore


class TestMediaStore:
    """Test MediaStore functionality."""
    
    @pytest.fixture
    def temp_storage(self, tmp_path):
        """Create temporary storage path."""
        return tmp_path / "media"
    
    @pytest.fixture
    def media_store(self, temp_storage):
        """Create MediaStore instance."""
        return MediaStore(
            storage_path=temp_storage,
            ttl_seconds=3600,  # 1 hour for tests
            max_files=10
        )
    
    def test_initialization(self, media_store, temp_storage):
        """Test MediaStore initializes correctly."""
        assert media_store.storage_path == temp_storage
        assert media_store.ttl_seconds == 3600
        assert media_store.max_files == 10
        assert temp_storage.exists()
    
    def test_store_base64_audio(self, media_store):
        """Test storing base64 audio data."""
        # Create sample audio data
        audio_data = b"RIFF" + b"\x00" * 100  # Fake WAV header
        base64_data = base64.b64encode(audio_data).decode()
        
        # Store it
        path = media_store.store(
            data=base64_data,
            media_type="audio/wav",
            session_id="test_session",
            source_name="test_audio.wav"
        )
        
        assert path is not None
        assert Path(path).exists()
        assert Path(path).suffix == ".wav"
        
        # Verify content
        stored_data = Path(path).read_bytes()
        assert stored_data == audio_data
    
    def test_store_deduplicates(self, media_store):
        """Test that storing same data returns same path."""
        audio_data = b"FLAC" + b"\x00" * 100
        base64_data = base64.b64encode(audio_data).decode()
        
        # Store twice
        path1 = media_store.store(
            data=base64_data,
            media_type="audio/flac",
            session_id="test_session"
        )
        path2 = media_store.store(
            data=base64_data,
            media_type="audio/flac",
            session_id="test_session"
        )
        
        assert path1 == path2
    
    def test_store_different_data(self, media_store):
        """Test that different data gets different paths."""
        data1 = base64.b64encode(b"DATA1" + b"\x00" * 50).decode()
        data2 = base64.b64encode(b"DATA2" + b"\x00" * 50).decode()
        
        path1 = media_store.store(data1, "audio/mp3", "session1")
        path2 = media_store.store(data2, "audio/mp3", "session1")
        
        assert path1 != path2
    
    def test_cleanup_removes_expired(self, media_store):
        """Test that cleanup removes expired files."""
        import time
        
        # Store data
        data = base64.b64encode(b"OLD_DATA").decode()
        path = media_store.store(data, "audio/mp3", "session1")
        
        # Idle for 2 hours: stored and last used back then
        for hash_key in media_store._metadata:
            media_store._metadata[hash_key]["created_at"] = time.time() - 7200
            media_store._metadata[hash_key]["last_accessed"] = time.time() - 7200
        media_store._save_metadata()

        # Run cleanup
        removed = media_store.cleanup()

        assert removed == 1
        assert not Path(path).exists()

    def test_storing_the_same_bytes_again_keeps_the_file(self, media_store):
        """A dedup hit hands the existing path to a new hint; the file must live."""
        import time

        data = base64.b64encode(b"REUSED_DATA").decode()
        path = media_store.store(data, "audio/mp3", "session1")
        for hash_key in media_store._metadata:
            media_store._metadata[hash_key]["created_at"] = time.time() - 7200
            media_store._metadata[hash_key]["last_accessed"] = time.time() - 7200

        assert media_store.store(data, "audio/mp3", "session1") == path
        assert media_store.cleanup() == 0
        assert Path(path).exists()
    
    def test_cleanup_enforces_max_files(self, media_store):
        """Test that cleanup enforces max_files limit."""
        # Store more than max_files
        paths = []
        for i in range(15):
            data = base64.b64encode(f"DATA_{i}".encode() + b"\x00" * 50).decode()
            path = media_store.store(data, "audio/mp3", "session1")
            paths.append(path)
        
        # Initially all files are stored
        assert len(media_store._metadata) == 15
        
        # Run cleanup to enforce max_files
        removed = media_store.cleanup()
        
        # Should have removed 5 oldest files
        assert removed == 5
        assert len(media_store._metadata) == media_store.max_files
    
    def test_get_stats(self, media_store):
        """Test statistics retrieval."""
        # Store some data
        data = base64.b64encode(b"TEST_DATA" * 10).decode()
        media_store.store(data, "audio/mp3", "session1")
        
        stats = media_store.get_stats()
        
        assert stats["file_count"] == 1
        assert stats["total_size_bytes"] > 0
        assert stats["ttl_seconds"] == 3600
        assert stats["max_files"] == 10


class TestEveryWireShapeIsStoredBeforeEviction:
    """Media that is not stored cannot be restored.

    The extraction used to be four hand-written format branches inside
    compaction.py, and they were one short: video_url was missing. Inline
    video was evicted without ever being written to disk, so the agent had
    no way back to it. Extraction now goes through the shared
    extract_inline_media, the same one media_ops uses.

    Driving EVERY shape is the point - a test for the shape the author
    happens to think of is exactly how the gap survived.
    """

    PAYLOAD = bytes([0, 1]) + b'binary-payload'
    SHAPES = ['anthropic_source', 'openai_image_url', 'gemini_inline_data',
              'openai_audio_url', 'video_url']

    def _item(self, shape):
        b64 = base64.b64encode(self.PAYLOAD).decode()
        return {
            'anthropic_source': {'type': 'image',
                                 'source': {'data': b64, 'media_type': 'image/png'}},
            'openai_image_url': {'type': 'image_url',
                                 'image_url': {'url': 'data:image/png;base64,' + b64}},
            'gemini_inline_data': {'type': 'image',
                                   'inline_data': {'data': b64, 'mime_type': 'image/png'}},
            'openai_audio_url': {'type': 'audio',
                                 'audio_url': 'data:audio/wav;base64,' + b64},
            'video_url': {'type': 'video',
                          'video_url': 'data:video/mp4;base64,' + b64},
        }[shape]

    def _compactor(self, tmp_path):
        from plugins.context_engineer.compaction import LayeredCompactionStrategy

        compactor = LayeredCompactionStrategy.__new__(LayeredCompactionStrategy)
        compactor.media_store = MediaStore(storage_path=tmp_path / 'media',
                                           ttl_seconds=3600, max_files=50)
        compactor.config = type('Cfg', (), {'store_media_before_compaction': True})()
        compactor._warned_shared_session = True
        return compactor

    @pytest.mark.parametrize('shape', SHAPES)
    def test_the_payload_reaches_disk(self, tmp_path, shape):
        stored = self._compactor(tmp_path)._store_inline_media(self._item(shape), 'image', 's1')

        assert stored, shape + ': nothing stored - it could not be restored'
        assert Path(stored).read_bytes() == self.PAYLOAD, \
            shape + ': stored bytes differ from the payload'

    def test_an_item_without_media_stores_nothing(self, tmp_path):
        """Counter-check: the tests above must pass because the shapes are
        recognised, not because the store accepts anything."""
        assert self._compactor(tmp_path)._store_inline_media(
            {'type': 'text', 'text': 'hello'}, 'image', 's1') is None


class TestMediaIdentityForDeduplication:
    """_compute_media_hash decides what counts as the same media.

    It was a fourth hand-written format reader and recognised exactly ONE of
    the five wire shapes - the OpenAI data URL. Everything else hashed to
    None, so deduplication never fired for it: the same 40 MB image twice in
    a conversation stayed twice in the request.

    It also hashed only the first 1000 base64 characters, so two different
    videos sharing a container header compared equal and one was dropped as
    a duplicate - a deletion, not just a missed saving.
    """

    def _hash(self, item):
        from plugins.context_engineer.compaction import LayeredCompactionStrategy
        return LayeredCompactionStrategy.__new__(
            LayeredCompactionStrategy)._compute_media_hash(item)

    def _shapes(self, payload):
        b64 = base64.b64encode(payload).decode()
        return {
            'anthropic': {'type': 'image', 'source': {'data': b64}},
            'openai': {'type': 'image_url',
                       'image_url': {'url': 'data:image/png;base64,' + b64}},
            'gemini': {'type': 'image', 'inline_data': {'data': b64}},
            'audio': {'type': 'audio', 'audio_url': 'data:audio/wav;base64,' + b64},
            'video': {'type': 'video', 'video_url': 'data:video/mp4;base64,' + b64},
            'loose': {'type': 'image', 'data': b64},
        }

    def test_every_shape_is_hashable(self):
        hashes = {k: self._hash(v) for k, v in self._shapes(b'payload').items()}
        unhashed = [k for k, h in hashes.items() if h is None]
        assert not unhashed, 'these shapes never deduplicate: ' + str(unhashed)

    def test_the_same_payload_is_the_same_media_whatever_the_shape(self):
        hashes = set(self._hash(v) for v in self._shapes(b'payload').values())
        assert len(hashes) == 1, 'one payload produced ' + str(len(hashes)) + ' identities'

    def test_a_shared_prefix_is_not_the_same_media(self):
        """The truncated hash merged different files that begin alike."""
        prefix = b'A' * 4000
        one = self._hash({'type': 'video', 'data': base64.b64encode(prefix + b'ONE').decode()})
        two = self._hash({'type': 'video', 'data': base64.b64encode(prefix + b'TWO').decode()})
        assert one and two, 'fixture produced no hash - test would be vacuous'
        assert one != two, 'two different files share one identity - one gets dropped'

    def test_a_path_still_wins_over_the_payload(self):
        assert self._hash({'type': 'image', 'path': '/tmp/a.png'}) == \
               self._hash({'type': 'image', 'path': '/tmp/a.png'})

    def test_an_undecodable_payload_is_not_an_identity_and_does_not_raise(self):
        """The extractor raises on a corrupt payload on purpose. Identity is a
        different question, and a broken item must not take the whole
        compaction down with it."""
        assert self._hash({'type': 'image', 'source': {'data': 'ABC123XYZ' * 199}}) is None

    def test_an_item_without_media_has_no_identity(self):
        assert self._hash({'type': 'text', 'text': 'hello'}) is None
