# LLM Usage Tracking

## Overview

The Agent System tracks token usage (prompt tokens, completion tokens, total tokens) from all LLM providers to enable cost monitoring, rate limiting, and usage analytics.

## Supported Providers

### OpenAI (Full Support)
- **Non-streaming**: Usage data included in response object via `resp.usage`
- **Streaming**: Usage data included in final chunk when `stream_options={'include_usage': True}` is set
- **Format**:
  ```python
  {
      "prompt_tokens": 10,
      "completion_tokens": 20,
      "total_tokens": 30
  }
  ```

### HTTPX Client (Full Support)
- **Non-streaming**: Usage data extracted from API response
- **Streaming**: Usage data extracted from SSE chunks and accumulated
- **Format**: Same as OpenAI (standardized format)

### Ollama (Full Support)
- **Non-streaming**: Usage data extracted from response when available
- **Streaming**: Usage data extracted from final chunk when `done=true`
- **Format** (native Ollama):
  ```python
  {
      "prompt_eval_count": 15,    # prompt tokens
      "eval_count": 25,            # completion tokens
      "total_duration": 1234567    # nanoseconds (optional)
  }
  ```
- **Normalized Format** (converted to standard):
  ```python
  {
      "prompt_tokens": 15,
      "completion_tokens": 25,
      "total_tokens": 40
  }
  ```

## Implementation

### OpenAI Client

#### Non-Streaming (`chat_tools`)
```python
# Usage extraction from response object
if hasattr(resp, 'usage'):
    if resp.usage:
        result["usage"] = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens
        }
```

#### Streaming (`chat_tools_streaming`)
```python
# Request usage in stream
opts = {
    "model": self.model,
    "messages": msgs,
    "stream": True,
    "stream_options": {"include_usage": True}  # ← Enable usage tracking
}

# Accumulate usage from chunks
accumulated_usage = None

async for chunk in stream:
    # Extract usage if available (appears in final chunk)
    if hasattr(chunk, 'usage') and chunk.usage:
        accumulated_usage = {
            "prompt_tokens": chunk.usage.prompt_tokens,
            "completion_tokens": chunk.usage.completion_tokens,
            "total_tokens": chunk.usage.total_tokens
        }

# Include in final result
final_result = {"assistant": assistant}
if accumulated_usage:
    final_result["usage"] = accumulated_usage

yield {"type": "final", **final_result}
```

### HTTPX Client

#### Non-Streaming (`_make_request_non_streaming`)
```python
# Extract usage from response data
usage = response_data.get("usage", {})
if usage:
    result["usage"] = usage
```

#### Streaming (`_make_request_streaming`)
```python
# Accumulate usage from SSE chunks
accumulated_usage = None

for line in response.iter_lines():
    chunk_data = json.loads(line[6:])  # Skip "data: "
    
    # Extract usage from chunk
    if "usage" in chunk_data:
        accumulated_usage = chunk_data["usage"]

# Include in final chunk
final_result = {"assistant": assistant}
if accumulated_usage:
    final_result["usage"] = accumulated_usage

yield {"type": "final", **final_result}
```

### Ollama Client

#### Non-Streaming (`chat_tools`)
```python
# Extract usage from response data
message = (data or {}).get("message") or {}
out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
# ... tool calls processing ...

# Build result with usage information
result = {"assistant": out}

# Extract usage metadata if available (Ollama format)
# Ollama provides: eval_count (completion tokens), prompt_eval_count (prompt tokens)
if "eval_count" in data or "prompt_eval_count" in data:
    usage = {}
    if "prompt_eval_count" in data:
        usage["prompt_tokens"] = data["prompt_eval_count"]
    if "eval_count" in data:
        usage["completion_tokens"] = data["eval_count"]
    if "prompt_eval_count" in data and "eval_count" in data:
        usage["total_tokens"] = data["prompt_eval_count"] + data["eval_count"]
    result["usage"] = usage

return result
```

#### Streaming (`chat_tools_streaming`)
```python
# Accumulate usage from final chunk
accumulated_usage = None

async for line in response.aiter_lines():
    chunk_data = json.loads(line)
    
    # Check if stream is done - final chunk may contain usage info
    if chunk_data.get("done"):
        # Extract usage metadata if available
        # Ollama provides: eval_count (completion tokens), prompt_eval_count (prompt tokens)
        if "eval_count" in chunk_data or "prompt_eval_count" in chunk_data:
            accumulated_usage = {}
            if "prompt_eval_count" in chunk_data:
                accumulated_usage["prompt_tokens"] = chunk_data["prompt_eval_count"]
            if "eval_count" in chunk_data:
                accumulated_usage["completion_tokens"] = chunk_data["eval_count"]
            if "prompt_eval_count" in chunk_data and "eval_count" in chunk_data:
                accumulated_usage["total_tokens"] = (
                    chunk_data["prompt_eval_count"] + chunk_data["eval_count"]
                )
        break

# Include in final result
final_result = {"assistant": assistant}
if accumulated_usage:
    final_result["usage"] = accumulated_usage

yield {"type": "final", **final_result}
```

## Testing

### OpenAI Client Tests

```python
@pytest.mark.asyncio
async def test_streaming_usage_tracking(self, openai_client):
    """Test that usage information is tracked and returned in streaming mode."""
    client, mock_instance = openai_client
    
    # Mock chunks with usage in final chunk
    chunks = [
        MockChunk(content="Hello"),
        MockChunk(content=" world"),
        MockChunk(usage=MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30))
    ]
    
    # Collect all events
    events = []
    async for event in client.chat_tools_streaming(messages, tools):
        events.append(event)
    
    # Verify final event has usage
    final_event = [e for e in events if e.get("type") == "final"][0]
    assert "usage" in final_event
    assert final_event["usage"]["prompt_tokens"] == 10
    assert final_event["usage"]["completion_tokens"] == 20
    assert final_event["usage"]["total_tokens"] == 30
```

### HTTPX Client Tests

```python
async def test_usage_tracking():
    """Test that usage data is properly tracked from SSE stream."""
    # Mock SSE response with usage in final chunk
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n',
        'data: [DONE]\n\n'
    ]
    
    # Verify usage is in result
    assert "usage" in result
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 20
    assert result["usage"]["total_tokens"] == 30
```

### Ollama Client Tests

```python
@pytest.mark.asyncio
async def test_chat_tools_usage_tracking():
    """Test that usage information is tracked in non-streaming mode."""
    # Mock response with usage metadata (Ollama format)
    mock_response.json.return_value = {
        "message": {"role": "assistant", "content": "Test response"},
        "prompt_eval_count": 12,  # prompt tokens
        "eval_count": 18,         # completion tokens
    }
    
    result = await client.chat_tools(messages, tools)
    
    # Verify usage is included and normalized to standard format
    assert "usage" in result
    assert result["usage"]["prompt_tokens"] == 12
    assert result["usage"]["completion_tokens"] == 18
    assert result["usage"]["total_tokens"] == 30  # 12 + 18

@pytest.mark.asyncio
async def test_streaming_usage_tracking():
    """Test that usage information is tracked from Ollama streaming."""
    # Mock streaming data with usage in final chunk
    streaming_data = [
        '{"message": {"content": "Hello"}, "done": false}\n',
        '{"message": {"content": " world"}, "done": false}\n',
        '{"done": true, "prompt_eval_count": 15, "eval_count": 25}\n',
    ]
    
    # Verify usage is in result (normalized to standard format)
    final_event = [e for e in events if e.get("type") == "final"][0]
    assert "usage" in final_event
    assert final_event["usage"]["prompt_tokens"] == 15
    assert final_event["usage"]["completion_tokens"] == 25
    assert final_event["usage"]["total_tokens"] == 40  # 15 + 25
```

## Usage Data Flow

```
LLM API Response
  ↓
Client (OpenAI/HTTPX/Ollama)
  ├─ Extract usage from response/chunk
  ├─ Normalize to standard format
  └─ Include in result dict
  ↓
Agent.run_events()
  ├─ Accumulate usage from all LLM calls
  └─ Track in context manager
  ↓
UsageTracker (plugin context_usage_tracker)
  ├─ Sum prompt_tokens, completion_tokens
  ├─ Calculate total_tokens
  └─ Store in data/context_usage_tracker/usage.db (SQLite)
  ↓
API Response
  └─ Include usage in metadata
```

## Standard Format

All clients normalize usage data to this format:

```python
{
    "usage": {
        "prompt_tokens": int,      # Input tokens consumed
        "completion_tokens": int,  # Output tokens generated
        "total_tokens": int        # Sum of both
    }
}
```

## Best Practices

### 1. Always Check for Usage

```python
# Good - defensive check
if "usage" in result:
    prompt_tokens = result["usage"]["prompt_tokens"]
    
# Bad - assumes usage is always present
prompt_tokens = result["usage"]["prompt_tokens"]  # May crash!
```

### 2. Handle Missing Usage Gracefully

```python
# Good - fallback to zero
usage = result.get("usage", {})
prompt_tokens = usage.get("prompt_tokens", 0)
completion_tokens = usage.get("completion_tokens", 0)

# Bad - no fallback
prompt_tokens = result["usage"]["prompt_tokens"]
```

### 3. Enable Usage Tracking in Streaming

```python
# OpenAI - Enable explicitly
opts = {
    "stream": True,
    "stream_options": {"include_usage": True}
}

# HTTPX - Automatically tracked from SSE chunks
# Ollama - Automatically tracked from final chunk
```

## Troubleshooting

### No Usage Data in Streaming

**Symptom**: `usage` field missing from final event

**Causes**:
1. **OpenAI**: `stream_options={'include_usage': True}` not set
2. **HTTPX**: SSE chunks don't contain usage data
3. **Ollama**: Final chunk (`done=true`) missing `eval_count` fields

**Fix**:
- **OpenAI**: Add `stream_options` to request options
- **HTTPX**: Verify API endpoint returns usage in SSE format
- **Ollama**: Check Ollama version supports usage metrics

### Incorrect Token Counts

**Symptom**: Usage numbers don't match API billing

**Causes**:
1. Multiple LLM calls not accumulated
2. Usage from sub-agents not included
3. Caching affecting token counts

**Fix**:
- Use the `context_usage_tracker` plugin (`UsageTracker`) to accumulate across calls
- Ensure sub-agent usage is propagated upward
- Consider cached responses may have zero prompt tokens

## Future Improvements

1. **Cost Calculation**: Convert tokens to USD based on model pricing
2. **Rate Limiting**: Throttle requests based on token usage
3. **Analytics**: Track usage trends over time
4. **Caching**: Detect and report cached vs non-cached usage
5. **Multi-Model**: Track usage per model type

## References

- [OpenAI Streaming Documentation](https://platform.openai.com/docs/api-reference/streaming)
- [Ollama API Documentation](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Context Usage Tracker](./caching_systems.md)
