"""Tests for multimodal API endpoint."""

import base64
from types import SimpleNamespace


# TestMultimodalEndpoint removed - requires running server with real LLM


class TestMultimodalValidation:
    """Test validation logic for multimodal endpoint."""
    
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


class TestTheModelTheAttachmentsReach:
    """Frueher stand die Auswahl-Logik als Kopie im Test — eine Mutation in
    app.py liess ihn gruen. Jetzt wird die Funktion selbst gefragt."""

    def test_the_override_wins_over_the_agent_default(self):
        from agent_system.app import capability_model_name

        override = SimpleNamespace(model="gpt-5")
        agent = SimpleNamespace(llm=SimpleNamespace(model="deepseek-chat"))
        assert capability_model_name(override, agent) == "gpt-5"

    def test_without_an_override_the_agent_decides(self):
        from agent_system.app import capability_model_name

        agent = SimpleNamespace(llm=SimpleNamespace(model="deepseek-chat"))
        assert capability_model_name(None, agent) == "deepseek-chat"

    def test_an_override_without_a_model_falls_back(self):
        from agent_system.app import capability_model_name

        agent = SimpleNamespace(llm=SimpleNamespace(model="gpt-5-mini"))
        assert capability_model_name(SimpleNamespace(), agent) == "gpt-5-mini"

    def test_no_model_anywhere_is_no_name(self):
        from agent_system.app import capability_model_name

        assert capability_model_name(None, SimpleNamespace(llm=None)) is None
