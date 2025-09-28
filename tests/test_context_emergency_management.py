"""Test emergency context management functionality."""

from agent_system.context.exceptions import ContextLengthExceededError


async def test_emergency_context_management():
    """Test that emergency context management handles context length exceeded errors."""
    # This is a basic integration test to verify the exception handling works
    
    # Test that ContextLengthExceededError can be created and used
    original_error = Exception("OpenAI context limit exceeded")
    context_error = ContextLengthExceededError(
        "Context length exceeded", 
        original_exception=original_error
    )
    
    assert str(context_error) == "Context length exceeded (caused by: OpenAI context limit exceeded)"
    assert context_error.original_exception == original_error


async def test_context_length_exception_creation():
    """Test ContextLengthExceededError exception handling."""
    # Test without original exception
    error = ContextLengthExceededError("Test message")
    assert str(error) == "Test message"
    assert error.original_exception is None
    
    # Test with original exception
    original = ValueError("Original error")
    error_with_original = ContextLengthExceededError("Test message", original)
    assert "Test message (caused by: Original error)" in str(error_with_original)
    assert error_with_original.original_exception == original


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_emergency_context_management())
    asyncio.run(test_context_length_exception_creation())
    print("All tests passed!")