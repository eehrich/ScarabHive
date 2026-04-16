# Simple Prompt Inject Plugin

Minimal hooks-only plugin that injects configurable text into conversations before every LLM call.

## Overview

Use this plugin to add persistent reminders, instructions, or context to LLM conversations without editing agent system prompts. The injected message is automatically replaced on each call (no duplicates).

Prompts can be defined inline (`prompt_text`) or loaded from a Markdown file (`prompt_file`). Both support **Jinja2 template rendering** with the agent's `template_vars`.

**Plugin Type:** Hook-only (`SchemaBasedPluginHook`)
**Hook:** `inject_prompt` (pre_llm_call)
**Default:** Disabled

## Configuration

Add to `config/plugins.yaml`:

```yaml
plugins:
  servers:
    simple_prompt_inject:
      type: simple_prompt_inject
      enabled: true
      config:
        prompt_text: "Remember: always respond in {{ lang }}."
        injection_position: "before_last_user"  # or "end"
        role: "system"                           # or "user"
```

### Using a Markdown file

```yaml
plugins:
  servers:
    simple_prompt_inject:
      type: simple_prompt_inject
      enabled: true
      config:
        prompt_file: "prompts/my_instructions.md"  # relative to config/
        injection_position: "before_last_user"
        role: "system"
```

The file path is resolved relative to `config/` or can be absolute. When `prompt_file` is set, it takes precedence over `prompt_text`.

### Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `prompt_text` | string | `""` | Text to inject. Empty = no-op. Supports Jinja2. |
| `prompt_file` | string | `""` | Path to `.md` file (relative to `config/` or absolute). Takes precedence over `prompt_text`. |
| `injection_position` | string | `"before_last_user"` | `before_last_user` inserts before the last user message; `end` appends. |
| `role` | string | `"system"` | Role of the injected message (`system` or `user`). |

### Jinja2 Template Variables

Both `prompt_text` and `prompt_file` content are rendered with the agent's `template_vars`. Variables come from (in priority order):

1. **Session-scoped** template_vars (set at runtime, e.g. by tools)
2. **Agent config** `template_vars` (static, from agent YAML)

Example agent config:

```yaml
my_agent:
  agent_config:
    template_vars:
      user_name: "Alice"
      lang: "German"
```

Example prompt (inline or in a `.md` file):

```
Hello {{ user_name }}, please respond in {{ lang }}.
```

If no `template_vars` are available, the template is injected as-is (no rendering). Invalid Jinja2 syntax gracefully falls back to the raw text.

### Per-Agent Override

Disable or enable the hook for specific agents in their agent config:

```yaml
my_agent:
  agent_config:
    hooks:
      overrides:
        simple_prompt_inject.inject_prompt:
          enabled: false
```

## How It Works

1. On each `pre_llm_call`, the plugin checks if a prompt is configured (text or file).
2. It removes any previously injected message (identified via `injected_by` field).
3. It renders the prompt template with Jinja2 using the agent's `template_vars`.
4. It creates a new `ChatMessage` with the configured role and rendered text.
5. It inserts the message at the configured position.

This ensures exactly **one** injected message is present regardless of how many LLM calls occur.

## Files

| File | Purpose |
|------|---------|
| `hooks.py` | `SimplePromptInjectPlugin` – hook implementation |
| `plugin.py` | `PLUGIN_FACTORY` – entry point |
| `schema.yaml` | Hook definition + config schema |
| `plugin.yaml` | Plugin metadata |
