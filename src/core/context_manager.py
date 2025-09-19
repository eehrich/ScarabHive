import logging
from typing import Any, Dict, List

class ContextManager:
    def __init__(self, config: Dict[str, Any]):
        self.context_window = config.get('context_window', 65535)
        self.summarization_threshold = config.get('summarization_threshold', 0.75)
        self.prediction_threshold = config.get('prediction_threshold', 0.90)
        self.actual_usage_stats = {
            'total_tokens': 0,
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'last_call_tokens': 0
        }
        # Define warning levels for UI/debug output. Prefer values from config if present.
        raw_levels = config.get('warning_levels') or {}

        def _resolve_level(val, default_frac):
            # Accept int absolute tokens, float fractions (0..1) or strings that parse to either
            try:
                if isinstance(val, (int,)):
                    return int(val)
                if isinstance(val, float):
                    # treat as fraction of context_window if between 0 and 1
                    if 0.0 < val <= 1.0:
                        return int(self.context_window * val)
                    return int(val)
                if isinstance(val, str):
                    v = float(val)
                    if 0.0 < v <= 1.0:
                        return int(self.context_window * v)
                    return int(v)
            except Exception:
                pass
            # fallback to default fraction
            return int(self.context_window * default_frac)

        self.warning_levels = {
            'warning_70': _resolve_level(raw_levels.get('warning_70'), 0.70),
            'warning_85': _resolve_level(raw_levels.get('warning_85'), 0.85),
            'warning_95': _resolve_level(raw_levels.get('warning_95'), 0.95),
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
            # Accumulate totals across calls rather than overwrite so the
            # context manager reflects the session's cumulative usage.
            total = usage_data.get('total_tokens', 0) or 0
            prompt = usage_data.get('prompt_tokens', 0) or 0
            completion = usage_data.get('completion_tokens', 0) or 0

            # Add to cumulative totals
            self.actual_usage_stats['total_tokens'] = self.actual_usage_stats.get('total_tokens', 0) + total
            self.actual_usage_stats['prompt_tokens'] = self.actual_usage_stats.get('prompt_tokens', 0) + prompt
            self.actual_usage_stats['completion_tokens'] = self.actual_usage_stats.get('completion_tokens', 0) + completion
            # last_call_tokens stays as the most recent call's total
            self.actual_usage_stats['last_call_tokens'] = total
            self.logger.debug(f"Accumulated token usage updated by {total} tokens")
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
        # Use a similar heuristic as Agent._estimate_token_count: count
        # characters in message content and tool call metadata and assume
        # ~4 characters per token (conservative). This keeps estimation
        # consistent across components.
        total_chars = 0
        for msg in messages:
            # msg may be a dict or object - attempt to read common fields
            try:
                content = msg.get('content') if isinstance(msg, dict) else getattr(msg, 'content', None)
            except Exception:
                content = None

            if content:
                total_chars += len(str(content))

            # Count tool-call like structures if present
            try:
                tool_calls = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get('function', {}) if isinstance(tc, dict) else tc
                        total_chars += len(str(func.get('name', '')))
                        total_chars += len(str(func.get('arguments', '')))
            except Exception:
                pass

            # Add rough overhead per message
            total_chars += 50

        estimated_tokens = total_chars // 4
        return estimated_tokens