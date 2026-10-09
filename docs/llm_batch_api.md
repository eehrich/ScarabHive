# LLM Batch API Support

## Overview

AgentSystem supports batch processing for OpenAI, Gemini and Anthropic LLM APIs, providing:
- **50% cost reduction** compared to synchronous API calls
- **Separate rate limits** from sync APIs
- **Automatic request grouping** by model and provider
- **Asynchronous result distribution** to waiting callers

## How It Works

1. **Collection Phase**: Requests are collected during a configurable time window (default: 10s)
2. **Submission Phase**: Collected requests are submitted as a batch job
3. **Polling Phase**: Job status is polled until completion (up to 24 hours)
4. **Distribution Phase**: Results are matched to original requests and returned

## Configuration

Batch processing is configured in `config/llm.yaml` with two parts:

### 1. Global Batch Configuration

The `batch:` section under `llm_system:` defines global settings per provider:

```yaml
llm_system:
  batch:
    storage_path: "data/batch_jobs"   # Where to store batch job data
    
    providers:
      gemini:
        enabled: true
        collection_window_seconds: 10
        max_requests_per_batch: 100
        poll_interval_seconds: 10
        max_wait_hours: 24
        max_retries: 3
        cancel_on_startup: true
        fallback_to_sync: false
        
      openai:
        enabled: true
        collection_window_seconds: 10
        max_requests_per_batch: 100
        poll_interval_seconds: 10
        max_wait_hours: 24
        max_retries: 3
        cancel_on_startup: true
        fallback_to_sync: false
```

### 2. Batch Model Definitions

Models that use batch processing have `provider: batch` and specify which batch API via `batch_provider`:

```yaml
  models:
    # Gemini batch model
    gemini-2-5-flash-batch:
      provider: batch               # Use batch processing
      batch_provider: gemini        # Use Gemini batch API
      model: gemini-2.5-flash
      api_key: "..."
      context_window: 200000
      capabilities:
        tools: true
        function_calling: true
        streaming: false            # Batch doesn't support streaming

    # OpenAI batch model
    gpt-5-mini-batch:
      provider: batch               # Use batch processing
      batch_provider: openai        # Use OpenAI batch API
      model: gpt-5-mini
      api_key: "..."
      context_window: 272000
      capabilities:
        tools: true
        function_calling: true
        streaming: false
```

### Configuration Options

#### Global Provider Settings (`batch.providers.*`)

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `true` | Enable this batch provider |
| `collection_window_seconds` | float | `10.0` | Time to collect requests before submitting |
| `max_requests_per_batch` | int | `100` | Max requests per batch (triggers early submission) |
| `poll_interval_seconds` | float | `10.0` | How often to check batch status |
| `max_wait_hours` | float | `24.0` | Max time to wait for batch completion |
| `max_retries` | int | `3` | Retries for server-side cancelled jobs |
| `cancel_on_startup` | bool | `true` | Cancel pending batches on system startup |
| `fallback_to_sync` | bool | `false` | Fall back to sync API if batch fails |

#### Model Settings

| Option | Type | Description |
|--------|------|-------------|
| `provider` | str | Must be `"batch"` for batch models |
| `batch_provider` | str | Which batch API: `"gemini"`, `"openai"`, `"openai_httpx"` or `"anthropic"` |
| `model` | str | The underlying model name |
| `api_key` | str | API key for the provider |

## API Reference

### BatchQueueManager

Central manager for batch request queuing and distribution.

```python
from agent_system.llm.batch import BatchQueueManager
from agent_system.config.models import BatchSystemConfig, BatchProviderConfig

# Initialize with global config
batch_config = BatchSystemConfig(
    storage_path="data/batch_jobs",
    providers={"openai": BatchProviderConfig(...)},
)
manager = BatchQueueManager(batch_system_config=batch_config)

# Register batch clients
manager.register_batch_client("openai", openai_client)
manager.register_batch_client("gemini", gemini_client)

# Start background tasks
await manager.start()

# Submit request (returns Future)
result = await manager.submit_request(
    model="gpt-4o",
    provider="openai",  # batch_provider from config
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
from plugins.llm_openai_compat.openai_batch import OpenAIBatchClient

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
from plugins.llm_gemini.gemini_batch import GeminiBatchClient

client = GeminiBatchClient(
    api_key="...",  # requires the google-genai SDK
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
        provider="openai",
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
            provider="openai",
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
        provider="gemini",
        messages=[{"role": "user", "content": query}],
        custom_id=f"research-{query.id}",
    )

# Results distributed as they complete
# Monitor via metrics
while manager.get_active_jobs() or any(manager.get_queue_sizes().values()):
    print(f"Waiting: {len(manager.get_active_jobs())} job(s) running")
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
- `Batch job <job_id> submitted to openai as batch_123` - Job submitted
- `Batch job <job_id> completed with status <status>, completed=N, failed=M` - Job finished (DEBUG)
- `Batch job <job_id> expired after 86400s` - Job timed out

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
├── base.py             # BatchProviderClient base class
├── models.py           # Data models (BatchRequest, BatchJob, etc.)
├── queue_manager/      # Central queue manager (BatchQueueManager)
│   ├── manager.py      # BatchQueueManager: start, job recovery, stop, metrics
│   ├── core.py         # Shared state, status reports, access to the batch clients
│   ├── clients.py      # BatchClientRegistry: which client serves which provider
│   ├── submission.py   # Queuing, collection window, batch submission, stalled queues
│   ├── polling.py      # Polling loop, job status, retries
│   └── outcome.py      # Results, job completion, cancellation
├── batch_client.py     # BatchLLMClient wrapper
├── initialization.py
└── job_tracker.py

src/plugins/llm_openai_compat/openai_batch.py   # OpenAI Batch API client
src/plugins/llm_gemini/gemini_batch.py          # Gemini Batch API client
src/plugins/llm_anthropic/anthropic_batch.py    # Anthropic Batch API client

tests/llm/
└── test_batch.py       # Unit tests

config/
└── llm.yaml            # Batch configuration (per model)
```
