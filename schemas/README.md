# JSON Schemas for AgentSystem Configuration

This directory contains JSON Schema definitions for validating AgentSystem configuration files.

## Files

### Configuration Schemas
- **`config-agents.schema.json`**: Schema for `config_agents` section in `config/mcp.yaml` (configuration-based agents)
- **`llm-config.schema.json`**: Schema for `config/llm.yaml` (LLM models and providers)
- **`mcp-config.schema.json`**: Schema for `config/config.yaml` (main system configuration)

### Validation Scripts
- **`../scripts/validate_config_agents_schema.py`**: Python validation script for config-based agents

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

1. Open any config file (`config/llm.yaml`, `config/config.yaml`, `config/mcp.yaml`)
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

### Config-Agents Schema (`config-agents.schema.json`)

Validates configuration-based agents (Epic 0043):

- **Agent names**: Lowercase, alphanumeric + underscores, 3-50 characters
- **Required fields**: `enabled`, `description`, `base_type`, `agent_config`
- **Agent config**: LLM profile, max steps, system prompt/template, tools, context management
- **Tool patterns**: Format `plugin_name/tool_name` or `plugin_name/*`
- **LLM profiles**: Must match profiles in `config/llm.yaml`
- **Context strategies**: Valid strategy names
- **Metadata**: Optional author, version, tags, category

### LLM Config Schema (`llm-config.schema.json`)

Validates LLM configuration:

- **httpx_timeouts**: Connection, read, write, pool timeouts
- **models**: Model definitions with provider, API keys, capabilities
- **capabilities**: Tools, streaming, vision, audio, JSON mode support
- **context_window**: Token limits (1 - 2,000,000)

### Main Config Schema (`mcp-config.schema.json`)

Validates main system configuration:

- **name, version, description**: System metadata
- **includes**: Config file includes
- **context**: Auto-datetime, timezone, location
- **network**: SSL, host, port, cache settings
- **default_agent**: Default agent name
- **auth**: Authentication, CORS, rate limiting, admin user
- **logging**: Log levels, file paths, cancellation settings

## Validation

### Using the Python Script

```bash
# Validate default config (config/mcp.yaml)
python scripts/validate_config_agents_schema.py

# Validate specific file
python scripts/validate_config_agents_schema.py --config path/to/mcp.yaml

# Verbose output with details
python scripts/validate_config_agents_schema.py --verbose

# Strict mode (warnings become errors)
python scripts/validate_config_agents_schema.py --strict
```

### Using jsonschema CLI (if installed)

```bash
# Install jsonschema CLI
pip install check-jsonschema

# Validate YAML against schema
check-jsonschema --schemafile schemas/config-agents.schema.json config/mcp.yaml
```

### Using VS Code

Add to `.vscode/settings.json`:

```json
{
  "yaml.schemas": {
    "./schemas/config-agents.schema.json": "config/mcp.yaml"
  }
}
```

This enables:
- Real-time validation as you type
- Auto-completion for fields
- Inline documentation tooltips

## Example Valid Configuration

```yaml
config_agents:
  my_analyst:
    enabled: true
    description: "Financial analyst for market research and stock analysis"
    base_type: agent
    
    agent_config:
      llm_profile: turbo
      max_steps: 20
      system_template: "config/prompts/analyst_prompt.yaml"
      
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
        blocked:
          - "ssh_control/*"
      
      context_management:
        enabled: true
        strategy: "SUMMARIZE_OLDEST"
        preserve_recent_messages: 8
    
    metadata:
      author: "Team Name"
      version: "1.0.0"
      tags: ["finance", "analysis"]
      category: "financial"
```

## Schema Constraints

### Agent Name Pattern

```regex
^[a-z][a-z0-9_]*$
```

- Must start with lowercase letter
- Only lowercase letters, digits, underscores
- 3-50 characters

### Tool Pattern

```regex
^[a-z_][a-z0-9_]*/([a-z_][a-z0-9_]*|\*)$
```

Examples:
- `yahoo_finance/*` - All tools from yahoo_finance plugin
- `web_scraper/scrape_url` - Specific tool
- `basic_operations/wait_for` - Another specific tool

### Base Type

Must be one of:
- `agent` (default)
- `server`

### Context Strategies

Valid strategies:
- `SUMMARIZE_OLDEST` (recommended)
- `TRUNCATE`
- `TRUNCATE_OLDEST`
- `SLIDING_WINDOW`
- `SMART_COMPRESSION`

### Max Steps Range

- Minimum: 1
- Maximum: 100
- Recommended: 5-30

### Metadata Categories

Pre-defined categories:
- `financial`
- `development`
- `research`
- `support`
- `general`
- `analysis`
- `automation`
- `communication`

Custom categories are allowed via `additionalProperties: true`.

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

## CI/CD Integration

### GitHub Actions

```yaml
name: Validate Config Agents Schema

on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install jsonschema PyYAML
      
      - name: Validate schema
        run: |
          python scripts/validate_config_agents_schema.py --strict
```

### Pre-commit Hook

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: validate-config-agents
        name: Validate config agents schema
        entry: python scripts/validate_config_agents_schema.py
        language: system
        files: ^config/mcp\.yaml$
        pass_filenames: false
```

## Schema Development

### Testing Schema Changes

```bash
# Test with verbose output
python scripts/validate_config_agents_schema.py --verbose

# Test with intentionally invalid config
python scripts/validate_config_agents_schema.py --config tests/fixtures/invalid_config.yaml

# Expect failure (for testing)
python scripts/validate_config_agents_schema.py --config tests/fixtures/invalid_config.yaml && echo "Should have failed!"
```

### Schema Versioning

The schema uses Draft 7 of JSON Schema:
- `$schema`: http://json-schema.org/draft-07/schema#
- `$id`: https://github.com/yourusername/AgentSystem/schemas/config-agents.json

Update `$id` when publishing schema to a public URL.

## See Also

- [Configuration-Based Agents Guide](../docs/config_based_agents.md) - Complete user documentation
- [MCP Configuration](../docs/mcp_configuration.md) - Full MCP config reference
- [JSON Schema Docs](https://json-schema.org/) - Official JSON Schema documentation
- [Epic 0043](../backlog.md) - Configuration-Based Agents epic

## Support

For issues or questions:
1. Validate your config: `python scripts/validate_config_agents_schema.py --verbose`
2. Check error messages for specific issues
3. Review examples in `config/mcp.yaml`
4. See [troubleshooting guide](../docs/config_based_agents.md#troubleshooting)
