# Message Validator Plugin

**Plugin Type:** Hook-only (inherits only from `PluginHook`)

A reference implementation of a hooks-only plugin that validates and sanitizes messages before LLM calls to ensure safety, proper formatting, and compliance with content policies.

## Features

- **Structure Validation**: Ensures messages have required fields (role, content)
- **Role Validation**: Checks message roles are valid and recognized
- **Content Sanitization**: Removes potentially harmful content (XSS, script injection)
- **Length Limits**: Enforces maximum message length
- **Sequence Validation**: Optionally enforces alternating user/assistant pattern
- **Auto-fixing**: Can auto-fix issues in non-strict mode
- **Validation Metrics**: Provides detailed statistics about validation performed

## Configuration

Configure in `config/plugins.yaml`:

```yaml
message_validator:
  enabled: true
  config:
    strict_mode: false              # Reject invalid vs auto-fix
    max_message_length: 100000      # Maximum allowed message length
    allow_empty_messages: false     # Allow messages with empty content
    sanitize_content: true          # Remove potentially harmful content
    valid_roles: ['system', 'user', 'assistant', 'function', 'tool']
    enforce_alternating: false      # Require alternating user/assistant
```

## Hook Points

### PRE_LLM_CALL

Validates and sanitizes messages before sending to the LLM.

**Input Context:**
- `messages`: List of conversation messages

**Output Result:**
- `messages`: Validated and sanitized message list
- `metadata.validation`: Validation statistics including:
  - `status`: 'passed', 'passed_with_fixes', or 'failed'
  - `total_messages`: Number of input messages
  - `validated_messages`: Number of output messages
  - `messages_fixed`: Count of auto-fixed messages
  - `issues_found`: Count of validation issues
  - `issues`: List of specific issues found

## Validation Rules

### Required Fields
- `role`: Must be present and valid
- `content`: Must be present (string or list for multimodal)

### Role Validation
- Default valid roles: `system`, `user`, `assistant`, `function`, `tool`
- Custom roles can be configured via `valid_roles`
- Invalid roles are auto-fixed to `user` in non-strict mode

### Content Validation
- **Empty Content**: Rejected unless `allow_empty_messages` is true
- **Length Limits**: Truncated if exceeding `max_message_length`
- **Type Checking**: Must be string or list (multimodal)

### Content Sanitization
Removes potentially harmful patterns:
- `<script>` tags and content
- `javascript:` protocol
- Event handlers (`onclick`, `onerror`, etc.)

### Sequence Validation
If `enforce_alternating` is true:
- User and assistant messages must alternate
- System messages are ignored in alternation check
- Non-alternating messages are flagged as issues

## Operating Modes

### Strict Mode (`strict_mode: true`)
- Validation failures abort the LLM call
- Returns error with detailed validation issues
- No auto-fixing attempted

### Non-Strict Mode (`strict_mode: false`, default)
- Auto-fixes issues when possible
- Skips invalid messages that cannot be fixed
- Returns success with issues logged in metadata

## Example Usage

The plugin is automatically loaded and executed before each LLM call if enabled:

```python
# No code changes needed - hooks are called automatically
# The agent will log validation stats:
# "Messages validated: 8 messages, 2 issues auto-fixed"
```

## Architecture

This plugin demonstrates the **hook-only** pattern:

```python
class MessageValidatorPlugin(PluginHook):
    """Hook-only plugin - does NOT inherit from MCPServer"""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # Validation logic
        pass
```

**Why hook-only?**
- This plugin observes and modifies the LLM call lifecycle
- It does NOT provide callable tools
- Therefore it only inherits from `PluginHook`, not `MCPServer`

See `docs/plugin_architecture.md` for more details on plugin types.

## Testing

Run tests for this plugin:

```bash
pytest tests/test_message_validator_plugin.py -v
```

## Performance

- **Overhead**: <2ms per LLM call for typical conversations
- **Regex Operations**: Minimal - only runs on string content when sanitization is enabled
- **Memory**: Minimal - operates on message copies, no persistent state

## Security Considerations

- **XSS Prevention**: Removes common XSS patterns
- **Injection Prevention**: Blocks JavaScript protocol and event handlers
- **Length Protection**: Prevents denial-of-service via oversized messages

**Note**: This is a reference implementation. For production use, consider:
- More comprehensive pattern matching
- Integration with dedicated security libraries
- Configurable sanitization rules per deployment

## See Also

- `docs/plugin_architecture.md` - Plugin type selection guide
- `src/plugins/context_optimizer/` - Another reference hook plugin
- `src/plugins/request_logger/` - Simple logging hook example
