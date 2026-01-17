# ComfyUI Plugin

Integration plugin for [ComfyUI](https://github.com/comfyanonymous/ComfyUI) - a powerful and modular stable diffusion GUI and backend.

## Features

- **Workflow Execution**: Execute pre-configured ComfyUI workflows with parameters
- **Job Management**: Track job status, history, and outputs
- **Synchronous Waiting**: Built-in `wait_for_completion` operation eliminates manual polling
- **Web UI**: Monitor jobs via built-in web interface at `/comfyui/ui`
- **CLI Tool**: Command-line interface for quick workflow execution
- **Agent Integration**: Full agent support with streaming status updates

## Prerequisites

1. **ComfyUI Server**: Must be running and accessible
   - Default: `http://127.0.0.1:8188`
   - Installation: https://github.com/comfyanonymous/ComfyUI#installing

2. **Models**: Download required models (Stable Diffusion, VAE, etc.)
   - Configure paths in ComfyUI's `extra_model_paths.yaml`
   - See "Model Configuration" section below

3. **Workflow Files**: Export workflows from ComfyUI using "Save (API Format)"
   - Save to `config/comfyui_workflows/`

## Installation

### 1. Start ComfyUI Server

```bash
cd /path/to/ComfyUI
python main.py --listen 127.0.0.1 --port 8188
```

### 2. Configure Model Paths

Edit ComfyUI's `extra_model_paths.yaml`:

```yaml
base_path: F:/ComfyUI_Models/models/

comfy_extra_models:
  checkpoints: checkpoints/      # SDXL, SD1.5 models
  vae: vae/
  loras: loras/
  upscale_models: upscale_models/
  embeddings: embeddings/
  clip: clip/
```

**Important**: Do not double "models" in the path (e.g., `checkpoints: checkpoints/`, not `checkpoints: models/checkpoints/`).

### 3. Enable Plugin

In `config/plugins.yaml`:

```yaml
plugins:
  servers:
    comfyui:
      type: comfyui
      enabled: true
      host: "127.0.0.1"
      port: 8188
      timeout_seconds: 300
      unknown_threshold_seconds: 60  # Fail early if job stays unknown (lost/dropped)
      output_dir: "data/comfyui/outputs"
      workflow_files_dir: "config/comfyui_workflows"
      workflows:
        - id: sd15_txt2img
          name: "SD 1.5 Text-to-Image"
          description: "Generate images using Stable Diffusion 1.5"
          category: image_generation
          workflow_file: sd15_txt2img.json
          parameters:
            - name: prompt
              type: string
              required: true
              description: "Text description of the image"
              node_id: "6"
              field: inputs.text
            - name: negative_prompt
              type: string
              required: false
              default: ""
              node_id: "7"
              field: inputs.text
            - name: width
              type: integer
              default: 512
              node_id: "5"
              field: inputs.width
            - name: height
              type: integer
              default: 512
              node_id: "5"
              field: inputs.height
            - name: seed
              type: integer
              default: -1
              description: "Random seed (-1 for random)"
              node_id: "3"
              field: inputs.seed
```

### Workflow Configuration Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique identifier for the workflow |
| `name` | Yes | Human-readable name |
| `description` | No | Description shown in list |
| `category` | No | Group workflows (image_generation, audio, video, tts) |
| `workflow_file` | Yes | JSON filename in workflow_files_dir |
| `parameters` | No | Array of configurable parameters |

### Parameter Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Parameter name (used in API calls) |
| `type` | No | string, integer, float, boolean |
| `required` | No | Whether parameter is required |
| `default` | No | Default value if not provided |
| `description` | No | Shown in workflow list |
| `node_id` | Yes | ComfyUI node ID to modify |
| `field` | Yes | Dot-path to field (e.g., `inputs.text`) |

## Usage

### Tool: comfyui_workflow

The plugin exposes a single tool with multiple operations:

#### List Available Workflows

```json
{
  "operation": "list",
  "category": "image_generation"  // optional filter
}
```

Response:
```json
{
  "workflows": [
    {
      "id": "sd15_txt2img",
      "name": "SD 1.5 Text-to-Image",
      "description": "...",
      "category": "image_generation",
      "parameters": [...]
    }
  ],
  "count": 1
}
```

#### Execute a Workflow

```json
{
  "operation": "execute",
  "workflow_id": "sd15_txt2img",
  "parameters": {
    "prompt": "a beautiful sunset over mountains, oil painting",
    "negative_prompt": "blurry, low quality",
    "width": 768,
    "height": 512
  }
}
```

Response:
```json
{
  "status": "queued",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "prompt_id": "abc123-def456",
  "workflow_id": "sd15_txt2img",
  "workflow_name": "SD 1.5 Text-to-Image"
}
```

#### Wait for Completion (Recommended)

**New in v1.0**: Synchronous waiting with internal polling eliminates manual status checks.

```json
{
  "operation": "wait_for_completion",
  "prompt_id": "abc123-def456",
  "poll_interval": 2     // optional, default: 2s
}
```

**Note**: Timeout is configured via plugin config (`timeout_seconds`), not per-request.

**Lost Job Detection**: If a job stays in "unknown" status (not in queue, not in history) for `unknown_threshold_seconds` (default: 60s), it fails immediately rather than waiting for the full timeout. This handles jobs that were lost/dropped by ComfyUI.

Response (success):
```json
{
  "status": "completed",
  "prompt_id": "abc123-def456",
  "elapsed_seconds": 45.2
}
```

Response (timeout):
```json
{
  "status": "timeout",
  "prompt_id": "abc123-def456",
  "elapsed_seconds": 300.0,
  "error": "Job did not complete within 300 seconds"
}
```

Response (failed):
```json
{
  "status": "failed",
  "prompt_id": "abc123-def456",
  "error": "Node execution error: ...",
  "elapsed_seconds": 12.3
}
```

**Benefits**:
- No manual polling loop needed
- Status updates sent to LLM every 10 seconds
- Handles cancellation gracefully
- Returns immediately on completion or error

#### Check Job Status (Manual)

```json
{
  "operation": "status",
  "prompt_id": "abc123-def456"
}
```

Response:
```json
{
  "status": "running",
  "prompt_id": "abc123-def456",
  "workflow_id": "sd15_txt2img",
  "submitted_at": "2024-01-15T10:30:00Z",
  "started_at": "2024-01-15T10:30:05Z",
  "position": 0
}
```

Status values: `queued`, `running`, `completed`, `failed`, `cancelled`

#### Get Results

```json
{
  "operation": "result",
  "prompt_id": "abc123-def456",
  "download": true,
  "output_prefix": "sunset"
}
```

Response:
```json
{
  "status": "completed",
  "prompt_id": "abc123-def456",
  "outputs": {
    "images": [
      {
        "filename": "ComfyUI_00001_.png",
        "local_path": "data/comfyui/outputs/sunset_ComfyUI_00001_.png"
      }
    ],
    "audio": [],
    "video": []
  },
  "total_files": 1
}
```

#### Check Server Status

```json
{
  "operation": "server_status"
}
```

#### View Queue

```json
{
  "operation": "queue"
}
```

#### Cancel a Job

```json
{
  "operation": "cancel",
  "prompt_id": "abc123-def456"
}
```

## CLI Tool

### Installation

The CLI tool is installed automatically with the plugin:

```bash
.venv/Scripts/comfyui --help
```

### Commands

#### Execute Workflow

```bash
# Basic execution (returns job_id)
.venv/Scripts/comfyui execute sdxl_txt2img --prompt "a red apple on a table"

# With all parameters
.venv/Scripts/comfyui execute sdxl_txt2img \
  --prompt "a red apple" \
  --negative-prompt "blurry, low quality" \
  --width 1024 \
  --height 1024 \
  --steps 30 \
  --seed 42

# Wait for completion (synchronous)
.venv/Scripts/comfyui execute sdxl_txt2img \
  --prompt "sunset over mountains" \
  --wait
```

#### Check Status

```bash
# Job status
.venv/Scripts/comfyui status <job_id>

# Server status
.venv/Scripts/comfyui status --server
```

#### List Jobs

```bash
# Recent jobs
.venv/Scripts/comfyui jobs --limit 10

# Filter by status
.venv/Scripts/comfyui jobs --status completed
```

#### List Workflows

```bash
.venv/Scripts/comfyui list

# Filter by category
.venv/Scripts/comfyui list --category image
```

## Web Monitor

Access the monitoring panel at:
```
http://localhost:8000/comfyui/ui
```

Features:
- Server status indicator (online/offline)
- Active jobs with real-time updates
- Recent completed jobs with outputs
- Job statistics and history
- Cancel functionality
- Output file preview and download

## Agent Integration

### Three-Step Pattern (Manual)

```python
# 1. Execute workflow
execute_result = await agent.call_tool("comfyui_workflow", {
    "operation": "execute",
    "workflow_id": "sdxl_txt2img",
    "parameters": {
        "prompt": "a red apple",
        "steps": 30
    }
})
job_id = execute_result["job_id"]

# 2. Wait for completion (internal polling, timeout from plugin config)
wait_result = await agent.call_tool("comfyui_workflow", {
    "operation": "wait_for_completion",
    "prompt_id": job_id
})

# 3. Get result
result = await agent.call_tool("comfyui_workflow", {
    "operation": "result",
    "job_id": job_id
})
```

### Recommended Pattern (Synchronous)

Use `wait_for_completion` for clean, synchronous execution:

```python
# Execute and wait in one step (timeout from plugin config)
result = await agent.call_tool("comfyui_workflow", {
    "operation": "wait_for_completion",
    "prompt_id": job_id,
    "poll_interval": 2
})

# Result immediately available if successful
if result["status"] == "completed":
    final_result = await agent.call_tool("comfyui_workflow", {
        "operation": "result",
        "job_id": result.get("job_id", job_id)
    })
```

## Example: Image Generation Agent

Create an agent that can generate images (`config/agents/comfyui_image_agent.yaml`):

```yaml
# config/agents/image_gen_agent.yaml
image_gen_agent:
  type: basic_agent
  enabled: true
  description: "Generate images using ComfyUI"
  agent_config:
    llm_profile: chat
    system_prompt: |
      You are an image generation assistant. You can create images using
      various Stable Diffusion models via ComfyUI.
      
      Available operations:
      - List workflows: operation='list'
      - Generate image: operation='execute', workflow_id='...', parameters={...}
      - Check status: operation='status', prompt_id='...'
      - Get results: operation='result', prompt_id='...'
      
      Always check the workflow list first to understand available options.
    tools:
      allowed:
        - "comfyui/*"
```

## Workflow File Tips

### Exporting from ComfyUI

1. Build your workflow in ComfyUI
2. Click the hamburger menu (☰)
3. Select "Save (API Format)"
4. Save to `config/comfyui_workflows/`

### Finding Node IDs

In ComfyUI, node IDs are shown in the top-left of each node (e.g., "#3"). These correspond to keys in the exported JSON.

Example workflow JSON structure:
```json
{
  "3": {
    "inputs": {
      "seed": 123456,
      "steps": 20,
      "cfg": 7.5
    },
    "class_type": "KSampler"
  },
  "6": {
    "inputs": {
      "text": "a photo of a cat"
    },
    "class_type": "CLIPTextEncode"
  }
}
```

## Troubleshooting

### ComfyUI Server Not Responding

```json
{
  "operation": "server_status"
}
```

If offline:
1. Check ComfyUI is running
2. Verify host/port in config
3. Check firewall settings

### Workflow Execution Fails

1. Test workflow manually in ComfyUI first
2. Check node IDs match between config and JSON
3. Verify required models are installed
4. Check ComfyUI logs for errors

### Missing Outputs

1. Check workflow completed successfully (operation='status')
2. Verify output nodes (SaveImage, etc.) are present
3. Check `data/comfyui/outputs/` directory

## File Structure

```
src/plugins/comfyui/
├── __init__.py           # Plugin exports
├── plugin.yaml           # Plugin metadata
├── schema.yaml           # Tool schema
├── server.py             # MCP server implementation
├── comfyui_client.py     # HTTP client for ComfyUI API
├── job_tracker.py        # SQLite job persistence
└── templates/
    └── monitor.html      # Web monitoring UI

config/comfyui_workflows/
├── README.md             # Workflow setup guide
└── *.json                # Exported workflow files

data/comfyui/
├── jobs.db               # Job tracking database
└── outputs/              # Downloaded output files
```

## API Reference

### HTTP Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/plugins/comfyui/` | GET | Monitor panel HTML |
| `/plugins/comfyui/jobs` | GET | List all jobs |
| `/plugins/comfyui/jobs/{id}` | GET | Get job details |
| `/plugins/comfyui/jobs/{id}/cancel` | POST | Cancel job |
| `/plugins/comfyui/workflows` | GET | List workflows |
| `/plugins/comfyui/stats` | GET | Get statistics |

## See Also

- [ComfyUI Documentation](https://github.com/comfyanonymous/ComfyUI)
- [Plugin Authoring Guide](../../docs/plugin_authoring.md)
- [Batch Monitor Plugin](../batch_monitor/README.md)
