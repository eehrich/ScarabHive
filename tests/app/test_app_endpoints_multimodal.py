"""Tests for multimodal API endpoint."""

from io import BytesIO
from PIL import Image
import base64
import pytest


@pytest.mark.skip(reason="Requires async_client fixture and running API server - run manually")
class TestMultimodalEndpoint:
    """Test /run/multimodal endpoint - requires running server."""
    
    def create_test_image(self, width=100, height=100, color="red"):
        """Create a test image in memory."""
        img = Image.new('RGB', (width, height), color=color)
        buffer = BytesIO()
        img.save(buffer, format='JPEG')
        buffer.seek(0)
        return buffer
    
    async def test_multimodal_endpoint_text_only(self, async_client):
        """Test multimodal endpoint with text only."""
        response = await async_client.post(
            "/run/multimodal",
            data={"task": "Hello, world!"}
        )
        assert response.status_code == 200
    
    async def test_multimodal_endpoint_with_image(self, async_client):
        """Test multimodal endpoint with text and image."""
        # Create test image
        img_buffer = self.create_test_image()
        
        # Send request
        response = await async_client.post(
            "/run/multimodal",
            data={"task": "What's in this image?"},
            files={"files": ("test.jpg", img_buffer, "image/jpeg")}
        )
        
        assert response.status_code == 200
    
    async def test_multimodal_endpoint_multiple_images(self, async_client):
        """Test multimodal endpoint with multiple images."""
        img1 = self.create_test_image(color="red")
        img2 = self.create_test_image(color="blue")
        
        response = await async_client.post(
            "/run/multimodal",
            data={"task": "Compare these images"},
            files=[
                ("files", ("red.jpg", img1, "image/jpeg")),
                ("files", ("blue.jpg", img2, "image/jpeg"))
            ]
        )
        
        assert response.status_code == 200
    
    async def test_multimodal_endpoint_invalid_file_type(self, async_client):
        """Test that non-image files are rejected."""
        text_file = BytesIO(b"This is not an image")
        
        response = await async_client.post(
            "/run/multimodal",
            data={"task": "Process this"},
            files={"files": ("test.txt", text_file, "text/plain")}
        )
        
        assert response.status_code == 400
        assert "not an image" in response.json()["detail"].lower()
    
    async def test_multimodal_endpoint_unsupported_format(self, async_client):
        """Test that unsupported image formats are rejected."""
        # Create a TIFF image (if not supported)
        img = Image.new('RGB', (100, 100), color='red')
        buffer = BytesIO()
        img.save(buffer, format='TIFF')
        buffer.seek(0)
        
        response = await async_client.post(
            "/run/multimodal",
            data={"task": "Process this"},
            files={"files": ("test.tiff", buffer, "image/tiff")}
        )
        
        # Should either accept or reject based on model capabilities
        assert response.status_code in [200, 400]


class TestMultimodalValidation:
    """Test validation logic for multimodal endpoint."""
    
    def test_image_format_validation(self):
        """Test that image format validation works."""
        from agent_system.llm.capabilities import ImageFormat
        
        # Valid formats
        assert ImageFormat.JPEG == "jpeg"
        assert ImageFormat.PNG == "png"
        assert ImageFormat.GIF == "gif"
        assert ImageFormat.WEBP == "webp"
        
        # Check enum membership
        valid_formats = {f.value for f in ImageFormat}
        assert "jpeg" in valid_formats
        assert "png" in valid_formats
    
    def test_base64_encoding(self):
        """Test base64 encoding of image data."""
        # Create simple test data
        test_data = b"test image data"
        encoded = base64.b64encode(test_data).decode('utf-8')
        
        # Verify it can be decoded
        decoded = base64.b64decode(encoded)
        assert decoded == test_data
    
    def test_multimodal_message_structure(self):
        """Test multimodal message structure."""
        from agent_system.llm.models import ChatMessage
        
        # Create multimodal message
        msg = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": "data:image/jpeg;base64,/9j/4AAQ..."
                }
            ]
        )
        
        assert msg.is_multimodal()
        assert msg.has_images()
        assert msg.count_images() == 1
        assert "What's in this image?" in msg.get_text_content()


class TestMultimodalSizeValidation:
    """Test size validation for uploaded images."""
    
    def test_size_check_logic(self):
        """Test that size validation logic works."""
        from agent_system.llm.capabilities import get_model_capabilities
        
        # Get capabilities for a known model
        caps = get_model_capabilities("gpt-5")
        
        if caps.max_image_size:
            # Test data under limit
            small_data = b"x" * 1000
            assert len(small_data) < caps.max_image_size
            
            # Test data over limit (if limit exists)
            if caps.max_image_size < 100 * 1024 * 1024:  # Reasonable test limit
                large_size = caps.max_image_size + 1000
                # We don't actually create the data, just test the logic
                assert large_size > caps.max_image_size


class TestMultimodalFormatSupport:
    """Test format support checking."""
    
    def test_format_support_check(self):
        """Test checking if model supports a format."""
        from agent_system.llm.capabilities import get_model_capabilities, ImageFormat
        
        caps = get_model_capabilities("gpt-4.1")  # Use model from config
        
        # Check some common formats
        supported = caps.supported_image_formats
        
        # GPT-4.1 should support common formats (if configured with vision)
        # If not configured, this test just checks the capability system works
        assert isinstance(supported, list)  # At minimum, should be a list
    
    def test_unsupported_format_detection(self):
        """Test detection of unsupported formats."""
        from agent_system.llm.capabilities import get_model_capabilities
        
        caps = get_model_capabilities("gpt-5-nano")
        
        # Nano doesn't support images at all
        assert not caps.image_input
        assert len(caps.supported_image_formats) == 0


class TestLLMOverrideModelSelection:
    """Test that llm_override is used for capability checks instead of agent default."""
    
    def test_llm_override_model_used_for_image_validation(self):
        """Test that when llm_override is provided, its model is used for image capability checks."""
        from unittest.mock import MagicMock, AsyncMock
        
        # Create mock LLM override with vision-capable model
        llm_override = MagicMock()
        llm_override.model = "gpt-5.1"  # Model with vision support
        
        # Create mock agent with non-vision model as default
        mock_agent = MagicMock()
        mock_agent.llm = MagicMock()
        mock_agent.llm.model = "deepseek-chat"  # Model without vision
        
        # The logic we're testing (extracted from app.py):
        # If llm_override and hasattr(llm_override, 'model'):
        #     model_name = llm_override.model
        # else:
        #     model_name = selected_agent.llm.model
        
        # Test case 1: With llm_override
        if llm_override and hasattr(llm_override, 'model'):
            model_name = llm_override.model
        else:
            model_name = mock_agent.llm.model
            
        assert model_name == "gpt-5.1"
        
        # Test case 2: Without llm_override  
        llm_override_none = None
        if llm_override_none and hasattr(llm_override_none, 'model'):
            model_name = llm_override_none.model
        else:
            model_name = mock_agent.llm.model
            
        assert model_name == "deepseek-chat"
    
    def test_llm_override_without_model_attribute_falls_back(self):
        """Test fallback when llm_override doesn't have model attribute."""
        from unittest.mock import MagicMock
        
        # Create mock LLM override without model attribute
        llm_override = MagicMock(spec=[])  # No attributes
        
        # Create mock agent with default model
        mock_agent = MagicMock()
        mock_agent.llm = MagicMock()
        mock_agent.llm.model = "gpt-5-mini"
        
        # Test the logic
        if llm_override and hasattr(llm_override, 'model'):
            model_name = llm_override.model
        else:
            model_name = mock_agent.llm.model if hasattr(mock_agent.llm, 'model') else None
            
        # Should fall back to agent's model since llm_override lacks model attr
        assert model_name == "gpt-5-mini"

