# Batch Monitor Plugin

Shows what the LLM batch system is doing: requests waiting to be batched, the jobs running at the providers, and the
totals since the batch manager started. Read-only — it gives agents no tools and changes nothing.

## Configuration

```yaml
# config/plugins.yaml
plugins:
  servers:
    batch_monitor:
      type: batch_monitor
      enabled: true
```

Nothing else to set. The plugin reads the batch queue manager, which exists once batch processing is configured in
`config/llm.yaml` (`batch:` block, see [LLM Batch API](../../docs/llm_batch_api.md)).

## The panel

Open **Batch Queues** from the panel launcher or the command palette (category *System*), or directly at
`/plugins/batch_monitor/`. It refreshes every five seconds from the start; the toolbar pauses that, refreshes at once,
and shows the time of the last update.

- **Stats**: queues listed, jobs running (every job not yet finished, including one not submitted yet), jobs and
  requests completed and failed since the manager started, and the mean processing time of a job with its minimum and
  maximum.
- **Table**: one row per job, running or finished within the last three minutes, with its status, progress (completed
  of all requests, and how many failed), estimated input tokens and time since submission (for a finished job: until it finished). The job ID's tooltip is the
  provider's ID. A queue with requests collected for its next job gets a *pending* row with their number and tokens;
  its time is the collection window after which they are submitted.
- **Batch processing is off** when there is no batch manager; **could not be loaded** with the server's message when a
  call fails. Neither keeps what was shown before.

## Endpoints

| Route | Answer |
|---|---|
| `GET /plugins/batch_monitor/` | the panel |
| `GET /plugins/batch_monitor/queues` | `{queues: [{queue_key, pending_requests, pending_estimated_tokens, jobs: [{job_id, provider_job_id, status, total_requests, completed_count, failed_count, elapsed_seconds, estimated_input_tokens}]}], collection_window_seconds}` — queues sorted by key; a queue with neither pending requests nor jobs is left out |
| `GET /plugins/batch_monitor/metrics` | `{completed_jobs, failed_jobs, total_completed_requests, total_failed_requests, processing_time: {min, max, mean}}` — seconds, `null` before the first is recorded |

Both JSON routes answer **503** when batch processing is not enabled.

## Tests

`tests/test_plugin_batch_monitor_panel.py` drives the panel in headless Chromium against the real plugin and a seeded
batch queue manager (`tests/panel_tests.html` holds the checks); `tests/test_plugin_batch_monitor.py` covers discovery,
routes and both answers without a browser.
