"""Test script to verify token optimizer status messages are visible."""
import asyncio
from agent_system.context.optimizer import TokenOptimizer
from agent_system.llm.models import ChatMessage

async def main():
    optimizer = TokenOptimizer()
    
    # Create test messages with enough content to optimize
    messages = [
        ChatMessage(role="user", content="Hello, can you help me with something?"),
        ChatMessage(role="assistant", content="Of course! I'd be happy to help you with anything you need. Just let me know what you're looking for and I'll do my best to assist you with your request. Whether it's information, advice, or just a conversation, I'm here for you."),
        ChatMessage(role="user", content="Tell me about the weather"),
        ChatMessage(role="assistant", content="I'd love to tell you about the weather, but I don't have access to current weather data. However, I can explain that weather is influenced by many factors including temperature, humidity, atmospheric pressure, wind speed and direction, and precipitation. If you want specific weather information for your location, I'd recommend checking a weather service or website."),
    ]
    
    print("🧪 Testing Token Optimizer Status Messages")
    print("=" * 60)
    print(f"📝 Input: {len(messages)} messages")
    print()
    
    # Run optimizer with request_id
    optimized = await optimizer.optimize_messages(messages, request_id="test-123")
    
    print()
    print(f"✅ Output: {len(optimized)} messages")
    print(f"📊 Stats: {optimizer.compression_stats}")

if __name__ == "__main__":
    asyncio.run(main())
