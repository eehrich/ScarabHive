# Context Summarizer Plugin

**Type:** Triple Hybrid Plugin (Schema-based Hooks + MCP Server + Web UI)
**Hook Type:** `pre_llm_call`
**MCP Tools:** `context_summarizer_summarize`, `context_summarizer_check_stats`
**Pattern:** SchemaBasedPluginHook + SchemaBasedMCPServer + Web UI Panel

## Overview

The Context Summarizer Plugin intelligently reduces conversation context size by using an LLM to create concise summaries of older messages. It provides both **automatic** (via hooks) and **manual** (via MCP tools) summarization capabilities.

**Automatic Mode (Hook):** Triggers automatically when context exceeds threshold
**Manual Mode (MCP Tools):** LLM can call tools to manually summarize when needed
**Web UI Feature:** Beautiful web panel to view summarization history and statistics

## Features

- **Automatic Summarization (Hook)**: Activates when context exceeds configurable percentage of LLM window
- **Manual Summarization (MCP Tool)**: LLM can trigger summarization via tool call
- **Context Statistics (MCP Tool)**: LLM can check token usage and get recommendations
- **Intelligent LLM-based Summarization**: Uses configured LLM to create concise, accurate summaries
- **Smart Message Categorization**: Preserves system messages and recent messages while summarizing older content
- **Tool Call Preservation**: Ensures `assistant` messages with `tool_calls` stay paired with their `tool` responses
- **Chunked Processing**: Processes messages in configurable batches for optimal summarization
- **Quality Guarantees**: Only applies summaries that meet minimum reduction thresholds
- **Metadata Storage**: Optionally stores original messages for audit trails
- **Web UI Integration**: Provides visual panel for monitoring summarization history and statistics

## MCP Tools (New!)

### `context_summarizer_summarize`

**Purpose:** Allows LLM to manually trigger context summarization when it detects the conversation history is becoming too long or contains irrelevant information.

**When to Use:**
- Conversation is getting very long
- Old information is no longer needed for current task
- Want to reduce token usage proactively
- Switching topics and old context is irrelevant

**Parameters:**
```json
{
  "reason": "optional description for logging",
  "chunk_size": 10,  // optional override
  "preserve_recent": 10  // optional override
}
```

**Returns:**
```json
{
  "status": "success",
  "original_count": 50,
  "summarized_count": 15,
  "tokens_saved": 45000,
  "summary_preview": "[Summary of 35 messages...]",
  "modified": true
}
```

**Example Usage (LLM perspective):**
```
// When LLM notices context is getting cluttered
context_summarizer_summarize({
  "reason": "Finished planning phase, moving to implementation"
})

// Result: Old planning discussion compressed, recent messages preserved
```

### `context_summarizer_check_stats`

**Purpose:** Check current conversation statistics to decide if manual summarization would be beneficial.

**Parameters:** None (empty object)

**Returns:**
```json
{
  "status": "success",
  "message_count": 85,
  "total_tokens": 75000,
  "context_window": 100000,
  "utilization_percentage": 75.0,
  "trigger_threshold": 60.0,
  "recommendation": "summarize"  // or "ok"
}
```

**Example Usage (LLM perspective):**
```
// Before deciding if summarization is needed
context_summarizer_check_stats({})

// If utilization_percentage >= trigger_threshold, consider calling summarize
```

## Usage Patterns

### Pattern 1: Automatic Only (Default)

Enable the hook, let it automatically trigger:

```yaml
hooks:
  enabled: true
  overrides:
    context_summarizer.summarize_context:
      enabled: true
```

### Pattern 2: Manual Control by LLM

Enable MCP tools, let LLM decide when to summarize:

```yaml
agent_config:
  tools:
    allowed:
      - "context_summarizer/*"
```

**LLM can then:**
1. Check stats periodically: `context_summarizer_check_stats({})`
2. Decide based on utilization: if > 70%, summarize
3. Trigger manually: `context_summarizer_summarize({"reason": "switching topics"})`

### Pattern 3: Hybrid (Automatic + Manual)

Enable both hook and tools:

```yaml
hooks:
  enabled: true
  overrides:
    context_summarizer.summarize_context:
      enabled: true

agent_config:
  tools:
    allowed:
      - "context_summarizer/*"
```

**Benefits:**
- Automatic failsafe if context gets too large
- LLM can proactively summarize before hitting threshold
- LLM can summarize for semantic reasons (topic change) not just token limits

## Status Messages

The plugin publishes real-time status messages during summarization via the status bus:

- **START**: Announces beginning of summarization with token count and target reduction
- **PROGRESS**: Reports number of messages being summarized and preserved
- **END**: Shows completion statistics (messages reduced, tokens saved, reduction ratio)

Status messages include detailed metadata and can be monitored via the `/events` SSE endpoint.

Example status messages:
```
START: Starting context summarization: 85000 tokens → target reduction 30%
PROGRESS: Summarizing 120 older messages using LLM (preserving 10 recent messages)
END: Summarization complete: 130 → 20 messages, 62000 tokens saved (73% reduction)
```

## Web UI

Access the summarization history panel at `/plugins` in your browser when the agent system is running.

### Panel Features

- **Event List**: See all recent summarization events with timestamps
- **Statistics Dashboard**: View total tokens saved, events count, and average reduction ratio
- **Message Comparison**: Click any event to see before/after messages
- **Session Tracking**: Each event shows session ID and request ID
- **Strategy Display**: Shows "LLM Summarization" strategy used
- **Summary Stats**: View number of summaries created per event

The panel automatically refreshes every 30 seconds and shows the last 100 events.

## Configuration

Configuration is defined in `schema.yaml`. All values have sensible defaults.

### Key Configuration Options

```yaml
config:
  summarization_trigger_tokens: 50000
    # Start summarization when context exceeds this token count

  summarization_chunk_size: 10
    # Number of older messages to summarize in one batch

  preserve_recent_count: 10
    # Always preserve the last N messages without summarization

  llm_profile: "fast"
    # LLM profile to use for summarization (fast, normal, advanced)

  min_summary_reduction: 0.3
    # Minimum reduction ratio (30%) to accept summary

  max_tracked_sessions: 200
    # Max sessions tracked for summarization timestamps (LRU eviction)

  summary_prompt_template: |
    Summarize the following conversation messages concisely...
    # Customizable prompt template for summarization
```

## How It Works

1. **Threshold Check**: Estimates token count and compares to trigger threshold
2. **Message Categorization**: Separates messages into:
   - System messages (always preserved)
   - Recent messages (preserved based on `preserve_recent_count`)
   - Old messages (candidates for summarization)
3. **Chunked Summarization**: Processes old messages in chunks
4. **LLM Summarization**: Calls configured LLM to create concise summaries
5. **Quality Check**: Validates that summary achieves minimum reduction
6. **Reconstruction**: Builds new message list: `system + summaries + recent`

## Usage

### Basic Setup

The plugin is automatically discovered if placed in `src/plugins/context_summarizer/`.

### Hook Ordering

The plugin is configured to run **after** `context_engineer`:

```yaml
hooks:
  - name: summarize_context
    type: pre_llm_call
    order:
      after: ["context_engineering"]
```

context_engineer externalizes and archives first; summarization runs only if the context is still too large.

### Global Configuration

Override settings in `config/plugins.yaml`:

```yaml
hooks:
  enabled: true
  overrides:
    context_summarizer.summarize_context:
      enabled: true
      timeout: 60.0
      config:
        summarization_trigger_tokens: 30000
        llm_profile: "normal"
```

## Example Output

**Before Summarization (50 messages, ~60K tokens):**
```
[system] You are a helpful assistant
[user] Question 1...
[assistant] Answer 1...
... (46 more messages)
[user] Recent question
[assistant] Recent answer
```

**After Summarization (~15K tokens):**
```
[system] You are a helpful assistant
[summary] [Summary of 46 messages from 10:00 to 11:30]
          The conversation covered project planning, technical discussions
          about architecture, and decisions on database choices...
[user] Recent question
[assistant] Recent answer
```

## Metadata

The plugin provides detailed metadata in hook results:

```python
{
  'summarization': {
    'original_message_count': 50,
    'summarized_message_count': 5,
    'messages_summarized': 46,
    'summary_count': 5,  # Number of summary chunks created
    'original_tokens': 60000,
    'new_tokens': 15000,
    'tokens_saved': 45000,
    'reduction_ratio': 0.75,  # 75% reduction
    'total_chunks': 5,
    'successful_chunks': 5,
    'failed_chunks': 0
  }
}
```

## Best Practices

1. **Set Appropriate Trigger**: Match `summarization_trigger_tokens` to your LLM's context window
2. **Preserve Enough Recent**: Keep enough recent messages for context continuity
3. **Choose Right LLM Profile**:
   - `fast`: Quick, cheaper summarization
   - `normal`: Better quality summaries
   - `advanced`: Best quality, slower, more expensive
4. **Monitor Reduction Ratio**: Adjust `min_summary_reduction` to ensure summaries are worthwhile
5. **Store Metadata in Development**: Enable `store_original_metadata` for debugging

## Performance Considerations

- **LLM Calls**: Each chunk requires an LLM call (can be slow/expensive)
- **Timeout**: Default 60s timeout (may need adjustment for large batches)
- **Chunk Size**: Larger chunks = fewer LLM calls but potentially lower quality
- **Order**: Runs after `context_engineer` to avoid unnecessary summarization

## Troubleshooting

### Summaries Not Being Created

- Check that token count exceeds `summarization_trigger_tokens`
- Verify LLM is available in hook context
- Check logs for "insufficient_reduction" warnings

### Summary Quality Issues

- Try a better LLM profile (`normal` or `advanced`)
- Adjust `summary_prompt_template` for better instructions
- Reduce `summarization_chunk_size` for more focused summaries

### Performance Too Slow

- Use `fast` LLM profile
- Increase `summarization_chunk_size` to reduce LLM calls
- Increase `summarization_trigger_tokens` to summarize less often

## Testing

Run tests with:
```bash
pytest tests/test_plugin_context_summarizer.py -v
```

## Related Plugins

- **context_engineer**: Layered context compaction (runs first)
- **message_validator**: Message format validation
- **request_logger**: Logging of agent lifecycle events
