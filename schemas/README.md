# JSON Schemas for AgentSystem Configuration

This directory contains JSON Schema definitions for validating AgentSystem configuration files.

## Files

### Configuration Schemas
- **`llm-config.schema.json`**: Schema for `config/llm.yaml` (LLM models and providers).
  GENERIERT aus `agent_system.config.models.LLMSystemConfig` — nicht von Hand
  editieren, sondern `src/scripts/generate_llm_config_schema.py` laufen lassen
  (der Anti-Drift-Test `tests/config/test_llm_config_schema.py` wird sonst rot).
- **`mcp-config.schema.json`**: Schema for `config/config.yaml` (main system configuration)
- **`plugin-config.schema.json`**: Schema for `config/plugins.yaml` (plugin and agent instance configurations)
- **`hooks-config.schema.json`**: Schema for hook configurations
- **`session-schema.json`**: Schema for session data structures

## VS Code Integration

### Setup (Already Configured)

The `.vscode/settings.json` file already contains schema mappings for automatic validation:

```json
{
  "yaml.schemas": {
    "./schemas/llm-config.schema.json": [
      "config/llm.yaml"
    ],
    "./schemas/mcp-config.schema.json": [
      "config/config.yaml"
    ],
    "./schemas/plugin-config.schema.json": [
      "config/plugins.yaml"
    ]
  },
  "yaml.customTags": [
    "!include"
  ]
}
```

### What You Get

✅ **Autocomplete**: IntelliSense for all config fields
✅ **Validation**: Real-time error detection while typing
✅ **Documentation**: Hover tooltips with field descriptions
✅ **Type checking**: Enum values, patterns, min/max constraints

### How to Use

1. Open any config file (`config/llm.yaml`, `config/config.yaml`, `config/plugins.yaml`)
2. Start typing - VS Code will suggest valid fields
3. Hover over fields to see documentation
4. Errors appear as red squiggles with helpful messages

### Example: Adding a New LLM Model

Open `config/llm.yaml` and start typing under `llm_system.models`:

```yaml
llm_system:
  models:
    my-new-model:  # VS Code suggests: provider, model, context_window, etc.
      provider: |  # Autocomplete shows: openai, anthropic, deepseek, etc.
```

## Schema Details

### Plugin Config Schema (`plugin-config.schema.json`)

Validates plugin and agent instance configurations:

- **Plugin servers**: Configuration for plugin-based servers and agent instances
- **Agent config**: LLM profile, max steps, system prompt/template, tools, context management
- **Tool patterns**: Format `plugin_name/tool_name` or `plugin_name/*`
- **LLM profiles**: Must match profiles in `config/llm.yaml`
- **Context strategies**: Valid strategy names
- **Metadata**: Optional author, version, tags, category

### LLM Config Schema (`llm-config.schema.json`)

Validates LLM configuration. Generated from the Pydantic models
(`LLMSystemConfig` and everything it references), so it always carries the
real providers, fields and enums. Every object is strict
(`additionalProperties: false`): unknown keys — the class of silent dead
config keys like the former `ollama_url`/`include_thinking` — light up in
the editor instead of being ignored at runtime.

Regenerate after any change to the LLM config models:

```bash
.venv/Scripts/python.exe src/scripts/generate_llm_config_schema.py
```

### Main Config Schema (`mcp-config.schema.json`)

Validates main system configuration:

- **name, version, description**: System metadata
- **includes**: Config file includes
- **context**: Auto-datetime, timezone, location
- **network**: SSL, host, port, cache settings
- **default_agent**: Default agent name
- **auth**: Authentication, CORS, rate limiting, admin user
- **logging**: Log levels, file paths, cancellation settings

## Common Validation Errors

### Error: Missing system_prompt or system_template

```
Validation error at my_agent -> agent_config: 
  {'system_prompt': '...'} is not valid under any of the given schemas
```

**Fix**: Provide either `system_prompt` (inline) OR `system_template` (file path), but not both.

### Error: Invalid tool pattern

```
Validation error at my_agent -> agent_config -> tools -> allowed -> 0:
  'invalid-tool' does not match '^[a-z_][a-z0-9_]*/...'
```

**Fix**: Use format `plugin_name/tool_name` or `plugin_name/*`.

### Error: Invalid LLM profile format

```
Validation error at my_agent -> agent_config -> llm_profile:
  'GPT-4' does not match '^[a-z][a-z0-9_-]*$'
```

**Fix**: Use lowercase profile names like `turbo`, `normal`, `deepseek`.

### Error: max_steps out of range

```
Validation error at my_agent -> agent_config -> max_steps:
  150 is greater than the maximum of 100
```

**Fix**: Use a value between 1 and 100 (5-30 recommended).

## See Also

- [Plugin Architecture](../docs/_sad_plugin_architecture.md) - Plugin system documentation
- [MCP Configuration](../docs/mcp_configuration.md) - Full MCP config reference
- [JSON Schema Docs](https://json-schema.org/) - Official JSON Schema documentation
