"""
Tests for image processing utilities.
"""
import base64
from io import BytesIO

import pytest
from PIL import Image as PILImage

from agent_system.utils.image_processor import (
    validate_image_file,
    encode_image_to_data_url,
    create_multimodal_message,
    ImageProcessingError
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
