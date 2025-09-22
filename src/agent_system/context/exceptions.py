"""Context management exceptions."""

from typing import Optional


class ContextLengthExceededError(Exception):
    """
    Raised when the context length exceeds the model's limit.
    
    This is a recoverable error that should trigger context management
    to reduce the message history size.
    """
    
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        super().__init__(message)
        self.original_exception = original_exception
        
    def __str__(self) -> str:
        base_msg = super().__str__()
        if self.original_exception:
            return f"{base_msg} (caused by: {self.original_exception})"
        return base_msg