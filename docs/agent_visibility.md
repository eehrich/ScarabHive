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

> ⚠️ **Sichtbarkeit ist Anzeige, keine Zugriffskontrolle.** `visibility` bestimmt nur,
> wo ein Agent *auftaucht* (UI-Liste, Tool-Liste anderer Agents). Wer seinen Namen kennt,
> konnte ihn trotzdem starten: `POST /run` und `/events` mit `agent_name`, `/chat/command`,
> ein SAM mit `allowed_agents: ["*"]`. Auch `private` schützt nichts. Wer einen Agent
> **ausführen** darf, regelt `metadata.min_role` (siehe unten).

## Wer darf einen Agent ausführen: `metadata.min_role`

```yaml
metadata:
  visibility: both
  min_role: admin        # guest | user | admin; weglassen = kein Gate
```

`min_role` ist die niedrigste Konto-Rolle, die den Agent **laufen lassen** darf. Gefragt
wird bei jedem Weg, auf dem ein Lauf beginnt (`src/agent_system/auth/agent_access.py`):

- **HTTP:** `POST /run`, neue `/events`-Läufe, `/chat/command`, `POST /api/sessions`
  (eine Session, deren Agent der Besitzer nicht ausführen darf, wird nicht angelegt).
  `GET /agents` listet den Agent nur, wem er erlaubt ist; `default` ist `null`, wenn der
  Einstiegs-Agent es nicht ist. `/agents/{name}/tools` und `/allowed-tools` sowie
  `/chat/commands` zeigen ihn nur dann. Eine Ablehnung antwortet genau wie ein Agent,
  den es nicht gibt (404 bzw. dieselbe Antwort; `/chat/command` auch für den
  Einstiegs-Agent, wenn kein Name mitkommt); der Grund steht nur im Server-Log.
  Ausnahmen: `/run` und `/events` ohne `agent_name` für den Einstiegs-Agent (403
  „Permission denied“) und `POST /api/sessions` (403 mit dem Grund -- dort werden auch
  Namen angenommen, die es nicht gibt, eine Antwort „unbekannt“ gibt es also nicht).
- **OpenAI-API (`openai_api`):** ein Agent, den der Aufrufer nicht ausführen darf, ist
  kein Modell für ihn -- `GET /models` listet ihn nicht, und `/models/{id}`,
  `/responses` und `/chat/completions` antworten 404 `model_not_found` wie für ein
  unbekanntes Modell.
- **Ohne Endpoint:** der Lauf selbst (`Agent.run_events`, auch stategraph `MachineAgent`)
  fragt vor allem anderen -- Sub-Agents über den SAM, Agents als Tool, stategraph,
  geweckte agent-cli-Läufe. Jedes Tool eines gegateten Agents (`<name>_*`) ebenso. Seine
  Ablehnung trägt `error_type` `agent_role_gate` (bzw. `foreign_session`, wenn die Session
  einem anderen Nutzer gehört): nichts davon wird gespeichert, die OpenAI-API antwortet
  404 bzw. 403, und SAM-`create` und -`continue` melden sie als Fehler statt als beendete
  Instanz. Ein abgelehntes `create` hinterlässt keine Instanz; eine fortgesetzte bleibt, mit
  `failed` und diesem `error_type`, und ein Hintergrund-Job endet ebenso -- auch im
  gespeicherten Stand, den ein späteres `poll` oder `wait` liest.
- **SAM:** `create` und `continue` lehnen vorher ab, als Tool-Fehler mit
  `error_type: "agent_role_gate"`, bevor eine Sub-Session entsteht.
- **Wecken:** eine Session, deren Agent ihr Besitzer nicht ausführen darf, wird nicht
  geweckt; die wartende Eingabe bleibt liegen.

Wer ist der Aufrufer? Der vom Framework registrierte Besitzer der Request-ID (API,
Tool-Ausführung, SAM, stategraph), sonst der Benutzer der Session (agent-cli), sonst
`anonymous`. **Ein Lauf ohne jede Identität zählt als `anonymous`: abgelehnt, außer
anonymer Zugang ist aktiv und seine Rolle reicht.** Der SAM und die Tools eines
gegateten Agents lehnen einen Aufrufer ohne Identität ohne diese Ausnahme ab.
Abgelehnt werden ebenso ein unbekanntes oder inaktives Konto, eine unbekannte Rolle
und ein nicht lesbarer User-Store. Die Rolle wird bei jedem Lauf
frisch aus dem User-Store gelesen.

`cli_user` (der Standard-Benutzer von `agent-cli`/`agent-run`) gilt **nur in diesen
lokalen Prozessen** als lokaler Betreiber und passiert jedes Gate -- und auch dort nur,
solange kein Konto dieses Namens existiert. Im API-Prozess ist `cli_user` ein Name ohne
Konto und wird abgelehnt.

Ohne `auth.enabled` gibt es keine Rollen: das Gate greift nicht, und der Server warnt beim
Start, welche Agents ein Gate tragen, das er nicht durchsetzen kann.

⚠️ **Vererbung:** `metadata` wird über die `type:`-Kette tief gemergt. Ein Agent, dessen
`type:` auf einen gegateten Agent zeigt, erbt dessen `min_role`, auch wenn er selbst
nichts setzt, und **`min_role: null` hebt einen geerbten Wert nicht auf** (null wird beim
Mergen übergangen). Wer einen niedrigeren Wert will, setzt ihn ausdrücklich (`guest` oder
`user`); gemessen mit `load_settings` + `get_tool_server_config`.

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
      system_template: "config/prompts/financial_analyst_prompt.md"
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
  research_agent:
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

### Plugin-Agents (plugin.toml)

Bei einem Plugin steht `visibility` in der `[plugin]`-Tabelle seines
Manifests — das ist die Tabelle, die `load_plugin_metadata` liefert und aus
der `ServerDecl.visibility` liest ([runtime.py:129](../src/agent_system/runtime.py#L129)):

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
visibility = "both"  # "ui", "tool", "both" oder "private"
```

**Reihenfolge** ([runtime.py:120-132](../src/agent_system/runtime.py#L120-L132)) —
die erste Quelle, die etwas sagt, gewinnt:

1. die Instanz-Metadaten (`metadata.visibility` beim Eintrag in
   `config/plugins.yaml`),
2. das Manifest,
3. sonst **`private`** — nicht sichtbar, sicher per Default. Ein
   Plugin-Agent, der nirgends eine Sichtbarkeit deklariert, taucht also
   weder in der UI noch als Tool auf.

⚠️ Gemessen am 06.09.2026: **kein** ausgeliefertes `plugin.toml` deklariert
eine Sichtbarkeit, und in `config/plugins.yaml` tut es genau ein Eintrag. Wer
sich auf einen großzügigen Default verlässt, verlässt sich auf nichts.

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
# In Agent.list_usable_tools()
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

If `metadata.visibility` is **not specified**, the default is `"private"` (see
the order above: instance metadata, then the plugin manifest, else private):
- Agent does NOT appear in the UI dropdown
- Agent is NOT available as a tool

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
Returns list of agents with `_tool_public=True` (visibility: "ui" or "both")

### Tool Discovery (Internal)
`list_usable_tools()` returns agents with `_tool_visible=True` (visibility: "tool" or "both")

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
