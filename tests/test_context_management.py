import pytest
import asyncio
from unittest.mock import Mock, patch
from src.core.context_manager import ContextManager

class TestContextManagementDualTrigger:
    @pytest.fixture
    def context_manager(self):
        config = {
            'enabled': True,
            'context_window': 10000,
            'prediction_threshold': 0.90,
            'summarization_threshold': 8000,
            'strategy': 'SUMMARIZE_OLDEST',
            'preserve_recent_messages': 5
        }
        return ContextManager(config)

    def test_prediction_trigger_threshold(self, context_manager):
        """Test that prediction trigger activates at 90% threshold."""
        # Mock messages that would exceed 90% of context window
        messages = [{"role": "user", "content": "x" * 9500}]  # Should trigger at 9000 tokens (90% of 10000)

        with patch.object(context_manager, '_estimate_tokens', return_value=9500):
            assert context_manager.should_manage_context_prediction(messages) is True

    def test_prediction_trigger_below_threshold(self, context_manager):
        """Test that prediction trigger doesn't activate below 90% threshold."""
        messages = [{"role": "user", "content": "short message"}]

        with patch.object(context_manager, '_estimate_tokens', return_value=5000):
            assert context_manager.should_manage_context_prediction(messages) is False

    def test_actual_usage_trigger(self, context_manager):
        """Test actual usage trigger based on LLM response."""
        # Update with usage that exceeds summarization threshold
        usage_data = {
            'total_tokens': 8500,
            'prompt_tokens': 7000,
            'completion_tokens': 1500
        }

        context_manager.update_token_usage(usage_data)
        assert context_manager.should_manage_context_actual() is True

    def test_actual_usage_below_threshold(self, context_manager):
        """Test actual usage trigger below threshold."""
        usage_data = {
            'total_tokens': 5000,
            'prompt_tokens': 4000,
            'completion_tokens': 1000
        }

        context_manager.update_token_usage(usage_data)
        assert context_manager.should_manage_context_actual() is False

    def test_usage_stats_tracking(self, context_manager):
        """Test that usage statistics are properly tracked."""
        usage_data = {
            'total_tokens': 7500,
            'prompt_tokens': 6000,
            'completion_tokens': 1500
        }

        context_manager.update_token_usage(usage_data)
        stats = context_manager.get_usage_stats()

        assert stats['actual_usage']['total_tokens'] == 7500
        assert stats['actual_usage']['prompt_tokens'] == 6000
        assert stats['actual_usage']['completion_tokens'] == 1500
        assert stats['context_window'] == 10000
        assert stats['prediction_threshold'] == 0.90
        assert stats['summarization_threshold'] == 8000

    @pytest.mark.asyncio
    async def test_dual_trigger_integration(self, context_manager):
        """Test integration of both trigger mechanisms."""
        # First test prediction trigger
        large_messages = [{"role": "user", "content": "x" * 9500}]
        with patch.object(context_manager, '_estimate_tokens', return_value=9500):
            prediction_triggered = context_manager.should_manage_context_prediction(large_messages)

        # Then test actual usage trigger
        usage_data = {'total_tokens': 8500, 'prompt_tokens': 7000, 'completion_tokens': 1500}
        context_manager.update_token_usage(usage_data)
        actual_triggered = context_manager.should_manage_context_actual()

        assert prediction_triggered is True
        assert actual_triggered is True
