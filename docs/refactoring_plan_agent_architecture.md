# Agent Architecture Refactoring Plan

**Date**: 2025-10-15  
**Status**: PROPOSAL  
**Author**: AI Architect

## Current Problems

### 1. Wrong Package Structure
- `agent_system/agent/interface_api.py` - **API doesn't belong in agent folder!**
  - Should be: `agent_system/api/interface_api.py` or just `api/app.py`
  - Current location violates separation of concerns
  - Agent is a server implementation, API is a web framework layer

### 2. God Object: Agent Class (1699 lines, 30+ methods)
The Agent class has too many responsibilities:

**Current Responsibilities:**
1. LLM interaction & message management
2. Tool discovery & filtering
3. Tool execution & delegation
4. Status event publishing
5. Session management
6. Cancellation handling
7. Message appending/draining
8. System prompt rendering
9. Request ID generation
10. MCP integration management
11. Hook system integration
12. Tool schema customization
13. Registry access
14. Event streaming
15. Result extraction

**Symptoms of Over-Complexity:**
- 1699 lines in single file
- 30+ public/private methods
- Deep nesting in `_run_events()` (core loop is 600+ lines!)
- Hard to test individual components
- Hard to understand control flow
- Mixing concerns: orchestration + execution + state management

### 3. Component Folder Exists But Underutilized
- `components/` has 4 modules, but Agent class still has most logic
- Components are called FROM Agent, not composing Agent
- Components are helper modules, not proper domain objects

## Proposed Refactoring

### Phase 1: Move API to Correct Location ✅ (Quick Win)

**From:**
```
agent_system/
  agent/
    interface_api.py  ❌ WRONG LOCATION
    __init__.py
```

**To:**
```
agent_system/
  api/
    app.py  ✅ (renamed from interface_api.py)
    endpoints/
      ... (existing)
```

**Changes:**
- Move `agent_system/agent/interface_api.py` → `agent_system/api/app.py`
- Update all imports
- Delete empty `agent_system/agent/` folder
- Update `pyproject.toml` if needed

---

### Phase 2: Split Agent into Focused Components 🎯 (Main Refactoring)

**New Architecture: Composition over Inheritance**

```
agent_system/
  servers/
    agent/
      server.py           # Agent (orchestrator only, 200-300 lines)
      components/
        llm_manager.py         # LLM interaction & message history
        tool_manager.py        # Tool discovery, filtering, schemas
        execution_engine.py    # Tool execution loop & state machine
        prompt_renderer.py     # System prompt + template rendering
        session_handler.py     # Session & message appending
        cancellation_manager.py # Request cancellation
        hook_integration.py    # ✅ Already exists
        mcp_integration.py     # ✅ Already exists
        status_forwarding.py   # ✅ Already exists
        tool_execution.py      # ✅ Already exists (but needs refactor)
```

#### Component Breakdown

##### 1. **LLMManager** (NEW)
**Responsibility**: LLM communication & message history
```python
class LLMManager:
    """Manages LLM interactions and conversation history."""
    
    def __init__(self, llm_client, llm_profile: str):
        self.llm = llm_client
        self.profile = llm_profile
    
    async def chat(self, messages: List[ChatMessage], tools: List[Dict]) -> Dict:
        """Send messages to LLM with tool schemas."""
        
    async def chat_no_tools(self, messages: List[ChatMessage]) -> str:
        """Send messages without tools (simple text response)."""
    
    def build_message_history(self, task: str, results: List) -> List[ChatMessage]:
        """Build conversation history from task + previous results."""
```

**Benefits:**
- Isolates LLM API details
- Easy to mock for testing
- Clear interface for conversation management

---

##### 2. **ToolManager** (NEW)
**Responsibility**: Tool discovery, filtering, schema management
```python
class ToolManager:
    """Discovers and manages available tools for agent."""
    
    def __init__(self, registry: MCPRegistry, mcp_integration, agent_config):
        self.registry = registry
        self.mcp_integration = mcp_integration
        self.config = agent_config
    
    async def discover_tools(self) -> List[str]:
        """Discover all available tool server names."""
        
    def filter_tools(self, tools: List[str]) -> List[str]:
        """Apply agent's allow/deny filters."""
    
    async def build_tool_schemas(self, tool_names: List[str]) -> List[Dict]:
        """Build OpenAI function schemas from tool names."""
    
    def apply_custom_descriptions(self, schemas: List[Dict]) -> None:
        """Apply agent's custom tool descriptions."""
```

**Benefits:**
- Single responsibility for tool management
- Easier to test filtering logic
- Clear separation from execution

---

##### 3. **ExecutionEngine** (REFACTOR from _run_events)
**Responsibility**: Core agent execution loop
```python
class ExecutionEngine:
    """Executes agent tasks using LLM + tools."""
    
    def __init__(
        self,
        llm_manager: LLMManager,
        tool_manager: ToolManager,
        tool_executor: ToolExecutor,  # existing component
        status_forwarder: StatusForwarder,  # existing component
    ):
        self.llm = llm_manager
        self.tools = tool_manager
        self.executor = tool_executor
        self.status = status_forwarder
    
    async def execute(
        self,
        task: str,
        request_id: str,
        max_steps: int,
        system_prompt: str,
    ) -> AsyncIterator[Dict]:
        """Main execution loop: LLM → Tools → LLM → ... → Result."""
        
        # Simplified state machine:
        # 1. Build tool schemas
        # 2. LLM call with tools
        # 3. Process tool calls
        # 4. Execute tools
        # 5. Append results to messages
        # 6. Loop until done or max_steps
```

**Benefits:**
- Core logic isolated from Agent class
- Easier to test execution flow
- Can be reused by different agent types
- Clear state machine vs spaghetti code

---

##### 4. **PromptRenderer** (NEW)
**Responsibility**: System prompt template rendering
```python
class PromptRenderer:
    """Renders system prompts from templates."""
    
    def __init__(self, agent_config, system_config):
        self.agent_config = agent_config
        self.system_config = system_config
    
    def render_system_prompt(
        self,
        available_tools: List[str],
        max_steps: int,
        context: Dict[str, Any]
    ) -> str:
        """Render system prompt from template with variables."""
    
    def render_user_prompt(self, context: Dict[str, Any]) -> Optional[str]:
        """Render user-facing prompt if configured."""
```

**Benefits:**
- Template logic isolated
- Easy to test different template scenarios
- No mixing with execution logic

---

##### 5. **SessionHandler** (NEW)
**Responsibility**: Session-based message appending
```python
class SessionHandler:
    """Handles session-based message persistence and appending."""
    
    def __init__(self, session_manager):
        self.session_manager = session_manager
        self._pending_messages: Dict[str, List[str]] = {}
    
    async def append_message(self, request_id: str, content: str) -> bool:
        """Append message to pending queue."""
    
    async def drain_messages(self, request_id: str) -> List[ChatMessage]:
        """Get and clear pending messages."""
    
    async def save_to_session(self, session_id: str, content: str) -> bool:
        """Persist message to session storage."""
```

**Benefits:**
- Session logic separated from agent core
- Clear interface for message appending
- Easier to test persistence

---

##### 6. **CancellationManager** (NEW)
**Responsibility**: Request cancellation tracking
```python
class CancellationManager:
    """Manages request cancellation tokens and state."""
    
    def __init__(self):
        self._cancellation_tokens: Dict[str, CancellationToken] = {}
    
    def create_token(self, request_id: str) -> CancellationToken:
        """Create new cancellation token."""
    
    def cancel(self, request_id: str) -> bool:
        """Cancel a request."""
    
    def is_cancelled(self, request_id: str) -> bool:
        """Check if request is cancelled."""
    
    def cleanup(self, request_id: str) -> None:
        """Remove cancellation token after completion."""
```

**Benefits:**
- Cancellation logic isolated
- Easy to test cancellation scenarios
- Clear lifecycle management

---

##### 7. **Existing Components** (Keep & Refine)
- ✅ `hook_integration.py` - HookIntegration (good as-is)
- ✅ `mcp_integration.py` - MCPIntegrationManager (good as-is)
- ✅ `status_forwarding.py` - StatusForwarder (good as-is)
- ✅ `tool_execution.py` - ToolExecutor (refactor to use ToolManager)

---

#### NEW Agent Class (Orchestrator Only)

```python
class Agent(MCPServer):
    """
    Orchestrates LLM-based task execution with tool access.
    
    This is a THIN ORCHESTRATOR that composes specialized components.
    All heavy lifting is delegated to components.
    """
    
    def __init__(self, name: str, system_config, mcp_config, registry):
        super().__init__(name, system_config, mcp_config)
        
        # Component composition
        self.llm_manager = LLMManager(...)
        self.tool_manager = ToolManager(registry, mcp_integration, mcp_config.agent_config)
        self.prompt_renderer = PromptRenderer(mcp_config.agent_config, system_config)
        self.session_handler = SessionHandler(...)
        self.cancellation_manager = CancellationManager()
        self.execution_engine = ExecutionEngine(
            self.llm_manager,
            self.tool_manager,
            ToolExecutor(...),
            StatusForwarder(...)
        )
    
    # --- Public API (thin wrappers) ---
    
    async def run_events(self, task: str, request_id: str, ...) -> AsyncIterator:
        """Execute task and yield events (delegates to ExecutionEngine)."""
        async for event in self.execution_engine.execute(task, request_id, ...):
            yield event
    
    async def cancel_request(self, request_id: str) -> bool:
        """Cancel request (delegates to CancellationManager)."""
        return self.cancellation_manager.cancel(request_id)
    
    async def list_tools(self) -> List[MCPTool]:
        """List available tools (delegates to ToolManager)."""
        return await self.tool_manager.list_tools()
    
    # ... other public methods are thin wrappers
```

**Result:**
- Agent class: ~200-300 lines (down from 1699!)
- Clear component boundaries
- Easy to test each component independently
- Easy to understand control flow
- Components can be reused across different agent types

---

### Phase 3: Update Tests 🧪

**Test Structure:**
```
tests/
  unit/
    agent/
      components/
        test_llm_manager.py
        test_tool_manager.py
        test_execution_engine.py
        test_prompt_renderer.py
        test_session_handler.py
        test_cancellation_manager.py
  integration/
    test_agent_comprehensive.py  # End-to-end agent tests
```

**Benefits:**
- Faster unit tests (no full agent setup needed)
- Better test isolation
- Easier to identify failing components
- Can mock individual components easily

---

## Migration Strategy

### Step 1: Move API (Low Risk) ✅
1. Move `interface_api.py` → `api/app.py`
2. Update imports
3. Run tests
4. Commit

### Step 2: Extract Components (Iterative)
For each component:
1. Create new component module
2. Move methods from Agent to component
3. Update Agent to use component
4. Write unit tests for component
5. Run integration tests
6. Commit

**Order:**
1. PromptRenderer (simple, no dependencies)
2. SessionHandler (simple, minimal dependencies)
3. CancellationManager (simple, isolated)
4. LLMManager (medium, some dependencies)
5. ToolManager (complex, many dependencies)
6. ExecutionEngine (most complex, depends on all others)

### Step 3: Cleanup & Documentation
1. Remove old commented code
2. Update architecture docs
3. Update plugin authoring guide
4. Final integration test sweep

---

## Benefits Summary

### Maintainability ⭐⭐⭐⭐⭐
- Small, focused classes (150-300 lines each)
- Single Responsibility Principle
- Easy to locate bugs
- Clear component boundaries

### Testability ⭐⭐⭐⭐⭐
- Components can be unit tested independently
- Easy mocking
- Faster test execution
- Better test coverage

### Extensibility ⭐⭐⭐⭐⭐
- Add new tool sources? → Extend ToolManager
- Add new LLM provider? → Swap LLMManager
- Change execution strategy? → Modify ExecutionEngine
- Components can be reused across agent types

### Understanding ⭐⭐⭐⭐⭐
- New developers can understand one component at a time
- Clear control flow
- No "god object" confusion
- Self-documenting architecture

---

## Risk Assessment

### Low Risk ✅
- Moving API file (Phase 1)
- Extracting simple components (PromptRenderer, SessionHandler, CancellationManager)

### Medium Risk ⚠️
- Extracting LLMManager (message history management)
- Extracting ToolManager (tool discovery + filtering)

### High Risk ⚠️⚠️
- Extracting ExecutionEngine (core loop logic)
- Need careful testing of edge cases

**Mitigation:**
- Incremental approach (one component at a time)
- Comprehensive integration tests before/after
- Keep both old and new code during transition
- Can roll back any single component if issues arise

---

## Timeline Estimate

- **Phase 1** (API move): 30 minutes
- **Phase 2** (Component extraction): 4-6 hours (iterative)
  - PromptRenderer: 30 min
  - SessionHandler: 30 min
  - CancellationManager: 30 min
  - LLMManager: 1 hour
  - ToolManager: 1.5 hours
  - ExecutionEngine: 2 hours
- **Phase 3** (Tests + cleanup): 1-2 hours

**Total**: 6-9 hours (can be done incrementally over multiple sessions)

---

## Decision Points

**Questions for Architect:**

1. ✅ **Move API?** YES - clear win, no controversy
2. ✅ **Component-based architecture?** YES - recommended
3. ❓ **All components at once or iterative?** → Iterative safer
4. ❓ **Keep backwards compatibility?** NO - you said "no legacy support"!
5. ❓ **Start with which component?** → PromptRenderer (simplest)

---

## Recommendation: START WITH PHASE 1

**Quick Win Strategy:**
1. Move API file NOW (30 min, low risk, immediate clarity)
2. Review this plan
3. Decide on component extraction order
4. Execute Phase 2 iteratively (commit after each component)

**Benefits:**
- Immediate improvement to project structure
- Clear foundation for next steps
- Low risk, high value
