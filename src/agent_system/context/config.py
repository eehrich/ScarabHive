"""Configuration management for context window handling."""

from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from enum import Enum


class ContextStrategy(Enum):
    """Strategy for handling context window limits.

    TRUNCATE_OLDEST: Simply removes oldest messages when limit is reached.
                     Fast but may lose important context.

    SUMMARIZE_OLDEST: Uses LLM to create intelligent summaries of older messages.
                      Preserves semantic meaning while reducing token count.
                      Recommended for most use cases.

    SLIDING_WINDOW: Maintains fixed-size window of recent messages.
                    Good for real-time applications with continuous flow.

    SMART_COMPRESSION: Advanced compression using semantic analysis.
                       Maximum context retention but higher computational cost.
    """
    TRUNCATE_OLDEST = "TRUNCATE_OLDEST"
    SUMMARIZE_OLDEST = "SUMMARIZE_OLDEST"
    SLIDING_WINDOW = "SLIDING_WINDOW"
    SMART_COMPRESSION = "SMART_COMPRESSION"


class WarningLevel(Enum):
    """Warning levels for context window usage."""
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


@dataclass
class ContextConfig:
    """Configuration for context window management.

    This class defines all parameters for intelligent context window management,
    including warning thresholds, summarization settings, and optimization options.
    """

    # Context window settings
    context_window: int = 32768  # Total available context window in tokens
    summarization_threshold: float = 0.80  # Start summarizing at 80% of context_window (can be float 0-1 for %, or int for absolute tokens)
    prediction_threshold: float = 0.90  # Trigger context management at 90% predicted token usage

    # Warning levels (as percentage of context window)
    warning_thresholds: Dict[WarningLevel, float] = field(default_factory=lambda: {
        WarningLevel.YELLOW: 0.7,   # 70% - First warning, informational
        WarningLevel.ORANGE: 0.85,  # 85% - More urgent warning, action recommended
        WarningLevel.RED: 0.95      # 95% - Critical warning, immediate action needed
    })

    # Context management strategy
    strategy: ContextStrategy = ContextStrategy.SUMMARIZE_OLDEST

    # Summarization settings
    summarization_ratio: float = 0.5  # Reduce to 50% of original size
    preserve_recent_messages: int = 10  # Always keep last N messages intact
    max_summary_words: int = 500  # Maximum words in generated summary (prevents overly long summaries)
    tool_result_preview_chars: int = 200  # Characters to show in tool result preview (balances detail vs brevity)

    # Token optimization settings
    enable_compression: bool = True  # Enable general token compression techniques
    compress_tool_results: bool = True  # Compress verbose tool outputs to save tokens
    max_tool_result_tokens: int = 1000  # Maximum tokens to preserve from tool results

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
        threshold_tokens = self.get_summarization_threshold_tokens()
        return current_tokens >= threshold_tokens

    def should_manage_context_prediction(self, predicted_tokens: int) -> bool:
        """Check if context management should trigger based on token prediction."""
        threshold_tokens = int(self.context_window * self.prediction_threshold)
        return predicted_tokens >= threshold_tokens

    def should_manage_context_actual(self, actual_tokens: int) -> bool:
        """Check if context management should trigger based on actual token usage."""
        threshold_tokens = self.get_summarization_threshold_tokens()
        return actual_tokens >= threshold_tokens

    def get_summarization_threshold_tokens(self) -> int:
        """Get the summarization threshold in absolute tokens."""
        if isinstance(self.summarization_threshold, float) and 0 <= self.summarization_threshold <= 1:
            # Percentage-based threshold
            return int(self.context_window * self.summarization_threshold)
        else:
            # Absolute token threshold (backwards compatibility)
            return int(self.summarization_threshold)

    def get_current_warning_level(self, current_tokens: int) -> Optional[WarningLevel]:
        """Get the current warning level based on token count."""
        usage_ratio = current_tokens / self.context_window

        for level in [WarningLevel.RED, WarningLevel.ORANGE, WarningLevel.YELLOW]:
            if usage_ratio >= self.warning_thresholds[level]:
                return level

        return None