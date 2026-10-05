# Batch Monitor

Shows what the server's LLM batch processing is doing: the requests waiting to be sent as a batch job, the jobs
running at the providers, those finished in the last three minutes, and the job and request totals since the server
started. Read-only -- it gives agents no tools and changes nothing.

- **Panel** Batch Queues (category *System*) -- the totals and processing times, and a table of every job and every
  queue with requests waiting, with status, progress, estimated input tokens and time; reloads every 5 seconds.

Enable it in `config/plugins.yaml` (`batch_monitor: {type: batch_monitor, enabled: true}`). It has something to show
only when `config/llm.yaml` sets batch processing up (a `batch:` block with an enabled provider and a model with
`provider: batch`); otherwise the panel says batch processing is off.

The full manual -- the panel's figures and columns, the two calls behind it, the setup and what the panel cannot
see -- is the plugin's guide, `batch_monitor.guide`, in the Help panel.
