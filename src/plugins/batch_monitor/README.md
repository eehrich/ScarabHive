# Batch Monitor Plugin

Real-time monitoring interface for LLM Batch API queues.

## Features

- **Real-time Queue Status**: Monitor all active batch queues
- **Job Progress Tracking**: See completion progress and elapsed time
- **Auto-refresh**: Optional 5-second auto-refresh
- **System Metrics**: Track completed/failed jobs across all queues
- **Visual Status Indicators**: Color-coded status badges with animations

## Usage

### Accessing the Panel

1. **Via the Launcher**: grid button in the header (or Ctrl+K) → "Batch Queues"
2. **Direct URL**: Navigate to `/plugins/batch_monitor/`

### UI Overview

```
┌─ Batch Queue Monitor ─────────────────────────┐
│ ⚡ Batch Queue Monitor                         │
│ [🔄 Refresh] [✓ Auto-refresh (5s)]           │
│                                                │
│ Total Queues: 2  Active Jobs: 1               │
│ Total Completed: 45  Total Failed: 2          │
│                                                │
│ ┌─ gemini:gemini-2.5-flash ──────────────┐   │
│ │ Status: ● in_progress                   │   │
│ │ Job: abf2d477...  Progress: 12/15       │   │
│ │ Elapsed: 2m 15s                         │   │
│ └─────────────────────────────────────────┘   │
│                                                │
│ ┌─ openai:gpt-4o ─────────────────────────┐   │
│ │ Status: ○ idle                          │   │
│ │ Pending: 3 requests  Next: ~60s         │   │
│ └─────────────────────────────────────────┘   │
└────────────────────────────────────────────────┘
```

### Status Indicators

| Status | Icon | Description |
|--------|------|-------------|
| `idle` | ○ | No activity |
| `pending` | ◐ | Requests collecting |
| `submitted` | ◑ | Batch submitted to provider |
| `in_progress` | ● | Processing (animated) |
| `completed` | ✓ | Successfully finished |
| `failed` | ✗ | Error occurred |

## API Endpoints

The plugin exposes the following REST endpoints:

### GET `/plugins/batch_monitor/`
Returns the monitoring panel HTML interface.

### GET `/plugins/batch_monitor/queues`
Returns JSON with all queue statuses.

**Response:**
```json
{
  "status": "success",
  "queue_count": 2,
  "queues": [
    {
      "queue_key": "gemini:gemini-2.5-flash",
      "status": "in_progress",
      "pending_requests": 0,
      "active_job": {
        "job_id": "abf2d477-...",
        "provider_job_id": "5j7hnh...",
        "status": "in_progress",
        "submitted_at": "2025-12-26T11:45:00Z",
        "elapsed_seconds": 135,
        "total_requests": 15,
        "completed_count": 12,
        "failed_count": 0
      }
    }
  ],
  "collection_window_seconds": 60,
  "poll_interval_seconds": 30
}
```

### GET `/plugins/batch_monitor/queues/{queue_key}`
Returns detailed status of a specific queue.

**Example:** `/plugins/batch_monitor/queues/gemini:gemini-2.5-flash`

### GET `/plugins/batch_monitor/metrics`
Returns overall batch system metrics.

**Response:**
```json
{
  "status": "success",
  "total_queues": 2,
  "total_submitted": 47,
  "total_completed": 45,
  "total_failed": 2,
  "collection_window_seconds": 60,
  "poll_interval_seconds": 30,
  "max_wait_hours": 24
}
```

## Configuration

Enable the plugin in `config/plugins.yaml`:

```yaml
plugins:
  servers:
    batch_monitor:
      type: batch_monitor
      enabled: true
      description: "Monitor and query batch queue status"
```

The plugin requires no additional configuration. It automatically discovers the batch queue manager if batch processing is enabled in `config/llm.yaml`.

## Architecture

```
┌─ Plugin Components ─────────────────────────┐
│                                              │
│  Web Panel (iframe)                         │
│  └─ templates/panel.html                    │
│     ├─ Auto-refresh UI                      │
│     ├─ Queue cards                          │
│     └─ Metrics summary                      │
│                                              │
│  API Endpoints                               │
│  └─ server.py (BatchMonitorWebFactory)      │
│     ├─ get_panel()                          │
│     ├─ get_queues()                         │
│     ├─ get_queue_detail()                   │
│     └─ get_metrics()                        │
│                                              │
│  Integration                                 │
│  └─ get_batch_queue_manager()               │
│     ├─ Reads from global manager            │
│     ├─ No coupling to core                  │
│     └─ Optional plugin                      │
└──────────────────────────────────────────────┘
```

## Design Decisions

### Why a Plugin?

- **Modularity**: Batch monitoring is optional, not core functionality
- **Separation of Concerns**: Queue manager stays in `llm/batch/`, UI is plugin
- **Discoverability**: Shows up in the panel launcher and the command palette
- **Maintainability**: Can be updated independently

### Why Not Agent Tools?

- Monitoring is a **human activity**, not agent task
- Real-time UI is better than status messages
- Agents don't need to query batch status programmatically

### Alternative Considered: Status Messages

**Rejected because:**
- Messages scroll away after agent completes
- Hard to track multiple queues simultaneously
- No persistent overview
- Poor UX for long-running batches

## Development

### File Structure

```
src/plugins/batch_monitor/
├── __init__.py           # Plugin exports
├── plugin.py             # PLUGIN_FACTORY
├── plugin.toml           # Plugin metadata
├── schema.yaml           # Web UI config
├── server.py             # Web endpoints
└── templates/
    └── panel.html        # Main UI
```

### Testing

```bash
# Start API server
.venv/Scripts/python -m uvicorn agent_system.app:build_app --factory

# Open panel
curl http://localhost:8000/plugins/batch_monitor/

# Test JSON endpoint
curl http://localhost:8000/plugins/batch_monitor/queues | jq
```

### Adding New Endpoints

1. Add endpoint definition to `schema.yaml`:
```yaml
web_ui:
  endpoints:
    - path: "/my-endpoint"
      method: "GET"
      handler: "my_handler"
      response_type: "json"
```

2. Implement handler in `server.py`:
```python
async def my_handler(self, request: Request) -> JSONResponse:
    return JSONResponse({"data": "..."})
```

Routes are auto-generated by `schema_router.py`.

## Troubleshooting

### "Batch System Unavailable"

**Cause:** Batch queue manager not initialized

**Solution:** Enable batch for at least one model in `config/llm.yaml`:

```yaml
models:
  gemini-flash-batch:
    provider: gemini
    model: gemini-2.0-flash-exp
    batch:
      enabled: true
      collection_window_seconds: 60
```

### "Not Found" Error

**Cause:** Plugin not loaded or server not restarted

**Solution:**
1. Check `config/plugins.yaml` has `batch_monitor` enabled
2. Restart API server
3. Verify plugin loaded: `.venv/Scripts/agent-cli plugins`

### Auto-refresh Not Working

**Cause:** Browser tab inactive or connection lost

**Solution:**
- Refresh page manually
- Check browser console for errors
- Verify API server is running

## Related Documentation

- [LLM Batch API](../../docs/llm_batch_api.md) - Batch system overview
- [Plugin Authoring](../../docs/plugin_authoring.md) - Plugin development guide
- [Schema-Based Web Routing](../../docs/schema_based_web_routing.md) - Auto-routing system

## License

Part of AgentSystem framework.
