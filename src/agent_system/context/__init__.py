"""Context window management package for agent system.

ContextManager and ConversationSummarizer have been moved to hook plugins:
- context_optimizer: Basic context optimization (truncation, deduplication)
- context_summarizer: Intelligent LLM-based summarization

This package now only contains:
- TokenOptimizer: Token-level compression (still used)
- Tracking utilities: agent_tracker, tracker, accumulator
- Exceptions: ContextLengthExceededError
"""

from .optimizer import TokenOptimizer
from .exceptions import ContextLengthExceededError

__all__ = [
    "TokenOptimizer",
    "ContextLengthExceededError",
]
