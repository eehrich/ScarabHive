# LLM Batch API Support

## Overview

AgentSystem supports batch processing for OpenAI and Gemini LLM APIs, providing:
- **50% cost reduction** compared to synchronous API calls
- **Separate rate limits** from sync APIs
- **Automatic request grouping** by model and provider
- **Asynchronous result distribution** to waiting callers

## How It Works

1. **Collection Phase**: Requests are collected during a configurable time window (default: 60s)
2. **Submission Phase**: Collected requests are submitted as a batch job
3. **Polling Phase**: Job status is polled until completion (up to 24 hours)
4. **Distribution Phase**: Results are matched to original requests and returned

## Configuration

Add batch configuration to `config/llm.yaml`:

```yaml
models:
  gpt-4o-batch:
    provider: openai
    api_key: ${OPENAI_API_KEY}
    model: gpt-4o
    batch:
      enabled: true
      collection_window_seconds: 60.0
      max_requests_per_batch: 1000
      poll_interval_seconds: 30.0
      max_wait_hours: 24.0
      cancel_on_startup: true
      fallback_to_sync: true
      storage_path: data/batch/
  
  gemini-flash-batch:
    provider: gemini
    api_key: ${GOOGLE_API_KEY}
    model: gemini-2.0-flash-001
    batch:
      enabled: true
      collection_window_seconds: 60.0
      max_requests_per_batch: 5000
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `false` | Enable batch processing for this model |
| `collection_window_seconds` | float | `60.0` | Time to collect requests before submitting |
| `max_requests_per_batch` | int | `1000` | Max requests per batch (triggers early submission) |
| `poll_interval_seconds` | float | `30.0` | How often to check batch status |
| `max_wait_hours` | float | `24.0` | Max time to wait for batch completion |
| `cancel_on_startup` | bool | `true` | Cancel pending batches on system startup |
| `fallback_to_sync` | bool | `true` | Fall back to sync API if batch fails |
| `storage_path` | str | `data/batch/` | Path for temporary batch files |

## API Reference

### BatchQueueManager

Central manager for batch request queuing and distribution.

```python
from agent_system.llm.batch import BatchQueueManager

# Initialize
manager = BatchQueueManager(config)

# Register batch clients
manager.register_batch_client("openai", openai_client)
manager.register_batch_client("gemini", gemini_client)

# Start background tasks
await manager.start()

# Submit request (returns Future)
result = await manager.submit_request(
    model="gpt-4o",
    provider="openai",
    messages=[{"role": "user", "content": "Hello"}],
    tools=[...],  # optional
    custom_id="my-request-1",  # optional
    timeout=3600,  # optional, in seconds
)

# Get metrics
metrics = manager.get_metrics()
print(f"Completed: {metrics.completed_jobs} jobs")

# Stop
await manager.stop()
```

### OpenAIBatchClient

Client for OpenAI Batch API operations.

```python
from agent_system.llm.batch import OpenAIBatchClient

client = OpenAIBatchClient(api_key="sk-...")

# Submit batch
batch_id = await client.submit_batch(job, storage_path)

# Check status
status = await client.get_batch_status(batch_id)
# Returns: {"status": "completed", "output_file_id": "file-123"}

# Get results
results = await client.get_batch_results(job)

# Cancel
await client.cancel_batch(batch_id)

# Cleanup
await client.close()
```

### GeminiBatchClient

Client for Gemini Batch API operations.

```python
from agent_system.llm.batch import GeminiBatchClient

client = GeminiBatchClient(
    api_key="...",
    use_sdk=True,  # Use Google GenAI SDK if available
)

# Same API as OpenAIBatchClient
batch_id = await client.submit_batch(job, storage_path)
status = await client.get_batch_status(batch_id)
results = await client.get_batch_results(job)
```

### Data Models

```python
from agent_system.llm.batch import BatchRequest, BatchJob, BatchResult, BatchStatus

# Create a request
request = BatchRequest(
    request_id="uuid-...",
    custom_id="my-custom-id",  # For correlation
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}],
    tools=[...],  # Optional
)

# Create a job
job = BatchJob(
    job_id="uuid-...",
    provider="openai",
    model="gpt-4o",
    requests=[request],
)

# Check job state
if job.is_terminal:
    print(f"Job finished with status: {job.status}")

# Check status values
BatchStatus.PENDING       # Collecting requests
BatchStatus.SUBMITTED     # Sent to provider
BatchStatus.IN_PROGRESS   # Processing
BatchStatus.COMPLETED     # Done successfully
BatchStatus.FAILED        # Error occurred
BatchStatus.EXPIRED       # Exceeded max_wait_hours
BatchStatus.CANCELLED     # Manually cancelled
```

## OpenAI Batch API Details

OpenAI's Batch API uses JSONL format for input:

```json
{"custom_id": "req-1", "method": "POST", "url": "/v1/chat/completions", "body": {"model": "gpt-4o", "messages": [...]}}
{"custom_id": "req-2", "method": "POST", "url": "/v1/chat/completions", "body": {"model": "gpt-4o", "messages": [...]}}
```

The flow is:
1. Upload JSONL file to Files API
2. Create batch with file ID
3. Poll batch status
4. Download output file when complete
5. Parse JSONL results

Reference: https://platform.openai.com/docs/guides/batch

## Gemini Batch API Details

Gemini's Batch API accepts direct batch requests:

```json
{
  "requests": [
    {"customId": "req-1", "request": {"contents": [...]}},
    {"customId": "req-2", "request": {"contents": [...]}}
  ]
}
```

For small batches, results may be returned immediately. For larger batches, the API returns a job name for polling.

Reference: https://ai.google.dev/gemini-api/docs/batch

## Use Cases

### Bulk Content Analysis

Process thousands of documents overnight:

```python
# Configure batch model in llm.yaml
# Use high collection_window for maximum batching

results = []
for doc in documents:
    future = await manager.submit_request(
        model="gpt-4o-batch",
        messages=[{"role": "user", "content": f"Analyze: {doc}"}],
    )
    results.append(future)

# Wait for all results
responses = await asyncio.gather(*results)
```

### Cost-Sensitive Workflows

Route non-urgent requests to batch:

```python
async def process_request(message, urgent=False):
    if urgent:
        # Use sync API
        return await llm_client.chat(message)
    else:
        # Use batch API (50% cheaper)
        return await batch_manager.submit_request(
            model="gpt-4o-batch",
            messages=[message],
        )
```

### Research and Data Pipeline

Batch processing for research pipelines:

```python
# Submit all research queries
for query in research_queries:
    await manager.submit_request(
        model="gemini-flash-batch",
        messages=[{"role": "user", "content": query}],
        custom_id=f"research-{query.id}",
    )

# Results distributed as they complete
# Monitor via metrics
while manager.get_metrics().pending_requests > 0:
    print(f"Waiting: {manager.get_metrics().pending_requests}")
    await asyncio.sleep(60)
```

## Monitoring

### Metrics

```python
metrics = manager.get_metrics()

print(f"Total Jobs: {metrics.total_jobs}")
print(f"Completed: {metrics.completed_jobs}")
print(f"Failed: {metrics.failed_jobs}")
print(f"Pending Requests: {metrics.total_requests - metrics.completed_requests}")
print(f"Token Usage: {metrics.total_prompt_tokens} prompt, {metrics.total_completion_tokens} completion")
```

### Logging

The batch module uses standard Python logging:

```python
import logging
logging.getLogger("agent_system.llm.batch").setLevel(logging.DEBUG)
```

Key log messages:
- `BatchQueueManager initialized` - Manager started
- `Batch job submitted to openai as batch_123` - Job submitted
- `Batch job completed: 100 requests` - Job finished
- `Batch job expired after 86400s` - Job timed out

## Error Handling

### Batch Failures

```python
try:
    result = await manager.submit_request(...)
except RuntimeError as e:
    # Batch failed - check fallback_to_sync
    print(f"Batch failed: {e}")
```

### Timeout

```python
try:
    result = await manager.submit_request(
        ...,
        timeout=7200,  # 2 hours
    )
except asyncio.TimeoutError:
    print("Batch did not complete in time")
```

### Cancellation

```python
# Cancel all pending batches
cancelled = await client.cancel_all_pending_batches()
print(f"Cancelled {cancelled} batches")
```

## File Structure

```
src/agent_system/llm/batch/
├── __init__.py         # Module exports
├── models.py           # Data models (BatchRequest, BatchJob, etc.)
├── queue_manager.py    # Central queue manager
├── openai_batch.py     # OpenAI Batch API client
└── gemini_batch.py     # Gemini Batch API client

tests/llm/
└── test_batch.py       # Unit tests (26 tests)

config/
└── llm.yaml            # Batch configuration (per model)
```
