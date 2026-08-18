# Message Validator Plugin

**Plugin Type:** Hook-only (inherits only from `PluginHook`)

Comprehensive message validation and repair before LLM calls to ensure OpenAI API compliance and proper message formatting.

## Features

- **Tool Call Consistency**: Detects and repairs orphaned tool calls and missing tool responses
- **Tool Name Validation**: Ensures tool names comply with OpenAI pattern (^[a-zA-Z0-9_-]+$)
- **Content Structure Validation**: Checks for malformed or problematic content structures
- **Message Sequence Validation**: Detects problematic patterns like consecutive assistant messages
- **Cascading Removal**: When removing assistant messages with tool_calls, also removes corresponding tool responses
- **Auto-fixing**: Can auto-fix issues in non-strict mode
- **Validation Metrics**: Provides detailed statistics about validation performed

## Configuration

Configure in `config/plugins.yaml`:

```yaml
message_validator:
  enabled: true
  config:
    log_level: "warning"             # Logging level for validation issues
    strict_mode: false               # Reject invalid vs auto-fix
    max_tool_response_size_kb: 50    # Tool response size that is an error
    warn_tool_response_size_kb: 20   # Tool response size that warns
```


> **Removed knobs.** `max_message_length`, `sanitize_content`,
> `enforce_alternating`, `allowed_roles` and `validation_level` used to be
> declared in `schema.yaml` and documented here, but no line of this plugin
> ever read them. A knob an operator can set and that does nothing is worse
> than an absent one — it is silently ignored configuration. They were dropped
> rather than implemented, because nobody asked for the behaviour.

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

### What is actually checked

Every entry below corresponds to a check in `hooks.py`; nothing here is
aspirational.

**Tool-call integrity** — the class of defect that makes a provider reject the
whole request:
- a tool response whose `tool_call_id` matches no assistant tool call
- a tool message without a `tool_call_id`
- an assistant tool call with no corresponding tool response
- a tool name that violates the OpenAI pattern `^[a-zA-Z0-9_-]+$`

**Tool response shape:**
- malformed JSON in a tool response
- size above `max_tool_response_size_kb` (error) or `warn_tool_response_size_kb` (warning)

**Conversation shape:**
- an assistant message with neither content nor tool calls
- a first non-system message that is not `user`
- two consecutive assistant messages

It does NOT truncate, sanitize content, rewrite roles, or enforce a strict
alternation. Earlier revisions of this file described all four; none of them
existed in the code.

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
