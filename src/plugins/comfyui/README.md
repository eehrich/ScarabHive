# ComfyUI

Runs [ComfyUI](https://github.com/comfyanonymous/ComfyUI) workflows for agents: images, audio, video and text. A
workflow is exported from ComfyUI in API format and configured with the inputs an agent may set; the agent runs it,
waits for it, gets the files back and can look at them itself. With several ComfyUI servers each job goes to the least
busy one. Every job is recorded; the **ComfyUI** panel shows the servers and the jobs and cancels one.

- **Tool** `comfyui_workflow` -- one tool with the operations `list`, `execute`, `wait_for_completion`, `status`,
  `result`, `load`, `cancel`, `server_status`, `queue` and `upload_image`. Image inputs that name a local file are
  uploaded to the server that runs the job.
- **Panel** ComfyUI -- each server's state, the active and the recently finished jobs, cancel.
- **Agent** `comfyui_agent` -- generates media with the tool.

Enable it in `config/plugins.yaml` (`comfyui: {type: comfyui, enabled: true, host: ..., workflows: [...]}`) and allow
`+comfyui/*` in an agent's tool list.

The full manual -- the panel, every operation, parameter and answer, the server settings and how to configure a
workflow -- is the plugin's guide, `comfyui.guide`, in the Help panel.
