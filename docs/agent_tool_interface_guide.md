# Agent Tool Interface Guide

**Purpose**: Prevent confusion between Agent's dual tool interfaces

## The Problem

Agent has TWO tool interfaces that serve completely different purposes but are easily confused:
1. Tools this agent **OFFERS** (external ToolServer interface)
2. Tools this agent **CAN USE** (internal execution interface)

## Clear Interface Names

### 1. EXTERNAL: What This Agent OFFERS to Others

```python
async def list_tools(self) -> List[ToolDef]:
    """Return tools this agent OFFERS to other agents (ToolServer interface)."""
```

- **Purpose**: ToolServer standard interface
- **Used by**: ToolSchemaBuilder when other agents discover available tools
- **Returns**: Single ToolDef representing this agent as a callable tool
- **Example**: When Agent A queries Agent B's tools, it gets B's schema (not B's internal tools)
- **Think**: "What can others call on me?"

**Call flow**:
```
Other Agent → ToolSchemaBuilder.build_schemas() 
           → server.list_tools() 
           → Returns: [ToolDef(name="my_agent", description="...", input_schema={...})]
```

### 2. INTERNAL: What This Agent CAN USE

```python
async def list_usable_tools(self) -> List[str]:
    """Return list of tool names this agent CAN USE (filtered by agent config)."""
```

- **Purpose**: Get tools available for this agent's execution
- **Used by**: `_run_events()` to build LLM prompt with available tools
- **Returns**: List of tool server names (filtered by `agent_config.tools.allowed`)
- **Example**: `["datetime", "web_search", "other_agent"]`
- **Think**: "What can I call during my execution?"

**Call flow**:
```
Agent._run_events() → self.list_usable_tools()
                   → ToolDiscoveryService.discover_allowed_tools()
                   → Returns: ["datetime", "web_search", ...]
```

### 3. UTILITY: Detailed Info for User-Facing Endpoints

```python
async def _list_usable_tools_with_details(self, params: Dict[str, Any]) -> List[Dict]:
    """Return detailed info about tools this agent CAN USE (name + description)."""
```

- **Purpose**: Provide human-readable tool listing for debugging/introspection
- **Used by**: BasicAgent's `list_available_tools` tool
- **Returns**: `[{"name": "datetime", "description": "..."}, ...]`
- **Think**: "What can I use? (with details for humans)"

## Quick Reference

| Method | Purpose | Returns | Used By |
|--------|---------|---------|---------|
| `list_tools()` | What I OFFER | `List[ToolDef]` | ToolServer interface, ToolSchemaBuilder |
| `list_usable_tools()` | What I CAN USE | `List[str]` | Internal execution (_run_events) |
| `_list_usable_tools_with_details()` | What I CAN USE (detailed) | `List[Dict]` | BasicAgent tools, API endpoints |

## Common Confusion Patterns

### ❌ WRONG: Thinking list_tools() returns tools the agent can use
```python
# This gets what the agent OFFERS, not what it CAN USE
tools = await agent.list_tools()
# tools = [ToolDef(name="my_agent", ...)]  # Just the agent itself!
```

### ✅ CORRECT: Getting tools the agent can use
```python
# This gets what the agent CAN USE internally
tool_names = await agent.list_usable_tools()
# tool_names = ["datetime", "web_search", "other_agent"]
```

### ❌ WRONG: Expecting list_tools() to be filtered by agent config
```python
# list_tools() is ToolServer interface - NOT filtered by agent config
tools = await agent.list_tools()
# It ALWAYS returns the agent itself, regardless of config
```

### ✅ CORRECT: Getting filtered tools
```python
# list_usable_tools() respects agent_config.tools.allowed patterns
tool_names = await agent.list_usable_tools()
# Filtered based on agent_config.tools.allowed = ["datetime/*"]
```

## Mental Model

Think of Agent as having two "faces":

```
┌─────────────────────────────────────────┐
│            AGENT (Dual Face)            │
├─────────────────────────────────────────┤
│                                         │
│  EXTERNAL FACE (ToolServer)              │
│  ├─ list_tools() ───> "I'm callable!"  │
│  │                                      │
│  └─ What others see when they query    │
│                                         │
├─────────────────────────────────────────┤
│                                         │
│  INTERNAL FACE (Executor)               │
│  ├─ list_usable_tools() ───> Tools I   │
│  │                           can call   │
│  └─ What I use during execution         │
│                                         │
└─────────────────────────────────────────┘
```

## Real-World Example

```python
# Agent A wants to use Agent B as a tool

# Step 1: Agent A queries what tools are available
available_tool_names = await agent_a.list_usable_tools()
# Returns: ["datetime", "agent_b", "web_search"]

# Step 2: ToolSchemaBuilder builds schemas for each tool
for tool_name in available_tool_names:
    server = registry.get(tool_name)
    schema = await server.list_tools()  # Gets ToolDef schema
    # For "agent_b": Returns [ToolDef(name="agent_b", description="...")]

# Step 3: Agent A calls Agent B
result = await agent_b.call("run", {"task": "analyze data"})
```

## Key Takeaways

1. **list_tools()** = ToolServer interface = What I OFFER to others
2. **list_usable_tools()** = Internal interface = What I CAN USE
3. They serve **completely different purposes** - don't confuse them!
4. When in doubt: 
   - External/ToolServer → `list_tools()`
   - Internal/Execution → `list_usable_tools()`

## History

- **Before refactoring**: Methods had confusing names (`list_allowed_tool_servers`)
- **After refactoring**: Clear separation with descriptive names
- **Reason**: Constant confusion between "tools I offer" vs "tools I use"
