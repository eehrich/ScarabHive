# Plugin Validation Script

## Overview

The `validate_plugin.py` script validates plugin conformity by checking:

- **plugin.toml** structure and required fields
- **schema.yaml** structure and tool definitions
- File structure and required files
- Schema compliance with JSON schemas
- Cross-references between files
- Hook configuration (if applicable)
- Template variable usage

## Usage

### Validate a Single Plugin

```bash
# By plugin name
python src/scripts/validate_plugin.py --plugin basic_operations

# By path
python src/scripts/validate_plugin.py src/plugins/basic_operations
```

### Validate All Plugins

```bash
python src/scripts/validate_plugin.py --all
```

`--all` covers every plugin under `src/plugins`, `src/plugins_writer`,
`src/plugins_trading` and `src/plugins_llm`.

### Verbose Output

```bash
python src/scripts/validate_plugin.py --plugin basic_operations --verbose
```

### Extract Config Parameters from Code

Analyze plugin code and display found config parameters (read-only, does not modify files):

```bash
# Extract config for a single plugin
python src/scripts/validate_plugin.py --plugin basic_operations --extract-config

# Extract config for all plugins
python src/scripts/validate_plugin.py --all --extract-config
```

**Output Example:**
```
======================================================================
Extracted Config: file_ops
======================================================================

max_file_size: 10485760
allowed_extensions: ['.txt', '.md', '.json', '.py']
security:
  audit_log: true
  read_only_paths: []
```

### Merge Config into schema.yaml

Merge extracted config parameters into `schema.yaml` (adds missing keys only, preserves existing documentation):

```bash
# Merge config for a single plugin
python src/scripts/validate_plugin.py --plugin basic_operations --merge-config

# Merge config for all plugins
python src/scripts/validate_plugin.py --all --merge-config
```

**Notes:**
- Only adds missing config keys that exist in code but not in schema.yaml
- Preserves existing config values and documentation in schema.yaml
- Uses AST analysis to extract config from plugin code
- Supports nested config structures (e.g., `security.audit_log`)

## What It Checks

### File Structure
- ✓ Required files exist (plugin.toml)
- ✓ Entrypoint module exists (plugin.py or server.py); `library` and `llm-provider` plugins have no entrypoint
- ✓ schema.yaml exists (warning if missing)

### plugin.toml Validation
- ✓ Valid against JSON schema (`schemas/plugin-config.schema.json`)
- ✓ Required fields: `name`, `version`, `description`, `requires.agent_system`
- ✓ Valid plugin type (list of: `tool-server`, `web`, `hooks`, `library`, `llm-provider`, `custom`)
- ✓ Entrypoint format: `module:FACTORY`

### schema.yaml Validation
- ✓ Tools section structure for plugins
- ✓ Tool definitions with required fields
- ✓ Parameter schemas with type and description
- ✓ Hook definitions for hook plugins
- ✓ Web UI configuration for web/hybrid plugins
- ✓ Template variable usage ({{ name }}, etc.)

### Cross-Validation
- ✓ Plugin type matches schema content
- ✓ plugins have tools defined
- ✓ Hooks plugins have hooks defined
- ✓ Entrypoint module file exists

### Tool Validation
- ✓ Tool names are unique
- ✓ Tools have descriptions
- ✓ Parameters have proper types
- ✓ Required parameters are in properties
- ✓ `additionalProperties` is set (recommended: false)

### Hook Validation
- ✓ Hook names are unique
- ✓ Valid hook types (pre_llm_call, post_tool_call, etc.)
- ✓ Hook ordering (before/after directives)
- ✓ Hook timeout settings

### Config Extraction (--extract-config)
- ✓ AST-based code analysis to find config usage
- ✓ Detects nested config structures (e.g., `security.audit_log`)
- ✓ Identifies default values from code
- ✓ Supports both flat and hierarchical config
- ✓ Handles getattr() patterns with defaults

### Config Merging (--merge-config)
- ✓ Adds missing config keys to schema.yaml
- ✓ Preserves existing config and documentation
- ✓ Creates nested YAML structures from flat keys
- ✓ Proper YAML formatting (multi-line lists, quoted special chars)
- ✓ Backup-friendly (only writes if changes needed)

## Output

### Success
```
======================================================================
Validation Results: web_scraper
======================================================================

[OK] All checks passed!
```

### Errors
```
======================================================================
Validation Results: basic_operations
======================================================================

[X] ERRORS (1):
   * plugin.toml schema validation failed: 'requires' is a required property at []

[!] WARNINGS (1):
   * Factory name 'BasicOperationsServer' doesn't follow conventions

[X] Validation failed with errors
```

### Multiple Plugins Summary
```
======================================================================
SUMMARY
======================================================================

  [OK] PASS  web_scraper
  [X] FAIL   basic_operations
  [OK] PASS  duckduckgo_search
  ...

Total: 39 plugins
Passed: 10
Failed: 29
```

## Exit Codes

- **0**: All validations passed
- **1**: At least one validation failed

## Common Issues Found

### plugin.toml Issues
- Missing required fields
- Invalid plugin type
- Entrypoint module not found

### schema.yaml Issues
- Tools missing descriptions
- Parameters missing type or description
- Missing `additionalProperties` directive
- Invalid hook types
- Duplicate tool/hook names

### File Structure Issues
- Missing plugin.py or server.py
- Entrypoint mismatch (specified server:FACTORY but plugin.py is expected)

## Integration

### Pre-commit Hook
Add to `.git/hooks/pre-commit`:
```bash
#!/bin/bash
python src/scripts/validate_plugin.py --all
if [ $? -ne 0 ]; then
    echo "Plugin validation failed. Fix errors before committing."
    exit 1
fi
```

### CI/CD Pipeline
```yaml
- name: Validate Plugins
  run: python src/scripts/validate_plugin.py --all
```

### Make Target
```makefile
validate-plugins:
	python src/scripts/validate_plugin.py --all
```

## Development

### Testing the Script
```bash
# Test on a known-good plugin
python src/scripts/validate_plugin.py --plugin web_scraper

# Test on a plugin with issues
python src/scripts/validate_plugin.py --plugin basic_operations

# Test on all plugins
python src/scripts/validate_plugin.py --all
```

### Adding New Validations

1. Add validation method to `PluginValidator` class:
```python
def _validate_new_check(self) -> None:
    """Validate something new."""
    if some_condition:
        self.errors.append("Error message")
    if some_warning:
        self.warnings.append("Warning message")
```

2. Call it from `validate()` method:
```python
def validate(self) -> bool:
    # ... existing checks ...
    self._validate_new_check()
    return self._report_results()
```

## See Also

- [Plugin Authoring Guide](../../docs/plugin_authoring.md)
- [Plugin Architecture](../../docs/_arch_plugin_architecture.md)
- [Plugin System SRS](../../docs/_srs_pluginsystem.md)
- [JSON Schemas](../../schemas/)
