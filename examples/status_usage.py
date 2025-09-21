"""
Example: How to use the status system

This shows how to use the clean status API with guaranteed delivery
and modern async patterns.
"""

from agent_system.mcp.status import (
    status_scope, 
    status_bus,
    publish_status,
    StatusPhase,
    StatusEvent
)
import asyncio


# OLD WAY (error-prone):
async def old_agent_run(request_id: str):
    await publish_status("agent_coordinator", "started", request_id, StatusPhase.START)
    await asyncio.sleep(0)  # ❌ Race condition prone!
    
    await publish_status("agent_worker", "started", request_id, StatusPhase.START) 
    await asyncio.sleep(0)  # ❌ Easy to forget!
    
    # ... work ...
    
    await publish_status("agent_worker", "completed", request_id, StatusPhase.END)
    await asyncio.sleep(0)  # ❌ Timing dependent!
    
    await publish_status("agent_coordinator", "completed (3 steps)", request_id, StatusPhase.END)
    await asyncio.sleep(0)  # ❌ Anti-pattern!


# NEW WAY (guaranteed safe):
async def new_agent_run(request_id: str):
    # Use context manager for automatic START/END pairing
    async with status_scope(
        status_bus,
        name="agent", 
        request_id=request_id
    ) as status:
        
        # All status updates are guaranteed to be delivered
        await status.step("Analyzing task")
        await status.progress("Processing data", is_coordinator=False)
        await status.step("Calling tools")
        await status.progress("Synthesizing results", is_coordinator=False)
        
        # START/END automatically handled - no more forgotten status messages!
        # No more asyncio.sleep(0) needed anywhere!


# For individual status messages (drop-in replacement):
async def manual_status_example(request_id: str):
    # This guarantees delivery without asyncio.sleep(0)
    await publish_status(
        "my_server", 
        "operation complete", 
        request_id, 
        StatusPhase.END
    )
    # ✅ Message is guaranteed delivered to all handlers before continuing


# For subscription and filtering:
async def subscribe_example():
    # Subscribe to all events
    queue = await status_bus.subscribe()
    
    # Or subscribe with filters
    server_queue = await status_bus.subscribe(server="my_server")
    request_queue = await status_bus.subscribe(request_id="req_123")
    both_queue = await status_bus.subscribe(server="my_server", request_id="req_123")
    
    # Read events
    event = await queue.get()
    print(f"Received: {event.server} - {event.message}")
    
    # Clean up
    status_bus.unsubscribe(queue)
    status_bus.unsubscribe(server_queue)
    status_bus.unsubscribe(request_queue)
    status_bus.unsubscribe(both_queue)


# For web_research_agent fix:
class WebResearchAgentImproved:
    async def _run_with_progress(self, task_prompt: str, request_id: str = None):
        async with status_scope(
            status_bus,
            name="web_research_agent",
            request_id=request_id
        ) as status:
            
            # Process events from base agent
            async for event in self.run_events(task_prompt, request_id=request_id):
                if event.get("type") == "mcp_call":
                    await status.step(f"Using {event.get('server', 'tool')}")
                elif event.get("type") == "thinking":
                    await status.progress("Processing...", is_coordinator=False)
                # No need to break on "final" - context manager handles completion!
            
            # ✅ Coordinator/Worker completion guaranteed by context manager
            return {"status": "completed"}


# Direct StatusEvent usage for advanced scenarios:
async def advanced_status_example():
    # Create events directly
    event = StatusEvent(
        server="advanced_server",
        request_id="req_456",
        message="Custom status message",
        phase=StatusPhase.PROGRESS,
        level="info",
        meta={"custom": "data"}
    )
    
    # Publish directly to bus
    await status_bus.publish(event)
    
    # Check metrics
    metrics = status_bus.get_status_metrics()
    print(f"Handlers: {metrics['handlers_count']}, Sequence: {metrics['sequence_counter']}")


if __name__ == "__main__":
    asyncio.run(new_agent_run("example_request_123"))