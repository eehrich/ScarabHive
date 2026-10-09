# Configuration File Merging

## Overview

The AgentSystem configuration supports splitting configuration across multiple YAML files using the `includes` mechanism. When multiple files define the same top-level key (e.g., `plugins`), they are **deep merged** instead of being overwritten.

**NEW**: Wildcard patterns are supported in includes! Use `agents/*.yaml` to include all agent files.

## Deep Merge Behavior

- **Nested dictionaries**: Recursively merged (overlay values override base values)
- **Lists and primitives**: Replaced entirely by overlay value
- **Missing keys**: Added from overlay

## Example: Agent Configuration Files

### Main config.yaml
```yaml
includes:
  - llm.yaml
  - plugins.yaml
  - mcp_servers.yaml
  - agents/*.yaml         # Wildcard: includes ALL .yaml files in agents/ directory
```

**Wildcard Patterns Supported:**
- `*.yaml` - All YAML files in current directory
- `agents/*.yaml` - All YAML files in agents/ subdirectory
- `agents/**/*.yaml` - All YAML files exactly one subdirectory below agents/ (`**` is not recursive)

**Note**: Wildcard matches are sorted alphabetically to ensure consistent load order.

### plugins.yaml
```yaml
plugins:
  servers:
    basic_agent:
      type: basic_agent
      enabled: true
    sub_agent_manager:
      type: sub_agent_manager
      enabled: true
```

### agents/meta_agent.yaml
```yaml
plugins:
  servers:
    meta_agent:
      type: basic_agent
      enabled: true
      agent_config:
        max_steps: 50
        llm_profile: normal
```

### Result After Merge
```yaml
plugins:
  servers:
    basic_agent:       # From plugins.yaml
      type: basic_agent
      enabled: true
    sub_agent_manager: # From plugins.yaml
      type: sub_agent_manager
      enabled: true
    meta_agent:        # From agents/meta_agent.yaml
      type: basic_agent
      enabled: true
      agent_config:
        max_steps: 50
        llm_profile: normal
```

## Structure Requirements

**IMPORTANT**: Agent configuration files MUST follow the same nested structure as `plugins.yaml`:

✅ **Correct Structure**:
```yaml
plugins:
  servers:     # <-- REQUIRED nesting level
    my_agent:
      type: basic_agent
      enabled: true
```

❌ **Wrong Structure** (will not merge correctly):
```yaml
plugins:
  my_agent:    # <-- Missing 'servers' level
    type: basic_agent
    enabled: true
```

## Benefits

1. **Modular Configuration**: Each agent can have its own configuration file
2. **No Overwrites**: Multiple files can contribute to the same configuration section
3. **Clean Separation**: Keep `plugins.yaml` for system plugins and separate files for agents
4. **Maintainability**: Easier to manage when each agent has 50+ lines of config
5. **Wildcard Support**: Automatically include all agent files without listing each one
6. **Consistent Load Order**: Wildcard matches sorted alphabetically for predictability

## Wildcard Include Examples

```yaml
# Include all agent files from agents/ directory
includes:
  - agents/*.yaml

# Include specific subdirectories
includes:
  - agents/production/*.yaml
  - agents/experimental/*.yaml

# Mix explicit and wildcard includes
includes:
  - llm.yaml
  - plugins.yaml
  - agents/*.yaml
  - custom/special_agent.yaml
```

**Wildcard Processing:**
1. Patterns are resolved relative to the config file directory
2. Matches are sorted alphabetically for consistent load order
3. If a pattern matches no files, it's silently ignored
4. Standard glob patterns supported: `*`, `?`, `[abc]` (`**` is not recursive: it matches like `*`, one directory level)

## Implementation

`load_settings()` in `src/agent_system/config/settings.py` runs the load; the files are read and merged by
the modules beside it:
- `layers.py` (`read_layers()`) reads the master config, its includes and the local layer, and decides which
  file may set what
- `merging.py`: the `deep_merge()` function handles recursive dictionary merging
- Wildcard expansion using Python's `glob` module (`layers._expand_includes()`)
- Applied to every top-level section an include sets (`plugins`, `llm_system`, `hooks`, ...), except `external_servers`, which the last include replaces; `paths`, `auth` (apart from route rules), `includes` and `files` are read from the master config only

## Testing

```python
from agent_system.config.settings import load_settings

config = load_settings()
plugins = config.plugins.servers

# Verify agents from different files are all present
assert 'sub_agent_manager' in plugins  # From plugins.yaml
assert 'okf_agent' in plugins        # From agents/okf_agent.yaml
assert 'sysadmin_agent' in plugins   # From agents/sysadmin_agent.yaml
```
