# Agent Server Refactoring Plan

## Status Update (Oct 24, 2025)
✅ **Phase 1-3 COMPLETED**: _run_events() extraction done (commit: 8cbaa11)
- Extracted _initialize_request_and_conversation() (165 LOC)
- Extracted _execute_llm_loop() (296 LOC)  
- Extracted _finalize_request() (96 LOC)
- New _run_events() is now clean orchestrator (117 LOC)
- **Result**: 606 → 117 LOC (81% reduction), file: 2,051 → 1,554 LOC (23% reduction)

## Current State (Post-Phase 3)
- **File**: `src/agent_system/servers/agent/server.py`
- **Size**: 1,554 lines
- **Methods**: 28
- **Remaining Issues**: Session tracking, request management, utility methods still embedded in main class

## Remaining Refactoring Opportunities
## Remaining Refactoring Opportunities

### Phase 4: Session & Message Tracking (HIGH PRIORITY)
**Target file**: `src/agent_system/servers/agent/components/session_tracking.py`
**Effort**: 2-3 hours

**Extract these methods from server.py:**
1. `append_user_message()` (lines 463-487, ~27 LOC)
   - Appends message to active request
   - Thread-safe with asyncio.Lock
   
2. `append_to_session()` (lines 489-506, ~19 LOC)  
   - Appends message to session history
   - Handles session-to-request mapping
   
3. `_drain_appended_messages()` (lines 508-529, ~23 LOC)
   - Consumes pending appended messages
   - Returns updated message list

**Data structures to move:**
- `_appended_messages: Dict[str, List[str]]` (request_id → messages)
- `_request_to_session: Dict[str, str]` (request_id → session_id)
- `_lock: asyncio.Lock` (for thread safety)

**New component interface:**
```python
class SessionTracker:
    """Manages request/session lifecycle and message appending."""
    
    def __init__(self):
        self._appended_messages: Dict[str, List[str]] = {}
        self._request_to_session: Dict[str, str] = {}
        self._lock = asyncio.Lock()
    
    async def append_user_message(self, request_id: str, content: str) -> bool
    async def append_to_session(self, session_id: str, content: str) -> bool  
    async def drain_appended_messages(self, request_id: str, messages: List[ChatMessage]) -> List[ChatMessage]
    def register_request(self, request_id: str, session_id: str) -> None
    def unregister_request(self, request_id: str) -> None
    def get_session_for_request(self, request_id: str) -> Optional[str]
```

**Impact:**
- ~100 LOC removed from server.py
- Better testability (isolated unit tests)
- Clear single responsibility

---

### Phase 5: Request Cancellation Management (MEDIUM PRIORITY)  
**Target file**: `src/agent_system/servers/agent/components/request_manager.py`
**Effort**: 1-2 hours

**Extract these methods from server.py:**
1. `cancel_request()` (lines 385-430, ~47 LOC)
   - Cancels active request via CancellationManager
   - Publishes cancellation status events
   - Returns success/failure
   
2. `_is_cancelled()` (lines 432-461, ~15 LOC)
   - Checks if request is cancelled
   - Handles both string and None request_id

**Data structures to move:**
- `_active_requests: Dict[str, str]` (request_id → session_id)

**New component interface:**
```python
class AgentRequestManager:
    """Manages active agent requests and cancellation."""
    
    def __init__(self, agent_name: str):
        self._agent_name = agent_name
        self._active_requests: Dict[str, str] = {}  
    
    async def cancel_request(self, request_id: str, status_bus: Any) -> bool
    def is_cancelled(self, request_id: Optional[str]) -> bool
    def register_active_request(self, request_id: str, session_id: str) -> None
    def unregister_active_request(self, request_id: str) -> None
    def get_active_requests(self) -> List[str]
```

**Impact:**
- ~70 LOC removed from server.py
- Centralized request lifecycle management
- Easier to extend with request metrics/monitoring

---

### Phase 6: Utility Methods Cleanup (LOW PRIORITY)
**Effort**: 30 minutes

**Move to appropriate locations:**

1. **`_extract_summary()` → `result_utils.py`** (lines 1525-1555, ~30 LOC)
   - Already has `collect_final_result()` in that file
   - Logical grouping of result processing utilities
   - Make it a module-level function (doesn't need instance state)
   
2. **`_extract_profile_info()` → Keep but make static** (lines 228-254, ~27 LOC)  
   - Doesn't use instance variables
   - Can be `@staticmethod` or module function
   - Or move to `utils/llm_utils.py`

**Impact:**
- ~30-60 LOC removed from server.py
- Better code organization
- Reduced coupling

---

## Target End State

**File size projection:**
- Current: 1,554 LOC
- After Phase 4: ~1,450 LOC (-100)
- After Phase 5: ~1,380 LOC (-70)  
- After Phase 6: ~1,320-1,350 LOC (-30 to -60)
- **Total reduction: ~200-230 LOC (13-15% smaller)**

**Method count:**
- Current: 28 methods
- After all phases: ~20-22 methods (6-8 methods extracted)

**Component structure:**
```
src/agent_system/servers/agent/
├── server.py (~1,350 LOC - core agent logic)
├── components/
│   ├── mcp_integration.py (existing)
│   ├── tool_execution.py (existing)
│   ├── status_forwarding.py (existing)
│   ├── session_tracking.py (NEW - Phase 4)
│   └── request_manager.py (NEW - Phase 5)
├── prompt_strategies.py (existing)
├── tool_discovery.py (existing)
├── tool_schema_builder.py (existing)
└── result_utils.py (enhanced - Phase 6)
```

---

## Original Phase 1-3 Documentation (COMPLETED)

---

## Original Phase 1-3 Documentation (COMPLETED)

### Phase 1: Setup/Initialization ✅ DONE
**Extracted to**: `_initialize_request_and_conversation()` (165 LOC)
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

**Returns**: `ConversationContext` dataclass

### Phase 2: Main Loop ✅ DONE
**Extracted to**: `_execute_llm_loop()` (296 LOC)
- Takes `ConversationContext` as input
- Runs max_steps iterations
- LLM calls with tool execution
- Loop guards and cancellation checks
- Yields events to caller

### Phase 3: Finalization ✅ DONE
**Extracted to**: `_finalize_request()` (96 LOC)
- Session persistence
- Request cleanup
- Status event finalization
- MCP integration shutdown
- Context var reset

### New _run_events() Structure ✅ DONE (117 LOC)
Clean orchestrator that delegates to focused helper methods.

---

## Implementation Order (Phases 4-6)

1. ✅ Update REFACTORING_PLAN.md with new phases
2. ⏳ Create `SessionTracker` component (Phase 4)
3. ⏳ Create `AgentRequestManager` component (Phase 5)
4. ⏳ Move `_extract_summary()` to `result_utils.py` (Phase 6)
5. ⏳ Refactor `server.py` to use new components
6. ⏳ Update imports and type hints
7. ⏳ Run full test suite (must pass all tests)
8. ⏳ Update documentation
9. ⏳ Commit with clear message

## Testing Strategy
- No new tests needed - existing tests verify behavior
- All existing tests must pass after refactoring (target: 1,780+/1,802)
- Focus on integration tests for agent request lifecycle

## Benefits Summary
✅ **Already achieved (Phases 1-3)**:
- _run_events() reduced from 606 → 117 LOC (81%)
- File size reduced from 2,051 → 1,554 LOC (23%)
- Clear separation of initialization/execution/cleanup

🎯 **Additional benefits (Phases 4-6)**:
- Further 13-15% file size reduction
- 6-8 fewer methods in main Agent class
- Better component isolation and testability
- Clearer single responsibility per component
- Easier to maintain and extend
