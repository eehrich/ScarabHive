"""Tests for ContextManagementConfig class."""

from agent_system.config.models import ContextManagementConfig as ContextConfig


class TestContextConfig:
    """Test ContextConfig functionality."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = ContextConfig()
        
        assert config.enabled is True
        assert config.summarization_threshold == 0.70  # 70%
        assert config.strategy == "SUMMARIZE_OLDEST"
        assert config.preserve_recent_messages == 10
        assert config.summarizer_llm_profile == "turbo"
        
        # Test default warning levels (percentages)
        assert config.warning_levels["yellow"] == 0.70   # 70%
        assert config.warning_levels["orange"] == 0.85  # 85%
        assert config.warning_levels["red"] == 0.95     # 95%
    
    def test_custom_config(self):
        """Test custom configuration values."""
        config = ContextConfig(
            summarization_threshold=0.80,
            strategy="TRUNCATE_OLDEST",
            preserve_recent_messages=5,
            summarizer_llm_profile="normal"
        )
        
        assert config.summarization_threshold == 0.80
        assert config.strategy == "TRUNCATE_OLDEST"
        assert config.preserve_recent_messages == 5
        assert config.summarizer_llm_profile == "normal"
    
    def test_context_strategies(self):
        """Test all context strategies are valid."""
        valid_strategies = ["TRUNCATE_OLDEST", "SUMMARIZE_OLDEST", "SLIDING_WINDOW", "SMART_COMPRESSION"]
        for strategy in valid_strategies:
            config = ContextConfig(strategy=strategy)
            assert config.strategy == strategy
    
    def test_warning_levels(self):
        """Test warning level configuration."""
        custom_levels = {
            "yellow": 0.60,
            "orange": 0.80,
            "red": 0.90
        }
        config = ContextConfig(warning_levels=custom_levels)
        assert config.warning_levels["yellow"] == 0.60
        assert config.warning_levels["orange"] == 0.80
        assert config.warning_levels["red"] == 0.90


class TestContextConfigEdgeCases:
    """Test edge cases for ContextConfig."""
    
    def test_preserve_messages_boundary(self):
        """Test boundary conditions for preserve_recent_messages."""
        config = ContextConfig(preserve_recent_messages=0)
        assert config.preserve_recent_messages == 0
        
        config = ContextConfig(preserve_recent_messages=100)
        assert config.preserve_recent_messages == 100
    
    def test_summarization_threshold_boundary(self):
        """Test boundary conditions for summarization threshold.""" 
        config = ContextConfig(summarization_threshold=0.95)
        assert config.summarization_threshold == 0.95
        
        config = ContextConfig(summarization_threshold=0.50)
        assert config.summarization_threshold == 0.50
    
    def test_prediction_threshold(self):
        """Test prediction threshold configuration."""
        config = ContextConfig(prediction_threshold=0.90)
        assert config.prediction_threshold == 0.90
    
    def test_summarization_settings(self):
        """Test summarization configuration."""
        config = ContextConfig(
            summarization_ratio=0.40,
            max_summary_words=3000,
            tool_result_preview_chars=300
        )
        assert config.summarization_ratio == 0.40
        assert config.max_summary_words == 3000
        assert config.tool_result_preview_chars == 300