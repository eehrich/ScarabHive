# Agent Visibility System

## Overview

The Agent Visibility System provides fine-grained control over where agents appear in the AgentSystem. This allows you to distinguish between user-facing UI agents, backend tool agents, dual-purpose agents, and experimental/private agents.

## Visibility Modes

The `metadata.visibility` field controls agent exposure:

| Mode | UI Dropdown | Tool Discovery | Use Case |
|------|-------------|----------------|----------|
| `ui` | ✅ Yes | ❌ No | User-facing agents (chat interface, default) |
| `tool` | ❌ No | ✅ Yes | Backend services for other agents |
| `both` | ✅ Yes | ✅ Yes | Dual-purpose agents |
| `private` | ❌ No | ❌ No | Testing/experimental agents |

**Default:** `ui` (backward compatible - agents visible in UI, not as tools)

## Configuration

### YAML Configuration (agents.yaml)

```yaml
agents:
  # UI-only agent (default)
  financial_analyst:
    enabled: true
    description: "Financial analyst for stock market analysis"
    base_type: agent
    agent_config:
      llm_profile: turbo
      max_steps: 20
      system_template: "config/prompts/financial_analyst_prompt.yaml"
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
    metadata:
      author: "AgentSystem"
      version: "1.0.0"
      visibility: "ui"  # Show in UI dropdown, NOT as tool
      category: "financial"

  # Tool-only agent (backend service)
  text_summarizer:
    enabled: true
    description: "Text summarization service for other agents"
    base_type: agent
    agent_config:
      llm_profile: normal
      max_steps: 5
      system_prompt: "You are a text summarizer. Provide concise summaries."
    metadata:
      visibility: "tool"  # Available as tool, NOT in UI
      category: "support"

  # Dual-purpose agent
  web_research_agent:
    enabled: true
    description: "Web research agent with search capabilities"
    base_type: agent
    agent_config:
      llm_profile: normal
      max_steps: 15
      tools:
        allowed:
          - "duckduckgo_search/*"
          - "web_scraper/*"
    metadata:
      visibility: "both"  # UI + Tool
      category: "research"

  # Private/experimental agent
  experimental_rag:
    enabled: true
    description: "Experimental RAG agent for testing"
    base_type: agent
    agent_config:
      llm_profile: deepseek
      max_steps: 10
    metadata:
      visibility: "private"  # Neither UI nor tool
      category: "development"
```

## Internal Implementation

### Visibility Flags

Each agent has two internal flags set based on `metadata.visibility`:

```python
agent._mcp_public: bool         # Show in UI dropdown (GET /agents)
agent._mcp_tool_visible: bool   # Available in tool discovery
```

**Flag Mapping:**
- `visibility: "ui"` → `_mcp_public=True, _mcp_tool_visible=False`
- `visibility: "tool"` → `_mcp_public=False, _mcp_tool_visible=True`
- `visibility: "both"` → `_mcp_public=True, _mcp_tool_visible=True`
- `visibility: "private"` → `_mcp_public=False, _mcp_tool_visible=False`

### Tool Discovery Filtering

When an agent queries `list_allowed_tool_servers()`, the registry is filtered:

```python
# In Agent.list_allowed_tool_servers()
for tool_name in self.registry.list():
    server = self.registry.get(tool_name)
    if hasattr(server, '_mcp_tool_visible'):
        if not server._mcp_tool_visible:
            continue  # Skip agents with tool_visible=False
    available_tools.append(tool_name)
```

This ensures that UI-only agents (`visibility: "ui"`) do **not** appear in tool lists for other agents.

## Use Cases

### 1. User-Facing Agents (UI-only)

**Example:** `financial_analyst`, `code_reviewer`, `research_assistant`

These agents are designed for direct user interaction:
- Visible in UI agent dropdown
- Users can start conversations with them
- NOT available as tools for other agents
- Prevents accidental nested agent calls

```yaml
metadata:
  visibility: "ui"
```

### 2. Backend Service Agents (Tool-only)

**Example:** `text_summarizer`, `data_validator`, `format_converter`

These agents are specialized backend services:
- NOT visible in UI dropdown
- Available as tools for other agents
- Optimized for programmatic use
- Can be called by multiple agents

```yaml
metadata:
  visibility: "tool"
```

**Usage by other agents:**
```yaml
system_admin:
  agent_config:
    tools:
      allowed:
        - "text_summarizer"  # Can use tool-only agent
```

### 3. Dual-Purpose Agents (Both)

**Example:** `web_research_agent`, `basic_agent`

These agents serve both purposes:
- Visible in UI dropdown (users can chat)
- Available as tools (agents can call them)
- Maximum flexibility
- Common for general-purpose agents

```yaml
metadata:
  visibility: "both"
```

### 4. Private/Experimental Agents

**Example:** `experimental_rag`, `test_agent`

These agents are hidden from both UI and tools:
- Testing new features
- Development/debugging
- Disabled temporarily
- Requires explicit configuration to use

```yaml
metadata:
  visibility: "private"
```

## Best Practices

### 1. Choose Appropriate Visibility

**UI-only** for:
- User-facing conversational agents
- Agents with complex workflows requiring human oversight
- Specialized analysts (financial, code review, etc.)

**Tool-only** for:
- Simple, focused utility functions
- Data transformation services
- Stateless operations
- High-frequency called services

**Both** for:
- General-purpose agents (search, research)
- Agents that benefit from both human and agent interaction
- Delegation patterns (user asks A, A delegates to B)

**Private** for:
- Work-in-progress agents
- Testing/debugging scenarios
- Temporarily disabled agents

### 2. Prevent Infinite Loops

When using agents as tools, be aware of potential circular dependencies:

```yaml
# ❌ BAD: Circular dependency
agent_a:
  agent_config:
    tools:
      allowed: ["agent_b"]

agent_b:
  agent_config:
    tools:
      allowed: ["agent_a"]  # Can loop infinitely!
```

**Solution:** Use `visibility: "ui"` for one of them to break the cycle.

### 3. Document Tool Agents

Tool-only agents should have clear descriptions since users won't see them in UI:

```yaml
text_summarizer:
  description: "Summarizes text using LLM. Input: long text. Output: concise summary."
  metadata:
    visibility: "tool"
    tags: ["summarization", "text-processing", "utility"]
```

### 4. Version Your Agents

When changing visibility, increment the version:

```yaml
metadata:
  version: "1.1.0"  # Incremented after changing visibility
  visibility: "both"  # Changed from "ui" to "both"
  changelog: "v1.1.0: Now available as tool for other agents"
```

## Migration Guide

### Updating Existing Agents

**Before** (implicit UI-only):
```yaml
financial_analyst:
  enabled: true
  description: "..."
  agent_config:
    # ...
  metadata:
    author: "AgentSystem"
    version: "1.0.0"
```

**After** (explicit visibility):
```yaml
financial_analyst:
  enabled: true
  description: "..."
  agent_config:
    # ...
  metadata:
    author: "AgentSystem"
    version: "1.0.0"
    visibility: "ui"  # ← Add this
```

### Default Behavior

If `metadata.visibility` is **not specified**, the default is `"ui"`:
- Agent appears in UI dropdown
- Agent is NOT available as tool
- Backward compatible with existing configs

## Schema Validation

The JSON schema (`schemas/config-agents.schema.json`) validates visibility values:

```json
"visibility": {
  "type": "string",
  "enum": ["ui", "tool", "both", "private"],
  "default": "ui"
}
```

Invalid values will be rejected during config validation.

## API Endpoints

### GET /agents
Returns list of agents with `_mcp_public=True` (visibility: "ui" or "both")

### Tool Discovery (Internal)
`list_allowed_tool_servers()` returns agents with `_mcp_tool_visible=True` (visibility: "tool" or "both")

## Future Enhancements

Planned improvements:
1. **Call Depth Tracking** - Prevent infinite recursion in agent-to-agent calls
2. **Permission Inheritance** - Control tool scope in nested calls
3. **Resource Limits** - Max concurrent agent calls, timeout controls
4. **Audit Logging** - Track agent-to-agent call chains
5. **Dynamic Visibility** - Change visibility at runtime via API

## See Also

- [Agent Configuration Guide](./agent_configuration.md)
- [Plugin Authoring Guide](./plugin_authoring.md)
- [Tool System Documentation](./tool_system.md)
- [Epic 0043: Configuration-Based Agents](./epic_0043_completion_summary.md)
