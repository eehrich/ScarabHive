# Simple Prompt Inject Plugin

Minimal hooks-only plugin that injects configurable text into conversations before every LLM call.

## Overview

Use this plugin to add persistent reminders, instructions, or context to LLM conversations without editing agent system prompts. The injected message is automatically replaced on each call (no duplicates).

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
        prompt_text: "Remember: always respond in German."
        injection_position: "before_last_user"  # or "end"
        role: "system"                           # or "user"
```

### Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `prompt_text` | string | `""` | Text to inject. Empty = no-op. |
| `injection_position` | string | `"before_last_user"` | `before_last_user` inserts before the last user message; `end` appends. |
| `role` | string | `"system"` | Role of the injected message (`system` or `user`). |

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

1. On each `pre_llm_call`, the plugin checks if `prompt_text` is non-empty.
2. It removes any previously injected message (identified via `injected_by` field).
3. It creates a new `ChatMessage` with the configured role and text.
4. It inserts the message at the configured position.

This ensures exactly **one** injected message is present regardless of how many LLM calls occur.

## Files

| File | Purpose |
|------|---------|
| `hooks.py` | `SimplePromptInjectPlugin` – hook implementation |
| `plugin.py` | `PLUGIN_FACTORY` – entry point |
| `schema.yaml` | Hook definition + config schema |
| `plugin.yaml` | Plugin metadata |
