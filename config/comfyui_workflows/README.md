# ComfyUI workflows

The workflow files the `comfyui` plugin runs, in ComfyUI's API format. The plugin reads them from
`workflow_files_dir` (this folder by default), once per `execute`: a changed file needs no restart.

## Adding a workflow

1. Build the workflow in ComfyUI and test it there.
2. Export it with **Save (API Format)** and save the JSON file in this folder.
3. Name it, with the inputs an agent may set, in the `comfyui` entry of `config/plugins.yaml`:

```yaml
plugins:
  servers:
    comfyui:
      workflows:
        - id: my_workflow
          name: "My Custom Workflow"
          description: "What this workflow does"
          category: image_generation
          workflow_file: my_workflow.json   # without it: <id>.json
          parameters:
            - name: prompt
              type: string
              required: true
              node_id: "3"          # the number ComfyUI shows on the node
              field: inputs.text    # the dot path of the value in that node
```

A parameter left out of a call gets its `default`; without one the file keeps its own value. A `required`
parameter left out fails the call.

## More

- `WORKFLOWS_README.md` -- the shipped workflows and the model files and custom nodes each needs.
- The plugin's guide, `comfyui.guide` in the Help panel -- every parameter key, the tool and its answers.
