"""
Example: How to use the improved status system

This shows how to replace the old asyncio.sleep(0) pattern with the new
guaranteed delivery system.
"""

from agent_system.mcp.improved_status import (
    status_scope, 
    improved_status_bus,
    publish_status_improved,
    StatusPhase,
    SSEStatusHandler,
    LogStatusHandler
)


# OLD WAY (error-prone):
async def old_agent_run(request_id: str):
    await publish_status("agent_coordinator", "started", request_id, PHASE_START)
    await asyncio.sleep(0)  # ❌ Race condition prone!
    
    await publish_status("agent_worker", "started", request_id, PHASE_START) 
    await asyncio.sleep(0)  # ❌ Easy to forget!
    
    # ... work ...
    
    await publish_status("agent_worker", "completed", request_id, PHASE_END)
    await asyncio.sleep(0)  # ❌ Timing dependent!
    
    await publish_status("agent_coordinator", "completed (3 steps)", request_id, PHASE_END)
    await asyncio.sleep(0)  # ❌ Anti-pattern!


# NEW WAY (guaranteed safe):
async def new_agent_run(request_id: str):
    # Setup handlers once during initialization
    improved_status_bus.add_handler(SSEStatusHandler(sse_queue))
    improved_status_bus.add_handler(LogStatusHandler())
    
    # Use context manager for automatic START/END pairing
    async with status_scope(
        improved_status_bus,
        coordinator_name="agent_coordinator", 
        worker_name="agent_worker",
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
    await publish_status_improved(
        "my_server", 
        "operation complete", 
        request_id, 
        StatusPhase.END
    )
    # ✅ Message is guaranteed delivered to all handlers before continuing


# For web_research_agent fix:
class WebResearchAgentImproved:
    async def _run_with_progress(self, task_prompt: str, request_id: str = None):
        async with status_scope(
            improved_status_bus,
            coordinator_name="web_research_agent_coordinator",
            worker_name="web_research_agent_worker", 
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