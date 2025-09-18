"""Tests for context configuration and warning levels."""

import pytest
from agent_system.context.config import (
    ContextConfig, 
    ContextStrategy, 
    WarningLevel
)


class TestContextConfig:
    """Test the ContextConfig dataclass."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = ContextConfig()
        
        assert config.context_window == 32768
        assert config.strategy == ContextStrategy.SUMMARIZE_OLDEST
        assert config.preserve_recent_messages == 10
        assert config.early_summarization_threshold == 26214  # 80% of 32768
        
        # Test default warning thresholds
        assert config.warning_thresholds[WarningLevel.YELLOW] == 22937  # 70%
        assert config.warning_thresholds[WarningLevel.ORANGE] == 27853  # 85%
        assert config.warning_thresholds[WarningLevel.RED] == 31129    # 95%
    
    def test_custom_config(self):
        """Test custom configuration values."""
        config = ContextConfig(
            context_window=100000,
            strategy=ContextStrategy.TRUNCATE_OLDEST,
            preserve_recent_messages=15,
            early_summarization_threshold=50000
        )
        
        assert config.context_window == 100000
        assert config.strategy == ContextStrategy.TRUNCATE_OLDEST
        assert config.preserve_recent_messages == 15
        assert config.early_summarization_threshold == 50000
        
        # Test calculated warning thresholds
        assert config.warning_thresholds[WarningLevel.YELLOW] == 70000   # 70%
        assert config.warning_thresholds[WarningLevel.ORANGE] == 85000   # 85%
        assert config.warning_thresholds[WarningLevel.RED] == 95000      # 95%
    
    def test_should_summarize(self):
        """Test the should_summarize method."""
        config = ContextConfig(early_summarization_threshold=1000)
        
        assert not config.should_summarize(500)   # Below threshold
        assert not config.should_summarize(999)   # Just below threshold
        assert config.should_summarize(1000)      # At threshold
        assert config.should_summarize(1500)      # Above threshold
    
    def test_get_warning_level(self):
        """Test warning level detection."""
        config = ContextConfig(context_window=1000)
        # Thresholds: Yellow=700, Orange=850, Red=950
        
        assert config.get_warning_level(500) is None          # Below all thresholds
        assert config.get_warning_level(700) == WarningLevel.YELLOW   # At yellow
        assert config.get_warning_level(750) == WarningLevel.YELLOW   # Above yellow
        assert config.get_warning_level(850) == WarningLevel.ORANGE   # At orange
        assert config.get_warning_level(900) == WarningLevel.ORANGE   # Above orange
        assert config.get_warning_level(950) == WarningLevel.RED      # At red
        assert config.get_warning_level(999) == WarningLevel.RED      # Above red
    
    def test_context_strategies(self):
        """Test all context strategy options."""
        strategies = [
            ContextStrategy.TRUNCATE_OLDEST,
            ContextStrategy.SUMMARIZE_OLDEST,
            ContextStrategy.SLIDING_WINDOW,
            ContextStrategy.SMART_COMPRESSION
        ]
        
        for strategy in strategies:
            config = ContextConfig(strategy=strategy)
            assert config.strategy == strategy
    
    def test_warning_levels_enum(self):
        """Test warning level enum values."""
        assert WarningLevel.YELLOW.value == "yellow"
        assert WarningLevel.ORANGE.value == "orange"
        assert WarningLevel.RED.value == "red"


class TestContextConfigEdgeCases:
    """Test edge cases and validation."""
    
    def test_zero_context_window(self):
        """Test behavior with zero context window."""
        config = ContextConfig(context_window=0)
        assert config.warning_thresholds[WarningLevel.YELLOW] == 0
        assert config.warning_thresholds[WarningLevel.ORANGE] == 0
        assert config.warning_thresholds[WarningLevel.RED] == 0
    
    def test_large_context_window(self):
        """Test behavior with very large context window."""
        config = ContextConfig(context_window=2000000)  # 2M tokens
        
        assert config.warning_thresholds[WarningLevel.YELLOW] == 1400000  # 70%
        assert config.warning_thresholds[WarningLevel.ORANGE] == 1700000  # 85%
        assert config.warning_thresholds[WarningLevel.RED] == 1900000     # 95%
    
    def test_preserve_messages_boundary(self):
        """Test preserve_recent_messages boundary conditions."""
        config = ContextConfig(preserve_recent_messages=0)
        assert config.preserve_recent_messages == 0
        
        config = ContextConfig(preserve_recent_messages=1000)
        assert config.preserve_recent_messages == 1000
    
    def test_early_summarization_threshold_boundary(self):
        """Test early summarization threshold edge cases."""
        config = ContextConfig(
            context_window=1000,
            early_summarization_threshold=1000  # Same as context window
        )
        assert config.early_summarization_threshold == 1000
        assert config.should_summarize(1000)
        assert not config.should_summarize(999)
        
        # Test threshold larger than context window
        config = ContextConfig(
            context_window=1000,
            early_summarization_threshold=1500  # Larger than context window
        )
        assert config.early_summarization_threshold == 1500
        assert config.should_summarize(1500)
        assert not config.should_summarize(1000)  # Below threshold even though above context window