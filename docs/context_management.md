# Context Window Management System

The AgentSystem includes a comprehensive context window management system that automatically handles token limits, provides warnings, and implements intelligent summarization strategies to maintain conversation continuity while staying within LLM context window constraints.

## Overview

The context management system consists of several key components:

- **ContextConfig**: Configuration management for all context-related settings
- **ContextManager**: Main orchestrator for context window monitoring and management
- **ConversationSummarizer**: Intelligent conversation summarization using LLM
- **TokenOptimizer**: Tool result compression and optimization

## Configuration

Context management is configured in `config/agent.yaml` under the `context_management` section:

```yaml
context_management:
  # Context window settings
  context_window: 128000  # Total available context window in tokens
  summarization_threshold: 102400  # Start summarizing at this token count (80% of context_window)
  
  # Warning levels (as percentage of context window)
  warning_thresholds:
    yellow: 0.7   # 70% - First warning
    orange: 0.85  # 85% - More urgent warning
    red: 0.95     # 95% - Critical warning
  
  # Context management strategy
  strategy: "SUMMARIZE_OLDEST"
  
  # Summarization settings
  summarization_ratio: 0.5  # Reduce conversation to 50% of original size
  preserve_recent_messages: 10  # Always keep last N messages intact
  max_summary_words: 500  # Maximum words in generated summary
  tool_result_preview_chars: 200  # Characters to show in tool result preview
  
  # Token optimization settings
  enable_compression: true
  compress_tool_results: true
  max_tool_result_tokens: 1000
```

## Context Management Strategies

The system supports four different strategies for handling context window limits:

### 1. TRUNCATE_OLDEST
**Description**: Simply removes the oldest messages from the conversation when the context window limit is approached.

**When to use**: 
- Simple use cases where conversation history is less important
- When you want predictable, fast context management
- For applications where only recent context matters

**Pros**:
- Fast and simple implementation
- Predictable behavior
- No LLM calls required

**Cons**:
- Loss of potentially important historical context
- Abrupt conversation discontinuity
- No semantic understanding of what's being removed

**Example behavior**: If you have 100 messages and need to reduce by 50%, it removes messages 1-50, keeping messages 51-100.

### 2. SUMMARIZE_OLDEST (Recommended)
**Description**: Uses an LLM to create intelligent summaries of older conversation portions while preserving recent messages intact.

**When to use**:
- Most conversational applications (default choice)
- When conversation history contains important context
- For complex, multi-turn conversations
- When you need to maintain conversation continuity

**Pros**:
- Preserves semantic meaning of conversation
- Maintains conversation continuity
- Intelligent compression based on content importance
- Keeps recent context fully intact

**Cons**:
- Requires additional LLM calls (cost/latency)
- Dependent on LLM summarization quality
- More complex implementation

**Example behavior**: 
1. Identifies older conversation portions to summarize
2. Creates intelligent summary preserving key decisions, facts, and context
3. Replaces original messages with concise summary
4. Keeps recent messages (configurable count) intact

### 3. SLIDING_WINDOW
**Description**: Maintains a fixed-size window of the most recent messages, continuously dropping older messages as new ones arrive.

**When to use**:
- Real-time applications with continuous message flow
- When you need consistent memory usage
- For applications where only recent context is relevant
- Chat applications with high message frequency

**Pros**:
- Consistent memory usage
- Good for real-time applications
- Simple and predictable
- Automatic maintenance

**Cons**:
- Fixed context size regardless of content importance
- May lose important historical context
- No semantic consideration of message importance

**Example behavior**: Maintains exactly the last N messages (e.g., 50 messages), always dropping the oldest when a new message arrives.

### 4. SMART_COMPRESSION
**Description**: Advanced compression that combines multiple techniques including semantic importance scoring, message clustering, and adaptive summarization.

**When to use**:
- Applications requiring maximum context retention
- Complex workflows with multiple conversation threads
- When conversation contains mixed content types (code, analysis, decisions)
- Advanced use cases where sophisticated context management is crucial

**Pros**:
- Maximum context retention efficiency
- Semantic understanding of content importance
- Adaptive to different conversation types
- Sophisticated handling of mixed content

**Cons**:
- Most computationally expensive
- Complex implementation
- Requires fine-tuning for optimal results
- Higher latency for context management operations

**Example behavior**:
1. Analyzes conversation for semantic clusters and importance
2. Preserves high-importance messages intact
3. Summarizes related message groups
4. Applies variable compression ratios based on content type
5. Maintains conversation flow and logical connections

## Warning System

The system provides three-tier warnings as token usage approaches limits:

### Warning Levels

- **Yellow (70%)**: First notification that context is filling up
- **Orange (85%)**: More urgent warning that action may be needed soon
- **Red (95%)**: Critical warning that context management will trigger soon

### Warning Behavior

Warnings are emitted as status events that can be consumed by:
- CLI applications (logged with appropriate severity)
- Web UI (displayed as notifications)
- API clients (included in event streams)

## Summarization Parameters

### max_summary_words
**Default**: 500 words
**Description**: Controls the maximum length of generated conversation summaries.

**Tuning guidance**:
- **Lower values (200-300)**: More aggressive compression, faster processing, may lose some detail
- **Higher values (700-1000)**: More detailed summaries, better context preservation, slower processing
- **Very high values (1000+)**: May defeat the purpose of summarization

### tool_result_preview_chars
**Default**: 200 characters
**Description**: When compressing tool results, this controls how many characters of the original result to preserve as a preview.

**Tuning guidance**:
- **Lower values (50-100)**: More aggressive compression, good for large tool outputs
- **Higher values (300-500)**: Better preservation of tool result details
- **Consider content type**: Code snippets may need more characters than simple text

## Integration and Usage

### Automatic Operation

The context management system operates automatically:

1. **Token Monitoring**: Continuously tracks conversation token count
2. **Warning Emission**: Sends notifications at configured thresholds
3. **Automatic Management**: Triggers summarization when threshold is reached
4. **Status Events**: Provides real-time feedback on operations

### Manual Triggering

Context management can also be triggered manually through the API or CLI when needed.

### Status Events

The system emits detailed status events during operation:

```json
{
  "type": "context_management",
  "phase": "START|PROGRESS|END|ERROR",
  "message": "Human-readable status message",
  "details": {
    "current_tokens": 54276,
    "context_window": 128000,
    "usage_percentage": 42.4,
    "strategy": "SUMMARIZE_OLDEST",
    "action": "summarizing_conversation"
  }
}
```

## Performance Considerations

### LLM Costs
- SUMMARIZE_OLDEST and SMART_COMPRESSION require additional LLM calls
- Summary generation uses the same LLM provider as the main conversation
- Consider using a smaller/cheaper model for summarization if needed

### Latency
- Summarization adds processing time when triggered
- TRUNCATE_OLDEST and SLIDING_WINDOW are faster alternatives
- Consider batching context management for high-frequency applications

### Memory Usage
- Context management operates on conversation history in memory
- Very long conversations may require significant memory for processing
- Consider periodic context management for long-running sessions

## Best Practices

### Strategy Selection
1. **Default choice**: Use SUMMARIZE_OLDEST for most applications
2. **High-frequency applications**: Consider SLIDING_WINDOW
3. **Simple use cases**: TRUNCATE_OLDEST may be sufficient
4. **Complex workflows**: Evaluate SMART_COMPRESSION

### Threshold Configuration
1. **Start conservative**: Begin with default thresholds and adjust based on usage
2. **Monitor warnings**: Track warning frequency to optimize thresholds
3. **Consider LLM context window**: Adjust `context_window` setting to match your LLM

### Parameter Tuning
1. **Test with typical conversations**: Use representative conversation patterns for testing
2. **Monitor summary quality**: Review generated summaries to ensure they preserve important context
3. **Adjust based on content**: Different applications may need different summarization parameters

## Troubleshooting

### Common Issues

**Summarization failing**:
- Check LLM client configuration
- Verify API keys and connectivity
- Check logs for specific error messages

**Context still growing after management**:
- Verify threshold configuration
- Check if recent message preservation is too high
- Review summarization ratio setting

**Poor summary quality**:
- Increase `max_summary_words` parameter
- Consider different summarization prompts
- Verify LLM model capabilities

**Infinite loop in conversation summarizer**:
- Check that summarizer has a dedicated LLM client (fixed in latest version)
- Verify `_summarization_in_progress` flag is functioning
- Monitor for recursive context management calls in logs
- Fallback to `TRUNCATE_OLDEST` strategy if loops persist

### Debugging

Enable debug logging to see detailed context management operations:

```yaml
logging:
  level: DEBUG
```

Monitor logs in `logs/api.log` for context management events and any errors.

## Architecture Notes

### Infinite Loop Prevention

The system includes safeguards to prevent infinite loops in context management:

1. **Dedicated LLM Client**: The conversation summarizer uses a separate LLM client that bypasses the agent's context management system, preventing recursive calls.

2. **Loop Detection Flag**: The `ContextManager` tracks active summarization operations with `_summarization_in_progress` flag and falls back to truncation if a recursive call is detected.

3. **Graceful Fallbacks**: If LLM summarization fails or is unavailable, the system automatically falls back to rule-based text extraction summarization.

These safeguards ensure that context management remains stable even under high-load conditions or configuration issues.

## Migration and Updates

When updating context management configuration:

1. **Test with non-production data first**
2. **Monitor warning frequency after changes**
3. **Review summary quality with new parameters**
4. **Consider gradual rollout for significant changes**

The system gracefully handles configuration updates and will use new settings for subsequent context management operations.