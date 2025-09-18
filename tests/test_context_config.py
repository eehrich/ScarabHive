"""Tests for ContextConfig class - FIXED VERSION."""

from agent_system.context.config import ContextConfig, ContextStrategy, WarningLevel


class TestContextConfig:
    """Test ContextConfig functionality."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = ContextConfig()
        
        assert config.context_window == 32768
        assert config.summarization_threshold == 25600  # 80% of 32768 
        assert config.strategy == ContextStrategy.SUMMARIZE_OLDEST
        assert config.preserve_recent_messages == 10
        
        # Test default warning thresholds (percentages)
        assert config.warning_thresholds[WarningLevel.YELLOW] == 0.7   # 70%
        assert config.warning_thresholds[WarningLevel.ORANGE] == 0.85  # 85%
        assert config.warning_thresholds[WarningLevel.RED] == 0.95     # 95%
    
    def test_custom_config(self):
        """Test custom configuration values."""
        config = ContextConfig(
            context_window=64000,
            summarization_threshold=50000,
            strategy=ContextStrategy.TRUNCATE_OLDEST,
            preserve_recent_messages=5
        )
        
        assert config.context_window == 64000
        assert config.summarization_threshold == 50000
        assert config.strategy == ContextStrategy.TRUNCATE_OLDEST
        assert config.preserve_recent_messages == 5
    
    def test_should_summarize(self):
        """Test should_summarize method."""
        config = ContextConfig(summarization_threshold=1000)
        
        assert not config.should_summarize(500)   # Below threshold
        assert not config.should_summarize(999)   # Just below threshold
        assert config.should_summarize(1000)      # At threshold
        assert config.should_summarize(1500)      # Above threshold
    
    def test_get_current_warning_level(self):
        """Test get_current_warning_level method."""
        config = ContextConfig(context_window=1000)
        
        assert config.get_current_warning_level(500) is None          # Below all thresholds
        assert config.get_current_warning_level(700) == WarningLevel.YELLOW  # 70%
        assert config.get_current_warning_level(850) == WarningLevel.ORANGE  # 85%
        assert config.get_current_warning_level(950) == WarningLevel.RED     # 95%
    
    def test_get_warning_threshold_tokens(self):
        """Test get_warning_threshold_tokens method."""
        config = ContextConfig(context_window=1000)
        
        assert config.get_warning_threshold_tokens(WarningLevel.YELLOW) == 700   # 70%
        assert config.get_warning_threshold_tokens(WarningLevel.ORANGE) == 850   # 85%
        assert config.get_warning_threshold_tokens(WarningLevel.RED) == 950      # 95%
    
    def test_context_strategies(self):
        """Test all context strategies are valid."""
        for strategy in ContextStrategy:
            config = ContextConfig(strategy=strategy)
            assert config.strategy == strategy
    
    def test_warning_levels_enum(self):
        """Test warning level enumeration."""
        levels = list(WarningLevel)
        assert WarningLevel.YELLOW in levels
        assert WarningLevel.ORANGE in levels
        assert WarningLevel.RED in levels


class TestContextConfigEdgeCases:
    """Test edge cases for ContextConfig."""
    
    def test_zero_context_window(self):
        """Test configuration with zero context window."""
        config = ContextConfig(context_window=0)
        assert config.get_warning_threshold_tokens(WarningLevel.YELLOW) == 0
    
    def test_large_context_window(self):
        """Test configuration with large context window."""
        config = ContextConfig(context_window=2000000)
        assert config.get_warning_threshold_tokens(WarningLevel.YELLOW) == 1400000  # 70%
    
    def test_preserve_messages_boundary(self):
        """Test boundary conditions for preserve_recent_messages."""
        config = ContextConfig(preserve_recent_messages=0)
        assert config.preserve_recent_messages == 0
        
        config = ContextConfig(preserve_recent_messages=100)
        assert config.preserve_recent_messages == 100
    
    def test_summarization_threshold_boundary(self):
        """Test boundary conditions for summarization threshold.""" 
        config = ContextConfig(
            context_window=1000,
            summarization_threshold=1000
        )
        assert config.should_summarize(1000)  # Exactly at threshold
        assert not config.should_summarize(999)  # Just below threshold
    
    def test_from_dict_conversion(self):
        """Test creating config from dictionary."""
        config_dict = {
            "context_window": 50000,
            "strategy": "TRUNCATE_OLDEST",
            "warning_thresholds": {
                "yellow": 0.6,
                "orange": 0.8,
                "red": 0.9
            }
        }
        
        config = ContextConfig.from_dict(config_dict)
        assert config.context_window == 50000
        assert config.strategy == ContextStrategy.TRUNCATE_OLDEST
        assert config.warning_thresholds[WarningLevel.YELLOW] == 0.6
        assert config.warning_thresholds[WarningLevel.ORANGE] == 0.8
        assert config.warning_thresholds[WarningLevel.RED] == 0.9
    
    def test_from_dict_with_enum_strategy(self):
        """Test creating config from dictionary with enum strategy."""
        config_dict = {
            "strategy": ContextStrategy.SLIDING_WINDOW
        }
        
        config = ContextConfig.from_dict(config_dict)
        assert config.strategy == ContextStrategy.SLIDING_WINDOW