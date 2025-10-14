# Context Summarizer Plugin

**Type:** Schema-based Hook Plugin  
**Hook Type:** `pre_llm_call`  
**Pattern:** SchemaBasedPluginHook

## Overview

The Context Summarizer Plugin intelligently reduces conversation context size by using an LLM to create concise summaries of older messages. Unlike simple truncation strategies, this plugin preserves key information, decisions, and context while significantly reducing token usage.

## Features

- **Intelligent Summarization**: Uses LLM to create meaningful summaries instead of truncating
- **Configurable Trigger**: Only activates when context exceeds a token threshold
- **Chunked Processing**: Summarizes messages in configurable batch sizes
- **Preservation Logic**: 
  - Always preserves system messages
  - Always preserves recent N messages
  - Only summarizes older conversation history
- **Quality Control**: Validates that summaries achieve minimum reduction ratio
- **Audit Trail**: Optionally stores original messages in metadata
- **Metadata Tracking**: Detailed statistics on summarization results

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

The plugin is configured to run **after** `context_optimizer`:

```yaml
hooks:
  - name: summarize_context
    type: pre_llm_call
    order:
      after: ["optimize_context"]  # Run after basic optimization
      before: ["validate_messages"]
```

This ensures basic optimizations (duplicate removal, truncation) happen first, then intelligent summarization if still needed.

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
- **Order**: Runs after `context_optimizer` to avoid unnecessary summarization

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

## Integration with Context Optimizer

This plugin works in tandem with `context_optimizer`:

1. **context_optimizer** runs first:
   - Removes duplicate messages
   - Truncates overly long messages
   - Basic token limit enforcement

2. **context_summarizer** runs second:
   - If context still exceeds threshold after optimization
   - Creates intelligent summaries of older messages
   - Preserves information while reducing size

## Related Plugins

- **context_optimizer**: Basic context optimization (truncation, deduplication)
- **message_validator**: Message format validation
- **request_logger**: Logging of agent lifecycle events
