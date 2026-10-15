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
- **Returns**: The tools this agent offers. The base `Agent` (`servers/agent/server.py`) returns a
  single ToolDef, the agent itself as a callable tool. A `SchemaBasedAgent` (`servers/agent/schema_based.py`;
  `BasicAgent` and so most configured agents) returns the tools of its `schema.yaml` instead, e.g.
  `<name>_execute_task` and `<name>_list_available_tools`
- **Example**: When Agent A queries Agent B's tools, it gets B's own tools (not the tools B may call)
- **Think**: "What can others call on me?"

**Call flow**:
```
Other Agent → ToolSchemaBuilder.build_schemas() 
           → server.list_tools() 
           → Returns: [ToolDef(name="my_agent", description="...", input_schema={...})]
             (a SchemaBasedAgent: [ToolDef(name="my_agent_execute_task", ...), ...])
```

### 2. INTERNAL: What This Agent CAN USE

```python
async def list_usable_tools(self) -> tuple[list[str], list[str] | None, list[str] | None]:
    """Return list of tool names this agent CAN USE (filtered by agent config)."""
```

- **Purpose**: Get tools available for this agent's execution
- **Used by**: `_initialize_request_and_conversation()` (`mixins/run_phases.py`, called from
  `_run_events()`) to build the tool schemas for the LLM; also the agent API routes
- **Returns**: Tuple of (tool server names filtered by `agent_config.tools.allowed`, allowed patterns, blocked patterns)
- **Example**: `["datetime", "web_search", "other_agent"]`
- **Think**: "What can I call during my execution?"

**Call flow**:
```
Agent._run_events() → _initialize_request_and_conversation() → self.list_usable_tools()
                   → ToolDiscoveryService.discover_allowed_tools()
                   → Returns: (["datetime", "web_search", ...], allowed, blocked)
```

### 3. UTILITY: Detailed Info for User-Facing Endpoints

```python
async def _list_usable_tools_with_details(self, params: Dict[str, Any]) -> List[Dict]:
    """Return detailed info about tools this agent CAN USE (name + description)."""
```

- **Purpose**: Provide human-readable tool listing for debugging/introspection
- **Used by**: BasicAgent's `list_available_tools` tool, the agent API routes and `agent-cli chat`
- **Returns**: `[{"name": "datetime", "description": "..."}, ...]`
- **Think**: "What can I use? (with details for humans)"

## Quick Reference

| Method | Purpose | Returns | Used By |
|--------|---------|---------|---------|
| `list_tools()` | What I OFFER | `List[ToolDef]` | ToolServer interface, ToolSchemaBuilder |
| `list_usable_tools()` | What I CAN USE | `(List[str], allowed, blocked)` | Internal execution (_run_events) |
| `_list_usable_tools_with_details()` | What I CAN USE (detailed) | `List[Dict]` | BasicAgent tools, API endpoints, `agent-cli chat` |

## Common Confusion Patterns

### ❌ WRONG: Thinking list_tools() returns tools the agent can use
```python
# This gets what the agent OFFERS, not what it CAN USE
tools = await agent.list_tools()
# tools = [ToolDef(name="my_agent", ...)]  # The agent itself (or its schema.yaml tools)!
```

### ✅ CORRECT: Getting tools the agent can use
```python
# This gets what the agent CAN USE internally
tool_names, _allowed, _blocked = await agent.list_usable_tools()
# tool_names = ["datetime", "web_search", "other_agent"]
```

### ❌ WRONG: Expecting list_tools() to be filtered by agent config
```python
# list_tools() is ToolServer interface - NOT filtered by agent config
tools = await agent.list_tools()
# It ALWAYS returns the agent's own tools, regardless of config
```

### ✅ CORRECT: Getting filtered tools
```python
# list_usable_tools() respects agent_config.tools.allowed patterns
tool_names, _allowed, _blocked = await agent.list_usable_tools()
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
available_tool_names, _, _ = await agent_a.list_usable_tools()
# Returns: ["datetime", "agent_b", "web_search"]

# Step 2: ToolSchemaBuilder builds schemas for each tool
for tool_name in available_tool_names:
    server = registry.get(tool_name)
    schema = await server.list_tools()  # Gets ToolDef schema
    # For "agent_b": Returns [ToolDef(name="agent_b", description="...")]

# Step 3: Agent A calls Agent B. The tool name equal to the agent's name runs a
# task (a SchemaBasedAgent hands that name to Agent.call too)
result = await agent_b.call("agent_b", {"task": "analyze data"})
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
