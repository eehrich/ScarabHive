# ComfyUI Plugin

Runs [ComfyUI](https://github.com/comfyanonymous/ComfyUI) workflows for agents: images, audio, video and text. Workflows
are exported from ComfyUI in API format and configured with the parameters an agent may set. Every job is recorded in a
local SQLite tracker; the **ComfyUI** panel shows the servers, the jobs and lets you cancel one.

## Configuration

```yaml
# config/plugins.yaml
plugins:
  servers:
    comfyui:
      type: comfyui
      enabled: true
      host: "127.0.0.1"               # primary server: used when `servers` is not set
      port: 8188
      servers:                        # optional: several ComfyUI instances, load-balanced
        - {host: "192.168.1.5", port: 8188}
        - {host: "192.168.1.5", port: 8189}
      strategy: least_loaded          # or random
      timeout_seconds: 300
      unknown_threshold_seconds: 60   # a job neither queued, running nor in the history this long is failed
      output_dir: "data/comfyui/outputs"          # may contain {session_id} to keep sessions apart
      workflow_files_dir: "config/comfyui_workflows"
      cleanup_age_hours: 48           # outputs older than this are deleted; 0 keeps them
      upload_source_dirs:             # the only roots upload_image may read from
        - "data/comfyui"
        - "data/writer/assets"
      workflows:
        - id: sd15_txt2img
          name: "SD 1.5 Text-to-Image"
          description: "Generate images using Stable Diffusion 1.5"
          category: image_generation
          workflow_file: sd15_txt2img.json
          parameters:
            - {name: prompt, type: string, required: true, node_id: "6", field: inputs.text}
            - {name: seed, type: integer, default: -1, node_id: "3", field: inputs.seed}
```

The job tracker lives next to the output directory (`data/comfyui/jobs.db`). With several servers the first one is the
primary: `server_status`, `queue` and `upload_image` ask it, and a job is always followed up on the server it was
submitted to.

A workflow parameter names the node (`node_id`, the `#3` shown on the node in ComfyUI) and the dot path of the field it
sets (`field`). Export a workflow with *Save (API Format)* into `workflow_files_dir`.

## The tool

`comfyui_workflow` with an `operation`:

| Operation | What it does |
|---|---|
| `list` | configured workflows with their parameters, optionally one `category` |
| `execute` | queue `workflow_id` with JSON-encoded `parameters` on the least loaded server; answers the `prompt_id` |
| `wait_for_completion` | poll until the job is finished, failed, lost (`unknown_threshold_seconds`) or timed out |
| `status` | the job's live status |
| `result` | download the outputs (`download`, `output_prefix`), with `include_content` also attach them for the model |
| `load` | attach an output generated before (`file_path` or `filename`) |
| `server_status`, `queue` | the primary server's state and queue |
| `cancel` | remove a queued job from its server's queue or interrupt it while it runs |
| `upload_image` | upload a local image from `upload_source_dirs` into ComfyUI's input folder |

`cancel` answers `cancelled`, `already_finished` (with the job's `job_status`, which the tracker takes over) or `error`:
a job that is neither queued, running nor in the history is not interrupted, as an interrupt stops whichever job runs.

## The panel

Open **ComfyUI** from the panel launcher or the command palette (category *Agents*), or directly at
`/plugins/comfyui/`. It refreshes every five seconds from the start; the toolbar pauses that, refreshes at once, and
shows the time of the last update.

- **Servers**: every configured server, online with how many jobs it runs and has queued, or offline with the reason.
- **Stats**: jobs active (queued or running), completed, failed, cancelled, and all tracked.
- **Active jobs**: each job still to finish with its workflow, status, submission time and how long it has been running
  (or waiting). **Cancel** asks first, then cancels on the job's server; a refusal of the server or a job that finished
  in the meantime is shown as an error, and the list is loaded anew either way.
- **Recently finished**: the last ten finished jobs with their outcome, error, finish time, duration and the number of
  outputs by kind.
- Before listing, each load brings the tracker up to date with the servers: a job a server runs is marked running, one
  in its history completed or failed, one found nowhere failed as lost. A job on a server that cannot be reached is left
  as it was.
- **Jobs could not be loaded** with the server's message when the call fails; nothing shown before stays.

## Endpoints

| Route | Answer |
|---|---|
| `GET /plugins/comfyui/` | the panel |
| `GET /plugins/comfyui/jobs` | `{servers: [{address, online, queue_pending, queue_running, error}], active: [job], recent: [job], stats: {total, queued, running, pending, completed, failed, cancelled}}` with `job` = `{prompt_id, workflow, status, submitted_at, completed_at, seconds, error, outputs: {kind: count}}`; `seconds` is the time since the job started (or was submitted) for an active job, its duration for a finished one, `null` if never recorded |
| `POST /plugins/comfyui/jobs/{prompt_id}/cancel` | the client's answer, `{status: "cancelled", prompt_id, ...}`; **404** for a job not tracked, **409** for one already finished, **502** when the server refuses or cannot be reached |

## Tests

- `tests/test_plugin_comfyui_panel.py` drives the panel in headless Chromium against the real plugin, with two ComfyUI
  servers answered in memory where the client opens its HTTP sessions (`tests/panel_tests.html` holds the checks).
- `tests/test_comfyui_cancel.py`: what a cancel records in the tracker, what the client sends, how the panel's route
  answers a refusal.
- `tests/test_comfyui.py`, `tests/test_comfyui_autoupload.py`, `tests/test_comfyui_status_events.py`: client, tracker,
  tool operations and their status lines.
