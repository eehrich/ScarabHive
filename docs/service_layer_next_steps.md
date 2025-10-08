# Service Layer Refactoring - Next Steps Guide

## 🎯 **Current Status**

### ✅ **Completed (100%)**
1. **Service Layer Implementation** - ALL 4 services complete
   - ConfigService: 193 LOC, 28 tests ✅
   - MCPService: 388 LOC, 25 tests ✅
   - ToolService: 411 LOC, 24 tests ✅
   - AgentService: 535 LOC, 31 tests ✅
   - **Total: 1,527 LOC services, 108 tests, 100% pass rate**

2. **Utilities Module** - atomic_write_text() extracted
   - `utils/io.py`: 49 LOC ✅
   - ToolService refactored to use shared utility ✅

### 🔄 **In Progress (10%)**
3. **interface_api.py Refactoring** - STARTED
   - ✅ Service imports added
   - ✅ ConfigService integrated in build_app()
   - ⏳ Need to refactor all endpoints to use services
   - Target: 1,631 → ~800 LOC (50% reduction)

### ⏳ **Pending**
4. **cli.py Refactoring** - NOT STARTED
   - Replace `_atomic_write_text` with utils.io import
   - Refactor to use all 4 services
   - Extract formatting helpers (optional)
   - Target: 1,679 → ~900 LOC (46% reduction)

5. **Final Validation** - NOT STARTED
   - Run full test suite
   - Integration testing
   - Documentation updates

---

## 📋 **Next Steps - Detailed Plan**

### **Step 1: Complete interface_api.py Refactoring**

#### 1.1 Agent Initialization
**Location**: Lines 210-350 in interface_api.py

**Current Code Pattern**:
```python
# Bootstrap servers
registry = MCPRegistry()
bootstrap_servers(config, registry)

# Get agent from registry
entry_name = config.default_agent or 'agent'
selected_agent = registry.get(entry_name)
```

**Refactored Approach**:
```python
# Initialize AgentService after agent is created
global _agent_service
_agent_service = AgentService(selected_agent, config)
```

**Files to modify**:
- `interface_api.py` lines ~220-350 (agent bootstrap section)

---

#### 1.2 `/run` Endpoint - Use AgentService
**Location**: Lines 429-550 in interface_api.py

**Current Code Pattern**:
```python
@app.post("/run")
async def run_task(request: Request):
    # Parse multipart or JSON
    # Create multimodal message if images
    # Call agent.run_events() directly
    async for event in agent.run_events(message, request_id, session_id):
        yield event
```

**Refactored Approach**:
```python
@app.post("/run")
async def run_task(request: Request):
    # Parse request
    async for event in _agent_service.execute_task(
        task=task,
        session_id=session_id,
        request_id=request_id,
        images=images
    ):
        yield event
```

**Estimated Reduction**: 429-550 (121 lines) → ~40 lines = **-67%**

---

#### 1.3 Session Endpoints - Use AgentService
**Location**: Lines 659-700 in interface_api.py

**Current Endpoints**:
```python
@app.post("/sessions")          # Create session
@app.get("/sessions")            # List sessions
@app.post("/sessions/{id}/append")  # Append message
@app.post("/sessions/{id}/force_optimize")  # Optimize
```

**Refactored Approach**:
```python
@app.post("/sessions")
async def create_session():
    session_id = await _agent_service.create_session()
    return {"session_id": session_id}

@app.get("/sessions")
async def list_sessions():
    return await _agent_service.list_sessions()

@app.post("/sessions/{id}/append")
async def append_message(id: str, content: str):
    success = await _agent_service.append_to_session(id, content)
    if not success:
        raise HTTPException(404, "Session not found")
    return {"success": True}

@app.post("/sessions/{id}/force_optimize")
async def optimize(id: str):
    return await _agent_service.optimize_session(id)
```

**Estimated Reduction**: 659-700 (41 lines) → ~20 lines = **-51%**

---

#### 1.4 MCP Endpoints - Use MCPService
**Location**: Various sections in interface_api.py

**Current Pattern**:
```python
# Direct MCPIntegration usage
servers = config.mcp_system.servers
client = mcp_integration.client_manager.get_client(server_name)
```

**Refactored Approach**:
```python
@app.get("/mcp/servers")
async def list_mcp_servers():
    return await _mcp_service.list_servers()

@app.post("/mcp/servers/{name}/connect")
async def connect_server(name: str):
    return await _mcp_service.connect_server(name)

@app.get("/mcp/servers/{name}/status")
async def server_status(name: str):
    return await _mcp_service.get_server_status(name)
```

**Estimated Reduction**: ~150 lines → ~30 lines = **-80%**

---

#### 1.5 Tool Endpoints - Use ToolService
**Location**: Various sections in interface_api.py

**Current Pattern**:
```python
# Direct YAML manipulation
config_path = Path("config/mcp.yaml")
with open(config_path) as f:
    config_data = yaml.safe_load(f)
# Modify config_data
with open(config_path, 'w') as f:
    yaml.safe_dump(config_data, f)
```

**Refactored Approach**:
```python
@app.post("/tools/{server}/{tool}/block")
async def block_tool(server: str, tool: str):
    return await _tool_service.block_tool(server, tool)

@app.post("/tools/{server}/{tool}/allow")
async def allow_tool(server: str, tool: str):
    return await _tool_service.allow_tool(server, tool)

@app.get("/tools/{server}")
async def list_tools(server: str):
    return await _tool_service.list_tools(server)
```

**Estimated Reduction**: ~200 lines → ~40 lines = **-80%**

---

### **Step 2: Complete cli.py Refactoring**

#### 2.1 Replace _atomic_write_text
**Location**: Lines 41-76 in cli.py

**Current Code**:
```python
def _atomic_write_text(path: Path, data: str) -> None:
    """Atomically write text to `path`..."""
    # 35 lines of implementation
```

**Refactored**:
```python
from .utils.io import atomic_write_text

# Replace all calls:
# _atomic_write_text(path, data) → atomic_write_text(path, data)
```

**Estimated Reduction**: -35 lines

---

#### 2.2 MCP Commands - Use MCPService
**Location**: Lines 100-400 in cli.py

**Current Pattern**:
```python
async def _mcp_list_servers(mcp_integration, args):
    # Direct MCPIntegration usage
    servers = config.mcp_system.servers
    # Complex status checking
```

**Refactored**:
```python
async def _mcp_list_servers(mcp_service, args):
    servers = await mcp_service.list_servers()
    # Simple formatting
    print(json.dumps(servers, indent=2))
```

**Similar refactoring for**:
- `_mcp_connect_server`
- `_mcp_disconnect_server`
- `_mcp_server_status`
- `_mcp_test_server`

**Estimated Reduction**: ~300 lines → ~100 lines = **-67%**

---

#### 2.3 Tool Commands - Use ToolService
**Location**: Lines 400-600 in cli.py

**Current Pattern**:
```python
async def _tool_action(mcp_integration, args):
    # Complex YAML manipulation
    config_path = Path("config/mcp.yaml")
    # Read, modify, write YAML
```

**Refactored**:
```python
async def _tool_action(tool_service, args):
    if args.action == "block":
        result = await tool_service.block_tool(args.server, args.tool)
    elif args.action == "allow":
        result = await tool_service.allow_tool(args.server, args.tool)
    elif args.action == "list":
        result = await tool_service.list_tools(args.server)
    print(json.dumps(result, indent=2))
```

**Estimated Reduction**: ~200 lines → ~50 lines = **-75%**

---

#### 2.4 Agent Commands - Use AgentService
**Location**: Lines 800-1200 in cli.py

**Current Pattern**:
```python
# Direct agent usage
async for event in agent.run_events(task, request_id):
    # Process events
```

**Refactored**:
```python
async for event in agent_service.execute_task(task, session_id, request_id):
    # Process events (same logic)
```

**Estimated Reduction**: ~400 lines → ~250 lines = **-37%**

---

### **Step 3: Service Initialization in cli.py**

Add service initialization at the start of CLI main():

```python
async def async_main():
    # Initialize services
    config_service = ConfigService(config_path)
    config = config_service.load_config()
    config_service.setup_logging()
    
    # Initialize MCP
    mcp_integration = await initialize_mcp(config)
    
    # Initialize services
    mcp_service = MCPService(mcp_integration, config)
    tool_service = ToolService(mcp_integration, config)
    
    # Agent service initialized after agent is created
    agent = ... # from bootstrap
    agent_service = AgentService(agent, config)
    
    # Pass services to command handlers
    await handle_command(args, mcp_service, tool_service, agent_service)
```

---

## 📊 **Expected Results**

### Before Refactoring
| File | LOC | Complexity |
|------|-----|------------|
| interface_api.py | 1,631 | High |
| cli.py | 1,679 | High |
| **Total** | **3,310** | - |

### After Refactoring
| File | LOC | Complexity | Reduction |
|------|-----|------------|-----------|
| interface_api.py | ~800 | Low | -50% |
| cli.py | ~900 | Medium | -46% |
| **Total** | **1,700** | - | **-49%** |

**Code Eliminated**: ~1,610 LOC (almost 50%!)

---

## 🎯 **Benefits of Refactoring**

1. **Reduced Duplication**
   - MCP operations: Unified in MCPService
   - Tool management: Unified in ToolService
   - Agent execution: Unified in AgentService
   - File I/O: Shared atomic_write_text

2. **Improved Testability**
   - Services already have 108 comprehensive tests
   - API/CLI just become thin wrappers
   - Integration tests easier to write

3. **Better Maintainability**
   - Single source of truth for each operation
   - Changes in one service = automatic propagation
   - Clear separation of concerns

4. **Cleaner Code**
   - API endpoints: 5-10 lines each
   - CLI commands: 10-20 lines each
   - No business logic in presentation layer

---

## ⚠️ **Refactoring Considerations**

### 1. Agent Initialization Complexity
**Challenge**: Agent is created by `bootstrap_servers()` which is called in both cli.py and interface_api.py

**Solution**: Create AgentService AFTER agent is initialized:
```python
# In both files:
agent = bootstrap_and_get_agent(config, registry)
agent_service = AgentService(agent, config)
```

### 2. Backward Compatibility
**Note**: Per project requirements, NO backward compatibility needed. Clean slate implementation.

### 3. Testing Strategy
**Approach**:
1. Refactor one file at a time
2. Run tests after each major change
3. Use existing tests to catch regressions
4. Add integration tests at the end

### 4. Error Handling
**Pattern**: Services already log all exceptions. API/CLI just need to:
```python
try:
    result = await service.method()
    return result
except Exception as e:
    logger.exception("Operation failed")
    raise HTTPException(500, str(e))  # API
    # or
    print(json.dumps({"error": str(e)}))  # CLI
```

---

## 🚀 **Recommended Execution Order**

### Phase 1: interface_api.py (Easier, more structured)
1. ✅ Add service imports (DONE)
2. ✅ Initialize ConfigService (DONE)
3. ⏳ Initialize MCPService, ToolService (STARTED)
4. ⏳ Initialize AgentService after agent bootstrap
5. ⏳ Refactor `/run` endpoint
6. ⏳ Refactor session endpoints
7. ⏳ Refactor MCP endpoints
8. ⏳ Refactor tool endpoints
9. ⏳ Test API with existing tests

**Estimated Time**: 2-3 hours

### Phase 2: cli.py (More complex, many commands)
1. Replace `_atomic_write_text` with utils.io
2. Add service initialization to main()
3. Refactor MCP commands
4. Refactor tool commands
5. Refactor agent commands
6. Test CLI with existing tests

**Estimated Time**: 3-4 hours

### Phase 3: Final Validation
1. Run full test suite: `pytest -q --tb=short`
2. Manual integration testing (API + CLI)
3. Update documentation
4. Git commit with detailed message

**Estimated Time**: 1 hour

**Total Estimated Time**: 6-8 hours

---

## 📝 **Code Templates**

### Template: API Endpoint using Service
```python
@app.get("/endpoint/{param}")
async def endpoint_handler(param: str):
    """Endpoint description."""
    logger = logging.getLogger(__name__)
    logger.debug("Handling endpoint: param=%s", param)
    
    try:
        result = await _service.method(param)
        return result
    except Exception as e:
        logger.exception("Endpoint failed: param=%s, error=%s", param, e)
        raise HTTPException(500, f"Operation failed: {e}")
```

### Template: CLI Command using Service
```python
async def _cli_command(service, args):
    """CLI command description."""
    logger = logging.getLogger(__name__)
    
    try:
        result = await service.method(args.param)
        
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            # Human-readable format
            print(f"Result: {result}")
            
    except Exception as e:
        logger.exception("Command failed: %s", e)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
```

---

## 🎓 **Key Learnings**

1. **Services are the foundation** - All business logic lives here
2. **API/CLI are thin wrappers** - Just handle I/O and call services
3. **Error handling is simple** - Services log, wrappers format
4. **Testing is easier** - Test services once, trust wrappers
5. **Maintenance is cleaner** - Change once, apply everywhere

---

## ✅ **Success Criteria**

- [ ] interface_api.py reduced from 1,631 to ~800 LOC (50% reduction)
- [ ] cli.py reduced from 1,679 to ~900 LOC (46% reduction)
- [ ] All existing tests passing (100% pass rate)
- [ ] No new bugs introduced (integration testing)
- [ ] Services used everywhere (no direct config/MCP/tool access)
- [ ] Code is cleaner and more maintainable

---

**Status**: Ready to continue refactoring! 🚀

**Next Action**: Complete interface_api.py refactoring, starting with agent initialization and `/run` endpoint.
