# Configuration File Merging

## Overview

The AgentSystem configuration supports splitting configuration across multiple YAML files using the `includes` mechanism. When multiple files define the same top-level key (e.g., `plugins`), they are **deep merged** instead of being overwritten.

## Deep Merge Behavior

- **Nested dictionaries**: Recursively merged (overlay values override base values)
- **Lists and primitives**: Replaced entirely by overlay value
- **Missing keys**: Added from overlay

## Example: Agent Configuration Files

### Main config.yaml
```yaml
includes:
  - plugins.yaml
  - agents/meta_agent.yaml
  - agents/sysadmin_agent.yaml
```

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

## Implementation

See `src/agent_system/config/settings.py`:
- `deep_merge()` function handles recursive dictionary merging
- Applied specifically to `plugins` key to allow multiple files to contribute agents

## Testing

```python
from agent_system.config.settings import load_settings

config = load_settings()
plugins = config.plugins.servers

# Verify agents from different files are all present
assert 'basic_agent' in plugins      # From plugins.yaml
assert 'meta_agent' in plugins       # From agents/meta_agent.yaml
assert 'sysadmin_agent' in plugins   # From agents/sysadmin_agent.yaml
```
