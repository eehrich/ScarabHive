import logging
from typing import Any, Dict, List

class ContextManager:
    def __init__(self, config: Dict[str, Any]):
        self.context_window = config.get('context_window', 400000)
        self.summarization_threshold = config.get('summarization_threshold', 128000)
        self.prediction_threshold = config.get('prediction_threshold', 0.90)
        self.actual_usage_stats = {
            'total_tokens': 0,
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'last_call_tokens': 0
        }
        self.logger = logging.getLogger(__name__)

    def should_manage_context_prediction(self, messages: List[Dict]) -> bool:
        """Check if context management should trigger based on token prediction."""
        predicted_tokens = self._estimate_tokens(messages)
        threshold_tokens = int(self.context_window * self.prediction_threshold)

        if predicted_tokens >= threshold_tokens:
            self.logger.debug(f"Prediction trigger: {predicted_tokens}/{threshold_tokens} tokens (90% threshold)")
            return True
        return False

    def should_manage_context_actual(self) -> bool:
        """Check if context management should trigger based on actual token usage."""
        if self.actual_usage_stats['total_tokens'] >= self.summarization_threshold:
            self.logger.debug(f"Actual usage trigger: {self.actual_usage_stats['total_tokens']}/{self.summarization_threshold} tokens")
            return True
        return False

    def update_token_usage(self, usage_data: Dict[str, int]):
        """Update actual token usage from LLM response."""
        if usage_data:
            self.actual_usage_stats.update({
                'total_tokens': usage_data.get('total_tokens', 0),
                'prompt_tokens': usage_data.get('prompt_tokens', 0),
                'completion_tokens': usage_data.get('completion_tokens', 0),
                'last_call_tokens': usage_data.get('total_tokens', 0)
            })
            self.logger.debug(f"Updated token usage: {self.actual_usage_stats}")

    def get_usage_stats(self) -> Dict[str, Any]:
        """Get current usage statistics for debugging."""
        return {
            'actual_usage': self.actual_usage_stats.copy(),
            'context_window': self.context_window,
            'prediction_threshold': self.prediction_threshold,
            'summarization_threshold': self.summarization_threshold,
            'warning_levels': self.warning_levels
        }

    def _estimate_tokens(self, messages: List[Dict]) -> int:
        """Estimate the number of tokens in the given messages."""
        # This is a placeholder implementation. Replace with actual token estimation logic.
        return sum(len(str(message).encode('utf-8')) for message in messages)