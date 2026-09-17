# Request Logger Plugin

Example hooks-only plugin demonstrating the Agent System hook system.

## Overview

This plugin logs agent lifecycle events without providing any tools or web endpoints. It demonstrates:

- **Hooks-only plugin** type (`hooks_only` in `plugin.yaml`)
- **Multiple hook types**: pre_llm_call, post_llm_call, session_start, session_end
- **Hook ordering**: post_llm hook comes after pre_llm hook
- **Timing measurements**: Calculates LLM call duration
- **Session tracking**: Tracks session lifecycle and request counts
- **Metadata passing**: Shares data between hooks using context metadata

## Hook Declarations

This plugin registers 4 hooks:

1. **log_pre_llm** (pre_llm_call)
   - Logs incoming LLM requests
   - Tracks request count
   - Stores timing data in metadata
   - Order: after "begin"

2. **log_post_llm** (post_llm_call)
   - Logs LLM responses
   - Calculates request duration
   - Order: after "log_pre_llm", before "end"

3. **log_session_start** (session_start)
   - Logs session initialization
   - Initializes session tracking
   - Order: after "begin"

4. **log_session_end** (session_end)
   - Logs session termination with summary
   - Cleans up session data
   - Order: before "end"

## Configuration

Add to `config/plugins.yaml`:

```yaml
plugins:
  request_logger:
    enabled: true
    config: {}
```

## Hook Implementation

The plugin class inherits from both `ToolServer` (required for plugin infrastructure) and `PluginHook`:

```python
class RequestLoggerPlugin(ToolServer, PluginHook):
    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        # Log request, store timing data
        context.metadata['start_time'] = time.time()
        return HookResult(success=True, modified=True, context=context)
    
    async def on_post_llm_call(self, context: HookContext) -> HookResult:
        # Calculate duration, log response
        duration = time.time() - context.metadata['start_time']
        logger.info(f"LLM call took {duration:.2f}s")
        return HookResult(success=True, modified=False, context=context)
```

## Logged Information

### Pre-LLM Call
- Request number
- Session ID
- Agent name
- Message count
- Last message preview

### Post-LLM Call
- Request number
- Response duration (milliseconds)
- Response content preview

### Session Start
- Session ID
- Agent name
- Start timestamp

### Session End
- Session ID
- Session duration (seconds)
- Total request count

## Example Output

```
[RequestLogger] Session Started: session_id=abc123, agent=default
[RequestLogger] Request #1 - Pre-LLM Call: session=abc123, messages=2, agent=default
[RequestLogger] Last message: role=user, content=What is 2+2?...
[RequestLogger] Request #1 - Post-LLM Call: duration=234.56ms, response_preview=The answer is 4...
[RequestLogger] Session Ended: session_id=abc123, duration=12.34s, total_requests=1
```

## Use Cases

- **Debugging**: Track agent request/response flow
- **Performance monitoring**: Measure LLM call durations
- **Audit logging**: Record all agent interactions
- **Development**: Example for building custom hook plugins

## Testing

Run plugin tests:

```bash
.venv/Scripts/python.exe -m pytest tests/test_plugin_request_logger.py -v
```

## Extending

This plugin can be extended to:
- Store logs in database
- Send metrics to monitoring systems
- Filter/sanitize sensitive data
- Generate usage reports
- Integrate with external logging services

## Related

- [Plugin Hooks Documentation](../../docs/plugin_hooks.md)
- [Plugin Authoring Guide](../../docs/plugin_authoring.md)
- [Hook System Architecture](../../tmp/EPIC_0046_STATUS.md)
