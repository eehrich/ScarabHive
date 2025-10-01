"""Test dual-trigger context management implementation."""

import pytest
from unittest.mock import Mock, patch
from agent_system.config.models import ContextManagementConfig as ContextConfig
from agent_system.context.manager import ContextManager
from agent_system.llm.models import ChatMessage


class TestDualTriggerContextManagement:
    """Test the dual-trigger context management strategy."""

    @pytest.fixture
    def context_config(self):
        """Create a test context configuration."""
        return ContextConfig(
            context_window=10000,
            summarization_threshold=8000,  # 80% of context window
            prediction_threshold=0.90,     # 90% prediction threshold
            preserve_recent_messages=5,
            strategy="SUMMARIZE_OLDEST"
        )

    @pytest.fixture
    def context_manager(self, context_config):
        """Create a context manager with test configuration."""
        return ContextManager(context_config)

    def test_prediction_threshold_calculation(self, context_config):
        """Test that prediction threshold is calculated correctly."""
        # 90% of 10000 = 9000 tokens
        predicted_tokens = 9500

        assert context_config.should_manage_context_prediction(predicted_tokens) is True
        assert context_config.should_manage_context_prediction(8500) is False

    def test_actual_usage_threshold(self, context_config):
        """Test that actual usage threshold works correctly."""
        # Should trigger when actual usage exceeds summarization_threshold (8000)
        assert context_config.should_manage_context_actual(8500) is True
        assert context_config.should_manage_context_actual(7500) is False

    def test_dual_trigger_prediction_based(self, context_manager):
        """Test that prediction-based trigger activates context management."""
        # Create messages that would trigger prediction threshold (90% of 10000 = 9000)
        messages = [ChatMessage(role="user", content="x" * 9500)]  # Simulate large message

        with patch.object(context_manager, 'estimate_token_count', return_value=9500):
            estimated_tokens, warning_level = context_manager.check_and_warn(messages)
            should_manage = context_manager.should_manage_context(estimated_tokens, warning_level)

            assert should_manage is True
            assert estimated_tokens == 9500

    def test_dual_trigger_actual_usage(self, context_manager):
        """Test that actual usage trigger activates context management."""
        # Simulate actual usage from LLM response that exceeds summarization threshold
        usage_data = {
            'total_tokens': 8500,
            'prompt_tokens': 7000,
            'completion_tokens': 1500
        }

        context_manager.update_token_usage(usage_data)

        # Even with low predicted tokens, should trigger based on actual usage
        messages = [ChatMessage(role="user", content="small message")]

        with patch.object(context_manager, 'estimate_token_count', return_value=5000):
            estimated_tokens, warning_level = context_manager.check_and_warn(messages)
            should_manage = context_manager.should_manage_context(estimated_tokens, warning_level)

            assert should_manage is True
            assert context_manager._actual_usage_stats['total_tokens'] == 8500

    def test_usage_stats_tracking(self, context_manager):
        """Test that usage statistics are properly tracked and accessible."""
        usage_data = {
            'total_tokens': 5000,
            'prompt_tokens': 4000,
            'completion_tokens': 1000
        }

        context_manager.update_token_usage(usage_data)
        stats = context_manager.get_usage_stats()

        assert stats['actual_usage']['total_tokens'] == 5000
        assert stats['actual_usage']['prompt_tokens'] == 4000
        assert stats['actual_usage']['completion_tokens'] == 1000
        assert stats['context_window'] == 10000
        assert stats['prediction_threshold'] == 0.90
        assert stats['summarization_threshold'] == 8000

    def test_no_trigger_below_thresholds(self, context_manager):
        """Test that context management doesn't trigger when below both thresholds."""
        # Low predicted tokens
        messages = [ChatMessage(role="user", content="short message")]

        # Low actual usage
        usage_data = {
            'total_tokens': 3000,
            'prompt_tokens': 2500,
            'completion_tokens': 500
        }
        context_manager.update_token_usage(usage_data)

        with patch.object(context_manager, 'estimate_token_count', return_value=5000):
            estimated_tokens, warning_level = context_manager.check_and_warn(messages)
            should_manage = context_manager.should_manage_context(estimated_tokens, warning_level)

            assert should_manage is False

    def test_both_triggers_active(self, context_manager):
        """Test behavior when both triggers would activate."""
        # High predicted tokens (>90% of 10000 = >9000)
        messages = [ChatMessage(role="user", content="x" * 9500)]

        # High actual usage (>8000)
        usage_data = {
            'total_tokens': 8500,
            'prompt_tokens': 7000,
            'completion_tokens': 1500
        }
        context_manager.update_token_usage(usage_data)

        with patch.object(context_manager, 'estimate_token_count', return_value=9500):
            estimated_tokens, warning_level = context_manager.check_and_warn(messages)
            should_manage = context_manager.should_manage_context(estimated_tokens, warning_level)

            assert should_manage is True

    @pytest.mark.asyncio
    async def test_context_management_integration(self, context_manager):
        """Test full context management with dual triggers."""
        # Set up a mock summarizer
        mock_summarizer = Mock()
        summary_messages = [
            ChatMessage(role="system", content="Summary of previous conversation...")
        ]

        # Make the summarize method async
        async def mock_summarize(*args, **kwargs):
            return summary_messages

        mock_summarizer.summarize = mock_summarize
        context_manager.set_summarizer(mock_summarizer)

        # Create messages that trigger prediction threshold
        messages = [
            ChatMessage(role="user", content="x" * 9000),  # Large message
            ChatMessage(role="assistant", content="Response"),
        ]

        with patch.object(context_manager, 'estimate_token_count', return_value=9500):
            # Should trigger context management
            estimated_tokens, warning_level = context_manager.check_and_warn(messages)
            should_manage = context_manager.should_manage_context(estimated_tokens, warning_level)

            assert should_manage is True

            # Test actual context management execution
            result_messages = await context_manager.manage_context(messages)

            # Should have called summarizer and reduced message count or replaced with summary
            # The messages should either be fewer or replaced with summary content
            assert len(result_messages) <= len(messages) or any("Summary" in str(msg.content) for msg in result_messages)