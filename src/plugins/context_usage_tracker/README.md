# Context Usage Tracker Plugin

Automatically tracks LLM context usage and token consumption.

## Features

- **Automatic Tracking**: Implements `post_llm_call` hook to capture usage after every LLM call
- **Comprehensive Data**: Records total tokens, prompt tokens, completion tokens, message count
- **Session-Aware**: Associates usage with specific agents and sessions
- **Debug Integration**: Powers the Context Usage Debug panel in the web UI

## Hook Details

- **Type**: `POST_LLM_CALL`
- **Priority**: 100
- **Enabled**: Yes (by default)

## Data Captured

From each LLM response:
- `total_tokens`: Total tokens used (prompt + completion)
- `prompt_tokens`: Tokens in the input prompt
- `completion_tokens`: Tokens in the generated response
- `message_count`: Number of messages in conversation
- `context_window`: Agent's configured context window size

## Usage

The plugin is automatically loaded when enabled in `config/plugins.yaml`. No manual configuration required.

### Viewing Usage Data

1. Open web UI
2. Click "Context Usage" button
3. View real-time token consumption statistics
4. See historical usage patterns in charts

## Implementation

Uses `record_context_usage()` from `agent_system.context.tracker` to store data points. Each LLM call creates a new usage record with:

- Agent identification
- Session tracking
- Token breakdown
- Timestamp
- Context window utilization percentage

**Context Window Detection**: The plugin intelligently detects the actual context window being used:

1. **Primary source**: Uses `context.llm.context_window` from the actual LLM instance in use (respects `llm_override` from WebUI profile selection)
2. **Fallback**: If LLM object doesn't have context_window, resolves from `agent_config.llm_profile` (agent's configured default profile)

This ensures accurate tracking when users override the LLM profile via WebUI (e.g., selecting "small" profile with 20k context while agent default is "normal" with 100k).

## Error Handling

- Gracefully handles missing usage data
- Logs errors but doesn't fail requests
- Continues execution even if tracking fails
