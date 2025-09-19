from typing import Dict, Any, List
import logging

class LLMClient:
    def __init__(self, config: Dict[str, Any], context_manager=None):
        self.config = config
        self.context_manager = context_manager
        self.logger = logging.getLogger(__name__)

    async def chat(self, messages: List[Dict], **kwargs) -> Dict:
        """Enhanced chat method with dual-trigger context management."""
        # Check prediction-based trigger before call
        if self.context_manager and self.context_manager.should_manage_context_prediction(messages):
            self.logger.info("Triggering context management based on token prediction")
            messages = await self.context_manager.manage_context(messages)

        response = await self._make_chat_request(messages, **kwargs)

        # Update actual token usage and check actual usage trigger
        if response.get('usage'):
            if self.context_manager:
                self.context_manager.update_token_usage(response['usage'])

                if self.context_manager.should_manage_context_actual():
                    self.logger.info("Scheduling context management based on actual token usage")
                    # Note: Context management will be applied on next call to avoid
                    # modifying the current conversation state mid-response

        return response

    async def _make_chat_request(self, messages: List[Dict], **kwargs) -> Dict:
        # ...existing code...
        response = {}  # This should be the actual response from the chat request
        result: Dict[str, Any] = {}
        # Ensure usage information is captured from response
        if isinstance(response, dict) and response.get('usage'):
            # response already provides usage as a dict
            result['usage'] = response['usage']
        elif hasattr(response, 'usage'):
            # response is an object with usage attributes
            result['usage'] = {
                'total_tokens': getattr(response.usage, 'total_tokens', None),
                'prompt_tokens': getattr(response.usage, 'prompt_tokens', None),
                'completion_tokens': getattr(response.usage, 'completion_tokens', None)
            }

        # ...existing code...
        # Return result if populated, otherwise return the raw response
        return result or response