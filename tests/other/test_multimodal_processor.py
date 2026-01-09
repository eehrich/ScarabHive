"""
Tests for multimodal processing utilities (images, audio, text files).
"""
import base64
from io import BytesIO

import pytest
from PIL import Image as PILImage

from agent_system.utils.multimodal_processor import (
    validate_image_file,
    encode_image_to_data_url,
    create_multimodal_message,
    ImageProcessingError,
    # New audio/text functions
    validate_audio_file,
    encode_audio_to_data_url,
    AudioProcessingError,
    validate_text_file,
    read_text_file,
    TextFileProcessingError,
    create_multimodal_message_extended,
    detect_file_type
)
from agent_system.llm.models import ChatMessage


@pytest.fixture
def temp_image(tmp_path):
    """Create a temporary test image."""
    img_path = tmp_path / "test_image.png"
    img = PILImage.new("RGB", (100, 100), color="red")
    img.save(img_path, "PNG")
    return img_path


@pytest.fixture
def large_image(tmp_path):
    """Create a large temporary test image (>10MB)."""
    img_path = tmp_path / "large_image.png"
    # Create a large image that will be > 10MB when saved
    img = PILImage.new("RGB", (5000, 5000), color="blue")
    img.save(img_path, "PNG")
    return img_path


class TestValidateImageFile:
    """Tests for validate_image_file function."""
    
    def test_validate_valid_image(self, temp_image):
        """Test validating a valid image file."""
        img_format, dimensions = validate_image_file(temp_image)
        assert img_format == "png"
        assert dimensions == (100, 100)
    
    def test_validate_nonexistent_file(self, tmp_path):
        """Test validating a file that doesn't exist."""
        non_existent = tmp_path / "nonexistent.png"
        with pytest.raises(ImageProcessingError, match="not found"):
            validate_image_file(non_existent)
    
    def test_validate_directory(self, tmp_path):
        """Test validating a directory instead of a file."""
        with pytest.raises(ImageProcessingError, match="Not a file"):
            validate_image_file(tmp_path)
    
    def test_validate_invalid_image(self, tmp_path):
        """Test validating a non-image file."""
        text_file = tmp_path / "test.txt"
        text_file.write_text("not an image")
        with pytest.raises(ImageProcessingError, match="Cannot open image"):
            validate_image_file(text_file)


class TestEncodeImageToDataUrl:
    """Tests for encode_image_to_data_url function."""
    
    def test_encode_valid_image(self, temp_image):
        """Test encoding a valid image to data URL."""
        data_url, metadata = encode_image_to_data_url(temp_image)
        
        assert data_url.startswith("data:image/png;base64,")
        assert metadata["format"] == "png"
        assert metadata["dimensions"] == (100, 100)
        assert metadata["size_mb"] < 1.0
        assert "path" in metadata
    
    def test_encode_with_max_size(self, tmp_path):
        """Test encoding with maximum size limit."""
        # Create small image
        small_img = tmp_path / "small.png"
        PILImage.new("RGB", (10, 10), color="red").save(small_img, "PNG")
        
        # Get actual size first
        data_url, metadata = encode_image_to_data_url(small_img)
        actual_size_mb = metadata["size_mb"]
        
        # Should succeed with limit above actual size
        data_url2, _ = encode_image_to_data_url(small_img, max_size_mb=actual_size_mb + 0.1)
        assert data_url2.startswith("data:image/png;base64,")
        
        # Should fail with limit below actual size
        with pytest.raises(ImageProcessingError, match="exceeds maximum"):
            encode_image_to_data_url(small_img, max_size_mb=actual_size_mb * 0.5)
    
    def test_encode_large_image_warning(self, tmp_path, caplog):
        """Test that large images generate warnings."""
        import logging
        
        # Create a medium-sized image
        img_path = tmp_path / "medium.png"
        PILImage.new("RGB", (500, 500), color="blue").save(img_path, "PNG")
        
        # Set logger to capture warnings
        logger = logging.getLogger("agent_system.utils.image_processor")
        logger.setLevel(logging.WARNING)
        
        # Get actual size
        _, metadata = encode_image_to_data_url(img_path)
        actual_size = metadata["size_mb"]
        
        # Encode with warning threshold below actual size
        caplog.clear()
        _, _ = encode_image_to_data_url(img_path, warn_size_mb=actual_size * 0.5)
        
        # Check if warning was logged
        warning_found = any("may exceed model limits" in record.message for record in caplog.records)
        assert warning_found, f"Expected warning not found. Actual size: {actual_size} MB, Records: {[r.message for r in caplog.records]}"
    
    def test_data_url_format(self, temp_image):
        """Test that data URL is properly formatted and decodable."""
        data_url, metadata = encode_image_to_data_url(temp_image)
        
        # Extract base64 part
        assert "base64," in data_url
        b64_data = data_url.split("base64,")[1]
        
        # Verify it's valid base64
        decoded = base64.b64decode(b64_data)
        assert len(decoded) > 0
        
        # Verify it's a valid image
        img = PILImage.open(BytesIO(decoded))
        assert img.size == (100, 100)


class TestCreateMultimodalMessage:
    """Tests for create_multimodal_message function."""
    
    def test_create_message_single_image(self, temp_image):
        """Test creating a multimodal message with one image."""
        text = "What's in this image?"
        message = create_multimodal_message(text, [temp_image])
        
        assert isinstance(message, ChatMessage)
        assert message.role == "user"
        assert isinstance(message.content, list)
        assert len(message.content) == 2  # text + 1 image
        
        # Check text content (Pydantic model)
        text_content = message.content[0]
        assert hasattr(text_content, 'type')
        assert text_content.type == "text"
        assert text_content.text == text
        
        # Check image content (Pydantic model)
        image_content = message.content[1]
        assert hasattr(image_content, 'type')
        assert image_content.type == "image_url"
        assert hasattr(image_content, 'image_url')
        assert isinstance(image_content.image_url, dict)
        assert image_content.image_url["url"].startswith("data:image/png;base64,")
    
    def test_create_message_multiple_images(self, tmp_path):
        """Test creating a multimodal message with multiple images."""
        # Create two test images
        img1_path = tmp_path / "img1.png"
        img2_path = tmp_path / "img2.jpg"
        
        PILImage.new("RGB", (50, 50), color="red").save(img1_path, "PNG")
        PILImage.new("RGB", (60, 60), color="blue").save(img2_path, "JPEG")
        
        text = "Compare these images"
        message = create_multimodal_message(text, [img1_path, img2_path])
        
        assert isinstance(message.content, list)
        assert len(message.content) == 3  # text + 2 images
        
        # Check both images are included
        image_contents = [c for c in message.content if hasattr(c, 'type') and c.type == "image_url"]
        assert len(image_contents) == 2
    
    def test_create_message_invalid_image(self, tmp_path):
        """Test creating message with invalid image raises error."""
        text_file = tmp_path / "not_an_image.txt"
        text_file.write_text("invalid")
        
        with pytest.raises(ImageProcessingError):
            create_multimodal_message("Test", [text_file])
    
    def test_create_message_with_size_limit(self, tmp_path):
        """Test creating message with size limit."""
        # Create a reasonably sized image
        img_path = tmp_path / "test.png"
        PILImage.new("RGB", (100, 100), color="blue").save(img_path, "PNG")
        
        # Should fail with very restrictive size limit
        with pytest.raises(ImageProcessingError, match="exceeds maximum"):
            create_multimodal_message("Test", [img_path], max_size_mb=0.0001)
    
    def test_create_message_empty_image_list(self):
        """Test creating message with no images."""
        text = "Just text, no images"
        message = create_multimodal_message(text, [])
        
        assert isinstance(message.content, list)
        assert len(message.content) == 1  # only text
        text_content = message.content[0]
        assert text_content.type == "text"
        assert text_content.text == text


class TestImageProcessingIntegration:
    """Integration tests for image processing workflow."""
    
    def test_end_to_end_workflow(self, tmp_path):
        """Test complete workflow from image file to ChatMessage."""
        # Create test image
        img_path = tmp_path / "test.png"
        test_img = PILImage.new("RGB", (200, 150), color="green")
        test_img.save(img_path, "PNG")
        
        # Validate
        img_format, dimensions = validate_image_file(img_path)
        assert img_format == "png"
        assert dimensions == (200, 150)
        
        # Encode
        data_url, metadata = encode_image_to_data_url(img_path)
        assert "base64," in data_url
        assert metadata["format"] == "png"
        
        # Create message
        message = create_multimodal_message("Analyze this", [img_path])
        assert len(message.content) == 2
        
        # Verify the image in the message matches the encoded data URL
        img_content = message.content[1]
        assert img_content.image_url["url"] == data_url


# =============================================================================
# Audio Processing Tests
# =============================================================================

@pytest.fixture
def temp_audio_wav(tmp_path):
    """Create a minimal valid WAV file."""
    wav_path = tmp_path / "test_audio.wav"
    # Minimal WAV header (44 bytes) + small data
    # RIFF header
    import struct
    sample_rate = 44100
    num_channels = 1
    bits_per_sample = 16
    num_samples = 100
    data_size = num_samples * num_channels * (bits_per_sample // 8)
    
    with open(wav_path, 'wb') as f:
        # RIFF chunk
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))  # file size - 8
        f.write(b'WAVE')
        # fmt chunk
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))  # chunk size
        f.write(struct.pack('<H', 1))   # audio format (PCM)
        f.write(struct.pack('<H', num_channels))
        f.write(struct.pack('<I', sample_rate))
        f.write(struct.pack('<I', sample_rate * num_channels * bits_per_sample // 8))  # byte rate
        f.write(struct.pack('<H', num_channels * bits_per_sample // 8))  # block align
        f.write(struct.pack('<H', bits_per_sample))
        # data chunk
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        f.write(b'\x00' * data_size)  # silence
    
    return wav_path


@pytest.fixture
def temp_audio_mp3(tmp_path):
    """Create a fake MP3 file (valid extension, minimal content)."""
    mp3_path = tmp_path / "test_audio.mp3"
    # Write minimal MP3 frame header (sync word + frame header)
    mp3_path.write_bytes(b'\xff\xfb\x90\x00' + b'\x00' * 100)
    return mp3_path


class TestValidateAudioFile:
    """Tests for validate_audio_file function."""
    
    def test_validate_valid_wav(self, temp_audio_wav):
        """Test validating a valid WAV file."""
        audio_format, size = validate_audio_file(temp_audio_wav)
        assert audio_format == "wav"
        assert size > 0
    
    def test_validate_valid_mp3(self, temp_audio_mp3):
        """Test validating a valid MP3 file."""
        audio_format, size = validate_audio_file(temp_audio_mp3)
        assert audio_format == "mp3"
        assert size > 0
    
    def test_validate_nonexistent_file(self, tmp_path):
        """Test validating a file that doesn't exist."""
        non_existent = tmp_path / "nonexistent.wav"
        with pytest.raises(AudioProcessingError, match="not found"):
            validate_audio_file(non_existent)
    
    def test_validate_unsupported_format(self, tmp_path):
        """Test validating an unsupported audio format."""
        bad_file = tmp_path / "test.xyz"
        bad_file.write_bytes(b"data")
        with pytest.raises(AudioProcessingError, match="Unsupported audio format"):
            validate_audio_file(bad_file)


class TestEncodeAudioToDataUrl:
    """Tests for encode_audio_to_data_url function."""
    
    def test_encode_valid_wav(self, temp_audio_wav):
        """Test encoding a valid WAV to data URL."""
        data_url, metadata = encode_audio_to_data_url(temp_audio_wav)
        
        assert data_url.startswith("data:audio/")
        assert ";base64," in data_url
        assert metadata["format"] == "wav"
        assert metadata["size_mb"] < 1.0
        assert "mime_type" in metadata
    
    def test_encode_with_max_size(self, temp_audio_wav):
        """Test encoding with maximum size limit."""
        # Set an impossibly small limit
        with pytest.raises(AudioProcessingError, match="exceeds maximum"):
            encode_audio_to_data_url(temp_audio_wav, max_size_mb=0.00001)


# =============================================================================
# Text File Processing Tests
# =============================================================================

@pytest.fixture
def temp_text_file(tmp_path):
    """Create a temporary text file."""
    txt_path = tmp_path / "test_document.txt"
    txt_path.write_text("Hello, this is a test document.\nLine 2.\nLine 3.", encoding="utf-8")
    return txt_path


@pytest.fixture
def temp_json_file(tmp_path):
    """Create a temporary JSON file."""
    json_path = tmp_path / "test_data.json"
    json_path.write_text('{"key": "value", "number": 42}', encoding="utf-8")
    return json_path


@pytest.fixture
def temp_python_file(tmp_path):
    """Create a temporary Python file."""
    py_path = tmp_path / "test_script.py"
    py_path.write_text('def hello():\n    print("Hello")\n', encoding="utf-8")
    return py_path


class TestValidateTextFile:
    """Tests for validate_text_file function."""
    
    def test_validate_valid_txt(self, temp_text_file):
        """Test validating a valid text file."""
        ext, size = validate_text_file(temp_text_file)
        assert ext == ".txt"
        assert size > 0
    
    def test_validate_valid_json(self, temp_json_file):
        """Test validating a valid JSON file."""
        ext, size = validate_text_file(temp_json_file)
        assert ext == ".json"
        assert size > 0
    
    def test_validate_valid_python(self, temp_python_file):
        """Test validating a valid Python file."""
        ext, size = validate_text_file(temp_python_file)
        assert ext == ".py"
        assert size > 0
    
    def test_validate_nonexistent_file(self, tmp_path):
        """Test validating a file that doesn't exist."""
        non_existent = tmp_path / "nonexistent.txt"
        with pytest.raises(TextFileProcessingError, match="not found"):
            validate_text_file(non_existent)
    
    def test_validate_unsupported_format(self, tmp_path):
        """Test validating an unsupported text format."""
        bad_file = tmp_path / "test.xyz"
        bad_file.write_text("data")
        with pytest.raises(TextFileProcessingError, match="Unsupported text format"):
            validate_text_file(bad_file)


class TestReadTextFile:
    """Tests for read_text_file function."""
    
    def test_read_valid_text(self, temp_text_file):
        """Test reading a valid text file."""
        content, metadata = read_text_file(temp_text_file)
        
        assert "Hello" in content
        assert metadata["extension"] == ".txt"
        assert metadata["lines"] == 3
        assert metadata["size_mb"] < 1.0
    
    def test_read_json_file(self, temp_json_file):
        """Test reading a JSON file."""
        content, metadata = read_text_file(temp_json_file)
        
        assert '"key"' in content
        assert metadata["extension"] == ".json"
    
    def test_read_with_size_limit(self, tmp_path):
        """Test reading with maximum size limit."""
        large_file = tmp_path / "large.txt"
        large_file.write_text("x" * 10000)
        
        with pytest.raises(TextFileProcessingError, match="exceeds maximum"):
            read_text_file(large_file, max_size_mb=0.001)


# =============================================================================
# Extended Multimodal Message Tests
# =============================================================================

class TestCreateMultimodalMessageExtended:
    """Tests for create_multimodal_message_extended function."""
    
    def test_create_text_only(self):
        """Test creating message with only text."""
        message = create_multimodal_message_extended("Hello world")
        
        assert isinstance(message, ChatMessage)
        assert message.role == "user"
        assert len(message.content) == 1
        assert message.content[0].text == "Hello world"
    
    def test_create_with_text_file(self, temp_text_file):
        """Test creating message with text file attachment."""
        message = create_multimodal_message_extended(
            "Analyze this file",
            text_file_paths=[temp_text_file]
        )
        
        assert isinstance(message, ChatMessage)
        assert len(message.content) == 2  # Text + text_file (separate items now)
        assert message.content[0].type == "text"
        assert message.content[0].text == "Analyze this file"
        assert message.content[1].type == "text_file"
        assert message.content[1].name == "test_document.txt"
        assert "Hello" in message.content[1].content
    
    def test_create_with_image(self, temp_image):
        """Test creating message with image."""
        message = create_multimodal_message_extended(
            "What's in this image?",
            image_paths=[temp_image]
        )
        
        assert len(message.content) == 2  # Text + image
        assert message.content[0].type == "text"
        assert message.content[1].type == "image_url"
    
    def test_create_with_audio(self, temp_audio_wav):
        """Test creating message with audio."""
        message = create_multimodal_message_extended(
            "Transcribe this audio",
            audio_paths=[temp_audio_wav]
        )
        
        assert len(message.content) == 2  # Text + audio
        assert message.content[0].type == "text"
        assert message.content[1].type == "audio"
    
    def test_create_with_all_types(self, temp_image, temp_audio_wav, temp_text_file):
        """Test creating message with all file types."""
        message = create_multimodal_message_extended(
            "Process these files",
            image_paths=[temp_image],
            audio_paths=[temp_audio_wav],
            text_file_paths=[temp_text_file]
        )
        
        # Should have: text + text_file + image + audio (4 items now)
        assert len(message.content) == 4
        
        types = [c.type for c in message.content]
        assert "text" in types
        assert "text_file" in types
        assert "image_url" in types
        assert "audio" in types


# =============================================================================
# File Type Detection Tests
# =============================================================================

class TestDetectFileType:
    """Tests for detect_file_type function."""
    
    def test_detect_image_types(self, tmp_path):
        """Test detecting image file types."""
        for ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
            path = tmp_path / f"test.{ext}"
            path.write_bytes(b"data")
            assert detect_file_type(path) == 'image'
    
    def test_detect_audio_types(self, tmp_path):
        """Test detecting audio file types."""
        for ext in ['mp3', 'wav', 'ogg', 'flac', 'm4a']:
            path = tmp_path / f"test.{ext}"
            path.write_bytes(b"data")
            assert detect_file_type(path) == 'audio'
    
    def test_detect_text_types(self, tmp_path):
        """Test detecting text file types."""
        for ext in ['txt', 'md', 'json', 'py', 'js', 'yaml']:
            path = tmp_path / f"test.{ext}"
            path.write_text("data")
            assert detect_file_type(path) == 'text'
    
    def test_detect_unknown_type(self, tmp_path):
        """Test detecting unknown file types."""
        path = tmp_path / "test.xyz"
        path.write_bytes(b"data")
        assert detect_file_type(path) == 'unknown'
