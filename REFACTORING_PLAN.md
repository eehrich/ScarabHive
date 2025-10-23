# Agent._run_events() Refactoring Plan

## Current State
- **Method**: `_run_events()` in `src/agent_system/servers/agent/server.py`
- **Lines**: 720-1317 (598 LOC)
- **Complexity**: Very High - handles entire agent execution loop

## Problem
Single method handles too many responsibilities:
1. Request initialization and tracking
2. Session management and history
3. Conversation building (system prompts, tool schemas)
4. LLM interaction loop (max_steps iterations)
5. Tool execution coordination
6. Response handling and validation
7. Emergency loop prevention
8. Cancellation checking
9. Status event forwarding
10. Cleanup and persistence

## Extraction Strategy

### Phase 1: Setup/Initialization (lines 720-875)
**Extract to**: `_initialize_request_and_conversation()`
- Cancellation token creation
- Request registration (`_active_requests`)
- Context var setup
- Session initialization
- Status forwarding start
- LLM availability check
- Tool discovery (`list_usable_tools()`)
- Prompt rendering (`_render_prompts()`)
- Message history loading
- Hook execution (session_start)
- Tool schema building

**Returns**: `ConversationContext` dataclass with:
- messages: List[ChatMessage]
- available_tools: List[str]
- tools_schema: List[Dict]
- tool_name_mapping: Dict[str, str]
- max_steps: int
- main_token: CancellationToken

### Phase 2: Main Loop (lines 876-1212)
**Extract to**: `_execute_llm_loop()` 
- Takes `ConversationContext` as input
- Runs max_steps iterations
- Calls helper methods for each step
- Yields events to caller
- Returns final results dict

**Helper methods**:
- `_execute_single_step()` - one LLM iteration
- `_check_cancellation_and_emit()` - check cancel + yield event
- `_handle_llm_response()` - process assistant message
- `_check_loop_guards()` - empty response / no-tool-call counters
- `_execute_tool_calls_streaming()` - already mostly delegated to ToolExecutionManager

### Phase 3: Finalization (lines 1213-1315)
**Extract to**: `_finalize_request()`
- Session persistence
- Request cleanup
- Status event finalization
- MCP integration shutdown
- Context var reset

**Returns**: None (cleanup only)

## Target Structure

```python
async def _run_events(self, task, request_id, session_id, status_coordinator, status_worker, ...):
    """Orchestrate agent execution - delegates to focused helper methods."""
    try:
        # Phase 1: Setup (< 50 LOC)
        context = await self._initialize_request_and_conversation(
            task, request_id, session_id, status_coordinator, status_worker, ...
        )
        yield {"type": "start", ...}
        
        # Phase 2: Main loop (< 80 LOC)
        async for event in self._execute_llm_loop(context, request_id, session_id, status_coordinator, status_worker):
            yield event
        
    except Exception as e:
        logger.exception("Agent execution failed")
        yield {"type": "error", "message": str(e)}
    finally:
        # Phase 3: Cleanup (< 30 LOC)
        await self._finalize_request(request_id, session_id, context if 'context' in locals() else None)
        
    yield {"type": "end"}
```

## Benefits
1. Each method < 100 LOC (target < 80)
2. Clear separation of concerns
3. Easier to test individual phases
4. Reduced cognitive load
5. Better error handling boundaries

## Implementation Order
1. Create `ConversationContext` dataclass
2. Extract `_initialize_request_and_conversation()`
3. Extract `_finalize_request()`
4. Extract `_execute_llm_loop()` with sub-helpers
5. Update main `_run_events()` to orchestrate
6. Run full test suite (must pass all 1766 tests)
7. Commit with clear message

## Testing Strategy
- No new tests needed - existing 1766 tests verify behavior
- All existing tests must pass after refactoring
- Focus on integration tests for _run_events flow
