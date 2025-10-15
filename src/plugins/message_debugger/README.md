# Message Debugger Plugin

Debug and inspect LLM conversation messages with detailed token analysis and history tracking.

## Overview

The Message Debugger plugin provides comprehensive debugging capabilities for LLM conversations in AgentSystem. It captures message snapshots before each LLM call, allowing you to inspect exactly what messages are being sent, including token counts, tool calls, and conversation flow.

## Features

- **Automatic Message Capture**: Hooks into pre_llm_call to capture messages before each LLM interaction
- **Token Analysis**: Detailed token count estimates for each message and total conversation
- **Tool Call Inspection**: View all tool calls with their arguments and results
- **Session Tracking**: Track messages across different sessions and agents
- **Web UI**: Interactive dashboard for browsing and filtering message snapshots
- **History Management**: Configurable history limits with automatic cleanup
- **Real-time Updates**: Optional auto-refresh for live debugging

## Configuration

The plugin is configured via `schema.yaml`:

```yaml
config:
  max_history_entries: 100           # Maximum snapshots to keep
  capture_enabled: true              # Enable/disable capturing
  include_tool_calls: true           # Include tool call details
  include_token_estimates: true      # Calculate token estimates
  auto_cleanup_threshold: 150        # Auto-cleanup when exceeded
```

## Usage

### Web UI

Access the Message Debugger panel in the main UI:

1. Navigate to the "Message Debugger" panel (🐛 icon)
2. View real-time statistics (snapshots, messages, tokens, sessions)
3. Filter by agent name, session ID, or limit results
4. Click on a snapshot to expand and view detailed messages
5. Use auto-refresh for live debugging
6. Clear history when needed

### API Endpoints

#### List Snapshots
```bash
GET /api/plugins/message-debugger/snapshots?agent_name=agent&limit=50
```

#### Get Specific Snapshot
```bash
GET /api/plugins/message-debugger/snapshots/{index}
```

#### Get Statistics
```bash
GET /api/plugins/message-debugger/stats
```

#### Clear History
```bash
DELETE /api/plugins/message-debugger/snapshots
```

## How It Works

1. **Hook Registration**: The plugin registers a `pre_llm_call` hook that runs after all other pre-processing hooks
2. **Message Capture**: Before each LLM call, it captures the current message state
3. **Token Estimation**: Uses `estimate_token_count` to calculate token usage
4. **Tool Analysis**: Extracts tool call information and arguments
5. **Storage**: Stores snapshots in memory with automatic cleanup
6. **Web Access**: Provides REST API and web panel for viewing captured data

## Message Details

Each captured snapshot includes:

- **Timestamp**: When the snapshot was taken
- **Agent Name**: Which agent made the request
- **Request ID**: Unique identifier for the request
- **Session ID**: Session identifier (if available)
- **Message Count**: Number of messages in the conversation
- **Total Tokens**: Estimated token count for all messages
- **Context Window**: LLM context window size

Each message includes:

- **Index**: Position in conversation
- **Role**: user, assistant, system, or tool
- **Content**: Message content (with preview limit)
- **Estimated Tokens**: Token count for this message
- **Tool Calls**: Detailed tool call information (if any)
- **Tool Result**: Flag indicating if this is a tool result

## Development

### Plugin Structure

```
message_debugger/
├── plugin.yaml              # Plugin metadata
├── schema.yaml              # Hook and config definitions
├── plugin.py                # Hybrid plugin factory
├── hooks.py                 # Hook implementations
├── web_endpoints.py         # REST API endpoints
├── templates/
│   └── panel.html          # Web UI panel
└── README.md               # This file
```

### Testing

Run the plugin tests:

```bash
pytest tests/test_plugin_message_debugger.py -v
```

## Best Practices

1. **History Management**: Set `max_history_entries` based on your debugging needs
2. **Performance**: Disable `include_token_estimates` for faster capture if token counts aren't needed
3. **Tool Debugging**: Enable `include_tool_calls` when debugging tool execution
4. **Session Tracking**: Use session IDs to trace multi-turn conversations
5. **Auto-Cleanup**: Set `auto_cleanup_threshold` higher than `max_history_entries` to prevent frequent cleanups

## Troubleshooting

### Snapshots Not Appearing

- Check that `capture_enabled: true` in schema.yaml
- Verify the plugin is loaded: Check `/api/plugins/message-debugger/stats`
- Ensure LLM requests are actually being made

### High Memory Usage

- Reduce `max_history_entries` to keep fewer snapshots
- Lower `auto_cleanup_threshold` for more aggressive cleanup
- Disable `include_token_estimates` to reduce per-message overhead

### Missing Tool Call Information

- Enable `include_tool_calls: true` in configuration
- Verify tool calls are actually present in messages

## Integration

The plugin integrates seamlessly with:

- **Hook System**: Runs as part of the pre_llm_call hook chain
- **Web UI**: Provides panel and REST API
- **Token Utils**: Uses standard token estimation utilities
- **Status System**: Logs capture events for monitoring

## License

Part of AgentSystem - see main project license.
