"""
Example: How to use the StatusScope system

This shows how to use the modern StatusScope API with guaranteed delivery
and clean async patterns. The StatusScope system replaces the old 
publish_status approach with a more robust design.
"""

from agent_system.mcp.status import (
    status_scope, 
    status_bus,
    StatusEvent,
    StatusPhase
)
import asyncio


# MODERN WAY (recommended):
async def modern_agent_run(request_id: str):
    """Example of using StatusScope context manager for automatic START/END handling."""
    async with status_scope(
        status_bus,
        name="agent", 
        request_id=request_id
    ) as status:
        
        # All status updates are guaranteed to be delivered
        await status.progress("Analyzing task")
        await status.progress("Processing data")
        await status.progress("Calling tools")
        await status.progress("Synthesizing results")
        
        # START/END automatically handled - no more forgotten status messages!
        # No more asyncio.sleep(0) needed anywhere!


# Manual status updates (for individual messages):
async def manual_status_example(request_id: str):
    """Example of creating a StatusScope manually for custom control."""
    from agent_system.mcp.status import StatusScope, get_status_bus
    
    status_bus_instance = get_status_bus()
    status = StatusScope(status_bus_instance, "my_server", request_id)
    
    async with status:
        await status.progress("Processing step 1")
        await status.progress("Processing step 2") 
        await status.end("Operation complete")


# For subscription and filtering:
async def subscribe_example():
    """Example of subscribing to status events."""
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


# Plugin example - how plugins use StatusScope:
class ExamplePlugin:
    """Example of how a plugin uses the StatusScope system."""
    
    async def call(self, action: str, params: dict):
        """Plugin call method that uses injected status object."""
        # Get the status object injected by the base class
        status = params.get("_status")
        
        if action == "process_data":
            if status:
                await status.progress("Starting data processing")
                await status.progress("Validating input")
                await status.progress("Processing...")
                await status.progress("Generating results")
                # status.end() called automatically by context manager
                
            # Do actual work here
            result = {"processed": "data"}
            return result


# Direct StatusEvent usage for advanced scenarios:
async def advanced_status_example():
    """Example of direct StatusEvent usage."""
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


# Error handling example:
async def error_handling_example(request_id: str):
    """Example of proper error handling with StatusScope."""
    async with status_scope(
        status_bus,
        name="error_example", 
        request_id=request_id
    ) as status:
        
        try:
            await status.progress("Starting risky operation")
            # Simulate some work that might fail
            raise ValueError("Something went wrong")
            
        except ValueError as e:
            await status.error(f"Operation failed: {e}")
            raise  # Re-raise the exception
        
        # If we reach here, the operation succeeded
        await status.progress("Operation completed successfully")


if __name__ == "__main__":
    asyncio.run(modern_agent_run("example_request_123"))