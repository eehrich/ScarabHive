"""Configuration management for context window handling."""

from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from enum import Enum


class ContextStrategy(Enum):
    """Strategy for handling context window limits."""
    TRUNCATE_OLDEST = "truncate_oldest"
    SUMMARIZE_OLDEST = "summarize_oldest"
    SLIDING_WINDOW = "sliding_window"
    SMART_COMPRESSION = "smart_compression"


class WarningLevel(Enum):
    """Warning levels for context window usage."""
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


@dataclass
class ContextConfig:
    """Configuration for context window management."""
    
    # Context window settings
    context_window: int = 32768
    summarization_threshold: int = 25600  # Start summarizing at ~80% of context_window by default
    
    # Warning levels (as percentage of context window)
    warning_thresholds: Dict[WarningLevel, float] = field(default_factory=lambda: {
        WarningLevel.YELLOW: 0.7,
        WarningLevel.ORANGE: 0.85,
        WarningLevel.RED: 0.95
    })
    
    # Context management strategy
    strategy: ContextStrategy = ContextStrategy.SUMMARIZE_OLDEST
    
    # Summarization settings
    summarization_ratio: float = 0.5  # Reduce to 50% of original size
    preserve_recent_messages: int = 10  # Always keep last N messages
    
    # Token optimization settings
    enable_compression: bool = True
    compress_tool_results: bool = True
    max_tool_result_tokens: int = 1000
    
    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "ContextConfig":
        """Create ContextConfig from dictionary."""
        # Convert strategy string to enum
        if "strategy" in config_dict:
            if isinstance(config_dict["strategy"], str):
                config_dict["strategy"] = ContextStrategy(config_dict["strategy"])
        
        # Convert warning thresholds
        if "warning_thresholds" in config_dict:
            thresholds = {}
            for level_str, threshold in config_dict["warning_thresholds"].items():
                level = WarningLevel(level_str)
                thresholds[level] = float(threshold)
            config_dict["warning_thresholds"] = thresholds
        
        return cls(**config_dict)
    
    def get_warning_threshold_tokens(self, level: WarningLevel) -> int:
        """Get warning threshold in tokens for a specific level."""
        return int(self.context_window * self.warning_thresholds[level])
    
    def should_summarize(self, current_tokens: int) -> bool:
        """Check if conversation should be summarized."""
        return current_tokens >= self.summarization_threshold
    
    def get_current_warning_level(self, current_tokens: int) -> Optional[WarningLevel]:
        """Get the current warning level based on token count."""
        usage_ratio = current_tokens / self.context_window
        
        for level in [WarningLevel.RED, WarningLevel.ORANGE, WarningLevel.YELLOW]:
            if usage_ratio >= self.warning_thresholds[level]:
                return level
        
        return None