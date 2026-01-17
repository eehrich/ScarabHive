# Context Optimizer Plugin

**Plugin Type:** Hook-only (inherits only from `PluginHook`)

A reference implementation of a hooks-only plugin that optimizes conversation context before LLM calls to improve token efficiency and response quality.

## Features

- **Duplicate Removal**: Removes consecutive duplicate messages
- **Message Truncation**: Truncates overly long messages while preserving meaning
- **Token Management**: Ensures context stays within configurable token limits
- **Smart Preservation**: Always preserves system messages and recent user messages
- **Optimization Metrics**: Provides detailed statistics about optimization performed

## Configuration

Configure in `config/plugins.yaml`:

```yaml
context_optimizer:
  enabled: true
  config:
    # Context window management
    max_context_percentage: 0.80    # Use up to 80% of LLM's context window
    
    # Message size limits (token-based for better accuracy)
    max_message_length_tokens: 8000       # Soft limit - triggers smart truncation
    hard_max_message_length_tokens: 16000 # Hard limit - force-truncate if exceeded
    
    # Smart JSON truncation for tool responses
    max_json_string_length: 50000   # Max chars per string within JSON
    keep_string_end: true           # Keep end of strings (newest data)
    
    # Message preservation rules
    preserve_system_messages: true  # Always preserve system messages
    preserve_last_n_messages: 7     # Always keep last N messages
    
    # Remove consecutive duplicate messages
    remove_duplicates: true
```

**Why Token-based?**
The plugin uses token-based limits for accurate estimation:
- Base64/binary content (audio, images) compresses efficiently in tokens
- JSON and code have predictable token-to-char ratios
- Matches actual LLM processing (LLMs work with tokens, not characters)

## Hook Points

### PRE_LLM_CALL

Optimizes the conversation context before sending to the LLM.

**Input Context:**
- `messages`: List of conversation messages

**Output Result:**
- `messages`: Optimized message list
- `metadata.optimization`: Optimization statistics including:
  - `original_message_count`: Number of messages before optimization
  - `optimized_message_count`: Number of messages after optimization
  - `messages_removed`: Count of removed messages
  - `bytes_saved`: Estimated bytes saved
  - `reduction_percentage`: Percentage reduction in context size

## How It Works

1. **Duplicate Removal**: Scans for consecutive messages with identical role and content
2. **Smart Truncation**: 
   - Estimates message tokens using `estimate_content_tokens()`
   - For JSON tool responses: intelligently truncates long strings while preserving structure
   - For other messages: simple truncation if exceeding token limits
   - **Base64/Binary Content**: No warnings for large base64 content (normal for audio/images)
3. **Token Limiting**: Estimates total tokens and removes oldest non-system messages if exceeding context window percentage

**Preservation Priority:**
1. System messages (if `preserve_system_messages` is true)
2. Most recent N messages (based on `preserve_recent_count`)
3. Older messages (added until token limit is reached)

## Example Usage

The plugin is automatically loaded and executed before each LLM call if enabled:

```python
# No code changes needed - hooks are called automatically
# The agent will log optimization stats:
# "Context optimized: 15 -> 12 messages, saved 2.5KB (18.3% reduction)"
```

## Architecture

This plugin demonstrates the **hook-only** pattern:

```python
class ContextOptimizerPlugin(PluginHook):
    """Hook-only plugin - does NOT inherit from MCPServer"""
    
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # Optimization logic
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
pytest tests/test_context_optimizer_plugin.py -v
```

## Performance

- **Overhead**: <5ms per LLM call for typical conversations
- **Token Savings**: 10-40% reduction in typical multi-turn conversations
- **Memory**: Minimal - operates on message copies, no persistent state

## See Also

- `docs/plugin_architecture.md` - Plugin type selection guide
- `src/plugins/message_validator/` - Another reference hook plugin
- `src/plugins/request_logger/` - Simple logging hook example
