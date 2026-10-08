# Agent Visibility System

## Overview

The Agent Visibility System provides fine-grained control over where agents appear in the AgentSystem. This allows you to distinguish between user-facing UI agents, backend tool agents, dual-purpose agents, and experimental/private agents.

## Visibility Modes

The `metadata.visibility` field controls agent exposure:

| Mode | UI Dropdown | Tool Discovery | Use Case |
|------|-------------|----------------|----------|
| `ui` | ✅ Yes | ❌ No | User-facing agents (chat interface only) |
| `tool` | ❌ No | ✅ Yes | Backend services for other agents |
| `both` | ✅ Yes | ✅ Yes | Dual-purpose agents |
| `private` | ❌ No | ❌ No | Testing/experimental agents (default) |

**Default:** `private` — an agent has to opt in to being listed.

> ⚠️ **Visibility is display, not access control.** `visibility` only determines
> where an agent *shows up* (UI list, tool list of other agents). Anyone who knows its name
> could still start it: `POST /run` and `/events` with `agent_name`, `/chat/command`,
> a SAM with `allowed_agents: ["*"]`. Even `private` protects nothing. Who may
> **run** an agent is governed by `metadata.min_role` (see below).

## Who may run an agent: `metadata.min_role`

```yaml
metadata:
  visibility: both
  min_role: admin        # guest | user | admin; omit = no gate
```

`min_role` is the lowest account role that may **run** the agent. It is checked
on every path on which a run starts (`src/agent_system/auth/agent_access.py`):

- **HTTP:** `POST /run`, new `/events` runs, `/chat/command`, `POST /api/sessions`
  (a session whose agent the owner may not run is not created).
  `GET /agents` lists the agent only for those it is allowed for; `default` is `null` if the
  entry agent is not allowed. `/agents/{name}/tools` and `/allowed-tools` as well as
  `/chat/commands` show it only then. A rejection answers exactly like an agent
  that does not exist (404 or the same response; `/chat/command` also for the
  entry agent if no name is passed); the reason appears only in the server log.
  Exceptions: `/run` and `/events` without `agent_name` for the entry agent (403
  "Permission denied") and `POST /api/sessions` (403 with the reason -- names that do not
  exist are accepted there too, so an "unknown" response does not exist).
- **OpenAI API (`openai_api`):** an agent the caller may not run is
  not a model for them -- `GET /models` does not list it, and `/models/{id}`,
  `/responses` and `/chat/completions` answer 404 `model_not_found` as for an
  unknown model.
- **Without an endpoint:** the run itself (`Agent.run_events`, also stategraph `MachineAgent`)
  checks before anything else -- sub-agents via the SAM, agents as tools, stategraph,
  woken agent-cli runs. Every tool of a gated agent (`<name>_*`) likewise. Its
  rejection carries `error_type` `agent_role_gate` (or `foreign_session` if the session
  belongs to another user): none of it is stored, the OpenAI API answers
  404 or 403, and SAM `create` and `continue` report it as an error instead of as a finished
  instance. A rejected `create` leaves no instance behind; a continued one remains, with
  `failed` and this `error_type`, and a background job ends the same way -- also in the
  stored state that a later `poll` or `wait` reads.
- **SAM:** `create` and `continue` reject beforehand, as a tool error with
  `error_type: "agent_role_gate"`, before a sub-session is created.
- **Waking:** a session whose agent its owner may not run is not
  woken; the pending input stays in place.

Who is the caller? The owner of the request ID registered by the framework (API,
tool execution, SAM, stategraph), otherwise the user of the session (agent-cli), otherwise
`anonymous`. **A run without any identity counts as `anonymous`: rejected, unless
anonymous access is active and its role suffices.** The SAM and the tools of a
gated agent reject a caller without identity without this exception.
An unknown or inactive account, an unknown role
and an unreadable user store are rejected as well. The role is read
fresh from the user store on every run.

`cli_user` (the default user of `agent-cli`/`agent-run`) counts as local operator
**only in these local processes** and passes every gate -- and even there only
as long as no account of that name exists. In the API process `cli_user` is a name without
an account and is rejected.

Without `auth.enabled` there are no roles: the gate does not apply, and the server warns at
startup which agents carry a gate that it cannot enforce.

⚠️ **Inheritance:** `metadata` is deep-merged along the `type:` chain. An agent whose
`type:` points to a gated agent inherits its `min_role` even if it sets
nothing itself, and **`min_role: null` does not clear an inherited value** (null is skipped
when merging). Whoever wants a lower value sets it explicitly (`guest` or
`user`); measured with `load_settings` + `get_tool_server_config`.

## Configuration

### YAML configuration

Agents are server entries under `plugins: servers:`, in `config/plugins.yaml`,
any `config/agents*/*.yaml` or a plugin's `agents/*.yaml` (see
[Configuration-Based Agents](config_based_agents.md)):

```yaml
plugins:
  servers:
    # UI-only agent
    support_chat:
      type: basic_agent
      enabled: true
      description: "Answers questions about the product"
      agent_config:
        llm_profile: [normal, think]
        max_steps: 20
        system_template: "./prompts/support_chat.md"
        tools:
          allowed: ["web_scraper/*"]
      metadata:
        visibility: "ui"       # in the UI dropdown, NOT as a tool
        category: "support"

    # Tool-only agent (backend service)
    text_summarizer:
      type: basic_agent
      enabled: true
      description: "Text summarization service for other agents"
      agent_config:
        llm_profile: normal
        max_steps: 5
        system_prompt: "You are a text summarizer. Provide concise summaries."
      metadata:
        visibility: "tool"     # callable by other agents, NOT in the UI

    # Dual-purpose agent
    research_agent:
      type: basic_agent
      enabled: true
      description: "Web research agent with search capabilities"
      agent_config:
        llm_profile: normal
        max_steps: 15
        tools:
          allowed: ["duckduckgo_search/*", "web_scraper/*"]
      metadata:
        visibility: "both"     # UI + tool

    # Private/experimental agent
    experimental_rag:
      type: basic_agent
      enabled: true
      description: "Experimental RAG agent for testing"
      agent_config:
        llm_profile: think
        max_steps: 10
      metadata:
        visibility: "private"  # neither UI nor tool (the default)
```

### Plugin-Agents (plugin.toml)

For a plugin, `visibility` is in the `[plugin]` table of its
manifest — that is the table `load_plugin_metadata` returns and from
which `ServerDecl.visibility` reads (`src/agent_system/runtime.py`):

```toml
# src/plugins/<name>/plugin.toml
[plugin]
name = "my_agent_plugin"
author = "Enrico Ehrich"
version = "0.1.0"
description = "Specialized web research agent"
entrypoint = "plugin:PLUGIN_FACTORY"
type = ["tool-server"]
category = "tools"
visibility = "both"  # "ui", "tool", "both" or "private"
```

**Order** (`ServerDecl.visibility` in `src/agent_system/runtime.py`) —
the first source that names a visibility wins:

1. the instance metadata, when it names one (`metadata.visibility` on the
   entry -- a `metadata` block with only `min_role` or `author` names none),
2. the manifest,
3. otherwise **`private`** — not visible, safe by default. A
   plugin agent that declares no visibility anywhere therefore appears
   neither in the UI nor as a tool.

⚠️ **No** shipped `plugin.toml` declares a visibility, and no entry in
`config/plugins.yaml` does: every agent that shows somewhere says so in its own
YAML. Anyone relying on a generous default is relying on nothing.

## Internal Implementation

### Visibility Flags

Each agent has two internal flags set based on `metadata.visibility`:

```python
agent._tool_public: bool         # Show in UI dropdown (GET /agents)
agent._tool_visible: bool   # Available in tool discovery
```

**Flag Mapping:**
- `visibility: "ui"` → `_tool_public=True, _tool_visible=False`
- `visibility: "tool"` → `_tool_public=False, _tool_visible=True`
- `visibility: "both"` → `_tool_public=True, _tool_visible=True`
- `visibility: "private"` → `_tool_public=False, _tool_visible=False`

### Tool Discovery Filtering

When an agent queries `list_usable_tools()`, the registry is filtered:

```python
# In ToolDiscoveryService._get_registry_tools() (servers/agent/tool_discovery.py), used by Agent.list_usable_tools()
for tool_name in self.registry.list():
    server = self.registry.get(tool_name)
    if hasattr(server, '_tool_visible'):
        if not server._tool_visible:
            continue  # Skip agents with tool_visible=False
    available_tools.append(tool_name)
```

This ensures that UI-only agents (`visibility: "ui"`) do **not** appear in tool lists for other agents.

## Use Cases

### 1. User-Facing Agents (UI-only)

**Example:** `support_chat`, `code_reviewer`, `research_assistant`

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

**Example:** `research_agent`, `basic_agent`

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
- Specialized analysts (research, code review, etc.)

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
```

`metadata` keeps `visibility`, `min_role`, `author`, `version`, `tags` and
`category`; other keys (a `changelog`) are dropped when the config loads.

## Migration Guide

### Updating Existing Agents

**Before** (no visibility -- private, shown nowhere):
```yaml
support_chat:
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
support_chat:
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

If `metadata.visibility` is **not specified**, the default is `"private"` (see
the order above: instance metadata, then the plugin manifest, else private):
- Agent does NOT appear in the UI dropdown
- Agent is NOT available as a tool

## Schema Validation

The config model (`AgentMetadata` in `src/agent_system/config/models.py`) and the
JSON schemas generated from it (`schemas/plugins-config.schema.json`,
`schemas/main-config.schema.json`, `schemas/config-part.schema.json`) accept
these values:

```json
"visibility": {
  "default": "private",
  "enum": ["ui", "tool", "both", "private"]
}
```

Invalid values are rejected when the config loads.

## API Endpoints

### GET /agents
Returns list of agents with `_tool_public=True` (visibility: "ui" or "both")

### Tool Discovery (Internal)
`list_usable_tools()` returns agents with `_tool_visible=True` (visibility: "tool" or "both")

## Future Enhancements

Planned improvements:
1. **Permission Inheritance** - Control tool scope in nested calls
2. **Resource Limits** - Max concurrent agent calls, timeout controls
3. **Audit Logging** - Track agent-to-agent call chains
4. **Dynamic Visibility** - Change visibility at runtime via API

(A call to an agent that already runs above it is refused today:
`error_type: "recursive_call"`.)

## See Also

- [Configuration-Based Agents](./config_based_agents.md)
- [Plugin Authoring Guide](./plugin_authoring.md)
